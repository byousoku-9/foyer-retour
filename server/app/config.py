"""Configuration centralisée (pydantic-settings).

Tous les seuils numériques `[HYPOTHÈSE]` du spine vivent ici et nulle part ailleurs ;
ils sont exposés dans `Trace.thresholds` via `Settings.thresholds()` et se règlent avec les évals.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8",
                                      env_ignore_empty=True, extra="ignore")

    env: Literal["dev", "prod"] = "dev"
    # None = dérivé de `env` : True en dev, False en prod (désactivé en production à la fin de 1.10).
    allow_ungated: bool | None = None
    anthropic_api_key: str = ""
    usd_eur: float = Field(0.92, gt=0)

    # Temps (AD-1, AD-9)
    deadline_s: float = Field(55.0, gt=0)
    llm_timeout_s: float = Field(25.0, gt=0)

    # Vérification des citations (AD-3)
    quote_min_chars: int = Field(25, ge=1)
    quote_min_ratio: float = Field(0.6, ge=0, le=1)

    # Retrouver (AD-1)
    max_opens: int = Field(6, ge=1)
    node_window: int = Field(30, ge=1)
    search_limit: int = Field(20, ge=1)
    max_llm_attempts: int = Field(4, ge=1)  # global à la requête
    max_llm_turns: int = Field(2, ge=1)

    # Coût (AD-9, AD-10)
    max_cost_eur_per_request: float = Field(0.10, ge=0)
    cost_alert_eur: float = Field(0.05, ge=0)

    # Limiteur best-effort par instance (AD-13)
    rate_limit_per_minute: int = Field(10, ge=1)
    rate_limit_per_day: int = Field(100, ge=1)

    # Ingestion (AD-8)
    coverage_threshold: float = Field(0.8, ge=0, le=1)
    kind_confidence_min: float = Field(0.7, ge=0, le=1)

    @model_validator(mode="after")
    def _coherence(self) -> Settings:
        if self.llm_timeout_s >= self.deadline_s:
            raise ValueError(f"llm_timeout_s ({self.llm_timeout_s}) doit être < deadline_s ({self.deadline_s})")
        if self.allow_ungated is None:
            self.allow_ungated = self.env == "dev"
        return self

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
