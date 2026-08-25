"""Le profil désigne des nœuds (story 2.3) : `domain/profil.py::noeuds_du_profil` et `Document.parcours`.

Ce module tient la moitié « données » de l'AC — quelles fiches un profil désigne, et pourquoi — sans
retrieval ni pipeline. La règle est celle du site (`web/app/ui.js::etapeConcerne`), à une inversion
près qu'un test amarre explicitement : le site **cache** des étapes et se tait sur une clé non
renseignée, nous **promouvons** des fiches et refusons de promouvoir dans le doute.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.app.domain import BlockRef, Document, Node, NodeRef, ParcoursCondition
from server.app.domain.document import Block
from server.app.domain.profil import Profil, noeuds_du_profil

# Les neuf conditions du guide réel (`data/lux-guide/source.js`), dans l'ordre du parcours.
PARCOURS = [
    ParcoursCondition(node_id="ecole", si={"enfants": True}),
    ParcoursCondition(node_id="garde", si={"enfants": True}),
    ParcoursCondition(node_id="recherche_logement", si={"logement": "Louer"}),
    ParcoursCondition(node_id="achat", si={"logement": "Acheter"}),
    ParcoursCondition(node_id="assurance_auto", si={"vehicule": True}),
    ParcoursCondition(node_id="allocations", si={"enfants": True}),
    ParcoursCondition(node_id="independant", si={"statut": "Independant"}),
    ParcoursCondition(node_id="vehicule", si={"vehicule": True}),
    ParcoursCondition(node_id="permis", si={"vehicule": True}),
]


def test_un_profil_vide_ne_designe_rien() -> None:
    """L'inversion assumée, et la raison d'être du test : un profil vide ne désigne **aucune** fiche.

    `ui.js::etapeConcerne` rendrait `true` pour les neuf conditions — il filtre un parcours affiché,
    et dans le doute il montre tout. Ici la même permissivité désignerait les neuf fiches
    conditionnées, c'est-à-dire n'en désignerait aucune : promouvoir tout le monde, c'est ne
    promouvoir personne, et le comportement d'avant la story serait perdu sans que rien ne le dise.
    """
    assert noeuds_du_profil(PARCOURS, Profil()) == []
    assert noeuds_du_profil(PARCOURS, {}) == []
    # une clé **présente mais vide ou nulle** ne satisfait pas davantage qu'une clé absente
    assert noeuds_du_profil(PARCOURS, {"enfants": None}) == []
    assert noeuds_du_profil(PARCOURS, {"enfants": ""}) == []


@pytest.mark.parametrize("valeur", ["1", "2", "3 ou plus", True])
def test_enfants_est_une_quantite_qui_vaut_vrai_sauf_aucun(valeur: object) -> None:
    """La clé est une quantité côté questionnaire, un booléen côté source : toute quantité non nulle
    désigne les trois fiches de l'enfance, dans l'ordre du parcours."""
    assert noeuds_du_profil(PARCOURS, {"enfants": valeur}) == ["ecole", "garde", "allocations"]


@pytest.mark.parametrize("valeur", ["Aucun", "aucun", "  AUCUN  ", "0", False])
def test_aucun_enfant_ne_designe_rien(valeur: object) -> None:
    assert noeuds_du_profil(PARCOURS, {"enfants": valeur}) == []


def test_louer_acheter_et_lindecision_du_site() -> None:
    """AC : `"Acheter"` désigne `achat` et pas `recherche_logement` ; l'inverse pour `"Louer"` ; les
    deux pour `"Pas encore decide"` — exactement ce que le site fait de son parcours."""
    assert noeuds_du_profil(PARCOURS, {"logement": "Acheter"}) == ["achat"]
    assert noeuds_du_profil(PARCOURS, {"logement": "Louer"}) == ["recherche_logement"]
    assert noeuds_du_profil(PARCOURS, {"logement": "Pas encore decide"}) == ["recherche_logement", "achat"]
    # `"Les deux"` est l'indécision de `statut`, pas celle de `logement` : elle ne s'y applique pas
    assert noeuds_du_profil(PARCOURS, {"logement": "Les deux"}) == []


def test_statut_et_son_indecision() -> None:
    assert noeuds_du_profil(PARCOURS, {"statut": "Independant"}) == ["independant"]
    assert noeuds_du_profil(PARCOURS, {"statut": "Salarie"}) == []
    assert noeuds_du_profil(PARCOURS, {"statut": "Les deux"}) == ["independant"]
    assert noeuds_du_profil(PARCOURS, {"statut": "Pas encore d'emploi"}) == []


def test_vehicule_ne_vaut_que_sur_oui() -> None:
    assert noeuds_du_profil(PARCOURS, {"vehicule": "Oui"}) == ["assurance_auto", "vehicule", "permis"]
    assert noeuds_du_profil(PARCOURS, {"vehicule": True}) == ["assurance_auto", "vehicule", "permis"]
    assert noeuds_du_profil(PARCOURS, {"vehicule": "Non"}) == []


def test_la_comparaison_ignore_la_casse_et_les_espaces() -> None:
    assert noeuds_du_profil(PARCOURS, {"logement": "  acheter "}) == ["achat"]
    assert noeuds_du_profil(PARCOURS, {"statut": "INDEPENDANT"}) == ["independant"]
    assert noeuds_du_profil(PARCOURS, {"vehicule": " oui "}) == ["assurance_auto", "vehicule", "permis"]


def test_toutes_les_cles_dune_condition_doivent_etre_satisfaites() -> None:
    """`si` est une conjonction : une clé non satisfaite — ou absente du profil — suffit à écarter."""
    parcours = [ParcoursCondition(node_id="n", si={"enfants": True, "logement": "Louer"})]
    assert noeuds_du_profil(parcours, {"enfants": "2", "logement": "Louer"}) == ["n"]
    assert noeuds_du_profil(parcours, {"enfants": "2"}) == []
    assert noeuds_du_profil(parcours, {"enfants": "2", "logement": "Acheter"}) == []


def test_une_cle_hors_profil_keys_ne_peut_pas_satisfaire_une_condition() -> None:
    """AD-11 : le profil est **filtré** par `PROFIL_KEYS` avant tout usage — une clé qu'un appelant
    ajouterait pour forcer une promotion n'est jamais lue."""
    parcours = [ParcoursCondition(node_id="n", si={"marmotte": "Oui"})]
    assert noeuds_du_profil(parcours, Profil(marmotte="Oui")) == []
    # sur un dictionnaire brut (usage interne), la comparaison d'égalité s'applique comme partout
    assert noeuds_du_profil(parcours, {"marmotte": "Oui"}) == ["n"]


