"""Faux client SDK pour tester `LlmClient` sans réseau.

- `FakeAnthropic(script)` rejoue des réponses scriptées (dicts → `anthropic.types.Message`) ou lève
  des exceptions SDK préparées par `provider_exception` ; chaque requête envoyée est enregistrée.
  La surface simulée est `messages.parse` (le transport du client) et `messages.count_tokens`.
- `RecordedAnthropic(recorder)` relie `LlmClient` au record/replay de `tests/fixtures.py` : avec la
  clé, l'appel réel est exécuté et sa réponse brute (`Message.to_dict()`) enregistrée ; sans clé,
  elle est rejouée et revalidée exactement comme le ferait le SDK.
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
from anthropic.types import Message, MessageTokensCount

from server.app.config import get_settings
from tests.fixtures import LLMRecorder, request_certificate, request_key


def fake_message(
    text: str = '{"mot": "bonjour"}',
    *,
    content: list[dict[str, Any]] | None = None,
    model: str = "claude-sonnet-5",
    stop_reason: str = "end_turn",
    input_tokens: int = 1000,
    cache_read: int = 0,
    cache_5m: int = 0,
    cache_1h: int = 0,
    output_tokens: int = 100,
) -> dict[str, Any]:
    """Réponse brute de l'API telle que le SDK la désérialise (`Message.model_validate`)."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content if content is not None else [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_5m + cache_1h,
            "cache_creation": {
                "ephemeral_5m_input_tokens": cache_5m,
                "ephemeral_1h_input_tokens": cache_1h,
            },
        },
    }


def provider_exception(cls: type[Exception], request_id: str | None = "req_test",
                       message: str | None = None, body: object | None = None) -> Exception:
    """Instancie une exception du SDK comme le client HTTP l'aurait fait."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    if issubclass(cls, anthropic.APIStatusError):
        status = getattr(cls, "status_code", 599)
        if not isinstance(status, int):
            status = 599
        headers = {"request-id": request_id} if request_id else {}
        response = httpx2.Response(status, request=request, headers=headers)
        return cls(message or f"{cls.__name__} simulée", response=response, body=body)
    if cls is anthropic.APITimeoutError:
        return cls(request=request)
    if issubclass(cls, anthropic.APIConnectionError):
        return cls(request=request)
    raise TypeError(f"classe non gérée : {cls}")


class _FakeMessages:
    def __init__(self, script: list[Any], token_counts: list[int]) -> None:
        self._script = list(script)
        self._token_counts = list(token_counts)
        self.requests: list[dict[str, Any]] = []
        self.count_requests: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Message:
        self.requests.append(kwargs)
        if not self._script:
            raise AssertionError("script épuisé : appel API non prévu par le test")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return Message.model_validate(item)

    async def create(self, **kwargs: Any) -> Message:
        return await self.parse(**kwargs)

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.count_requests.append(kwargs)
        if not self._token_counts:
            raise AssertionError("token_counts épuisé : comptage non prévu par le test")
        return MessageTokensCount(input_tokens=self._token_counts.pop(0))


class FakeAnthropic:
    """Assez du SDK pour `LlmClient` : `messages.parse` et `messages.count_tokens`."""

    def __init__(self, script: list[Any] | None = None, token_counts: list[int] | None = None) -> None:
        self.messages = _FakeMessages(script or [], token_counts or [])

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.messages.requests

    @property
    def remaining_script(self) -> int:
        return len(self.messages._script)


def _key_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Paramètres identifiants d'une requête — sans `timeout` (dépend de l'horloge, pas de la requête)."""
    return {k: v for k, v in kwargs.items() if k != "timeout" and k != "messages" and k != "model"}


class _RecordedMessages:
    def __init__(self, recorder: LLMRecorder) -> None:
        self._recorder = recorder
        self._real: Any = None

    def _real_client(self) -> Any:
        if self._real is None:
            self._real = anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key, max_retries=0)
        return self._real

    async def parse(self, **kwargs: Any) -> Message:
        params = _key_params(kwargs)
        key = request_key(kwargs["model"], kwargs["messages"], **params)
        certificate = request_certificate(kwargs["model"], kwargs["messages"], **params)

        async def fn() -> dict[str, Any]:
            # `output_format` volontairement absent : le corps est déjà complet (`output_config.format`)
            # et le SDK ne doit pas valider avant de rendre la réponse (voir client.py).
            message = await self._real_client().messages.parse(**kwargs)
            return message.to_dict()

        return Message.model_validate(await self._recorder.call(key, fn, request=certificate))

    async def create(self, **kwargs: Any) -> Message:
        params = _key_params(kwargs)
        key = request_key(kwargs["model"], kwargs["messages"], **params)
        certificate = request_certificate(kwargs["model"], kwargs["messages"], **params)

        async def fn() -> dict[str, Any]:
            message = await self._real_client().messages.create(**kwargs)
            return message.to_dict()

        return Message.model_validate(await self._recorder.call(key, fn, request=certificate))

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        params = _key_params(kwargs)
        key = "count:" + request_key(kwargs["model"], kwargs["messages"], **params)
        certificate = request_certificate(kwargs["model"], kwargs["messages"], **params)

        async def fn() -> dict[str, Any]:
            counted = await self._real_client().messages.count_tokens(**kwargs)
            return counted.to_dict()

        return MessageTokensCount.model_validate(await self._recorder.call(
            key, fn, request=certificate,
        ))


class RecordedAnthropic:
    """Client SDK branché sur le record/replay des fixtures (`tests/llm_fixtures/`)."""

    def __init__(self, recorder: LLMRecorder) -> None:
        self.messages = _RecordedMessages(recorder)
