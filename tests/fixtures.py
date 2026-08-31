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
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ALIAS_FIELDS = frozenset({"request", "target", "certificate"})
_CERTIFICATE_FIELDS = frozenset({
    "version", "model", "system_sha256", "messages_sha256", "tools_sha256", "schema_sha256",
})


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


def _digest_canonique(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=_json_default,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def request_certificate(model: str, messages: Any, **params: Any) -> dict[str, Any]:
    """Empreinte auditable des dimensions dont une réponse enregistrée dépend."""
    output_config = params.get("output_config")
    schema = {
        "output_config_format": (
            output_config.get("format") if isinstance(output_config, dict) else None
        ),
        "output_format": params.get("output_format"),
    }
    return {
        "version": 2,
        "model": model,
        "system_sha256": _digest_canonique(params.get("system", [])),
        "messages_sha256": _digest_canonique(messages),
        "tools_sha256": _digest_canonique(params.get("tools", [])),
        "schema_sha256": _digest_canonique(schema),
    }


def _request_model(key: str) -> str | None:
    request = key.removeprefix("count:")
    model, separator, digest = request.rpartition(":")
    return model if separator and re.fullmatch(r"[0-9a-f]{16}", digest) else None


def _validate_certificate(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CERTIFICATE_FIELDS:
        raise FixtureMissing(f"{context} : certificat de requête invalide")
    if (type(value["version"]) is not int or value["version"] != 2
            or not isinstance(value["model"], str) or not value["model"]):
        raise FixtureMissing(f"{context} : certificat de requête invalide")
    if any(not isinstance(value[field], str) or not _DIGEST.fullmatch(value[field])
           for field in ("system_sha256", "messages_sha256", "tools_sha256", "schema_sha256")):
        raise FixtureMissing(f"{context} : empreinte canonique invalide")
    return value


def _validate_entries(entries: dict[str, Any], path: Path) -> None:
    reserved = [key for key in entries if key.startswith("__") and key != "__aliases__"]
    if reserved:
        raise FixtureMissing(f"fixture corrompue {path} : clé réservée {reserved[0]!r}")
    aliases = entries.get("__aliases__", {})
    if not isinstance(aliases, dict):
        raise FixtureMissing(f"fixture corrompue {path} : __aliases__ doit être un objet")
    for source, alias in aliases.items():
        if not isinstance(source, str) or source.startswith("__"):
            raise FixtureMissing(f"fixture corrompue {path} : clé d'alias réservée")
        if source in entries:
            raise FixtureMissing(f"fixture corrompue {path} : alias et réponse en conflit pour {source!r}")
        if not isinstance(alias, dict) or set(alias) != _ALIAS_FIELDS:
            raise FixtureMissing(f"fixture corrompue {path} : alias {source!r} invalide")
        if alias["request"] != source:
            raise FixtureMissing(f"fixture corrompue {path} : request incohérente pour {source!r}")
        target = alias["target"]
        if not isinstance(target, str) or target.startswith("__"):
            raise FixtureMissing(f"fixture corrompue {path} : cible réservée pour {source!r}")
        seen = {source}
        cursor = target
        while cursor in aliases:
            if cursor in seen:
                raise FixtureMissing(f"fixture corrompue {path} : cycle d'alias depuis {source!r}")
            seen.add(cursor)
            chained = aliases[cursor]
            cursor = chained.get("target") if isinstance(chained, dict) else None
        if len(seen) > 1:
            raise FixtureMissing(f"fixture corrompue {path} : alias en chaîne depuis {source!r}")
        entry = entries.get(target)
        if not isinstance(entry, dict):
            raise FixtureMissing(f"fixture corrompue {path} : cible ordinaire absente pour {source!r}")
        if entry.get("request") != target:
            raise FixtureMissing(f"fixture corrompue {path} : request incohérente pour {target!r}")
        if "response" not in entry:
            raise FixtureMissing(f"fixture corrompue {path} : response absente pour {target!r}")
        certificate = _validate_certificate(alias["certificate"], context=f"alias {source!r}")
        if _validate_certificate(entry.get("certificate"), context=f"cible {target!r}") != certificate:
            raise FixtureMissing(f"fixture corrompue {path} : certificats incompatibles pour {source!r}")
        source_model = _request_model(source)
        target_model = _request_model(target)
        if source_model != target_model or source_model != certificate["model"]:
            raise FixtureMissing(f"fixture corrompue {path} : alias inter-modèle interdit pour {source!r}")


def _model_name(model: type[BaseModel]) -> str:
    return f"{model.__module__}.{model.__qualname__}"


def _serialize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return {"__model__": _model_name(type(value)), "data": value.model_dump(mode="json")}
    return json.loads(json.dumps(value, default=_json_default))


def _modele_expose(value: Any) -> tuple[bool, Any]:
    if isinstance(value, BaseModel) and "model" in type(value).model_fields:
        return True, value.model
    if isinstance(value, dict):
        if "model" in value:
            return True, value["model"]
        data = value.get("data")
        if "__model__" in value and isinstance(data, dict) and "model" in data:
            return True, data["model"]
    return False, None


def _valider_modele_reponse(value: Any, request: dict[str, Any] | None, *, context: str) -> None:
    expose, response_model = _modele_expose(value)
    if not expose:
        return
    certificate = _validate_certificate(request, context=f"requête {context!r}")
    if response_model != certificate["model"]:
        raise FixtureMissing(
            f"fixture : réponse enregistrée pour {response_model!r}, "
            f"certificat pour {certificate['model']!r}"
        )


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
                _validate_entries(self._entries, self.path)
            except ValueError as exc:
                raise FixtureMissing(f"fixture corrompue {self.path} : {exc} — la supprimer et ré-enregistrer") from exc
        self.calls_made = 0

    @property
    def recording(self) -> bool:
        return bool(self.api_key)

    async def call(self, key: str, fn: Callable[[], Awaitable[T]], model: type[BaseModel] | None = None,
                   request: dict[str, Any] | None = None) -> Any:
        if self.recording:
            result = await fn()
            _valider_modele_reponse(result, request, context=key)
            self.calls_made += 1
            self._entries[key] = {"request": key, "response": _serialize(result)}
            if request is not None:
                self._entries[key]["certificate"] = request
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._entries, indent=2, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
            return result
        requested = key
        aliases = self._entries.get("__aliases__", {})
        if key not in self._entries and key in aliases:
            alias = aliases[key]
            if request is None or request != alias["certificate"]:
                raise FixtureMissing(
                    f"fixture {self.path} : certificat de la requête {requested!r} absent ou "
                    f"incohérent (observé={json.dumps(request, sort_keys=True)})"
                )
            key = alias["target"]
        if key not in self._entries:
            raise FixtureMissing(
                f"fixture absente pour {requested!r} dans {self.path} et ANTHROPIC_API_KEY vide : "
                "lancer le test avec la clé pour l'enregistrer"
            )
        response = self._entries[key].get("response")
        _valider_modele_reponse(response, request, context=requested)
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
