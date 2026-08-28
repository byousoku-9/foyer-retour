"""AD-1 / AD-6 / AD-16 — Le pipeline sinistre : les mêmes cinq étapes, un verdict au bout.

`comprendre → retrouver → rédiger → vérifier → restituer`, dans cet ordre, toujours — exactement la
chaîne du guide (AD-1 : « l'ordre est constant »). Le sinistre ne fabrique **aucune** étape nouvelle :
il donne aux étapes existantes des prompts dédiés, leur transmet les faits déclarés, et laisse
*vérifier* dériver `applicable` puis appliquer la table d'AD-6. Le contrat de sortie est l'unique
`Answer` d'AD-4, dont `verdict` est un champ — il n'y a pas de second objet de réponse.

Trois différences avec `guide.py`, et rien d'autre :

- **les faits** : `Faits` est une entrée de plein droit (AD-5 les nomme déjà), bornée par le domaine
  (`description ≤ 2 000` caractères) et rejetée, jamais tronquée ;
- **la recherche** : *retrouver* reçoit `kinds_prioritaires` — à score égal, les blocs
  `garantie|exclusion|condition|franchise` passent devant (AC de la story) ;
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
from server.app.domain.answer import AbsenceProof, Answer, AnswerDraft, AnswerSegment, Claim, Verification
from server.app.domain.conversation import ConversationAction, ContinuationState, appliquer
from server.app.domain.errors import (
    BudgetExceeded,
    TruncatedRead,
    CorpusUnavailable,
    InvalidRequest,
    LlmParse,
    LlmUnavailable,
    PipelineError,
    Timeout,
)
from server.app.domain.profil import Profil
from server.app.domain.question import ClarificationRequise, Faits, ParsedQuestion, QuestionScope
from server.app.domain.trace import CheckResult, StepTrace, Trace
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
    libelles_de_blocs,
    normaliser_langue_pipeline,
    relance_abandonnee,
    relance_utile,
    retrieval_budget,
)
from server.app.steps.comprendre import comprendre
from server.app.steps.rediger import rediger
from server.app.steps.restituer import REGISTRE_SINISTRE, restituer
from server.app.steps.retrouver import retrouver_deterministe
from server.app.steps.verifier import verifier

PIPELINE = "sinistre"
# AD-1 : *retrouver* a deux variantes (déterministe en code pur, agentique en epic 4). À J+1 le
# sinistre n'en connaît qu'une, et une variante inconnue est refusée **avant** tout appel facturé
# plutôt que traitée comme la déterministe (AD-16 : jamais de dégradé silencieux).
VARIANTES = frozenset({"deterministe"})
VARIANT = "deterministe"

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
        for quote in claim.quotes:
            try:
                document = corpus.documents[index.doc_of(quote.block_id)]
                kind = document.block(quote.block_id).kind
            except KeyError:
                continue
            if kind in KINDS_FONDATEURS:
                return True
    return False


def _fondatrice_survivante(verification: Verification, *, corpus: Any, index: Any) -> bool:
    """Une claim affichée cite-t-elle une garantie ou exclusion confirmée qui peut fonder AD-6 ?

    Corrective 4.2a : le typage doit être **confirmé**, comme pour `_fondatrices_omises` et le
    prédicat décisionnel. Une clause au kind non confirmé vaut `applicable="humain"` (AD-6) — elle
    ne peut fonder ni `couvert` ni `non_couvert` — et la seule apparition d'un kind ne doit jamais
    faire perdre plusieurs acquis à la dominance.
    """
    for claim in verification.claims:
        for quote in claim.quotes:
            try:
                document = corpus.documents[index.doc_of(quote.block_id)]
                bloc = document.block(quote.block_id)
            except KeyError:
                continue
            if bloc.kind in KINDS_FONDATEURS and bloc.kind_confirmed:
                return True
    return False


def _fondatrices_omises(verification: Verification, retrieval: Any,
                        settings: Settings) -> list[str]:
    """Blocs `garantie|exclusion` confirmés retrouvés dont aucune claim survivante ne cite un seul.

    Preuve finale 4.2a : la rédaction peut ne rendre qu'une définition et un segment limite —
    aucun rejet, donc aucun motif, donc aucune relance — alors que le retrieval portait une clause
    décisionnelle confirmée. Le témoin d'AD-6 exige qu'une règle retrouvée soit rendue vérifiable,
    son applicabilité étant **calculée par le code** : une portée contraire vaut `applicable="non"`,
    jamais une omission. Le `kind` vient de l'ingestion, relu sur les blocs du retrieval ; aucun
    vocabulaire de la question n'entre dans la décision. Dès qu'une seule fondatrice est citée par
    une claim survivante, rien n'est signalé : la base décisionnelle existe. Ce déclencheur est
    complémentaire de `_fondatrice_rejetee` (clause citée mais rejetée) et de
    `_fondatrice_survivante` (adoption de la seconde vérification) : il couvre la clause jamais
    citée, que les deux autres ne voient pas.
    """
    fondatrices = [b.block_id for b in retrieval.blocs
                   if b.kind in KINDS_FONDATEURS and b.kind_confirmed]
    if not fondatrices:
        return []
    citees = {quote.block_id for claim in verification.claims for quote in claim.quotes}
    if citees & set(fondatrices):
        return []
    return fondatrices[:settings.draft_max_claims]


def _reconduire_acquis(draft: AnswerDraft, relance: AnswerDraft, acquise: Verification,
                       settings: Settings) -> AnswerDraft:
    """Fusionne les claims déjà vérifiées dans la relance, sans dépasser les bornes existantes.

    Une consigne de prompt aide le rédacteur à les reconduire, mais ne constitue pas une garantie :
    une sortie modèle peut l'ignorer. La fusion repart des claims **déjà soumises** au premier
    contrôle, sélectionnées par les identifiants effectivement retenus. Elle n'invente donc aucun
    texte ni aucune citation. Les acquis passent d'abord ; la place restante accueille la correction
    et ses identifiants conflictuels sont renommés localement. Les segments non factuels de la
    relance (limites, transitions) sont conservés après les factuels, sous `draft_max_segments`,
    comme `_rattacher_claims_sinistre` le fait déjà : `Answer.unknown` est rempli depuis les seuls
    segments `limite` (AD-4), et les supprimer ici ferait taire « Ce que je ne sais pas » puis
    abaisserait `nb_manques` avant la dominance. La seconde vérification relit ensuite tout ce
    résultat et la dominance reste l'autorité d'adoption.
    """
    acquis_ids = {claim.claim_id for claim in acquise.claims}
    claims: list[Claim] = [claim for claim in draft.claims if claim.claim_id in acquis_ids]
    utilises = {claim.claim_id for claim in claims}

    def identifiant_libre() -> str:
        for place in range(1, settings.draft_max_claims + 1):
            candidate = f"r{place}"
            if candidate not in utilises:
                return candidate
        raise ValueError("aucun identifiant de claim libre sous la borne de rédaction")

    for claim in relance.claims:
        if len(claims) >= settings.draft_max_claims:
            break
        if claim.claim_id in utilises:
            # Même contenu : le modèle a bien reconduit l'acquis, ne le duplique pas. Contenu
            # différent : sa correction reste contrôlable sous un identifiant non ambigu.
            ancienne = next(c for c in claims if c.claim_id == claim.claim_id)
            if ancienne.text == claim.text and ancienne.quotes == claim.quotes:
                continue
            claim = claim.model_copy(update={"claim_id": identifiant_libre()})
        utilises.add(claim.claim_id)
        claims.append(claim)

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
    non_factuels = [AnswerSegment(text=segment.text, kind=segment.kind, claim_ids=[])
                    for segment in relance.segments if segment.kind != "factuel"][:place]
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
              variant: str = "deterministe", lang: str | None = None, deadline_s: float | None = None,
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

    steps: list[StepTrace] = []
    relances = 0
    truncated = False
    intent: str | None = None

    def echeance(avant: str) -> None:
        """AD-1/AD-9 : la deadline monotone est vérifiée **avant** chaque étape, jamais après coup."""
        if budget.remaining() <= 0:
            raise Timeout(f"deadline épuisée avant l'étape {avant} ({budget.remaining():.1f} s restantes)")

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
        echeance("restituer")  # *restituer* est une étape : la deadline se vérifie avant elle aussi
        compris, ignores = faits_compris(scope)
        answer, step = restituer(language=language, lang_fallback=lang_fallback,
                                 reason=absence(kind, parsed),
                                 clarification=clarification,
                                 verdict=_verdict_de_refus(kind, dossier),
                                 faits_compris=compris,
                                 registre=REGISTRE_SINISTRE)
        noter_hors_borne(step, ignores)
        steps.append(step)
        return answer, tracer()

    async def chaine() -> tuple[Answer, Trace]:
        """Les cinq étapes. Sortie normale : un `Answer` et sa `Trace`. Échec terminal : `PipelineError`."""
        nonlocal relances, truncated, intent
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

        # --- retrouver (code pur) -------------------------------------------
        echeance("retrouver")
        retrieval, step_retrouver = retrouver_deterministe(
            parsed, corpus=corpus, index=index, budget=retrieval_budget(settings), settings=settings,
            doc_id=doc_id, kinds_prioritaires=KINDS_DECISIONNELS,
            dictionnaire=dictionnaire)
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

        # --- relance unique (AD-3) ------------------------------------------
        omises = _fondatrices_omises(verification, retrieval, settings)
        if (verification.motif and (
            relance_utile(verification, settings)
            or _fondatrice_rejetee(verification, corpus=corpus, index=index)
        )) or omises:
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
            acquise = verification
            appels_avant = budget.attempts
            try:
                if budget.remaining() <= settings.llm_retry_margin_s:
                    raise Timeout(f"marge insuffisante pour la relance ({budget.remaining():.1f} s restantes)")
                if budget.attempts + APPELS_DE_LA_RELANCE > budget.max_attempts:
                    raise BudgetExceeded(
                        f"plafond d'appels trop bas pour la relance et sa vérification "
                        f"({budget.attempts}/{budget.max_attempts}, {APPELS_DE_LA_RELANCE} requis)")
                draft_2, step_rediger_2 = await rediger(parsed, retrieval, [], client=client, budget=budget,
                                                        index=index, doc_id=doc_id, settings=settings,
                                                        motif=motif_relance,
                                                        blocs_a_conserver=sorted(blocs_cites(acquise)),
                                                        prompt="rediger_sinistre")
                draft_2 = _reconduire_acquis(draft, draft_2, acquise, settings)
                steps.append(step_rediger_2)
                appels_avant = budget.attempts  # la relance a abouti : seule la suite peut encore rater
                relances += 1
                if draft_2.digest() == draft.digest():
                    step_rediger_2.checks.append(CheckResult(
                        name="relance_sans_effet", ok=False,
                        detail="l'ébauche relancée est identique (même hash canonique) : arrêt sur la première vérification"))
                else:
                    seconde, step_verifier_2 = await verifier(draft_2, parsed=parsed, retrieval=retrieval,
                                                              corpus=corpus, index=index, client=client,
                                                              budget=budget, settings=settings,
                                                              faits=faits, dossier=dossier)
                    steps.append(step_verifier_2)
                    # Une relance corrige, elle ne troque jamais l'acquis contre une autre qualité.
                    # La rédaction reçoit donc les blocs déjà retenus à reconduire
                    # (`blocs_a_conserver`, puis la fusion `_reconduire_acquis`), et la dominance
                    # générique tranche : aucun kind (confirmé ou non) ne contourne found, claims,
                    # facettes, blocs cités, complétude ou nombre de manques.
                    # Campagne B 2.7 et revue corrective 4.2b (garde reconduite en 4.2a) : retrouver
                    # une clause fondatrice est une amélioration stricte non seulement sur zéro
                    # claim, mais aussi sur une auxiliaire survivante (p. ex. une définition). Sans
                    # cela `_fondatrice_rejetee` déclenche bien la relance, puis la dominance
                    # générale conserve justement la version qui n'a plus aucune base pour AD-6 ni
                    # question sur ses qualités.
                    relance_trouve_clause = seconde.found and (
                        not acquise.found
                        or (
                            _fondatrice_survivante(seconde, corpus=corpus, index=index)
                            and not _fondatrice_survivante(acquise, corpus=corpus, index=index)
                        )
                    )
                    if relance_trouve_clause or domine(seconde, acquise):
                        verification = seconde
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
                commence = budget.attempts > appels_avant or (exc.step is not None and bool(exc.step.calls))
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

        # --- restituer ------------------------------------------------------
        echeance("restituer")
        compris, ignores = faits_compris(parsed.scope)
        if not verification.found and retrieval.truncated:
            # NFR2 / AD-1, la même règle que dans le pipeline guide (revue Codex 2.3, B3) : une
            # lecture **bornée** dont rien n'a survécu ne prouve aucune absence, et un contrat est
            # l'endroit où l'affirmer à tort coûte le plus cher — « aucune clause n'a été retrouvée »
            # lu sur un contrat que nous n'avons pas fini de lire est une réponse d'assureur. Échec
            # terminal avec sa trace partielle (AD-16), jamais un `AbsenceProof`.
            raise TruncatedRead(
                "aucune clause n'a survécu à la vérification, et la lecture du contrat avait été "
                f"tronquée ({settings.max_opens} nœuds, {settings.retrieval_max_blocks} blocs, "
                f"{settings.retrieval_max_tokens} tokens) : aucune absence du contrat n'est affirmée")
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
        steps.append(step_restituer)
        return answer, tracer()

    try:
        return await chaine()
    except PipelineError as exc:
        # AD-16 : « 503 avec trace partielle ». Ce qui a déjà tourné voyage avec l'erreur.
        if exc.trace is None:
            exc.trace = tracer()
        raise


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
