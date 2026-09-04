"""Rejeu **hors ligne** des trois cas divergents du gate `-18` (story 5.7, L1u).

Ce que la mesure du 04/09/2026 a rendu sur trois répétitions du même cas et du même corpus :

- `b-invite-cigarette` (Baloise) — `ne_tranche_pas`, **`non_couvert`**, `ne_tranche_pas`. La seule
  répétition qui cite l'exclusion des biens confiés (`p31:4`) prononce « exclu », par la règle
  (3bis) de L1r : une exclusion dont le rattachement nomme un fait déclaré valait `oui`, quoi que
  disent ses champs typés. Un « exclu » hors des valeurs admissibles pour ce cas ;
- `s-bougie-canape` (AXA) — `ne_tranche_pas`, `ne_tranche_pas`, **`sous_conditions`**. Deux
  répétitions citent `p34:7` (l'incendie, « des flammes se **propageant** … en dehors de leur
  domaine normal ») : la deuxième la juge `non`, la troisième la rend `oui` sur un rattachement qui
  dit « l'absence d'embrasement ni de flammes propagées » — l'inverse de ce que la clause exige,
  tenu pour vrai parce que le code n'y recoupe que des mots (porte de L1b/L1c, élargie par L1q) ;
- `b-bougie-canape` (Baloise) — même forme, sur les mêmes deux règles.

Les deux règles sont retirées. Le témoin est double : la mesure est figée dans
`tests/data/gate-18-l1u.json` (le rouge, tel que le gate l'a écrit), et la table courante rejouée
sur ces mêmes dossiers rend **trois verdicts identiques** par cas (le vert). Aucun appel réseau : le
corpus est celui de `data/`, le rapport est figé.

La règle retirée ne peut évidemment plus être rejouée par le code. Pour le cas AXA, le rapport porte
le marqueur qui suffit à la rejouer côté entrée (`ferme_par_le_rattachement`, la lecture de la
mesure) et le premier témoin reproduit alors la signature mesurée. Pour les deux cas Baloise, la
divergence tenait à la règle (3bis) — une décision de la table, pas une entrée : ce qui est vérifié
là est la claim qui la portait, nommée et relue sur le corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.domain.verdict import applicabilites_des_claims, decider
from tests.rejeu_gate import rapport, rejouer, verdicts, verdicts_observes

ROOT = Path(__file__).resolve().parents[1]
RAPPORT = rapport("gate-18-l1u.json")
CAS = {cas["nom"]: cas for cas in RAPPORT["cas"]}
DU_RUN = frozenset({"L1u"})  # les deux règles que ce tour retire, en vigueur le jour du run
INCENDIE = "axa-lu-optihome-2017:p34:7"
BIENS_CONFIES = "baloise-lu-home-2-2024:p31:4"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(ROOT / "data", allow_ungated=True)


@pytest.mark.parametrize("nom", sorted(CAS))
def test_la_mesure_diverge_et_le_gate_le_disait(nom: str) -> None:
    """Le rouge, tel que le gate `-18` l'a écrit : le même cas, deux verdicts au moins."""
    observes = verdicts_observes(CAS[nom])
    assert len(observes) == 3
    assert len(set(observes)) > 1, observes


def test_la_reconstruction_reproduit_la_signature_mesuree_du_cas_bougie(corpus: Corpus) -> None:
    """Sans reproduire le run, le témoin d'après ne dirait rien de lui.

    Le cas AXA se rejoue entièrement côté entrée : la seule répétition qui diverge est celle qui
    cite `p34:7`, et le rapport dit que son `oui` était fermé par le rattachement. Rejouée avec la
    porte (`avant={"L1u"}`), elle rend `sous_conditions` là où les deux autres rendent
    `ne_tranche_pas` — la signature mesurée, mot pour mot.
    """
    cas = CAS["s-bougie-canape"]
    rejoues = verdicts(cas, corpus=corpus, index=Index(corpus), avant=DU_RUN)
    assert rejoues == verdicts_observes(cas)
    assert rejoues == ["ne_tranche_pas", "ne_tranche_pas", "sous_conditions"]


@pytest.mark.parametrize("nom", sorted(CAS))
def test_les_trois_repetitions_convergent_sur_la_table_de_l1u(nom: str, corpus: Corpus) -> None:
    """Le vert : le même cas, le même corpus, **trois verdicts identiques**.

    Rien n'y dépend plus de ce que le tirage a cité : ni l'exclusion des biens confiés que seule la
    deuxième répétition de la cigarette produit, ni `p34:7` que seule la troisième répétition de la
    bougie cite.
    """
    rejoues = verdicts(CAS[nom], corpus=corpus, index=Index(corpus))
    assert rejoues == ["ne_tranche_pas"] * 3, rejoues


