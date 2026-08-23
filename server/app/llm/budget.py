"""NFR3 / AD-9 — Deadline monotone, compteur d'appels et cumul de coût, partagés par toutes les
étapes d'une requête. Créé par l'API (story 1.6) avec `deadline_s` ; le client LLM le consulte
avant chaque appel et y consigne chaque usage réel."""

from __future__ import annotations

import time


class RequestBudget:
    """Budget d'une requête : temps (deadline monotone), appels, euros."""

    def __init__(self, deadline_s: float, max_attempts: int, max_cost_eur: float) -> None:
        self.deadline_s = deadline_s
        self.max_attempts = max_attempts
        self.max_cost_eur = max_cost_eur
        self.attempts = 0
        self.cost_eur = 0.0
        self.cost_alerted = False  # AD-10 : `cout_eleve` levé une seule fois par requête, au franchissement
        self._t0 = time.monotonic()

    def remaining(self) -> float:
        """Secondes restantes avant la deadline (peut être négatif)."""
        return self.deadline_s - (time.monotonic() - self._t0)

    def timeout_for_call(self, llm_timeout_s: float) -> float:
        """Timeout à passer au SDK : min(llm_timeout_s, deadline restante)."""
        return min(llm_timeout_s, self.remaining())

    def note_call(self, usage) -> None:
        """Consigne un appel réellement facturé : cumule son coût (les attempts sont comptés à l'envoi)."""
        self.cost_eur = round(self.cost_eur + usage.cost_eur, 4)
