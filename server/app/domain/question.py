"""AD-5 / AD-11 — Question comprise, tour de conversation, faits d'un sinistre."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Intent = Literal["question", "suivi", "meteo", "bavardage", "hors_perimetre"]
Role = Literal["user", "assistant"]


class Turn(BaseModel):
    role: Role
    texte: str = Field(max_length=2000)


class Faits(BaseModel):
    """Faits déclarés d'un sinistre (POST /api/v1/sinistre)."""

    date: str | None = None
    lieu: str | None = None
    montant_eur: float | None = None
    description: str = Field(max_length=2000)


class Scope(BaseModel):
    """Portée dérivée du profil ou des faits du sinistre."""

    themes: list[str] = Field(default_factory=list)  # école, allocations, auto…
    bien: str | None = None
    evenement: str | None = None
    lieu: str | None = None
    cause: str | None = None
    moment: str | None = None


class ParsedQuestion(BaseModel):
    question_resolue: str
    intent: Intent
    language: str = "fr"
    terms: list[str] = Field(default_factory=list)  # toujours en français
    scope: Scope = Field(default_factory=Scope)
