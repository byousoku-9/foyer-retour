from __future__ import annotations

import pytest

from server.app.config import Settings
from server.app.llm import pricing
from server.app.llm.models import TIERS

OPUS, SONNET, HAIKU = TIERS["ingest"], TIERS["reason"], TIERS["micro"]


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_prices_table_covers_all_tiers_with_list_prices() -> None:
    assert set(pricing.PRICES) == set(TIERS.values())
    # Prix de liste USD/MTok du 23/08/2026 ; l'intro Sonnet 5 (2/10) est ignorée : coût majorant.
    assert pricing.PRICES[OPUS] == {"input": 5.0, "cache_write": 6.25, "cache_write_1h": 10.0,
                                    "cache_read": 0.5, "output": 25.0}
    assert pricing.PRICES[SONNET] == {"input": 3.0, "cache_write": 3.75, "cache_write_1h": 6.0,
                                      "cache_read": 0.3, "output": 15.0}
    assert pricing.PRICES[HAIKU] == {"input": 1.0, "cache_write": 1.25, "cache_write_1h": 2.0,
                                     "cache_read": 0.1, "output": 5.0}
    assert pricing.BATCH_DISCOUNT == 0.5


def _api_usage(input_tokens=0, cache_read=0, cache_5m=0, cache_1h=0, output=0) -> dict:
    return {"input_tokens": input_tokens, "cache_read_input_tokens": cache_read,
            "cache_creation": {"ephemeral_5m_input_tokens": cache_5m, "ephemeral_1h_input_tokens": cache_1h},
            "output_tokens": output}


def test_cost_from_usage_nominal_is_rounded_to_4_decimals() -> None:
    u = pricing.cost_from_usage(SONNET, _api_usage(input_tokens=10_000, output=2_000), usd_eur=0.92)
    # (10000×3 + 2000×15) / 1e6 = 0.06 USD → 0.0552 €
    assert u.cost_eur == 0.0552 and u.cost_eur_original == 0.0552
    assert (u.input, u.cached, u.output) == (10_000, 0, 2_000)
    assert u.cached_response is False


def test_cost_from_usage_cache_read_at_a_tenth() -> None:
    u = pricing.cost_from_usage(SONNET, _api_usage(cache_read=3000), usd_eur=1.0)
    assert u.cached == 3000
    assert u.cost_eur == round(3000 * 0.3 / 1e6, 4)


def test_cost_from_usage_cache_write_1h_costs_double() -> None:
    one_hour = pricing.cost_from_usage(SONNET, _api_usage(cache_1h=2000), usd_eur=1.0)
    five_min = pricing.cost_from_usage(SONNET, _api_usage(cache_5m=2000), usd_eur=1.0)
    assert one_hour.cost_eur == round(2000 * 6.0 / 1e6, 4)
    assert five_min.cost_eur == round(2000 * 3.75 / 1e6, 4)
    assert one_hour.cost_eur > five_min.cost_eur


def test_cost_from_usage_legacy_total_uses_model_ttl() -> None:
    legacy = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 2000,
              "output_tokens": 0}
    assert pricing.cost_from_usage(SONNET, legacy, usd_eur=1.0).cost_eur == round(2000 * 6.0 / 1e6, 4)  # reason = 1 h
    assert pricing.cost_from_usage(HAIKU, legacy, usd_eur=1.0).cost_eur == round(2000 * 1.25 / 1e6, 4)  # micro = 5 min


def test_cost_from_usage_batch_discount_halves() -> None:
    full = pricing.cost_from_usage(OPUS, _api_usage(input_tokens=100_000, output=10_000), usd_eur=1.0)
    batch = pricing.cost_from_usage(OPUS, _api_usage(input_tokens=100_000, output=10_000), usd_eur=1.0, batch=True)
    assert batch.cost_eur == round(full.cost_eur / 2, 4)


