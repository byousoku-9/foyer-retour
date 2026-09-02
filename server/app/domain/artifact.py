"""Identité publique unique d'un artefact documentaire, partagée par tous les étages."""

from __future__ import annotations

import hashlib
import json


def document_artifact_uid(*, document_uid: str, source_hash: str | None,
                          ingest_fingerprint: str | None = None) -> str:
    """Retourne l'identité de contenu disponible avant comme après parsing.

    ``ingest_fingerprint`` reste accepté pour rendre explicite la provenance de l'appelant, mais
    n'entre pas dans l'identité : la structure est précisément une entrée de cette empreinte et ne
    pourrait sinon jamais être corrélée au document qu'elle contribue à construire.
    """
    del ingest_fingerprint
    raw = json.dumps(
        {"document_uid": document_uid, "source_hash": source_hash or ""},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "artifact-v1:" + hashlib.sha256(raw).hexdigest()
