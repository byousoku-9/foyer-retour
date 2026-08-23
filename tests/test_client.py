"""Matrice I/O de la spec 1.3 : chaque ligne du tableau a son test, plus la requête envoyée
(bloc système unique avec cache_control, ordre des messages, timeout, output_config, extra_body)."""

from __future__ import annotations

import anthropic
import pytest
from pydantic import BaseModel

from server.app.config import Settings
from server.app.domain.errors import BudgetExceeded, ErrorCode, LlmParse, LlmUnavailable, Timeout
from server.app.domain.trace import StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient, MemoryResponseCache, map_provider_error
from server.app.llm.models import TIERS
from tests.llm_fake import FakeAnthropic, fake_message, provider_exception

SONNET, HAIKU, OPUS = TIERS["reason"], TIERS["micro"], TIERS["ingest"]


class Mot(BaseModel):
    mot: str


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget(deadline_s: float = 30.0, max_attempts: int = 4, max_cost: float = 0.10) -> RequestBudget:
    return RequestBudget(deadline_s=deadline_s, max_attempts=max_attempts, max_cost_eur=max_cost)


def _client(script: list, settings: Settings | None = None, cache=None) -> tuple[LlmClient, FakeAnthropic]:
    fake = FakeAnthropic(script)
    return LlmClient(settings or _settings(), anthropic_client=fake, cache=cache), fake


async def _call(client: LlmClient, *, tier="micro", budget=None, step=None, **kw):
    return await client.parse(tier=tier, system_prefix="préfixe stable", messages=[{"role": "user", "content": "q"}],
                              output_model=Mot, budget=budget or _budget(), step=step or StepTrace(name="comprendre"),
                              **kw)


async def test_nominal_call_traces_counts_and_costs() -> None:
    client, fake = _client([fake_message(model=HAIKU, input_tokens=1000, output_tokens=100)])
    budget, step = _budget(), StepTrace(name="comprendre")
    result = await _call(client, tier="micro", budget=budget, step=step)
    assert result.parsed == Mot(mot="bonjour")
    assert budget.attempts == 1
    assert budget.cost_eur == result.usage.cost_eur > 0
    assert len(step.calls) == 1 and step.calls[0] is result.call
    assert step.usage.cost_eur == result.usage.cost_eur
    assert result.call.model == HAIKU and result.call.ms >= 0
    assert step.checks == []  # pas de cout_eleve : 1000×1 + 100×5 USD/MTok « 0,05 €


async def test_request_shape_micro_no_effort_temperature_zero() -> None:
    client, fake = _client([fake_message(model=HAIKU)])
    budget = _budget(deadline_s=40)
    history = [{"role": "user", "content": "avant"}, {"role": "assistant", "content": "réponse"},
               {"role": "user", "content": "question"}]
    step = StepTrace(name="comprendre")
    await client.parse(tier="micro", system_prefix="préfixe stable", messages=history, output_model=Mot,
                       budget=budget, step=step)
    (req,) = fake.requests
    assert req["model"] == HAIKU
    assert req["system"] == [{"type": "text", "text": "préfixe stable", "cache_control": {"type": "ephemeral"}}]
    assert req["messages"] == history  # historique et question après le préfixe, ordre conservé
    assert req["extra_body"] == {"temperature": 0}
    assert "effort" not in req["output_config"]
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["format"]["schema"]["required"] == ["mot"]
    assert req["max_tokens"] == _settings().llm_max_output_tokens
    assert 0 < req["timeout"] <= 25.0  # min(llm_timeout_s=25, restant≈40)
    assert "tools" not in req


