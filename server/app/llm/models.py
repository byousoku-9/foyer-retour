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
    "comprendre": "micro",
    "retrouver": "reason",
    "rediger": "reason",
    "verifier": "micro",
    "restituer": None,
    "ingest": "ingest",
}


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
