"""AD-7 — Chargement en lecture seule de `data/` : manifest, hashes recalculés, quarantaine par document.

`corpus` n'importe que `domain` et la stdlib ; `allow_ungated` est passé par l'appelant (depuis `config.py`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from server.app.domain import Document, Manifest, ManifestEntry

from .text import normalize

SOURCE_FILES = ("source.js", "source.pdf")  # la première présente est comparée à `manifest.source_hash`


@dataclass
class Corpus:
    documents: dict[str, Document] = field(default_factory=dict)
    manifest: Manifest = field(default_factory=dict)
    summaries: dict[str, str] = field(default_factory=dict)
    quarantine: dict[str, str] = field(default_factory=dict)  # doc_id → raison
    alerts: dict[str, list[str]] = field(default_factory=dict)  # doc_id → alertes (document servi)

    @property
    def served(self) -> list[str]:
        return sorted(self.documents)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    return str(errors[0].get("msg", "")) if errors else str(exc)


def _load_one(doc_dir: Path, doc_id: str, entry: ManifestEntry, *,
              allow_ungated: bool) -> tuple[Document | None, str, list[str]]:
    """Renvoie (document | None, raison de quarantaine, alertes). Aucune exception ne sort : tout devient une raison."""
    if entry.status == "quarantaine":
        return None, "quarantaine (manifest)", []
    if doc_dir.name != doc_id:
        return None, f"dossier {doc_dir.name!r} différent du doc_id", []
    doc_path = doc_dir / "document.json"
    if not doc_path.is_file():
        return None, "document.json absent", []
    try:
        if _sha256(doc_path) != entry.document_hash:
            return None, "document_hash différent du manifest", []
        source_found = False
        for name in SOURCE_FILES:
            src = doc_dir / name
            if src.is_file():
                source_found = True
                if _sha256(src) != entry.source_hash:
                    return None, f"source_hash différent du manifest ({name})", []
                break
        doc = Document.model_validate_json(doc_path.read_bytes())
    except ValidationError as exc:
        return None, f"document.json invalide : {_first_error(exc)}", []
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"document.json illisible : {type(exc).__name__}: {exc}"[:500], []
    if doc.doc_id != doc_id:
        return None, f"doc_id {doc.doc_id!r} différent de la clé du manifest", []
    if doc.source_hash != entry.source_hash:
        return None, "source_hash du document différent du manifest", []
    if doc.edition != entry.edition:
        return None, f"edition {doc.edition!r} différente du manifest ({entry.edition!r})", []
    summary_path = doc_dir / "summary.md"
    if not summary_path.is_file():
        return None, "sommaire_absent", []
    alerts: list[str] = []
    if entry.gate is None:
        if not allow_ungated:
            return None, "sans_gate", []
        alerts.append("sans_gate")
    elif (entry.gate.source_hash, entry.gate.ingest_fingerprint) != (entry.source_hash, entry.ingest_fingerprint):
        alerts.append("gate_perime")
    if not source_found:
        alerts.append("source_absente")
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    return doc, "", alerts


def load_corpus(data_dir: Path | str, *, allow_ungated: bool) -> Corpus:
    """Charge chaque document du manifest ; une incohérence met ce seul document en quarantaine (AD-7)."""
    data_dir = Path(data_dir)
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        return Corpus()
    try:
        manifest = TypeAdapter(Manifest).validate_json(manifest_path.read_bytes())
    except ValidationError as exc:
        return Corpus(quarantine={"*": f"manifest invalide : {_first_error(exc)}"})
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return Corpus(quarantine={"*": f"manifest invalide : {type(exc).__name__}: {exc}"[:500]})
    corpus = Corpus(manifest=manifest)
    for doc_id, entry in sorted(manifest.items()):
        doc, reason, alerts = _load_one(data_dir / doc_id, doc_id, entry, allow_ungated=allow_ungated)
        if doc is None:
            corpus.quarantine[doc_id] = reason
            continue
        try:
            summary = (data_dir / doc_id / "summary.md").read_text("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            corpus.quarantine[doc_id] = f"summary.md illisible : {type(exc).__name__}: {exc}"[:500]
            continue
        corpus.documents[doc_id] = doc
        corpus.summaries[doc_id] = summary
        corpus.alerts[doc_id] = alerts
    return corpus
