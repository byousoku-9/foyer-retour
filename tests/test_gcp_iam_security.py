"""Les audits IAM du bootstrap doivent être exécutables et fail-closed hors réseau."""

from scripts import gcp_iam_security
from scripts.gcp_iam_security import (
    policy_has_unconditional_binding,
    provider_is_exact,
    service_account_is_active,
    service_account_wif_policy_ok,
    source_bucket_policy_ok,
)

ISSUER = "https://token.actions.githubusercontent.com"
ROLE_EXPR = "expression"
CONDITION = "condition"
READER = "serviceAccount:source-reader@example.iam.gserviceaccount.com"
WIF = "principalSet://iam.googleapis.com/pool/attribute.role/source-reader"


def _provider() -> dict:
    return {
        "state": "ACTIVE",
        "oidc": {"issuerUri": ISSUER},
        "attributeCondition": CONDITION,
        "attributeMapping": {
            "google.subject": "assertion.sub",
            "attribute.repository": "assertion.repository",
            "attribute.actor": "assertion.actor",
            "attribute.role": ROLE_EXPR,
        },
    }


def test_provider_requires_exact_mapping_condition_issuer_and_active_state() -> None:
    assert provider_is_exact(
        _provider(), condition=CONDITION, role_expression=ROLE_EXPR, issuer=ISSUER
    )
    for path, divergent in (
        ("attributeCondition", "other"),
        ("state", "DELETED"),
        ("oidc", {"issuerUri": "https://evil.invalid"}),
        ("attributeMapping", {"google.subject": "assertion.sub"}),
    ):
        state = _provider() | {path: divergent}
        assert not provider_is_exact(
            state, condition=CONDITION, role_expression=ROLE_EXPR, issuer=ISSUER
        )


def test_binding_checks_reject_conditions_and_extra_sa_principals() -> None:
    exact = {"bindings": [{"role": "roles/iam.workloadIdentityUser", "members": [WIF]}]}
    assert policy_has_unconditional_binding(
        exact, role="roles/iam.workloadIdentityUser", member=WIF
    )
    assert service_account_wif_policy_ok(exact, member=WIF)
    conditioned = {
        "bindings": [{
            "role": "roles/iam.workloadIdentityUser",
            "members": [WIF],
            "condition": {"expression": "false"},
        }]
    }
    assert not policy_has_unconditional_binding(
        conditioned, role="roles/iam.workloadIdentityUser", member=WIF
    )
    assert not service_account_wif_policy_ok(conditioned, member=WIF)
    assert not service_account_wif_policy_ok(
        {"bindings": exact["bindings"] + [{"role": "roles/viewer", "members": ["user:x"]}]},
        member=WIF,
    )


def test_source_bucket_policy_rejects_public_extra_or_conditioned_reader_rights() -> None:
    exact = {"bindings": [{"role": "roles/storage.objectViewer", "members": [READER]}]}
    assert source_bucket_policy_ok(exact, reader=READER)
    assert not source_bucket_policy_ok(
        {"bindings": exact["bindings"] + [{"role": "roles/storage.objectViewer", "members": ["allUsers"]}]},
        reader=READER,
    )
    assert not source_bucket_policy_ok(
        {"bindings": [{"role": "roles/storage.admin", "members": [READER]}]}, reader=READER
    )
    assert not source_bucket_policy_ok(
        {"bindings": [{
            "role": "roles/storage.objectViewer",
            "members": [READER],
            "condition": {"expression": "false"},
        }]},
        reader=READER,
    )


def test_service_account_requires_an_email_and_must_not_be_disabled() -> None:
    assert service_account_is_active({"email": "reader@example.iam.gserviceaccount.com"})
    assert not service_account_is_active({"email": "reader@example.iam.gserviceaccount.com", "disabled": True})
    assert not service_account_is_active({})


def test_cli_returns_distinct_status_for_missing_binding_and_invalid_json(monkeypatch) -> None:
    import io
    import json

    monkeypatch.setattr(gcp_iam_security.sys, "stdin", io.StringIO(json.dumps({"bindings": []})))
    assert gcp_iam_security.main(["has-binding", "roles/iam.workloadIdentityUser", WIF]) == 1
    # GCP omet `bindings` sur une policy valide et vide d'un compte fraîchement créé.
    monkeypatch.setattr(gcp_iam_security.sys, "stdin", io.StringIO('{"etag": "ACAB"}'))
    assert gcp_iam_security.main(["has-binding", "roles/iam.workloadIdentityUser", WIF]) == 1
    monkeypatch.setattr(gcp_iam_security.sys, "stdin", io.StringIO("{}"))
    assert gcp_iam_security.main(["has-binding", "roles/iam.workloadIdentityUser", WIF]) == 2
    monkeypatch.setattr(gcp_iam_security.sys, "stdin", io.StringIO("not-json"))
    assert gcp_iam_security.main(["has-binding", "roles/iam.workloadIdentityUser", WIF]) == 2
    monkeypatch.setattr(gcp_iam_security.sys, "stdin", io.StringIO('{"bindings": {}}'))
    assert gcp_iam_security.main(["has-binding", "roles/iam.workloadIdentityUser", WIF]) == 2
