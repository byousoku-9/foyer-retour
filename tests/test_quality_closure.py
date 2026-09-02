from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from server.evals.quality_closure import (DEPENDENCIES, OTHER_ROW_IDS, ROW_IDS, BranchKey,
                                          Closure, ClosureInput, EvidenceArtifact, EvidenceStatus,
                                          QualityClosureRunInput, canonical_registry_hash,
                                          canonical_registry, canonical_required_refs,
                                          evaluate_row, evaluate_run,
                                          evaluate_v05, open_run_result, validate_canonical_dag)


def _input(row_id: str = "P-01", *, resolved: bool = True) -> ClosureInput:
    key = BranchKey(
        row_id=row_id, trigger_branch="TRUE",
        t2_eligibility_mode="ISOLATED" if row_id == "T-01" else "NONE",
    )
    refs = canonical_required_refs(key)
    registry = canonical_registry()
    artifacts = {
        ref: EvidenceArtifact.create(
            ref=ref, run_uid="run-hermetique-1",
            source=("LIVE_ORCHESTRATOR" if registry[ref].evidence_class == "LIVE"
                    else "HERMETIC_RUNNER"),
            payload=f"preuve:{ref}",
        ) for ref in refs
    }
    requires_live = any(registry[ref].evidence_class == "LIVE" for ref in refs)
    return ClosureInput(
        branch_key=key, trigger="TRUE", tests=("GREEN",),
        live_mode="REQUIRED" if requires_live else "N_A",
        live_branch="SATISFIED_LIVE" if requires_live else "SATISFIED_NO_LIVE",
        live_justification=None if requires_live else "preuve hermétique",
        gate_rule="TRUE", provided_ref_list=tuple(sorted(refs)), required_refs=refs,
        evidence_status={ref: (EvidenceStatus.RESOLVED if resolved else EvidenceStatus.MISSING)
                         for ref in refs},
        evidence_artifacts=artifacts,
        registry=registry, registry_hash=canonical_registry_hash(registry),
        hermetic_selection="NON_EMPTY",
    )


def test_canonical_dag_has_exactly_30_rows_and_v05_depends_on_the_other_29() -> None:
    validate_canonical_dag()
    assert len(ROW_IDS) == 30
    assert len(OTHER_ROW_IDS) == 29
    assert DEPENDENCIES["V-05"] == OTHER_ROW_IDS
    assert all(not DEPENDENCIES[row] for row in OTHER_ROW_IDS)


def test_missing_evidence_keeps_a_row_open_even_if_every_other_input_is_green() -> None:
    result = evaluate_row(_input(resolved=False))
    assert result.selection_valid
    assert not result.hermetic_evidence_resolved
    assert not result.gate
    assert result.closure == Closure.OPEN


def test_une_liste_de_tests_vide_ne_se_ferme_jamais_par_verite_vacante() -> None:
    result = evaluate_row(_input().model_copy(update={"tests": ()}))
    assert not result.tests_ok and not result.gate and result.closure == Closure.OPEN


def test_unknown_or_duplicated_selection_is_fail_closed() -> None:
    base = _input()
    for refs in (("H:P-01", "H:P-01"), ("H:P-01", "H:unknown")):
        result = evaluate_row(base.model_copy(update={"provided_ref_list": refs}))
        assert not result.selection_valid
        assert not result.gate
        assert result.closure == Closure.OPEN


def test_v05_stays_false_when_its_own_proof_is_missing() -> None:
    own = evaluate_row(_input("V-05", resolved=False))
    assert not evaluate_v05({row: True for row in OTHER_ROW_IDS}, own)


def test_resolved_sans_artefact_verifie_reste_open() -> None:
    base = _input()
    ref = next(
        ref for ref in base.required_refs
        if base.registry[ref].evidence_class == "HERMETIC"
    )
    artifacts = dict(base.evidence_artifacts)
    artifacts[ref] = artifacts[ref].model_copy(update={"sha256": "0" * 64})
    result = evaluate_row(base.model_copy(update={"evidence_artifacts": artifacts}))
    assert not result.evidence_ok and not result.gate and result.closure == Closure.OPEN


def test_preuve_ne_peut_etre_empruntee_a_une_autre_ref_ou_source() -> None:
    base = _input()
    ref = next(ref for ref in base.required_refs
               if base.registry[ref].evidence_class == "HERMETIC")
    artifact = base.evidence_artifacts[ref]
    for borrowed in (
        artifact.model_copy(update={"ref": "H:AUTRE"}),
        artifact.model_copy(update={"source": "LIVE_ORCHESTRATOR"}),
        artifact.model_copy(update={"artifact_uid": "evidence-v1:" + "0" * 64}),
    ):
        result = evaluate_row(base.model_copy(update={
            "evidence_artifacts": {**base.evidence_artifacts, ref: borrowed},
        }))
        assert not result.evidence_ok and not result.gate


