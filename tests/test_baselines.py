from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from server.evals import baselines, run as runner
from server.evals.baselines import (
    BaselineCell,
    BaselineError,
    canonical_plan,
    dominant,
    markdown,
    resolve_4_2d_proof,
    run_comparison,
    validate_4_2d_proof,
)


MECHANISM_ORDER = ["dictionnaire", "faq", "sommaire", "outils"]


def _retrieval_identity(variant: str, tier: str) -> dict[str, object]:
    return {"variant": variant, "tier": tier, "prompt_cache": tier == "reason",
            "mechanism_order": MECHANISM_ORDER}


def _image_identity() -> dict[str, object]:
    return {
        "pipeline_digest": "1" * 64,
        "prompts_digest": "2" * 64,
        "harness_digest": "3" * 64,
        "plancher_digest": "4" * 64,
        "model_ids": {"micro": "m", "reason": "r"},
        "normalize_version": "n1",
    }


def _scope_identity() -> dict[str, object]:
    return {"case_ids": ["g"], "suites": ["guide"], "profile": "vertical",
            "quick": False, "repeat": 1, "references_digest": None,
            "campaign_id": None, "series_kind": None, "series_id": None}


def _valid_child(argv: list[str], *, producer: str = "builder", cost: float = 0.1,
                 recall: float = 0.8, latency: int = 10) -> dict[str, object]:
    variant = argv[argv.index("--variant") + 1]
    tier = os.environ["RETROUVER_OUTILS_TIER"]
    return {
        "producer": producer, "complete": True, "stop_reason": None,
        "executions_planned": 1, "cases_planned": 1, "cost_eur": cost,
        "cost_eur_original": cost,
        "cases_hash": "a" * 64, "profile": "vertical", "repeat": 1,
        "metrics": {"recall": recall, "labels": {"citation_introuvable": 0}},
        "results": [{"cost_eur": cost, "cost_eur_original": cost,
                     "latency_ms": latency}],
        "identity": {"run_digest": "f" * 64, "image": _image_identity(),
                     "scope": _scope_identity(), "documents": {},
                     "comparison_parameters": {"thresholds": {"max_opens": 6},
                                               "usd_eur": 0.92},
                     "retrieval": _retrieval_identity(variant, tier)},
    }


def test_plan_is_canonical_under_cli_permutations_and_rejects_empty() -> None:
    first = canonical_plan("navigation,full_context", "reason", suite="guide")
    second = canonical_plan("full_context,navigation", "reason", suite="guide")
    assert first == second
    assert [cell.key for cell in first] == [
        "navigation/reason", "full_context/reason"]
    with pytest.raises(BaselineError, match="vides"):
        canonical_plan("navigation,,full_context", "reason", suite="guide")


def _cell(key: str, recall: float, cost: float, latency: int, *, complete: bool = True):
    variant, tier = key.split("/")
    return {
        "key": key, "complete": complete,
        "cell": {"variant": variant, "tier": tier, "prompt_cache": tier == "reason"},
        "metrics": {"recall": recall, "average_cost_eur": cost,
                    "latency_p50_ms": latency, "citation_introuvable_rate": 0.0},
    }


def test_only_strict_triple_dominance_can_recommend() -> None:
    cells = [_cell("navigation/reason", 1.0, 0.01, 10),
             _cell("full_context/reason", 0.9, 0.02, 20)]
    assert dominant(cells)["key"] == "navigation/reason"
    cells[-1]["complete"] = False
    assert dominant(cells) is None


def test_invalid_or_mismatched_4_2d_proof_never_reuses_measurements() -> None:
    fingerprints = {"cases_hash": "a" * 64, "pipeline_digest": "b" * 64,
                    "prompts_digest": "c" * 64, "harness_digest": "d" * 64}
    proof = {
        "date": "2026-08-28",
        "runs": {"deterministe": "1" * 64, "outils": "2" * 64},
        "measures": {
            "deterministe": {"recall": 1.0, "cost_eur": 0.1, "latency_p50_ms": 10},
            "outils": {"recall": 1.0, "cost_eur": 0.05, "latency_p50_ms": 8},
        },
        "fingerprints": {**fingerprints, "pipeline_digest": "e" * 64},
    }
    assert validate_4_2d_proof(proof, expected_fingerprints=fingerprints) == {
        "status": "replay_required", "reference_date": "2026-08-28",
        "reference_runs": proof["runs"], "measures": None,
        "fingerprints": proof["fingerprints"]}
    proof["measures"]["outils"]["cost_eur"] = float("nan")
    with pytest.raises(BaselineError, match="finies"):
        validate_4_2d_proof(proof, expected_fingerprints=fingerprints)


