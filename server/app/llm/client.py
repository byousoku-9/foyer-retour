"""AD-9/10/16 — Le client Claude unique : async, structured outputs, cache de préfixe, deadline,
plafonds, coût réel, mapping exhaustif des erreurs fournisseur, trace de chaque appel.

Choix d'implémentation (Design Notes de la spec 1.3, convention LLM du spine amendée en revue 1.3) :
l'appel passe par `client.messages.parse(..., output_config={"format": …})` — schéma produit par
`anthropic.transform_schema` — **sans** `output_format=output_model` : avec `output_format`, le SDK
1.0.0 valide le texte avant de rendre la réponse et lève `ValidationError` — `usage`, `stop_reason`
et le texte reçu seraient perdus pour la trace (AD-10), le coût réel et le retry. La validation est
faite localement par `TypeAdapter(output_model).validate_json`, le code même de `parse_text` du SDK ;
le corps envoyé est identique sur le fil.

Le client Anthropic est construit avec `max_retries=0` : les retries du SDK (429/5xx)
casseraient la deadline et le compteur d'appels (AD-9).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

import anthropic
import pydantic
from anthropic import AsyncAnthropic
from pydantic import TypeAdapter

from server.app.config import Settings
from server.app.domain.errors import BudgetExceeded, ErrorCode, LlmParse, LlmUnavailable, PipelineError, Timeout
from server.app.domain.trace import CheckResult, LLMCall, StepTrace, Usage

from .budget import RequestBudget
from .models import EFFORT, MODEL_CAPS, Tier, model_for
from .pricing import cost_from_usage, estimate_cost

T = TypeVar("T", bound=pydantic.BaseModel)

# Table exhaustive de mapping des erreurs fournisseur (AD-16), testée classe par classe.
# L'ordre n'importe pas : la résolution parcourt le MRO de l'exception (APITimeoutError avant
# APIConnectionError, sous-classes de statut avant APIStatusError).
PROVIDER_ERRORS: dict[type[Exception], ErrorCode] = {
    anthropic.APITimeoutError: ErrorCode.timeout,
    anthropic.APIConnectionError: ErrorCode.llm_unavailable,  # réseau
    anthropic.RateLimitError: ErrorCode.llm_unavailable,  # 429
    anthropic.OverloadedError: ErrorCode.llm_unavailable,  # 529
    anthropic.InternalServerError: ErrorCode.llm_unavailable,  # 5xx
    anthropic.ServiceUnavailableError: ErrorCode.llm_unavailable,  # 503
    anthropic.DeadlineExceededError: ErrorCode.llm_unavailable,  # 504
    anthropic.AuthenticationError: ErrorCode.llm_unavailable,  # 401
    anthropic.PermissionDeniedError: ErrorCode.llm_unavailable,  # 403
    anthropic.BadRequestError: ErrorCode.internal,  # 400 : notre requête est fausse
    anthropic.NotFoundError: ErrorCode.internal,  # 404
    anthropic.UnprocessableEntityError: ErrorCode.internal,  # 422
    anthropic.RequestTooLargeError: ErrorCode.internal,  # 413
    anthropic.ConflictError: ErrorCode.internal,  # 409
    anthropic.APIStatusError: ErrorCode.internal,  # tout statut restant
    anthropic.APIResponseValidationError: ErrorCode.internal,  # réponse hors schéma SDK
    anthropic.RetryableError: ErrorCode.llm_unavailable,  # le SDK la déclare transitoire
    anthropic.AnthropicError: ErrorCode.internal,  # toute erreur SDK résiduelle — le mapping est total
}


def map_provider_error(exc: Exception) -> PipelineError:
    """Erreur SDK → erreur typée du domaine ; message = classe SDK + request_id, jamais la clé ni le corps."""
    request_id = getattr(exc, "request_id", None)
    message = f"{type(exc).__name__} (request_id={request_id or 'absent'})"
    for cls in type(exc).__mro__:
        code = PROVIDER_ERRORS.get(cls)
        if code is None:
            continue
        if code is ErrorCode.timeout:
            return Timeout(message)
        return LlmUnavailable(message, code=code)
    raise exc  # pas une erreur du SDK Anthropic : on ne l'avale pas


class ResponseCache(Protocol):
    """Cache d'évals (AD-11, implémentation persistante en 4.1) : réponse brute + coût d'origine."""

    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, value: dict[str, Any]) -> None: ...


class MemoryResponseCache:
    """Implémentation mémoire du protocole, suffisante pour tester `cached_response`."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._store.get(key)

    def set(self, key: str, value: dict[str, Any]) -> None:
        self._store[key] = value


