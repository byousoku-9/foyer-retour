"""Rejeu **hors ligne** des trois répétitions du gate AXA `-19`, cas `s-bougie-canape` (L1v).

Ce que la mesure du 04/09/2026 a rendu sur trois répétitions du même cas, du même corpus et de la
même déclaration : `ne_tranche_pas`, **`sous_conditions`**, **`sous_conditions`**. Les quatre mêmes
clauses sont citées les trois fois (`p22:5`, `p34:12`, `p34:6`, `p34:7`) ; toute la divergence tient
dans un champ du vérificateur sur `p34:7` — « L'incendie, c'est-à-dire la destruction par des
flammes se propageant … en dehors de leur domaine normal » —, `fait_requis_present` rendu vrai deux
fois et faux une fois.

La déclaration, elle, ne varie pas : « … a brûlé sur une partie, **sans embrasement ni commencement
d'incendie : il n'y a eu ni flammes propagées**, ni dégât au bâtiment ». Les mots que la clause
exige y sont tous, tous niés. Le chemin de corroboration comptait la présence du mot ; le modèle,
lui, hésitait — et c'est ce que le verdict lisait.

Le témoin est double : la mesure est figée dans `tests/data/gate-19-l1v.json` (le rouge, tel que le
gate l'a écrit), et la table courante rejouée sur ces mêmes dossiers rend **trois verdicts
identiques** (le vert). Aucun appel réseau : le corpus est celui de `data/`, le rapport est figé.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.domain.verdict import applicabilites_des_claims
from tests.rejeu_gate import rapport, rejouer, verdicts, verdicts_observes

ROOT = Path(__file__).resolve().parents[1]
RAPPORT = rapport("gate-19-l1v.json")
FAITS = RAPPORT["faits"]
DU_RUN = frozenset({"L1v"})  # la relecture que ce tour ajoute, absente le jour du run
INCENDIE = "axa-lu-optihome-2017:p34:7"
CHALEUR = "axa-lu-optihome-2017:p34:12"

# Les deux déclarations des témoins (b) et (c) : la même clause, la même mécanique, et rien de nié
# qui la concerne. Elles remplacent la déclaration du cas dans le rejeu — c'est le seul texte que
# L1v lit, et donc la seule chose à faire varier pour montrer ce que la règle exige.
FAITS_SANS_NEGATION = ("Une bougie allumée est tombée sur le canapé. "
                       "Le feu s'est propagé dans l'appartement.")
FAITS_NEGATION_AILLEURS = ("Une bougie allumée est tombée sur le canapé. "
                           "Pas de dégât au bâtiment, mais des flammes propagées dans le salon.")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(ROOT / "data", allow_ungated=True)


def test_la_mesure_diverge_et_le_gate_le_disait() -> None:
    """Le rouge, tel que le gate `-19` l'a écrit : le même cas, deux verdicts."""
    observes = verdicts_observes(RAPPORT)
    assert observes == ["ne_tranche_pas", "sous_conditions", "sous_conditions"]


def test_le_tirage_des_citations_nexplique_pas_la_divergence() -> None:
    """Ce qui rend cette mesure différente des précédentes : le tirage n'y est pour rien.

    L1o, L1p, L1q et L1u nommaient chacune un découpage de citations — une amorce citée seule, un
    item cité sans son amorce, une exclusion citée dans une seule répétition. Ici les deux clauses
    qui décident, `p34:7` et `p34:12`, sont citées les trois fois, et la seule qui varie (`p22:5`,
    l'exclusion de la faute intentionnelle) est présente dans **une** répétition qui rend
    `ne_tranche_pas` et dans **une** qui rend `sous_conditions` : elle n'explique rien. Ce qui varie
    est un champ typé, sur la même clause.
    """
    cites = [{bloc for claim in repetition["claims"] for bloc in claim["blocs"]}
             for repetition in RAPPORT["repetitions"]]
    assert all(INCENDIE in blocs and CHALEUR in blocs for blocs in cites)
    exclusion = "axa-lu-optihome-2017:p22:5"
    observes = verdicts_observes(RAPPORT)
    citee = {observes[i] for i, blocs in enumerate(cites) if exclusion in blocs}
    assert citee == {"ne_tranche_pas", "sous_conditions"}


def test_la_reconstruction_reproduit_la_signature_mesuree(corpus: Corpus) -> None:
    """Sans reproduire le run, le témoin d'après ne dirait rien de lui.

    Rejouée sans la relecture de la déclaration (`avant={"L1v"}`), la reconstruction rend la
    signature mesurée mot pour mot : les deux répétitions qui déclarent le fait requis présent sur
    `p34:7` passent `sous_conditions`, la troisième reste `ne_tranche_pas`.
    """
    rejoues = verdicts(RAPPORT, corpus=corpus, index=Index(corpus), avant=DU_RUN)
    assert rejoues == verdicts_observes(RAPPORT)


