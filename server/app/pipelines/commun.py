"""Ce que les deux pipelines partagent : les bornes d'entrée, la relance d'AD-3, les digests d'AD-10.

Le guide (1.5) et le sinistre (1.8) enchaînent les **mêmes** cinq étapes dans le **même** ordre
(AD-1) ; ce qui les distingue tient à leurs prompts, à leur document et à leur verdict. Les règles
d'arbitrage de la relance, elles, sont des invariants d'architecture — « une relance ne remplace
l'acquis que si elle le domine sur tous les axes », « une relance coûte deux appels indissociables »,
« les motifs de relance d'AD-3 sont des défauts de citation » — et deux copies de ces règles auraient
divergé au premier amendement. Elles vivent donc ici, testées une fois.

Ce module reste dans la couche `pipelines` : il n'importe ni `corpus`, ni `llm`, ni le SDK.
"""

from __future__ import annotations

from functools import lru_cache

from server.app.config import Settings
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.answer import Verification
from server.app.domain.retrieval import RetrievalBudget

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
                           max_tokens=settings.retrieval_max_tokens)


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
    blocs**, ne pas déclarer moins complet, ne pas déclarer plus d'inconnu. À égalité non dominante,
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
            and len(seconde.unknown) <= len(acquise.unknown))


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
