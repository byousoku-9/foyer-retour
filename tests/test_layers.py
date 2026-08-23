"""Dépendances de couches du spine, vérifiées statiquement (AST, aucun import exécuté).

- `domain` n'importe rien d'autre que la stdlib et pydantic ;
- une étape (`steps/*.py`) n'importe jamais une autre étape ;
- chaque couche n'importe que celles autorisées par la table du spine.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "server" / "app"

ALLOWED: dict[str, set[str]] = {
    "domain": set(),
    "corpus": {"domain"},
    "llm": {"domain"},
    "steps": {"domain", "corpus", "llm", "config"},
    "pipelines": {"steps", "domain", "config"},
    "api": {"pipelines", "corpus", "domain", "config", "digests", "llm"},
}
DOMAIN_EXTERNAL_ALLOWED = {"pydantic"}


def _stdlib(name: str) -> bool:
    return name in sys.stdlib_module_names


def _imports(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # import relatif : résolu dans le paquet courant
                pkg = path.relative_to(APP).parts[0]
                found.append((f"server.app.{pkg}." + (node.module or ""), node.lineno))
            else:
                found.append((node.module or "", node.lineno))
    return found


def _layer_of(module: str) -> str | None:
    if module.startswith("server.app."):
        return module.split(".")[2]
    return None


def _layer_files(layer: str) -> list[Path]:
    d = APP / layer
    return sorted(d.rglob("*.py")) if d.is_dir() else []


def test_domain_imports_only_stdlib_and_pydantic() -> None:
    violations = []
    for f in _layer_files("domain"):
        for mod, line in _imports(f):
            top = mod.split(".")[0]
            if mod.startswith("server.app.domain") or _stdlib(top) or top in DOMAIN_EXTERNAL_ALLOWED:
                continue
            violations.append(f"{f.relative_to(APP)}:{line} importe {mod}")
    assert not violations, "\n".join(violations)


def test_steps_never_import_another_step() -> None:
    violations = []
    for f in _layer_files("steps"):
        for mod, line in _imports(f):
            if mod.startswith("server.app.steps.") and mod != f"server.app.steps.{f.stem}":
                violations.append(f"{f.relative_to(APP)}:{line} importe {mod}")
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_dependencies(layer: str) -> None:
    violations = []
    for f in _layer_files(layer):
        for mod, line in _imports(f):
            target = _layer_of(mod)
            if target is None or target == layer or target in ALLOWED[layer]:
                continue
            violations.append(f"{f.relative_to(APP)}:{line} importe {mod} (couche {layer} → {target} interdite)")
    assert not violations, "\n".join(violations)


def test_violation_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le test de couches doit échouer avec le chemin fautif quand a.py importe b.py."""
    fake_app = tmp_path / "app"
    (fake_app / "steps").mkdir(parents=True)
    (fake_app / "steps" / "a.py").write_text("from server.app.steps.b import x\n")
    (fake_app / "steps" / "b.py").write_text("x = 1\n")
    monkeypatch.setattr(sys.modules[__name__], "APP", fake_app)
    with pytest.raises(AssertionError, match=r"steps/a\.py:1 importe server\.app\.steps\.b"):
        test_steps_never_import_another_step()