def test_matching_4_2d_proof_is_reused_and_markdown_publishes_its_measures(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fingerprints = {"cases_hash": "a" * 64, "pipeline_digest": "b" * 64,
                    "prompts_digest": "c" * 64, "harness_digest": "d" * 64}
    proof = {
        "date": "2026-08-28",
        "runs": {"deterministe": "1" * 64, "outils": "2" * 64},
        "measures": {
            "deterministe": {"recall": 0.8, "cost_eur": 0.1, "latency_p50_ms": 120},
            "outils": {"recall": 0.9, "cost_eur": 0.2, "latency_p50_ms": 150},
        },
        "fingerprints": fingerprints,
    }
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(proof), encoding="utf-8")
    monkeypatch.setattr(baselines, "current_4_2d_fingerprints", lambda: fingerprints)
    resolved = resolve_4_2d_proof(path)
    assert resolved["status"] == "reused"
    assert resolved["reference"] == baselines.REFERENCE_4_2D
    assert resolved["current"]["date"] == proof["date"]
    assert resolved["current"]["runs"] == proof["runs"]
    assert resolved["delta"]["deterministe"] == {
        "recall": 0.1333, "cost_eur": -0.0649, "latency_p50_ms": -14123.0}
    rendered = markdown({
        "complete": False, "max_cost_eur": 1.0,
        "default_before": {"variant": "full_context", "tier": "micro", "prompt_cache": True},
        "cells": [], "recommendation": None, "sinistre_4_2d": resolved,
    })
    assert "mesures structurées reprises" in rendered
    assert "| `deterministe` | 0.8000 | +0.1333 | 0.1000 | -0.0649 | 120 | -14123 |" in rendered
    assert "exige un rejeu orchestré" not in rendered


def test_recall_outside_unit_interval_is_not_a_reusable_measure() -> None:
    fingerprints = {"cases_hash": "a" * 64}
    proof = {
        "date": "2026-08-28",
        "runs": {"deterministe": "1" * 64, "outils": "2" * 64},
        "measures": {
            "deterministe": {"recall": 1.01, "cost_eur": 0.1, "latency_p50_ms": 10},
            "outils": {"recall": 1.0, "cost_eur": 0.1, "latency_p50_ms": 10},
        },
        "fingerprints": fingerprints,
    }
    with pytest.raises(BaselineError, match="recall hors intervalle"):
        validate_4_2d_proof(proof, expected_fingerprints=fingerprints)