async def test_request_shape_reason_effort_1h_ttl_no_temperature() -> None:
    client, fake = _client([fake_message(model=SONNET)])
    await _call(client, tier="reason")
    (req,) = fake.requests
    assert req["system"] == [{"type": "text", "text": "préfixe stable",
                              "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    assert req["output_config"]["effort"] == "medium"
    assert "extra_body" not in req


async def test_request_shape_ingest_effort_high_5m() -> None:
    client, fake = _client([fake_message(model=OPUS)])
    await _call(client, tier="ingest")
    (req,) = fake.requests
    assert req["output_config"]["effort"] == "high"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_timeout_is_capped_by_remaining_deadline() -> None:
    client, fake = _client([fake_message(model=HAIKU)])
    await _call(client, budget=_budget(deadline_s=10))
    assert fake.requests[0]["timeout"] <= 10


async def test_provider_cache_hit_is_priced_at_cache_read() -> None:
    client, _ = _client([fake_message(model=SONNET, input_tokens=50, cache_read=3000, output_tokens=10)])
    result = await _call(client, tier="reason")
    assert result.usage.cached == 3000
    assert result.call.cache_read == 3000
    expected = round((50 * 3.0 + 3000 * 0.3 + 10 * 15.0) / 1e6 * _settings().usd_eur, 4)
    assert result.usage.cost_eur == expected


async def test_cache_write_1h_costs_double_and_ttl_is_sent() -> None:
    client, fake = _client([fake_message(model=SONNET, cache_1h=2000, input_tokens=0, output_tokens=0)])
    result = await _call(client, tier="reason")
    assert result.call.cache_write == 2000
    assert result.usage.cost_eur == round(2000 * 6.0 / 1e6 * _settings().usd_eur, 4)  # 2× le prix d'entrée
    assert fake.requests[0]["system"][0]["cache_control"]["ttl"] == "1h"


async def test_exhausted_deadline_raises_timeout_without_calling() -> None:
    client, fake = _client([])
    budget = _budget(deadline_s=0)
    with pytest.raises(Timeout):
        await _call(client, budget=budget)
    assert fake.requests == [] and budget.attempts == 0


async def test_sdk_timeout_maps_to_timeout_and_counts_the_attempt() -> None:
    client, _ = _client([provider_exception(anthropic.APITimeoutError)])
    budget, step = _budget(), StepTrace(name="comprendre")
    with pytest.raises(Timeout):
        await _call(client, budget=budget, step=step)
    assert budget.attempts == 1
    # revue Codex B3 (AD-10) : l'appel en échec est tracé — modèle demandé, durée, usage nul.
    assert len(step.calls) == 1
    assert step.calls[0].model == HAIKU and step.calls[0].usage.cost_eur == 0.0


async def test_invalid_parse_then_ok_retries_once_with_motive() -> None:
    client, fake = _client([fake_message(text="pas du JSON", model=HAIKU),
                            fake_message(text='{"mot": "ok"}', model=HAIKU)])
    budget, step = _budget(), StepTrace(name="comprendre")
    result = await _call(client, budget=budget, step=step)
    assert result.parsed == Mot(mot="ok")
    assert budget.attempts == 2 and len(step.calls) == 2
    assert [c.name for c in step.checks] == ["parse_retry"]
    first, second = fake.requests
    assert second["system"] == first["system"]  # le préfixe reste byte-identique
    assert second["messages"][:1] == first["messages"]
    assert second["messages"][1] == {"role": "assistant", "content": "pas du JSON"}
    assert second["messages"][2]["role"] == "user" and "invalide" in second["messages"][2]["content"]


async def test_max_tokens_stop_reason_triggers_the_same_retry() -> None:
    client, fake = _client([fake_message(text='{"mot": "tronq', model=HAIKU, stop_reason="max_tokens"),
                            fake_message(model=HAIKU)])
    step = StepTrace(name="comprendre")
    result = await _call(client, step=step)
    assert result.parsed == Mot(mot="bonjour")
    assert "max_tokens" in step.checks[0].detail


async def test_invalid_parse_without_margin_fails_after_one_call() -> None:
    settings = _settings(llm_retry_margin_s=50.0)  # restant (≈30) < marge
    client, fake = _client([fake_message(text="pas du JSON", model=HAIKU)], settings)
    budget = _budget(deadline_s=30)
    with pytest.raises(LlmParse):
        await _call(client, budget=budget)
    assert budget.attempts == 1 and len(fake.requests) == 1


async def test_invalid_parse_without_remaining_attempts_fails_after_one_call() -> None:
    client, fake = _client([fake_message(text="pas du JSON", model=HAIKU)])
    budget = _budget(max_attempts=1)
    with pytest.raises(LlmParse):
        await _call(client, budget=budget)
    assert budget.attempts == 1 and len(fake.requests) == 1


async def test_attempts_cap_raises_budget_exceeded_without_calling() -> None:
    client, fake = _client([])
    budget = _budget(max_attempts=2)
    budget.attempts = 2
    with pytest.raises(BudgetExceeded, match="appels"):
        await _call(client, budget=budget)
    assert fake.requests == []


async def test_cost_cap_raises_budget_exceeded_without_calling() -> None:
    client, fake = _client([])
    budget = _budget(max_cost=0.10)
    budget.cost_eur = 0.09  # estimation (> 0,01 € avec 4096 tokens de sortie) + cumul > plafond
    with pytest.raises(BudgetExceeded, match="coût"):
        await _call(client, tier="reason", budget=budget)
    assert fake.requests == [] and budget.attempts == 0


async def test_cost_cap_blocks_a_realistic_reason_call_near_the_cap() -> None:
    # revue Codex 1.3 tour 2, B5 (reproduction) : 0,08 € déjà engagés, appel reason au préfixe du sommaire
    # guide (10 359 car., coût réel mesuré ≈ 0,03 € avec écriture 1 h) — l'estimation doit majorer ce réel
    # et bloquer l'appel avant qu'il parte (l'ancienne heuristique l'estimait 0,0134 € et le laissait passer).
    client, fake = _client([])
    budget = _budget(max_cost=0.10)
    budget.cost_eur = 0.08
    with pytest.raises(BudgetExceeded, match="coût"):
        await client.parse(tier="reason", system_prefix="x" * 10_359,
                           messages=[{"role": "user", "content": "q"}], output_model=Mot,
                           budget=budget, step=StepTrace(name="retrouver"), max_tokens=300)
    assert fake.requests == [] and budget.attempts == 0


async def test_expensive_call_succeeds_but_flags_cout_eleve() -> None:
    client, _ = _client([fake_message(model=OPUS, input_tokens=10_000, output_tokens=4_000)])
    step = StepTrace(name="ingest")
    result = await _call(client, tier="ingest", step=step, budget=_budget(max_cost=1.0))
    # 10000×5 + 4000×25 = 0.15 USD → 0.138 € > cost_alert_eur (0.05)
    assert result.usage.cost_eur > 0.05
    assert [c.name for c in step.checks] == ["cout_eleve"]
    assert step.checks[0].ok is False


async def test_cout_eleve_fires_once_on_the_cumulated_request_cost() -> None:
    # revue Codex I1 (AD-10) : deux appels chacun sous 0,05 € dont seul le cumul franchit le seuil —
    # le check est levé une seule fois, au franchissement.
    client, _ = _client([fake_message(model=OPUS, input_tokens=4_000, output_tokens=800),
                         fake_message(model=OPUS, input_tokens=4_000, output_tokens=800)])
    budget, step = _budget(max_cost=1.0), StepTrace(name="ingest")
    first = await _call(client, tier="ingest", budget=budget, step=step)
    assert first.usage.cost_eur < 0.05 and step.checks == []
    second = await _call(client, tier="ingest", budget=budget, step=step)
    assert second.usage.cost_eur < 0.05 and budget.cost_eur > 0.05
    assert [c.name for c in step.checks] == ["cout_eleve"]


@pytest.mark.parametrize("cls", [anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.OverloadedError,
                                 anthropic.InternalServerError, anthropic.ServiceUnavailableError,
                                 anthropic.DeadlineExceededError, anthropic.AuthenticationError,
                                 anthropic.PermissionDeniedError])
async def test_provider_errors_map_to_llm_unavailable(cls: type[Exception]) -> None:
    client, _ = _client([provider_exception(cls)])
    step = StepTrace(name="comprendre")
    with pytest.raises(LlmUnavailable) as exc_info:
        await _call(client, step=step)
    assert exc_info.value.code is ErrorCode.llm_unavailable
    assert cls.__name__ in exc_info.value.message
    assert len(step.calls) == 1 and step.calls[0].usage.cost_eur == 0.0  # revue Codex B3 : échec tracé


@pytest.mark.parametrize("cls", [anthropic.BadRequestError, anthropic.NotFoundError,
                                 anthropic.UnprocessableEntityError, anthropic.RequestTooLargeError,
                                 anthropic.ConflictError])
async def test_our_bad_requests_map_to_internal(cls: type[Exception]) -> None:
    client, _ = _client([provider_exception(cls)])
    step = StepTrace(name="comprendre")
    with pytest.raises(LlmUnavailable) as exc_info:
        await _call(client, step=step)
    assert exc_info.value.code is ErrorCode.internal
    assert cls.__name__ in exc_info.value.message and "req_test" in exc_info.value.message
    assert len(step.calls) == 1 and step.calls[0].model == HAIKU  # revue Codex B3 : échec tracé


async def test_unknown_status_error_maps_to_internal_and_others_are_reraised() -> None:
    class TeapotError(anthropic.APIStatusError):
        pass

    mapped = map_provider_error(provider_exception(TeapotError))
    assert isinstance(mapped, LlmUnavailable) and mapped.code is ErrorCode.internal
    with pytest.raises(ValueError):
        map_provider_error(ValueError("pas une erreur SDK"))


async def test_mapping_is_total_over_sdk_errors_and_never_leaks_the_body() -> None:
    # revue Codex B6 : APIResponseValidationError, RetryableError et toute erreur SDK résiduelle
    # restent dans l'enveloppe AD-16 — message = classe + request_id, jamais le corps fournisseur.
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    validation = anthropic.APIResponseValidationError(
        response=httpx2.Response(200, request=request), body={"error": "clé sk-fake-123 dans le corps"})
    mapped = map_provider_error(validation)
    assert isinstance(mapped, LlmUnavailable) and mapped.code is ErrorCode.internal
    assert "APIResponseValidationError" in mapped.message and "sk-fake-123" not in mapped.message

    retryable = map_provider_error(anthropic.RetryableError("à réessayer"))
    assert isinstance(retryable, LlmUnavailable) and retryable.code is ErrorCode.llm_unavailable

    class ResidualSdkError(anthropic.AnthropicError):
        pass

    residual = map_provider_error(ResidualSdkError("message fournisseur sensible"))
    assert isinstance(residual, LlmUnavailable) and residual.code is ErrorCode.internal
    assert residual.message == "ResidualSdkError (request_id=absent)"


async def test_error_message_is_only_class_and_request_id_never_secret_nor_body() -> None:
    # revue P4 : l'exception SDK simulée porte un secret et du texte utilisateur dans son message et son corps ;
    # rien de tout cela ne doit atteindre le message mappé.
    exc = provider_exception(anthropic.AuthenticationError,
                             message="invalid x-api-key sk-fake-123 pour la question « mon sinistre »",
                             body={"error": {"message": "clé sk-fake-123 rejetée"}})
    client, _ = _client([exc])
    with pytest.raises(LlmUnavailable) as exc_info:
        await _call(client)
    assert exc_info.value.message == "AuthenticationError (request_id=req_test)"
    assert "sk-fake-123" not in exc_info.value.message and "sinistre" not in exc_info.value.message


@pytest.mark.parametrize("stop_reason", ["tool_use", "pause_turn"])
async def test_tool_use_stop_reason_is_llm_parse_without_retry(stop_reason: str) -> None:
    # revue P9 : le dialogue d'outils (2.6) n'est pas supporté — rejouer la requête reproduirait le stop_reason.
    client, fake = _client([fake_message(text="", model=SONNET, stop_reason=stop_reason)])
    budget = _budget()
    with pytest.raises(LlmParse, match="outils"):
        await _call(client, tier="reason", budget=budget)
    assert len(fake.requests) == 1 and budget.attempts == 1


async def test_corrupt_evals_cache_entry_raises_llm_parse() -> None:
    # revue P10 : une entrée de cache invalide ne fait pas fuir une ValidationError brute.
    class BadCache:
        def get(self, key):
            return {"response": fake_message(text="pas du JSON", model=HAIKU), "cost_eur": 0.01}

        def set(self, key, value):  # pragma: no cover - jamais atteint
            raise AssertionError

    client, fake = _client([], cache=BadCache())
    with pytest.raises(LlmParse, match="cache d'évals"):
        await _call(client)
    assert fake.requests == []


async def test_refusal_raises_llm_parse_without_retry() -> None:
    client, fake = _client([fake_message(text="non", model=HAIKU, stop_reason="refusal")])
    budget = _budget()
    with pytest.raises(LlmParse, match="refus"):
        await _call(client, budget=budget)
    assert len(fake.requests) == 1 and budget.attempts == 1


async def test_evals_cache_hit_skips_the_call_and_costs_zero() -> None:
    cache = MemoryResponseCache()
    client, fake = _client([fake_message(model=HAIKU, input_tokens=1000, output_tokens=100)], cache=cache)
    budget, step = _budget(), StepTrace(name="comprendre")
    first = await _call(client, budget=budget, step=step)
    assert first.usage.cached_response is False and first.usage.cost_eur > 0

    budget2, step2 = _budget(), StepTrace(name="comprendre")
    second = await _call(client, budget=budget2, step=step2)
    assert len(fake.requests) == 1  # aucun second appel API
    assert budget2.attempts == 0 and budget2.cost_eur == 0.0
    assert second.usage.cached_response is True
    assert second.usage.cost_eur == 0.0
    assert second.usage.cost_eur_original == first.usage.cost_eur
    assert second.parsed == first.parsed
    assert len(step2.calls) == 1  # le hit reste visible dans la trace


async def test_evals_cache_key_depends_on_the_question() -> None:
    cache = MemoryResponseCache()
    client, fake = _client([fake_message(model=HAIKU), fake_message(model=HAIKU)], cache=cache)
    await client.parse(tier="micro", system_prefix="p", messages=[{"role": "user", "content": "q1"}],
                       output_model=Mot, budget=_budget(), step=StepTrace(name="s"))
    await client.parse(tier="micro", system_prefix="p", messages=[{"role": "user", "content": "q2"}],
                       output_model=Mot, budget=_budget(), step=StepTrace(name="s"))
    assert len(fake.requests) == 2


async def test_tools_are_passed_through_and_traced() -> None:
    tools = [{"name": "chercher", "description": "d", "input_schema": {"type": "object"}}]
    client, fake = _client([fake_message(model=SONNET)])
    result = await _call(client, tier="reason", tools=tools)
    assert fake.requests[0]["tools"] == tools
    assert result.call.tools == ["chercher"]


async def test_estimate_counts_tools_and_non_text_blocks() -> None:
    # revue P11 : les définitions d'outils et les blocs non-texte (tool_result) entrent dans l'estimation.
    from server.app.llm.pricing import estimate_cost

    s = _settings()
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r" * 4000}]}]
    tools = [{"name": "chercher", "description": "d" * 4000, "input_schema": {"type": "object"}}]
    bare = estimate_cost(HAIKU, "sys", [{"role": "user", "content": "q"}], 100, s)
    with_blocks = estimate_cost(HAIKU, "sys", messages, 100, s)
    with_tools = estimate_cost(HAIKU, "sys", messages, 100, s, tools=tools)
    assert bare < with_blocks < with_tools

    # et parse() transmet bien `tools` à l'estimation : le plafond tombe à cause des outils
    tools_lourds = [{"name": "chercher", "description": "d" * 400_000, "input_schema": {"type": "object"}}]
    client, fake = _client([])
    with pytest.raises(BudgetExceeded, match="coût"):
        await _call(client, tools=tools_lourds, budget=_budget(max_cost=0.002), max_tokens=10)
    assert fake.requests == []


async def test_explicit_max_tokens_overrides_the_setting() -> None:
    client, fake = _client([fake_message(model=HAIKU)])
    await _call(client, max_tokens=300)
    assert fake.requests[0]["max_tokens"] == 300


async def test_count_tokens_returns_the_api_count_with_timeout() -> None:
    fake = FakeAnthropic(token_counts=[3210])
    client = LlmClient(_settings(), anthropic_client=fake)
    assert await client.count_tokens(SONNET, None, [{"role": "user", "content": "x"}]) == 3210
    (req,) = fake.messages.count_requests
    assert req["timeout"] == _settings().count_tokens_timeout_s
    assert "system" not in req


async def test_count_tokens_maps_provider_errors() -> None:
    fake = FakeAnthropic()
    fake.messages.count_tokens = _raise_rate_limit  # type: ignore[method-assign]
    client = LlmClient(_settings(), anthropic_client=fake)
    with pytest.raises(LlmUnavailable):
        await client.count_tokens(SONNET, None, [{"role": "user", "content": "x"}])


async def _raise_rate_limit(**kwargs):
    raise provider_exception(anthropic.RateLimitError)
