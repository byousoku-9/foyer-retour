from __future__ import annotations

from pathlib import Path

import pytest

from server.app.llm import models


def test_tiers_and_step_assignment() -> None:
    assert set(models.TIERS) == {"ingest", "reason", "micro"}
    assert models.TIERS["ingest"].startswith("claude-opus-5")
    assert models.TIERS["reason"].startswith("claude-sonnet-5")
    assert models.TIERS["micro"].startswith("claude-haiku-4-5")
    assert models.STEP_TIERS == {"comprendre": "micro", "retrouver": "reason", "rediger": "reason",
                                 "verifier": "micro", "restituer": None, "ingest": "ingest"}


def test_check_without_key_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # pas de .env
    assert models.main(["--check"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