@pytest.mark.parametrize("mutation", ["malformed", "fingerprint"])
def test_malformed_or_divergent_4_2d_proof_resolves_to_replay_without_measures(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    fingerprints = {"cases_hash": "a" * 64, "pipeline_digest": "b" * 64}
    value: object
    if mutation == "malformed":
        value = {"date": "pas-une-date"}
    else:
        value = {
            "date": "2026-08-28",
            "runs": {"deterministe": "1" * 64, "outils": "2" * 64},
            "measures": {
                "deterministe": {"recall": 0.8, "cost_eur": 0.1, "latency_p50_ms": 120},
                "outils": {"recall": 0.9, "cost_eur": 0.2, "latency_p50_ms": 150},
            },
            "fingerprints": {**fingerprints, "cases_hash": "f" * 64},
        }
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(baselines, "current_4_2d_fingerprints", lambda: fingerprints)
    resolved = resolve_4_2d_proof(path)
    assert resolved["status"] == "replay_required" and resolved["measures"] is None


def test_interruption_publishes_chaque_etat_and_preserves_technical_code(tmp_path: Path) -> None:
    default_path = tmp_path / "default.json"
    default_path.write_text(
        '{"prompt_cache":true,"tier":"micro","variant":"full_context"}\n', encoding="utf-8")
    calls = 0

    def invoke(argv: list[str]) -> int:
        nonlocal calls
        calls += 1
        output = Path(argv[argv.index("--output-json") + 1])
        report = {
            "producer": "builder",
            "complete": calls == 1, "stop_reason": None if calls == 1 else "budget",
            "executions_planned": 1, "cases_planned": 1,
            "cost_eur": 0.1 if calls == 1 else 0.0,
            "cost_eur_original": 0.1 if calls == 1 else 0.0,
            "cases_hash": "a" * 64, "profile": "vertical", "repeat": 1,
            "metrics": {"recall": 1.0, "labels": {"citation_introuvable": 0}},
            "results": ([{"cost_eur": 0.1, "cost_eur_original": 0.1,
                          "latency_ms": 10}] if calls == 1 else []),
            "identity": {"run_digest": "f" * 64, "image": _image_identity(),
                         "scope": _scope_identity(),
                         "documents": {}, "comparison_parameters": {"thresholds": {},
                                                                    "usd_eur": 0.92},
                         "retrieval": _retrieval_identity(
                             argv[argv.index("--variant") + 1],
                             __import__("os").environ["RETROUVER_OUTILS_TIER"])},
        }
        output.write_text(json.dumps(report), encoding="utf-8")
        return 0 if calls == 1 else 3

    output_json, output_md = tmp_path / "baselines.json", tmp_path / "baselines.md"
    plan = canonical_plan("navigation,full_context", "reason", suite="guide")
    assert run_comparison(plan=plan, max_cost_eur=1.0, base_argv=[], invoke=invoke,
                          output_json=output_json, output_markdown=output_md,
                          default_path=default_path) == 3
    report = json.loads(output_json.read_text(encoding="utf-8"))
    assert [cell["status"] for cell in report["cells"]] == ["completed", "interrupted"]
    assert all(value is None for value in report["cells"][1]["metrics"].values())
    assert default_path.read_text(encoding="utf-8") == \
        '{"prompt_cache":true,"tier":"micro","variant":"full_context"}\n'


def test_divergent_effective_retrieval_settings_interrupt_without_mutating_default(
        tmp_path: Path) -> None:
    default_path = tmp_path / "default.json"
    original = '{"prompt_cache":true,"tier":"micro","variant":"full_context"}\n'
    default_path.write_text(original, encoding="utf-8")

    def invoke(argv: list[str]) -> int:
        output = Path(argv[argv.index("--output-json") + 1])
        output.write_text(json.dumps({
            "producer": "builder",
            "complete": True,
            "executions_planned": 1,
            "cases_planned": 1,
            "cost_eur": 0.01,
            "cost_eur_original": 0.01,
            "cases_hash": "a" * 64,
            "profile": "vertical",
            "repeat": 1,
            "metrics": {"recall": 1.0, "labels": {"citation_introuvable": 0}},
            "results": [{"cost_eur": 0.01, "cost_eur_original": 0.01,
                         "latency_ms": 10}],
            "identity": {
                "run_digest": "f" * 64,
                "image": {"harness_digest": "e" * 64},
                "scope": _scope_identity(),
                "documents": {},
                # La première cellule demande reason/cache : ce rapport prétend avoir exécuté
                # micro/no-cache. Le plan ne peut jamais remplacer ce fait runtime.
                "retrieval": _retrieval_identity("navigation", "micro"),
            },
        }), encoding="utf-8")
        return 0

    plan = canonical_plan("navigation,full_context", "reason", suite="guide")
    output_json = tmp_path / "baselines.json"
    code = run_comparison(
        plan=plan, max_cost_eur=1.0, base_argv=[], invoke=invoke,
        output_json=output_json, output_markdown=tmp_path / "baselines.md",
        default_path=default_path, proof_4_2d_path=tmp_path / "missing-proof.json")

    report = json.loads(output_json.read_text(encoding="utf-8"))
    assert code == 3
    assert [cell["status"] for cell in report["cells"]] == ["interrupted", "not_run"]
    assert "divergents" in report["cells"][0]["stop_reason"]
    assert report["recommendation"] is None and report["default_changed"] is False
    assert default_path.read_text(encoding="utf-8") == original


def test_cell_profile_pairs_reason_with_cache_and_micro_without_cache() -> None:
    plan = canonical_plan("navigation,full_context", "reason", suite="guide")
    assert all(isinstance(cell, BaselineCell) for cell in plan)
    assert all(cell.prompt_cache is (cell.tier == "reason") for cell in plan)


def test_orchestrator_matrix_keeps_provenance_without_4_2b_repeat_requirement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_comparison(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(baselines, "run_comparison", fake_comparison)
    monkeypatch.setenv("LIVE_CAMPAIGN_ID", "baseline-4-4-test")
    code = runner.main([
        "--suite", "guide", "--compare", "full_context,navigation",
        "--tiers", "reason", "--producer", "orchestrator",
        "--series-kind", "baseline", "--series-id", "guide-4-4",
        "--max-cost", "0.2", "--output-json", str(tmp_path / "baselines.json"),
        "--output-markdown", str(tmp_path / "baselines.md"),
    ])
    assert code == 0
    base_argv = captured["base_argv"]
    assert isinstance(base_argv, list)
    assert base_argv[base_argv.index("--producer") + 1] == "orchestrator"
    assert "--comparison-cell" not in base_argv
    assert "--repeat" not in base_argv


def test_complete_strict_winner_replaces_the_whole_default_triplet_atomically(tmp_path: Path) -> None:
    default_path = tmp_path / "default.json"
    default_path.write_text(
        '{"prompt_cache":true,"tier":"micro","variant":"full_context"}\n', encoding="utf-8")

    def invoke(argv: list[str]) -> int:
        output = Path(argv[argv.index("--output-json") + 1])
        variant = argv[argv.index("--variant") + 1]
        tier = __import__("os").environ["RETROUVER_OUTILS_TIER"]
        winner = variant == "full_context" and tier == "reason"
        report = {
            "producer": "orchestrator",
            "complete": True, "stop_reason": None, "executions_planned": 1,
            "cases_planned": 1, "cost_eur": 0.01 if winner else 0.02,
            "cost_eur_original": 0.01 if winner else 0.02, "cases_hash": "a" * 64,
            "profile": "vertical", "repeat": 1,
            "metrics": {"recall": 1.0 if winner else 0.9,
                        "labels": {"citation_introuvable": 0}},
            "results": [{"cost_eur": 0.01 if winner else 0.02,
                         "cost_eur_original": 0.01 if winner else 0.02,
                         "latency_ms": 5 if winner else 10}],
            "identity": {"run_digest": "f" * 64, "image": _image_identity(),
                         "scope": _scope_identity(), "documents": {},
                         "comparison_parameters": {"thresholds": {}, "usd_eur": 0.92},
                         "retrieval": _retrieval_identity(variant, tier)},
        }
        output.write_text(json.dumps(report), encoding="utf-8")
        return 0

    plan = canonical_plan("navigation,full_context", "reason", suite="guide")
    code = run_comparison(
        plan=plan, max_cost_eur=1.0, base_argv=[], invoke=invoke,
        output_json=tmp_path / "baselines.json", output_markdown=tmp_path / "baselines.md",
        producer="orchestrator", default_path=default_path,
        proof_4_2d_path=tmp_path / "missing-proof.json")
    assert code == 0
    assert json.loads(default_path.read_text(encoding="utf-8")) == {
        "variant": "full_context", "tier": "reason", "prompt_cache": True}


def test_complete_builder_matrix_is_diagnostic_and_never_promotes(tmp_path: Path) -> None:
    default_path = tmp_path / "default.json"
    original = b'{"prompt_cache":true,"tier":"micro","variant":"full_context"}\n'
    default_path.write_bytes(original)

    def invoke(argv: list[str]) -> int:
        report = _valid_child(
            argv, recall=1.0 if argv[argv.index("--variant") + 1] == "full_context" else 0.8,
            cost=0.01 if argv[argv.index("--variant") + 1] == "full_context" else 0.02,
            latency=5 if argv[argv.index("--variant") + 1] == "full_context" else 10)
        Path(argv[argv.index("--output-json") + 1]).write_text(
            json.dumps(report), encoding="utf-8")
        return 0

    output = tmp_path / "baselines.json"
    code = run_comparison(
        plan=canonical_plan("navigation,full_context", "reason", suite="guide"),
        max_cost_eur=1.0, base_argv=[], invoke=invoke, output_json=output,
        output_markdown=tmp_path / "baselines.md", default_path=default_path,
        proof_4_2d_path=tmp_path / "missing.json")
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0 and report["complete"] is True
    assert report["trusted"] is False and report["recommendation"] is None
    assert default_path.read_bytes() == original


def test_common_identity_divergence_makes_orchestrator_report_partial(tmp_path: Path) -> None:
    default_path = tmp_path / "default.json"
    original = b'{"prompt_cache":true,"tier":"micro","variant":"full_context"}\n'
    default_path.write_bytes(original)
    count = 0

    def invoke(argv: list[str]) -> int:
        nonlocal count
        report = _valid_child(argv, producer="orchestrator", recall=0.8, cost=0.02)
        if count == 1:
            # La **seconde** cellule diverge : la matrice n'en compte plus que deux depuis sa
            # reformulation, et ce qui est mesuré est la divergence entre cellules, pas son rang.
            report["identity"]["image"]["harness_digest"] = "9" * 64  # type: ignore[index]
        count += 1
        Path(argv[argv.index("--output-json") + 1]).write_text(
            json.dumps(report), encoding="utf-8")
        return 0

    output = tmp_path / "baselines.json"
    code = run_comparison(
        plan=canonical_plan("navigation,full_context", "reason", suite="guide"),
        max_cost_eur=1.0, base_argv=[], invoke=invoke, output_json=output,
        output_markdown=tmp_path / "baselines.md", producer="orchestrator",
        default_path=default_path, proof_4_2d_path=tmp_path / "missing.json")
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 3 and report["complete"] is False
    assert "paramètres communs divergents" in report["comparison_error"]
    assert report["recommendation"] is None and default_path.read_bytes() == original


@pytest.mark.parametrize(
    ("failure", "expected_code"), [("invoke", 3), ("no_code", 3), ("json", 4)])
def test_runner_boundary_always_publishes_chaque_etat_on_exceptions(
        tmp_path: Path, failure: str, expected_code: int) -> None:
    def invoke(argv: list[str]) -> int:
        if failure == "invoke":
            raise RuntimeError("panne injectée")
        if failure == "no_code":
            return None  # type: ignore[return-value]
        Path(argv[argv.index("--output-json") + 1]).write_text("{", encoding="utf-8")
        return 4

    default_path = tmp_path / "default.json"
    default_path.write_text(
        '{"prompt_cache":true,"tier":"micro","variant":"full_context"}\n', encoding="utf-8")
    output = tmp_path / "baselines.json"
    code = run_comparison(
        plan=canonical_plan("navigation,full_context", "reason", suite="guide"),
        max_cost_eur=1.0, base_argv=[], invoke=invoke, output_json=output,
        output_markdown=tmp_path / "baselines.md", default_path=default_path,
        proof_4_2d_path=tmp_path / "missing.json")
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == expected_code
    assert [cell["status"] for cell in report["cells"]] == ["interrupted", "not_run"]
    assert all(value is None for value in report["cells"][0]["metrics"].values())


@pytest.mark.parametrize("mutation", ["missing_latency", "boolean_cost", "nan_recall", "short"])
def test_completed_child_requires_finite_complete_billable_metrics(
        tmp_path: Path, mutation: str) -> None:
    def invoke(argv: list[str]) -> int:
        report = _valid_child(argv)
        if mutation == "missing_latency":
            del report["results"][0]["latency_ms"]  # type: ignore[index]
        elif mutation == "boolean_cost":
            report["results"][0]["cost_eur_original"] = False  # type: ignore[index]
        elif mutation == "nan_recall":
            report["metrics"]["recall"] = float("nan")  # type: ignore[index]
        else:
            report["executions_planned"] = 2
        Path(argv[argv.index("--output-json") + 1]).write_text(
            json.dumps(report), encoding="utf-8")
        return 0

    default_path = tmp_path / "default.json"
    default_path.write_text(
        '{"prompt_cache":true,"tier":"micro","variant":"full_context"}\n', encoding="utf-8")
    output = tmp_path / "baselines.json"
    assert run_comparison(
        plan=canonical_plan("navigation,full_context", "reason", suite="guide"),
        max_cost_eur=1.0, base_argv=[], invoke=invoke, output_json=output,
        output_markdown=tmp_path / "baselines.md", default_path=default_path,
        proof_4_2d_path=tmp_path / "missing.json") == 3
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["cells"][0]["status"] == "interrupted"
    assert all(value is None for value in report["cells"][0]["metrics"].values())


def test_cells_receive_global_remaining_balance_and_isolated_response_caches(tmp_path: Path) -> None:
    balances: list[float] = []
    cache_dirs: list[Path] = []
    private_reports: list[Path] = []
    costs = iter((0.1, 0.2, 0.7))

    def invoke(argv: list[str]) -> int:
        balances.append(float(argv[argv.index("--max-cost") + 1]))
        cache_dir = Path(argv[argv.index("--cache-dir") + 1])
        private_reports.extend([
            Path(argv[argv.index("--output-json") + 1]),
            Path(argv[argv.index("--output-markdown") + 1]),
        ])
        assert cache_dir not in cache_dirs and not cache_dir.exists()
        cache_dirs.append(cache_dir)
        cache_dir.mkdir()
        cost = next(costs)
        report = _valid_child(argv, cost=cost)
        Path(argv[argv.index("--output-json") + 1]).write_text(
            json.dumps(report), encoding="utf-8")
        return 0

    default_path = tmp_path / "default.json"
    default_path.write_text(
        '{"prompt_cache":true,"tier":"micro","variant":"full_context"}\n', encoding="utf-8")
    output = tmp_path / "baselines.json"
    code = run_comparison(
        plan=canonical_plan("navigation,full_context", "reason", suite="guide"),
        max_cost_eur=1.0, base_argv=[], invoke=invoke, output_json=output,
        output_markdown=tmp_path / "baselines.md", default_path=default_path,
        proof_4_2d_path=tmp_path / "missing.json")
    report = json.loads(output.read_text(encoding="utf-8"))
    assert balances == [1.0, 0.9]
    assert len(set(cache_dirs)) == 2
    assert not any(path.exists() for path in [*cache_dirs, *private_reports])
    assert all(path.parent == cache_dirs[0].parent for path in private_reports)
    assert [cell["status"] for cell in report["cells"]] == ["completed", "completed"]
    assert code == 0


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--orchestrator-report", "rapport.json"),
        ("--candidate-revision", "1" * 40),
        ("--relecture-verdict", "verdict.json"),
        ("--relecture-images", "images"),
    ],
)
def test_la_matrice_refuse_les_options_4_5_quelle_ne_consomme_pas(
        option: str, value: str) -> None:
    code = runner.main([
        "--suite", "guide", "--compare", "navigation,full_context",
        "--tiers", "reason", option, value,
    ])
    assert code == 2


