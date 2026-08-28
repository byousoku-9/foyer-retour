"""Story 4.2b — Le plancher pré-enregistré : chargement, digest, règle mécanique de classement.

Le protocole précède toute mesure : `server/evals/reference/plancher.yaml` fige, par témoin, le
plancher, le `N`, le numérateur, le dénominateur et les règles d'incident **avant** que le premier
appel payant parte. Ce module :

1. **charge et valide** le plancher — un témoin sans dénominateur, un `N` sous `n_minimum`, ou un
   plancher qui descend sous le floor 4.2a ou la règle trusted importés : refus, jamais une
   approximation ;
2. **calcule son digest** (SHA-256 des octets du fichier) — le runner l'inscrit dans l'identité de
   run, si bien que deux rapports ne se comparent qu'à protocole égal ;
3. porte la **règle mécanique du checkpoint** : le classement des configurations candidates —
   admissibles au plancher d'abord, puis la moins chère, puis la plus rapide. Aucun jugement, aucune
   question humaine : c'est le premier des trois rôles du checkpoint de finalisation.

Aucun seuil numérique n'est écrit ici : ils vivent tous dans `plancher.yaml` (Boundaries de la
spec 4.2b — `config.py` ou `plancher.yaml`, jamais en dur).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

REFERENCE_DIR = Path(__file__).resolve().parent / "reference"
PLANCHER_PATH = REFERENCE_DIR / "plancher.yaml"


class PlancherInvalide(Exception):
    """Le plancher ne peut pas être chargé : le protocole n'existe pas, aucune mesure ne part."""


class Temoin(BaseModel):
    """Un témoin du plancher : plancher, N, numérateur, dénominateur, règle d'incident (AC 4.2b)."""

    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1)
    pipeline: str = Field(min_length=1)
    famille: str = Field(min_length=1)
    criticite: Literal["bloquant", "alerte"]
    plancher: float = Field(ge=0, le=1)
    n: int = Field(ge=1)
    numerateur: str = Field(min_length=1)
    denominateur: str = Field(min_length=1)
    incident: str = Field(min_length=1)
    scope: Literal["repo", "run", "suite", "case", "live_http"]
    mesure_par: Literal["eval_runner", "orchestrator"]
    case_id: str | None = None
    note: str | None = None


class ImportFloor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    n_minimum: int = Field(ge=1)
    thresholds: dict[str, float] = Field(min_length=1)


class ImportTrusted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: int
    proof_producer: Literal["orchestrator"]
    baseline_per_named_witness: int = Field(ge=1)
    final_per_named_witness: int = Field(ge=1)
    live_budget_default_eur: float = Field(gt=0)


