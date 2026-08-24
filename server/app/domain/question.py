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


# Convention « Données & formats » du spine : langues **ISO 639-1**, donc exactement deux lettres
# (revue Codex 1.4, I2 — `^[a-z]{2,3}$` laissait passer `eng`/`fra` jusque dans la consigne de rédaction).
_LANG = re.compile(r"^[a-z]{2}$")


def _lang_or_fr(value: str) -> str:
    """Code ISO 639-1 en minuscules (deux lettres) ; tout le reste retombe sur `fr` (convention Langue).

    La normalisation vit ici, pas dans les étapes : *comprendre* et *rédiger* ne peuvent pas
    s'importer l'une l'autre (AD-9) et la dupliquaient avec deux sémantiques légèrement différentes —
    un `ParsedQuestion(language="EN")` construit ailleurs (pipeline 1.5, reprise) retombait alors
    silencieusement sur `fr` à la rédaction seulement (revue 1.4). Les deux issues de *comprendre*
    (question résolue ou clarification) partagent la même règle.
    """
    v = (value or "").strip().lower()
    return v if _LANG.match(v) else "fr"


class ParsedQuestion(DomainModel):
    """Question **autonome** : toutes les anaphores sont résolues (AD-5).

    Ce type est le seul laissez-passer vers *retrouver*. Quand l'historique ne permet pas de résoudre
    une référence, *comprendre* ne construit pas cet objet du tout : il rend un `ClarificationRequise`.
    """

    question_resolue: str
    intent: Intent
    language: str = "fr"
    terms: list[str] = Field(default_factory=list)  # toujours en français
    scope: QuestionScope = Field(default_factory=QuestionScope)

    @field_validator("language")
    @classmethod
    def _normalise_language(cls, value: str) -> str:
        return _lang_or_fr(value)


class ClarificationRequise(DomainModel):
    """Seconde issue de *comprendre* : la question n'est pas résoluble, il faut la poser (AD-5).

    AD-5, mot pour mot : « une anaphore non résoluble avec l'historique produit `Answer.clarification`
    (question à l'utilisateur) — *comprendre* ne fabrique jamais une `question_resolue` ». Le signal
    naît donc à l'étape qui constate l'échec de résolution, et il est **typé** : un tour 2 (revue
    Codex 1.4, B4) l'avait porté par un champ de `ParsedQuestion`, ce qui laissait subsister une
    `question_resolue` non autonome — rien n'empêchait alors *retrouver* de chercher « et pour eux ? ».
    Les deux issues sont désormais des types distincts, et la question non autonome n'existe nulle
    part dans le résultat. Sa *restitution* à l'utilisateur reste l'AC de la story 2.2 ;
    `Answer.clarification` existe déjà pour la porter, `found=False`.
    """

    clarification: str  # la question courte à poser à l'utilisateur, dans sa langue
    intent: Intent
    language: str = "fr"

    @field_validator("language")
    @classmethod
    def _normalise_language(cls, value: str) -> str:
        return _lang_or_fr(value)
