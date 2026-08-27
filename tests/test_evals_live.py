"""Point d'entrée payant explicite du harness ; exclu de ``pytest -q`` par configuration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server.evals import run


@pytest.mark.evals
def test_questions_temoins_live(pytestconfig: pytest.Config, tmp_path: Path) -> None:
    max_cost = pytestconfig.getoption("--evals-max-cost")
    cache_dir = Path(os.environ.get("EVALS_CACHE_DIR", tmp_path / "cache"))
    output_json = Path(os.environ.get("EVALS_OUTPUT_JSON", tmp_path / "results.json"))
    output_markdown = Path(os.environ.get("EVALS_OUTPUT_MARKDOWN", tmp_path / "results.md"))
    args = [
        "--profile", os.environ.get("EVALS_PROFILE", "full"),
        "--max-cost", str(max_cost),
        "--cache-dir", str(cache_dir),
        "--output-json", str(output_json),
        "--output-markdown", str(output_markdown),
    ]
    if os.environ.get("EVALS_QUICK", "").lower() in {"1", "true", "yes"}:
        args.append("--quick")
    assert run.main(args) == 0
    assert output_json.is_file() and output_markdown.is_file()
