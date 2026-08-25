from __future__ import annotations

import pytest

from server.app.llm import models


def test_tiers_and_step_assignment() -> None:
    assert set(models.TIERS) == {"ingest", "reason", "micro"}
    assert models.TIERS["ingest"].startswith("claude-opus-5")
    assert models.TIERS["reason"].startswith("claude-sonnet-5")
    assert models.TIERS["micro"].startswith("claude-haiku-4-5")
    assert models.STEP_TIERS == {"comprendre": "micro", "retrouver": "reason", "rediger": "reason",
                                 "verifier": "micro", "restituer": None, "ingest": "ingest"}


def test_effort_par_prompt_publie_la_derogation_sinistre() -> None:
    assert getattr(models, "EFFORT_PAR_PROMPT", None) == {"rediger_sinistre": "low"}


def _settings(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    from server.app import config

    monkeypatch.setattr(config, "get_settings", lambda: config.Settings(_env_file=None, anthropic_api_key=key))


def test_check_without_key_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _settings(monkeypatch, "")
    assert models.main(["--check"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_check_exit_codes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _settings(monkeypatch, "k")

    async def all_ok(api_key: str) -> dict[str, bool]:
        return {m: True for m in models.TIERS.values()}

    async def one_missing(api_key: str) -> dict[str, bool]:
        return {m: m != models.TIERS["micro"] for m in models.TIERS.values()}

    async def boom(api_key: str) -> dict[str, bool]:
        raise RuntimeError("401 authentication_error")

    monkeypatch.setattr(models, "check_models", all_ok)
    assert models.main(["--check"]) == 0
    assert capsys.readouterr().out.count("OK") == 3
    monkeypatch.setattr(models, "check_models", one_missing)
    assert models.main(["--check"]) == 1
    assert "ABSENT" in capsys.readouterr().out
    monkeypatch.setattr(models, "check_models", boom)
    assert models.main(["--check"]) == 3
    assert "401" in capsys.readouterr().err
