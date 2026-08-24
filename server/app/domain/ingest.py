"""AD-7 / AD-8 — Contrats partagés entre l'ingestion, le corpus et l'API : rapport, gate, manifest."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .document import DomainModel

CheckLevel = Literal["bloquant", "alerte", "info"]
GateProfile = Literal["vertical", "full"]
ManifestStatus = Literal["servi", "quarantaine"]


class Check(DomainModel):
    """Un contrôle statique d'ingestion (AD-8) : calculé sans pipeline."""

    name: str
    level: CheckLevel
    detail: str = ""


class Report(DomainModel):
    """`report.json` : liste de checks + statistiques descriptives."""

    doc_id: str
    checks: list[Check] = Field(default_factory=list)
    stats: dict[str, int | float | str | dict[str, int]] = Field(default_factory=dict)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.level == "bloquant"]

    @property
    def alerts(self) -> list[Check]:
        return [c for c in self.checks if c.level == "alerte"]


class Gate(DomainModel):
    """Écrit uniquement par `evals run --gate {doc_id}` (AD-7) ; jamais par l'ingestion."""

    profile: GateProfile
    source_hash: str
    ingest_fingerprint: str
    cases_hash: str
    pipeline_digest: str
    prompts_digest: str
    model_ids: dict[str, str] = Field(default_factory=dict)
    evals_ok: bool
    date: str
    overlay_hash: str | None = None  # empreinte de `typing.manual.json` au moment du gate (revue Codex 1.2)
    # Nombre de cas de la suite réellement exécutés par le run qui a écrit ce gate (story 1.10).
    # L'accueil annonce « niveau de validation : vertical — N cas relus à la main » : ce N est une
    # propriété **du gate**, pas un littéral de la page — écrit là où il est constaté, publié par
    # `/api/v1/sante` (`gate_cases`), et couvert par `cases_hash` qui dit *quels* cas c'étaient.
    # Défaut 0 : les gates écrits avant ce champ (il n'y en a aucun) resteraient lisibles.
    cases: int = Field(default=0, ge=0)


class GateContext(DomainModel):
    """Ce que l'image en cours sait d'elle-même ; comparé au gate du manifest ⇒ alerte `gate_perime` (AD-7)."""

    pipeline_digest: str = ""
    prompts_digest: str = ""
    model_ids: dict[str, str] = Field(default_factory=dict)


class ManifestEntry(DomainModel):
    status: ManifestStatus
    source_hash: str
    ingest_fingerprint: str
    document_hash: str  # sha256 de `document.json`, recalculé par le loader (AD-7)
    edition: str
    # sha256 de `typing.manual.json` (None si absent à l'ingestion) : l'overlay est couvert par le manifest et par
    # le gate comme `document.json` l'est par `document_hash` (amendement AD-7, revue Codex 1.2).
    overlay_hash: str | None = None
    gate: Gate | None = None


Manifest = dict[str, ManifestEntry]
