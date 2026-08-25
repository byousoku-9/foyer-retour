"""Profil déclaré par l'utilisateur du guide (AD-11 : filtré par PROFIL_KEYS, extra='allow')."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict

from .document import ParcoursCondition

# Clés du profil du site conservées telles quelles ; le reste est ignoré par l'API.
#
# `horizon` est l'un des six champs que le questionnaire du site demande (`web/app/chat.js`,
# `CHAMPS` : situation, enfants, statut, logement, vehicule, horizon) et le seul qui manquait ici :
# l'oubli datait de la story 1.0 et ne se voyait pas tant que `chat.js` envoyait le profil en
# **chaîne**. Depuis la story 1.7, le profil part **brut** (AD-11) — sans cette clé, `filtered()`
# perdrait en silence un sixième de ce qui a été demandé à l'utilisateur.
PROFIL_KEYS: frozenset[str] = frozenset({
    "situation", "nationalite", "pays_origine", "statut", "famille", "enfants",
    "logement", "vehicule", "commune", "langue", "arrivee", "emploi", "horizon",
})


class Profil(BaseModel):
    model_config = ConfigDict(extra="allow")

    def filtered(self) -> dict[str, Any]:
        """Sous-ensemble du profil limité à PROFIL_KEYS."""
        return {k: v for k, v in self.model_dump().items() if k in PROFIL_KEYS}


# Les valeurs du questionnaire qui disent « je n'ai pas tranché » (`web/app/chat.js::CHAMPS`, sans
# accent : ce sont des identifiants, pas des libellés d'affichage). Le site les traite comme
# satisfaisant **toutes** les valeurs de leur clé — quelqu'un qui hésite entre louer et acheter voit
# les deux branches du parcours — et le serveur en fait autant : deux nœuds désignés, pas un filtre.
INDECIS: dict[str, str] = {"logement": "pas encore decide", "statut": "les deux"}
# `enfants` est une **quantité** dans le questionnaire (`"Aucun"`, `"1"`, `"2"`, `"3 ou plus"`) et un
# booléen dans la condition de la source (`si: {enfants: true}`) : la clé est vraie dès qu'il y a des
# enfants, c'est-à-dire partout sauf ici. `""` couvre le champ laissé vide par un profil bricolé.
ENFANTS_FAUX: frozenset[str] = frozenset({"aucun", "0", "", "non", "false"})
# `vehicule` n'a que deux réponses (`"Oui"`/`"Non"`) ; seule la première désigne les fiches du volet
# automobile. Un booléen est accepté tel quel : c'est ce que la condition de la source écrit, et un
# appelant du domaine (tests, évals) peut poser `Profil(vehicule=True)` sans passer par le formulaire.
VEHICULE_VRAI: frozenset[str] = frozenset({"oui", "true"})


def _plat(valeur: Any) -> str:
    """Forme comparable d'une valeur de profil ou de condition : casse, espaces **et accents** neutralisés.

    Pas de `normalize()` de `corpus` — la couche `domain` n'importe que la stdlib et pydantic
    (`tests/test_layers.py`) — mais le retrait des diacritiques, lui, est repris : les valeurs du
    questionnaire sont écrites sans accent (« Independant », « Pas encore decide ») **parce que ce
    sont des identifiants**, et un profil bricolé ou recopié à la main (« Indépendant ») ne
    correspondait à rien, en silence, sans qu'aucun test ni aucune alerte ne le dise (revue
    coordonnée 2.3, A12). Deux lignes de stdlib valent mieux qu'un échec muet.
    """
    if isinstance(valeur, bool):
        return "true" if valeur else "false"
    plat = " ".join(str(valeur).split()).casefold()
    return "".join(c for c in unicodedata.normalize("NFD", plat) if not unicodedata.combining(c))


def _booleen(cle: str, valeur_profil: str) -> bool:
    """Ce que le profil dit d'une clé que la source écrit en booléen (`enfants`, `vehicule`)."""
    if cle == "enfants":  # une quantité côté profil (voir ENFANTS_FAUX), un booléen côté condition
        return valeur_profil not in ENFANTS_FAUX
    return valeur_profil in VEHICULE_VRAI