def test_cost_from_usage_accepts_sdk_object() -> None:
    from anthropic.types import Usage as ApiUsage

    api = ApiUsage(input_tokens=1000, output_tokens=100, cache_read_input_tokens=500)
    u = pricing.cost_from_usage(HAIKU, api, usd_eur=1.0)
    assert u.input == 1000 and u.cached == 500 and u.output == 100
    assert u.cost_eur == round((1000 * 1.0 + 500 * 0.1 + 100 * 5.0) / 1e6, 4)


def test_unknown_model_is_an_error_never_a_default_price() -> None:
    with pytest.raises(ValueError, match="PRICES"):
        pricing.cost_from_usage("claude-inconnu", _api_usage(), usd_eur=1.0)
    with pytest.raises(ValueError, match="PRICES"):
        pricing.estimate_cost("claude-inconnu", "", [], 10, _settings())


def test_estimate_cost_uses_configured_heuristic_and_max_tokens() -> None:
    s = _settings(estimate_chars_per_token=4.0, estimate_tokenizer_factor=1.3, usd_eur=0.92)
    system = [{"type": "text", "text": "a" * 4000}]
    messages = [{"role": "user", "content": "b" * 4000}, {"role": "assistant", "content": [{"type": "text", "text": "c" * 2000}]}]
    est = pricing.estimate_cost(SONNET, system, messages, max_tokens=1000, settings=s)
    prefix_tokens = 4000 / 4.0 * 1.3  # préfixe cacheable au tarif d'écriture 1 h (reason)
    suffix_tokens = 6000 / 4.0 * 1.3  # messages au tarif d'entrée plein
    expected = round((prefix_tokens * 6.0 + suffix_tokens * 3.0 + 1000 * 15.0) / 1e6 * 0.92, 4)
    assert est == expected


def test_estimate_cost_string_system_and_empty_messages() -> None:
    s = _settings()
    assert pricing.estimate_cost(HAIKU, "abcd" * 100, [], max_tokens=10, settings=s) > 0


def test_estimate_cost_majorizes_measured_live_costs() -> None:
    # revue Codex 1.3 tour 2, B5 : l'estimation doit majorer le coût réel, écriture de cache comprise.
    # Mesures tokens.py du 23/08/2026 : guide 10 359 car. → 4 570 tokens (reason), contrat 6 594 → 3 997
    # (reason) / 3 707 (micro) — pire cas 1,65 car./token, sous lequel 2,0/1,3 ≈ 1,54 reste calibré.
    s = _settings()  # défauts de config.py, ceux du client réel
    for chars, tokens in ((10_359, 4_570), (6_594, 3_997)):
        real = pricing.cost_from_usage(SONNET, _api_usage(cache_1h=tokens, output=300), usd_eur=s.usd_eur)
        est = pricing.estimate_cost(SONNET, "x" * chars, [], max_tokens=300, settings=s)
        assert est >= real.cost_eur, f"{chars} car. : estimé {est} < réel {real.cost_eur}"
    real = pricing.cost_from_usage(HAIKU, _api_usage(cache_5m=3_707, output=300), usd_eur=s.usd_eur)
    est = pricing.estimate_cost(HAIKU, "x" * 6_594, [], max_tokens=300, settings=s)
    assert est >= real.cost_eur


def test_estimate_cost_counts_the_output_schema() -> None:
    # revue Codex 1.3 tour 2, B5 : le schéma structuré (`output_config.format`) est un poste facturable.
    s = _settings()
    schema = {"type": "json_schema", "schema": {"properties": {"mot": {"type": "string",
                                                                       "description": "d" * 4000}},
                                                "required": ["mot"]}}
    base = pricing.estimate_cost(HAIKU, "sys", [{"role": "user", "content": "q"}], 100, s)
    with_schema = pricing.estimate_cost(HAIKU, "sys", [{"role": "user", "content": "q"}], 100, s,
                                        output_schema=schema)
    assert with_schema > base
