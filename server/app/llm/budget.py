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
        # Story 1.4 (reprise B5) : empreintes des préfixes déjà écrits dans le cache pendant cette requête —
        # `estimate_cost` peut alors compter le préfixe au tarif `cache_read` (0,1×) au lieu de l'écriture (2×).
        # Le cache est nécessairement chaud : deadline 55 s, TTL minimal 5 min.
        self._prefixes: set[str] = set()
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

    def note_prefix(self, digest: str) -> None:
        """Consigne l'empreinte d'un préfixe (modèle + système + tools + schéma) écrit dans le cache —
        seulement après une réponse réussie du fournisseur (un échec d'appel ne note rien)."""
        self._prefixes.add(digest)

    def prefix_seen(self, digest: str) -> bool:
        """Le préfixe a-t-il déjà été écrit dans cette requête ?"""
        return digest in self._prefixes
