"""Configuration centralisée (pydantic-settings).

Tous les seuils numériques `[HYPOTHÈSE]` du spine vivent ici et nulle part ailleurs ;
ils sont exposés dans `Trace.thresholds` via `Settings.thresholds()` et se règlent avec les évals.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["dev", "prod"] = "dev"
    allow_ungated: bool = True  # désactivé en production à la fin de 1.10
    anthropic_api_key: str = ""
    usd_eur: float = 0.92

    # Temps (AD-1, AD-9)
    deadline_s: float = 55.0
    llm_timeout_s: float = 25.0

    # Vérification des citations (AD-3)
    quote_min_chars: int = 25
    quote_min_ratio: float = 0.6

    # Retrouver (AD-1)
    max_opens: int = 6
    node_window: int = 30
    search_limit: int = 20
    max_llm_attempts: int = 4  # global à la requête
    max_llm_turns: int = 2

    # Coût (AD-9, AD-10)
    max_cost_eur_per_request: float = 0.10
    cost_alert_eur: float = 0.05

    # Limiteur best-effort par instance (AD-13)
    rate_limit_per_minute: int = 10
    rate_limit_per_day: int = 100

    # Ingestion (AD-8)
    coverage_threshold: float = 0.8
    kind_confidence_min: float = 0.7

    def thresholds(self) -> dict[str, float | int]:
        """Seuils actifs, tels qu'exposés dans `Trace.thresholds`."""
        return {
            "deadline_s": self.deadline_s,
            "llm_timeout_s": self.llm_timeout_s,
            "quote_min_chars": self.quote_min_chars,
            "quote_min_ratio": self.quote_min_ratio,
            "max_opens": self.max_opens,
            "node_window": self.node_window,
            "search_limit": self.search_limit,
            "max_llm_attempts": self.max_llm_attempts,
            "max_llm_turns": self.max_llm_turns,
            "max_cost_eur_per_request": self.max_cost_eur_per_request,
            "cost_alert_eur": self.cost_alert_eur,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "rate_limit_per_day": self.rate_limit_per_day,
            "coverage_threshold": self.coverage_threshold,
            "kind_confidence_min": self.kind_confidence_min,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
