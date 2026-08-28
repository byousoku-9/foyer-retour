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
    n_minimum: int = Field(ge=1)
    thresholds: dict[str, float] = Field(min_length=1)


class ImportTrusted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
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
    series: dict[str, Any]
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


def charger_plancher(path: Path = PLANCHER_PATH) -> ChargePlancher:
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
    return ChargePlancher(plancher=plancher, digest=hashlib.sha256(octets).hexdigest())


# --- la règle mécanique du checkpoint (trois rôles, rôle 1) ---------------------------------------

class Configuration(BaseModel):
    """Une configuration candidate au checkpoint : son admissibilité et ses deux coûts."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    admissible: bool
    cost_eur: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
    run_digest: str | None = None


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
            configurations = [Configuration.model_validate(item) for item in brut]
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
