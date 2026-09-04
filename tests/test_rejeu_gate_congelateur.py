"""Rejeu **hors ligne** des trois répétitions du gate Baloise `-16` sur le cas `b-congelateur`.

Story 5.7 (L1p). Le gate a rendu `sous_conditions`, `sous_conditions` puis `ne_tranche_pas` sur le
même cas et le même corpus : signatures divergentes, `stabilite_sinistre` rouge, Baloise non promu.
Le harnais est celui de L1n, généralisé (`tests.rejeu_gate`). Aucun appel réseau.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.steps.verifier import _mots_renvoi, _texte_de_la_clause
from tests.rejeu_gate import rapport, rejouer, verdicts, verdicts_observes

ROOT = Path(__file__).resolve().parents[1]
RAPPORT = rapport("gate-baloise-16-congelateur.json")
DOC = "baloise-lu-home-2-2024"
AMORCE = f"{DOC}:p21:4"  # « Nous garantissons […] dans la limite prévue dans vos conditions particulières […] résultant : »
ITEM = f"{DOC}:p21:5"  # « d'un arrêt accidentel du congélateur […] ; d'un arrêt accidentel du courant électrique […] »
EXCLUSION = f"{DOC}:p21:9"  # l'exclusion voisine, dans le nœud frère
DU_RUN = frozenset({"L1p"})  # le run du 04/09 portait déjà L1o, pas encore L1p


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(ROOT / "data", allow_ungated=True)


def test_la_reconstruction_reproduit_la_signature_mesuree_du_gate(corpus: Corpus) -> None:
    """La mesure d'abord : le rejeu doit rendre la signature du rapport, à la lecture du jour du run.

    Le rapport `-16` a tourné sur `04d62b9`, qui porte L1o : la répétition 3 y montre déjà une
    affirmation réduite à la seule amorce et laissée hors de la table (`applicable: null`). Seul L1p
    manque, et c'est lui que la reconstruction retire pour rejouer le run.
    """
    rejoues = verdicts(RAPPORT, corpus=corpus, index=Index(corpus), avant=DU_RUN)
    assert rejoues == verdicts_observes(RAPPORT)
    assert rejoues == ["sous_conditions", "sous_conditions", "ne_tranche_pas"]


def test_la_claim_et_la_regle_qui_faisaient_basculer_le_verdict(corpus: Corpus) -> None:
    """Ce que la mesure nomme : la claim de la garantie « denrées en congélateur », et T18.

    Le contrat écrit son renvoi une seule fois, en tête de l'énumération : « Nous garantissons, au
    lieu d'assurance, **dans la limite prévue dans vos conditions particulières**, les détériorations
    des denrées […] résultant : ». L'item qui suit — « d'un arrêt accidentel du congélateur […] » —
    ne le réécrit pas.

    Aux répétitions 1 et 2, le modèle a cité l'amorce **avec** son item dans une même affirmation :
    `_mots_renvoi` lisait « conditions particulières » sur le texte de l'amorce, T18 forçait
    `cp_requise`, et la règle (2bis) d'AD-6 rendait `sous_conditions`. À la répétition 3, il a rangé
    l'amorce dans une affirmation séparée (que L1o laisse hors de la table) et cité l'item seul : le
    renvoi disparaissait, plus aucune garantie n'était conditionnée, et la table tombait en (4),
    `ne_tranche_pas`. Le **découpage des citations** décidait donc à la place du texte du contrat.
    """
    index = Index(corpus)
    document = corpus.documents[DOC]
    assert "conditions particulieres" in _mots_renvoi(document.block(AMORCE).text)
    assert _mots_renvoi(document.block(ITEM).text) == set()

    cp_par_repetition = []
    for repetition in RAPPORT["repetitions"]:
        _verdict, jugees = rejouer(repetition, corpus=corpus, index=index, avant=DU_RUN)
        portantes = [j for j in jugees if any(c.block_id == ITEM for c in j.clauses)]
        assert len(portantes) == 1
        assert portantes[0].champs is not None
        cp_par_repetition.append(portantes[0].champs.cp_requise)
    assert cp_par_repetition == [True, True, False]  # le même contrat, deux lectures


def test_l1p_fait_converger_les_trois_repetitions_du_gate(corpus: Corpus) -> None:
    """Le témoin de L1p : le même cas, le même corpus, **trois verdicts identiques**.

    Un item d'énumération n'est pas une phrase complète : l'amorce porte le verbe et tout ce que
    l'énumération subordonne. `Index.amorce_qui_introduit` le dit sur la structure, et
    `_texte_de_la_clause` fait relire l'item avec elle — que le modèle l'ait citée ou non. Le renvoi
    aux conditions particulières est alors lu aux trois répétitions, et les trois rendent
    `sous_conditions` : le verdict que le contrat écrit, et non celui que le découpage du jour donnait.

    C'est l'inverse exact de L1o, et les deux tiennent l'énumération par les deux bouts.
    """
    index = Index(corpus)
    assert index.amorce_qui_introduit(ITEM) == AMORCE
    avant = verdicts(RAPPORT, corpus=corpus, index=index, avant=DU_RUN)
    assert len(set(avant)) == 2  # la mesure : le même cas rendait deux verdicts
    assert verdicts(RAPPORT, corpus=corpus, index=index) == ["sous_conditions"] * 3


def test_l1p_ne_deborde_pas_sur_ce_qui_n_est_pas_un_item(corpus: Corpus) -> None:
    """Contre-épreuve : la règle ne s'applique qu'aux items, et une amorce n'en est pas un.

    Trois bornes, mesurées sur les deux contrats servis. L'exclusion voisine `p21:9` vit dans un
    nœud frère : elle ne reçoit pas le renvoi de la garantie, faute de quoi toute exclusion du
    document serait devenue conditionnée aux conditions particulières. L'amorce elle-même n'a pas
    d'amorce — la lecture est d'un seul niveau. Et un bloc qui **suit** la liste de son nœud n'en est
    pas un item : `axa:p78:1`, la condition qui clôt `a4.1.4`, ne reçoit rien de `p77:6` — « Les
    présentes conditions spéciales sont applicables si les conditions particulières mentionnent… » —,
    parce que le corps de ce nœud n'est pas une énumération.

    Le rayon de la règle sur les deux contrats servis : 157 des 1247 blocs décisionnels sont des
    items, et 7 d'entre eux seulement héritent d'un renvoi que leur propre texte n'écrit pas.
    """
    index = Index(corpus)
    assert index.amorce_qui_introduit(AMORCE) is None
    assert index.amorce_qui_introduit(EXCLUSION) is None
    assert _mots_renvoi(_texte_de_la_clause(
        EXCLUSION, document=corpus.documents[DOC], index=index)) == set()

    axa = corpus.documents["axa-lu-optihome-2017"]
    epilogue = "axa-lu-optihome-2017:p78:1"
    assert "conditions particulieres" in _mots_renvoi(axa.block("axa-lu-optihome-2017:p77:6").text)
    assert index.amorce_qui_introduit(epilogue) is None
    assert _mots_renvoi(_texte_de_la_clause(epilogue, document=axa, index=index)) == set()
