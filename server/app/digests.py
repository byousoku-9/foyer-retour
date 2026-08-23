"""Définitions partagées : empreintes du code du pipeline, des prompts et du golden set.

Un digest est un SHA-256 hex sur la liste triée des chemins relatifs et de leur contenu.
Un dossier absent donne le hash de la liste vide (jamais d'exception).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = APP_DIR / "prompts"
_EXCLUDED_PARTS = {"__pycache__", "prompts", ".pytest_cache"}


def _iter_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix in suffixes and not (set(p.relative_to(root).parts) & _EXCLUDED_PARTS)
    )


def digest_paths(paths: Iterable[Path], root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(Path(p) for p in paths):
        rel = p.resolve().relative_to(root.resolve()).as_posix() if p.resolve().is_relative_to(root.resolve()) else p.name
        h.update(rel.encode("utf-8") + b"\0")
        h.update(p.read_bytes() + b"\0")
    return h.hexdigest()


def pipeline_digest(app_dir: Path = APP_DIR) -> str:
    """Empreinte du code Python de `server/app/` (hors prompts)."""
    return digest_paths(_iter_files(app_dir, (".py",)), app_dir)


def prompts_digest(prompts_dir: Path = PROMPTS_DIR) -> str:
    """Empreinte des prompts (`server/app/prompts/*.md|txt|j2`)."""
    return digest_paths(_iter_files(prompts_dir, (".md", ".txt", ".j2", ".jinja")), prompts_dir)


def cases_hash(paths: Iterable[Path], root: Path | None = None) -> str:
    """Empreinte d'un golden set (chemins relatifs triés + contenu).

    `root` sert à relativiser les chemins ; par défaut, leur ancêtre commun.
    """
    paths = [Path(p).resolve() for p in paths]
    if not paths:
        return hashlib.sha256().hexdigest()
    if root is None:
        root = Path(os.path.commonpath(paths))
        if root.is_file():
            root = root.parent
    return digest_paths(paths, root)