def test_pair_and_default_are_rolled_back_on_publication_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default_path = tmp_path / "default.json"
    output_json, output_md = tmp_path / "baselines.json", tmp_path / "baselines.md"
    original_default = b'{"prompt_cache":true,"tier":"micro","variant":"full_context"}\n'
    original_json, original_md = b'{"old":true}\n', b"ancien markdown\n"
    default_path.write_bytes(original_default)
    output_json.write_bytes(original_json)
    output_md.write_bytes(original_md)
    real_write = baselines._write_atomic
    failed = False

    def fail_default_once(path: Path, text: str) -> None:
        nonlocal failed
        if path == default_path and not failed:
            failed = True
            raise OSError("panne défaut")
        real_write(path, text)

    def invoke(argv: list[str]) -> int:
        variant = argv[argv.index("--variant") + 1]
        winner = variant == "full_context" and os.environ["RETROUVER_OUTILS_TIER"] == "reason"
        report = _valid_child(
            argv, producer="orchestrator", recall=1.0 if winner else 0.8,
            cost=0.01 if winner else 0.02, latency=5 if winner else 10)
        Path(argv[argv.index("--output-json") + 1]).write_text(
            json.dumps(report), encoding="utf-8")
        return 0

    monkeypatch.setattr(baselines, "_write_atomic", fail_default_once)
    with pytest.raises(OSError, match="panne défaut"):
        run_comparison(
            plan=canonical_plan("navigation,full_context", "reason", suite="guide"),
            max_cost_eur=1.0, base_argv=[], invoke=invoke, output_json=output_json,
            output_markdown=output_md, producer="orchestrator", default_path=default_path,
            proof_4_2d_path=tmp_path / "missing.json")
    assert default_path.read_bytes() == original_default
    assert output_json.read_bytes() == original_json and output_md.read_bytes() == original_md


