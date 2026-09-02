"""AD-1 / AD-6 / AD-16 — Le pipeline sinistre : les mêmes cinq étapes, un verdict au bout.

`comprendre → retrouver → rédiger → vérifier → restituer`, dans cet ordre, toujours — exactement la
chaîne du guide (AD-1 : « l'ordre est constant »). Le sinistre ne fabrique **aucune** étape nouvelle :
il donne aux étapes existantes des prompts dédiés, leur transmet les faits déclarés, et laisse
*vérifier* dériver `applicable` puis appliquer la table d'AD-6. Le contrat de sortie est l'unique
`Answer` d'AD-4, dont `verdict` est un champ — il n'y a pas de second objet de réponse.

Trois différences avec `guide.py`, et rien d'autre :

- **les faits** : `Faits` est une entrée de plein droit (AD-5 les nomme déjà), bornée par le domaine
  (`description ≤ 2 000` caractères) et rejetée, jamais tronquée ;
- **la recherche** : la variante `deterministe` reçoit `kinds_prioritaires` — à score égal, les blocs
  `garantie|exclusion|condition|franchise` passent devant (AC de la story 1.8). Ce départage est un
  tri *dans l'index* ; la variante `outils`, devenue le mode par défaut (AD-1, amendement du
  25/08/2026), ne classe pas : elle laisse le modèle choisir ses termes puis ses nœuds ;
- **le refus** : AD-16 interdit tout repli côté sinistre, et un refus sinistre porte donc un verdict
  `ne_tranche_pas` composé par le code, jamais un verdict vide. `ne_tranche_pas` n'est pas un repli :
  c'est le résultat d'une table qui n'a rien trouvé à trancher, et il est dit comme tel.

Comme le guide, le pipeline ne voit ni `corpus` ni `llm` (table des couches) : `corpus`, `index` et
`client` sont annotés `Any` et lui viennent de l'appelant (l'API, story 1.9).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from server.app.config import Settings
from server.app.domain.answer import (
    RAISONS_CORRIGEABLES,
    AbsenceProof,
    Answer,
    AnswerDraft,
    AnswerSegment,
    Claim,
    Lacune,
    Verification,
)
from server.app.domain.conversation import ConversationAction, ContinuationState, appliquer
from server.app.domain.errors import (
    ErrorCode,
    BudgetExceeded,
    # `TruncatedRead` n'est plus levée ici (story 4.2f) : voir le commentaire de `pipelines/guide.py`.
    CorpusUnavailable,
    InvalidRequest,
    LlmParse,
    LlmUnavailable,
    PipelineError,
    Timeout,
)
from server.app.domain.profil import Profil
from server.app.domain.question import ClarificationRequise, Faits, ParsedQuestion, QuestionScope
from server.app.domain.trace import (ETAPES_SANS_APPEL, CheckResult, StepTrace,
                                     Trace)
from server.app.domain.verdict import (
    KINDS_DECISIONNELS,
    KINDS_FONDATEURS,
    PORTEE,
    MissingPackage,
    Verdict,
    questions_du_paquet_manquant,
)
from server.app.pipelines.commun import (
    APPELS_DE_LA_RELANCE,
    INTENTS_REFUSES,
    blocs_cites,
    dictionnaire_de,
    digests,
    domine,
    gate_de,
    lecture_partielle_de,
    libelles_de_blocs,
    normaliser_langue_pipeline,
    relance_abandonnee,
    relance_utile,
    retrieval_budget,
)
from server.app.steps.comprendre import comprendre
from server.app.steps.rediger import rediger
from server.app.steps.restituer import REGISTRE_SINISTRE, restituer
from server.app.steps.retrouver import (couvrir_facettes, retrouver_deterministe,
                                        retrouver_outils, satisfaire_demande)
from server.app.steps.verifier import verifier

PIPELINE = "sinistre"
# AD-1, amendement du 25/08/2026 : « la navigation par outils est le mode par défaut de *retrouver* »,
# et la variante `deterministe` (index + ouverture groupée) « devient la baseline de comparaison des
# évals et le repli quand le budget de tours est épuisé ». Le sinistre sert donc la même variante que
# le guide, par la **même** fonction d'étape (`steps.retrouver.retrouver_outils`) et le même repli.
# Une variante inconnue reste refusée **avant** tout appel facturé plutôt que traitée comme l'une des
# deux (AD-16 : jamais de dégradé silencieux).
VARIANT = "outils"
VARIANT_DETERMINISTE = "deterministe"
VARIANTES = frozenset({VARIANT, VARIANT_DETERMINISTE})

# Ce que le refus d'un sinistre annonce, par `AbsenceProof.kind`. Composé par le **code**, comme les
# phrases de `restituer.PHRASES_DE_REFUS` : aucune de ces situations n'est une lecture du contrat.
RAISONS_DE_REFUS: dict[str, str] = {
    "hors_perimetre": "La demande ne porte pas sur ce que couvre un contrat d'assurance habitation",
    "zero_hit": "Aucune clause du contrat n'a été retrouvée sur les termes du sinistre décrit",
    "claims_rejetes": "Aucune clause citée n'a passé le contrôle des citations",
    "clarification_requise": "La demande n'a pas pu être rendue autonome : rien n'a été cherché",
}


# Ce que l'on dit quand `AbsenceProof.kind` n'est pas dans la table ci-dessus. AD-16 interdit le
# dégradé silencieux : retomber sur la phrase de `claims_rejetes` ferait affirmer au verdict qu'aucune
# clause n'a passé le contrôle des citations, alors que ce n'est *pas* ce qui s'est passé — un kind
# ajouté plus tard (AD-4 en a déjà gagné un en 1.5) mentirait sans que rien ne le signale.
RAISON_DE_REFUS_GENERIQUE = "Le dossier n'a pas pu être confronté aux clauses du contrat"


# Story 4.2e — ce que coûte la **reprise** d'une demande de contexte, en appels : *vérifier* une
# seconde fois, et rien d'autre. C'est la différence de nature avec la relance d'AD-3, qui en coûte
# deux (`APPELS_DE_LA_RELANCE`) parce qu'elle rédige avant de vérifier : la satisfaction, elle, est
# du code pur — `satisfaire_demande` n'appelle aucun modèle.
APPELS_DE_LA_REPRISE = 1
# La lacune typée qu'une demande de contexte restée ouverte laisse dans la réponse. Comme
# `LACUNE_RELANCE_ABANDONNEE`, c'est une cause, pas une phrase : *restituer* seul la projette dans la
# langue de la réponse.
LACUNE_CONTEXTE_NON_RELU = Lacune(kind="contexte_non_relu")


def _contexte_non_relu(verification: Verification, *, lecture_bornee: bool) -> Verification:
    """La demande de contexte n'a pas été satisfaite, ou sa reprise a été refusée (story 4.2e).

    Exactement le patron de `relance_abandonnee` (AD-4 : « `complete=True` exige aucune troncature de
    budget ») : la réponse acquise est servie, mais elle n'est pas donnée pour complète, et la cause
    typée dit **pourquoi** — sans quoi l'utilisateur lirait « partiel » sans savoir ce qui manque, ce
    que l'invariant `complete ⟺ found ∧ rien qui manque` du domaine interdit de toute façon.

    Sur un refus, la lacune ne se pose que si la lecture est **bornée** — c'est la règle exacte
    qu'AD-4 a prise en story 4.2f (`verifier` calcule ses lacunes sur `not found ∧ truncated`) : un
    refus non tronqué porte déjà son porteur, l'`AbsenceProof`, qui dit tout ; une `LecturePartielle`,
    elle, **exige** de dire sa borne et *restituer* refuse par contrat une réponse qui chiffre sa
    lecture sans dire pourquoi celle-ci n'a pas suffi.

    Le drapeau est un paramètre et non une relecture de `verification` parce que la borne peut naître
    **après** le contrôle : la passe de satisfaction écarte ses propres candidats sous le budget de
    l'étape, si bien qu'une lecture devenue tronquée n'a plus aucune lacune calculée par *vérifier*.
    Sans ce paramètre, ce chemin sortait en `ValueError` — un 500 générique sur la cause même que
    cette story ouvre.

    La claim que la demande bloquait, elle, vaut déjà `humain` — *vérifier* l'a écartée de
    l'applicabilité par le mécanisme existant, et AD-6 en déduit seule `ne_tranche_pas`. Rien n'est
    fabriqué ici : ni verdict, ni substitution.
    """
    lacunes = list(verification.lacunes)
    if (verification.found or lecture_bornee) and LACUNE_CONTEXTE_NON_RELU not in lacunes:
        lacunes.append(LACUNE_CONTEXTE_NON_RELU)
    return verification.model_copy(update={"complete": False, "lacunes": lacunes})


def _fondatrice_rejetee(verification: Verification, *, corpus: Any, index: Any) -> bool:
    """Une claim fondatrice rejetée exige la relance sinistre, même si une auxiliaire survit.

    Revue 3.3 post-suite : le budget de rédaction peut réserver une place à une définition ou une
    limite. Si cette auxiliaire passe le contrôle tandis que la garantie ou l'exclusion est rejetée
    sur sa pertinence, `found=True` ne signifie pas qu'AD-6 dispose encore d'une base décisionnelle :
    sans relance, les qualités contractuelles de la fondatrice disparaissent aussi des questions au
    client. Le `kind` est relu dans le corpus ; ni le texte de la claim ni le document ne décident.
    """
    for claim in verification.rejected_claims:
        if claim.rejection_kind != "non_pertinente":
            continue
        if claim.rejection_reason is not None and claim.rejection_reason not in RAISONS_CORRIGEABLES:
            # Correctif du tour 2 (rapport rédiger F). **Une relance n'est due que si elle peut
            # changer quelque chose.** Une citation `non_soutenue` ou une `conclusion_ajoutee` sont
            # des défauts de rédaction qu'une reformulation corrige. Un `hors_objet` est un jugement
            # de périmètre : il porte sur ce que la clause vise, pas sur la façon dont elle est
            # rapportée, et le relancer est une dépense sûre — deux appels, ~30 s et ~0,07 € mesurés
            # — pour un gain nul. C'est le cas nominal dès que le retrieval ramène une exclusion
            # hors périmètre, et l'audit montre le modèle ré-émettant la même claim à l'octet près.
            # Le vrai manque, lui, est visé par la couverture des sous-questions.
            continue
        # Sans raison fermée rendue par le contrôle, on ne sait pas laquelle des deux natures on a :
        # le doute profite à la relance, comme avant ce correctif.
        for quote in claim.quotes:
            try:
                document = corpus.documents[index.doc_of(quote.block_id)]
                kind = document.block(quote.block_id).kind
            except KeyError:
                continue
            if kind in KINDS_FONDATEURS:
                return True
    return False


def _fondatrices_omises(verification: Verification, retrieval: Any, settings: Settings,
                        parsed: ParsedQuestion) -> list[str]:
    """Blocs `garantie|exclusion` confirmés retrouvés dont aucune claim survivante ne cite un seul.

    Preuve finale 4.2a : la rédaction peut ne rendre qu'une définition et un segment limite —
    aucun rejet, donc aucun motif, donc aucune relance — alors que le retrieval portait une clause
    décisionnelle confirmée. Le témoin d'AD-6 exige qu'une règle retrouvée soit rendue vérifiable,
    son applicabilité étant **calculée par le code** : une portée contraire vaut `applicable="non"`,
    jamais une omission. Le `kind` vient de l'ingestion, relu sur les blocs du retrieval ; aucun
    vocabulaire de la question n'entre dans la décision. La lecture est faite **par sous-question**
    dès que la couverture par facette a été mesurée : une fondatrice citée pour l'une ne dit rien
    de l'autre. Ce déclencheur est
    complémentaire de `_fondatrice_rejetee` (clause citée mais rejetée) : il couvre la clause
    jamais citée, que l'autre ne voit pas. L'adoption de la seconde vérification, elle, passe par
    la dominance générique seule (revue Codex 4.2a, B3).
    """
    fondatrices = [b.block_id for b in retrieval.blocs
                   if b.kind in KINDS_FONDATEURS and b.kind_confirmed]
    if not fondatrices:
        return []
    citees = {quote.block_id for claim in verification.claims for quote in claim.quotes}
    if not retrieval.facettes:
        # Aucune couverture par facette mesurée (variante guide, repli déterministe, question à une
        # seule sous-question) : la question **est** la sous-question, et la règle historique vaut
        # telle quelle — une fondatrice citée quelque part prouve que la base décisionnelle existe.
        if citees & set(fondatrices):
            return []
        return fondatrices[:settings.draft_max_claims]
    # Correctif du tour 2 : la règle historique se désarmait à la **première** fondatrice citée,
    # quel que soit le nombre de sous-questions ouvertes. Sur A16 #2, la lecture portait les deux
    # règles, la rédaction n'en citait qu'une, et ce garde-fou se taisait parce que « une »
    # suffisait. La base décisionnelle existe **par sous-question**, ou elle n'existe pas : une
    # fondatrice confirmée retrouvée pour une facette que rien n'a couverte, et qu'aucune claim ne
    # cite, est exactement la clause que la relance doit faire rendre.
    fondatrices_confirmees = set(fondatrices)
    omises: list[str] = []
    for rang in _facettes_non_couvertes(verification, parsed):
        couverture = retrieval.facette(rang)
        if couverture is None:
            continue
        omises.extend(block_id for block_id in couverture.block_ids
                      if block_id in fondatrices_confirmees and block_id not in citees
                      and block_id not in omises)
    return omises[:settings.draft_max_claims]


def _blocs_juges_hors_objet(verification: Verification) -> list[str]:
    """Les blocs qu'une affirmation rejetée `hors_objet` citait, à ne pas redemander à la relance.

    Le motif de relance dit « appuie-toi sur un passage qui répond à cet objet » ; la consigne
    permanente de la story 3.3 dit, dans le **même** message, « rends une claim courte pour ce bloc,
    même si sa portée semble différente du cas ». Les deux se contredisent, et l'audit montre le
    modèle ré-émettant la claim rejetée à l'octet près. La contradiction se ferme ici, avec le seul
    fait typé qui la distingue : la raison fermée du rejet.
    """
    return list(dict.fromkeys(
        quote.block_id for claim in verification.rejected_claims
        if claim.rejection_reason == "hors_objet" for quote in claim.quotes))


def _facettes_non_couvertes(verification: Verification, parsed: ParsedQuestion) -> list[int]:
    """Les rangs de `ParsedQuestion.facettes` qu'aucune affirmation affichée ne couvre.

    *vérifier* rend la couverture qu'il a **mesurée** ; ce qui manque est le complément, calculé ici
    et nulle part ailleurs — c'est le seul endroit qui décide quoi en faire (AD-1 : *vérifier* ne
    touche aucun outil). Les rangs viennent du découpage arrêté par *comprendre* avant tout
    retrieval : ils sont stables pour toute la requête, et ce sont eux, jamais un libellé, qui
    circulent.
    """
    couvertes = set(verification.facettes_couvertes)
    return [rang for rang in range(len(parsed.facettes)) if rang not in couvertes]


def _reconduire_acquis(draft: AnswerDraft, relance: AnswerDraft, acquise: Verification,
                       settings: Settings, *, step: StepTrace) -> AnswerDraft:
    """Fusionne les acquis vérifiés dans la relance, sans jamais tronquer en silence.

    Une consigne de prompt aide le rédacteur à reconduire les acquis, mais ne constitue pas une
    garantie : une sortie modèle peut l'ignorer. La fusion repart des claims **déjà soumises** au
    premier contrôle, sélectionnées par les identifiants effectivement retenus — elle n'invente
    aucun texte ni aucune citation. Les acquis passent d'abord ; la place restante sous
    `draft_max_claims` accueille la correction, dont les identifiants conflictuels sont renommés
    localement.

    Revue Codex 4.2a (B1) : la borne `draft_max_claims` est appliquée mécaniquement dès la sortie
    de *rédiger* (`_rattacher_claims_sinistre`), l'appelant vérifie qu'il reste une place avant de
    lancer une relance fondatrice, et toute correction que la borne écarte quand même est tracée
    (`corrections_non_retenues`) — jamais jetée en silence.

    Revue Codex 4.2a (B2, durci au recheck) : les segments non factuels des **deux** ébauches sont
    fusionnés et dédupliqués, limites acquises d'abord. `Answer.unknown` est rempli depuis les
    seuls segments `limite` (AD-4) : perdre une limite acquise que la relance ne répète pas
    abaisserait `nb_manques` avant la dominance et ferait passer pour plus complète une réponse qui
    a oublié une réserve. La place des limites **acquises** — celles de `acquise.unknown`,
    l'autorité AD-4 de ce qui a survécu au contrôle (recheck tour 2, N1) — est donc **réservée
    structurellement** : les corrections de la relance ne peuvent pas saturer
    `draft_max_segments` au point d'en chasser une — l'appelant refuse d'ailleurs de lancer la
    relance quand acquis + limites acquises + une correction ne tiennent pas sous la borne
    (`relance_sans_place_pour_les_limites`). Une limite du draft rejetée par le contrôle n'est ni
    réservée ni reconduite ; seules les limites **nouvelles** de la relance peuvent encore être
    bornées, et c'est tracé (`limites_non_reconduites`). La seconde vérification relit ensuite
    tout ce résultat et la dominance reste l'autorité d'adoption.
    """
    acquis_ids = {claim.claim_id for claim in acquise.claims}
    claims: list[Claim] = [claim for claim in draft.claims if claim.claim_id in acquis_ids]
    if len(claims) > settings.draft_max_claims:
        raise ValueError("plus d'acquis que de claims autorisées : la borne draft_max_claims doit "
                         "avoir été appliquée à la sortie de *rédiger*")
    utilises = {claim.claim_id for claim in claims}

    def identifiant_libre() -> str:
        for place in range(1, settings.draft_max_claims + 1):
            candidate = f"r{place}"
            if candidate not in utilises:
                return candidate
        raise ValueError("aucun identifiant de claim libre sous la borne de rédaction")

    # La borne effective des factuels réserve la place des limites **acquises** : une correction de
    # plus ne vaut jamais une réserve acquise de moins. Recheck tour 2 (N1) : les limites acquises
    # se dérivent de `acquise.unknown` — l'autorité AD-4 de ce qui a survécu au contrôle —, jamais
    # du draft brut : une limite rejetée (`soutenu=false`) n'est ni réservée ni ressuscitée. Les
    # textes d'`unknown` sont déjà normalisés à la source (`_rattacher_claims_sinistre`, B2) : la
    # multiplicité comparée par `nb_manques` est stable des deux côtés. Les acquis eux-mêmes ne
    # sont jamais rognés par cette réserve (une première ébauche légale tenait déjà claims +
    # limites sous `draft_max_segments`).
    limites_acquises = {u.strip() for u in acquise.unknown if u.strip()}
    borne_factuels = max(len(claims),
                         min(settings.draft_max_claims,
                             settings.draft_max_segments - len(limites_acquises)))

    ecartees = 0
    reconduites = 0
    # Ce que les acquis prouvent déjà, passage par passage. Une claim de relance n'est retenue que
    # si elle **apporte un passage nouveau** : c'est ce qui distingue une correction (une citation
    # mieux recopiée, une autre clause) d'une reconduction reformulée, que la comparaison
    # byte-exacte laissait passer et qui dédoublait la réponse servie.
    preuves = set().union(*(claim.preuve for claim in claims)) if claims else set()
    for claim in relance.claims:
        apport = claim.preuve - preuves
        if not apport:
            # Aucun passage que les acquis ne portent déjà : c'est une reconduction, quelle que
            # soit sa formulation. Une affirmation réellement neuve sur un passage déjà cité est le
            # prix de cette règle, et il est assumé : la place est comptée (`draft_max_claims`), et
            # une réponse qui se répète coûte plus cher à qui la lit qu'une nuance perdue.
            reconduites += 1
            continue
        if claim.claim_id in utilises:
            # La correction reste contrôlable sous un identifiant non ambigu.
            if len(claims) >= borne_factuels:
                ecartees += 1
                continue
            claim = claim.model_copy(update={"claim_id": identifiant_libre()})
        elif len(claims) >= borne_factuels:
            ecartees += 1
            continue
        utilises.add(claim.claim_id)
        claims.append(claim)
        preuves |= apport
    if reconduites:
        step.checks.append(CheckResult(
            name="acquis_reconduits", ok=True,
            detail=f"{reconduites} affirmation(s) de la relance n'apportent aucun passage que les "
                   "acquis ne citent déjà : reconduction, non dupliquée dans la réponse"))
    if ecartees:
        step.checks.append(CheckResult(
            name="corrections_non_retenues", ok=False,
            detail=f"{ecartees} correction(s) de la relance au-delà de la borne effective "
                   "(draft_max_claims, place des réserves acquises réservée), écartée(s) après "
                   "la reconduction des acquis : borne mécanique tracée, jamais muette"))

    if len(claims) > settings.draft_max_segments:
        # Même invariant que `_rattacher_claims_sinistre` : aucune claim vérifiée ne disparaît
        # sans trace. `Settings._coherence` garantit draft_max_claims <= draft_max_segments ; si
        # une borne effective plus basse rendait la fusion tronquante, elle refuse plutôt que de
        # faire disparaître silencieusement un acquis.
        raise ValueError("plus de claims que de segments autorisés : la configuration doit garantir "
                         "draft_max_claims <= draft_max_segments")
    factuels = [AnswerSegment(text=claim.text, kind="factuel", claim_ids=[claim.claim_id])
                for claim in claims]
    place = settings.draft_max_segments - len(factuels)
    # Limites d'abord — les acquises (celles d'`unknown`) avant les nouvelles de la relance —,
    # transitions ensuite : sous une place bornée, une réserve vaut plus qu'une liaison. Une
    # limite du draft **rejetée** par le contrôle (`soutenu=false`, absente d'`unknown`) n'est pas
    # reconduite : la fusion ne ressuscite pas ce que la vérification a écarté (N1).
    par_priorite = ([s for s in draft.segments
                     if s.kind == "limite" and s.text.strip() in limites_acquises]
                    + [s for s in relance.segments if s.kind == "limite"]
                    + [s for s in relance.segments if s.kind == "transition"]
                    + [s for s in draft.segments if s.kind == "transition"])
    vus: set[tuple[str, str]] = set()
    non_factuels: list[AnswerSegment] = []
    limites_ecartees = 0
    for segment in par_priorite:
        cle = (segment.kind, segment.text.strip())
        if not segment.text.strip() or cle in vus:
            continue
        vus.add(cle)
        if len(non_factuels) >= place:
            if segment.kind == "limite":
                limites_ecartees += 1
            continue
        non_factuels.append(AnswerSegment(text=segment.text, kind=segment.kind, claim_ids=[]))
    if limites_ecartees:
        step.checks.append(CheckResult(
            name="limites_non_reconduites", ok=False,
            detail=f"{limites_ecartees} segment(s) limite ne tiennent pas sous draft_max_segments "
                   "après les claims fusionnées : réserve(s) perdue(s) nommée(s), jamais tue(s)"))
    return AnswerDraft(segments=[*factuels, *non_factuels], claims=claims)


def _verdict_de_refus(kind: str, dossier: MissingPackage | None = None) -> Verdict:
    """AD-16 : un refus sinistre porte `ne_tranche_pas`, jamais rien.

    Le front sinistre affiche d'abord un badge de verdict ; sans verdict, il n'aurait qu'une absence à
    montrer là où l'utilisateur attend une position. `ne_tranche_pas` **est** cette position, et sa
    raison dit franchement pourquoi la table n'a rien eu à trancher.

    Le paquet manquant reste **entier**, et il est donc **demandé** : c'est le dossier qui a le plus
    besoin d'être complété, et le laisser repartir avec quatre pièces annoncées absentes et aucune
    question serait le seul verdict du système à ne rien réclamer (revue 1.8). Les questions sont
    celles qu'un verdict ordinaire compose — même code, mêmes mots, aucune claim à interroger.
    """
    paquet = (dossier or MissingPackage()).model_copy(deep=True, update={"faits": []})
    return Verdict(value="ne_tranche_pas",
                   reason=f"{RAISONS_DE_REFUS.get(kind, RAISON_DE_REFUS_GENERIQUE)} ({PORTEE})",
                   missing=paquet,
                   ask_client=questions_du_paquet_manquant(paquet),
                   escalate=["Aucune clause du contrat n'a pu être opposée au sinistre : "
                             "reprendre le dossier à la main."])


# Borne du domaine, lue sur le schéma : `Faits.description` la porte (AD-11), et la recopier ici en
# dur la ferait mentir le jour où elle bouge. Absente du schéma, elle ferait servir « limitée à None
# caractères » à l'appelant : mieux vaut que l'import échoue au démarrage, là où un humain le voit.
_DESCRIPTION_MAX = next((m.max_length for m in Faits.model_fields["description"].metadata
                         if getattr(m, "max_length", None) is not None), None)
if _DESCRIPTION_MAX is None:  # pragma: no cover — invariant de schéma, vérifié au chargement du module
    raise RuntimeError("Faits.description a perdu sa borne de longueur : AD-11 exige un rejet chiffré, "
                       "et le message d'erreur du pipeline la cite")


def _faits(faits: Faits | Mapping[str, Any]) -> Faits:
    """Borne d'entrée (AD-11/AD-16) : les faits sont **rejetés** hors bornes, jamais tronqués.

    Le domaine porte déjà la borne (`Faits.description ≤ 2 000` caractères) ; ce qu'ajoute le pipeline,
    c'est le **code d'erreur** : une `ValidationError` pydantic remontée telle quelle deviendrait un
    500 `internal` (AD-16), là où l'appelant a simplement envoyé une description trop longue. Tronquer
    serait pire : la fin d'une description de sinistre porte souvent la cause.
    """
    if isinstance(faits, Faits):
        return faits
    try:
        # `dict(faits)` est **dans** le `try` : une chaîne, une liste ou un objet non convertible lève
        # `ValueError`/`TypeError`, que l'API traduirait en 500 `internal` — un corps mal formé est
        # pourtant exactement ce que le 400 d'AD-16 décrit (revue 1.8).
        return Faits.model_validate(dict(faits))
    except (ValidationError, TypeError, ValueError) as exc:
        # AD-15 : rien de ce que l'appelant a envoyé n'entre dans le message — ni la valeur, ni le
        # chemin pydantic, qui peut porter une clé inconnue de son cru. Seuls le **compte** d'erreurs
        # et notre propre borne, lue sur le schéma pour qu'elle ne se désynchronise pas du domaine.
        combien = exc.error_count() if isinstance(exc, ValidationError) else 0
        detail = (f"{combien} champ(s) invalide(s)" if combien
                  else "les faits ne forment pas un objet lisible")
        raise InvalidRequest(f"faits du sinistre hors bornes : {detail} (la description est limitée à "
                             f"{_DESCRIPTION_MAX} caractères, jamais tronquée côté serveur)") from exc


async def run(doc_id: str | None, question: str, faits: Faits | Mapping[str, Any], *, corpus: Any,
              index: Any, client: Any, settings: Settings, request_id: str,
              variant: str = VARIANT, lang: str | None = None, deadline_s: float | None = None,
              budget: Any = None, dossier: MissingPackage | None = None,
              dictionnaire: Any = None,
              pipeline_digest_hex: str | None = None,
              prompts_digest_hex: str | None = None) -> tuple[Answer, Trace]:
    """Un sinistre décrit → l'unique `Answer` d'AD-4, verdict compris, et sa `Trace`.

    Toute sortie normale — verdict, refus, clauses toutes rejetées — est un `Answer` (l'API en fera un
    200, AD-11). Seules les entrées hors bornes (`InvalidRequest`), le document non servi
    (`CorpusUnavailable`) et les échecs terminaux des étapes (`Timeout`, `LlmParse`, `BudgetExceeded`,
    `LlmUnavailable`) remontent — sans repli, AD-16.

    `dossier` (revue Codex 1.8, B1) est le paquet contractuel que l'appelant **détient** : un
    `MissingPackage` dont les booléens à `False` disent les pièces qu'il n'a plus à réclamer. C'est le
    seul chemin vers `couvert` (règle (3) d'AD-6), et il est **explicite** : sans lui, tout est réputé
    inconnu et la règle (2) plafonne le verdict à `sous_conditions` — « au regard des conditions
    générales seules ». Rien dans le pipeline ne le fabrique ni ne le devine ; les `faits[]` qu'il
    porterait sont ignorés, ils sont dérivés des libellés rendus par le modèle.
    """
    if variant not in VARIANTES:
        # Avant tout appel facturé : une variante inconnue est une faute d'appel, pas un cas à traiter.
        raise InvalidRequest(f"variante de recherche inconnue : {variant!r} "
                             f"(connues : {', '.join(sorted(VARIANTES))})")
    lang = normaliser_langue_pipeline(lang)
    if budget is not None and deadline_s is not None:
        # Le budget **porte** sa deadline, et elle court déjà (horloge monotone armée à sa création).
        # Accepter les deux laissait `deadline_s` sans effet, en silence : l'appelant croyait borner la
        # requête et ne bornait rien (revue 1.8). AD-16 : jamais de dégradé silencieux.
        raise InvalidRequest("budget et deadline_s sont exclusifs : un budget porte déjà sa deadline "
                             "(passer l'un ou l'autre, jamais les deux)")
    faits = _faits(faits)
    doc_id = doc_id or settings.sinistre_doc_id
    if doc_id not in corpus.documents:
        # Contrat absent, mal nommé ou en quarantaine (AD-7) : levé **avant** le premier appel modèle.
        raise CorpusUnavailable(f"document {doc_id!r} non servi (absent du corpus ou en quarantaine)")
    if budget is None:
        budget = client.new_budget(deadline_s=deadline_s) if deadline_s is not None else client.new_budget()
    document = corpus.documents[doc_id]
    budget.bind_artifact(
        document_uid=document.doc_id, source_hash=document.source_hash,
        ingest_fingerprint=document.ingest_fingerprint)

    steps: list[StepTrace] = []
    # Les étapes sans tier dont la deadline était déjà dépassée : elles ne dépensent rien,
    # elles sont servies, et le fait est publié plutôt que payé d'un 503 (correctif C1).
    depassements: list[str] = []
    relances = 0
    truncated = False
    intent: str | None = None
    # La question comprise, dès qu'elle existe : `tracer()` en publie les termes et le
    # découpage, y compris sur les chemins d'erreur où seule la trace partielle sort.
    question_comprise: ParsedQuestion | None = None

    def echeance(avant: str) -> None:
        """AD-1/AD-9 : la deadline monotone est vérifiée **avant** chaque étape, jamais après coup.

        **Correctif du tour 4 (C1) : une étape qui ne dépense rien est une remise, pas une
        dépense.** La deadline protège le budget d'appels du fournisseur — c'est son unique objet.
        Elle refusait pourtant *restituer* comme les autres, alors que cette étape n'appelle aucun
        modèle (`STEP_TIERS["restituer"] is None`, `calls=[]`, **0 ms mesuré**) et ne fait que
        composer l'`Answer` à partir d'un travail déjà payé. Mesuré sur A16 : une réponse conforme,
        vérifiée et servable à 56,7 s a été jetée en 503 pour `remaining = -0,011 s`, après
        0,24 € dépensés. Un dépassement sur une remise se **dit** — la trace publie déjà
        `deadline_remaining_s`, et le check le nomme —, il ne se **paie** pas d'une erreur.

        Le fait employé est celui de la table des étapes (`ETAPES_SANS_APPEL`, jumelle de
        `STEP_TIERS` dont le tier y vaut `None`) : aucun nom n'est décidé ici. Un court-circuit
        de code pur, absent de la table des tiers, y est traité comme ce qu'il est — quelque
        chose qui ne dépense rien.

        La latence réelle reste bornée ailleurs, et rien n'y touche : `client_abort_margin_s`
        côté navigateur (AD-11) et le délai d'infrastructure au déploiement.
        """
        if budget.remaining() > 0:
            return
        if avant not in ETAPES_SANS_APPEL:
            raise Timeout(f"deadline épuisée avant l'étape {avant} ({budget.remaining():.1f} s restantes)")
        depassements.append(f"{avant} ({budget.remaining():.1f} s)")

    def noter_depassement(step: StepTrace) -> None:
        """AD-10 : le dépassement d'une étape sans appel est nommé dans la trace, jamais tu."""
        if depassements:
            step.checks.append(CheckResult(
                name="deadline_depassee", ok=False,
                detail=f"deadline dépassée avant {', '.join(depassements)} : l'étape n'appelle "
                       "aucun modèle, la réponse déjà payée est servie plutôt que refusée"))

    def tracer() -> Trace:
        digest_pipeline, digest_prompts = (pipeline_digest_hex, prompts_digest_hex)
        if digest_pipeline is None or digest_prompts is None:
            defaut = digests()
            digest_pipeline = digest_pipeline if digest_pipeline is not None else defaut[0]
            digest_prompts = digest_prompts if digest_prompts is not None else defaut[1]
        entry = corpus.manifest.get(doc_id)
        retries = sum(1 for s in steps for c in s.checks if c.name == "parse_retry") + relances
        return Trace(
            request_id=request_id, pipeline=PIPELINE, variant=variant, intent=intent, steps=steps,
            total_cost_eur=round(budget.cost_eur, 4),
            source_hash={doc_id: entry.source_hash} if entry is not None else {},
            ingest_fingerprint={doc_id: entry.ingest_fingerprint} if entry is not None else {},
            pipeline_digest=digest_pipeline, prompts_digest=digest_prompts,
            thresholds=settings.thresholds(), retries=retries, truncations=int(truncated),
            deadline_remaining_s=round(budget.remaining(), 3),
            # Story 2.5 : les mêmes résolutions que le guide (`pipelines/commun`) — l'outil sinistre
            # affiche la même trace, et il doit pouvoir **nommer ses clauses** au lieu d'aligner des
            # identifiants de blocs. Le dictionnaire publié est uniquement celui du contrat lu.
            blocs=libelles_de_blocs(corpus, doc_id, steps),
            gate=gate_de(corpus, doc_id),
            dictionnaire=dictionnaire_de(
                dictionnaire, doc_id,
                court_circuit_autorise=False),
            # Correctif du tour 2 : ce que *comprendre* a décidé, et dont tout le reste
            # dépend. Sans ces deux listes, trois réponses différentes à la même question
            # ne se rejouent pas — même avec l'audit.
            termes=(question_comprise.termes_de_recherche()
                    if question_comprise is not None else []),
            facettes=(list(question_comprise.facettes)
                      if question_comprise is not None else []),
        )

    def absence(kind: str, parsed: ParsedQuestion | None) -> AbsenceProof:
        """Preuve d'absence (AD-4) : ce qui a été cherché, jamais les variantes ni les déclencheurs."""
        if parsed is None:  # rien n'a été cherché : ni termes, ni passages parcourus
            return AbsenceProof(kind=kind)
        document = corpus.documents.get(doc_id)
        termes = parsed.termes_de_recherche()
        elargi = dictionnaire is not None and dictionnaire.utilisable_pour(doc_id)
        return AbsenceProof(kind=kind,
                            terms_searched=dictionnaire.canoniser(termes) if elargi else termes,
                            variants_count=dictionnaire.variants_count(termes) if elargi else 0,
                            blocks_scanned=len(document.blocks) if document is not None else 0,
                            documents=[doc_id] if document is not None else [])

    def faits_compris(scope: QuestionScope | None) -> tuple[QuestionScope | None, list[str]]:
        """Ce que *comprendre* a compris des faits, borné avant d'atteindre l'écran (story 1.9, D4).

        `ParsedQuestion.scope` porte des libellés **du modèle** (bien, événement, lieu, cause,
        moment), et l'AC de la story les affiche. La règle de D8 (spec 1.8) leur vaut donc : hors
        borne, le libellé est ignoré, jamais tronqué. Le pipeline est le seul à voir `settings` et
        la question comprise — *restituer* ne fait que recopier ce qu'on lui donne (AD-4).
        """
        if scope is None:
            return None, []
        return scope.borner(settings.fait_manquant_max_chars, settings.scope_max_themes)

    def noter_hors_borne(step: StepTrace, ignores: list[str]) -> None:
        """AD-10 : un libellé écarté se **dit**. Le check nomme les champs, jamais leur contenu."""
        if ignores:
            step.checks.append(CheckResult(
                name="faits_compris_hors_borne", ok=False,
                detail=f"{len(ignores)} champ(s) des faits compris dépassent "
                       f"{settings.fait_manquant_max_chars} caractères et ne sont pas affichés "
                       f"(jamais tronqués) : {', '.join(ignores)}"))

    def refuser(kind: str, parsed: ParsedQuestion | None, *, language: str,
                lang_fallback: bool = False,
                scope: QuestionScope | None = None,
                clarification: str | None = None) -> tuple[Answer, Trace]:
        # *restituer* ne dépense rien : un dépassement y est nommé, pas payé (correctif C1).
        echeance("restituer")
        compris, ignores = faits_compris(scope)
        answer, step = restituer(language=language, lang_fallback=lang_fallback,
                                 reason=absence(kind, parsed),
                                 clarification=clarification,
                                 verdict=_verdict_de_refus(kind, dossier),
                                 faits_compris=compris,
                                 registre=REGISTRE_SINISTRE)
        noter_hors_borne(step, ignores)
        noter_depassement(step)
        steps.append(step)
        return answer, tracer()

    async def chaine() -> tuple[Answer, Trace]:
        """Les cinq étapes. Sortie normale : un `Answer` et sa `Trace`. Échec terminal : `PipelineError`."""
        nonlocal relances, truncated, intent, question_comprise
        # --- comprendre -----------------------------------------------------
        echeance("comprendre")
        # Ni historique ni profil : un dossier de sinistre n'est pas une conversation, et le profil du
        # guide (enfants, véhicule, statut) n'oriente aucune clause. Ce sont les **faits** qui portent
        # le contexte, et ils partent délimités par `untrusted()` (AD-15).
        parsed, step_comprendre = await comprendre(question, [], Profil(), client=client, budget=budget,
                                                   settings=settings, lang=lang,
                                                   prompt="comprendre_sinistre", faits=faits)
        steps.append(step_comprendre)
        intent = parsed.intent
        if isinstance(parsed, ParsedQuestion):
            question_comprise = parsed

        if isinstance(parsed, ClarificationRequise):
            # Seul refus qui ne publie **aucun** fait compris, et ce n'est pas un oubli (revue 1.9) :
            # AD-5 donne à *comprendre* deux sorties typées **exclusives**, et `ClarificationRequise`
            # ne porte pas de `scope` — il n'y a rien à publier, pas même une portée partielle. Le
            # sens de la règle tient : *comprendre* n'a pas pu rendre la question autonome, donc il
            # n'a rien « compris » du sinistre qu'on puisse afficher sans l'inventer. La question
            # posée à l'utilisateur (`answer.clarification`) est ce que cet écran a à montrer.
            return refuser("clarification_requise", None, language=parsed.language,
                           lang_fallback=parsed.lang_fallback,
                           # Servie même sous un repli de détection (revue Codex 2.4, tour 2,
                           # NB1) : AD-5 l'exige sans réserve, `lang_fallback` publie la divergence.
                           clarification=parsed.clarification)
        if parsed.intent in INTENTS_REFUSES:
            # Court-circuit d'AD-5 : l'étage `reason` n'est jamais atteint pour un refus. Les faits
            # compris, eux, existent déjà — *comprendre* a tourné —, et c'est justement sur un refus
            # « hors périmètre » qu'ils comptent le plus : ils disent ce que le système a cru lire.
            return refuser("hors_perimetre", None, language=parsed.language,
                           lang_fallback=parsed.lang_fallback, scope=parsed.scope)

        # --- retrouver -------------------------------------------------------
        echeance("retrouver")
        # Une seule borne pour l'étape entière, repli compris (AD-1 : « un `RetrievalBudget` borne
        # **toute** l'étape »). La hisser dans une variable est ce qui empêche le repli de repartir
        # sur une borne neuve, c'est-à-dire de ne plus être borné.
        borne_retrieval = retrieval_budget(settings)
        if variant == VARIANT:
            # AD-1 : la navigation par outils est le mode par défaut. `kinds_prioritaires` n'y est
            # pas porté — le départage de la story 1.8 est un tri à score égal **dans l'index**, et
            # la variante outils ne classe pas : elle laisse le modèle choisir ses termes puis ses
            # nœuds. L'ajouter serait un mécanisme de rappel de plus, pas le câblage de cette story.
            candidats_outils: list[str] = []
            try:
                retrieval, step_retrouver = await retrouver_outils(
                    parsed, corpus=corpus, index=index, budget=borne_retrieval, settings=settings,
                    client=client, request_budget=budget, doc_id=doc_id,
                    dictionnaire=dictionnaire, candidats_out=candidats_outils,
                    kinds_suffisants=KINDS_FONDATEURS)
            except PipelineError as exc:
                # AD-16, comme au guide : l'étape partielle voyage avec l'erreur. Sans cela, un
                # échec pendant la navigation ressortirait en 503 sans dire ce qui avait été appelé.
                if exc.step is not None:
                    steps.append(exc.step)
                exc.trace = tracer()
                raise
        else:
            retrieval, step_retrouver = retrouver_deterministe(
                parsed, corpus=corpus, index=index, budget=borne_retrieval, settings=settings,
                doc_id=doc_id, kinds_prioritaires=KINDS_DECISIONNELS,
                dictionnaire=dictionnaire)
        if variant == VARIANT and retrieval.truncated and not retrieval.blocs:
            # Le repli du guide, à la condition près de rien : `truncated ∧ aucun bloc`. Des blocs
            # outils **partiels** restent un contexte honnête, que la suite de la chaîne publiera
            # avec `complete=False` ; les remplacer par une sélection déterministe coûterait plus et
            # masquerait la lecture bornée. Une seule tentative, sous la même borne, sans tour modèle
            # supplémentaire — et `kinds_prioritaires` décisionnels, comme l'appel déterministe
            # qu'elle remplace : la baseline sinistre reste ce qu'elle était (story 1.8).
            candidats_deterministes: list[str] = []
            fallback, fallback_step = retrouver_deterministe(
                parsed, corpus=corpus, index=index, budget=borne_retrieval, settings=settings,
                doc_id=doc_id, kinds_prioritaires=KINDS_DECISIONNELS,
                dictionnaire=dictionnaire, candidats_out=candidats_deterministes)
            step_retrouver.checks.append(CheckResult(
                name="repli_deterministe", ok=False,
                detail="navigation par outils tronquée sans bloc ; repli déterministe borné transmis"))
            step_retrouver.checks.extend(fallback_step.checks)
            step_retrouver.ms += fallback_step.ms
            step_retrouver.opened_block_ids = list(fallback.opened_block_ids)
            finaux = set(fallback.opened_block_ids)
            candidats = [*candidats_outils, *candidats_deterministes]
            discarded = list(dict.fromkeys(b for b in candidats if b not in finaux))
            step_retrouver.discarded_block_ids = discarded
            retrieval = fallback.model_copy(update={"discarded_block_ids": discarded})
        steps.append(step_retrouver)
        truncated = retrieval.truncated
        if not retrieval.blocs and retrieval.truncated:
            # AD-1 : un retrieval vidé **par le budget** ne dit rien du contrat. Le convertir en
            # `zero_hit` fabriquerait une absence à partir d'une borne qui est la nôtre.
            raise BudgetExceeded(
                f"le budget de retrieval n'a laissé passer aucun bloc ({settings.retrieval_max_blocks} blocs, "
                f"{settings.retrieval_max_tokens} tokens) : aucune absence du contrat n'est affirmée")
        if not retrieval.blocs:
            return refuser("zero_hit", parsed, language=parsed.language,
                           lang_fallback=parsed.lang_fallback, scope=parsed.scope)

        # --- rédiger --------------------------------------------------------
        echeance("rediger")
        draft, step_rediger = await rediger(parsed, retrieval, [], client=client, budget=budget,
                                            index=index, doc_id=doc_id, settings=settings,
                                            prompt="rediger_sinistre")
        steps.append(step_rediger)

        # --- vérifier -------------------------------------------------------
        echeance("verifier")
        verification, step_verifier = await verifier(draft, parsed=parsed, retrieval=retrieval,
                                                     corpus=corpus, index=index, client=client,
                                                     budget=budget, settings=settings, faits=faits,
                                                     dossier=dossier)
        steps.append(step_verifier)
        # Story 4.2e : **quelle** ébauche et **quelle** étape ont produit la vérification retenue.
        # La relance d'AD-3 peut les remplacer plus bas ; la demande de contexte, elle, est rendue
        # par le contrôle qui fait foi, et c'est cette ébauche-là qu'une reprise doit relire (et
        # cette étape-là que ses checks doivent nommer).
        draft_verifie, step_de_la_verification = draft, step_verifier

        # --- couverture des facettes, avant toute relance --------------------
        # *vérifier* mesure quelles sous-questions une affirmation **affichée** couvre ; jusqu'ici
        # ce constat ne déclenchait rien. Une facette laissée de côté par la rédaction — ou dont la
        # lecture n'avait rapporté aucune règle — se contentait d'abaisser `complete`, et le second
        # cycle re-rédigeait sur exactement les mêmes blocs : il ne pouvait pas couvrir ce qui
        # n'avait pas été retrouvé.
        #
        # La reprise est **ciblée** sur les seules facettes non couvertes, **en code pur** (aucun
        # appel : `couvrir_facettes` ne fait que classer et rouvrir), **sous le budget de l'étape**
        # — le même objet `borne_retrieval` que la passe initiale et que la satisfaction 4.2e, sans
        # quoi une seconde passe repartie de zéro ne serait plus bornée du tout. Une passe, jamais
        # une boucle : ce qui reste sans clause après elle est déclaré absent, et la chaîne cesse.
        rangs_non_couverts = _facettes_non_couvertes(verification, parsed)
        retrieval_relance = retrieval
        consigne_facette: str | None = None
        blocs_des_facettes: list[str] = []
        if rangs_non_couverts:
            complement_facettes, step_facettes = couvrir_facettes(
                parsed, retrieval=retrieval, corpus=corpus, index=index, budget=borne_retrieval,
                settings=settings, doc_id=doc_id, kinds_suffisants=KINDS_FONDATEURS,
                dictionnaire=dictionnaire, rangs=rangs_non_couverts)
            # Fusion de trace, comme le repli déterministe et la satisfaction 4.2e : l'étape
            # *retrouver* reste une, et ce qu'elle vient de rouvrir rejoint ce qu'elle publiait.
            step_retrouver.checks.extend(step_facettes.checks)
            step_retrouver.ms += step_facettes.ms
            step_retrouver.opened_block_ids = list(complement_facettes.opened_block_ids)
            step_retrouver.discarded_block_ids = list(complement_facettes.discarded_block_ids)
            # La borne se dit **tout de suite**, même si le complément n'est pas adopté (revue
            # 4.2e, F) : un refus aval ne doit jamais repartir d'une lecture donnée pour exhaustive
            # alors que le budget a écarté des candidats. Les **blocs**, eux, restent ceux que la
            # vérification servie a réellement vus.
            truncated = truncated or complement_facettes.truncated
            retrieval = retrieval.model_copy(update={
                "truncated": truncated,
                "discarded_block_ids": list(complement_facettes.discarded_block_ids)})
            retrieval_relance = complement_facettes.model_copy(update={"truncated": truncated})
            # Correctif du tour 3 (R4). **On ne relance que sur ce qu'une relance peut rendre
            # pertinent.** Deux gardes, et elles sont mesurées toutes les deux :
            #
            # 1. les blocs viennent de `FacetteCouverture.block_ids`, qui ne contient plus que des
            #    correspondances **pleines** depuis R1 — c'est ce qui a fait ordonner au rédacteur,
            #    en réel, une claim sur une exclusion de responsabilité civile immeuble ;
            # 2. la reprise doit avoir **réellement rouvert** quelque chose pour cette
            #    sous-question. Quand elle ne rouvre rien, la relance n'a devant elle que des blocs
            #    déjà soumis au rédacteur, qui les a lus et n'en a rien tiré : lui redemander la
            #    même chose est une dépense sûre pour un gain nul (mesuré : 42 s et 0,09 € sur la
            #    troisième réponse, pour deux claims que le contrôle a rejetées). Si l'un de ces
            #    blocs est une fondatrice confirmée jamais citée, `_fondatrices_omises` le nomme —
            #    c'est le chemin précis, et il reste ouvert.
            rouverts = set(step_facettes.opened_block_ids)
            blocs_des_facettes[:] = list(dict.fromkeys(
                block_id for rang in rangs_non_couverts
                for block_id in (complement_facettes.facette(rang).block_ids
                                 if complement_facettes.facette(rang) is not None else ())
                if block_id in rouverts))
            if not rouverts and complement_facettes.facettes:
                # Fin de chaîne honnête : rien de décisionnel n'existe dans le contrat lu pour ces
                # sous-questions, et aucune relance de *rédiger* ne peut le fabriquer. La réponse
                # le dit — *vérifier* dépose la lacune `facettes_sans_clause` sur la déclaration
                # d'absence de *retrouver*, et *restituer* la projette dans `unknown[]`.
                #
                # `complement.facettes` vide veut dire « pas mesuré » (question à une seule
                # sous-question, où la facette **est** la question) : il n'y a alors rien à dire de
                # plus que ce que la chaîne dit déjà, et la trace se tait plutôt que d'annoncer une
                # absence qu'aucune passe n'a cherché à lever.
                step_de_la_verification.checks.append(CheckResult(
                    name="facettes_sans_clause", ok=False,
                    detail=f"{len(rangs_non_couverts)} sous-question(s) sans clause décisionnelle "
                           "neuve après une reprise ciblée de retrouver : la relance n'aurait que "
                           "des blocs déjà soumis à proposer, l'absence est dite plutôt que "
                           "fabriquée"))

        # --- relance unique (AD-3) ------------------------------------------
        omises = _fondatrices_omises(verification, retrieval_relance, settings, parsed)
        # Les deux consignes disent deux choses différentes et ne doivent pas dire la même deux
        # fois : `omises` nomme les **fondatrices** qu'aucune claim ne cite (leur consigne est celle
        # de la story 3.3, la plus précise) ; ce qui reste des blocs décisionnels d'une facette non
        # couverte — conditions, franchises, fondatrices déjà citées ailleurs — relève de la
        # consigne de facette. Un identifiant n'apparaît donc que dans l'une des deux.
        blocs_des_facettes = [block_id for block_id in blocs_des_facettes
                              if block_id not in set(omises)]
        # Correctif du tour 2 : **la sous-question restée sans réponse est nommée.** Le motif ne
        # savait dire que « telle claim a été rejetée » ; il ne disait jamais « il te reste cette
        # sous-question à traiter », et le rédacteur ne recevait pas non plus le découpage. La ligne
        # est composée par le code (AD-15), depuis les libellés que *comprendre* a arrêtés avant
        # tout retrieval, déjà bornés en nombre et en longueur ; elle voyage sous `untrusted()`
        # comme tout le motif.
        libelles_manquants = [parsed.facettes[rang] for rang in rangs_non_couverts
                              if 0 <= rang < len(parsed.facettes) and parsed.facettes[rang].strip()]
        consigne_facettes_nommees = (
            "Sous-question(s) de la demande restée(s) sans affirmation affichée, à traiter "
            "explicitement : " + " ; ".join(libelles_manquants) + "."
        ) if libelles_manquants else None
        if blocs_des_facettes:
            # Composée par le code, comme tout motif (AD-15) : les identifiants viennent du corpus
            # typé, jamais de la question — la consigne nomme des blocs à rendre vérifiables.
            consigne_facette = (
                f"{len(rangs_non_couverts)} sous-question(s) de la demande n'ont reçu aucune "
                "affirmation affichée, alors que la lecture porte pour elles des clauses "
                "décisionnelles confirmées (" + ", ".join(blocs_des_facettes) + ") : rends "
                "pour au moins l'une d'elles une claim courte qui rapporte sa règle "
                "conditionnelle, avec sa plus courte citation contiguë, sans décider de son "
                "applicabilité au dossier — le code la calcule. Conserve les affirmations "
                "déjà acquises.")
        if omises and len(verification.claims) >= settings.draft_max_claims:
            # Revue Codex 4.2a (B1) : la fusion doit reconduire **tous** les acquis et au moins une
            # correction fondatrice. Quand les affirmations retenues occupent déjà
            # `draft_max_claims`, une relance ne pourrait que tronquer — l'état est nommé, la
            # réponse acquise est servie sans être donnée pour complète (même lacune typée que la
            # relance abandonnée faute de budget : une relance due n'a pas pu démarrer).
            step_verifier.checks.append(CheckResult(
                name="relance_fondatrice_sans_place", ok=False,
                detail=f"clause décisionnelle confirmée jamais citée ({', '.join(omises)}) mais "
                       f"les {len(verification.claims)} affirmation(s) retenue(s) occupent déjà "
                       "draft_max_claims : la relance fondatrice ne peut pas reconduire les "
                       "acquis et ajouter la clause"))
            verification = relance_abandonnee(verification)
            omises = []
        if consigne_facette is not None and len(verification.claims) >= settings.draft_max_claims:
            # Même raison que ci-dessus, et elle vaut mot pour mot : la fusion doit reconduire tous
            # les acquis **et** ajouter la clause de la sous-question laissée de côté. Sous
            # `draft_max_claims` déjà occupé, la relance ne pourrait que troquer une facette contre
            # une autre — exactement ce que la dominance interdit.
            step_verifier.checks.append(CheckResult(
                name="relance_facette_sans_place", ok=False,
                detail=f"{len(rangs_non_couverts)} sous-question(s) sans affirmation affichée mais "
                       f"les {len(verification.claims)} affirmation(s) retenue(s) occupent déjà "
                       "draft_max_claims : la relance ne peut pas reconduire les acquis et "
                       "couvrir la facette"))
            verification = relance_abandonnee(verification)
            consigne_facette = None
        relance_due = bool((verification.motif and (
            relance_utile(verification, settings)
            or _fondatrice_rejetee(verification, corpus=corpus, index=index)
        )) or omises or consigne_facette)
        if relance_due:
            # Revue Codex 4.2a (B2, recheck) : le pré-contrôle couvre aussi la borne de segments.
            # La fusion doit reconduire tous les acquis, **toutes leurs limites** et au moins une
            # correction ; si `draft_max_segments` ne le permet pas, la relance produirait un
            # candidat amputé dont `nb_manques` aurait baissé artificiellement **avant** la
            # dominance. Elle n'est pas lancée : l'état est nommé, la réponse acquise — limites
            # comprises — est servie avec la lacune de relance abandonnée, jamais donnée pour
            # complète. La dominance ne voit ainsi jamais un candidat amputé.
            # Recheck tour 2 (N1) : les limites **acquises** sont celles de `Verification.unknown`
            # — l'autorité AD-4 de ce qui a survécu au contrôle —, jamais celles du draft brut :
            # une limite rejetée (`soutenu=false`) ne compte pas, et ne peut plus interdire à tort
            # une relance qui tient réellement sous la borne.
            limites_acquises = [u.strip() for u in verification.unknown if u.strip()]
            if len(verification.claims) + 1 + len(limites_acquises) > settings.draft_max_segments:
                step_verifier.checks.append(CheckResult(
                    name="relance_sans_place_pour_les_limites", ok=False,
                    detail=f"les {len(verification.claims)} affirmation(s) retenue(s), leurs "
                           f"{len(limites_acquises)} réserve(s) acquise(s) et une correction ne "
                           "tiennent pas sous draft_max_segments : la relance tronquerait une "
                           "limite acquise avant la dominance — acquis servi, réserves comprises"))
                verification = relance_abandonnee(verification)
                relance_due = False
        if relance_due:
            motif_relance = verification.motif
            if omises:
                # Composé par le code, comme tout motif (AD-15) : les identifiants viennent du
                # corpus typé, pas de la question, et la consigne reprend celle de la story 3.3 —
                # rendre la clause vérifiable, laisser le code décider de l'applicabilité.
                consigne_fondatrice = (
                    "aucune affirmation vérifiable ne rapporte la règle d'une clause décisionnelle "
                    "confirmée pourtant retrouvée (" + ", ".join(omises) + ") : rends pour au "
                    "moins l'une d'elles une claim courte qui rapporte sa règle conditionnelle, "
                    "avec sa plus courte citation contiguë, sans décider de son applicabilité au "
                    "dossier — le code la calcule, même si son périmètre semble différent du cas "
                    "déclaré.")
                motif_relance = (f"{motif_relance}\n{consigne_fondatrice}" if motif_relance
                                 else consigne_fondatrice)
            for consigne in (consigne_facettes_nommees, consigne_facette):
                if consigne is not None:
                    motif_relance = (f"{motif_relance}\n{consigne}" if motif_relance
                                     else consigne)
            acquise = verification
            appels_avant = budget.attempts
            redaction_relancee = False
            try:
                # C2 : le temps que le **cycle entier** demande, au débit minoré — la somme des
                # durées majorées de ses deux appels —, et non un nombre de secondes sans rapport
                # avec ce qu'il va écrire. Mesuré sur A16 : la porte s'ouvrait à 43,3 s restantes
                # pour un cycle qui en demande 74,8 ; les deux appels sont partis, le second a
                # expiré sans écrire un token, et il a emporté la marge de la remise.
                duree_du_cycle = (settings.duree_majoree_pour(settings.rediger_max_tokens)
                                  + settings.duree_majoree_pour(settings.verifier_sinistre_max_tokens))
                if budget.remaining() <= duree_du_cycle:
                    raise Timeout(f"temps insuffisant pour la relance : {duree_du_cycle:.1f} s "
                                  f"requises au débit minoré, {budget.remaining():.1f} s restantes")
                if budget.attempts + APPELS_DE_LA_RELANCE > budget.max_attempts:
                    raise BudgetExceeded(
                        f"plafond d'appels trop bas pour la relance et sa vérification "
                        f"({budget.attempts}/{budget.max_attempts}, {APPELS_DE_LA_RELANCE} requis)")
                # La relance rédige et se vérifie sur la lecture **complétée** : sans cela, la
                # reprise ciblée n'aurait servi à rien — la facette redemandée n'aurait toujours
                # pas ses blocs sous les yeux du rédacteur. Cette lecture-là n'est adoptée que si
                # la vérification qu'elle produit l'est aussi (revue 4.2e, F).
                draft_2, step_rediger_2 = await rediger(parsed, retrieval_relance, [], client=client,
                                                        budget=budget,
                                                        index=index, doc_id=doc_id, settings=settings,
                                                        motif=motif_relance,
                                                        blocs_a_conserver=sorted(blocs_cites(acquise)),
                                                        blocs_hors_objet=_blocs_juges_hors_objet(acquise),
                                                        prompt="rediger_sinistre")
                draft_2 = _reconduire_acquis(draft, draft_2, acquise, settings,
                                             step=step_rediger_2)
                steps.append(step_rediger_2)
                appels_avant = budget.attempts  # la relance a abouti : seule la suite peut encore rater
                redaction_relancee = True
                relances += 1
                if draft_2.digest() == draft.digest():
                    step_rediger_2.checks.append(CheckResult(
                        name="relance_sans_effet", ok=False,
                        detail="l'ébauche relancée est identique (même hash canonique) : arrêt sur la première vérification"))
                else:
                    seconde, step_verifier_2 = await verifier(draft_2, parsed=parsed,
                                                              retrieval=retrieval_relance,
                                                              corpus=corpus, index=index, client=client,
                                                              budget=budget, settings=settings,
                                                              faits=faits, dossier=dossier)
                    steps.append(step_verifier_2)
                    # Une relance corrige, elle ne troque jamais l'acquis contre une autre qualité.
                    # Revue Codex 4.2a (B3) : aucun kind — confirmé ou non — ne contourne la
                    # dominance. La fusion `_reconduire_acquis` vient de reconduire les acquis et
                    # leurs limites dans l'ébauche relancée : une adoption légitime domine donc
                    # réellement sur les six axes (found, claims, facettes, blocs cités, complete,
                    # manques). La seule exception reste la garde de la campagne B 2.7 : une clause
                    # effectivement vérifiée bat zéro claim — il n'y a alors **aucun** acquis à
                    # perdre (facettes et blocs de l'acquise sont vides), et seul l'axe des manques
                    # peut reculer, ce qui est exactement le vide qu'une lecture tronquée
                    # transformait en 503.
                    relance_trouve_clause = seconde.found and not acquise.found
                    if relance_trouve_clause or domine(seconde, acquise,
                                                       redaction_nouvelle=True):
                        verification = seconde
                        # La lecture servie est celle que cette vérification-là a réellement vue :
                        # le complément de facettes n'est adopté qu'avec elle.
                        retrieval, truncated = retrieval_relance, retrieval_relance.truncated
                        # Story 4.2e : la vérification retenue est celle de l'ébauche relancée. Une
                        # reprise de contexte qui repartirait de la première soumettrait un lot où
                        # le `claim_id` de la demande n'existe pas — et, la dominance n'étant pas
                        # stricte, pourrait faire servir la rédaction d'avant la relance.
                        draft_verifie, step_de_la_verification = draft_2, step_verifier_2
                    else:
                        step_verifier_2.checks.append(CheckResult(
                            name="relance_moins_bonne", ok=False,
                            detail=f"la relance ne domine pas la première vérification "
                                   f"({len(seconde.claims)} affirmation(s) contre {len(acquise.claims)}, "
                                   f"{len(seconde.facettes_couvertes)} facette(s) couverte(s) contre "
                                   f"{len(acquise.facettes_couvertes)}, "
                                   f"{len(blocs_cites(seconde))} bloc(s) cité(s) contre "
                                   f"{len(blocs_cites(acquise))}, "
                                   f"complete={seconde.complete} contre {acquise.complete}, "
                                   f"manques={seconde.nb_manques} contre {acquise.nb_manques}) : "
                                   f"la première fait foi"))
            except (BudgetExceeded, Timeout, LlmParse, LlmUnavailable) as exc:
                # Même partage qu'au guide (AD-16, revue Codex 1.5, B5) : un appel **commencé** qui
                # échoue reste terminal, une relance qui n'a jamais démarré laisse l'acquis servir.
                #
                # **Amendement d'AD-16 (correctif du tour 2, rapport rédiger B), écrit comme tel.**
                # Une exception : quand la **rédaction relancée a abouti** et que seule sa
                # vérification échoue, la première vérification existe et elle est servable. La
                # relance est discrétionnaire — le pipeline l'a décidée, l'utilisateur ne l'a pas
                # demandée — et jeter une réponse acquise, vérifiée et complète parce qu'une
                # amélioration facultative a expiré est le contraire de ce que la règle protège. Le
                # cas est réel : un `APITimeoutError` sur un second *vérifier* mesuré à 26,3 s
                # (A16 #2) transforme un 200 valide en 503, à 34 % de marge sous `llm_timeout_s`.
                # La réponse acquise est donc servie, **jamais donnée pour complète** (même lacune
                # typée que la relance non démarrée), et l'échec reste nommé dans la trace avec son
                # étape et son coût. Si l'acquis n'a **rien** trouvé, il n'y a rien à servir : la
                # règle d'origine s'applique sans exception.
                if redaction_relancee and acquise.found:
                    if exc.step is not None:
                        steps.append(exc.step)
                    verification = relance_abandonnee(acquise)
                    step_verifier.checks.append(CheckResult(
                        name="relance_abandonnee", ok=False,
                        detail=f"la vérification de la relance a échoué ({exc.code.value}) : la "
                               f"première vérification, servable, fait foi — {exc.message}"))
                else:
                    commence = (budget.attempts > appels_avant
                                or (exc.step is not None and bool(exc.step.calls)))
                    if commence:
                        if exc.step is not None:
                            steps.append(exc.step)
                        exc.trace = tracer()
                        raise
                    verification = relance_abandonnee(acquise)
                    step_verifier.checks.append(CheckResult(
                        name="relance_abandonnee", ok=False,
                        detail=f"relance de rédiger non démarrée ({exc.code.value}) : "
                               f"la première vérification fait foi — {exc.message}"))
                    if not verification.found:
                        step_verifier.checks[-1].detail += " ; aucune affirmation n'avait survécu"

        # --- demande de contexte typée (story 4.2e) --------------------------
        # **Après** la relance d'AD-3, et jamais dans son chemin : ce sont deux mécanismes distincts.
        # La relance corrige une **rédaction** (une citation mal recopiée, une clause omise) en
        # repayant *rédiger* puis *vérifier* ; la demande de contexte, elle, ne reproche rien au
        # rédacteur — elle dit que le **contrôle** n'avait pas sous les yeux de quoi juger. Elle ne
        # rédige donc pas une seconde fois : elle rouvre du corpus en code pur, puis relit.
        #
        # Bornage strict, et il est structurel : `Verification.demande_contexte` est un objet, pas
        # une liste ; le bloc n'est pas une boucle ; une seconde demande est refusée plus bas. Une
        # satisfaction, une reprise, et rien d'autre ne peut s'enchaîner.
        demande = verification.demande_contexte
        if demande is not None:
            acquise = verification
            appels_avant = budget.attempts
            place = None
            # C2 : la reprise ne coûte qu'une vérification (`APPELS_DE_LA_REPRISE`) ; c'est donc sa
            # seule durée majorée qui décide, au lieu d'une marge fixe.
            duree_de_la_reprise = settings.duree_majoree_pour(settings.verifier_sinistre_max_tokens)
            if budget.remaining() <= duree_de_la_reprise:
                place = (f"temps insuffisant : {duree_de_la_reprise:.1f} s requises au débit "
                         f"minoré, {budget.remaining():.1f} s restantes")
            elif budget.attempts + APPELS_DE_LA_REPRISE > budget.max_attempts:
                place = (f"plafond d'appels trop bas ({budget.attempts}/{budget.max_attempts}, "
                         f"{APPELS_DE_LA_REPRISE} requis)")
            if place is not None:
                # Aucune passe, aucun appel : la place se contrôle **avant** de rouvrir quoi que ce
                # soit. Même conséquence qu'une demande insatisfaite, jamais une erreur terminale —
                # la réponse acquise est servie, sans être donnée pour complète.
                step_de_la_verification.checks.append(CheckResult(
                    name="reprise_sans_place", ok=False,
                    detail=f"demande de contexte non reprise ({place}) : la vérification acquise "
                           "fait foi, le contexte demandé n'a pas été relu"))
                verification = _contexte_non_relu(acquise, lecture_bornee=truncated or retrieval.truncated)
            else:
                complement, step_satisfaire = satisfaire_demande(
                    demande, retrieval=retrieval, corpus=corpus, index=index,
                    # **Le même objet** que la passe initiale (AD-1 : le budget borne toute l'étape,
                    # pas chaque passe). Le patron est celui du repli 4.2d juste au-dessus.
                    budget=borne_retrieval, settings=settings, doc_id=doc_id)
                # Fusion de trace, comme le repli déterministe : l'étape *retrouver* reste une, et
                # ses blocs rouverts rejoignent ceux qu'elle avait déjà publiés.
                step_retrouver.checks.extend(step_satisfaire.checks)
                step_retrouver.ms += step_satisfaire.ms
                step_retrouver.opened_block_ids = list(complement.opened_block_ids)
                step_retrouver.discarded_block_ids = list(complement.discarded_block_ids)
                # La borne de cette passe est celle de l'étape : un candidat écarté par le budget
                # est une lecture tronquée, et `Trace.truncations` doit le dire. Sans cela, la trace
                # publiait des blocs écartés en annonçant zéro troncature, et un refus repartait sur
                # une preuve d'absence qui promet un balayage exhaustif (NFR2).
                truncated = truncated or complement.truncated
                # La borne se dit aussi sur la lecture servie, même quand le complément n'est pas
                # adopté (revue 4.2e, F) : un refus aval ne doit jamais repartir d'une lecture
                # donnée pour exhaustive alors que le budget a écarté des candidats. Les **blocs**,
                # eux, restent ceux que la vérification servie a réellement vus — sans quoi la
                # réponse chiffrerait une lecture plus large que le jugement qui la porte.
                retrieval = retrieval.model_copy(update={
                    "truncated": truncated,
                    "discarded_block_ids": list(complement.discarded_block_ids)})
                neufs = len(complement.blocs) - len(retrieval.blocs)
                # Revue croisée 4.2e (I1) : **rouvrir ne suffit pas, il faut avoir tout rouvert.**
                # Une passe qui ouvre une cible et en écarte une autre sous la borne de l'étape n'a
                # relu le contexte demandé qu'à moitié. Reprendre là-dessus faisait juger sur un
                # contexte incomplet, et la reprise pouvait rendre un verdict décisoire : la place
                # insuffisante ne fermait plus vers `humain`. Une satisfaction partielle est donc
                # traitée exactement comme une satisfaction nulle — c'est la même phrase de trace,
                # parce que c'est la même conséquence pour qui lit la réponse.
                if neufs <= 0 or step_satisfaire.discarded_block_ids:
                    step_de_la_verification.checks.append(CheckResult(
                        name="demande_insatisfaite", ok=False,
                        detail="le contexte demandé n'existe pas dans le contrat lu, ou le budget "
                               "de l'étape ne laissait pas la place de le rouvrir en entier : "
                               "aucune reprise, la vérification acquise fait foi"))
                    verification = _contexte_non_relu(acquise, lecture_bornee=truncated or retrieval.truncated)
                else:
                    step_de_la_verification.checks.append(CheckResult(
                        name="demande_satisfaite", ok=True,
                        detail=f"{neufs} bloc(s) rouvert(s) pour la demande de contexte, sous le "
                               "budget de l'étape et sans appel modèle"))
                    try:
                        # L'ébauche relue est celle qui a **produit** cette vérification-là : c'est
                        # sur elle que la demande a été formulée, ce sont ses `claim_id` et ses
                        # citations. Le complément n'est adopté comme lecture servie que si la
                        # reprise l'est aussi — sinon la réponse chiffrerait une lecture que la
                        # vérification servie n'a pas vue.
                        reprise, step_verifier_reprise = await verifier(
                            draft_verifie, parsed=parsed, retrieval=complement, corpus=corpus,
                            index=index, client=client, budget=budget, settings=settings,
                            faits=faits, dossier=dossier)
                    except (BudgetExceeded, Timeout, LlmParse, LlmUnavailable) as exc:
                        # Même partage qu'à la relance (AD-16) : un appel **commencé** qui échoue
                        # reste terminal ; une reprise qui n'a jamais démarré laisse l'acquis servir.
                        if budget.attempts > appels_avant or (exc.step is not None
                                                              and bool(exc.step.calls)):
                            if exc.step is not None:
                                steps.append(exc.step)
                            exc.trace = tracer()
                            raise
                        step_de_la_verification.checks.append(CheckResult(
                            name="reprise_sans_place", ok=False,
                            detail=f"reprise de vérifier non démarrée ({exc.code.value}) : la "
                                   f"vérification acquise fait foi — {exc.message}"))
                        verification = _contexte_non_relu(acquise, lecture_bornee=truncated or retrieval.truncated)
                    else:
                        steps.append(step_verifier_reprise)
                        step_verifier_reprise.checks.append(CheckResult(
                            name="reprise_unique", ok=True,
                            detail="une seule reprise de vérifier après la satisfaction de la "
                                   "demande de contexte : aucune autre n'est possible"))
                        if reprise.demande_contexte is not None:
                            # Une reprise qui redemande du contexte ouvrirait la boucle que le
                            # bornage interdit. Elle est refusée **en entier** : son jugement s'est
                            # de nouveau déclaré non fondé, il ne peut pas remplacer l'acquis.
                            step_verifier_reprise.checks.append(CheckResult(
                                name="seconde_demande_refusee", ok=False,
                                detail="la reprise redemande du contexte : refusée sans être "
                                       "satisfaite — une satisfaction et une reprise, jamais deux"))
                            verification = _contexte_non_relu(acquise, lecture_bornee=truncated or retrieval.truncated)
                        elif domine(reprise, acquise):
                            # Revue 4.2e (L) : les lacunes de l'acquis sont **reconduites**. Une
                            # relance abandonnée faute de place avait posé sa lacune sur `acquise` ;
                            # adopter la reprise telle quelle l'effaçait, et la réponse repartait
                            # `complete=True` alors qu'une relance due n'avait jamais démarré. Une
                            # reprise qui domine sur les six axes ne dit rien de ce que l'acquis
                            # avait déjà constaté manquant.
                            verification = reprise.model_copy(update={
                                "lacunes": list(dict.fromkeys([*acquise.lacunes,
                                                               *reprise.lacunes])),
                                "complete": reprise.complete and acquise.complete})
                            # La lecture servie est celle que cette vérification-là a réellement
                            # vue : le complément n'est adopté qu'avec elle (revue 4.2e, F).
                            retrieval, truncated = complement, complement.truncated
                        else:
                            # Exactement la règle de la relance (AD-1) : une reprise qui perdrait
                            # une facette ou un bloc cité troquerait une sous-question contre une
                            # autre. Aucun axe nouveau, aucun score.
                            step_verifier_reprise.checks.append(CheckResult(
                                name="reprise_moins_bonne", ok=False,
                                detail=f"la reprise ne domine pas la première vérification "
                                       f"({len(reprise.claims)} affirmation(s) contre "
                                       f"{len(acquise.claims)}, "
                                       f"{len(reprise.facettes_couvertes)} facette(s) couverte(s) "
                                       f"contre {len(acquise.facettes_couvertes)}, "
                                       f"{len(blocs_cites(reprise))} bloc(s) cité(s) contre "
                                       f"{len(blocs_cites(acquise))}, "
                                       f"complete={reprise.complete} contre {acquise.complete}, "
                                       f"manques={reprise.nb_manques} contre "
                                       f"{acquise.nb_manques}) : la première fait foi"))
                            # Revue 4.2e (E) : cette fermeture-ci servait tout de même la réponse
                            # comme **complète**. C'était la seule des cinq à ne pas le dire, et la
                            # moins défendable : le jugement servi est celui rendu **avant**
                            # relecture du contexte, sur une affirmation que le contrôle avait
                            # déclarée injugeable. Pour une demande de `renvoi`, `complete=True`
                            # contredisait en outre AD-1 — « aucun renvoi non résolu sur une claim
                            # décisionnelle ». Le contexte demandé n'a pas nourri la réponse
                            # servie : elle le dit, comme les quatre autres fermetures.
                            verification = _contexte_non_relu(acquise, lecture_bornee=truncated or retrieval.truncated)

        # --- restituer ------------------------------------------------------
        echeance("restituer")
        compris, ignores = faits_compris(parsed.scope)
        if not verification.found and retrieval.truncated:
            # NFR2 / AD-1, la même règle que dans le pipeline guide (revue Codex 2.3, B3) : une
            # lecture **bornée** dont rien n'a survécu ne prouve aucune absence, et un contrat est
            # l'endroit où l'affirmer à tort coûte le plus cher — « aucune clause n'a été retrouvée »
            # lu sur un contrat que nous n'avons pas fini de lire est une réponse d'assureur.
            #
            # **Story 4.2f : l'absence reste interdite, le 503 ne l'est plus.** L'issue est une
            # `LecturePartielle` — combien de nœuds lus, combien de clauses transmises — servie en
            # 200 par *restituer*. Le verdict, lui, est celui que *vérifier* a calculé sur zéro clause
            # affichée : la règle (0bis) d'AD-6 rend `ne_tranche_pas`, et `_verdict_par_defaut` n'est
            # que la ceinture d'AD-16 pour un acquis venu d'ailleurs. Jamais un verdict de
            # remplacement, jamais un sinistre sans verdict.
            verification = _verdict_par_defaut(verification, dossier)
            answer, step_restituer = restituer(
                language=parsed.language, lang_fallback=parsed.lang_fallback,
                verification=verification,
                lecture_partielle=lecture_partielle_de(retrieval, doc_id=doc_id),
                faits_compris=compris, registre=REGISTRE_SINISTRE)
            noter_hors_borne(step_restituer, ignores)
            noter_depassement(step_restituer)
            steps.append(step_restituer)
            return answer, tracer()
        if not verification.found:
            # AD-3 : zéro claim survivante après la relance ⇒ refus motivé. Le verdict, lui, a bien été
            # calculé par *vérifier* sur zéro clause affichée : c'est un `ne_tranche_pas` gagné, et
            # *restituer* le recopie tel quel (AD-16 : jamais un refus sinistre sans verdict).
            verification = _verdict_par_defaut(verification, dossier)
            answer, step_restituer = restituer(language=parsed.language,
                                               lang_fallback=parsed.lang_fallback,
                                               verification=verification,
                                               reason=absence("claims_rejetes", parsed),
                                               faits_compris=compris,
                                               registre=REGISTRE_SINISTRE)
        else:
            answer, step_restituer = restituer(language=parsed.language,
                                               lang_fallback=parsed.lang_fallback,
                                               verification=verification,
                                               faits_compris=compris,
                                               registre=REGISTRE_SINISTRE)
        noter_hors_borne(step_restituer, ignores)
        noter_depassement(step_restituer)
        steps.append(step_restituer)
        return answer, tracer()

    try:
        return await chaine()
    except PipelineError as exc:
        # AD-16 : « 503 avec trace partielle ». Ce qui a déjà tourné voyage avec l'erreur.
        if exc.trace is None:
            exc.trace = tracer()
        raise
    except Exception as exc:  # noqa: BLE001 — la garde d'AD-16, pas un avalement
        # Correctif du tour 2 (rapport citations, B3). **Aucune exception ne sort nue de la
        # chaîne.** Une `ValidationError` échappée d'une étape — l'incident réel du 02/09/2026 —
        # remontait jusqu'à la couche HTTP sans être un `PipelineError` : l'utilisateur recevait un
        # 500 « erreur interne », **sans trace partielle**, après une minute payée, et aucun
        # diagnostic n'était possible. AD-16 exige une trace partielle sur tout échec terminal ; la
        # règle ne peut pas dépendre du type d'exception qu'un défaut interne aura pris.
        #
        # Ce n'est pas un avalement : l'erreur reste terminale et le code publié reste `internal`
        # (donc un 500, et `MESSAGE_INTERNE` côté client — rien du message d'origine n'est publié,
        # AD-15). Ce qui change est que la trace part avec, comme pour toute autre panne.
        interne = PipelineError(ErrorCode.internal, f"{type(exc).__name__} dans la chaîne sinistre")
        interne.trace = tracer()
        raise interne from exc


def _verdict_par_defaut(verification: Verification,
                        dossier: MissingPackage | None = None) -> Verification:
    """Ceinture d'AD-16 : une vérification sinistre sans verdict n'atteint jamais *restituer*.

    *vérifier* en mode sinistre en calcule toujours un — y compris sur zéro claim affichée, où la
    table rend `ne_tranche_pas` par sa règle (0bis). Le cas ne se produit donc que si l'acquis vient
    d'ailleurs (une vérification construite hors du mode sinistre par un appelant futur) ; il vaut
    mieux le combler ici, où l'on sait que la requête est un sinistre, que laisser passer un refus
    sans verdict jusqu'au front.
    """
    if verification.verdict is not None:
        return verification
    return verification.model_copy(
        update={"verdict": _verdict_de_refus("claims_rejetes", dossier)})


def run_followup(state: ContinuationState, action: ConversationAction, *, settings: Settings,
                 request_id: str) -> tuple[Answer, Trace, ContinuationState]:
    """Un tour de suivi entièrement pur : aucune lecture de corpus, aucun retrieval, aucun modèle.

    Les cinq étapes restent visibles parce qu'elles sont le vocabulaire stable de la trace. Les
    trois étapes normalement payantes disent explicitement qu'elles ont réutilisé l'état vérifié ;
    aucune ne contient de texte fourni par le client.
    """
    try:
        updated = appliquer(state, action, request_id=request_id,
                            ask_client_max=settings.ask_client_max,
                            max_turns=settings.conversation_max_turns,
                            active_questions_max=settings.conversation_active_questions_max)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    steps = [
        StepTrace(name="comprendre", checks=[CheckResult(
            name="reponse_liee", ok=True,
            detail="réponse rattachée à une question active de l'état signé")]),
        StepTrace(name="retrouver", checks=[CheckResult(
            name="corpus_reutilise", ok=True,
            detail="aucun retrieval : corpus et empreintes du premier tour réutilisés")]),
        StepTrace(name="rediger", checks=[CheckResult(
            name="sans_modele", ok=True,
            detail="aucun appel modèle : ajout d'un événement typé seulement")]),
        StepTrace(name="verifier", checks=[CheckResult(
            name="etat_signe", ok=True,
            detail="état décisif vérifié avant recalcul par la table AD-6")]),
        StepTrace(name="restituer", checks=[CheckResult(
            name="verdict_recalcule", ok=True,
            detail=f"tour {updated.turn} rendu déterministement")]),
    ]
    trace = Trace(
        request_id=request_id, pipeline=PIPELINE, variant=VARIANT, intent="suivi", steps=steps,
        total_cost_eur=0.0, source_hash={state.doc_id: state.source_hash},
        ingest_fingerprint={state.doc_id: state.ingest_fingerprint},
        pipeline_digest=state.pipeline_digest, prompts_digest=state.prompts_digest,
        thresholds=settings.thresholds(), retries=0, truncations=0,
    )
    return updated.answer, trace, updated
