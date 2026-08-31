from __future__ import annotations

from collections.abc import Collection

from server.app.domain import Report


def assert_stats_structurelles_exactes(
        regenerated: Report, committed: Report, terminal_keys: Collection[str],
) -> None:
    """Ferme la projection structurelle et les seules statistiques post-typage permises."""
    terminal = set(terminal_keys)
    structural = set(regenerated.stats)
    assert structural.isdisjoint(terminal)
    assert set(committed.stats) == structural | terminal
    assert regenerated.stats == {key: committed.stats[key] for key in structural}
