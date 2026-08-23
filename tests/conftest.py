from __future__ import annotations

import pytest

from tests.fixtures import LLMRecorder


@pytest.fixture
def llm_recorder(request: pytest.FixtureRequest) -> LLMRecorder:
    """Enregistre ou rejoue les appels LLM du test courant (`tests/llm_fixtures/{nom}.json`)."""
    return LLMRecorder(request.node.name)
