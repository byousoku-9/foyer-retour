"""Fonction totale de fermeture de l'Epic 5.

Ce module ne contient aucun résultat produit : il transforme exclusivement des états de tests,
branches et preuves content-addressées en décisions dérivées et fail-closed.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROW_IDS: tuple[str, ...] = (
    "P-01", "P-02", "A-01", "A-02", "A-03", "A-04", "A-05",
    "T-01", "T-02", "T-03", "R-01", "R-02", "R-03", "R-04",
    "R-05", "R-06", "R-07", "R-08", "R-09", "R-10", "R-11",
    "M-01", "M-02", "O-01", "O-02", "V-01", "V-02", "V-03",
    "V-04", "V-05",
)
OTHER_ROW_IDS: tuple[str, ...] = tuple(row for row in ROW_IDS if row != "V-05")
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    **{row: () for row in OTHER_ROW_IDS},
    "V-05": OTHER_ROW_IDS,
}


def canonical_required_refs(key: BranchKey) -> frozenset[str]:
    """Sélection fermée de la matrice ; l'appelant ne peut pas redéfinir son dénominateur."""
    row = key.row_id
    special: dict[str, tuple[str, ...]] = {
        "A-01": ("H:A-01", "H:A-04", "H:A-01-A04-NONCITABLE",
                 "REF_ORACLE:A-01-A-04", "L:A-01", "L:A-04", "L:A-01-A04-NONCITABLE"),
        "A-04": ("H:A-01", "H:A-04", "H:A-01-A04-NONCITABLE",
                 "REF_ORACLE:A-01-A-04", "L:A-01", "L:A-04", "L:A-01-A04-NONCITABLE"),
        "T-03": ("H:T-03", "N_A:T-03"),
        "R-09": ("H:R-09", "N_A:R-09"),
        "M-02": ("H:M-02", "REF_ORACLE:M-02", "REF_PLATEAU:M-02", "L:M-02"),
        "V-04": ("H:V-04", "BUILDER:V-04", "BUILD_AUTO:V-04", "CANDIDATE:V-04",
                 "CHECKLIST:V-04", "REVIEW:V-04", "N_A:V-04"),
        "V-05": ("H:V-05-DAG", "H:V-05", "H:STATE-HERMETIC-EVIDENCE", "L:V-05"),
    }
    if row == "T-01":
        mode_ref = ("T2_ISOLATION:T-01" if key.t2_eligibility_mode == "ISOLATED"
                    else "LEGAL_KINDS_INVENTORY:T-01")
        return frozenset(("H:T-01", "H:T-01-RENVOI-T2", mode_ref,
                          "L:T-01", "L:T-01-RENVOI-T2"))
    if row == "R-11":
        return frozenset(("H:R-11-TRUE", "ARCH:R-11", "REVIEW_ARCH:R-11")
                         if key.trigger_branch == "TRUE"
                         else ("H:R-11-FALSE", "NO_CHANGE:R-11"))
    if row == "V-03":
        return frozenset(("H:V-03-TRUE", "DOC_UPDATE:V-03")
                         if key.trigger_branch == "TRUE"
                         else ("H:V-03-FALSE", "NO_CHANGE:V-03"))
    if row in special:
        return frozenset(special[row])
    return frozenset((f"H:{row}", f"L:{row}"))


class EvidenceStatus(StrEnum):
    MISSING = "MISSING"
    INVALID = "INVALID"
    RESOLVED = "RESOLVED"


class Closure(StrEnum):
    OPEN = "OPEN"
    HERMITIC_GREEN = "HERMITIC_GREEN"
    LIVE_GREEN = "LIVE_GREEN"
    CLOSED = "CLOSED"
    BLOCKED_INFRA = "BLOCKED_INFRA"


class BranchKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_id: str
    trigger_branch: Literal["TRUE", "FALSE"]
    t2_eligibility_mode: Literal["NONE", "ISOLATED", "LEGAL_KINDS_INVENTORY"] = "NONE"

    @model_validator(mode="after")
    def _canonical(self) -> BranchKey:
        if self.row_id not in ROW_IDS:
            raise ValueError("row_id inconnu")
        if self.row_id not in {"R-11", "V-03"} and self.trigger_branch != "TRUE":
            raise ValueError("seules R-11 et V-03 possèdent une branche FALSE")
        if self.row_id == "T-01" and self.t2_eligibility_mode == "NONE":
            raise ValueError("T-01 exige un mode T2 explicite")
        if self.row_id != "T-01" and self.t2_eligibility_mode != "NONE":
            raise ValueError("mode T2 interdit hors T-01")
        return self


