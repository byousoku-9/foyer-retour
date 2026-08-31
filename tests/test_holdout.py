from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from server.evals import holdout

SECRET = "secret-synthetique-armement"


def _case(case_id: str, suite: str) -> dict[str, object]:
    base: dict[str, object] = {
        "id": case_id,
        "suite": suite,
        "profile": "full",
        "question": f"Question synthétique {case_id}",
        "scenario": "Scénario synthétique sans donnée produit",
        "famille": "parcours" if suite == "guide" else "contradictoire",
        "expected": {"found": False, "complete": True},
        "truth": {
            "source": "codex",
            "countersigned_by": None,
            "validated_by_expert": False,
            "note": "Attente synthétique non contresignée.",
        },
        "mode_attendu": "faux_refus",
    }
    if suite == "sinistre":
        base["faits"] = {"description": f"Faits synthétiques {case_id}"}
    return base


def _lot() -> holdout.LotScelle:
    cases = [
        {"doc_id": None, "case": _case("guide-neutre", "guide")},
        *(
            {"doc_id": "document-neutre", "case": _case(f"sinistre-neutre-{index}", "sinistre")}
            for index in range(1, 4)
        ),
    ]
    return holdout.LotScelle.model_validate({
        "schema_version": 1,
        "cases": cases,
        "references": [{
            "case_id": "guide-neutre",
            "ordre_juste": ["étape synthétique"],
            "documents_cites": ["document synthétique"],
            "interlocuteur": "interlocuteur synthétique",
            "provenance": "codex",
            "countersigned_by": None,
        }],
    })


