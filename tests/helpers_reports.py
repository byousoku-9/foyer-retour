from __future__ import annotations

from collections.abc import Collection

from server.app.domain import Report
from server.ingest.type_clauses import TYPING_STATS_KEYS


def assert_stats_structurelles_exactes(
        regenerated: Report, committed: Report, terminal_keys: Collection[str],
) -> None:
    """Ferme la projection structurelle et les seules statistiques post-typage permises.

    La régénération depuis le PDF rend la projection structurelle, et elle seule : ses valeurs
    doivent être exactement celles committées. Tout ce que l'artefact committé porte en plus doit
    appartenir au typage (`TYPING_STATS_KEYS`, déclaré par le code qui les produit) — une campagne
    payée ou rejouée publie son registre et son bilan, qu'aucune régénération ne peut refaire.
    `terminal_keys` nomme les statistiques de typage que ce document doit au minimum porter.
    """
    terminal = set(terminal_keys)
    structural = set(regenerated.stats)
    assert terminal <= TYPING_STATS_KEYS, sorted(terminal - TYPING_STATS_KEYS)
    assert structural.isdisjoint(TYPING_STATS_KEYS), sorted(structural & TYPING_STATS_KEYS)
    assert terminal <= set(committed.stats), sorted(terminal - set(committed.stats))
    hors_cloture = set(committed.stats) - structural - TYPING_STATS_KEYS
    assert not hors_cloture, sorted(hors_cloture)
    assert regenerated.stats == {key: committed.stats[key] for key in structural}
