from __future__ import annotations

import math
import os

import pytest

from tests.fixtures import LLMRecorder


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--evals-max-cost", type=float, default=None,
        help="plafond fini et strictement positif du run pytest -m evals",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Une sélection live explicite refuse de démarrer avant le premier test sans ses deux gardes."""
    if config.option.markexpr.strip() != "evals":
        return
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    max_cost = config.getoption("--evals-max-cost")
    if not key:
        raise pytest.UsageError("pytest -m evals exige ANTHROPIC_API_KEY avant tout appel")
    if max_cost is None or not math.isfinite(max_cost) or max_cost <= 0:
        raise pytest.UsageError(
            "pytest -m evals exige --evals-max-cost fini et strictement positif avant tout appel")


@pytest.fixture
def llm_recorder(request: pytest.FixtureRequest) -> LLMRecorder:
    """Enregistre ou rejoue les appels LLM du test courant (`tests/llm_fixtures/{module}.{test}.json`)."""
    return LLMRecorder(request.node.nodeid)
