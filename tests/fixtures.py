"""Record / replay générique des appels LLM pour des tests sans réseau.

- `ANTHROPIC_API_KEY` non vide dans l'environnement (ou, si la variable est **absente**, dans `.env`
  via `get_settings()`) ⇒ l'appel réel est exécuté et sa réponse sérialisée dans
  `tests/llm_fixtures/{module}.{test}.json` (sans la clé, messages hashés).
- variable d'environnement **vide** ⇒ replay forcé, même si `.env` porte une clé : le gate hors
  ligne `ANTHROPIC_API_KEY= uv run pytest -q` est hermétique (AC 1.3, revue Codex B2).
- clé absente partout ⇒ replay ; fixture absente, corrompue ou d'un autre modèle que celui
  demandé ⇒ `FixtureMissing`.

L'utilitaire ne connaît pas le client LLM (story 1.3) : il enveloppe une coroutine `call()`
et un `key` qui identifie la requête (modèle + hash des messages).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

FIXTURES_DIR = Path(__file__).resolve().parent / "llm_fixtures"
T = TypeVar("T")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class FixtureMissing(RuntimeError):
    pass


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return str(value)


def fixture_name(nodeid_or_name: str) -> str:
    """`tests/test_x.py::TestA::test_y[cas]` → `test_x.TestA.test_y_cas_` (jamais de `/`)."""
    name = nodeid_or_name.split("::", 1)
    module = Path(name[0]).stem if len(name) == 2 else ""
    rest = name[-1].replace("::", ".")
    full = f"{module}.{rest}" if module else rest
    return _UNSAFE.sub("_", full)


def request_key(model: str, messages: Any, **params: Any) -> str:
    """Clé stable d'une requête : modèle + hash des messages et paramètres (jamais leur texte)."""
    payload = json.dumps({"model": model, "messages": messages, "params": params},
                         sort_keys=True, ensure_ascii=False, default=_json_default)
    return f"{model}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _model_name(model: type[BaseModel]) -> str:
    return f"{model.__module__}.{model.__qualname__}"


def _serialize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return {"__model__": _model_name(type(value)), "data": value.model_dump(mode="json")}
    return json.loads(json.dumps(value, default=_json_default))


class LLMRecorder:
    def __init__(self, test_name: str, fixtures_dir: Path = FIXTURES_DIR, api_key: str | None = None) -> None:
        self.path = fixtures_dir / f"{fixture_name(test_name)}.json"
        if api_key is None:
            if "ANTHROPIC_API_KEY" in os.environ:
                # La variable explicite fait foi — vide ⇒ replay forcé, sans repli vers `.env`
                # (Settings ignore les variables vides via `env_ignore_empty`).
                api_key = os.environ["ANTHROPIC_API_KEY"]
            else:
                from server.app.config import get_settings

                api_key = get_settings().anthropic_api_key
        self.api_key = api_key
        self._entries: dict[str, Any] = {}
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text("utf-8"))
                if not isinstance(self._entries, dict):
                    raise ValueError("la fixture doit être un objet JSON")
            except ValueError as exc:
                raise FixtureMissing(f"fixture corrompue {self.path} : {exc} — la supprimer et ré-enregistrer") from exc
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
        requested = key
        aliases = self._entries.get("__aliases__", {})
        if key not in self._entries and isinstance(aliases, dict):
            target = aliases.get(key)
            if isinstance(target, str):
                key = target
        if key not in self._entries:
            raise FixtureMissing(
                f"fixture absente pour {requested!r} dans {self.path} et ANTHROPIC_API_KEY vide : "
                "lancer le test avec la clé pour l'enregistrer"
            )
        response = self._entries[key].get("response")
        if isinstance(response, dict) and "__model__" in response:
            if model is not None:
                if response["__model__"] != _model_name(model):
                    raise FixtureMissing(
                        f"fixture {self.path} enregistrée pour {response['__model__']}, "
                        f"{_model_name(model)} demandé : la ré-enregistrer"
                    )
                return model.model_validate(response["data"])
            return response["data"]
        return response
