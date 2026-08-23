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


class ManifestEntry(DomainModel):
    status: ManifestStatus
    source_hash: str
    ingest_fingerprint: str
    document_hash: str  # sha256 de `document.json`, recalculé par le loader (AD-7)
    edition: str
    gate: Gate | None = None


Manifest = dict[str, ManifestEntry]
