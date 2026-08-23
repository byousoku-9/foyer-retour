"""Artefacts d'ingestion partagés par les ingestions (AD-7) : `document.json`, écriture atomique, manifest.

Paramétré par `doc_id` : `kb_to_blocks` (guide) et `pdf_to_blocks` (contrat) écrivent les mêmes fichiers avec les
mêmes règles. `SCHEMA_VERSION` entre dans chaque `ingest_fingerprint` : tout changement de sérialisation l'incrémente.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from server.app.domain import Document, ManifestEntry

# "2" (story 1.2) : `exclude_defaults` — les valeurs par défaut sont rétablies par le modèle au chargement (reprise 1.1).
SCHEMA_VERSION = "2"


def document_json(doc: Document) -> str:
    """`text_norm` n'est jamais écrit (recalculé au chargement) ; les valeurs par défaut non plus."""
    data = doc.model_dump(exclude_defaults=True, exclude={"blocks": {"__all__": {"text_norm"}}})
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


OVERLAY_FILE = "typing.manual.json"


def overlay_hash(doc_dir: Path) -> str | None:
    """sha256 de `typing.manual.json` s'il existe (écrit dans le manifest, vérifié par le loader), sinon None."""
    path = doc_dir / OVERLAY_FILE
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def load_previous(path: Path) -> Document | None:
    """`document.json` précédent, pour `ids_disparus` ; illisible ou invalide ⇒ comme absent."""
    if not path.is_file():
        return None
    try:
        return Document.model_validate_json(path.read_bytes())
    except (ValidationError, ValueError, OSError):
        return None


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)


def read_manifest(manifest_path: Path) -> dict[str, Any]:
    """Lu et validé avant toute écriture : un manifest illisible bloque sans rien modifier sur disque."""
    raw: dict[str, Any] = json.loads(manifest_path.read_text("utf-8")) if manifest_path.is_file() else {}
    if not isinstance(raw, dict):
        raise ValueError(f"{manifest_path} : un objet JSON {{doc_id: entrée}} est attendu")
    return raw


def merge_manifest(manifest_path: Path, raw: dict[str, Any], doc_id: str, entry: ManifestEntry) -> ManifestEntry:
    """Fusionne l'entrée `doc_id` ; les autres documents sont conservés tels quels, même invalides (avertissement)."""
    adapter = TypeAdapter(ManifestEntry)
    previous = raw.get(doc_id)
    gate = None
    if previous:
        try:
            gate = adapter.validate_python(previous).gate  # jamais écrit par l'ingestion (AD-7) : conservé
        except ValidationError as exc:
            print(f"avertissement : entrée {doc_id} précédente invalide, gate ignoré ({exc.errors()[0].get('msg', '')})",
                  file=sys.stderr)
    for other, value in raw.items():
        if other == doc_id:
            continue
        try:
            adapter.validate_python(value)
        except ValidationError:
            print(f"avertissement : entrée {other!r} du manifest invalide, conservée telle quelle", file=sys.stderr)
    entry = entry.model_copy(update={"gate": gate})
    raw[doc_id] = entry.model_dump()
    write_atomic(manifest_path, json.dumps(dict(sorted(raw.items())), indent=2, ensure_ascii=False) + "\n")
    return entry
