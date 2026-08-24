"""Profil déclaré par l'utilisateur du guide (AD-11 : filtré par PROFIL_KEYS, extra='allow')."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

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
