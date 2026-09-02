"""AD-9 — La seule table de prix ; `cost_eur` calculé uniquement depuis `usage` renvoyé par l'API.

Prix de liste USD/MTok lus sur la page de prix Anthropic (référence API du 23/08/2026) ; l'offre
d'introduction Sonnet 5 (2/10 jusqu'au 31/08/2026) est ignorée : le coût calculé est un majorant.
Écriture de cache : 1,25× le prix d'entrée (TTL 5 min), 2× (TTL 1 h) ; lecture : 0,1×.
`estimate_cost()` est le majorant avant appel du spine (AD-9, NFR4) : caractères /
`estimate_chars_per_token` × `estimate_tokenizer_factor`, préfixe cacheable (outils, système) et
schéma de sortie au tarif d'écriture de cache du TTL du modèle, messages au tarif d'entrée plein,
sortie comptée à `max_tokens`. `estimate_chars_per_token` (2,0) et le facteur (1,3) sont calibrés
pour que 2,0/1,3 ≈ 1,54 car./token reste sous le pire mesuré (1,65 sur le sommaire du contrat,
`tokens.py` du 23/08/2026) : l'estimation majore chaque poste, elle ne sous-estime jamais
(revue Codex 1.3 tour 2, B5).
"""

from __future__ import annotations

import json
import math
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


def estimate_run_majorant(executions_payantes: int, settings: Settings) -> float:
    """Majorant **de référence** d'une campagne d'évals, avant le premier appel (story 4.2b).

    Par exécution payante (cas × répétition), la référence est le plafond par requête de
    **production** (AD-9, `max_cost_eur_per_request`) — le pire chemin avec relance sous lequel
    `estimate_cost` refuse l'appel qui déborderait. Ce n'est **pas** une borne dure du chemin
    évals : là, chaque exécution reçoit le restant du plafond de run (AD-9, « remplacé par un
    plafond par run ») et peut donc coûter jusqu'à ce restant. La borne dure d'une campagne est le
    budget effectif (`min(--max-cost, LIVE_BUDGET_EUR)`), appliqué avant chaque appel par le
    client et entre les exécutions par le runner — acquis conservés, répétitions manquantes rouges
    au dénominateur. Le préflight refuse sur cette référence les campagnes payantes
    (`--repeat` ≥ 2, cache désarmé) ; le rapport publie ensuite la comparaison avec le coût froid
    réel (p50/p95/max par exécution). Revue 4.2b, MEDIUM 4.
    """
    return _round4(executions_payantes * settings.max_cost_eur_per_request)


def estimate_tokens(text: str, settings: Settings) -> int:
    """Majorant du nombre de tokens d'un texte, sans appeler le tokenizer (AD-1, revue Codex 1.4 B1).

    Même heuristique que le poste « messages » d'`estimate_cost` — `estimate_chars_per_token` corrigé
    par `estimate_tokenizer_factor`, calibré pour majorer le pire mesuré. *retrouver* déterministe est
    du code pur : il ne peut pas appeler `/v1/messages/count_tokens` (réseau, latence, deadline) pour
    honorer `RetrievalBudget.max_tokens`, il majore.
    """
    return math.ceil(len(text) * settings.estimate_tokenizer_factor / settings.estimate_chars_per_token)


