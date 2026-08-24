"""AD-16 — Codes d'erreur : Enum unique, enveloppe {error: {code, message, request_id}, trace?}."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from .document import DomainModel

from .trace import Trace


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


class ErrorBody(DomainModel):
    code: ErrorCode
    message: str
    request_id: str


class ErrorEnvelope(DomainModel):
    error: ErrorBody
    trace: Trace | None = None


class PipelineError(Exception):
    """Erreur terminale d'une étape ; l'API la convertit en enveloppe."""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        super().__init__(message or code.value)
        self.code = code
        self.message = message or code.value


class InvalidRequest(PipelineError):
    """Entrée hors bornes constatée par le pipeline (AD-11/AD-16) : jamais de troncature silencieuse.

    AD-11 borne l'historique à 6 tours et exige « 400 au-delà, jamais tronqué côté serveur ». La borne
    est portée par le pipeline et non par l'API : c'est lui qui passe l'historique à *comprendre* et à
    *rédiger*, et il doit lever **avant** le premier appel modèle (rien n'est facturé pour une requête
    qu'on refuse).
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(ErrorCode.invalid_request, message)


class LlmUnavailable(PipelineError):
    """Fournisseur injoignable ou en erreur (`llm_unavailable`), ou requête fausse de notre part (`internal`)."""

    def __init__(self, message: str = "", code: ErrorCode = ErrorCode.llm_unavailable) -> None:
        if code not in (ErrorCode.llm_unavailable, ErrorCode.internal):
            raise ValueError(f"LlmUnavailable n'admet que llm_unavailable ou internal, pas {code.value}")
        super().__init__(code, message)


class LlmParse(PipelineError):
    """Réponse non conforme au schéma après le retry autorisé, ou refus du modèle."""

    def __init__(self, message: str = "") -> None:
        super().__init__(ErrorCode.llm_parse, message)


class Timeout(PipelineError):
    """Deadline globale épuisée ou appel en timeout."""

    def __init__(self, message: str = "") -> None:
        super().__init__(ErrorCode.timeout, message)


class BudgetExceeded(PipelineError):
    """Plafond d'appels ou de coût par requête atteint — avant l'appel, jamais après."""

    def __init__(self, message: str = "") -> None:
        super().__init__(ErrorCode.budget_exceeded, message)
