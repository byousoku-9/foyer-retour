from __future__ import annotations

import time

from server.app.domain.trace import Usage
from server.app.llm.budget import RequestBudget


def test_budget_starts_clean() -> None:
    b = RequestBudget(deadline_s=55, max_attempts=4, max_cost_eur=0.10)
    assert b.attempts == 0 and b.cost_eur == 0.0
    assert 54 < b.remaining() <= 55


def test_remaining_is_monotonic_and_can_go_negative() -> None:
    b = RequestBudget(deadline_s=0.01, max_attempts=1, max_cost_eur=1.0)
    first = b.remaining()
    time.sleep(0.02)
    assert b.remaining() < first
    assert b.remaining() < 0


def test_timeout_for_call_is_min_of_timeout_and_remaining() -> None:
    b = RequestBudget(deadline_s=10, max_attempts=1, max_cost_eur=1.0)
    assert b.timeout_for_call(25.0) <= 10
    assert b.timeout_for_call(0.5) == 0.5


def test_note_call_accumulates_cost_rounded() -> None:
    b = RequestBudget(deadline_s=10, max_attempts=4, max_cost_eur=1.0)
    b.note_call(Usage(cost_eur=0.0101))
    b.note_call(Usage(cost_eur=0.0202))
    assert b.cost_eur == 0.0303
    assert b.attempts == 0  # les attempts sont comptés à l'envoi par le client, pas ici


def test_prefix_tracking_starts_empty_and_remembers_digests() -> None:
    # Story 1.4 (reprise B5) : le budget mémorise les empreintes de préfixes déjà écrits dans la requête.
    b = RequestBudget(deadline_s=10, max_attempts=4, max_cost_eur=1.0)
    assert not b.prefix_seen("abc")
    b.note_prefix("abc")
    assert b.prefix_seen("abc")
    assert not b.prefix_seen("def")
    b2 = RequestBudget(deadline_s=10, max_attempts=4, max_cost_eur=1.0)
    assert not b2.prefix_seen("abc")  # jamais partagé entre requêtes
