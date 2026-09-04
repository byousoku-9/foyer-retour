"""Ce que les deux pipelines partagent : les bornes d'entrée, la relance d'AD-3, les digests d'AD-10.

Le guide (1.5) et le sinistre (1.8) enchaînent les **mêmes** cinq étapes dans le **même** ordre
(AD-1) ; ce qui les distingue tient à leurs prompts, à leur document et à leur verdict. Les règles
d'arbitrage de la relance, elles, sont des invariants d'architecture — « une relance ne remplace
l'acquis que si elle le domine sur tous les axes », « une relance coûte deux appels indissociables »,
« les motifs de relance d'AD-3 sont des défauts de citation » — et deux copies de ces règles auraient
divergé au premier amendement. Elles vivent donc ici, testées une fois.

S'y ajoutent, depuis la story 2.5, les trois **résolutions de trace** d'AD-10 : les libellés des blocs
qu'une trace nomme, le gate du document interrogé, l'état du dictionnaire. Les deux pipelines
publient la même `Trace` et la même page la lit ; deux copies auraient divergé pour la même raison
que ci-dessus, et le sinistre n'a de toute façon pas de dictionnaire à décrire.

Ce module reste dans la couche `pipelines` : il n'importe ni `corpus`, ni `llm`, ni le SDK. Le
corpus et le dictionnaire lui arrivent **en paramètres** annotés `Any`, exactement comme aux deux
pipelines qui les reçoivent de l'API (table des couches du spine).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from server.app.config import Settings
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.answer import Lacune, LecturePartielle, Verification
from server.app.domain.errors import InvalidRequest
from server.app.domain.langue import normaliser_langue_forcee
from server.app.domain.question import CLARIFICATION_MAX_CHARS
from server.app.domain.retrieval import RetrievalBudget, RetrievalResult
from server.app.domain.trace import BlocTrace, DictionnaireTrace, GateTrace, StepTrace

# AD-5 : les intents qui se tranchent sur la seule sortie de *comprendre*, avant tout appel `reason`.
INTENTS_REFUSES = frozenset({"meteo", "bavardage", "hors_perimetre"})
# AD-3 nomme les motifs de relance par des défauts de **citation** (« quote introuvable dans block_id
# X, bloc heading, quote trop courte ») : ce sont eux que le modèle peut corriger en recopiant mieux.
# `ambigue` en fait partie — et depuis la story 1.8, il couvre aussi « une seule clause par
# affirmation » (D6), qui est bien un défaut de la citation, corrigeable en éclatant la claim.
REJETS_DE_CITATION = frozenset({"non_retrouvee", "ambigue"})
# Ce que coûte la relance d'AD-3 en **appels** : rédiger une seconde fois, puis vérifier ce qu'elle a
# rendu. Les deux sont indissociables — AD-3 interdit de montrer un draft relancé mais non vérifié.
APPELS_DE_LA_RELANCE = 2
# La lacune qu'une relance non démarrée laisse dans la réponse (story 2.3). C'est une cause typée,
# pas une phrase : *restituer* seul la projette dans la langue de la réponse. Elle vit ici parce que
# seul le pipeline sait qu'une relance a été empêchée.
LACUNE_RELANCE_ABANDONNEE = Lacune(kind="relance_abandonnee")


def lecture_partielle_de(retrieval: RetrievalResult, *, doc_id: str) -> LecturePartielle:
    """Story 4.2f — ce que la lecture a **effectivement** couvert, chiffré depuis le retrieval.

    Les deux compteurs se lisent sur ce qui est parti au modèle, jamais sur la taille du document :
    `AbsenceProof.blocks_scanned` publie `len(document.blocks)`, c'est-à-dire l'annonce d'un balayage
    exhaustif — exactement ce qu'une lecture bornée ne peut pas promettre. `opened_node_ids` est déjà
    filtré par *retrouver* sur les nœuds ayant réellement contribué aux blocs transmis.

    Partagé par les deux pipelines : deux copies auraient divergé au premier amendement, et c'est un
    chiffre que l'utilisateur lit.
    """
    return LecturePartielle(nodes_read=len(retrieval.opened_node_ids),
                            blocks_read=len(retrieval.blocs),
                            documents=[doc_id])


def normaliser_langue_pipeline(lang: str | None) -> str | None:
    """Validation commune des langues forcées par les deux pipelines, avant tout appel facturé."""
    try:
        return normaliser_langue_forcee(lang)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


@lru_cache(maxsize=1)
def digests() -> tuple[str, str]:
    """Repli mémoïsé : `pipeline_digest()`/`prompts_digest()` relisent toute l'arborescence du code.

    L'appelant les calcule une fois au démarrage (story 1.6) et les passe ; sans lui, on les calcule
    au premier appel et on les garde — jamais à chaque requête (des dizaines de fichiers lus).
    """
    return pipeline_digest(), prompts_digest()


def retrieval_budget(settings: Settings) -> RetrievalBudget:
    """AD-1 : le budget borne **toute** l'étape. Reprise 1.4 : `max_blocks`/`max_tokens` venaient de
    `config.py` mais personne ne les renseignait — *rédiger* levait `BudgetExceeded` sur une fiche
    entière au lieu de recevoir un retrieval borné."""
    return RetrievalBudget(max_opens=settings.max_opens, node_window=settings.node_window,
                           search_limit=settings.search_limit,
                           max_blocks=settings.retrieval_max_blocks,
                           max_tokens=settings.retrieval_max_tokens)


def libelles_de_blocs(corpus: Any, doc_id: str, steps: list[StepTrace]) -> list[BlocTrace]:
    """Les blocs qu'une trace nomme, résolus jusqu'au titre de leur fiche (AD-10, story 2.5).

    **Union ordonnée et dédupliquée** des `opened_block_ids` puis des `discarded_block_ids` de
    **toutes** les étapes : l'AC réclame « les blocs ouverts **et** écartés », et un bloc qu'une étape
    ouvre après qu'une autre l'a écarté ne doit paraître qu'une fois. L'ordre est celui de la trace,
    donc celui de la chaîne : il est déterministe et se relit à côté des étapes.

    **La règle de `fiche_id` est celle d'`api/presenter._source_item`, pas une seconde** : le nœud
    parent privé du préfixe `{doc_id}:f`, `None` s'il ne commence pas par là. Deux règles pour un même
    identifiant, c'est un jour où le panneau nomme une fiche que `sources[]` nomme autrement.

    **Un id non résolu est omis, jamais inventé** (M3) : bloc d'un autre document, bloc absent du
    corpus servi, document hors du corpus. Le front affiche alors l'identifiant seul — il l'a par
    `StepTrace` — et n'a aucun titre à deviner (AD-16).

    Le rattachement passe par `Document.node_of()`, qui **est** la table qu'`Index.parent_node()`
    publie (l'index la construit depuis les mêmes `Node.items`) : la prendre sur le document évite de
    faire voyager l'index jusqu'ici pour lire deux fois le même fait, et referme le cas « le bloc
    appartient à un autre document que celui qu'on interroge » sans le tester à part.
    """
    document = corpus.documents.get(doc_id)
    if document is None:
        return []
    noeuds = {n.node_id: n for n in document.nodes}
    prefixe = f"{doc_id}:f"
    vus: set[str] = set()
    sortie: list[BlocTrace] = []
    for step in steps:
        for block_id in (*step.opened_block_ids, *step.discarded_block_ids):
            if block_id in vus:
                continue
            vus.add(block_id)
            try:
                node_id = document.node_of(block_id)
            except KeyError:
                continue  # pas un bloc de ce document : rien à en dire, donc rien n'est dit
            node = noeuds.get(node_id)
            if node is None:
                continue
            sortie.append(BlocTrace(
                block_id=block_id, doc_id=doc_id, node_id=node_id,
                fiche_id=node_id[len(prefixe):] if node_id.startswith(prefixe) else None,
                titre=node.title))
    return sortie


def gate_de(corpus: Any, doc_id: str) -> GateTrace | None:
    """AD-7 / AD-14 — Ce qui valide le document interrogé, tel que le manifest chargé le porte.

    Trois états, et aucun repli entre eux : pas d'entrée de manifest ⇒ `None` (on ne sait rien, la
    rubrique disparaît de l'écran) ; entrée sans gate ⇒ un `GateTrace` dont les trois champs sont
    `None` mais dont les `alerts` sont dites (c'est là que `sans_gate` s'écrit) ; entrée gatée ⇒ le
    profil, le nombre de cas et la contresignature **écrits par le run qui les a constatés**.

    **Le profil est celui du manifest, même quand le loader a neutralisé le gate localement**
    (`sans_gate`, empreintes du gate différentes de l'entrée). C'est un choix, et il tient à ce que
    les alertes voyagent dans le même objet : `EtatApp.gate_profile` doit rendre `null` parce qu'il
    **résume** les documents servis en un scalaire que la page d'accueil affiche seul — ici, profil et
    alertes sont lus ensemble, et « vertical, sans_gate » dit strictement plus que « rien ». Rien
    n'est tu, ce qui est la seule chose qu'AD-11 exige.
    """
    entry = corpus.manifest.get(doc_id)
    if entry is None:
        return None
    alerts = list(corpus.alerts.get(doc_id, []))
    gate = entry.gate
    if gate is None:
        return GateTrace(alerts=alerts)
    return GateTrace(profile=gate.profile, cases=gate.cases, countersigned=gate.countersigned,
                     alerts=alerts)


def dictionnaire_de(dictionnaire: Any, doc_id: str, *,
                    court_circuit_autorise: bool = True) -> DictionnaireTrace | None:
    """AD-5 — L'état du dictionnaire **pour ce document**, donc l'état du refus « zéro hit ».

    `None` quand le pipeline n'en a pas : la rubrique disparaît au lieu d'annoncer un dictionnaire
    inerte qui n'a jamais été consulté. Guide et contrats peuvent chacun recevoir leur dictionnaire
    propriétaire ; un fichier absent est un `Dictionnaire` inerte, pas un `None` (`api/etat`).

    `court_circuit_actif` est pris par `court_circuit_pour(doc_id)` et non par la propriété du même
    nom : c'est la décision **de cette requête**, et un dictionnaire qui décrit un autre document
    n'arme rien ici, quoi qu'il arme ailleurs (revue Codex 2.1, B3). La politique de la requête peut
    en outre le désarmer, notamment sous `perimetre_tronque` (amendement AD-5, story 2.5).
    """
    if dictionnaire is None:
        return None
    return DictionnaireTrace(charge=dictionnaire.charge, validated=dictionnaire.validated,
                             corpus_ok=dictionnaire.corpus_ok,
                             court_circuit_actif=(court_circuit_autorise
                                                  and dictionnaire.court_circuit_pour(doc_id)))


def blocs_cites(verification: Verification) -> set[str]:
    """Les blocs du corpus sur lesquels repose ce qui est **affiché** : l'identité stable d'un contenu.

    Ni les `claim_id` (refaits à neuf par chaque appel de *rédiger*) ni les offsets d'une quote (qui
    bougent dès que le modèle recopie un passage un peu plus large) ne sont comparables d'une ébauche
    à l'autre. Le `block_id`, lui, est **notre** identifiant, produit par l'ingestion : deux ébauches
    de la même question qui s'appuient sur le même passage citent le même bloc.
    """
    return {q.block_id for c in verification.claims for q in c.quotes}


def blocs_par_facette(verification: Verification) -> dict[int, set[str]]:
    """Les blocs qui fondent **chaque** sous-question couverte, et non l'ensemble en vrac.

    `blocs_cites` répond « sur quoi repose ce qui est affiché » ; il ne dit pas *à quoi* chaque bloc
    sert. La dominance a besoin des deux : ce qui garantit qu'une relance n'échange pas une
    sous-question contre une autre, c'est que chaque sous-question déjà couverte garde une base —
    pas que tout bloc jamais cité soit reconduit. `facettes_claims` porte cet appariement, mesuré
    par *vérifier* sur les claims **affichées** ; les claims rejetées n'y entrent pas, comme dans
    `blocs_cites`.
    """
    par_id = {claim.claim_id: claim for claim in verification.claims}
    couvertes = set(verification.facettes_couvertes)
    return {rang: {quote.block_id for cid in cids if cid in par_id
                   for quote in par_id[cid].quotes}
            for rang, cids in verification.facettes_claims.items() if rang in couvertes}


def preuves_citees(verification: Verification) -> set[tuple[str, str]]:
    """Les **passages** sur lesquels repose ce qui est affiché, pas seulement leurs blocs.

    `blocs_cites` compare des ensembles de `block_id` : deux vérifications qui citent le même bloc
    par deux passages différents y sont indistinguables, et une relance qui n'a fait que reformuler
    y paraît aussi fondée que l'acquise. Le passage canonique tranche (`Claim.preuve`).
    """
    return {preuve for claim in verification.claims for preuve in claim.preuve}


def domine(seconde: Verification, acquise: Verification, *,
           redaction_nouvelle: bool = False) -> bool:
    """La seconde vérification est-elle au moins aussi bonne que l'acquise, sur **tous** les axes ?

    AD-3 relance pour *améliorer*. Compter les seules claims laissait passer une relance qui, à
    nombre égal, perdait `complete`, ajoutait un `unknown` ou remplaçait une affirmation par une
    autre moins bien placée (revue Codex 1.5, I2). La dominance est donc explicite, et elle porte sur
    des **ensembles** là où des compteurs ne suffisent pas : deux vérifications qui couvrent chacune
    une facette *différente* ont le même compte, et prendre la seconde échangerait une sous-question
    contre une autre (tour 3, I2). Sont donc exigés : trouver au moins autant, garder au moins autant
    d'affirmations, couvrir **au moins les mêmes facettes**, s'appuyer sur **au moins les mêmes
    blocs** (sauf à répondre à une sous-question de plus, voir `blocs_conserves`), ne pas déclarer
    moins complet, ne pas laisser plus de **manques** (déclarés par le
    modèle ou constatés par le code). À égalité non dominante,
    l'acquis fait foi.

    Les rangs de facettes sont stables entre les deux ébauches : le découpage vient de *comprendre*,
    qui n'a tourné qu'une fois pour la requête (AD-4). Les `block_id` le sont aussi — ils viennent de
    l'ingestion. C'est ce qui rend la comparaison possible sans rien inventer ; l'appariement plus fin
    des passages (mêmes offsets, même phrase) reste une reprise ouverte vers 4.2.

    Le `Verdict` n'entre **pas** dans la comparaison (story 1.8) : il n'a pas d'ordre. Un
    `non_couvert` n'est ni meilleur ni pire qu'un `couvert` — il est plus ou moins *fondé*, et ce que
    « mieux fondé » veut dire est exactement ce que les six axes ci-dessus mesurent déjà (plus de
    clauses affichées, sur au moins les mêmes blocs). Classer les valeurs entre elles reviendrait à
    préférer une réponse pour ce qu'elle conclut.
    """
    couvre_plus = set(seconde.facettes_couvertes) > set(acquise.facettes_couvertes)
    # Correctif du tour 2 (rapport citations, A1). **Une rédaction relancée qui n'apporte aucun
    # passage neuf n'apporte rien.** Le compte de claims est le seul axe gonflable — `blocs_cites`
    # et `facettes_couvertes` sont des ensembles —, et une paraphrase dupliquée le gonflait : la
    # relance « dominait » et remplaçait l'acquis par lui-même, dit deux fois.
    #
    # `redaction_nouvelle` dit que la seconde vérification juge une **ébauche réécrite**, seul cas
    # où « mêmes passages » signifie « rien de neuf ». La reprise après demande de contexte (4.2e)
    # relit **la même ébauche** avec plus de contexte : ses passages sont identiques par
    # construction, et l'y appliquer reviendrait à interdire par principe ce que la story a
    # construit. Et sans aucune preuve d'un côté comme de l'autre — deux ébauches entièrement
    # rejetées —, il n'y a rien à dupliquer : les autres axes décident, comme avant.
    if (redaction_nouvelle and preuves_citees(acquise)
            and preuves_citees(seconde) == preuves_citees(acquise) and not couvre_plus):
        return False
    # Correctif du tour 2 (rapport retrouver, correctif 8). **Répondre à une sous-question de plus
    # vaut plus que déclarer une réserve de moins.** L'axe des manques est le seul qui puisse faire
    # écarter une relance strictement meilleure sur la couverture : un `unknown` de plus, une limite
    # honnêtement nommée, et une réponse qui traitait enfin les deux moitiés de la question était
    # rejetée — c'est ce qui s'est produit sur A16 #2 (`manques=4 contre 3`). L'exception est
    # **fermée** : elle exige une couverture **strictement** plus large et des passages qui
    # contiennent au moins ceux de l'acquise. Rien n'est perdu, et une sous-question de plus est
    # rendue.
    manques_admis = (seconde.nb_manques <= acquise.nb_manques
                     or (couvre_plus and preuves_citees(seconde) >= preuves_citees(acquise)))
    # Correctif du tour 10 (A16 r1, `a16-final1/a16-r1.json`). **Une extension marginale de moins ne
    # se paie pas d'une sous-question de moins.** Sur ce run, l'acquise citait `p39:9`, `p39:10` et
    # `p40:6` pour **une** facette sur deux ; la relance citait `p39:9`, `p40:6` et `p34:11` et
    # couvrait les **deux**, avec moins de manques. Elle était écartée pour le seul `p39:10`, une
    # extension marginale — et la réponse servie ignorait la fumée.
    #
    # Ce que l'axe protège est intact et n'est pas déplacé d'un cran : **on n'échange jamais une
    # sous-question contre une autre.** C'est pourquoi la levée exige une couverture **strictement**
    # plus large (les facettes de l'acquise sont déjà toutes reprises, axe précédent) et vérifie,
    # sous-question par sous-question, qu'au moins un des blocs qui la fondaient reste cité. Ce qui
    # n'est plus exigé, c'est le sur-ensemble des blocs qui ne fondaient **aucune** sous-question
    # couverte. La table `facettes_claims` doit couvrir exactement les facettes déclarées couvertes :
    # sans cet appariement, il n'y a rien à vérifier par facette et la règle historique s'applique.
    par_facette = blocs_par_facette(acquise)
    blocs_conserves = (blocs_cites(seconde) >= blocs_cites(acquise)
                       or (couvre_plus
                           and set(par_facette) == set(acquise.facettes_couvertes)
                           and all(blocs & blocs_cites(seconde)
                                   for blocs in par_facette.values())))
    return (seconde.found >= acquise.found
            and len(seconde.claims) >= len(acquise.claims)
            and set(seconde.facettes_couvertes) >= set(acquise.facettes_couvertes)
            and blocs_conserves
            and seconde.complete >= acquise.complete
            # `manques` et non `unknown` (story 2.3) : ce qui manque à une réponse se dit maintenant
            # dans deux canaux — ce que le modèle a déclaré, ce que le code a constaté —, et ne
            # comparer que le premier laisserait passer une relance qui a lu moins, couvert moins ou
            # perdu plus de phrases, pourvu qu'elle se taise autant.
            and manques_admis)


def relance_abandonnee(verification: Verification) -> Verification:
    """La relance d'AD-3 n'a pas démarré : la réponse acquise est servie, et elle le dit.

    AD-4 : « `complete=True` exige aucune troncature de budget » — un plafond d'appels ou une
    deadline qui empêchent la relance en est une. Story 2.3 : `complete=False` ne suffit plus, parce
    que le domaine fait désormais de `complete ⟺ found ∧ rien qui manque` un invariant — une réponse
    déclarée incomplète dont la section « Ce que je ne sais pas » est vide est un « PARTIEL » que
    l'utilisateur lit sans savoir ce qui manque, et c'est exactement ce que l'AC de la story corrige.
    La cause typée est composée **par le code** (AD-16) et ne nomme ni budget ni code d'erreur : le
    chiffre est dans `Trace` et le motif dans le `CheckResult` de l'appelant. Elle rejoint
    `Verification.lacunes` et non `unknown` (revue coordonnée 2.3, A3) ; *restituer* la projettera
    dans la langue de la réponse, comme toutes les autres lacunes du code.

    Aucune lacune sur un refus : `found=False` porte déjà son `AbsenceProof`, qui dit tout.
    Les deux pipelines passent par ici — deux copies auraient divergé au premier amendement.

    **Story 5.6 (L1i) : la cause est posée, le badge ne l'est plus.** « Je n'ai pas pu reprendre ma
    réponse pour l'améliorer » est un avis de service (`LACUNES_AVIS`) : la réponse servie est celle
    que le contrôle avait vérifiée du premier coup, chacune de ses phrases est appuyée par un
    passage cité, et aucune sous-question n'a disparu au passage. La dire « partielle » à ce titre
    faisait lire un échec là où il n'y a qu'un renoncement à mieux faire. `complete` n'est donc plus
    forcé ici — il reste celui que *vérifier* a calculé sur les manques —, et la lacune, elle, est
    toujours déposée : elle atteindra l'utilisateur dans `Answer.avis[]`.
    """
    lacunes = list(verification.lacunes)
    if verification.found and LACUNE_RELANCE_ABANDONNEE not in lacunes:
        lacunes.append(LACUNE_RELANCE_ABANDONNEE)
    return verification.model_copy(update={"lacunes": lacunes})


def relance_utile(verification: Verification, settings: Settings) -> bool:
    """Une relance de *rédiger* a-t-elle une chance de changer quelque chose (AD-3) ?

    Oui dans deux cas, et deux seulement : un défaut de citation (le modèle peut recopier
    correctement), ou **rien** qui ait survécu (la relance est alors le seul chemin vers une réponse,
    et AD-3 fait du refus `claims_rejetes` ce qui vient *après* elle).

    Non quand la réponse tient déjà et que les seuls rejets sont des jugements de pertinence : les
    claims écartées sont **conservées** dans `rejected_claims[]` comme AD-3 le demande, et payer un
    second appel `reason` (≈ 0,03 €, le tiers du budget de la requête) pour rattraper une affirmation
    que le code a décidé de ne pas montrer contredirait NFR4. C'est une lecture d'AD-3 — dont les
    exemples de motifs sont tous des défauts de citation — et non une évidence : le seuil
    `relance_sur_non_pertinence` la rend explicite et mesurable par les questions-témoins (4.2).
    """
    if verification.facettes_melangees:
        # Story 5.7 (L1q) : un quatrième cas, du même ordre que le troisième. Une affirmation qui
        # fond deux sous-questions n'est pas « une affirmation de moins » : c'est la réponse à deux
        # parts de la question rendue en un paragraphe qui n'en traite bien aucune, et seul le
        # modèle peut la découper (AD-1 : le code ne réécrit pas). Sans cette ligne, le rejet
        # n'aurait relancé que lorsque **rien** n'avait survécu — une affirmation fondue au milieu
        # d'affirmations propres aurait été perdue sans réparation.
        return True
    if verification.phrases_a_reecrire:
        # Story 5.6 (L1g) : un troisième cas, et il est du même ordre que le premier. Le paragraphe
        # servi n'est pas « une affirmation de moins » mais un texte amputé de ses liaisons, que
        # personne n'a écrit tel quel ; seul le modèle peut le refaire, une phrase par passage. Il ne
        # coûte pas un appel de plus : c'est la relance unique d'AD-3, déjà bornée par le pipeline.
        return True
    if not verification.rejected_claims:
        return False
    if not verification.found:
        return True
    if settings.relance_sur_non_pertinence:
        return True
    return any(c.rejection_kind in REJETS_DE_CITATION for c in verification.rejected_claims)


# --- Ce que le guide **propose** quand il ne répond pas (tour G1) -------------------------------
#
# Un refus par intent court-circuite tout retrieval (AD-5) : *restituer* rend alors sa phrase
# d'absence, et rien d'autre. Mesuré sur la batterie du 03/09, cette phrase seule est un mur — « Cette
# question sort de ce que couvre le guide » ne dit pas *ce que le guide couvre*, et une salutation la
# recevait comme un rejet. Le pipeline compose donc, **par du code et sans un appel de plus**, la
# phrase d'ouverture qui l'accompagne : `Answer.clarification`, que les deux fronts rendent déjà à
# côté de la phrase de refus.
#
# Sa matière est le **sommaire du document servi** (`Corpus.perimetres`, la projection des titres que
# *comprendre* a lue) : les thèmes proposés sont donc ceux que le guide traite réellement, jamais une
# liste écrite à la main qui vieillirait à la première fiche ajoutée. Aucun texte de bloc n'y entre —
# ce sont des titres de nœuds, écrits par l'ingestion (AD-10).
THEMES_ORIENTATION = 3
THEMES_ACCUEIL = 5
PHRASES_ORIENTATION: dict[str, str] = {
    "fr": "Je peux vous aider sur : {themes}.",
    "en": "I can help you with: {themes}.",
    "de": "Ich kann Ihnen zu folgenden Themen helfen: {themes}.",
    "pt": "Posso ajudá-lo sobre: {themes}.",
}
PHRASES_ACCUEIL: dict[str, str] = {
    "fr": "Bonjour ! Posez-moi une question sur votre installation : {themes}…",
    "en": "Hello! Ask me a question about settling in: {themes}…",
    "de": "Guten Tag! Stellen Sie mir eine Frage zu Ihrer Ankunft: {themes}…",
    "pt": "Olá! Faça-me uma pergunta sobre a sua instalação: {themes}…",
}


def _sans_accents(texte: str) -> str:
    """Minuscules sans accents — la comparaison de deux titres, pas une recherche dans le corpus.

    `corpus.text.normalize` ferait mieux, et le pipeline n'a pas le droit de l'importer (table des
    couches du spine : `pipelines → steps, domain, config, digests`). Ce n'est pas une perte : ce
    qui est comparé ici est un titre de rubrique contre un terme de recherche, pas un passage.
    """
    decompose = unicodedata.normalize("NFD", texte.lower())
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn")


def rubriques_du_perimetre(perimetre: str) -> list[tuple[str, str]]:
    """`[(titre de rubrique, titre + ses fiches)]`, dans l'ordre du sommaire.

    Le format vient de `corpus/loader._perimetre` : une ligne par rubrique de niveau 1,
    `- Logement : Signer un bail, Assurer son logement`, et son palier dégradé `- Logement`. Les deux
    se lisent ici, parce que c'est le même sommaire dégradé qui arme l'alerte `perimetre_tronque` et
    qu'un refus sous cette alerte doit tout de même savoir quoi proposer.
    """
    rubriques: list[tuple[str, str]] = []
    for ligne in perimetre.splitlines():
        ligne = ligne.strip()
        if not ligne.startswith("- "):
            continue
        corps = ligne[2:].strip()
        titre = corps.split(" : ", 1)[0].strip()
        if titre:
            rubriques.append((titre, corps))
    return rubriques


def themes_proches(perimetre: str, termes: Sequence[str], *, garder: int) -> list[str]:
    """Les rubriques du sommaire les plus **proches** des termes cherchés, au plus `garder`.

    La proximité est un simple recouvrement de mots entre les termes de la question et la ligne
    entière de la rubrique (son titre **et** ses fiches) : « déposer des bitcoins à la commune »
    touche « Démarches » par *commune*, sans qu'aucune fiche ne parle de bitcoins. Le score ne décide
    rien — il ne sert qu'à ordonner une proposition —, et l'égalité se tranche par l'ordre du
    sommaire, qui est celui du guide.

    Aucun terme, ou aucun recouvrement : les premières rubriques du sommaire. Une proposition vaut
    toujours mieux qu'un refus nu, et le guide commence par ce qui concerne le plus d'arrivants.
    """
    rubriques = rubriques_du_perimetre(perimetre)
    if not rubriques:
        return []
    mots = {m for terme in termes for m in _sans_accents(terme).split() if len(m) > 3}
    proches: list[str] = []
    if mots:
        scores = [(sum(m in _sans_accents(ligne) for m in mots), -rang, titre)
                  for rang, (titre, ligne) in enumerate(rubriques)]
        proches = [titre for score, _, titre in sorted(scores, reverse=True) if score][:garder]
    # Un seul recouvrement ne fait pas trois thèmes : le reste vient de la tête du sommaire, celle
    # par laquelle le guide commence. Proposer une piste et s'arrêter là serait plus étroit que le
    # refus qu'on accompagne.
    for titre, _ in rubriques:
        if len(proches) >= garder:
            break
        if titre not in proches:
            proches.append(titre)
    return proches


def _phrase_de_themes(patron: str, themes: Sequence[str], *, max_chars: int) -> str:
    """Le patron rempli, borné en **retirant un thème entier**, jamais en coupant un titre.

    `Answer.clarification` est bornée par `CLARIFICATION_MAX_CHARS` : un sommaire aux titres
    inhabituellement longs ferait sinon lever une `ValidationError` pydantic sur le chemin le plus
    exposé du guide — celui du refus (AD-16). Sans aucun thème tenable, la phrase n'est pas rendue :
    *restituer* garde alors son refus nu, qui reste vrai.
    """
    retenus = list(themes)
    while retenus:
        phrase = patron.format(themes=", ".join(retenus))
        if len(phrase) <= max_chars:
            return phrase
        retenus.pop()
    return ""


def orientation_de(perimetre: str, termes: Sequence[str], language: str) -> str | None:
    """« Je peux vous aider sur : … » — trois thèmes proches, ou `None` si le sommaire est vide."""
    themes = themes_proches(perimetre, termes, garder=THEMES_ORIENTATION)
    patron = PHRASES_ORIENTATION.get(language) or PHRASES_ORIENTATION["fr"]
    return _phrase_de_themes(patron, themes, max_chars=CLARIFICATION_MAX_CHARS) or None


def accueil_de(perimetre: str, language: str) -> str | None:
    """La salutation rendue à un `bavardage` : elle **demande** la question au lieu de refuser.

    Elle ne cherche aucune proximité — il n'y a rien à quoi être proche : « Bonjour » ne porte pas de
    termes. Ce sont donc les premières rubriques du sommaire, celles par lesquelles le guide commence.
    """
    themes = themes_proches(perimetre, (), garder=THEMES_ACCUEIL)
    patron = PHRASES_ACCUEIL.get(language) or PHRASES_ACCUEIL["fr"]
    return _phrase_de_themes(patron, themes, max_chars=CLARIFICATION_MAX_CHARS) or None