def test_json_markdown_pair_is_rolled_back_if_second_projection_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    json_path, markdown_path = tmp_path / "baselines.json", tmp_path / "baselines.md"
    original_json, original_markdown = b'{"old":true}\n', b"ancien\n"
    json_path.write_bytes(original_json)
    markdown_path.write_bytes(original_markdown)
    report = json.loads(
        (baselines.REPO_ROOT / "docs" / "evals" / "baselines.json").read_text(encoding="utf-8"))
    real_write = baselines._write_atomic
    failed = False

    def fail_markdown_once(path: Path, text: str) -> None:
        nonlocal failed
        if path == markdown_path and not failed:
            failed = True
            raise OSError("panne markdown")
        real_write(path, text)

    monkeypatch.setattr(baselines, "_write_atomic", fail_markdown_once)
    with pytest.raises(OSError, match="panne markdown"):
        baselines.write_report(report, json_path, markdown_path)
    assert json_path.read_bytes() == original_json
    assert markdown_path.read_bytes() == original_markdown


def test_shipped_baseline_pair_is_reproducible() -> None:
    json_path = baselines.REPO_ROOT / "docs" / "evals" / "baselines.json"
    markdown_path = baselines.REPO_ROOT / "docs" / "evals" / "baselines.md"
    assert markdown(json.loads(json_path.read_text(encoding="utf-8"))) == \
        markdown_path.read_text(encoding="utf-8")


