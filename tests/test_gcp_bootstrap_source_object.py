"""Exécute hors ligne les branches GCS critiques de ``ensure_source_object``."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "gcp_bootstrap.sh"
PDF = b"%PDF source exacte"
EXPECTED = hashlib.sha256(PDF).hexdigest()


def _function_source() -> str:
    script = BOOTSTRAP.read_text("utf-8")
    start = script.index("ensure_source_object()")
    end = script.index('\nensure_source_object "axa-lu-optihome-2017"', start)
    return script[start:end]


def _run(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess[str], str]:
    fake = tmp_path / "gcloud"
    calls = tmp_path / "calls"
    local = tmp_path / "source.pdf"
    local.write_bytes(PDF)
    fake.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$1 $2 $3" in
  "storage objects list")
    [ "$SCENARIO" = inventory-error ] && exit 9
    case "$SCENARIO" in present-* ) printf 'doc-a.pdf\\n' ;; esac
    ;;
  "storage cat gs://foyer-retour-sources/doc-a.pdf")
    case "$SCENARIO" in *cat-error ) exit 7 ;; esac
    case "$SCENARIO" in *divergent ) printf 'mauvais' ;; * ) printf '%s' '%PDF source exacte' ;; esac
    ;;
  "storage cp"*)
    [ "$SCENARIO" = cp-error ] && exit 8
    exit 0
    ;;
esac
""",
        "utf-8",
    )
    fake.chmod(0o755)
    shell = f"""set -euo pipefail
G={fake!s}
SOURCES_BUCKET=gs://foyer-retour-sources
EXPECTED={EXPECTED}
present() {{ :; }}
read_committed_source_sha() {{ printf '%s' "$EXPECTED"; }}
sha256_of() {{ shasum -a 256 "$1" | cut -d' ' -f1; }}
sha256_stdin() {{ shasum -a 256 | cut -d' ' -f1; }}
{_function_source()}
ensure_source_object doc-a {local!s} DOC_LOCAL
"""
    env = os.environ | {"SCENARIO": scenario, "CALL_LOG": str(calls)}
    completed = subprocess.run(["bash", "-c", shell], text=True, capture_output=True, env=env)
    return completed, calls.read_text("utf-8") if calls.exists() else ""


@pytest.mark.parametrize(
    ("scenario", "success", "expects_cp"),
    [
        ("inventory-error", False, False),
        ("present-ok", True, False),
        ("present-divergent", False, False),
        ("present-cat-error", False, False),
        ("cp-error", False, True),
        ("absent-ok", True, True),
        ("reread-divergent", False, True),
        ("reread-cat-error", False, True),
    ],
)
def test_source_object_paths_fail_closed(
    tmp_path: Path, scenario: str, success: bool, expects_cp: bool
) -> None:
    completed, calls = _run(tmp_path, scenario)
    assert (completed.returncode == 0) is success, completed.stderr
    assert ("storage cp" in calls) is expects_cp
    if scenario == "inventory-error":
        assert "inventaire du bucket source impossible" in completed.stderr
