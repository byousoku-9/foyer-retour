"""AD-10 — La trace est un objet, émise à chaque réponse."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Usage(BaseModel):
    """AD-9 : usage réel renvoyé par l'API, coût en euros calculé depuis cet usage."""

    input: int = 0
    cached: int = 0
    output: int = 0
    cost_eur: float = 0.0
    cached_response: bool = False
    cost_eur_original: float = 0.0


class CheckResult(BaseModel):
    name: str
    ok: bool = True
    detail: str = ""


class LLMCall(BaseModel):
    model: str
    ms: int = 0
    usage: Usage = Field(default_factory=Usage)
    cache_read: int = 0
    cache_write: int = 0
    tools: list[str] = Field(default_factory=list)


class StepTrace(BaseModel):
    name: str
    tier: str | None = None
    ms: int = 0
    usage: Usage = Field(default_factory=Usage)
    opened_block_ids: list[str] = Field(default_factory=list)
    discarded_block_ids: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    calls: list[LLMCall] = Field(default_factory=list)


class Trace(BaseModel):
    request_id: str
    pipeline: str
    variant: str = "deterministe"
    steps: list[StepTrace] = Field(default_factory=list)
    total_cost_eur: float = 0.0
    source_hash: dict[str, str] = Field(default_factory=dict)  # par doc_id
    ingest_fingerprint: dict[str, str] = Field(default_factory=dict)
    pipeline_digest: str = ""
    prompts_digest: str = ""
    thresholds: dict[str, float | int] = Field(default_factory=dict)  # rempli par Settings.thresholds()
    retries: int = 0
    truncations: int = 0
    deadline_remaining_s: float | None = None