class Imports(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor_4_2a: ImportFloor
    regle_trusted: ImportTrusted


class BudgetPlancher(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment_variable: str = Field(min_length=1)
    default_eur: float = Field(gt=0)
    enforced_by: list[str] = Field(min_length=1)
    on_exceeded: dict[str, Any]


class SeriesPlancher(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_per_named_witness: int = Field(ge=1)
    final_per_named_witness: int = Field(ge=1)
    repeat_required: bool
    max_cost_required: bool
    # Borne de sûreté de la CLI : elle vit dans le protocole versionné, jamais dans le runtime.
    max_repeat: int = Field(ge=3)


class Plancher(BaseModel):
    """Le protocole entier, tel que figé — validé strictement, jamais complété par défaut."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    story: str = Field(min_length=1)
    effective_from: str = Field(min_length=1)
    producer_de_preuve: Literal["orchestrator"]
    n_minimum: int = Field(ge=3)
    splits: dict[str, dict[str, Any]]
    budget: BudgetPlancher
    series: SeriesPlancher
    regles_incident: list[str] = Field(min_length=1)
    imports: Imports
    temoins: list[Temoin] = Field(min_length=1)

    @model_validator(mode="after")
    def _sans_diminution(self) -> Plancher:
        """Le floor 4.2a et la règle trusted sont importés **sans diminution** (AC 4.2b)."""
        par_metric = {t.metric: t for t in self.temoins}
        if len(par_metric) != len(self.temoins):
            raise ValueError("deux témoins portent la même metric : le plancher serait ambigu")
        floor = self.imports.floor_4_2a
        for metric, seuil in floor.thresholds.items():
            temoin = par_metric.get(metric)
            if temoin is None:
                raise ValueError(f"témoin {metric!r} du floor 4.2a absent : retirer un témoin est interdit")
            if temoin.plancher < seuil:
                raise ValueError(f"témoin {metric!r} : plancher {temoin.plancher} < floor 4.2a {seuil}")
            if temoin.n < floor.n_minimum:
                raise ValueError(f"témoin {metric!r} : n {temoin.n} < n_minimum 4.2a {floor.n_minimum}")
        if self.n_minimum < floor.n_minimum:
            raise ValueError(f"n_minimum {self.n_minimum} < n_minimum du floor 4.2a {floor.n_minimum}")
        trusted = self.imports.regle_trusted
        if self.budget.default_eur != trusted.live_budget_default_eur:
            raise ValueError(
                f"budget.default_eur {self.budget.default_eur} ≠ règle trusted "
                f"{trusted.live_budget_default_eur} : la règle trusted fait foi")
        if self.budget.environment_variable != "LIVE_BUDGET_EUR":
            raise ValueError("la règle trusted nomme LIVE_BUDGET_EUR : la variable ne se renomme pas")
        if self.series.baseline_per_named_witness != trusted.baseline_per_named_witness:
            raise ValueError("series.baseline_per_named_witness diverge de la règle trusted")
        if self.series.final_per_named_witness != trusted.final_per_named_witness:
            raise ValueError("series.final_per_named_witness diverge de la règle trusted")
        stabilite = [t for t in self.temoins if t.famille == "stabilite"]
        if not stabilite:
            raise ValueError("au moins un témoin de stabilité est requis (répétitions N>=3 sans cache)")
        for temoin in stabilite:
            if temoin.n < self.n_minimum:
                raise ValueError(f"témoin {temoin.metric!r} : la stabilité exige n >= {self.n_minimum}")
        for split in ("A", "B", "C"):
            if split not in self.splits:
                raise ValueError(f"split {split!r} absent : le protocole pré-enregistre les trois splits")
        return self

    def temoin(self, metric: str) -> Temoin | None:
        return next((t for t in self.temoins if t.metric == metric), None)


class ChargePlancher(BaseModel):
    """Le plancher chargé et son empreinte, indissociables."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plancher: Plancher
    digest: str


def _racine_autorite(plancher: Plancher, path: Path) -> Path | None:
    """Trouve facultativement le dépôt de contrôle, sans l'imposer au produit autonome."""
    sources = (Path(plancher.imports.floor_4_2a.source),
               Path(plancher.imports.regle_trusted.source))
    for parent in path.resolve().parents:
        if all((parent / source).is_file() for source in sources):
            return parent
    return None


def _yaml_objet(path: Path) -> dict[str, Any]:
    try:
        valeur = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PlancherInvalide(
            f"source d'autorité {path} illisible ({type(exc).__name__})") from exc
    if not isinstance(valeur, dict):
        raise PlancherInvalide(f"source d'autorité {path} : un objet YAML est attendu")
    return valeur


def _valider_snapshot(path: Path, digest: str) -> dict[str, Any]:
    try:
        octets = path.read_bytes()
    except OSError as exc:
        raise PlancherInvalide(f"snapshot figé {path} introuvable") from exc
    observe = hashlib.sha256(octets).hexdigest()
    if observe != digest:
        raise PlancherInvalide(
            f"snapshot figé {path.name} divergent : digest {observe}, attendu {digest}")
    return _yaml_objet(path)


def _valider_sources_figees(plancher: Plancher, path: Path) -> None:
    """Valide les copies produit : la CI publique ne dépend jamais du dépôt parent."""
    reference_dir = path.parent
    floor_importe = plancher.imports.floor_4_2a
    trusted_importe = plancher.imports.regle_trusted
    if not (reference_dir / floor_importe.snapshot).is_file():
        reference_dir = REFERENCE_DIR
    floor = _valider_snapshot(reference_dir / floor_importe.snapshot,
                              floor_importe.snapshot_digest)
    trusted = _valider_snapshot(reference_dir / trusted_importe.snapshot,
                                trusted_importe.snapshot_digest)
    if (floor_importe.n_minimum != floor.get("n_minimum")
            or floor_importe.thresholds != floor.get("thresholds")):
        raise PlancherInvalide("l'import floor 4.2a diverge du snapshot figé")
    attendu_trusted = {
        "schema_version": trusted.get("schema_version"),
        "proof_producer": (trusted.get("live") or {}).get("proof_producer"),
        "baseline_per_named_witness": ((trusted.get("live") or {}).get("series") or {}).get(
            "baseline_per_named_witness"),
        "final_per_named_witness": ((trusted.get("live") or {}).get("series") or {}).get(
            "final_per_named_witness"),
        "live_budget_default_eur": (trusted.get("budget") or {}).get("default_eur"),
    }
    if trusted_importe.model_dump(exclude={"source", "snapshot", "snapshot_digest"}) != attendu_trusted:
        raise PlancherInvalide("l'import trusted diverge du snapshot figé")


def _valider_sources_autorite(plancher: Plancher, racine: Path) -> None:
    """Recoupe les snapshots avec le dépôt parent lorsqu'il est présent."""
    floor_relatif = Path(plancher.imports.floor_4_2a.source)
    trusted_relatif = Path(plancher.imports.regle_trusted.source)
    floor = _yaml_objet(racine / floor_relatif)
    trusted = _yaml_objet(racine / trusted_relatif)
    importe_floor = plancher.imports.floor_4_2a
    importe_trusted = plancher.imports.regle_trusted
    if (importe_floor.n_minimum != floor.get("n_minimum")
            or importe_floor.thresholds != floor.get("thresholds")):
        raise PlancherInvalide("l'import floor 4.2a diverge de sa source d'autorité")
    attendu_trusted = {
        "schema_version": trusted.get("schema_version"),
        "proof_producer": (trusted.get("live") or {}).get("proof_producer"),
        "baseline_per_named_witness": ((trusted.get("live") or {}).get("series") or {}).get(
            "baseline_per_named_witness"),
        "final_per_named_witness": ((trusted.get("live") or {}).get("series") or {}).get(
            "final_per_named_witness"),
        "live_budget_default_eur": (trusted.get("budget") or {}).get("default_eur"),
    }
    if importe_trusted.model_dump(exclude={"source", "snapshot", "snapshot_digest"}) != attendu_trusted:
        raise PlancherInvalide("l'import trusted diverge de sa source d'autorité")


def charger_plancher(path: Path = PLANCHER_PATH, *, producer: str = "builder") -> ChargePlancher:
    """Charge, valide et fige le plancher — sinon `PlancherInvalide`, jamais un défaut silencieux."""
    try:
        octets = path.read_bytes()
        brut = yaml.safe_load(octets.decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PlancherInvalide(f"{path} : plancher illisible ({type(exc).__name__})") from exc
    if not isinstance(brut, dict):
        raise PlancherInvalide(f"{path} : un objet YAML est attendu")
    try:
        plancher = Plancher.model_validate(brut)
    except ValidationError as exc:
        premier = exc.errors()[0]
        champ = ".".join(str(p) for p in premier.get("loc", ())) or "(racine)"
        raise PlancherInvalide(f"{path} : champ {champ} — {premier.get('msg', '')}") from exc
    except ValueError as exc:
        raise PlancherInvalide(f"{path} : {exc}") from exc
    _valider_sources_figees(plancher, path)
    racine = _racine_autorite(plancher, path)
    if racine is not None:
        _valider_sources_autorite(plancher, racine)
    elif producer == "orchestrator":
        raise PlancherInvalide(
            "preuve non vérifiable : sources d'autorité 4.2a/trusted absentes du dépôt de contrôle")
    return ChargePlancher(plancher=plancher, digest=hashlib.sha256(octets).hexdigest())


# --- la règle mécanique du checkpoint (trois rôles, rôle 1) ---------------------------------------

class Configuration(BaseModel):
    """Une configuration candidate au checkpoint : son admissibilité et ses deux coûts."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    admissible: bool
    cost_eur: float = Field(ge=0, allow_inf_nan=False)
    latency_ms: int = Field(ge=0)
    run_digest: str | None = None
    report_digest: str | None = None


def _configuration_depuis_rapport(item: Any, *, base: Path) -> Configuration:
    if not isinstance(item, dict) or set(item) != {"name", "report"}:
        raise ValueError("chaque configuration porte exactement name et report")
    report_path = Path(str(item["report"]))
    if not report_path.is_absolute():
        report_path = base / report_path
    octets = report_path.read_bytes()
    rapport = json.loads(octets)
    if not isinstance(rapport, dict) or rapport.get("schema_version") != 3:
        raise ValueError(f"rapport {report_path} hors schéma 3")
    decisions = rapport.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError(f"rapport {report_path} sans décisions")
    identite = rapport.get("identity") or {}
    run_digest = identite.get("run_digest")
    if not isinstance(run_digest, str) or len(run_digest) != 64:
        raise ValueError(f"rapport {report_path} sans run_digest")
    metrics = rapport.get("metrics") or {}
    cost = rapport.get("cost_eur")
    latency = metrics.get("latency_p50_ms")
    if (isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost)
            or isinstance(latency, bool) or not isinstance(latency, int)):
        raise ValueError(f"rapport {report_path} sans coût/latence finis")
    admissible = bool(rapport.get("complete") is True
                      and not rapport.get("unexecuted_cases")
                      and all(isinstance(d, dict) and d.get("status") == "green"
                              and d.get("producer") == "orchestrator" for d in decisions))
    return Configuration(
        name=str(item["name"]), admissible=admissible, cost_eur=float(cost),
        latency_ms=latency, run_digest=run_digest,
        report_digest=hashlib.sha256(octets).hexdigest())


def classer_configurations(configurations: list[Configuration]) -> list[Configuration]:
    """Classement mécanique : admissibles au plancher d'abord, puis la moins chère, puis la plus rapide.

    C'est la règle du checkpoint de finalisation (spec 4.2b) : aucun jugement, aucune pondération.
    Une configuration inadmissible n'est jamais promue devant une admissible, quel que soit son
    coût ; `aucun_admissible` (liste sans admissible) est un résultat rouge que l'appelant publie
    tel quel, jamais une question humaine.
    """
    return sorted(configurations,
                  key=lambda c: (not c.admissible, c.cost_eur, c.latency_ms, c.name))


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m server.evals.plancher",
        description="Plancher 4.2b : digest du protocole et règle mécanique de classement.")
    parser.add_argument("--digest", action="store_true", help="afficher le digest du plancher chargé")
    parser.add_argument("--classer", type=Path, metavar="CONFIGS_JSON",
                        help="classer des configurations candidates (JSON : liste d'objets "
                             "{name, admissible, cost_eur, latency_ms})")
    args = parser.parse_args(argv)
    try:
        charge = charger_plancher()
    except PlancherInvalide as exc:
        print(f"refus : {exc}", file=sys.stderr)
        return 2
    if args.classer is not None:
        try:
            brut = json.loads(args.classer.read_text(encoding="utf-8"))
            if not isinstance(brut, list):
                raise ValueError("une liste de configurations est attendue")
            configurations = [
                _configuration_depuis_rapport(item, base=args.classer.parent) for item in brut]
            if len({c.name for c in configurations}) != len(configurations):
                raise ValueError("les noms de configurations doivent être uniques")
        except (OSError, ValueError, ValidationError) as exc:
            print(f"refus : configurations illisibles ({type(exc).__name__}: {exc})", file=sys.stderr)
            return 2
        classement = classer_configurations(configurations)
        aucun_admissible = not any(c.admissible for c in classement)
        print(json.dumps({
            "plancher_digest": charge.digest,
            "aucun_admissible": aucun_admissible,
            "classement": [c.model_dump() for c in classement],
        }, ensure_ascii=False, indent=2))
        # `aucun_admissible` est un rouge publié, jamais une question (Boundaries 4.2b).
        return 1 if aucun_admissible else 0
    print(f"plancher_digest={charge.digest} temoins={len(charge.plancher.temoins)} "
          f"n_minimum={charge.plancher.n_minimum}")
    return 0


if __name__ == "__main__":  # pragma: no cover — point d'entrée
    raise SystemExit(_main())
