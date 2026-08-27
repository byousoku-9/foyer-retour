"""Valide les garde-fous du bucket source dans le JSON rendu par ``gcloud storage``."""

from __future__ import annotations

import json
import sys
from typing import Any


def source_bucket_security_ok(state: dict[str, Any]) -> bool:
    """Exige UBLA actif et PAP explicitement ``enforced`` dans le schéma gcloud réel."""
    return (
        state.get("uniform_bucket_level_access") is True
        and state.get("public_access_prevention") == "enforced"
    )


def source_bucket_security_understood(state: dict[str, Any]) -> bool:
    return (
        isinstance(state.get("uniform_bucket_level_access"), bool)
        and isinstance(state.get("public_access_prevention"), str)
    )


def main() -> int:
    try:
        state = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 2
    if not isinstance(state, dict):
        return 2
    if not source_bucket_security_understood(state):
        return 2
    return 0 if source_bucket_security_ok(state) else 1


if __name__ == "__main__":
    raise SystemExit(main())