def test_harness_digest_changes_when_real_eval_code_changes(tmp_path: Path) -> None:
    from server.app.digests import harness_digest

    evals = tmp_path / "evals"
    evals.mkdir()
    source = evals / "run.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    before = harness_digest(evals)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert harness_digest(evals) != before


def test_actual_4_2d_witness_corpus_floor_and_harness_drift_require_replay(tmp_path: Path) -> None:
    fingerprints = baselines.current_4_2d_fingerprints()
    proof = {
        "date": "2026-08-28",
        "runs": {"deterministe": "1" * 64, "outils": "2" * 64},
        "measures": {
            "deterministe": {"recall": 0.8, "cost_eur": 0.1, "latency_p50_ms": 120},
            "outils": {"recall": 0.9, "cost_eur": 0.2, "latency_p50_ms": 150},
        },
        "fingerprints": fingerprints,
    }
    path = tmp_path / "proof.json"
    for field in ("cases_hash", "source_hash", "plancher_digest", "harness_digest"):
        mutated = json.loads(json.dumps(proof))
        mutated["fingerprints"][field] = "0" * 64
        if fingerprints[field] == "0" * 64:
            mutated["fingerprints"][field] = "1" * 64
        path.write_text(json.dumps(mutated), encoding="utf-8")
        resolved = resolve_4_2d_proof(path)
        assert resolved["status"] == "replay_required" and resolved["measures"] is None
        assert resolved["reason"] == "empreintes divergentes"


