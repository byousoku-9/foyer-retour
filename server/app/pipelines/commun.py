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

from functools import lru_cache
from typing import Any

from server.app.config import Settings
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.answer import Lacune, Verification
from server.app.domain.errors import InvalidRequest
from server.app.domain.langue import normaliser_langue_forcee
from server.app.domain.retrieval import RetrievalBudget
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
                           search_limit=settings.search_limit, max_llm_turns=settings.max_llm_turns,
                           max_blocks=settings.retrieval_max_blocks,
                           max_tokens=settings.retrieval_max_tokens,
                           profil_max_opens=settings.profil_max_opens)


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


def dictionnaire_de(dictionnaire: Any, doc_id: str) -> DictionnaireTrace | None:
    """AD-5 — L'état du dictionnaire **pour ce document**, donc l'état du refus « zéro hit ».

    `None` quand le pipeline n'en a pas (le sinistre) : la rubrique disparaît au lieu d'annoncer un
    dictionnaire inerte qui n'a jamais été consulté. Le pipeline du guide en reçoit toujours un —
    un fichier absent est un `Dictionnaire` inerte, pas un `None` (`api/etat`).

    `court_circuit_actif` est pris par `court_circuit_pour(doc_id)` et non par la propriété du même
    nom : c'est la décision **de cette requête**, et un dictionnaire qui décrit un autre document
    n'arme rien ici, quoi qu'il arme ailleurs (revue Codex 2.1, B3).
    """
    if dictionnaire is None:
        return None
    return DictionnaireTrace(charge=dictionnaire.charge, validated=dictionnaire.validated,
                             corpus_ok=dictionnaire.corpus_ok,
                             court_circuit_actif=dictionnaire.court_circuit_pour(doc_id))


def blocs_cites(verification: Verification) -> set[str]:
    """Les blocs du corpus sur lesquels repose ce qui est **affiché** : l'identité stable d'un contenu.

    Ni les `claim_id` (refaits à neuf par chaque appel de *rédiger*) ni les offsets d'une quote (qui
    bougent dès que le modèle recopie un passage un peu plus large) ne sont comparables d'une ébauche
    à l'autre. Le `block_id`, lui, est **notre** identifiant, produit par l'ingestion : deux ébauches
    de la même question qui s'appuient sur le même passage citent le même bloc.
    """
    return {q.block_id for c in verification.claims for q in c.quotes}


def domine(seconde: Verification, acquise: Verification) -> bool:
    """La seconde vérification est-elle au moins aussi bonne que l'acquise, sur **tous** les axes ?

    AD-3 relance pour *améliorer*. Compter les seules claims laissait passer une relance qui, à
    nombre égal, perdait `complete`, ajoutait un `unknown` ou remplaçait une affirmation par une
    autre moins bien placée (revue Codex 1.5, I2). La dominance est donc explicite, et elle porte sur
    des **ensembles** là où des compteurs ne suffisent pas : deux vérifications qui couvrent chacune
    une facette *différente* ont le même compte, et prendre la seconde échangerait une sous-question
    contre une autre (tour 3, I2). Sont donc exigés : trouver au moins autant, garder au moins autant
    d'affirmations, couvrir **au moins les mêmes facettes**, s'appuyer sur **au moins les mêmes
    blocs**, ne pas déclarer moins complet, ne pas laisser plus de **manques** (déclarés par le
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
    return (seconde.found >= acquise.found
            and len(seconde.claims) >= len(acquise.claims)
            and set(seconde.facettes_couvertes) >= set(acquise.facettes_couvertes)
            and blocs_cites(seconde) >= blocs_cites(acquise)
            and seconde.complete >= acquise.complete
            # `manques` et non `unknown` (story 2.3) : ce qui manque à une réponse se dit maintenant
            # dans deux canaux — ce que le modèle a déclaré, ce que le code a constaté —, et ne
            # comparer que le premier laisserait passer une relance qui a lu moins, couvert moins ou
            # perdu plus de phrases, pourvu qu'elle se taise autant.
            and seconde.nb_manques <= acquise.nb_manques)


def relance_abandonnee(verification: Verification) -> Verification:
    """La relance d'AD-3 n'a pas démarré : la réponse acquise est servie, mais pas donnée pour complète.

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
    """
    lacunes = list(verification.lacunes)
    if verification.found and LACUNE_RELANCE_ABANDONNEE not in lacunes:
        lacunes.append(LACUNE_RELANCE_ABANDONNEE)
    return verification.model_copy(update={"complete": False, "lacunes": lacunes})


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
    if not verification.rejected_claims:
        return False
    if not verification.found:
        return True
    if settings.relance_sur_non_pertinence:
        return True
    return any(c.rejection_kind in REJETS_DE_CITATION for c in verification.rejected_claims)
