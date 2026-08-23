"""Un appel réel par tier (AC 1.3), enregistré avec la clé dans `tests/llm_fixtures/`, rejoué sans.

Avec la clé : trois appels réels + un second appel `reason` au même préfixe pour observer le cache
fournisseur (le préfixe fait > 1024 tokens, minimum cacheable de Sonnet 5). Sans clé : mêmes tests,
réponses brutes rejouées et revalidées — zéro réseau.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from server.app.config import Settings, get_settings
from server.app.domain.trace import StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import Tier
from server.app.llm.prompting import load_prompt
from tests.fixtures import LLMRecorder
from tests.llm_fake import RecordedAnthropic


class Reponse(BaseModel):
    mot: str


# Préfixe stable et assez long pour être cacheable (≥ 1024 tokens sur Sonnet 5) : les règles
# communes répétées, byte-identiques d'un appel à l'autre.
def _prefix() -> str:
    commun = load_prompt("commun")
    return ("Tu réponds en un seul mot, en JSON conforme au schéma demandé.\n\n" + commun) + \
        "\n\n---\n\n" + "\n".join(f"Rappel {i} : {commun}" for i in range(4))


def _client(llm_recorder: LLMRecorder) -> LlmClient:
    settings = Settings(_env_file=None, anthropic_api_key="")  # la clé ne sert qu'au vrai client, côté recorder
    return LlmClient(settings, anthropic_client=RecordedAnthropic(llm_recorder))


def _budget() -> RequestBudget:
    s = get_settings()
    return RequestBudget(deadline_s=s.deadline_s, max_attempts=s.max_llm_attempts,
                         max_cost_eur=s.max_cost_eur_per_request)


@pytest.mark.parametrize("tier,question", [("micro", "Réponds exactement le mot bonjour."),
                                           ("reason", "Réponds exactement le mot merci."),
                                           ("ingest", "Réponds exactement le mot oui.")])
async def test_each_tier_answers_with_real_usage(tier: Tier, question: str, llm_recorder: LLMRecorder) -> None:
    client = _client(llm_recorder)
    budget, step = _budget(), StepTrace(name="live")
    result = await client.parse(tier=tier, system_prefix=_prefix(), messages=[{"role": "user", "content": question}],
                                output_model=Reponse, budget=budget, step=step, max_tokens=300)
    assert isinstance(result.parsed, Reponse) and result.parsed.mot
    assert result.usage.cost_eur > 0
    assert result.usage.output > 0
    assert budget.attempts >= 1 and budget.cost_eur == step.usage.cost_eur
    assert step.calls and step.calls[0].model.startswith(("claude-haiku", "claude-sonnet", "claude-opus"))


async def test_reason_second_call_same_prefix_hits_the_provider_cache(llm_recorder: LLMRecorder) -> None:
    client = _client(llm_recorder)
    budget = _budget()
    calls = []
    for question in ("Réponds exactement le mot un.", "Réponds exactement le mot deux."):
        step = StepTrace(name="live")
        await client.parse(tier="reason", system_prefix=_prefix(),
                           messages=[{"role": "user", "content": question}],
                           output_model=Reponse, budget=budget, step=step, max_tokens=300)
        calls.append(step.calls[0])
    assert calls[0].cache_write > 0 or calls[0].cache_read > 0  # préfixe écrit (ou déjà présent)
    assert calls[1].cache_read > 0  # le second appel lit le préfixe caché — un miss (double écriture) échoue


async def test_count_tokens_live(llm_recorder: LLMRecorder) -> None:
    client = _client(llm_recorder)
    from server.app.llm.models import TIERS

    count = await client.count_tokens(TIERS["micro"], None, [{"role": "user", "content": "bonjour " * 100}])
    assert count > 100
