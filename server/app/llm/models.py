"""AD-9 — Une table des tiers, une affectation étape → tier.

`python -m server.app.llm.models --check` vérifie que chaque ID existe via l'API
(exit 0 si tous présents, 1 si un ID manque, 2 sans clé, 3 sur erreur API ou réseau).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Literal

Tier = Literal["ingest", "reason", "micro"]

TIERS: dict[Tier, str] = {
    "ingest": "claude-opus-5",
    "reason": "claude-sonnet-5",
    "micro": "claude-haiku-4-5-20251001",  # l'alias sans date n'est pas listé par models.list()
}

STEP_TIERS: dict[str, Tier | None] = {
    "comprendre": "reason",
    "retrouver": "reason",
    "rediger": "reason",
    "verifier": "reason",
    "restituer": None,
    "ingest": "ingest",
}

# Capacités par modèle (AD-9 amendé, story 1.3) : `effort` — Haiku 4.5 rejette `output_config.effort` ;
# `temperature` — Sonnet 5 et Opus 5 rejettent (400) toute valeur non défaut, Haiku 4.5 accepte `temperature=0` ;
# `cache_ttl` — durée du breakpoint `cache_control` du préfixe (spine : « cache 1 h » pour `reason`).
MODEL_CAPS: dict[str, dict[str, object]] = {
    TIERS["ingest"]: {"effort": True, "temperature": False, "cache_ttl": "5m",
                      "context_window": 200_000},
    TIERS["reason"]: {"effort": True, "temperature": False, "cache_ttl": "1h",
                      "context_window": 200_000},
    TIERS["micro"]: {"effort": False, "temperature": True, "cache_ttl": "5m",
                     "context_window": 200_000},
}

# Effort explicite par tier quand le modèle l'accepte (AD-9) ; `micro` n'en a pas (MODEL_CAPS.effort=False).
EFFORT: dict[Tier, str] = {
    "ingest": "high",
    "reason": "medium",
}

# Dérogations explicites par prompt (Convention Seuils, revue 2.7 M3). La rédaction sinistre
# transcrit des clauses déjà retrouvées : son raisonnement de couverture appartient à *vérifier*.
# La valeur reste distincte du défaut du tier `reason` et versionnée au même endroit que celui-ci.
EFFORT_PAR_PROMPT: dict[str, str] = {
    "rediger_sinistre": "low",
}


def model_for(tier: Tier) -> str:
    """ID de modèle du tier ; tier inconnu ⇒ ValueError (jamais de modèle par défaut)."""
    try:
        return TIERS[tier]
    except KeyError:
        raise ValueError(f"tier inconnu : {tier!r} (attendu : {', '.join(TIERS)})") from None


async def check_models(api_key: str) -> dict[str, bool]:
    """Présence de chaque ID de `TIERS` dans `models.list()` (auto-pagination)."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    available: set[str] = set()
    async for m in client.models.list(limit=100):
        available.add(m.id)
    return {model_id: model_id in available for model_id in TIERS.values()}


def main(argv: list[str]) -> int:
    if "--check" not in argv:
        for tier, model_id in TIERS.items():
            print(f"{tier}: {model_id}")
        return 0
    from server.app.config import get_settings

    key = get_settings().anthropic_api_key
    if not key:
        print("ANTHROPIC_API_KEY absente (environnement ou .env) : impossible de vérifier les modèles", file=sys.stderr)
        return 2
    try:
        results = asyncio.run(check_models(key))
    except Exception as exc:  # 401, réseau, 5xx : on ne distingue pas, on signale
        print(f"vérification impossible : {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    for tier, model_id in TIERS.items():
        print(f"{tier:7} {model_id:28} {'OK' if results[model_id] else 'ABSENT'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
