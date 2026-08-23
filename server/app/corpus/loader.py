"""AD-7 — Chargement en lecture seule de `data/` : manifest, hashes recalculés, quarantaine par document.

`corpus` n'importe que `domain` et la stdlib ; `allow_ungated` est passé par l'appelant (depuis `config.py`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from server.app.domain import Document, GateContext, Manifest, ManifestEntry

from .text import normalize

SOURCE_FILES = ("source.js", "source.pdf")  # la première présente est comparée à `manifest.source_hash`
OVERLAY_FILE = "typing.manual.json"  # typage manuel (FR20) fusionné avant validation ; `document.json` intact
OVERLAY_FIELDS = ("kind", "defines", "scope_node_id", "kind_source")


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


def _first_error(exc: ValueError) -> str:
    """Premier message d'une `ValidationError` (qui hérite de ValueError) sans importer pydantic dans `corpus`."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        items = errors()
        if items:
            return str(items[0].get("msg", ""))
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _gate_alerts(entry: ManifestEntry, current: GateContext | None, *, allow_ungated: bool) -> tuple[str, list[str]]:
    """Règle du gate (AD-7) : (raison de quarantaine, alertes).

    - pas de gate, ou gate dont `source_hash`/`ingest_fingerprint` ≠ l'entrée ⇒ gate invalide : `sans_gate`
      (quarantaine, sauf `allow_ungated` ⇒ alerte) ;
    - `evals_ok=False` ⇒ `gate_echoue`, jamais servi ;
    - `pipeline_digest`/`prompts_digest`/`model_ids` ≠ `current` (si fourni) ⇒ servi avec l'alerte `gate_perime`.
    """
    gate = entry.gate
    if gate is not None and (gate.source_hash, gate.ingest_fingerprint) != (entry.source_hash, entry.ingest_fingerprint):
        gate = None
    if gate is None:
        return ("", ["sans_gate"]) if allow_ungated else ("sans_gate", [])
    if not gate.evals_ok:
        return "gate_echoue", []
    if current is not None and (gate.pipeline_digest, gate.prompts_digest, gate.model_ids) != (
            current.pipeline_digest, current.prompts_digest, current.model_ids):
        return "", ["gate_perime"]
    return "", []


def _apply_overlay(raw_doc: dict, overlay: object) -> str:
    """Fusionne `typing.manual.json` sur le dict brut de `document.json` ; renvoie une raison de quarantaine ou ""."""
    if not isinstance(overlay, dict) or not isinstance(overlay.get("blocks"), dict):
        return "overlay : objet {blocks: {block_id: {kind, …}}} attendu"
    if overlay.get("doc_id") not in (None, raw_doc.get("doc_id")):
        return f"overlay : doc_id {overlay.get('doc_id')!r} différent du document"
    blocks = {b.get("block_id"): b for b in raw_doc.get("blocks", []) if isinstance(b, dict)}
    node_ids = {n.get("node_id") for n in raw_doc.get("nodes", []) if isinstance(n, dict)}
    for block_id, entry in overlay["blocks"].items():
        if block_id not in blocks:
            return f"overlay : bloc inconnu {block_id}"
        if not isinstance(entry, dict) or entry.get("kind_source") != "manual":
            return f"overlay : kind_source ≠ manual pour {block_id}"
        unknown = sorted(set(entry) - set(OVERLAY_FIELDS))
        if unknown:
            return f"overlay : champs inattendus pour {block_id} : {unknown}"
        scope = entry.get("scope_node_id")
        if scope is not None and scope not in node_ids:
            return f"overlay : nœud inconnu {scope} pour {block_id}"
        blocks[block_id].update({k: v for k, v in entry.items()})
    return ""


def _load_one(doc_dir: Path, doc_id: str, entry: ManifestEntry, *, allow_ungated: bool,
              current: GateContext | None) -> tuple[Document | None, str, list[str]]:
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
        raw_doc = json.loads(doc_path.read_bytes())
        overlay_path = doc_dir / OVERLAY_FILE
        if overlay_path.is_file():
            try:
                overlay = json.loads(overlay_path.read_bytes())
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                return None, f"overlay illisible : {_first_error(exc)}"[:500], []
            reason = _apply_overlay(raw_doc, overlay) if isinstance(raw_doc, dict) else ""
            if reason:
                return None, reason, []
        doc = Document.model_validate(raw_doc)
    except ValueError as exc:  # ValidationError et JSONDecodeError en héritent
        return None, f"document.json invalide : {_first_error(exc)}", []
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"document.json illisible : {type(exc).__name__}: {exc}"[:500], []
    if doc.doc_id != doc_id:
        return None, f"doc_id {doc.doc_id!r} différent de la clé du manifest", []
    if doc.source_hash != entry.source_hash:
        return None, "source_hash du document différent du manifest", []
    if doc.ingest_fingerprint != entry.ingest_fingerprint:
        return None, "ingest_fingerprint du document différent du manifest", []
    if doc.edition != entry.edition:
        return None, f"edition {doc.edition!r} différente du manifest ({entry.edition!r})", []
    if not (doc_dir / "summary.md").is_file():
        return None, "sommaire_absent", []
    reason, alerts = _gate_alerts(entry, current, allow_ungated=allow_ungated)
    if reason:
        return None, reason, []
    if not source_found:
        alerts.append("source_absente")
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    return doc, "", alerts


def load_corpus(data_dir: Path | str, *, allow_ungated: bool, current: GateContext | None = None) -> Corpus:
    """Charge chaque document du manifest ; une incohérence met ce seul document en quarantaine (AD-7).

    `current` décrit l'image en cours (digests, modèles) ; sans lui, la péremption du gate n'est pas évaluée.
    """
    data_dir = Path(data_dir)
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        return Corpus()
    try:
        raw = json.loads(manifest_path.read_bytes())
        if not isinstance(raw, dict):
            raise ValueError("un objet JSON {doc_id: entrée} est attendu")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return Corpus(quarantine={"*": f"manifest invalide : {_first_error(exc)}"[:500]})
    corpus = Corpus()
    for doc_id in sorted(raw):
        try:
            entry = ManifestEntry.model_validate(raw[doc_id])
        except ValueError as exc:
            corpus.quarantine[doc_id] = f"entrée de manifest invalide : {_first_error(exc)}"
            continue
        corpus.manifest[doc_id] = entry
        doc, reason, alerts = _load_one(data_dir / doc_id, doc_id, entry, allow_ungated=allow_ungated, current=current)
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