def _cle_satisfaite(cle: str, valeur_profil: Any, valeur_condition: Any) -> bool:
    """Une clé de `si` est-elle satisfaite par ce que le profil déclare ?

    Les règles sont celles que le site applique dans `web/app/ui.js::etapeConcerne`, avec deux
    différences assumées : l'économie décrite par `noeuds_du_profil` (une clé absente ne satisfait
    pas), et le fait qu'une condition **booléenne** est lue pour ce qu'elle dit.

    `etapeConcerne` ignore la valeur écrite dans `si` pour `enfants` et `vehicule` : il teste
    seulement « le profil a-t-il des enfants / un véhicule ». C'est sans effet sur le guide livré,
    dont les neuf conditions sont toutes positives — mais une condition « cette fiche concerne qui
    n'a **pas** de voiture » (`si: {vehicule: false}`), que le type `dict[str, str | bool]` autorise
    et que l'ingestion accepterait, se serait trouvée satisfaite exactement par les profils qui en
    ont une (revue coordonnée 2.3, A10). Une inversion silencieuse ne se laisse pas hériter : la
    valeur est honorée.
    """
    if valeur_profil is None:
        # `null` envoyé par la page vaut « pas renseigné », pas « autre valeur » : même traitement
        # qu'une clé absente (voir l'inversion assumée), sans quoi `_plat(None)` vaudrait « none »,
        # une chaîne que rien ne reconnaît comme fausse — et un profil vidé désignerait `ecole`.
        return False
    profil = _plat(valeur_profil)
    if cle in ("enfants", "vehicule"):
        if isinstance(valeur_condition, bool):
            return _booleen(cle, profil) is valeur_condition
        # Une condition écrite en toutes lettres (`si: {vehicule: "Oui"}`) retombe sur l'égalité :
        # c'est la règle générale, et elle dit la même chose sans avoir à deviner.
        return profil == _plat(valeur_condition)
    indecis = INDECIS.get(cle)
    return profil == _plat(valeur_condition) or (indecis is not None and profil == indecis)


def noeuds_du_profil(parcours: Iterable[ParcoursCondition], profil: Profil | dict[str, Any]) -> list[str]:
    """Les nœuds que le profil **désigne** : `si` entièrement satisfaite, dans l'ordre du parcours.

    Story 2.3. Ce que la fonction rend est une liste de `node_id` sans doublon, que le pipeline passe
    à *retrouver* comme `noeuds_prioritaires`. Elle **ordonne**, elle n'ajoute jamais : un nœud
    désigné n'est ouvert que s'il est déjà candidat pour les termes cherchés — aucune fiche n'entre
    dans le contexte du modèle du seul fait du profil, et le profil n'est jamais un filtre.

    **Inversion assumée par rapport au site.** `ui.js::etapeConcerne` est permissif sur une clé non
    renseignée : il **cache** des étapes, donc dans le doute il montre (`if (v === undefined)
    return;`). Ici on **promeut** des fiches : dans le doute, on ne promeut pas — sans quoi un profil
    vide satisferait toutes les conditions et désignerait les neuf fiches conditionnées, c'est-à-dire
    aucune. Les deux règles disent la même chose dans deux économies opposées ; une clé absente du
    profil ne satisfait donc **pas** la condition.
    """
    declare = profil.filtered() if isinstance(profil, Profil) else dict(profil)
    designes: list[str] = []
    for condition in parcours:
        if not condition.si or condition.node_id in designes:
            continue
        if all(cle in declare and _cle_satisfaite(cle, declare[cle], valeur)
               for cle, valeur in condition.si.items()):
            designes.append(condition.node_id)
    return designes
