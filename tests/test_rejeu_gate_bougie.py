"""Rejeu **hors ligne** des trois répétitions du gate AXA `-14` sur le cas `s-bougie-canape`.

Story 5.7 (L1n). Le gate a rendu `ne_tranche_pas`, `ne_tranche_pas` puis `sous_conditions` sur le
même cas et le même corpus : signatures divergentes, `stabilite_sinistre` rouge, AXA non promu. Le
harnais de rejeu vit désormais dans `tests.rejeu_gate` — il sert aussi le gate Baloise `-16` (L1p) —
et ce fichier ne porte plus que ce que le rapport AXA prouve. Aucun appel réseau.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.domain.verdict import applicable_de_claim, decider
from server.app.steps.verifier import _mots_qualifiants
from tests.rejeu_gate import rapport, rejouer, verdicts, verdicts_observes

ROOT = Path(__file__).resolve().parents[1]
RAPPORT = rapport("gate-axa-14-bougie.json")
AMORCE = "axa-lu-optihome-2017:p34:6"  # « La Compagnie assure les biens désignés, contre les périls suivants : »
ITEM = "axa-lu-optihome-2017:p34:12"  # l'item qui écrit « soudain » et « subite »
DU_RUN = frozenset({"L1o", "L1p"})  # les correctifs postérieurs au run du 04/09


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(ROOT / "data", allow_ungated=True)


def test_la_reconstruction_reproduit_la_signature_mesuree_du_gate(corpus: Corpus) -> None:
    """Sans L1n il n'y aurait rien à mesurer : la reconstruction doit d'abord dire le run.

    Les qualités dérivées du texte ne changent aucun des trois verdicts observés — elles ne
    referment que des `oui` portés par une clause qui écrit un qualificatif, et aucune des trois
    répétitions n'en a. Le rejeu rend donc exactement la signature du rapport, à la lecture du jour
    du run : c'est la mesure, et c'est elle que le correctif de L1o fait bouger.
    """
    rejoues = verdicts(RAPPORT, corpus=corpus, index=Index(corpus), avant=DU_RUN)
    assert rejoues == verdicts_observes(RAPPORT)
    assert rejoues == ["ne_tranche_pas", "ne_tranche_pas", "sous_conditions"]


def test_l1n_rend_identiques_les_exigences_de_la_clause_qui_porte_le_cas(corpus: Corpus) -> None:
    """Ce que L1n stabilise : l'item `p34:12` exige les mêmes qualités aux trois répétitions.

    C'est la garantie du correctif, et elle est vérifiable sans le JSON du vérificateur : les
    qualités sont lues dans le texte du bloc, qui ne varie pas d'un appel à l'autre. Quelle que soit
    l'énumération rendue par le modèle — vide, partielle, ou rangée dans les seules établies —,
    « soudain » et « subite » sont exigées, aucune n'est établie, et la claim vaut `humain` partout.
    """
    index = Index(corpus)
    exigences: list[list[str]] = []
    for repetition in RAPPORT["repetitions"]:
        _verdict, jugees = rejouer(repetition, corpus=corpus, index=index)
        portant = [j for j in jugees if any(c.block_id == ITEM for c in j.clauses)]
        assert len(portant) == 1
        assert portant[0].champs is not None
        exigences.append(sorted(portant[0].champs.qualites_exigees))
        assert next(c["applicable"] for c in repetition["claims"]
                    if c["claim_id"] == portant[0].claim_id) == "humain"
    assert len(set(map(tuple, exigences))) == 1
    assert set(_mots_qualifiants(" ".join(exigences[0]))) >= {"soudain", "subit"}


def test_l1o_fait_converger_les_trois_repetitions_du_gate(corpus: Corpus) -> None:
    """Le témoin de L1o : le même cas, le même corpus, **trois verdicts identiques**.

    La divergence du gate `-14` ne tenait pas à l'item `p34:12`, `humain` aux trois répétitions, mais
    à une affirmation réduite à la **seule amorce** de l'énumération, `p34:6` — « La Compagnie assure
    les biens désignés, contre les périls suivants : ». Cette phrase n'énonce aucun péril et n'écrit
    aucun qualificatif : le texte ne donnait au code rien à exiger (L1n ne pouvait donc pas fermer ce
    chemin), et le modèle l'a jugée `non` (rép. 1), non soutenue (rép. 2), puis `oui` (rép. 3) — ce
    `oui` seul faisant basculer la table en `sous_conditions` par la condition de section de L1e.

    L1o la sort de la table sur la **structure** : citée seule, elle ne laisse aucune clause qui
    décide, la claim vaut `applicable=None`, et le verdict ne dépend plus de ce que le modèle en
    aura pensé ce jour-là. Les deux lectures sont rejouées côte à côte : c'est la même entrée qui
    divergeait et qui converge.
    """
    index = Index(corpus)
    avant = verdicts(RAPPORT, corpus=corpus, index=index, avant=DU_RUN)
    apres = []
    for repetition in RAPPORT["repetitions"]:
        verdict, jugees = rejouer(repetition, corpus=corpus, index=index)
        apres.append(verdict)
        for jugee in jugees:
            if [c.block_id for c in jugee.clauses] == [AMORCE]:
                assert jugee.clauses[0].amorce is True
                assert jugee.clauses_decisionnelles == []
                assert applicable_de_claim(jugee) is None
                assert jugee.kind is None
    assert len(set(avant)) == 2  # la mesure : le même cas rendait deux verdicts
    assert apres == ["ne_tranche_pas"] * 3
    seules_amorces = {r["repetition"]: [c["applicable"] for c in r["claims"] if c["blocs"] == [AMORCE]]
                      for r in RAPPORT["repetitions"]}
    assert seules_amorces == {1: ["non"], 2: [], 3: ["oui"]}


def test_l1p_ne_deplace_rien_sur_le_cas_bougie(corpus: Corpus) -> None:
    """Contre-épreuve de L1p sur le rapport voisin : la règle ne s'applique pas ici, rien ne bouge.

    Les items de l'énumération incendie ont bien une amorce (`p34:6`), et le code la lit désormais
    avec eux — mais « La Compagnie assure les biens désignés, contre les périls suivants : » n'écrit
    ni qualificatif, ni renvoi aux conditions particulières, ni qualité de personne. Une amorce qui
    ne subordonne rien n'ajoute rien : les trois répétitions restent `ne_tranche_pas`, exactement
    comme sous L1o seul. C'est la borne de L1p — il n'ajoute jamais qu'un mot que le contrat écrit.
    """
    index = Index(corpus)
    assert index.amorce_qui_introduit(ITEM) == AMORCE
    assert verdicts(RAPPORT, corpus=corpus, index=index) == ["ne_tranche_pas"] * 3
    assert verdicts(RAPPORT, corpus=corpus, index=index,
                    avant=frozenset({"L1p"})) == ["ne_tranche_pas"] * 3


def test_l1t_ne_retire_aucune_qualite_au_cas_bougie(corpus: Corpus) -> None:
    """Contre-épreuve de L1t : un rattachement ne dispense pas une garantie de ses qualités.

    L1t tait ce qu'un rattachement **retenu** a établi, et son verrou est `applicable = "oui"`. Le
    cas bougie est exactement la borne : `p34:12` exige « soudain » et « subite », les faits ne les
    disent pas, la claim vaut `humain` — et rien de ce que le rattachement affirmerait par ailleurs
    ne referme cela. Les trois répétitions sont donc rejouées avec `fait_rattache` posé partout : les
    verdicts ne bougent pas, et les deux qualités restent des questions au client.
    """
    index = Index(corpus)
    for repetition in RAPPORT["repetitions"]:
        _verdict, jugees = rejouer(repetition, corpus=corpus, index=index)
        rattachees = [j.model_copy(update={"fait_rattache": True}) for j in jugees]
        verdict = decider(rattachees, ask_client_max=8)
        assert verdict.value == "ne_tranche_pas"
        assert any(ITEM in claim["blocs"] for claim in repetition["claims"])
        demandes = " ".join(verdict.missing.faits)
        assert set(_mots_qualifiants(demandes)) >= {"soudain", "subit"}, verdict.missing.faits
