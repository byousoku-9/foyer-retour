"""Validateurs purs des policies et identités GCP réconciliées par le bootstrap."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def provider_is_exact(
    state: dict[str, Any], *, condition: str, role_expression: str, issuer: str
) -> bool:
    expected_mapping = {
        "google.subject": "assertion.sub",
        "attribute.repository": "assertion.repository",
        "attribute.actor": "assertion.actor",
        "attribute.role": role_expression,
    }
    return (
        state.get("state") == "ACTIVE"
        and state.get("oidc", {}).get("issuerUri") == issuer
        and state.get("attributeCondition") == condition
        and state.get("attributeMapping") == expected_mapping
    )


def policy_has_unconditional_binding(policy: dict[str, Any], *, role: str, member: str) -> bool:
    return any(
        binding.get("role") == role
        and binding.get("condition") is None
        and member in binding.get("members", [])
        for binding in policy.get("bindings", [])
    )


def service_account_wif_policy_ok(policy: dict[str, Any], *, member: str) -> bool:
    bindings = policy.get("bindings", [])
    actual = [
        (binding.get("role"), principal, binding.get("condition"))
        for binding in bindings
        for principal in binding.get("members", [])
    ]
    return actual == [("roles/iam.workloadIdentityUser", member, None)]


def source_bucket_policy_ok(policy: dict[str, Any], *, reader: str) -> bool:
    bindings = policy.get("bindings", [])
    reader_bindings = [
        (binding.get("role"), principal, binding.get("condition"))
        for binding in bindings
        for principal in binding.get("members", [])
        if principal == reader
    ]
    public = [
        principal
        for binding in bindings
        for principal in binding.get("members", [])
        if principal in {"allUsers", "allAuthenticatedUsers"}
    ]
    return reader_bindings == [("roles/storage.objectViewer", reader, None)] and not public


def service_account_is_active(state: dict[str, Any]) -> bool:
    return state.get("disabled") is not True and bool(state.get("email"))


def _policy_understood(policy: dict[str, Any]) -> bool:
    # `gcloud ... get-iam-policy --format=json` omet entièrement `bindings` pour une policy
    # valide mais vide et ne rend alors que son `etag`. C'est précisément l'état initial d'un
    # compte de service fraîchement créé : il doit signifier « binding absent » (status 1), pas
    # « JSON incompréhensible » (status 2). Un objet arbitraire vide reste en revanche refusé.
    if "bindings" not in policy:
        return isinstance(policy.get("etag"), str)
    bindings = policy["bindings"]
    return isinstance(bindings, list) and all(
        isinstance(binding, dict) and isinstance(binding.get("members", []), list)
        for binding in bindings
    )


def _provider_understood(state: dict[str, Any]) -> bool:
    return (
        isinstance(state.get("state"), str)
        and isinstance(state.get("oidc"), dict)
        and isinstance(state["oidc"].get("issuerUri"), str)
        and isinstance(state.get("attributeCondition"), str)
        and isinstance(state.get("attributeMapping"), dict)
    )


def _load_stdin() -> dict[str, Any] | None:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    provider = sub.add_parser("provider-exact")
    provider.add_argument("condition")
    provider.add_argument("role_expression")
    provider.add_argument("issuer")
    binding = sub.add_parser("has-binding")
    binding.add_argument("role")
    binding.add_argument("member")
    sa = sub.add_parser("audit-sa-wif")
    sa.add_argument("member")
    bucket = sub.add_parser("audit-source-bucket")
    bucket.add_argument("reader")
    sub.add_parser("sa-active")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state = _load_stdin()
    if state is None:
        return 2
    if args.command == "provider-exact":
        if not _provider_understood(state):
            return 2
        ok = provider_is_exact(
            state,
            condition=args.condition,
            role_expression=args.role_expression,
            issuer=args.issuer,
        )
    elif args.command == "has-binding":
        if not _policy_understood(state):
            return 2
        ok = policy_has_unconditional_binding(state, role=args.role, member=args.member)
    elif args.command == "audit-sa-wif":
        if not _policy_understood(state):
            return 2
        ok = service_account_wif_policy_ok(state, member=args.member)
    elif args.command == "audit-source-bucket":
        if not _policy_understood(state):
            return 2
        ok = source_bucket_policy_ok(state, reader=args.reader)
    else:
        if not isinstance(state.get("email"), str) or not isinstance(state.get("disabled", False), bool):
            return 2
        ok = service_account_is_active(state)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
