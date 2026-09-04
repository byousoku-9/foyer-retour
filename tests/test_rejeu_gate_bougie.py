"""Rejeu **hors ligne** des trois répétitions du gate AXA `-14` sur le cas `s-bougie-canape`.

Story 5.7 (L1n). Le gate a rendu `ne_tranche_pas`, `ne_tranche_pas` puis `sous_conditions` sur le
même cas et le même corpus : signatures divergentes, `stabilite_sinistre` rouge, AXA non promu. Le
rapport porte les claims et leurs citations ; il ne porte pas le JSON brut du vérificateur. Le rejeu
reconstruit donc, pour chaque affirmation, le **jeu de champs canonique** qui produit l'`applicable`
observé, puis rejoue la table AD-6 sur les clauses relues dans le corpus réel. Le premier témoin
vérifie que cette reconstruction reproduit la signature mesurée — sans quoi tout ce qui suit ne
dirait rien du run.

Aucun appel réseau : le corpus est celui de `data/`, le rapport est figé dans `tests/data/`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.domain.verdict import ChampsApplicabilite, ClaimJugee, decider
from server.app.steps.verifier import _mots_qualifiants, _qualites_de_la_clause, _clauses_citees

ROOT = Path(__file__).resolve().parents[1]
RAPPORT = json.loads((Path(__file__).parent / "data" / "gate-axa-14-bougie.json").read_text())
AMORCE = "axa-lu-optihome-2017:p34:6"  # « La Compagnie assure les biens désignés, contre les périls suivants : »
ITEM = "axa-lu-optihome-2017:p34:12"  # l'item qui écrit « soudain » et « subite »


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(ROOT / "data", allow_ungated=True)


def _rejouer(repetition: dict, *, corpus: Corpus, index: Index) -> tuple[str, list[ClaimJugee]]:
    """La répétition rejouée : champs canoniques + qualités **dérivées du texte** (L1n), puis AD-6.

    Un `applicable` observé se réécrit sans ambiguïté en champs typés : `oui` = fait requis présent
    et rien d'ouvert, `non` = fait requis absent **sans** fait manquant (la signature du fait connu
    et contraire), `humain` = un fait manquant nommé. Par-dessus, on applique ce que L1n rend
    indépendant du modèle : les qualités que le texte des clauses citées écrit et que rien
    n'établit — c'est la seule chose que le correctif ajoute, et elle ne peut que fermer un `oui`.
    """
    jugees: list[ClaimJugee] = []
    for claim in repetition["claims"]:
        clauses = _clauses_citees(claim["blocs"], corpus=corpus, index=index)
        observe = claim["applicable"]
        exigees = _qualites_de_la_clause(clauses, nommees="", place=8) if observe != "non" else []
        jugees.append(ClaimJugee(
            claim_id=claim["claim_id"], clauses=clauses, retenue=True,
            champs=ChampsApplicabilite(
                fait_requis_present=observe == "oui", option_requise=False, cp_requise=False,
                fait_manquant=None if observe != "humain" else "fait exigé par la clause citée",
                qualites_exigees=exigees, qualites_non_etablies=exigees)))
    return decider(jugees, ask_client_max=6).value, jugees


def test_la_reconstruction_reproduit_la_signature_mesuree_du_gate(corpus: Corpus) -> None:
    """Sans L1n il n'y aurait rien à mesurer : la reconstruction doit d'abord dire le run.

    Les qualités dérivées du texte ne changent aucun des trois verdicts observés — elles ne
    referment que des `oui` portés par une clause qui écrit un qualificatif, et aucune des trois
    répétitions n'en a. Le rejeu rend donc exactement la signature du rapport.
    """
    index = Index(corpus)
    rejoues = [_rejouer(r, corpus=corpus, index=index)[0] for r in RAPPORT["repetitions"]]
    assert rejoues == [r["verdict_observe"] for r in RAPPORT["repetitions"]]
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
        _verdict, jugees = _rejouer(repetition, corpus=corpus, index=index)
        portant = [j for j in jugees if any(c.block_id == ITEM for c in j.clauses)]
        assert len(portant) == 1
        assert portant[0].champs is not None
        exigences.append(sorted(portant[0].champs.qualites_exigees))
        assert next(c["applicable"] for c in repetition["claims"]
                    if c["claim_id"] == portant[0].claim_id) == "humain"
    assert len(set(map(tuple, exigences))) == 1
    assert set(_mots_qualifiants(" ".join(exigences[0]))) >= {"soudain", "subit"}


def test_ce_qui_fait_encore_diverger_le_cas_est_une_claim_reduite_a_l_amorce(corpus: Corpus) -> None:
    """Le reste à faire, épinglé plutôt que perdu — et il n'est pas une affaire de qualités.

    La divergence du gate `-14` ne tient pas à l'item `p34:12`, `humain` aux trois répétitions, mais
    à une affirmation réduite à la **seule amorce** de l'énumération, `p34:6` — « La Compagnie assure
    les biens désignés, contre les périls suivants : ». Cette phrase n'énonce aucun péril et n'écrit
    aucun qualificatif : le texte ne donne au code rien à exiger, et le modèle l'a jugée `non`
    (rép. 1), non soutenue (rép. 2), puis `oui` (rép. 3) — et c'est ce `oui` seul qui fait basculer
    la table en `sous_conditions` par la condition de section de L1e.

    L1n ne peut donc pas fermer ce chemin : il lit ce que la clause écrit, et celle-ci n'écrit rien.
    Ce qu'il faudra, c'est la règle jumelle sur la **structure** — une amorce citée seule est le
    contexte d'une clause, pas une clause (`Index.amorce_de_lenumeration`, déjà employée par
    `corpus.ebauche`). Ce témoin échouera le jour où elle sera posée : c'est le signal attendu.
    """
    index = Index(corpus)
    verdicts = []
    for repetition in RAPPORT["repetitions"]:
        verdict, jugees = _rejouer(repetition, corpus=corpus, index=index)
        verdicts.append(verdict)
        for jugee in jugees:
            if [c.block_id for c in jugee.clauses] == [AMORCE]:
                assert jugee.clauses[0].qualificatifs == []
    assert len(set(verdicts)) == 2, (
        "les trois répétitions ne convergent pas encore : voir la règle de structure ci-dessus")
    seules_amorces = {r["repetition"]: [c["applicable"] for c in r["claims"] if c["blocs"] == [AMORCE]]
                      for r in RAPPORT["repetitions"]}
    assert seules_amorces == {1: ["non"], 2: [], 3: ["oui"]}
