"""Identité canonique commune des lignes PDF, du parsing aux consommateurs aval."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence


def line_uid(*, document_uid: str, page: int, source_uids: Sequence[str],
             bbox: Sequence[float], order: int, text: str) -> str:
    """Rend l'identité content-addressée d'une ligne portée, sans dépendre de son bloc."""
    payload = json.dumps({
        "version": 1,
        "document_uid": document_uid,
        "page": page,
        "source_uids": list(source_uids),
        "bbox": [round(value, 4) for value in bbox],
        "order": order,
        "text": text,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "line-v1:" + hashlib.sha256(payload).hexdigest()
