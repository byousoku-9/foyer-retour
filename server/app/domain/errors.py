"""AD-16 — Codes d'erreur : Enum unique, enveloppe {error: {code, message, request_id}, trace?}."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ErrorCode(str, Enum):
    invalid_request = "invalid_request"
    input_too_long = "input_too_long"
    rate_limited = "rate_limited"
    llm_unavailable = "llm_unavailable"
    llm_parse = "llm_parse"
    timeout = "timeout"
    budget_exceeded = "budget_exceeded"
    corpus_unavailable = "corpus_unavailable"
    internal = "internal"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.invalid_request: 400,
    ErrorCode.input_too_long: 413,
    ErrorCode.rate_limited: 429,
    ErrorCode.llm_unavailable: 503,
    ErrorCode.llm_parse: 503,
    ErrorCode.timeout: 503,
    ErrorCode.budget_exceeded: 503,
    ErrorCode.corpus_unavailable: 503,
    ErrorCode.internal: 500,
}


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody
    trace: dict | None = None


class PipelineError(Exception):
    """Erreur terminale d'une étape ; l'API la convertit en enveloppe."""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        super().__init__(message or code.value)
        self.code = code
        self.message = message or code.value
