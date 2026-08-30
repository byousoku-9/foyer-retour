"""Reprise différée 1.1/1.2 — compter des textes au tokenizer réel d'Anthropic.

Ce module est **pur du point de vue du système de fichiers** : il reçoit des textes déjà lus et rend
leur comptage. Il ne lit aucun chemin, et c'est structurel (patch croisé 1/3, `N1-TOKENS-OMIS`).

Avant ce tour, il lisait chaque chemin pour le comptage, **payait**, puis rouvrait le même chemin
pour en mesurer la longueur : un lot de sommaires couverts pouvait donc faire compter les tokens
d'une génération et publier la longueur d'une autre, sans aucun repère ni préflight de racine. La
correction ne pouvait pas se faire ici : la table des couches du spine interdit à `llm` d'importer
`corpus` (`tests/test_layers.py`), et l'API de repère y vit. La lecture pincée et le refus avant coût
appartiennent donc à l'entrypoint opérateur `server/evals/tokens.py`, seule couche autorisée à voir
`corpus` **et** `llm` — c'est la frontière que ce tour déplace, et le sens de la flèche est celui que
la table permet déjà (`evals → corpus, llm`).

Commande opérateur : `uv run python -m server.evals.tokens <fichier> [...]`.
"""

from __future__ import annotations

from pathlib import Path

from server.app.config import get_settings

from .client import LlmClient
from .models import TIERS, Tier

MEASURED_TIERS: tuple[Tier, ...] = ("reason", "micro")


async def measure(lot: list[tuple[Path, str]]) -> list[tuple[Path, dict[Tier, int]]]:
    """Compte chaque texte **déjà lu** du lot, sans jamais rouvrir son chemin.

    Le texte reçu est le seul qui sera compté *et* mesuré par l'appelant : il n'y a pas de seconde
    ouverture, donc pas de fenêtre où une bascule dissocierait les tokens de la longueur.
    """
    client = LlmClient(get_settings())
    results: list[tuple[Path, dict[Tier, int]]] = []
    for path, text in lot:
        counts: dict[Tier, int] = {}
        for tier in MEASURED_TIERS:
            counts[tier] = await client.count_tokens(TIERS[tier], None, [{"role": "user", "content": text}])
        results.append((path, counts))
    return results
