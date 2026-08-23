"""AD-9 — Une table des tiers, une affectation étape → tier.

`python -m server.app.llm.models --check` vérifie que chaque ID existe via l'API
(exit 0 si tous présents, 1 si un ID manque, 2 sans clé).
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Literal

Tier = Literal["ingest", "reason", "micro"]

TIERS: dict[str, str] = {
    "ingest": "claude-opus-5",
    "reason": "claude-sonnet-5",
    "micro": "claude-haiku-4-5-20251001",  # l'alias sans date n'est pas listé par models.list()
}

STEP_TIERS: dict[str, str | None] = {
    "comprendre": "micro",
    "retrouver": "reason",
    "rediger": "reason",
    "verifier": "micro",
    "restituer": None,
    "ingest": "ingest",
}


async def check_models(api_key: str) -> dict[str, bool]:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    available: set[str] = set()
    page = await client.models.list(limit=100)
    async for m in page:
        available.add(m.id)
    return {model_id: model_id in available for model_id in TIERS.values()}


def _load_dotenv_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    try:
        with open(".env", encoding="utf-8") as f:
            for line in f:
                if line.startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def main(argv: list[str]) -> int:
    if "--check" not in argv:
        for tier, model_id in TIERS.items():
            print(f"{tier}: {model_id}")
        return 0
    key = _load_dotenv_key()
    if not key:
        print("ANTHROPIC_API_KEY absente : impossible de vérifier les modèles", file=sys.stderr)
        return 2
    results = asyncio.run(check_models(key))
    for tier, model_id in TIERS.items():
        print(f"{tier:7} {model_id:22} {'OK' if results[model_id] else 'ABSENT'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
