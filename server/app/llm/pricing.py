"""AD-9 — La seule table de prix ; `cost_eur` calculé uniquement depuis `usage` renvoyé par l'API.

Prix de liste USD/MTok lus sur la page de prix Anthropic (référence API du 23/08/2026) ; l'offre
d'introduction Sonnet 5 (2/10 jusqu'au 31/08/2026) est ignorée : le coût calculé est un majorant.
Écriture de cache : 1,25× le prix d'entrée (TTL 5 min), 2× (TTL 1 h) ; lecture : 0,1×.
`estimate_cost()` est une borne haute avant appel : caractères / `estimate_chars_per_token`
× `estimate_tokenizer_factor` (facteur +30 % des modèles 5), sortie comptée à `max_tokens`, sans cache.
"""

from __future__ import annotations

import json
from typing import Any

from server.app.config import Settings
from server.app.domain.trace import Usage

from .models import MODEL_CAPS, TIERS

# USD par million de tokens ; clés alignées sur le spine (`input`, `cache_write`, `cache_read`, `output`)
# + `cache_write_1h` (le spine impose le cache 1 h pour `reason`, facturé 2×).
_BASE: dict[str, tuple[float, float]] = {  # model -> (input, output) USD/MTok
    TIERS["ingest"]: (5.0, 25.0),  # Claude Opus 5
    TIERS["reason"]: (3.0, 15.0),  # Claude Sonnet 5 (prix de liste, intro ignorée)
    TIERS["micro"]: (1.0, 5.0),  # Claude Haiku 4.5
}

PRICES: dict[str, dict[str, float]] = {
    model: {
        "input": inp,
        "cache_write": round(inp * 1.25, 6),
        "cache_write_1h": round(inp * 2.0, 6),
        "cache_read": round(inp * 0.10, 6),
        "output": out,
    }
    for model, (inp, out) in _BASE.items()
}

BATCH_DISCOUNT = 0.5  # Batch API (story 3.2) : tous les postes × 0,5

_MTOK = 1_000_000


def _round4(x: float) -> float:
    return round(x, 4)


def cost_from_usage(model: str, api_usage: Any, usd_eur: float, *, batch: bool = False) -> Usage:
    """`Usage` du domaine depuis l'objet `usage` de l'API (anthropic.types.Usage ou dict équivalent).

    Postes : `input_tokens` (hors cache), `cache_read_input_tokens`,
    `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`, `output_tokens`.
    Si seul `cache_creation_input_tokens` est présent (ancien format), il est facturé au TTL
    configuré pour le modèle (`MODEL_CAPS.cache_ttl`).
    """
    if model not in PRICES:
        raise ValueError(f"modèle absent de PRICES : {model!r}")
    p = PRICES[model]
    get = api_usage.get if isinstance(api_usage, dict) else lambda k, d=None: getattr(api_usage, k, d)

    input_tokens = int(get("input_tokens") or 0)
    cached = int(get("cache_read_input_tokens") or 0)
    output = int(get("output_tokens") or 0)
    creation = get("cache_creation")
    if creation is not None:
        cget = creation.get if isinstance(creation, dict) else lambda k, d=None: getattr(creation, k, d)
        write_5m = int(cget("ephemeral_5m_input_tokens") or 0)
        write_1h = int(cget("ephemeral_1h_input_tokens") or 0)
    else:
        total_write = int(get("cache_creation_input_tokens") or 0)
        write_1h = total_write if MODEL_CAPS[model]["cache_ttl"] == "1h" else 0
        write_5m = total_write - write_1h

    usd = (
        input_tokens * p["input"]
        + cached * p["cache_read"]
        + write_5m * p["cache_write"]
        + write_1h * p["cache_write_1h"]
        + output * p["output"]
    ) / _MTOK
    if batch:
        usd *= BATCH_DISCOUNT
    cost = _round4(usd * usd_eur)
    return Usage(input=input_tokens, cached=cached, output=output, cost_eur=cost,
                 cached_response=False, cost_eur_original=cost)


def _json_len(value: Any) -> int:
    """Longueur JSON d'une valeur non textuelle (bloc tool_result/image, définition d'outil)."""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _text_len(content: Any) -> int:
    """Caractères d'un contenu de message : texte des blocs `text`, longueur JSON des autres (revue P11)."""
    if isinstance(content, str):
        return len(content)
    total = 0
    for block in content or []:
        get = block.get if isinstance(block, dict) else lambda k, d=None: getattr(block, k, d)
        text = get("text")
        total += len(text) if isinstance(text, str) else _json_len(block)
    return total


def estimate_cost(model: str, system: Any, messages: Any, max_tokens: int, settings: Settings,
                  *, tools: Any = None) -> float:
    """Borne haute en euros d'un appel avant de le faire (AD-9) — jamais un coût facturé."""
    if model not in PRICES:
        raise ValueError(f"modèle absent de PRICES : {model!r}")
    p = PRICES[model]
    chars = _text_len(system) + sum(_text_len(m.get("content") if isinstance(m, dict) else getattr(m, "content", ""))
                                    for m in messages or [])
    if tools:
        chars += _json_len(tools)
    input_tokens = chars / settings.estimate_chars_per_token * settings.estimate_tokenizer_factor
    usd = (input_tokens * p["input"] + max_tokens * p["output"]) / _MTOK
    return _round4(usd * settings.usd_eur)
