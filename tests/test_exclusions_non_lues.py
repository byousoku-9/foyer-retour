"""Story 5.7 (L1q) — une garantie lue oblige à lire ce que le contrat en retire.

Mesuré en prod le 04/09/2026 (plaques à induction, appartement en feu) : la navigation a ouvert
l'étendue de la garantie incendie, aucune exclusion, et la réponse servie l'a dit elle-même — « ma
lecture n'a pas porté sur les exclusions générales ». La règle est celle de la couverture des
sous-questions (L1j) appliquée à l'autre moitié d'une garantie, et elle se lit **sur l'arbre**.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus

AXA = "axa-lu-optihome-2017"
BAL = "baloise-lu-home-2-2024"


@pytest.fixture(scope="module")
def index() -> Index:
    return Index(load_corpus(Path("data"), allow_ungated=True))


def test_les_exclusions_generales_sont_celles_que_larbre_place_le_plus_haut(index: Index) -> None:
    """Une par contrat, nommée par son titre — jamais une section de garantie, jamais un corps."""
    assert index.exclusions_generales(AXA) == [f"{AXA}:a2.10"]
    assert index.exclusions_generales(BAL) == [f"{BAL}:s13"]


def test_les_deux_rangements_dune_section_dexclusions_sont_lus(index: Index) -> None:
    """AXA la range à côté de la garantie, Baloise dedans : ni l'un ni l'autre n'est un cas particulier."""
    assert index.exclusions_de_la_garantie(f"{AXA}:a3.1.2.1") == [f"{AXA}:a3.1.2.2"]
    assert index.exclusions_de_la_garantie(f"{BAL}:s7.1") == [f"{BAL}:s7.1.1"]


def test_le_titre_ne_suffit_pas_le_typage_majoritaire_rattrape(index: Index) -> None:
    """« Vols exclus », « Dommages exclus », « Sont exclus : » : aucune ne s'appelle « Exclusions »."""
    assert index.est_section_dexclusions(f"{AXA}:a3.1.6.2")
    assert index.est_section_dexclusions(f"{AXA}:a3.1.10.3")
    assert index.est_section_dexclusions(f"{BAL}:s7.2.1")


def test_une_partie_du_contrat_nest_pas_une_section_dexclusions_de_garantie(index: Index) -> None:
    """Contre-épreuve : `s10` (« Les garanties optionnelles… ») a un corps majoritairement exclusif.

    Sans la borne de profondeur, il remontait comme une exclusion attachée à **toute** garantie
    Baloise, et le refus aurait demandé d'ouvrir une partie entière du contrat.
    """
    assert index.est_section_dexclusions(f"{BAL}:s10")
    assert f"{BAL}:s10" not in index.exclusions_de_la_garantie(f"{BAL}:s7.1")


def test_une_section_est_couverte_par_lun_de_ses_descendants(index: Index) -> None:
    """`s13` est un intertitre : `ouvrir_noeud` le refuse, ce sont ses enfants qui portent le texte."""
    assert index.couvre_la_section(f"{BAL}:s13", [f"{BAL}:s13.2"])
    assert not index.couvre_la_section(f"{BAL}:s13", [f"{BAL}:s7.1"])


def test_un_noeud_sans_bloc_garantie_nouvre_aucune_dette(index: Index) -> None:
    """La règle est inerte là où le typage ne pose aucune garantie — le guide, une définition."""
    assert index.porte_une_garantie(f"{AXA}:a3.1.1.1.1")
    assert not index.porte_une_garantie(f"{AXA}:a2.10")
