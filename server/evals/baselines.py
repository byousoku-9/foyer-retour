"""Comparaison canonique 4.4 : trois variantes × deux profils de retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from server.app.config import (
    REPO_ROOT,
    RETRIEVAL_DEFAULT_PATH,
    RetrievalDefault,
    load_retrieval_default,
)
from server.app.digests import cases_hash, harness_digest, pipeline_digest, prompts_digest
from server.evals.plancher import PlancherInvalide, charger_plancher

VARIANTS = ("deterministe", "outils", "full_context")
TIERS = ("reason",)
CANONICAL_CELLS = tuple((variant, tier) for variant in VARIANTS for tier in TIERS)
SHA256_LEN = 64
REFERENCE_4_2D = {
    "date": "2026-08-28",
    "runs": {
        "deterministe": "a8aa84c5c1c6be723e36c2891861d4190427c62e7e34de16e17a5f0281be625a",
        "outils": "a20f53cd63a224ce239ccd54c94ab2b7b2c397971374a04eaf59fe4b431ddf0b",
    },
    "measures": {
        "deterministe": {"recall": 0.6667, "cost_eur": 0.1649, "latency_p50_ms": 14_243},
        "outils": {"recall": 0.6667, "cost_eur": 0.0703, "latency_p50_ms": 15_342},
    },
}


class BaselineError(ValueError):
    pass


@dataclass(frozen=True)
class BaselineCell:
    variant: str
    tier: str
    prompt_cache: bool

    @property
    def key(self) -> str:
        return f"{self.variant}/{self.tier}"


def parse_axis(raw: str, *, expected: Iterable[str], name: str) -> tuple[str, ...]:
    expected_tuple = tuple(expected)
    parts = tuple(part.strip() for part in raw.split(","))
    if not parts or any(not part for part in parts):
        raise BaselineError(f"--{name} refuse les éléments vides")
    if len(parts) != len(set(parts)):
        raise BaselineError(f"--{name} refuse les doublons")
    unknown = sorted(set(parts) - set(expected_tuple))
    missing = sorted(set(expected_tuple) - set(parts))
    if unknown or missing:
        raise BaselineError(
            f"--{name} doit contenir exactement {','.join(expected_tuple)} "
            f"(inconnus={unknown}, manquants={missing})")
    return expected_tuple


def canonical_plan(compare: str, tiers: str, *, suite: str | None) -> list[BaselineCell]:
    if suite != "guide":
        raise BaselineError("la matrice 4.4 exige exactement --suite guide")
    parse_axis(compare, expected=VARIANTS, name="compare")
    parse_axis(tiers, expected=TIERS, name="tiers")
    return [BaselineCell(variant, tier, prompt_cache=(tier == "reason"))
            for variant, tier in CANONICAL_CELLS]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_sha256(value: object) -> bool:
    return (isinstance(value, str) and len(value) == SHA256_LEN
            and all(character in "0123456789abcdef" for character in value))


def write_retrieval_default_atomic(path: Path, value: RetrievalDefault) -> bool:
    payload = json.dumps(asdict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        if path.read_text(encoding="utf-8") == payload:
            return False
    except FileNotFoundError:
        pass
    _write_atomic(path, payload)
    return True


def cell_identity(cell: BaselineCell, report: dict[str, Any], *, require_complete: bool) -> dict[str, Any]:
    identity = report.get("identity") or {}
    image = identity.get("image") or {}
    retrieval = identity.get("retrieval")
    expected = {
        "variant": cell.variant,
        "tier": cell.tier,
        "prompt_cache": cell.prompt_cache,
    }
    if not isinstance(retrieval, dict) or any(
            retrieval.get(key) != value for key, value in expected.items()):
        raise BaselineError(
            f"cellule {cell.key} : réglages retrieval enfant divergents "
            f"(attendus={expected}, publiés={retrieval!r})")
    mechanism_order = retrieval.get("mechanism_order")
    if (not isinstance(mechanism_order, list)
            or set(mechanism_order) != {"dictionnaire", "faq", "sommaire", "outils"}
            or len(mechanism_order) != 4):
        raise BaselineError(
            f"cellule {cell.key} : ordre mécanistique enfant absent ou invalide")
    if require_complete:
        required_digests = {
            "cases_hash": report.get("cases_hash"),
            "pipeline_digest": image.get("pipeline_digest"),
            "prompts_digest": image.get("prompts_digest"),
            "harness_digest": image.get("harness_digest"),
            "plancher_digest": image.get("plancher_digest"),
            "run_digest": identity.get("run_digest"),
        }
        invalid = [name for name, value in required_digests.items() if not _is_sha256(value)]
        if invalid:
            raise BaselineError(
                f"cellule {cell.key} : digests effectifs absents ou invalides ({', '.join(invalid)})")
        scope = identity.get("scope")
        common_types = {
            "model_ids": isinstance(image.get("model_ids"), dict),
            "normalize_version": isinstance(image.get("normalize_version"), str),
            "documents": isinstance(identity.get("documents"), dict),
            "comparison_parameters": isinstance(identity.get("comparison_parameters"), dict),
            "scope": isinstance(scope, dict),
            "case_ids": isinstance(scope, dict) and isinstance(scope.get("case_ids"), list)
            and all(isinstance(item, str) for item in scope["case_ids"]),
            "suites": isinstance(scope, dict) and isinstance(scope.get("suites"), list)
            and all(isinstance(item, str) for item in scope["suites"]),
            "profile": isinstance(report.get("profile"), str),
            "repeat": (not isinstance(report.get("repeat"), bool)
                       and isinstance(report.get("repeat"), int) and report["repeat"] >= 1),
            "cases_planned": (not isinstance(report.get("cases_planned"), bool)
                              and isinstance(report.get("cases_planned"), int)
                              and report["cases_planned"] >= 1),
        }
        invalid_common = [name for name, valid in common_types.items() if not valid]
        if invalid_common:
            raise BaselineError(
                f"cellule {cell.key} : identité comparable absente ou invalide "
                f"({', '.join(invalid_common)})")
    scope = identity.get("scope") or {}
    common = {
        "cases_hash": report.get("cases_hash"),
        "pipeline_digest": image.get("pipeline_digest"),
        "prompts_digest": image.get("prompts_digest"),
        "harness_digest": image.get("harness_digest"),
        "plancher_digest": image.get("plancher_digest"),
        "model_ids": image.get("model_ids"),
        "normalize_version": image.get("normalize_version"),
        "documents": identity.get("documents") or {},
        "case_ids": scope.get("case_ids") or [],
        "suites": scope.get("suites") or [],
        "references_digest": scope.get("references_digest"),
        "cache_namespace_digests": identity.get("cache_namespace_digests") or {},
        "comparison_parameters": identity.get("comparison_parameters") or {},
        "scope_common": {
            key: scope.get(key) for key in (
                "profile", "quick", "repeat", "campaign_id", "series_kind", "series_id")
        },
        "executions_planned": report.get("executions_planned"),
        "cases_planned": report.get("cases_planned"),
        "parameters": {
            "profile": report.get("profile"),
            "repeat": report.get("repeat"),
            "variant": retrieval["variant"],
            "tier": retrieval["tier"],
            "prompt_cache": retrieval["prompt_cache"],
            "mechanism_order": mechanism_order,
        },
    }
    return {**common, "digest": hashlib.sha256(_canonical(common).encode()).hexdigest()}


def _cell_from_report(cell: BaselineCell, report: dict[str, Any], *, status: str) -> dict[str, Any]:
    results = report.get("results")
    if not isinstance(results, list) or any(not isinstance(result, dict) for result in results):
        raise BaselineError(f"cellule {cell.key} : results absent ou invalide")
    planned = report.get("executions_planned")
    if isinstance(planned, bool) or not isinstance(planned, int) or planned <= 0:
        raise BaselineError(f"cellule {cell.key} : executions_planned invalide")
    actual_cost = report.get("cost_eur")
    if (isinstance(actual_cost, bool) or not isinstance(actual_cost, (int, float))
            or not math.isfinite(actual_cost) or actual_cost < 0):
        raise BaselineError(f"cellule {cell.key} : cost_eur absent ou invalide")
    original_total = report.get("cost_eur_original")
    if (isinstance(original_total, bool) or not isinstance(original_total, (int, float))
            or not math.isfinite(original_total) or original_total < 0):
        raise BaselineError(f"cellule {cell.key} : cost_eur_original absent ou invalide")
    complete = status == "completed"
    metrics_out = {"recall": None, "citation_introuvable_rate": None,
                   "average_cost_eur": None, "latency_p50_ms": None}
    if complete:
        if report.get("complete") is not True or len(results) != planned:
            raise BaselineError(f"cellule {cell.key} : rapport complet de mauvaise longueur")
        metrics = report.get("metrics")
        labels = metrics.get("labels") if isinstance(metrics, dict) else None
        recall = metrics.get("recall") if isinstance(metrics, dict) else None
        citation_count = labels.get("citation_introuvable") if isinstance(labels, dict) else None
        if (isinstance(recall, bool) or not isinstance(recall, (int, float))
                or not math.isfinite(recall) or not 0 <= recall <= 1):
            raise BaselineError(f"cellule {cell.key} : recall absent, non fini ou hors bornes")
        if (isinstance(citation_count, bool) or not isinstance(citation_count, int)
                or not 0 <= citation_count <= planned):
            raise BaselineError(
                f"cellule {cell.key} : compteur citation_introuvable invalide")
        original_costs: list[float] = []
        billed_costs: list[float] = []
        latencies: list[float] = []
        for result in results:
            cost = result.get("cost_eur_original")
            billed = result.get("cost_eur")
            latency = result.get("latency_ms")
            if (isinstance(cost, bool) or not isinstance(cost, (int, float))
                    or not math.isfinite(cost) or cost < 0):
                raise BaselineError(
                    f"cellule {cell.key} : coût fournisseur par cas absent ou invalide")
            if (isinstance(billed, bool) or not isinstance(billed, (int, float))
                    or not math.isfinite(billed) or billed < 0):
                raise BaselineError(
                    f"cellule {cell.key} : coût facturé par cas absent ou invalide")
            if (isinstance(latency, bool) or not isinstance(latency, (int, float))
                    or not math.isfinite(latency) or latency < 0):
                raise BaselineError(
                    f"cellule {cell.key} : latence par cas absente ou invalide")
            original_costs.append(float(cost))
            billed_costs.append(float(billed))
            latencies.append(float(latency))
        if abs(sum(billed_costs) - float(actual_cost)) > 0.0005:
            raise BaselineError(f"cellule {cell.key} : coût facturé agrégé divergent")
        if abs(sum(original_costs) - float(original_total)) > 0.0005:
            raise BaselineError(f"cellule {cell.key} : coût fournisseur agrégé divergent")
        metrics_out = {
            "recall": float(recall),
            "citation_introuvable_rate": citation_count / planned,
            "average_cost_eur": round(sum(original_costs) / planned, 4),
            "latency_p50_ms": int(statistics.median(latencies)),
        }
    return {
        "cell": asdict(cell),
        "key": cell.key,
        "status": status,
        "complete": complete,
        "identity": cell_identity(cell, report, require_complete=complete),
        "metrics": metrics_out,
        "results": results,
        "stop_reason": report.get("stop_reason"),
        "run_digest": (report.get("identity") or {}).get("run_digest"),
        "actual_cost_eur": float(actual_cost),
    }


def _common_comparison_identity(cell: dict[str, Any]) -> dict[str, Any]:
    identity = cell.get("identity") or {}
    parameters = identity.get("parameters") or {}
    return {
        key: identity.get(key) for key in (
            "cases_hash", "pipeline_digest", "prompts_digest", "harness_digest",
            "plancher_digest", "model_ids", "normalize_version", "documents", "case_ids",
            "suites", "references_digest",
            "scope_common", "executions_planned", "cases_planned",
            "comparison_parameters",
        )
    } | {
        "profile": parameters.get("profile"),
        "repeat": parameters.get("repeat"),
        "mechanism_order": parameters.get("mechanism_order"),
    }


def comparison_error(cells: list[dict[str, Any]]) -> str | None:
    if len(cells) != len(CANONICAL_CELLS) or not all(cell.get("complete") for cell in cells):
        return "une ou plusieurs cellules sont incomplètes"
    expected = _canonical(_common_comparison_identity(cells[0]))
    divergent = [cell["key"] for cell in cells[1:]
                 if _canonical(_common_comparison_identity(cell)) != expected]
    if divergent:
        return "paramètres communs divergents : " + ", ".join(divergent)
    return None


def empty_cell(cell: BaselineCell, *, status: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "cell": asdict(cell), "key": cell.key, "status": status, "complete": False,
        "identity": None,
        "metrics": {"recall": None, "citation_introuvable_rate": None,
                    "average_cost_eur": None, "latency_p50_ms": None},
        "results": [], "stop_reason": reason, "run_digest": None, "actual_cost_eur": None,
    }


def dominant(cells: list[dict[str, Any]]) -> dict[str, Any] | None:
    if ([cell.get("key") for cell in cells]
            != [f"{variant}/{tier}" for variant, tier in CANONICAL_CELLS]
            or not all(cell.get("complete") is True for cell in cells)):
        return None
    for cell in cells:
        metrics = cell.get("metrics")
        if not isinstance(metrics, dict):
            return None
        for name in ("recall", "average_cost_eur", "latency_p50_ms"):
            value = metrics.get(name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                return None
        if metrics["recall"] > 1:
            return None
    for candidate in cells:
        cm = candidate["metrics"]
        if all(
            cm["recall"] > other["metrics"]["recall"]
            and cm["average_cost_eur"] < other["metrics"]["average_cost_eur"]
            and cm["latency_p50_ms"] < other["metrics"]["latency_p50_ms"]
            for other in cells if other is not candidate
        ):
            return candidate
    return None


def validate_4_2d_proof(value: Any, *, expected_fingerprints: dict[str, str]) -> dict[str, Any]:
    """Valide une preuve structurée fermée ; toute divergence impose un rejeu sans mesure."""
    expected_keys = {"date", "runs", "measures", "fingerprints"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise BaselineError("preuve 4.2d : champs exacts date, runs, measures, fingerprints")
    try:
        parsed_date = date.fromisoformat(value["date"])
    except (TypeError, ValueError):
        raise BaselineError("preuve 4.2d : date ISO invalide") from None
    runs = value["runs"]
    if (not isinstance(runs, dict) or set(runs) != {"deterministe", "outils"}
            or any(not isinstance(v, str) or len(v) != SHA256_LEN
                   or any(c not in "0123456789abcdef" for c in v) for v in runs.values())):
        raise BaselineError("preuve 4.2d : deux run digests SHA-256 sont exigés")
    measures = value["measures"]
    measure_keys = {"recall", "cost_eur", "latency_p50_ms"}
    if not isinstance(measures, dict) or set(measures) != {"deterministe", "outils"}:
        raise BaselineError("preuve 4.2d : mesures complètes des deux variantes exigées")
    for variant, metrics in measures.items():
        if not isinstance(metrics, dict) or set(metrics) != measure_keys:
            raise BaselineError(f"preuve 4.2d : schéma de mesures fermé pour {variant}")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               or v < 0 for v in metrics.values()):
            raise BaselineError(f"preuve 4.2d : mesures finies positives pour {variant}")
        if metrics["recall"] > 1:
            raise BaselineError(f"preuve 4.2d : recall hors intervalle pour {variant}")
    fingerprints = value["fingerprints"]
    if not isinstance(fingerprints, dict) or set(fingerprints) != set(expected_fingerprints):
        raise BaselineError("preuve 4.2d : empreintes exhaustives exigées")
    if fingerprints != expected_fingerprints:
        return {"status": "replay_required", "reference_date": parsed_date.isoformat(),
                "reference_runs": runs, "measures": None, "fingerprints": fingerprints}
    return {"status": "reused", "reference_date": parsed_date.isoformat(),
            "reference_runs": runs, "measures": measures, "fingerprints": fingerprints}


def current_4_2d_fingerprints() -> dict[str, str]:
    """Empreintes exhaustives exigées pour reprendre le témoin sinistre historique."""
    cases_dir = REPO_ROOT / "server" / "evals" / "cases"
    case_path = cases_dir / "sinistre" / "s-bougie-canape.yaml"
    if not case_path.is_file():
        raise BaselineError(f"témoin 4.2d absent : {case_path}")
    manifest_path = REPO_ROOT / "data" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"manifest 4.2d illisible : {exc}") from exc
    entry = manifest.get("axa-lu-optihome-2017") if isinstance(manifest, dict) else None
    if not isinstance(entry, dict):
        raise BaselineError("entrée témoin axa-lu-optihome-2017 absente du manifest")
    try:
        plancher = charger_plancher().digest
    except PlancherInvalide as exc:
        raise BaselineError(f"plancher de protocole 4.2d invalide : {exc}") from exc
    fields = {
        "cases_hash": cases_hash([case_path], cases_dir),
        "pipeline_digest": pipeline_digest(),
        "prompts_digest": prompts_digest(),
        "harness_digest": harness_digest(),
        "plancher_digest": plancher,
        "source_hash": entry.get("source_hash"),
        "ingest_fingerprint": entry.get("ingest_fingerprint"),
        "overlay_hash": entry.get("overlay_hash") or hashlib.sha256(b"").hexdigest(),
    }
    if any(not _is_sha256(value) for value in fields.values()):
        raise BaselineError("empreintes courantes 4.2d absentes ou mal formées")
    return fields


def resolve_4_2d_proof(path: Path) -> dict[str, Any]:
    """Reprend une preuve structurée seulement à égalité totale ; sinon exige le rejeu."""
    if not path.is_file():
        return {"status": "replay_required", "reference": REFERENCE_4_2D,
                "measures": None, "reason": "preuve structurée absente"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        resolved = validate_4_2d_proof(
            value, expected_fingerprints=current_4_2d_fingerprints())
    except (OSError, json.JSONDecodeError, BaselineError) as exc:
        return {"status": "replay_required", "reference": REFERENCE_4_2D,
                "measures": None, "reason": str(exc)}
    if resolved["status"] == "replay_required":
        return {"status": "replay_required", "reference": REFERENCE_4_2D,
                "current": {
                    "date": resolved["reference_date"],
                    "runs": resolved["reference_runs"],
                    "fingerprints": resolved["fingerprints"],
                }, "measures": None, "delta": None, "reason": "empreintes divergentes"}
    measures = resolved["measures"]
    delta = {
        variant: {
            metric: round(float(measures[variant][metric])
                          - float(REFERENCE_4_2D["measures"][variant][metric]), 4)
            for metric in ("recall", "cost_eur", "latency_p50_ms")
        }
        for variant in ("deterministe", "outils")
    }
    return {"status": "reused", "reference": REFERENCE_4_2D,
            "current": {
                "date": resolved["reference_date"],
                "runs": resolved["reference_runs"],
                "fingerprints": resolved["fingerprints"],
            }, "measures": measures, "delta": delta, "reason": None}


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Baselines retrieval du guide", "",
        f"Run **{'complet' if report['complete'] else 'partiel'}** — budget global "
        f"{report['max_cost_eur']:.4f} €.", "",
        f"Défaut servi au départ : `{report['default_before']['variant']}/"
        f"{report['default_before']['tier']}` (prompt_cache="
        f"{str(report['default_before']['prompt_cache']).lower()}).", "",
        "| Cellule | État | Bonne fiche / recall | citation_introuvable | Coût moyen (€) | Latence p50 (ms) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for cell in report["cells"]:
        metrics = cell["metrics"]
        fmt = lambda value, digits=4: "—" if value is None else f"{value:.{digits}f}"  # noqa: E731
        latency = "—" if metrics["latency_p50_ms"] is None else str(metrics["latency_p50_ms"])
        lines.append(
            f"| `{cell['key']}` | `{cell['status']}` | {fmt(metrics['recall'])} | "
            f"{fmt(metrics['citation_introuvable_rate'])} | {fmt(metrics['average_cost_eur'])} | "
            f"{latency} |")
    sinistre = report["sinistre_4_2d"]
    reference = sinistre["reference"]
    lines.extend(["", "## Comparaison sinistre 4.2d", "",
                  f"Référence {reference['date']} : `deterministe` "
                  f"{reference['runs']['deterministe']}, `outils` "
                  f"{reference['runs']['outils']}.", ""])
    if sinistre["status"] == "reused":
        current = sinistre["current"]
        lines.extend([
            "Empreintes concordantes : mesures structurées reprises sans appel live. "
            f"Preuve courante {current['date']} : `deterministe` "
            f"{current['runs']['deterministe']}, `outils` {current['runs']['outils']}.", "",
            "| Variante | Recall | Δ recall | Coût (€) | Δ coût (€) | Latence p50 (ms) | Δ latence (ms) |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for variant in ("deterministe", "outils"):
            measures = sinistre["measures"][variant]
            delta = sinistre["delta"][variant]
            lines.append(
                f"| `{variant}` | {measures['recall']:.4f} | {delta['recall']:+.4f} | "
                f"{measures['cost_eur']:.4f} | {delta['cost_eur']:+.4f} | "
                f"{measures['latency_p50_ms']:.0f} | {delta['latency_p50_ms']:+.0f} |")
        lines.extend(["", "Les deltas ci-dessus sont calculés contre la référence historique, "
                      "qui reste visible sans être masquée par le rejeu.", ""])
    else:
        reason = sinistre.get("reason") or "preuve courante non réutilisable"
        lines.extend([
            f"La comparaison exige un rejeu orchestré : {reason}. Aucune mesure courante n'est "
            "reprise.", "",
        ])
    if report["recommendation"] is None:
        lines.append("Aucune recommandation : preuve non trusted, cellules non comparables ou "
                     "aucune domination triple.")
    else:
        lines.append(f"Recommandation : `{report['recommendation']}`.")
    lines.extend(["", "Règle : si une baseline gagne sur recall, coût et latence, elle devient la variante par défaut.", ""])
    return "\n".join(lines)


def _write_atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _write_atomic(path: Path, text: str) -> None:
    _write_atomic_bytes(path, text.encode("utf-8"))


def _snapshot(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _restore(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        _write_atomic_bytes(path, content)


def write_report(report: dict[str, Any], json_path: Path,
                 markdown_path: Path) -> tuple[bytes | None, bytes | None]:
    """Publie les deux projections comme une paire ; toute panne restaure les deux anciennes."""
    json_text = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    markdown_text = markdown(report)
    # Validation avant la première mutation : les deux projections doivent décrire le même objet.
    decoded = json.loads(json_text)
    if markdown(decoded) != markdown_text:
        raise BaselineError("projections JSON/Markdown incohérentes avant publication")
    previous = (_snapshot(json_path), _snapshot(markdown_path))
    try:
        _write_atomic(json_path, json_text)
        _write_atomic(markdown_path, markdown_text)
    except BaseException:
        _restore(json_path, previous[0])
        _restore(markdown_path, previous[1])
        raise
    return previous


def restore_report(paths: tuple[Path, Path], previous: tuple[bytes | None, bytes | None]) -> None:
    _restore(paths[0], previous[0])
    _restore(paths[1], previous[1])


def run_comparison(*, plan: list[BaselineCell], max_cost_eur: float,
                   base_argv: list[str], invoke: Callable[[list[str]], int],
                   output_json: Path, output_markdown: Path,
                   producer: Literal["builder", "orchestrator"] = "builder",
                   default_path: Path = RETRIEVAL_DEFAULT_PATH,
                   proof_4_2d_path: Path | None = None) -> int:
    """Exécute les cellules Sonnet reason sous un unique solde fini."""
    if [(cell.variant, cell.tier) for cell in plan] != list(CANONICAL_CELLS):
        raise BaselineError("le plan interne doit contenir toutes les cellules canoniques")
    if not math.isfinite(max_cost_eur) or max_cost_eur <= 0:
        raise BaselineError("le plafond global doit être fini et strictement positif")
    before = load_retrieval_default(default_path)
    cells: list[dict[str, Any]] = []
    spent = 0.0
    technical_code = 0
    quality_failure = False
    with tempfile.TemporaryDirectory(prefix="baseline-4-4-") as directory:
        root = Path(directory)
        for index, cell in enumerate(plan):
            remaining = max_cost_eur - spent
            if remaining <= 1e-12:
                cells.append(empty_cell(cell, status="not_run", reason="budget global épuisé"))
                continue
            cell_json = root / f"{index}.json"
            cell_md = root / f"{index}.md"
            cell_cache = root / f"cache-{index}"
            old_tier = os.environ.get("RETROUVER_OUTILS_TIER")
            old_cache = os.environ.get("RETRIEVAL_PROMPT_CACHE")
            os.environ["RETROUVER_OUTILS_TIER"] = cell.tier
            os.environ["RETRIEVAL_PROMPT_CACHE"] = "true" if cell.prompt_cache else "false"
            code: int | None = None
            reason = "runner enfant sans code de sortie"
            try:
                code = invoke([
                    *base_argv, "--variant", cell.variant, "--max-cost", f"{remaining:.10f}",
                    "--cache-dir", str(cell_cache),
                    "--output-json", str(cell_json), "--output-markdown", str(cell_md),
                ])
            except BaseException as exc:  # frontière du runner enfant, rapport parent obligatoire
                reason = f"invoke {type(exc).__name__}: {exc}"
            finally:
                if old_tier is None:
                    os.environ.pop("RETROUVER_OUTILS_TIER", None)
                else:
                    os.environ["RETROUVER_OUTILS_TIER"] = old_tier
                if old_cache is None:
                    os.environ.pop("RETRIEVAL_PROMPT_CACHE", None)
                else:
                    os.environ["RETRIEVAL_PROMPT_CACHE"] = old_cache
            if code is None:
                cells.append(empty_cell(cell, status="interrupted", reason=reason))
                technical_code = 3
                cells.extend(empty_cell(rest, status="not_run", reason="cellule antérieure interrompue")
                             for rest in plan[index + 1:])
                break
            if isinstance(code, bool) or not isinstance(code, int):
                cells.append(empty_cell(cell, status="interrupted", reason="code runner invalide"))
                technical_code = 3
                cells.extend(empty_cell(rest, status="not_run", reason="cellule antérieure interrompue")
                             for rest in plan[index + 1:])
                break
            if not cell_json.is_file():
                cells.append(empty_cell(cell, status="interrupted", reason=f"runner code {code}"))
                technical_code = code if code != 0 else 3
                cells.extend(empty_cell(rest, status="not_run", reason="cellule antérieure interrompue")
                             for rest in plan[index + 1:])
                break
            try:
                child = json.loads(cell_json.read_text(encoding="utf-8"))
                if not isinstance(child, dict):
                    raise BaselineError("rapport enfant JSON non objet")
                if child.get("producer") != producer:
                    raise BaselineError(
                        f"cellule {cell.key} : producer enfant divergent "
                        f"({child.get('producer')!r} != {producer!r})")
                status = ("completed" if code in (0, 1) and child.get("complete") is True
                          else "interrupted")
                parsed_cell = _cell_from_report(cell, child, status=status)
            except BaseException as exc:
                cells.append(empty_cell(cell, status="interrupted", reason=str(exc)))
                technical_code = code if code not in (0, 1) else 3
                cells.extend(
                    empty_cell(rest, status="not_run",
                               reason="cellule antérieure invalide")
                    for rest in plan[index + 1:])
                break
            cells.append(parsed_cell)
            spent += parsed_cell["actual_cost_eur"]
            quality_failure = quality_failure or code == 1
            if code not in (0, 1) or not parsed_cell["complete"]:
                technical_code = code if code != 0 else 3
                cells.extend(empty_cell(rest, status="not_run", reason="cellule antérieure interrompue")
                             for rest in plan[index + 1:])
                break
    if len(cells) < len(plan):
        cells.extend(empty_cell(cell, status="not_run") for cell in plan[len(cells):])
    comparability = comparison_error(cells)
    complete = comparability is None
    trusted = producer == "orchestrator"
    winner = dominant(cells) if complete and trusted else None
    recommendation = winner["key"] if winner is not None else None
    selected_default = RetrievalDefault(**winner["cell"]) if winner is not None else before
    changed = selected_default != before
    report = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "complete": complete,
        "max_cost_eur": max_cost_eur,
        "cost_eur": round(spent, 4),
        "producer": producer,
        "trusted": trusted,
        "comparison_error": comparability,
        "default_before": asdict(before),
        "default_after": asdict(selected_default),
        "default_changed": changed,
        "recommendation": recommendation,
        "cells": cells,
        "sinistre_4_2d": resolve_4_2d_proof(
            proof_4_2d_path or REPO_ROOT / "docs" / "evals" / "sinistre-4.2d-proof.json"),
    }
    previous_report = write_report(report, output_json, output_markdown)
    if changed:
        previous_default = _snapshot(default_path)
        try:
            write_retrieval_default_atomic(default_path, selected_default)
        except BaseException:
            _restore(default_path, previous_default)
            restore_report((output_json, output_markdown), previous_report)
            raise
    return technical_code or (1 if quality_failure else 0 if complete else 3)