def _public(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    payload = tmp_path / "payload.enc"
    payload.write_bytes(b"ciphertext-neutre")
    receipt = {
        "schema_version": 1,
        "created_at": "2026-08-31T00:00:00+00:00",
        "cases_hash": "a" * 64,
        "schema_digest": "b" * 64,
        "parameters_digest": "c" * 64,
        "source_digests": {"guide": "d" * 64, "sinistre": "e" * 64},
        "payload_digest": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "payload_hmac": "f" * 64,
        "arm_token_digest": hashlib.sha256(hmac.new(
            SECRET.encode(), b"holdout-c/arm-token/v1", hashlib.sha256).digest()).hexdigest(),
        "payload_bytes": payload.stat().st_size,
        "counts": {"guide": 1, "sinistre": 3, "total": 4},
        "cipher": holdout.CIPHER,
        "key_service": "service-neutre",
        "key_account": "compte-neutre",
        "sealed": True,
        "executed": False,
        "isolation": {
            "sandbox": "sandbox-exec+codex-workspace",
            "code_readable": False,
            "cases_a_readable": False,
            "outside_allowlist_readable": False,
            "stdout_contains_cleartext": False,
            "stderr_contains_cleartext": False,
        },
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(holdout.json_canonique(receipt) + b"\n")
    lock = {
        "schema_version": 1,
        "receipt_digest": holdout.digest_json(receipt),
        "state": "sealed",
        "attempts_remaining": 1,
        "conditions": {condition: False for condition in holdout.CONDITIONS},
        "created_at": "2026-08-31T00:00:00+00:00",
        "armed_at": None,
        "consumed_at": None,
        "arm_token": None,
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_bytes(holdout.json_canonique(lock) + b"\n")
    return receipt_path, lock_path, payload, receipt


def test_un_lot_codex_full_reste_en_memoire_et_lie_son_hash() -> None:
    lot = _lot()
    fourni = lot.pour_runner(lot.cases_hash)
    assert len(fourni.cases) == 4
    assert fourni.snapshot.cases_hash == lot.cases_hash
    assert {case.truth.source for case in fourni.cases} == {"codex"}
    assert {case.profile for case in fourni.cases} == {"full"}


def test_un_lot_refuse_source_ou_contenu_hors_contrat() -> None:
    raw = _lot().model_dump(mode="json")
    raw["cases"][0]["case"]["truth"]["source"] = "claude"
    with pytest.raises(ValueError, match="truth.source=codex"):
        holdout.LotScelle.model_validate(raw)


def test_un_lot_refuse_une_reference_guide_dupliquee() -> None:
    raw = _lot().model_dump(mode="json")
    raw["references"].append(dict(raw["references"][0]))
    with pytest.raises(ValueError, match="exactement une référence"):
        holdout.LotScelle.model_validate(raw)


def test_le_recu_public_ne_rend_que_digests_comptages_et_etat(tmp_path: Path) -> None:
    receipt, lock, payload, _ = _public(tmp_path)
    public = holdout.verifier_public(receipt_path=receipt, lock_path=lock, payload_path=payload)
    assert public["lock_state"] == "sealed"
    assert public["executed"] is False
    assert public["attempts_remaining"] == 1
    assert "key_service" not in public and "payload_hmac" not in public
    assert set(public) == {
        "cases_hash", "schema_digest", "parameters_digest", "source_digests",
        "payload_digest", "counts", "sealed", "executed", "lock_state",
        "attempts_remaining", "isolation",
    }


def test_le_verrou_ferme_refuse_avant_toute_consommation(tmp_path: Path) -> None:
    _, lock, _, receipt = _public(tmp_path)
    with pytest.raises(holdout.RefusHoldout, match="encore fermé"):
        holdout.prendre_tentative_unique(lock, receipt["arm_token_digest"])
    assert not (tmp_path / "consumed.marker").exists()
    assert holdout.lire_verrou(lock).attempts_remaining == 1


def test_un_verrou_arme_par_edition_directe_refuse_sans_jeton_authentique(
        tmp_path: Path) -> None:
    receipt_path, lock, payload, receipt = _public(tmp_path)
    raw_lock = json.loads(lock.read_text("utf-8"))
    raw_lock.update({
        "state": "armed",
        "conditions": {condition: True for condition in holdout.CONDITIONS},
        "armed_at": "2026-09-01T00:00:00+00:00",
        "arm_token": "0" * 64,
    })
    lock.write_bytes(holdout.json_canonique(raw_lock) + b"\n")
    with pytest.raises(holdout.RefusHoldout, match="jeton authentique"):
        holdout.verifier_public(
            receipt_path=receipt_path, lock_path=lock, payload_path=payload)
    with pytest.raises(holdout.RefusHoldout, match="jeton authentique"):
        holdout.prendre_tentative_unique(lock, receipt["arm_token_digest"])
    assert not (tmp_path / "consumed.marker").exists()


def test_les_trois_preuves_arment_puis_lunique_prise_est_irreversible(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt, lock, _, raw_receipt = _public(tmp_path)
    monkeypatch.setattr(holdout, "_secret_keychain", lambda _receipt: SECRET)
    attestations = []
    for index, condition in enumerate(holdout.CONDITIONS):
        path = tmp_path / f"attestation-{index}.json"
        path.write_bytes(holdout.json_canonique({
            "schema_version": 1,
            "condition": condition,
            "completed": True,
            "evidence_digest": str(index + 1) * 64,
            "completed_at": "2026-09-01T00:00:00+00:00",
        }) + b"\n")
        attestations.append(path)
    holdout.armer(receipt_path=receipt, lock_path=lock, attestations=attestations)
    assert holdout.lire_verrou(lock).state == "armed"
    holdout.prendre_tentative_unique(lock, raw_receipt["arm_token_digest"])
    assert holdout.lire_verrou(lock).state == "consumed"
    with pytest.raises(holdout.RefusHoldout, match="encore fermé|déjà consommée"):
        holdout.prendre_tentative_unique(lock, raw_receipt["arm_token_digest"])


def test_le_chiffrement_authentifie_ne_materialise_aucun_fichier_clair(tmp_path: Path) -> None:
    secret = "secret-synthetique-" + "1" * 32
    clair = holdout.json_canonique(_lot().model_dump(mode="json"))
    chiffre = holdout._openssl(clair, secret, decrypt=False)
    mac_key = hmac.new(secret.encode(), b"holdout-c/mac/v1", hashlib.sha256).digest()
    receipt = holdout.RecuPublic.model_validate({
        "schema_version": 1,
        "created_at": "2026-08-31T00:00:00+00:00",
        "cases_hash": _lot().cases_hash,
        "schema_digest": "b" * 64,
        "parameters_digest": "c" * 64,
        "source_digests": {"guide": "d" * 64, "sinistre": "e" * 64},
        "payload_digest": hashlib.sha256(chiffre).hexdigest(),
        "payload_hmac": hmac.new(mac_key, chiffre, hashlib.sha256).hexdigest(),
        "arm_token_digest": "1" * 64,
        "payload_bytes": len(chiffre),
        "counts": {"guide": 1, "sinistre": 3, "total": 4},
        "cipher": holdout.CIPHER,
        "key_service": "service-neutre",
        "key_account": "compte-neutre",
        "sealed": True,
        "executed": False,
        "isolation": {
            "sandbox": "sandbox-exec+codex-workspace",
            "code_readable": False,
            "cases_a_readable": False,
            "outside_allowlist_readable": False,
            "stdout_contains_cleartext": False,
            "stderr_contains_cleartext": False,
        },
    })
    assert holdout.dechiffrer(chiffre, receipt, secret) == clair
    assert not list(tmp_path.iterdir())


def test_executer_consomme_avant_la_cle_et_transmet_le_lot_memoire(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt_path, lock_path, payload_path, receipt = _public(tmp_path)
    lot_raw = _lot().model_dump(mode="json")
    receipt["cases_hash"] = holdout.digest_json(sorted(
        lot_raw["cases"], key=lambda item: item["case"]["id"]))
    receipt_path.write_bytes(holdout.json_canonique(receipt) + b"\n")
    arm_token = hmac.new(
        SECRET.encode(), b"holdout-c/arm-token/v1", hashlib.sha256).hexdigest()
    raw_lock = json.loads(lock_path.read_text("utf-8"))
    raw_lock.update({
        "receipt_digest": holdout.digest_json(receipt),
        "state": "armed",
        "conditions": {condition: True for condition in holdout.CONDITIONS},
        "armed_at": "2026-09-01T00:00:00+00:00",
        "arm_token": arm_token,
    })
    lock_path.write_bytes(holdout.json_canonique(raw_lock) + b"\n")
    observed: dict[str, object] = {}

    def secret_after_consumption(_receipt: holdout.RecuPublic) -> str:
        observed["state_at_key"] = holdout.lire_verrou(lock_path).state
        return SECRET

    def fake_runner(argv: list[str], *, lot_cases_fourni: object) -> int:
        observed["argv"] = argv
        observed["lot"] = lot_cases_fourni
        return 7

    monkeypatch.setattr(holdout, "_secret_keychain", secret_after_consumption)
    monkeypatch.setattr(holdout, "dechiffrer", lambda *_args: holdout.json_canonique(lot_raw))
    monkeypatch.setattr(holdout.runner, "main", fake_runner)
    assert holdout.executer(
        receipt_path=receipt_path,
        lock_path=lock_path,
        payload_path=payload_path,
        runner_args=["--profile", "full"],
    ) == 7
    assert observed["state_at_key"] == "consumed"
    assert observed["argv"] == ["--profile", "full"]
    assert len(observed["lot"].cases) == 4


def test_le_recu_refuse_un_total_ou_une_source_non_lies(tmp_path: Path) -> None:
    receipt, _, _, raw = _public(tmp_path)
    raw["counts"]["total"] = 99
    receipt.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(holdout.RefusHoldout):
        holdout.lire_recu(receipt)