def test_les_trois_repetitions_convergent_sur_la_table_de_l1v(corpus: Corpus) -> None:
    """Le vert : le même cas, le même corpus, la même déclaration, **trois verdicts identiques**."""
    rejoues = verdicts(RAPPORT, corpus=corpus, index=Index(corpus))
    assert rejoues == ["ne_tranche_pas"] * 3, rejoues


def test_lincendie_est_contredit_et_la_garantie_voisine_ne_lest_pas(corpus: Corpus) -> None:
    """Ce que la relecture change, claim par claim — et ce qu'elle ne touche pas.

    `p34:7` **définit** l'incendie, et la déclaration ne nomme sa définition que pour la nier : elle
    vaut `non` par le fait connu et contraire, dans les trois répétitions. `p34:12` — « les dégâts
    occasionnés au mobilier assuré … par un événement soudain » — écrit elle aussi « embrasement »
    et « incendie », mais **sous une négation** (« même lorsqu'il n'y a pas eu … ») : ces mots-là ne
    sont pas ce qu'elle exige, et le mobilier qu'elle exige, lui, a bien brûlé. Elle reste `humain`,
    et « soudain » comme « subite » restent dus au client.
    """
    index = Index(corpus)
    for repetition in RAPPORT["repetitions"]:
        _verdict, jugees = rejouer(repetition, corpus=corpus, index=index, faits=FAITS)
        etat = {claim_id: value for claim_id, (value, _r) in applicabilites_des_claims(jugees).items()}
        for jugee in jugees:
            blocs = {clause.block_id for clause in jugee.clauses}
            if INCENDIE in blocs:
                assert etat[jugee.claim_id] == "non", repetition["repetition"]
            if CHALEUR in blocs:
                assert etat[jugee.claim_id] == "humain", repetition["repetition"]


@pytest.mark.parametrize("nom, declaration", [
    ("sans négation", FAITS_SANS_NEGATION),
    ("négation portée ailleurs", FAITS_NEGATION_AILLEURS)])
def test_une_declaration_qui_corrobore_laisse_le_verdict_du_run(
        nom: str, declaration: str, corpus: Corpus) -> None:
    """La borne de L1v : ce n'est pas la clause qui décide, c'est la **déclaration**.

    Deux déclarations, la même clause, les mêmes champs typés. « Le feu s'est propagé dans
    l'appartement » corrobore la propagation ; « Pas de dégât au bâtiment, mais des flammes
    propagées dans le salon » aussi — la négation y porte sur un autre fragment, sept mots plus
    loin que la fenêtre de `negation_fenetre_mots`. Dans les deux cas la relecture ne s'exerce pas
    et le verdict est celui que le run a rendu.
    """
    index = Index(corpus)
    rejoues = [rejouer(r, corpus=corpus, index=index, faits=declaration)[0]
               for r in RAPPORT["repetitions"]]
    assert rejoues == verdicts_observes(RAPPORT), nom


# Les déclarations des trois autres cas rejoués hors ligne par la suite. Aucune ne porte de
# négation : la relecture de L1v n'a rien à y lire, et leurs témoins sont donc inchangés — c'est ce
# que ce dernier test tient, sur les mêmes rapports figés que les leurs.
DECLARATIONS_VOISINES = {
    "gate-18-l1u.json": "Le canapé meublant appartient au propriétaire du logement. Un invité l'a "
                        "brûlé avec sa cigarette pendant une visite.",
    "gate-baloise-16-congelateur.json": "Une coupure de courant a arrêté le congélateur ; "
                                        "l'appareil fonctionne de nouveau mais les aliments "
                                        "congelés sont perdus.",
}


@pytest.mark.parametrize("fichier", sorted(DECLARATIONS_VOISINES))
def test_les_cas_voisins_ne_bougent_pas(fichier: str, corpus: Corpus) -> None:
    """Cigarette et congélateur : les mêmes rapports, leur vraie déclaration, les mêmes verdicts.

    Leurs témoins tournent sans déclaration (leurs rapports n'en portent pas) ; les leur donner est
    la contre-épreuve qui manquerait sinon — une relecture qui ne lit que des négations ne doit rien
    changer là où il n'y en a aucune.
    """
    index = Index(corpus)
    voisin = rapport(fichier)
    cas = voisin["cas"] if isinstance(voisin["cas"], list) else [voisin]
    repetitions = [r for c in cas for r in c["repetitions"]]
    for repetition in repetitions:
        sans = rejouer(repetition, corpus=corpus, index=index)[0]
        avec = rejouer(repetition, corpus=corpus, index=index,
                       faits=DECLARATIONS_VOISINES[fichier])[0]
        assert avec == sans, (fichier, repetition["repetition"])
