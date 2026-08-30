"""Définitions partagées : empreintes du code du pipeline, des prompts et du golden set.

Un digest est un SHA-256 hex sur la liste triée des chemins relatifs et de leur contenu.
Un dossier absent donne le hash de la liste vide (jamais d'exception).
`pipeline_digest` couvre exactement `steps`, `pipelines`, `corpus`, `domain`, `llm` ;
`prompts_digest` couvre `llm/prompts`.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SERVER_DIR = APP_DIR.parent
EVALS_DIR = SERVER_DIR / "evals"
PROMPTS_DIR = APP_DIR / "llm" / "prompts"
PIPELINE_LAYERS = ("steps", "pipelines", "corpus", "domain", "llm")
_EXCLUDED_PARTS = {"__pycache__", "prompts", ".pytest_cache"}


def _iter_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix in suffixes and not (set(p.relative_to(root).parts) & _EXCLUDED_PARTS)
    )


def _key(p: Path, root: Path) -> str:
    """Chemin relatif posix sous `root`, sinon chemin absolu posix."""
    return p.relative_to(root).as_posix() if p.is_relative_to(root) else p.as_posix()


def digest_paths(paths: Iterable[Path], root: Path) -> str:
    """SHA-256 sur (clé de chemin, contenu) des fichiers, triés par clé ; les non-fichiers sont ignorés."""
    root = Path(root).resolve()
    files = sorted({Path(p).resolve() for p in paths if Path(p).is_file()}, key=lambda p: _key(p, root))
    h = hashlib.sha256()
    for p in files:
        h.update(_key(p, root).encode("utf-8") + b"\0")
        h.update(p.read_bytes() + b"\0")
    return h.hexdigest()


def pipeline_digest(app_dir: Path = APP_DIR) -> str:
    """Empreinte des couches qui définissent et exécutent le pipeline (hors `llm/prompts`)."""
    files = [f for layer in PIPELINE_LAYERS for f in _iter_files(app_dir / layer, (".py",))]
    return digest_paths(files, app_dir)


def prompts_digest(prompts_dir: Path = PROMPTS_DIR) -> str:
    """Empreinte des prompts (`server/app/llm/prompts/*.md|txt|j2`)."""
    return digest_paths(_iter_files(prompts_dir, (".md", ".txt", ".j2", ".jinja")), prompts_dir)


def harness_digest(evals_dir: Path = EVALS_DIR) -> str:
    """Empreinte du code qui exécute, juge et publie les évals (hors cas et références)."""
    return digest_paths(_iter_files(evals_dir, (".py",)), evals_dir)


def cases_hash(paths: Iterable[Path], root: Path | None = None) -> str:
    """Empreinte d'un golden set (chemins relatifs triés + contenu).

    `root` sert à relativiser les chemins ; par défaut, l'ancêtre commun des fichiers.
    """
    files = [Path(p).resolve() for p in paths if Path(p).is_file()]
    if not files:
        return hashlib.sha256().hexdigest()
    if root is None:
        root = Path(os.path.commonpath(files))
        if root.is_file():
            root = root.parent
    return digest_paths(files, root)
