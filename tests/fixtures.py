"""Record / replay générique des appels LLM pour des tests sans réseau.

- `ANTHROPIC_API_KEY` présente ⇒ l'appel réel est exécuté et sa réponse sérialisée dans
  `tests/llm_fixtures/{test_name}.json` (sans la clé, messages hashés).
- clé absente ⇒ la réponse est rejouée depuis la fixture ; fixture absente ⇒ `FixtureMissing`.

L'utilitaire ne connaît pas le client LLM (story 1.3) : il enveloppe une coroutine `call()`
et un `key` qui identifie la requête (modèle + hash des messages).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

FIXTURES_DIR = Path(__file__).resolve().parent / "llm_fixtures"
T = TypeVar("T")


class FixtureMissing(RuntimeError):
    pass


def request_key(model: str, messages: Any, **params: Any) -> str:
    """Clé stable d'une requête : modèle + hash des messages et paramètres (jamais leur texte)."""
    payload = json.dumps({"model": model, "messages": messages, "params": params}, sort_keys=True, ensure_ascii=False)
    return f"{model}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _serialize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return {"__model__": f"{type(value).__module__}.{type(value).__qualname__}", "data": value.model_dump(mode="json")}
    return value


class LLMRecorder:
    def __init__(self, test_name: str, fixtures_dir: Path = FIXTURES_DIR, api_key: str | None = None) -> None:
        self.path = fixtures_dir / f"{test_name}.json"
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "") if api_key is None else api_key
        self._entries: dict[str, Any] = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        self.calls_made = 0

    @property
    def recording(self) -> bool:
        return bool(self.api_key)

    async def call(self, key: str, fn: Callable[[], Awaitable[T]], model: type[BaseModel] | None = None) -> Any:
        if self.recording:
            result = await fn()
            self.calls_made += 1
            self._entries[key] = {"request": key, "response": _serialize(result)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._entries, indent=2, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
            return result
        if key not in self._entries:
            raise FixtureMissing(
                f"fixture absente pour {key!r} dans {self.path} et ANTHROPIC_API_KEY vide : "
                "lancer le test avec la clé pour l'enregistrer"
            )
        response = self._entries[key]["response"]
        if isinstance(response, dict) and "__model__" in response:
            if model is not None:
                return model.model_validate(response["data"])
            return response["data"]
        return response