def test_un_noeud_conditionne_deux_fois_nest_designe_quune_fois() -> None:
    """Trois étapes du guide conditionnent `ecole` sur `{enfants: true}` : ce qui est désigné est une
    **fiche**, pas une étape, et l'ordre est celui de la première apparition."""
    parcours = [ParcoursCondition(node_id="ecole", si={"enfants": True}),
                ParcoursCondition(node_id="garde", si={"enfants": True}),
                ParcoursCondition(node_id="ecole", si={"enfants": True})]
    assert noeuds_du_profil(parcours, {"enfants": "1"}) == ["ecole", "garde"]


def test_une_condition_vide_ne_designe_jamais() -> None:
    """`si: {}` serait satisfaite par n'importe quel profil, profil vide compris : c'est le seul cas
    où « toutes les clés satisfaites » vaut vrai sans qu'aucune n'ait été vérifiée."""
    assert noeuds_du_profil([ParcoursCondition(node_id="n")], {"enfants": "2"}) == []


# --- l'invariant d'arbre (AD-2) --------------------------------------------
def _doc(parcours: list[ParcoursCondition]) -> Document:
    return Document(doc_id="d", kind="guide", title="t", edition="e",
                    nodes=[Node(node_id="root", items=[NodeRef(node_id="n1")]),
                           Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")])],
                    blocks=[Block(block_id="d:p1:1", text="Texte.", loc="p1", seq=1)],
                    parcours=parcours)


def test_un_parcours_ne_vise_que_des_noeuds_du_document() -> None:
    assert _doc([ParcoursCondition(node_id="n1", si={"enfants": True})]).parcours[0].node_id == "n1"
    with pytest.raises(ValidationError, match="parcours vise un nœud inconnu"):
        _doc([ParcoursCondition(node_id="n404", si={"enfants": True})])


def test_le_parcours_est_vide_par_defaut() -> None:
    """Défaut vide ⇒ `exclude_defaults=True` laisse le `document.json` d'un contrat inchangé (AD-7)."""
    doc = _doc([])
    assert doc.parcours == []
    assert "parcours" not in doc.model_dump(exclude_defaults=True)


# --- ce que la revue coordonnée a fait trancher -----------------------------
def test_une_condition_booleenne_negative_est_honoree_et_non_inversee() -> None:
    """A10 : `si: {vehicule: false}` désigne qui n'a **pas** de véhicule, pas l'inverse.

    Le site ignore la valeur écrite dans `si` pour `enfants`/`vehicule` (`etapeConcerne` ne teste que
    « le profil a-t-il un véhicule »). Sans effet sur le guide livré — ses neuf conditions sont toutes
    positives — mais le type `dict[str, str | bool]` autorise la négative, et l'ingestion l'accepte :
    la fiche « se déplacer sans voiture » aurait été promue exactement pour les automobilistes.
    """
    sans_voiture = [ParcoursCondition(node_id="transports", si={"vehicule": False})]
    assert noeuds_du_profil(sans_voiture, {"vehicule": "Non"}) == ["transports"]
    assert noeuds_du_profil(sans_voiture, {"vehicule": "Oui"}) == []
    # …et une clé absente ne satisfait toujours pas, y compris une condition négative : dans le doute
    # on ne promeut pas (l'inversion assumée vaut pour les deux polarités).
    assert noeuds_du_profil(sans_voiture, {}) == []

    sans_enfants = [ParcoursCondition(node_id="celibataire", si={"enfants": False})]
    assert noeuds_du_profil(sans_enfants, {"enfants": "Aucun"}) == ["celibataire"]
    assert noeuds_du_profil(sans_enfants, {"enfants": "2"}) == []


def test_une_condition_booleenne_ecrite_en_toutes_lettres_retombe_sur_legalite() -> None:
    """Le repli quand `si` n'écrit pas un booléen : la règle générale, sans rien deviner."""
    parcours = [ParcoursCondition(node_id="auto", si={"vehicule": "Oui"})]
    assert noeuds_du_profil(parcours, {"vehicule": "Oui"}) == ["auto"]
    assert noeuds_du_profil(parcours, {"vehicule": "Non"}) == []


def test_un_profil_accentue_designe_comme_le_profil_du_questionnaire() -> None:
    """A12 : les valeurs du questionnaire sont des identifiants sans accent, mais un profil recopié
    à la main ne doit pas échouer **en silence**. Les diacritiques sont retirés des deux côtés."""
    assert noeuds_du_profil(PARCOURS, {"statut": "Indépendant"}) == ["independant"]
    assert noeuds_du_profil(PARCOURS, {"logement": "Pas encore décidé"}) == ["recherche_logement", "achat"]
    assert noeuds_du_profil(PARCOURS, {"statut": "Les Deux"}) == ["independant"]