def estimate_cost(model: str, system: Any, messages: Any, max_tokens: int, settings: Settings,
                  *, tools: Any = None, output_schema: Any = None, prefix_cached: bool = False,
                  prompt_cache: bool = True) -> float:
    """Majorant en euros d'un appel avant de le faire (AD-9, NFR4) — jamais un coût facturé.

    Le plafond par requête (`BudgetExceeded`) se vérifie sur `budget.cost_eur` réel + cette
    estimation (revue Codex 1.3 tour 2, B5) : elle doit majorer, jamais sous-estimer. Chaque poste
    est donc compté à son tarif le plus défavorable — préfixe cacheable (outils, système) et schéma
    de sortie au tarif d'écriture de cache du TTL du modèle (comme si le préfixe était réécrit à
    chaque appel), messages au tarif d'entrée plein (jamais cachés, spine AD-9), sortie à `max_tokens`.

    `prefix_cached=True` (story 1.4) : le préfixe et le schéma sont comptés au tarif `cache_read`
    (0,1×). L'appelant ne le passe que si le fournisseur a lui-même confirmé, sur un appel précédent
    de la même requête, avoir écrit ou lu ce préfixe (`RequestBudget.prefix_seen`, alimenté depuis
    l'`usage` renvoyé) ; le cache reste alors chaud (deadline 55 s, TTL ≥ 5 min) et l'estimation reste
    un majorant. Le passer sur un préfixe que le fournisseur n'a pas caché — sous sa taille minimale
    cacheable — ferait sous-estimer l'appel : c'est ce que le client refuse de faire."""
    return _round4(estimate_cost_unrounded(
        model, system, messages, max_tokens, settings, tools=tools,
        output_schema=output_schema, prefix_cached=prefix_cached, prompt_cache=prompt_cache,
    ))


def estimate_cost_unrounded(
        model: str, system: Any, messages: Any, max_tokens: int, settings: Settings, *,
        tools: Any = None, output_schema: Any = None, prefix_cached: bool = False,
        prompt_cache: bool = True) -> float:
    """Même majorant qu'`estimate_cost`, avant tout arrondi monétaire.

    Les plans multi-appels additionnent ces valeurs, puis arrondissent le total vers le haut. Une
    addition de valeurs déjà arrondies au plus proche pourrait perdre jusqu'à 0,00005 EUR par appel
    et n'est donc pas un préflight sûr.
    """
    if model not in PRICES:
        raise ValueError(f"modèle absent de PRICES : {model!r}")
    p = PRICES[model]
    tokens_per_char = settings.estimate_tokenizer_factor / settings.estimate_chars_per_token
    prefix_chars = _text_len(system)
    if tools:
        prefix_chars += _json_len(tools)
    if output_schema:
        prefix_chars += _json_len(output_schema)
    suffix_chars = sum(_text_len(m.get("content") if isinstance(m, dict) else getattr(m, "content", ""))
                       for m in messages or [])
    if not prompt_cache:
        write_rate = p["input"]
    elif prefix_cached:
        write_rate = p["cache_read"]
    else:
        write_rate = p["cache_write_1h"] if MODEL_CAPS[model]["cache_ttl"] == "1h" else p["cache_write"]
    usd = (prefix_chars * tokens_per_char * write_rate
           + suffix_chars * tokens_per_char * p["input"]
           + max_tokens * p["output"]) / _MTOK
    return usd * settings.usd_eur


def estimate_cost_from_token_bounds(
        model: str, *, prefix_tokens: int, message_tokens: int, max_tokens: int,
        settings: Settings) -> float:
    """Coût EUR non arrondi depuis des bornes de tokens déjà prouvées par l'appelant.

    Cette variante sert aux enveloppes Unicode dont une borne contextuelle byte-level est plus
    conservatrice que l'estimation calibrée en caractères. Préfixe et messages restent tarifés aux
    mêmes postes défavorables que `estimate_cost`; aucune remise Batch ni cache supposé n'entre ici.
    """
    if model not in PRICES:
        raise ValueError(f"modèle absent de PRICES : {model!r}")
    if min(prefix_tokens, message_tokens, max_tokens) < 0:
        raise ValueError("les bornes de tokens doivent être positives")
    prices = PRICES[model]
    write_rate = (prices["cache_write_1h"]
                  if MODEL_CAPS[model]["cache_ttl"] == "1h"
                  else prices["cache_write"])
    usd = (
        prefix_tokens * write_rate
        + message_tokens * prices["input"]
        + max_tokens * prices["output"]
    ) / _MTOK
    return usd * settings.usd_eur