def test_hash_du_registre_est_stable_quel_que_soit_pythonhashseed() -> None:
    code = (
        "from server.evals.quality_closure import canonical_registry,canonical_registry_hash;"
        "print(canonical_registry_hash(canonical_registry()))"
    )
    hashes = set()
    for seed in ("1", "2", "314159"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "ANTHROPIC_API_KEY": ""}
        hashes.add(subprocess.check_output(
            [sys.executable, "-c", code], cwd=Path(__file__).parents[1], env=env, text=True,
        ).strip())
    assert len(hashes) == 1


def test_preuve_live_est_interdite_sous_no_live() -> None:
    base = _input("P-01")
    result = evaluate_row(base.model_copy(update={
        "live_mode": "N_A", "live_branch": "SATISFIED_NO_LIVE",
        "live_justification": "aucun live autorisé",
    }))
    assert not result.selection_valid and result.closure == Closure.OPEN


def test_branche_conditionnelle_avec_refs_live_ne_peut_pas_satisfaire_sans_live() -> None:
    base = _input("P-01")
    result = evaluate_row(base.model_copy(update={
        "live_mode": "CONDITIONAL", "live_branch": "SATISFIED_NO_LIVE",
        "live_justification": "condition fausse déclarée",
    }))
    assert result.selection_valid
    assert not result.branch_ok and not result.gate


def test_full_run_derives_v05_from_all_other_rows_and_its_own_proofs() -> None:
    initial = tuple(_input(row) for row in ROW_IDS)
    registry = canonical_registry()
    registry_hash = canonical_registry_hash(registry)
    rows = tuple(row.model_copy(update={"registry": registry, "registry_hash": registry_hash})
                 for row in initial)
    result = evaluate_run(QualityClosureRunInput(dependencies=DEPENDENCIES, rows=rows))
    assert result.complete_input
    assert result.v05_gate
    assert result.rows["V-05"].closure == Closure.CLOSED


def test_v05_reste_open_si_une_des_29_dependances_est_rouge() -> None:
    rows = list(_input(row) for row in ROW_IDS)
    index = next(i for i, row in enumerate(rows) if row.branch_key.row_id == "P-01")
    rows[index] = rows[index].model_copy(update={"tests": ("RED",)})
    registry = canonical_registry()
    registry_hash = canonical_registry_hash(registry)
    coherent = tuple(row.model_copy(update={"registry": registry, "registry_hash": registry_hash})
                     for row in rows)
    result = evaluate_run(QualityClosureRunInput(dependencies=DEPENDENCIES, rows=coherent))
    assert not result.v05_gate and result.rows["V-05"].closure == Closure.OPEN


def test_absent_artifact_publishes_thirty_open_rows() -> None:
    result = open_run_result()
    assert not result.complete_input
    assert not result.v05_gate
    assert set(result.rows) == set(ROW_IDS)
    assert all(row.closure == Closure.OPEN and not row.gate for row in result.rows.values())


def test_runner_charge_absent_invalide_rouge_et_vert_sans_auto_declaration(
        tmp_path: Path) -> None:
    from server.evals.run import charger_cloture_qualite

    path = tmp_path / "quality-evidence.json"
    assert not charger_cloture_qualite(path).complete_input
    path.write_text("{invalide", encoding="utf-8")
    assert not charger_cloture_qualite(path).complete_input

    rows = tuple(_input(row) for row in ROW_IDS)
    green = QualityClosureRunInput(dependencies=DEPENDENCIES, rows=rows)
    red_rows = list(rows)
    red_rows[0] = red_rows[0].model_copy(update={"tests": ("RED",)})
    red = QualityClosureRunInput(dependencies=DEPENDENCIES, rows=tuple(red_rows))
    path.write_text(json.dumps(red.model_dump(mode="json")), encoding="utf-8")
    red_result = charger_cloture_qualite(path)
    assert red_result.complete_input and not red_result.v05_gate
    assert red_result.rows["V-05"].closure == Closure.OPEN

    path.write_text(json.dumps(green.model_dump(mode="json")), encoding="utf-8")
    green_result = charger_cloture_qualite(path)
    assert green_result.complete_input and green_result.v05_gate
    assert green_result.rows["V-05"].closure == Closure.CLOSED
