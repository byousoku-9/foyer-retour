"""AD-16 — Codes d'erreur : Enum unique, enveloppe {error: {code, message, request_id}, trace?}."""

from __future__ import annotations

from enum import Enum
from typing import Any

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
    """Erreur terminale d'une étape ; l'API la convertit en enveloppe.

    AD-16 : « 503 avec trace partielle ». L'erreur porte donc ce qui a déjà été observé au moment où
    elle est levée — `step`, le `StepTrace` de l'étape qui a échoué (renseigné par l'étape, avec ses
    `calls[]` : un appel facturé qui échoue reste tracé, AD-10), et `trace`, la `Trace` partielle
    assemblée par le pipeline. Sans eux, le coût d'un appel raté disparaîtrait de la trace tout en
    comptant dans le budget (revue Codex 1.5, B5).
    """

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        super().__init__(message or code.value)
        self.code = code
        self.message = message or code.value
        self.step: Any = None
        self.trace: Trace | None = None
        # Le runner d'évaluation renseigne cette valeur depuis son RequestBudget même lorsqu'une
        # trace partielle n'a pas encore pu être assemblée.
        self.eval_cost_eur: float = 0.0


class InvalidRequest(PipelineError):
    """Entrée hors bornes constatée par le pipeline (AD-11/AD-16) : jamais de troncature silencieuse.

    AD-11 borne l'historique à 6 tours et exige « 400 au-delà, jamais tronqué côté serveur ». Le
    pipeline porte la borne parce qu'il est le seul chemin vers *comprendre* et *rédiger*, et il doit
    lever **avant** le premier appel modèle (rien n'est facturé pour une requête qu'on refuse). Depuis
    la story 1.6, la route l'applique **aussi** (`api/schemas.ChatRequest.borner`) : sur un appel HTTP
    elle tranche la première, et le contrôle du pipeline reste le filet de tout autre appelant (évals,
    scripts, tests) — d'où deux points d'application, volontairement, et le même message.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(ErrorCode.invalid_request, message)


class CorpusUnavailable(PipelineError):
    """Le document demandé n'est pas servi : absent du manifest, ou mis en quarantaine (AD-7).

    Levé par le pipeline **avant** le premier appel modèle : sans ce contrôle, `retrouver` finit sur
    un `KeyError` nu — donc un 500 `internal` — après une étape déjà facturée (revue 1.5).
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(ErrorCode.corpus_unavailable, message)


class LlmUnavailable(PipelineError):
    """Fournisseur injoignable ou en erreur (`llm_unavailable`), ou requête fausse de notre part (`internal`)."""

    def __init__(self, message: str = "", code: ErrorCode = ErrorCode.llm_unavailable) -> None:
        if code not in (ErrorCode.llm_unavailable, ErrorCode.internal):
            raise ValueError(f"LlmUnavailable n'admet que llm_unavailable ou internal, pas {code.value}")
        super().__init__(code, message)


class LlmParse(PipelineError):
    """Réponse non conforme au schéma après le retry autorisé, ou refus du modèle.

    `stop_reason` porte la raison rendue par le **fournisseur** quand elle est connue. Une sortie
    coupée par `max_tokens` n'est pas une sortie mal formée : elle est bien formée et inachevée, et
    l'appelant qui sait la redemander autrement (le tour terminal de `steps/naviguer.py`) doit
    pouvoir l'en distinguer sans relire le texte français du motif — un message d'erreur n'est pas
    un contrat, et le lire ainsi casserait à la première reformulation.
    """

    def __init__(self, message: str = "", *, stop_reason: str | None = None) -> None:
        super().__init__(ErrorCode.llm_parse, message)
        self.stop_reason = stop_reason


class Timeout(PipelineError):
    """Deadline globale épuisée ou appel en timeout."""

    def __init__(self, message: str = "") -> None:
        super().__init__(ErrorCode.timeout, message)


class BudgetExceeded(PipelineError):
    """Plafond d'appels ou de coût par requête atteint — avant l'appel, jamais après."""

    def __init__(self, message: str = "") -> None:
        super().__init__(ErrorCode.budget_exceeded, message)


class TruncatedRead(BudgetExceeded):
    """Lecture bornée sans résultat fiable : sortie mesurée rouge, pas plafond financier."""

    def __init__(self, message: str = "") -> None:
        # Le contrat HTTP historique reste un 503 `budget_exceeded`; le type Python permet au
        # harness de le distinguer d'un vrai dépassement de coût et de poursuivre les répétitions.
        super().__init__(message)
