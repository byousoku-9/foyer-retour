"""AD-6 — Verdict « au regard des conditions générales seules », décidé par table."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

VerdictValue = Literal["couvert", "non_couvert", "sous_conditions", "ne_tranche_pas"]


class MissingPackage(BaseModel):
    conditions_particulieres: bool = True
    options_souscrites: bool = True
    avenants: bool = True
    date_effet: bool = True
    faits: list[str] = Field(default_factory=list)


class Verdict(BaseModel):
    value: VerdictValue
    reason: str
    missing: MissingPackage = Field(default_factory=MissingPackage)
    ask_client: list[str] = Field(default_factory=list)
    escalate: list[str] = Field(default_factory=list)
