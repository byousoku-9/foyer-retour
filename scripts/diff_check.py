"""Garde locale 4.1 : le harness ne traverse pas la frontière vers le système mesuré."""

from __future__ import annotations

import ast
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app.config import REPO_ROOT
from server.app.digests import pipeline_digest, prompts_digest
from server.app.llm.models import TIERS

SURFACES_LECTURE_SEULE = (
    "server/app/pipelines/",
    "data/",
    "web/",
)


def _git(*args: str) -> list[str]:
    resultat = subprocess.run(["git", *args], cwd=REPO_ROOT, check=True,
                              capture_output=True, text=True)
    return [ligne for ligne in resultat.stdout.splitlines() if ligne]


def _fichiers_modifies(baseline: str) -> list[str]:
    """Union baseline..HEAD + index/worktree + non suivis, donc sensible après commit."""
    subprocess.run(["git", "rev-parse", "--verify", f"{baseline}^{{commit}}"], cwd=REPO_ROOT,
                   check=True, capture_output=True, text=True)
    suivis = _git("diff", "--name-only", baseline, "--")
    non_suivis = _git("ls-files", "--others", "--exclude-standard")
    return sorted(set(suivis + non_suivis))


def _verifier_frontiere_imports() -> list[str]:
    erreurs: list[str] = []
    for path in sorted((REPO_ROOT / "server" / "app").rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            if any(module == "server.evals" or module.startswith("server.evals.")
                   for module in modules):
                erreurs.append(f"{path.relative_to(REPO_ROOT)} importe server.evals")
    return erreurs


def _verifier_gates() -> list[str]:
    manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_text("utf-8"))
    courant = {
        "pipeline_digest": pipeline_digest(),
        "prompts_digest": prompts_digest(),
        "model_ids": dict(TIERS),
    }
    erreurs = []
    for doc_id, entry in manifest.items():
        gate = entry.get("gate")
        if gate is None:
            continue
        for champ, attendu in courant.items():
            if gate.get(champ) != attendu:
                erreurs.append(f"{doc_id}.{champ} ne correspond plus à l'image courante")
    return erreurs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Garde de frontière de la story 4.1")
    parser.add_argument("--baseline", required=True,
                        help="commit de base ; compare baseline..HEAD, index, worktree et non suivis")
    args = parser.parse_args(argv)
    erreurs = _verifier_frontiere_imports() + _verifier_gates()
    for path in _fichiers_modifies(args.baseline):
        if path.startswith(SURFACES_LECTURE_SEULE):
            erreurs.append(f"surface déclarée en lecture seule modifiée : {path}")
    if erreurs:
        for erreur in erreurs:
            print(f"ERREUR: {erreur}")
        return 1
    print("diff-check: frontière app/evals, surfaces en lecture seule et empreintes conformes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