@pytest.mark.parametrize("nom", sorted(CAS))
def test_le_verdict_ne_depend_plus_du_rattachement(nom: str, corpus: Corpus) -> None:
    """La propriété générique de L1u, sur les dossiers réels : le rattachement ne décide de rien.

    Chaque répétition est rejouée deux fois — `fait_rattache` posé sur chaque affirmation dont le
    run a publié un rattachement, puis retiré partout. C'est la seule chose que les deux règles
    retirées lisaient ; les verdicts, ce qui reste à demander et les faits manquants sont identiques
    des deux côtés.
    """
    index = Index(corpus)
    for repetition in CAS[nom]["repetitions"]:
        _verdict, jugees = rejouer(repetition, corpus=corpus, index=index)
        publies = {claim["claim_id"]: claim["rattachement"] for claim in repetition["claims"]}
        assert any(publies.values()), repetition["repetition"]
        avec = decider([j.model_copy(update={"fait_rattache": publies.get(j.claim_id, False)})
                        for j in jugees], ask_client_max=6)
        sans = decider([j.model_copy(update={"fait_rattache": False}) for j in jugees],
                       ask_client_max=6)
        assert avec.value == sans.value
        assert avec.ask_client == sans.ask_client
        assert avec.missing.faits == sans.missing.faits


def test_lexclusion_qui_prononcait_exclu_ne_conclut_plus(corpus: Corpus) -> None:
    """La claim qui portait le « exclu » de la cigarette, nommée et relue sur le corpus.

    `p31:4` — les biens confiés — est bien une exclusion, la deuxième répétition la rend `oui`, et
    c'est la seule des trois à la citer. Sans la règle (3bis), elle retombe sur sa portée déclarée,
    qui ne couvre pas les nœuds du cas : elle vaut `non`, et aucune répétition ne prononce « exclu ».
    """
    index = Index(corpus)
    cas = CAS["b-invite-cigarette"]
    citee = [repetition["repetition"] for repetition in cas["repetitions"]
             if any(BIENS_CONFIES in claim["blocs"] for claim in repetition["claims"])]
    assert citee == [2]
    for repetition in cas["repetitions"]:
        verdict, jugees = rejouer(repetition, corpus=corpus, index=index)
        assert verdict != "non_couvert"
        etat = {claim_id: value
                for claim_id, (value, _r) in applicabilites_des_claims(jugees).items()}
        for jugee in jugees:
            if any(clause.block_id == BIENS_CONFIES for clause in jugee.clauses):
                assert jugee.kind == "exclusion"
                assert etat[jugee.claim_id] == "non"


def test_la_repetition_qui_cite_lincendie_rend_le_meme_verdict_que_les_autres(
        corpus: Corpus) -> None:
    """La contre-épreuve du cas bougie : `p34:7` citée ou non, le verdict ne bouge pas.

    Deux répétitions la citent et une seule en tirait un `oui` — c'était toute la mesure : cette
    lecture-là faisait passer le cas de `ne_tranche_pas` à `sous_conditions`. L'exigence que la
    clause écrit — la propagation des flammes hors de leur domaine normal — reste due, la claim vaut
    `humain` comme les autres garanties du cas, et le verdict est le même si on la retire du dossier.
    """
    index = Index(corpus)
    cas = CAS["s-bougie-canape"]
    citee = [repetition["repetition"] for repetition in cas["repetitions"]
             if any(INCENDIE in claim["blocs"] for claim in repetition["claims"])]
    assert citee == [2, 3]
    troisieme = cas["repetitions"][2]
    verdict, jugees = rejouer(troisieme, corpus=corpus, index=index)
    assert verdict == "ne_tranche_pas"
    etat = {claim_id: value for claim_id, (value, _r) in applicabilites_des_claims(jugees).items()}
    portantes = [jugee for jugee in jugees
                 if any(clause.block_id == INCENDIE for clause in jugee.clauses)]
    assert len(portantes) == 1
    assert etat[portantes[0].claim_id] == "humain"
    # Et sans elle : le même verdict, ce qui est exactement ce que « le verdict ne dépend pas des
    # citations du tirage » veut dire.
    sans = decider([j for j in jugees if j.claim_id != portantes[0].claim_id], ask_client_max=6)
    assert sans.value == verdict
