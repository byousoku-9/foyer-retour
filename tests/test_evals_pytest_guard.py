"""Préflight de la sélection payante, sans lancer de sous-processus pytest."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests import conftest as plugin


class _Item:
    def __init__(self, evals: bool) -> None:
        self.evals = evals

    def get_closest_marker(self, name: str) -> object | None:
        return object() if self.evals and name == "evals" else None


class _Config:
    option = SimpleNamespace(markexpr="evals and not lent")

    def __init__(self, max_cost: float | None) -> None:
        self.max_cost = max_cost

    def getoption(self, name: str) -> float | None:
        assert name == "--evals-max-cost"
        return self.max_cost


def test_expression_composite_evals_exige_la_cle(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(pytest.UsageError, match="ANTHROPIC_API_KEY"):
        plugin.pytest_collection_modifyitems(_Config(1.0), [_Item(True)])


def test_aucun_item_eval_selectionne_ne_declenche_pas_le_preflight(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    plugin.pytest_collection_modifyitems(_Config(None), [_Item(False)])