@dataclass
class LlmResult(Generic[T]):
    parsed: T
    usage: Usage
    call: LLMCall


def _cache_key(body: dict[str, Any]) -> str:
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text_of(message: Any) -> str:
    return "".join(block.text for block in message.content if getattr(block, "type", None) == "text")


class LlmClient:
    """Client unique des étapes ; ne connaît ni les étapes ni les pipelines."""

    def __init__(self, settings: Settings, anthropic_client: Any | None = None,
                 cache: ResponseCache | None = None) -> None:
        self._settings = settings
        self._cache = cache
        if anthropic_client is None:
            anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=0)
        self._anthropic = anthropic_client

    async def parse(
        self,
        *,
        tier: Tier,
        system_prefix: str,
        messages: list[dict[str, Any]],
        output_model: type[T],
        budget: RequestBudget,
        step: StepTrace,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> LlmResult[T]:
        """Un appel structuré : préfixe caché, timeout borné par la deadline, 1 retry sur parse invalide."""
        settings = self._settings
        model = model_for(tier)
        caps = MODEL_CAPS[model]
        max_tokens = max_tokens or settings.llm_max_output_tokens
        adapter: TypeAdapter[T] = TypeAdapter(output_model)

        cache_control: dict[str, Any] = {"type": "ephemeral"}
        if caps["cache_ttl"] == "1h":
            cache_control["ttl"] = "1h"
        system = [{"type": "text", "text": system_prefix, "cache_control": cache_control}]
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(adapter.json_schema())},
        }
        if caps["effort"]:
            output_config["effort"] = EFFORT[tier]
        extra_body = {"temperature": 0} if caps["temperature"] else None

        msgs = list(messages)
        retried = False
        while True:
            if budget.remaining() <= 0:
                raise Timeout(f"deadline épuisée avant l'appel ({budget.remaining():.1f} s restantes)")

            body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": msgs,
                    "output_config": output_config, "tools": tools, "extra_body": extra_body}
            key = _cache_key(body)
            if self._cache is not None and (hit := self._cache.get(key)) is not None:
                try:
                    message = anthropic.types.Message.model_validate(hit["response"])
                    parsed_hit = adapter.validate_json(_text_of(message))
                except (pydantic.ValidationError, KeyError, TypeError) as exc:
                    raise LlmParse(f"entrée de cache d'évals invalide : {type(exc).__name__}") from exc
                usage = Usage(cached_response=True, cost_eur=0.0, cost_eur_original=hit["cost_eur"])
                call = LLMCall(model=message.model, ms=0, usage=usage)
                self._note_call(step, call)
                return LlmResult(parsed=parsed_hit, usage=usage, call=call)

            if budget.attempts >= budget.max_attempts:
                raise BudgetExceeded(f"plafond d'appels atteint ({budget.attempts}/{budget.max_attempts})")
            estimate = estimate_cost(model, system, msgs, max_tokens, settings, tools=tools)
            if budget.cost_eur + estimate > budget.max_cost_eur:
                raise BudgetExceeded(
                    f"plafond de coût par requête : {budget.cost_eur:.4f} € déjà engagés "
                    f"+ {estimate:.4f} € estimés > {budget.max_cost_eur:.4f} €"
                )

            timeout = budget.timeout_for_call(settings.llm_timeout_s)
            kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system,
                                      "messages": msgs, "output_config": output_config, "timeout": timeout}
            if tools is not None:
                kwargs["tools"] = tools
            if extra_body is not None:
                kwargs["extra_body"] = extra_body

            tool_names = [t.get("name", "") for t in tools] if tools else []
            budget.attempts += 1
            t0 = time.monotonic()
            try:
                message = await self._anthropic.messages.parse(**kwargs)
            except Exception as exc:  # noqa: BLE001 — mapping total des erreurs SDK, le reste est relancé tel quel
                # AD-10 : l'appel en échec est tracé aussi — modèle demandé, durée, usage nul
                # (l'API n'a rien renvoyé : timeout, 429, 529, réseau…).
                ms = int((time.monotonic() - t0) * 1000)
                self._note_call(step, LLMCall(model=model, ms=ms, usage=Usage(), tools=tool_names))
                raise map_provider_error(exc) from exc
            ms = int((time.monotonic() - t0) * 1000)

            usage = cost_from_usage(model, message.usage, settings.usd_eur)
            budget.note_call(usage)
            call = LLMCall(model=message.model, ms=ms, usage=usage,
                           cache_read=usage.cached, cache_write=self._cache_write_tokens(message.usage),
                           tools=tool_names)
            self._note_call(step, call)
            # AD-10 (revue Codex 1.3, I1) : le seuil porte sur le coût cumulé de la requête — un appel
            # cher isolé le franchit aussi ; le check n'est ajouté qu'une fois, au franchissement.
            if budget.cost_eur > settings.cost_alert_eur and not budget.cost_alerted:
                budget.cost_alerted = True
                step.checks.append(CheckResult(
                    name="cout_eleve", ok=False,
                    detail=f"coût cumulé de la requête {budget.cost_eur:.4f} € > "
                           f"cost_alert_eur {settings.cost_alert_eur:.4f} € (dernier appel : {usage.cost_eur:.4f} €)"))

            text = _text_of(message)
            if message.stop_reason == "refusal":
                raise LlmParse("le modèle a refusé de répondre (stop_reason=refusal)")
            if message.stop_reason in ("tool_use", "pause_turn"):
                # revue P9 : le dialogue d'outils (story 2.6) n'est pas supporté par ce client — pas de retry,
                # rejouer la même requête reproduirait le même stop_reason.
                raise LlmParse(f"dialogue d'outils non supporté par ce client (story 2.6) — "
                               f"stop_reason={message.stop_reason}")

            problem: str | None = None
            if message.stop_reason == "max_tokens":
                problem = f"réponse tronquée (stop_reason=max_tokens, max_tokens={max_tokens})"
            else:
                try:
                    parsed = adapter.validate_json(text)
                except pydantic.ValidationError as exc:
                    problem = f"réponse non conforme au schéma : {exc.error_count()} erreur(s) de validation"
            if problem is None:
                if self._cache is not None:
                    self._cache.set(key, {"response": message.to_dict(), "cost_eur": usage.cost_eur})
                return LlmResult(parsed=parsed, usage=usage, call=call)

            can_retry = (not retried and budget.attempts < budget.max_attempts
                         and budget.remaining() > settings.llm_retry_margin_s)
            if not can_retry:
                raise LlmParse(problem)
            retried = True
            step.checks.append(CheckResult(name="parse_retry", ok=False, detail=problem))
            # Le préfixe reste byte-identique : le motif est porté par un tour supplémentaire.
            msgs = [*msgs,
                    {"role": "assistant", "content": text or "(réponse vide)"},
                    {"role": "user", "content": f"Ta réponse précédente était invalide — {problem}. "
                                                "Réponds à nouveau, uniquement avec le JSON conforme au schéma."}]

    async def count_tokens(self, model: str, system: str | None, messages: list[dict[str, Any]]) -> int:
        """Tokens réels d'une requête au tokenizer du modèle (`/v1/messages/count_tokens`)."""
        kwargs: dict[str, Any] = {"model": model, "messages": messages,
                                  "timeout": self._settings.count_tokens_timeout_s}
        if system is not None:
            kwargs["system"] = system
        try:
            counted = await self._anthropic.messages.count_tokens(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise map_provider_error(exc) from exc
        return counted.input_tokens

    @staticmethod
    def _cache_write_tokens(api_usage: Any) -> int:
        creation = getattr(api_usage, "cache_creation", None)
        if creation is not None:
            return int(getattr(creation, "ephemeral_5m_input_tokens", 0) or 0) + \
                int(getattr(creation, "ephemeral_1h_input_tokens", 0) or 0)
        return int(getattr(api_usage, "cache_creation_input_tokens", 0) or 0)

    @staticmethod
    def _note_call(step: StepTrace, call: LLMCall) -> None:
        step.calls.append(call)
        u, s = call.usage, step.usage
        s.input += u.input
        s.cached += u.cached
        s.output += u.output
        s.cost_eur = round(s.cost_eur + u.cost_eur, 4)
        s.cost_eur_original = round(s.cost_eur_original + u.cost_eur_original, 4)
