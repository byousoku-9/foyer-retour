from __future__ import annotations

import pytest

from server.app.config import Settings
from server.app.domain.trace import Trace


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
            "rate_limit_per_minute", "rate_limit_per_day", "deadline_s"} <= set(t.thresholds)
    assert all(isinstance(v, (int, float)) for v in t.thresholds.values())
