from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from server.app.config import REPO_ROOT, Settings
from server.app.domain.trace import Trace

THRESHOLD_VARS = [k.upper() for k in Settings.model_fields] + ["ENV", "ALLOW_UNGATED"]


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in THRESHOLD_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_match_spine_hypotheses() -> None:
    s = Settings(_env_file=None)
    assert s.deadline_s == 55 and s.llm_timeout_s == 25
    assert s.quote_min_chars == 25 and s.quote_min_ratio == 0.6
    assert s.max_opens == 6 and s.node_window == 30 and s.search_limit == 20 and s.max_llm_turns == 2
    assert s.max_cost_eur_per_request == 0.10 and s.cost_alert_eur == 0.05
    assert s.rate_limit_per_minute == 10 and s.rate_limit_per_day == 100
    assert s.coverage_threshold == 0.8 and s.kind_confidence_min == 0.7
    assert s.env == "dev" and s.allow_ungated is True


def test_thresholds_feed_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUOTE_MIN_CHARS", "30")
    monkeypatch.setenv("ALLOW_UNGATED", "false")
    s = Settings(_env_file=None)
    assert s.quote_min_chars == 30 and s.allow_ungated is False
    t = Trace(request_id="r", pipeline="guide", thresholds=s.thresholds())
    assert t.thresholds["quote_min_chars"] == 30
    assert {"max_opens", "node_window", "search_limit", "max_llm_attempts", "max_cost_eur_per_request",
            "rate_limit_per_minute", "rate_limit_per_day", "deadline_s",
            # story 1.4 : plafonds de sortie par étape et borne en blocs de *retrouver*
            "comprendre_max_tokens", "rediger_max_tokens", "retrieval_max_blocks"} <= set(t.thresholds)
    assert all(isinstance(v, (int, float)) for v in t.thresholds.values())


def test_allow_ungated_follows_env_unless_explicit() -> None:
    assert Settings(_env_file=None, env="prod").allow_ungated is False
    assert Settings(_env_file=None, env="prod", allow_ungated=True).allow_ungated is True
    assert Settings(_env_file=None, env="dev", allow_ungated=False).allow_ungated is False


def test_bounds_and_coherence() -> None:
    with pytest.raises(ValidationError, match="llm_timeout_s"):
        Settings(_env_file=None, llm_timeout_s=60, deadline_s=55)
    with pytest.raises(ValidationError, match="llm_retry_margin_s"):
        Settings(_env_file=None, llm_retry_margin_s=60, deadline_s=55)
    # revue 1.4 : un plafond par étape ne peut pas dépasser le plafond de sortie du client — il part
    # tel quel au fournisseur et entre au tarif `output` dans le majorant `estimate_cost`.
    with pytest.raises(ValidationError, match="rediger_max_tokens"):
        Settings(_env_file=None, rediger_max_tokens=8192, llm_max_output_tokens=4096)
    with pytest.raises(ValidationError, match="comprendre_max_tokens"):
        Settings(_env_file=None, comprendre_max_tokens=8192, llm_max_output_tokens=4096)
    for bad in ({"deadline_s": 0}, {"quote_min_ratio": 1.5}, {"max_opens": 0}, {"max_cost_eur_per_request": -1},
                {"rate_limit_per_day": 0}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **bad)


def test_env_file_is_read_from_repo_root(tmp_path: Path) -> None:
    assert Settings.model_config["env_file"] == REPO_ROOT / ".env"
    assert (REPO_ROOT / "pyproject.toml").is_file()
    env = tmp_path / ".env"
    env.write_text('ANTHROPIC_API_KEY="sk-test-123"\nUSD_EUR=0.5\n')
    s = Settings(_env_file=env)
    assert s.anthropic_api_key == "sk-test-123" and s.usd_eur == 0.5


def test_env_example_loads_as_is() -> None:
    s = Settings(_env_file=REPO_ROOT / ".env.example")
    assert s.anthropic_api_key == "" and s.env == "dev" and s.allow_ungated is True and s.usd_eur == 0.92


def test_empty_env_values_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNGATED", "")
    monkeypatch.setenv("MAX_OPENS", "")
    assert Settings(_env_file=None).max_opens == 6
