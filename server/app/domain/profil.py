"""Profil déclaré par l'utilisateur du guide (AD-11 : filtré par PROFIL_KEYS, extra='allow')."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# Clés du profil du site conservées telles quelles ; le reste est ignoré par l'API.
PROFIL_KEYS: frozenset[str] = frozenset({
    "situation", "nationalite", "pays_origine", "statut", "famille", "enfants",
    "logement", "vehicule", "commune", "langue", "arrivee", "emploi",
})


class Profil(BaseModel):
    model_config = ConfigDict(extra="allow")

    def filtered(self) -> dict[str, Any]:
        """Sous-ensemble du profil limité à PROFIL_KEYS."""
        return {k: v for k, v in self.model_dump().items() if k in PROFIL_KEYS}
