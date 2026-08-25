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
    "pipelines": {"steps", "domain", "config", "digests"},
    "api": {"pipelines", "corpus", "domain", "config", "digests", "llm"},
    "config": set(),
    "digests": set(),
}
EXTERNAL_ALLOWED: dict[str, set[str]] = {
    "domain": {"pydantic"},
    "corpus": set(),  # stdlib + domain seulement : jamais pydantic en direct
    "llm": {"anthropic", "pydantic"},  # AC 1.3 : rien d'autre hors domain, config, stdlib
    "steps": {"pydantic"},  # NFR9 (story 1.4) : jamais anthropic — le SDK ne se voit qu'à travers llm
    # Story 1.5 : un pipeline enchaîne des étapes et rien d'autre — ni `corpus`, ni `llm`, ni le SDK.
    # `corpus`, `index` et `client` lui arrivent en paramètres (annotés `Any`) depuis l'API.
    "pipelines": {"pydantic"},
    # Story 1.6 : la couche HTTP, et rien d'autre — `anthropic` en est absent, le SDK ne se voit
    # qu'à travers `llm` (AD-9), et `httpx` aussi : l'API ne sort jamais elle-même sur le réseau.
    "api": {"fastapi", "starlette", "pydantic"},
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


def test_the_verdict_table_is_pure_code() -> None:
    """AD-6 (story 1.8) : `domain/verdict.py` porte le découpage d'exécution — le **code** décide.

    `check_external("domain")` couvre déjà toute la couche, mais l'invariant vaut d'être nommé pour ce
    fichier-là : la table d'AD-6 doit rester jouable sans corpus, sans client de modèle et sans étape,
    c'est ce qui la rend testable ligne par ligne (`tests/test_verdict.py`) et ce qui interdit qu'un
    verdict se mette un jour à dépendre d'un appel.
    """
    fichier = APP / "domain" / "verdict.py"
    assert fichier.is_file()
    interdits = []
    for mod, ligne in _imports(fichier, APP):
        top = mod.split(".")[0]
        cible = _layer_of(mod)
        if cible in ("corpus", "llm", "steps", "pipelines", "api"):
            interdits.append(f"verdict.py:{ligne} importe {mod}")
        elif not _stdlib(top) and top not in ("pydantic",) and cible is None:
            interdits.append(f"verdict.py:{ligne} importe {mod}")
    assert not interdits, "\n".join(interdits)


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


def test_lapplication_nimporte_jamais_les_evals() -> None:
    """Convention Couches (spec 1.10) : `server/evals/` n'est **jamais** importé par `server/app/`.

    Le sens de la flèche est tout : `evals → pipelines, corpus, llm, domain` (le même assemblage que
    fait `api/etat.py`), et rien dans l'autre sens. Un serveur qui importerait le runner ferait
    dépendre le système mesuré de ce qui le mesure — et le gate, qui est censé juger l'image, serait
    juge et partie. Le contrôle est statique : aucun import n'est exécuté.
    """
    violations = []
    for layer in sorted(ALLOWED):
        for f in _layer_files(layer):
            for mod, line in _imports(f, APP):
                if mod.startswith("server.evals"):
                    violations.append(f"{f.relative_to(APP)}:{line} importe {mod}")
    assert not violations, "\n".join(violations)


# --- AD-1 : qui voit l'historique (story 2.2) --------------------------------
# AD-1 : « seules *comprendre* et *rédiger* reçoivent l'historique ; *retrouver* ne voit que
# `ParsedQuestion` ». L'invariant est une **absence**, et une absence ne se prouve pas en exerçant le
# pipeline : un test d'exécution ne rougirait que le jour où quelqu'un ferait *voyager* l'historique
# jusqu'à *vérifier*, pas le jour où il l'y déclarerait. Seul un contrôle statique qui échoue dès que
# le paramètre **apparaît** protège la règle. Contrôle AST, aucun import exécuté, comme le reste.
#
# **C'est le répertoire qui fait foi, pas cette table** (revue 2.2, P2) : `check_historique` balaye
# `steps/*.py` comme `_layer_files` le fait partout ailleurs dans ce fichier, et un module d'étape
# absent de la table **est** une violation. Une table seule ne dit rien d'un `steps/comparer.py`
# ajouté demain avec un paramètre `historique` : le contrôle ne l'ouvrirait pas, rendrait `[]`, et
# resterait vert — exactement le mode de défaillance qu'il existe pour empêcher. Ajouter une étape
# oblige donc à dire, ici, si l'historique lui est dû.
#
# Module d'étape → (fonction d'entrée, l'historique lui est-il dû ?).
ETAPES_HISTORIQUE: dict[str, tuple[str, bool]] = {
    "comprendre": ("comprendre", True),
    "rediger": ("rediger", True),
    "retrouver": ("retrouver_deterministe", False),
    "verifier": ("verifier", False),
    "restituer": ("restituer", False),
}
HISTORIQUE = "historique"


def _modules_detape(app: Path) -> dict[str, Path]:
    """Les modules de `steps/`, `__init__.py` exclu : le marqueur de paquet n'est pas une étape."""
    dossier = app / "steps"
    if not dossier.is_dir():
        return {}
    return {f.stem: f for f in sorted(dossier.glob("*.py")) if f.stem != "__init__"}


def _fonction(tree: ast.Module, nom: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """La fonction de premier niveau nommée `nom` dans le module, `None` si elle a disparu."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == nom:
            return node
    return None


def _parametres(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Tous les noms de paramètres, positionnels, nommés, `*args` et `**kwargs` compris."""
    a = fn.args
    noms = {arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    return noms | {v.arg for v in (a.vararg, a.kwarg) if v is not None}


def _identifiants(node: ast.AST) -> dict[str, int]:
    """Les **identifiants** du sous-arbre → la première ligne où chacun paraît.

    Jamais le contenu des chaînes : une docstring qui écrit « ne voit que `ParsedQuestion` — jamais
    l'historique » (c'est le cas de `steps/retrouver.py`, l. 10) énonce l'invariant, et la compter
    comme une violation ferait échouer le contrôle sur le texte même qui le justifie. Ce qui est
    interdit, c'est de *nommer* la variable.
    """
    lignes: dict[str, int] = {}

    def noter(nom: str, ligne: int) -> None:
        if nom not in lignes or ligne < lignes[nom]:
            lignes[nom] = ligne

    for sous in ast.walk(node):
        ligne = getattr(sous, "lineno", 0)
        if isinstance(sous, ast.Name):
            noter(sous.id, ligne)
        elif isinstance(sous, ast.arg):
            noter(sous.arg, ligne)
        elif isinstance(sous, ast.Attribute):
            noter(sous.attr, ligne)
        elif isinstance(sous, ast.keyword) and sous.arg is not None:
            noter(sous.arg, ligne)
        elif isinstance(sous, ast.alias):
            # `from x import historique as h` : le nom lié dans le module compte comme les autres.
            noter(sous.asname or sous.name.split(".")[-1], ligne)
    return lignes


def check_historique(app: Path = APP) -> list[str]:
    violations = []
    modules = _modules_detape(app)
    for module in sorted(set(modules) | set(ETAPES_HISTORIQUE)):
        f = modules.get(module)
        if f is None:
            violations.append(f"steps/{module}.py absent")
            continue
        if module not in ETAPES_HISTORIQUE:
            violations.append(f"steps/{module}.py : étape absente de ETAPES_HISTORIQUE "
                              f"(AD-1 : dire qui voit `{HISTORIQUE}` est dû pour chaque étape)")
            continue
        nom, du = ETAPES_HISTORIQUE[module]
        tree = ast.parse(f.read_text("utf-8"), filename=str(f))
        fn = _fonction(tree, nom)
        if fn is None:
            violations.append(f"steps/{module}.py : fonction {nom} introuvable")
            continue
        parametres = _parametres(fn)
        if du:
            if HISTORIQUE not in parametres:
                # L'autre moitié du contrat : une étape qui *doit* le recevoir et ne le déclare plus
                # a cassé la résolution des anaphores (story 2.2) sans qu'aucun type ne s'en aperçoive.
                violations.append(f"steps/{module}.py:{fn.lineno} {nom} ne déclare plus `{HISTORIQUE}`")
            continue
        if HISTORIQUE in parametres:
            violations.append(f"steps/{module}.py:{fn.lineno} {nom} déclare `{HISTORIQUE}` (AD-1 l'interdit)")
            continue
        # Le **module entier**, pas la seule fonction d'entrée (revue 2.2, P3) : un helper privé qui
        # reçoit l'historique et le nomme passerait sinon sans bruit, alors qu'il fait entrer la
        # conversation dans une étape à qui AD-1 la refuse.
        ligne = _identifiants(tree).get(HISTORIQUE)
        if ligne is not None:
            violations.append(f"steps/{module}.py:{ligne} nomme `{HISTORIQUE}` (AD-1 l'interdit)")
    return violations


def test_seules_comprendre_et_rediger_voient_lhistorique() -> None:
    """AD-1, mot pour mot : *retrouver* « ne voit que `ParsedQuestion` », jamais l'historique.

    La règle vaut au-delà de *retrouver* : *vérifier* et *restituer* ne le reçoivent pas davantage —
    l'un juge une ébauche contre des blocs, l'autre met en forme. Faire entrer la conversation dans
    l'un des trois rouvrirait la porte qu'AD-5 a fermée en story 1.4 : une question non autonome
    cherchée telle quelle, ou une réponse influencée par un tour précédent qu'aucune citation ne
    soutient.
    """
    violations = check_historique()
    assert not violations, "\n".join(violations)


def test_une_etape_qui_declare_lhistorique_est_detectee(tmp_path: Path) -> None:
    """Un contrôle d'absence qui ne rougit jamais ne protège rien : on le fait rougir ici.

    Les six cas que le contrat distingue : le paramètre interdit, le nommage interdit **hors** de la
    fonction d'entrée (le helper privé de P3), le paramètre dû qui disparaît, la fonction d'entrée
    introuvable (renommée ou supprimée), l'étape neuve que personne n'a inscrite dans la table (P2),
    et le module de la table qui a disparu du répertoire. `__init__.py` reste ignoré : c'est le
    marqueur du paquet, pas une étape.
    """
    app = _fake_app(tmp_path, {
        "steps/__init__.py": "historique = 1\n",
        "steps/comparer.py": "def comparer(parsed, historique):\n    return historique\n",
        "steps/comprendre.py": "async def comprendre(question, profil):\n    return question\n",
        "steps/rediger.py": "async def rediger(parsed, retrieval, historique):\n    return historique\n",
        "steps/retrouver.py": "def retrouver_deterministe(parsed, *, historique=None):\n    return parsed\n",
        "steps/verifier.py": ("def _juger(historique):\n    return historique\n\n\n"
                              "async def verifier(draft, *, parsed):\n    return _juger(parsed)\n"),
    })
    assert check_historique(app) == [
        "steps/comparer.py : étape absente de ETAPES_HISTORIQUE "
        "(AD-1 : dire qui voit `historique` est dû pour chaque étape)",
        "steps/comprendre.py:1 comprendre ne déclare plus `historique`",
        "steps/restituer.py absent",
        "steps/retrouver.py:1 retrouver_deterministe déclare `historique` (AD-1 l'interdit)",
        "steps/verifier.py:1 nomme `historique` (AD-1 l'interdit)",
    ]