def test_current_4_2d_fingerprints_reject_missing_witness_and_non_hex_hash(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baselines, "REPO_ROOT", tmp_path)
    with pytest.raises(BaselineError, match="témoin 4.2d absent"):
        baselines.current_4_2d_fingerprints()

    witness = tmp_path / "server" / "evals" / "cases" / "sinistre" / "s-bougie-canape.yaml"
    witness.parent.mkdir(parents=True)
    witness.write_text("id: s\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "manifest.json").write_text(json.dumps({
        "axa-lu-optihome-2017": {
            "source_hash": "z" * 64, "ingest_fingerprint": "1" * 64,
            "overlay_hash": None,
        }}), encoding="utf-8")
    monkeypatch.setattr(baselines, "cases_hash", lambda *args: "1" * 64)
    monkeypatch.setattr(baselines, "pipeline_digest", lambda: "2" * 64)
    monkeypatch.setattr(baselines, "prompts_digest", lambda: "3" * 64)
    monkeypatch.setattr(baselines, "harness_digest", lambda: "4" * 64)
    monkeypatch.setattr(
        baselines, "charger_plancher", lambda: type("Loaded", (), {"digest": "5" * 64})())
    with pytest.raises(BaselineError, match="mal formées"):
        baselines.current_4_2d_fingerprints()
