"""Reprise différée 1.1/1.2 — mesurer les sommaires au tokenizer réel d'Anthropic.

`uv run python -m server.app.llm.tokens data/lux-guide/summary.md data/axa-lu-optihome-2017/summary.md`
imprime, pour chaque fichier, le nombre de tokens compté par `count_tokens` pour les modèles des
tiers `reason` et `micro` (le fichier est envoyé comme unique message user ; le comptage inclut
donc quelques tokens d'enrobage du tour). Exit 0 si tout est compté, 2 sans clé, 3 sur erreur API.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from server.app.config import get_settings

from .client import LlmClient
from .models import TIERS, Tier

MEASURED_TIERS: tuple[Tier, ...] = ("reason", "micro")


async def measure(paths: list[Path]) -> list[tuple[Path, dict[Tier, int]]]:
    client = LlmClient(get_settings())
    results: list[tuple[Path, dict[Tier, int]]] = []
    for path in paths:
        text = path.read_text("utf-8")
        counts: dict[Tier, int] = {}
        for tier in MEASURED_TIERS:
            counts[tier] = await client.count_tokens(TIERS[tier], None, [{"role": "user", "content": text}])
        results.append((path, counts))
    return results


def main(argv: list[str]) -> int:
    if not argv:
        print("usage : python -m server.app.llm.tokens <fichier> [...]", file=sys.stderr)
        return 2
    paths = [Path(a) for a in argv]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        print(f"fichier(s) introuvable(s) : {', '.join(map(str, missing))}", file=sys.stderr)
        return 2
    if not get_settings().anthropic_api_key:
        print("ANTHROPIC_API_KEY absente (environnement ou .env) : impossible de compter les tokens", file=sys.stderr)
        return 2
    try:
        results = asyncio.run(measure(paths))
    except Exception as exc:  # erreur API ou réseau : on signale sans distinguer
        print(f"comptage impossible : {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    for path, counts in results:
        chars = len(path.read_text("utf-8"))
        per_tier = "  ".join(f"{tier} ({TIERS[tier]}) : {counts[tier]} tokens" for tier in MEASURED_TIERS)
        print(f"{path}  [{chars} caractères]  {per_tier}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