def canonical_branch_keys() -> tuple[BranchKey, ...]:
    """Univers fermé des branches, y compris les alternatives non sélectionnées du run."""
    keys: list[BranchKey] = []
    for row_id in ROW_IDS:
        if row_id == "T-01":
            keys.extend((
                BranchKey(row_id=row_id, trigger_branch="TRUE",
                          t2_eligibility_mode="ISOLATED"),
                BranchKey(row_id=row_id, trigger_branch="TRUE",
                          t2_eligibility_mode="LEGAL_KINDS_INVENTORY"),
            ))
        elif row_id in {"R-11", "V-03"}:
            keys.extend((BranchKey(row_id=row_id, trigger_branch="FALSE"),
                         BranchKey(row_id=row_id, trigger_branch="TRUE")))
        else:
            keys.append(BranchKey(row_id=row_id, trigger_branch="TRUE"))
    return tuple(keys)


class EvidenceOwner(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_class: Literal["HERMETIC", "LIVE"]
    ownership_kind: Literal["COMMON", "ALTERNATIVE_EXCLUSIVE"]
    owners: frozenset[BranchKey] = Field(min_length=1)
    exclusive_scope: frozenset[BranchKey] = frozenset()

    @model_validator(mode="after")
    def _ownership(self) -> EvidenceOwner:
        if self.ownership_kind == "ALTERNATIVE_EXCLUSIVE":
            if len(self.owners) != 1 or not self.owners <= self.exclusive_scope:
                raise ValueError("une preuve exclusive exige un propriétaire unique dans sa portée")
        elif self.exclusive_scope:
            raise ValueError("une preuve COMMON ne porte pas de portée exclusive")
        return self


class EvidenceArtifact(BaseModel):
    """Preuve liée à une ref, un run et un producteur autoritaire.

    Le digest couvre l'enveloppe entière et l'identifiant est content-addressé : une preuve ne
    peut donc être empruntée à une autre ref/run ni s'auto-déclarer valide avec le hash arbitraire
    de son seul contenu.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=1)
    run_uid: str = Field(min_length=1)
    source: Literal["HERMETIC_RUNNER", "LIVE_ORCHESTRATOR"]
    artifact_uid: str = Field(pattern=r"^evidence-v1:[0-9a-f]{64}$")
    payload: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def _canonical_bytes(self) -> bytes:
        return json.dumps(
            {"payload": self.payload, "ref": self.ref, "run_uid": self.run_uid,
             "source": self.source},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")

    @property
    def verified(self) -> bool:
        digest = hashlib.sha256(self._canonical_bytes()).hexdigest()
        return self.sha256 == digest and self.artifact_uid == f"evidence-v1:{digest}"

    def verified_for(self, ref: str, evidence_class: Literal["HERMETIC", "LIVE"]) -> bool:
        expected_source = ("HERMETIC_RUNNER" if evidence_class == "HERMETIC"
                           else "LIVE_ORCHESTRATOR")
        return self.verified and self.ref == ref and self.source == expected_source

    @classmethod
    def create(cls, *, ref: str, run_uid: str,
               source: Literal["HERMETIC_RUNNER", "LIVE_ORCHESTRATOR"],
               payload: str) -> EvidenceArtifact:
        raw = json.dumps(
            {"payload": payload, "ref": ref, "run_uid": run_uid, "source": source},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        return cls(ref=ref, run_uid=run_uid, source=source,
                   artifact_uid=f"evidence-v1:{digest}", payload=payload, sha256=digest)


def _owner_payload(owner: EvidenceOwner) -> dict[str, object]:
    def key(branch: BranchKey) -> tuple[str, str, str]:
        return branch.row_id, branch.trigger_branch, branch.t2_eligibility_mode

    return {
        "evidence_class": owner.evidence_class,
        "ownership_kind": owner.ownership_kind,
        "owners": [branch.model_dump(mode="json") for branch in sorted(owner.owners, key=key)],
        "exclusive_scope": [branch.model_dump(mode="json")
                            for branch in sorted(owner.exclusive_scope, key=key)],
    }


def canonical_registry_hash(registry: dict[str, EvidenceOwner]) -> str:
    payload = {
        ref: _owner_payload(owner)
        for ref, owner in sorted(registry.items())
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def canonical_registry() -> dict[str, EvidenceOwner]:
    """Registre d'ownership fermé dérivé des 34 branches canoniques."""
    keys = canonical_branch_keys()
    owners_by_ref: dict[str, set[BranchKey]] = {}
    for key in keys:
        for ref in canonical_required_refs(key):
            owners_by_ref.setdefault(ref, set()).add(key)
    t2_scope = frozenset(key for key in keys if key.row_id == "T-01")
    conditional_scopes = {
        row: frozenset(key for key in keys if key.row_id == row)
        for row in ("R-11", "V-03")
    }
    registry: dict[str, EvidenceOwner] = {}
    for ref, owners in sorted(owners_by_ref.items()):
        owner_set = frozenset(owners)
        modal = ref in {"T2_ISOLATION:T-01", "LEGAL_KINDS_INVENTORY:T-01"}
        conditional = next((row for row in conditional_scopes
                            if any(key.row_id == row for key in owner_set)), None)
        exclusive = modal or conditional is not None
        scope = (t2_scope if modal else
                 conditional_scopes[conditional] if conditional is not None else frozenset())
        registry[ref] = EvidenceOwner(
            evidence_class="LIVE" if ref.startswith("L:") else "HERMETIC",
            ownership_kind="ALTERNATIVE_EXCLUSIVE" if exclusive else "COMMON",
            owners=owner_set, exclusive_scope=scope,
        )
    return registry


def registry_is_canonical(registry: dict[str, EvidenceOwner], registry_hash: str) -> bool:
    expected = canonical_registry()
    return registry == expected and registry_hash == canonical_registry_hash(expected)


class ClosureInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    branch_key: BranchKey
    trigger: Literal["UNRESOLVED", "TRUE", "FALSE"]
    tests: tuple[Literal["PENDING", "GREEN", "RED"], ...]
    live_mode: Literal["REQUIRED", "N_A", "CONDITIONAL"]
    live_branch: Literal[
        "PENDING", "SATISFIED_NO_LIVE", "SATISFIED_LIVE", "FAILED", "INFRA_EXHAUSTED",
    ]
    live_justification: str | None = None
    gate_rule: Literal["UNRESOLVED", "TRUE", "FALSE"]
    provided_ref_list: tuple[str, ...]
    required_refs: frozenset[str]
    evidence_status: dict[str, EvidenceStatus]
    evidence_artifacts: dict[str, EvidenceArtifact]
    registry: dict[str, EvidenceOwner]
    registry_hash: str
    hermetic_selection: Literal["NON_EMPTY", "EXPLICIT_EMPTY"] | None = None


class ClosureResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selection_valid: bool
    evidence_ok: bool
    hermetic_evidence_resolved: bool
    hermetic_ready: bool
    trigger_ok: bool
    tests_ok: bool
    branch_ok: bool
    rule_ok: bool
    gate: bool
    closure: Closure


class QualityClosureRunInput(BaseModel):
    """Artefact fermé consommé par le runner ; aucune ligne ne peut être omise ou ajoutée."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dependencies: dict[str, tuple[str, ...]]
    rows: tuple[ClosureInput, ...]

    @model_validator(mode="after")
    def _closed_world(self) -> QualityClosureRunInput:
        validate_canonical_dag(self.dependencies)
        ids = [row.branch_key.row_id for row in self.rows]
        if len(ids) != len(ROW_IDS) or set(ids) != set(ROW_IDS):
            raise ValueError("l'artefact doit porter exactement une entrée pour chacune des 30 lignes")
        if len(ids) != len(set(ids)):
            raise ValueError("une ligne de clôture ne peut apparaître deux fois")
        registry_hashes = {row.registry_hash for row in self.rows}
        registries = {
            json.dumps(
                {ref: _owner_payload(owner) for ref, owner in sorted(row.registry.items())},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            for row in self.rows
        }
        if len(registry_hashes) != 1 or len(registries) != 1:
            raise ValueError("les 30 lignes doivent consommer le même registre fermé")
        registry = self.rows[0].registry
        registry_hash = self.rows[0].registry_hash
        if not registry_is_canonical(registry, registry_hash):
            raise ValueError("REF_OWNERSHIP_REGISTRY incomplet, surnuméraire ou non canonique")
        artifacts_by_uid: dict[str, EvidenceArtifact] = {}
        run_uids: set[str] = set()
        for row in self.rows:
            for ref, artifact in row.evidence_artifacts.items():
                if artifact.ref != ref:
                    raise ValueError("une preuve ne peut être empruntée à une autre ref")
                previous = artifacts_by_uid.setdefault(artifact.artifact_uid, artifact)
                if previous.ref != ref:
                    raise ValueError("un artefact content-addressé ne peut couvrir plusieurs refs")
                run_uids.add(artifact.run_uid)
        if len(run_uids) > 1:
            raise ValueError("toutes les preuves doivent appartenir au même run")
        return self


class QualityClosureRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    complete_input: bool
    rows: dict[str, ClosureResult]
    v05_gate: bool


def evaluate_row(value: ClosureInput) -> ClosureResult:
    """Évalue une ligne sans accepter de ``gate`` ou ``closure`` fourni par l'appelant."""
    key = value.branch_key
    trigger_ok = value.trigger != "UNRESOLVED" and value.trigger == key.trigger_branch
    # Une liste vide ne prouve aucun test : elle est OPEN, jamais verte par vérité vacante.
    tests_ok = bool(value.tests) and all(status == "GREEN" for status in value.tests)
    branch_ok = (
        (value.live_mode == "REQUIRED" and value.live_branch == "SATISFIED_LIVE")
        or (value.live_mode == "N_A" and value.live_branch == "SATISFIED_NO_LIVE"
            and bool(value.live_justification and value.live_justification.strip()))
        or (value.live_mode == "CONDITIONAL"
            and value.live_branch in {"SATISFIED_LIVE", "SATISFIED_NO_LIVE"}
            and not (
                value.live_branch == "SATISFIED_NO_LIVE"
                and any(value.registry.get(ref) is not None
                        and value.registry[ref].evidence_class == "LIVE"
                        for ref in value.required_refs)
            ))
    )
    registry_valid = registry_is_canonical(value.registry, value.registry_hash)
    provided = set(value.provided_ref_list)
    selection_valid = (
        registry_valid
        and len(provided) == len(value.provided_ref_list)
        and value.required_refs == canonical_required_refs(key)
        and provided == set(value.required_refs)
        and not (
            value.live_mode == "N_A"
            and any(value.registry[ref].evidence_class == "LIVE" for ref in provided
                    if ref in value.registry)
        )
        and all(
            ref in value.registry
            and (
                (value.registry[ref].ownership_kind == "COMMON"
                 and key in value.registry[ref].owners)
                or (value.registry[ref].ownership_kind == "ALTERNATIVE_EXCLUSIVE"
                    and value.registry[ref].owners == frozenset({key}))
            )
            for ref in provided
        )
    )
    all_resolved = all(
        value.evidence_status.get(ref) == EvidenceStatus.RESOLVED
        and ref in value.evidence_artifacts
        and ref in value.registry
        and value.evidence_artifacts[ref].verified_for(ref, value.registry[ref].evidence_class)
        for ref in value.required_refs
    )
    evidence_ok = selection_valid and all_resolved
    hermetic_refs = {
        ref for ref in value.required_refs
        if ref in value.registry and value.registry[ref].evidence_class == "HERMETIC"
    }
    selection_coherent = (
        (value.hermetic_selection == "NON_EMPTY" and bool(hermetic_refs))
        or (value.hermetic_selection == "EXPLICIT_EMPTY" and not hermetic_refs)
    )
    hermetic_evidence_resolved = (
        selection_valid and selection_coherent
        and all(value.evidence_status.get(ref) == EvidenceStatus.RESOLVED
                and ref in value.evidence_artifacts
                and ref in value.registry
                and value.evidence_artifacts[ref].verified_for(
                    ref, value.registry[ref].evidence_class)
                for ref in hermetic_refs)
    )
    hermetic_ready = trigger_ok and tests_ok and hermetic_evidence_resolved
    rule_ok = value.gate_rule == "TRUE"
    gate = (selection_valid and hermetic_evidence_resolved and trigger_ok and tests_ok
            and branch_ok and evidence_ok and rule_ok)
    if not hermetic_ready:
        closure = Closure.OPEN
    elif gate:
        closure = Closure.CLOSED
    elif value.live_branch == "INFRA_EXHAUSTED":
        closure = Closure.BLOCKED_INFRA
    elif value.live_branch == "SATISFIED_LIVE":
        closure = Closure.LIVE_GREEN
    else:
        closure = Closure.HERMITIC_GREEN
    return ClosureResult(
        selection_valid=selection_valid, evidence_ok=evidence_ok,
        hermetic_evidence_resolved=hermetic_evidence_resolved,
        hermetic_ready=hermetic_ready, trigger_ok=trigger_ok, tests_ok=tests_ok,
        branch_ok=branch_ok, rule_ok=rule_ok, gate=gate, closure=closure,
    )


def validate_canonical_dag(dependencies: dict[str, tuple[str, ...]] = DEPENDENCIES) -> None:
    """Refuse tout ID, dépendance supplémentaire, cycle ou lien retour depuis V-05."""
    if set(dependencies) != set(ROW_IDS):
        raise ValueError("le DAG doit contenir exactement les 30 lignes")
    if dependencies.get("V-05") != OTHER_ROW_IDS:
        raise ValueError("V-05 doit dépendre exactement des 29 autres lignes")
    if any(dependencies[row] for row in OTHER_ROW_IDS):
        raise ValueError("les 29 lignes ne peuvent dépendre de V-05 ni de l'agrégat")


def evaluate_v05(other_gates: dict[str, bool], own_result: ClosureResult) -> bool:
    """Conjonction dérivée : 29 gates canoniques et seules preuves propres V-05."""
    if set(other_gates) != set(OTHER_ROW_IDS):
        return False
    return all(other_gates.values()) and own_result.gate


def evaluate_run(value: QualityClosureRunInput) -> QualityClosureRunResult:
    """Évalue les 30 lignes et remplace le gate local V-05 par la conjonction canonique."""
    by_id = {row.branch_key.row_id: (row, evaluate_row(row)) for row in value.rows}
    other_gates = {row_id: by_id[row_id][1].gate for row_id in OTHER_ROW_IDS}
    v05_input, v05_local = by_id["V-05"]
    v05_gate = evaluate_v05(other_gates, v05_local)
    if v05_gate:
        v05_closure = Closure.CLOSED
    elif not v05_local.hermetic_ready or not all(other_gates.values()):
        v05_closure = Closure.OPEN
    elif v05_input.live_branch == "INFRA_EXHAUSTED":
        v05_closure = Closure.BLOCKED_INFRA
    elif v05_input.live_branch == "SATISFIED_LIVE":
        v05_closure = Closure.LIVE_GREEN
    else:
        v05_closure = Closure.HERMITIC_GREEN
    results = {row_id: result for row_id, (_row, result) in by_id.items()}
    results["V-05"] = v05_local.model_copy(update={"gate": v05_gate, "closure": v05_closure})
    return QualityClosureRunResult(complete_input=True, rows=results, v05_gate=v05_gate)


def open_run_result() -> QualityClosureRunResult:
    """Projection fail-closed d'un artefact absent ou invalide, sans fabriquer d'entrée verte."""
    open_row = ClosureResult(
        selection_valid=False, evidence_ok=False, hermetic_evidence_resolved=False,
        hermetic_ready=False, trigger_ok=False, tests_ok=False, branch_ok=False,
        rule_ok=False, gate=False, closure=Closure.OPEN,
    )
    return QualityClosureRunResult(
        complete_input=False, rows={row_id: open_row for row_id in ROW_IDS}, v05_gate=False)


validate_canonical_dag()
