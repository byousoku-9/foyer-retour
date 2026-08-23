"""Dépendances de couches du spine, vérifiées statiquement (AST, aucun import exécuté).

- `domain` n'importe rien d'autre que la stdlib et pydantic ;
- `config.py` et `digests.py` (modules simples) n'importent que stdlib, pydantic, pydantic-settings ;
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
    "llm": {"domain", "config"},
    "steps": {"domain", "corpus", "llm", "config"},
    "pipelines": {"steps", "domain", "config"},
    "api": {"pipelines", "corpus", "domain", "config", "digests", "llm"},
    "config": set(),
    "digests": set(),
}
EXTERNAL_ALLOWED: dict[str, set[str]] = {
    "domain": {"pydantic"},
    "corpus": set(),  # stdlib + domain seulement : jamais pydantic en direct
    "llm": {"anthropic", "pydantic"},  # AC 1.3 : rien d'autre hors domain, config, stdlib
    "config": {"pydantic", "pydantic_settings"},
    "digests": {"pydantic"},
}


def _stdlib(name: str) -> bool:
    return name in sys.stdlib_module_names


def _imports(path: Path, app: Path) -> list[tuple[str, int]]:
    """Modules importés (absolus, les relatifs résolus depuis la racine `server/app`) et ligne."""
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    pkg_parts = ("server", "app", *path.relative_to(app).parent.parts)
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(a.name, node.lineno) for a in node.names]  # `import a.b as c` → a.b
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                mod = ".".join((*base, node.module) if node.module else base)
            else:
                mod = node.module or ""
            if mod in ("server.app", "server.app.steps"):
                # `from server.app import steps` / `from server.app.steps import x` : le nom importé compte
                found += [(f"{mod}.{a.name}", node.lineno) for a in node.names]
            else:
                found.append((mod, node.lineno))
    return found


def _layer_of(module: str) -> str | None:
    if module.startswith("server.app."):
        return module.split(".")[2]
    return None


def _layer_files(layer: str, app: Path = APP) -> list[Path]:
    d = app / layer
    if d.is_dir():
        return sorted(d.rglob("*.py"))
    f = app / f"{layer}.py"
    return [f] if f.is_file() else []


def check_external(layer: str, app: Path = APP) -> list[str]:
    violations = []
    for f in _layer_files(layer, app):
        for mod, line in _imports(f, app):
            top = mod.split(".")[0]
            target = _layer_of(mod)
            if target is not None and (target == layer or target in ALLOWED.get(layer, set())):
                continue  # dépendance interne : vérifiée par check_layer
            if mod.startswith(f"server.app.{layer}") or _stdlib(top) or top in EXTERNAL_ALLOWED.get(layer, set()):
                continue
            violations.append(f"{f.relative_to(app)}:{line} importe {mod}")
    return violations


def check_steps(app: Path = APP) -> list[str]:
    violations = []
    for f in _layer_files("steps", app):
        for mod, line in _imports(f, app):
            if mod.startswith("server.app.steps") and mod not in (f"server.app.steps.{f.stem}", "server.app.steps"):
                violations.append(f"{f.relative_to(app)}:{line} importe {mod}")
    return violations


def check_layer(layer: str, app: Path = APP) -> list[str]:
    violations = []
    for f in _layer_files(layer, app):
        for mod, line in _imports(f, app):
            target = _layer_of(mod)
            if target is None or target == layer or target in ALLOWED[layer]:
                continue
            violations.append(f"{f.relative_to(app)}:{line} importe {mod} (couche {layer} → {target} interdite)")
    return violations


@pytest.mark.parametrize("layer", sorted(EXTERNAL_ALLOWED))
def test_pure_layers_import_only_stdlib_and_pydantic(layer: str) -> None:
    assert _layer_files(layer), f"couche {layer} absente"
    violations = check_external(layer)
    assert not violations, "\n".join(violations)


def test_steps_never_import_another_step() -> None:
    assert not check_steps()


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_dependencies(layer: str) -> None:
    violations = check_layer(layer)
    assert not violations, "\n".join(violations)


def _fake_app(tmp_path: Path, files: dict[str, str]) -> Path:
    app = tmp_path / "app"
    for rel, content in files.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_text(content)
    return app


def test_step_importing_step_is_detected(tmp_path: Path) -> None:
    app = _fake_app(tmp_path, {"steps/a.py": "from server.app.steps.b import x\n", "steps/b.py": "x = 1\n",
                               "steps/c.py": "from .b import x\n",
                               "steps/d.py": "import server.app.steps.b as bb\n",
                               "steps/e.py": "from server.app.steps import b\n",
                               "steps/f.py": "from . import b\n"})
    assert check_steps(app) == ["steps/a.py:1 importe server.app.steps.b", "steps/c.py:1 importe server.app.steps.b",
                                "steps/d.py:1 importe server.app.steps.b", "steps/e.py:1 importe server.app.steps.b",
                                "steps/f.py:1 importe server.app.steps.b"]


def test_layer_violation_is_detected(tmp_path: Path) -> None:
    app = _fake_app(tmp_path, {"corpus/x.py": "import server.app.llm.client\n",
                               "corpus/y.py": "from server.app import steps\n",
                               "corpus/z.py": "from ..domain import document\n",
                               "corpus/w.py": "import server.app.steps.rediger as r\n",
                               "corpus/v.py": "from server.app.steps import rediger\n"})
    out = check_layer("corpus", app)
    assert out == ["corpus/v.py:1 importe server.app.steps.rediger (couche corpus → steps interdite)",
                   "corpus/w.py:1 importe server.app.steps.rediger (couche corpus → steps interdite)",
                   "corpus/x.py:1 importe server.app.llm.client (couche corpus → llm interdite)",
                   "corpus/y.py:1 importe server.app.steps (couche corpus → steps interdite)"]


def test_domain_violation_is_detected(tmp_path: Path) -> None:
    app = _fake_app(tmp_path, {"domain/a.py": "import pydantic\nfrom server.app.corpus import text\nimport httpx\n",
                               "domain/b.py": "from .a import x\nimport re\n"})
    assert check_external("domain", app) == ["domain/a.py:2 importe server.app.corpus", "domain/a.py:3 importe httpx"]
