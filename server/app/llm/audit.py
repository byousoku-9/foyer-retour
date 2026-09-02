"""Artefact contrôlé des enveloppes exactes vues par les LLM.

La projection publique ne contient jamais le corps : elle est dérivée du même événement et ne
publie que son identité, ses hashes et ses tailles.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                      separators=(",", ":")).encode("utf-8")


def _json_value(value: Any) -> Any:
    """Projette les objets SDK/doubles sans les aplatir en une chaîne opaque."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "to_dict"):
        return _json_value(value.to_dict())
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return {
            str(key): _json_value(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_") and not callable(item)
        }
    return str(value)


class ExactLlmAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_uid: str
    run_uid: str
    artifact_uid: str = ""
    step: str
    tier: str
    model: str
    cache_hit: bool = False
    trusted_line_uids: tuple[str, ...] = ()
    request: dict[str, Any]
    response: dict[str, Any] | None = None
    error_class: str | None = None

    def public_projection(self) -> dict[str, Any]:
        request = _canonical(self.request)
        response = _canonical(self.response) if self.response is not None else b""
        return {
            "call_uid": self.call_uid,
            "run_uid": self.run_uid,
            "artifact_uid": self.artifact_uid,
            "step": self.step,
            "tier": self.tier,
            "model": self.model,
            "cache_hit": self.cache_hit,
            "trusted_line_uids": list(self.trusted_line_uids),
            "request_sha256": hashlib.sha256(request).hexdigest(),
            "request_bytes": len(request),
            "response_sha256": hashlib.sha256(response).hexdigest(),
            "response_bytes": len(response),
            "error_class": self.error_class,
        }


class AuditSink(Protocol):
    def append(self, event: ExactLlmAuditEvent) -> dict[str, Any]: ...


class ProjectionAuditSink:
    """Sink en ligne sans état : calcule la projection sûre puis oublie l'enveloppe exacte."""

    persistent = False

    def append(self, event: ExactLlmAuditEvent) -> dict[str, Any]:
        return event.public_projection()


class MemoryAuditSink:
    persistent = False

    def __init__(self) -> None:
        self.events: list[ExactLlmAuditEvent] = []

    def append(self, event: ExactLlmAuditEvent) -> dict[str, Any]:
        self.events.append(event)
        return event.public_projection()


class JsonlAuditSink:
    """Sink exact hors ligne, permissions utilisateur, taille et rétention bornées.

    Le verrou porte sur un inode stable distinct du JSONL : une rotation ne permet donc jamais à
    deux processus d'écrire chacun dans une génération différente du même chemin.
    """

    persistent = True
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, path: Path, *, max_bytes: int = 16 * 1024 * 1024,
                 retention_files: int = 4) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes doit être positif")
        if retention_files < 1:
            raise ValueError("retention_files doit être positif")
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")
        self.max_bytes = max_bytes
        self.retention_files = retention_files
        key = str(path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.Lock())

    def append(self, event: ExactLlmAuditEvent) -> dict[str, Any]:
        payload = _canonical(event.model_dump(mode="json")) + b"\n"
        if len(payload) > self.max_bytes:
            raise ValueError(
                f"événement d'audit ({len(payload)} octets) > borne {self.max_bytes}")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_descriptor = os.open(
                self.lock_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.fchmod(lock_descriptor, 0o600)
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                current_size = self.path.stat().st_size if self.path.exists() else 0
                if current_size and current_size + len(payload) > self.max_bytes:
                    oldest = self.path.with_name(
                        f"{self.path.name}.{self.retention_files - 1}")
                    if self.retention_files > 1 and oldest.exists():
                        oldest.unlink()
                    for index in range(self.retention_files - 2, 0, -1):
                        source = self.path.with_name(f"{self.path.name}.{index}")
                        if source.exists():
                            os.replace(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
                    if self.retention_files > 1:
                        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
                    else:
                        self.path.unlink()
                descriptor = os.open(
                    self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                try:
                    os.fchmod(descriptor, 0o600)
                    view = memoryview(payload)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("écriture JSONL incomplète")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_descriptor)
        return event.public_projection()


def append_ingest_audit(path: Path, *, run_uid: str, step: str, model: str,
                        request: dict[str, Any], response: Any | None,
                        trusted_line_uids: tuple[str, ...] = (),
                        artifact_uid: str = "",
                        error_class: str | None = None,
                        max_bytes: int = 16 * 1024 * 1024,
                        retention_files: int = 4) -> dict[str, Any]:
    """Couture commune aux clients d'ingestion synchrones (Opus/T1/T2/arbitre)."""
    projected = _json_value(response)
    serialized_response = (projected if projected is None or isinstance(projected, dict)
                           else {"value": projected})
    event = ExactLlmAuditEvent(
        call_uid=f"call:{uuid.uuid4()}", run_uid=run_uid, artifact_uid=artifact_uid,
        step=step, tier="ingest",
        model=model, request=request, response=serialized_response,
        trusted_line_uids=trusted_line_uids, error_class=error_class,
    )
    return JsonlAuditSink(
        path, max_bytes=max_bytes, retention_files=retention_files,
    ).append(event)
