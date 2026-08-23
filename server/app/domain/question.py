"""AD-5 / AD-11 — Question comprise, tour de conversation, faits d'un sinistre."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .document import DomainModel

Intent = Literal["question", "suivi", "meteo", "bavardage", "hors_perimetre"]
Role = Literal["user", "assistant"]


class Turn(DomainModel):
    role: Role
    texte: str = Field(max_length=2000)


class Faits(DomainModel):
    """Faits déclarés d'un sinistre (POST /api/v1/sinistre)."""

    date: str | None = None
    lieu: str | None = None
    montant_eur: float | None = Field(None, ge=0)
    description: str = Field(max_length=2000)


class QuestionScope(DomainModel):
    """Portée dérivée du profil ou des faits du sinistre."""

    themes: list[str] = Field(default_factory=list)  # école, allocations, auto…
    bien: str | None = None
    evenement: str | None = None
    lieu: str | None = None
    cause: str | None = None
    moment: str | None = None


_LANG = re.compile(r"^[a-z]{2,3}$")


class ParsedQuestion(DomainModel):
    question_resolue: str
    intent: Intent
    language: str = "fr"
    terms: list[str] = Field(default_factory=list)  # toujours en français
    scope: QuestionScope = Field(default_factory=QuestionScope)

    @field_validator("language")
    @classmethod
    def _lang_or_fr(cls, value: str) -> str:
        """Code ISO 639 en minuscules ; tout le reste retombe sur `fr` (convention Langue du spine).

        La normalisation vit ici, pas dans les étapes : *comprendre* et *rédiger* ne peuvent pas
        s'importer l'une l'autre (AD-9) et la dupliquaient avec deux sémantiques légèrement
        différentes — un `ParsedQuestion(language="EN")` construit ailleurs (pipeline 1.5, reprise)
        retombait alors silencieusement sur `fr` à la rédaction seulement (revue 1.4).
        """
        v = (value or "").strip().lower()
        return v if _LANG.match(v) else "fr"
