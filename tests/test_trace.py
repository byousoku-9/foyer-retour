"""Story 4.2e — la trace publie le tier **réellement employé** par chaque appel.

`StepTrace.tier` est le tier *demandé de l'étape* : il est posé avant qu'elle ne commence, il vaut
pour toute l'étape, et il ne dit donc pas ce qu'un appel donné a employé. `LLMCall.model` ne le dit
pas non plus — il n'existe aucune table inverse modèle → tier, et une matrice de réglages peut
parfaitement pointer deux tiers sur le même modèle. L'AC de la story demande « les tiers réellement
employés » : ils se publient donc par appel, à l'endroit qui les connaît.

Aucun réseau : `FakeAnthropic` rejoue des réponses scriptées, et lève sur tout appel non prévu.
"""

from __future__ import annotations

import anthropic
import pytest
from pydantic import BaseModel

from server.app.config import Settings
from server.app.domain.errors import LlmUnavailable
from server.app.domain.trace import LLMCall, StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient, MemoryResponseCache
from server.app.llm.models import STEP_TIERS, TIERS
from tests.llm_fake import FakeAnthropic, fake_message, provider_exception


class Mot(BaseModel):
    mot: str


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget() -> RequestBudget:
    return RequestBudget(deadline_s=100.0, max_attempts=4, max_cost_eur=0.10)


def _client(script: list, cache=None) -> tuple[LlmClient, FakeAnthropic]:
    fake = FakeAnthropic(script)
    return LlmClient(_settings(), anthropic_client=fake, cache=cache), fake


async def _parse(client: LlmClient, *, tier: str, step: StepTrace):
    return await client.parse(tier=tier, system_prefix="préfixe stable",
                              messages=[{"role": "user", "content": "q"}], output_model=Mot,
                              budget=_budget(), step=step)


def test_un_appel_construit_hors_du_client_nannonce_aucun_tier() -> None:
    """AD-16 : `None` est une absence de mesure, jamais un tier par défaut."""
    assert LLMCall(model=TIERS["micro"]).tier is None


async def test_chaque_appel_publie_le_tier_reellement_employe() -> None:
    step = StepTrace(name="verifier", tier="micro")
    result = await _parse(_client([fake_message(model=TIERS["micro"])])[0], tier="micro", step=step)
    assert [call.tier for call in step.calls] == ["micro"]
    assert result.call.tier == "micro" and result.call.model == TIERS["micro"]


async def test_le_tier_de_lappel_peut_differer_du_tier_demande_de_letape() -> None:
    """C'est tout l'objet du champ : le tier de l'étape ne prouve pas celui de l'appel.

    Une étape peut faire plusieurs appels (les tours d'outils de *retrouver*), et un appelant peut
    lui passer un tier qui n'est pas le sien. Sans ce champ, la trace annonçait le tier *demandé* et
    laissait croire qu'il avait été employé.
    """
    step = StepTrace(name="verifier", tier="micro")
    client, fake = _client([fake_message(model=TIERS["micro"]), fake_message(model=TIERS["reason"])])
    await _parse(client, tier="micro", step=step)
    await _parse(client, tier="reason", step=step)

    assert fake.remaining_script == 0
    assert step.tier == "micro"
    assert [call.tier for call in step.calls] == ["micro", "reason"]
    assert [call.model for call in step.calls] == [TIERS["micro"], TIERS["reason"]]


async def test_un_appel_en_echec_publie_aussi_son_tier() -> None:
    """AD-10 : l'appel raté est tracé — modèle, durée, usage nul — et son tier avec lui."""
    step = StepTrace(name="verifier", tier="micro")
    client, _fake = _client([provider_exception(anthropic.InternalServerError)])
    with pytest.raises(LlmUnavailable):
        await _parse(client, tier="reason", step=step)
    assert [call.tier for call in step.calls] == ["reason"]


async def test_une_reponse_rejouee_depuis_le_cache_publie_son_tier() -> None:
    """Le chemin le plus facile à oublier : un `LLMCall` construit sans appel fournisseur."""
    cache = MemoryResponseCache()
    client, fake = _client([fake_message(model=TIERS["micro"])], cache=cache)
    await _parse(client, tier="micro", step=StepTrace(name="verifier", tier="micro"))
    assert fake.remaining_script == 0

    rejoue = StepTrace(name="verifier", tier="micro")
    await _parse(client, tier="micro", step=rejoue)  # aucun appel scripté restant : c'est le cache
    assert [call.tier for call in rejoue.calls] == ["micro"]
    assert rejoue.calls[0].usage.cached_response is True


async def test_un_tour_doutils_publie_le_tier_de_chaque_tour() -> None:
    step = StepTrace(name="retrouver", tier=STEP_TIERS["retrouver"])
    client, fake = _client([fake_message(model=TIERS["micro"], content=[])])
    await client.tool_turn(tier="micro", system_prefix="préfixe stable",
                           messages=[{"role": "user", "content": "q"}],
                           tools=[{"name": "sommaire", "description": "d",
                                   "input_schema": {"type": "object", "properties": {}}}],
                           budget=_budget(), step=step, max_tokens=64)

    assert fake.remaining_script == 0
    assert [call.tier for call in step.calls] == ["micro"]
    # Le tier **demandé** de l'étape reste celui d'AD-9 : les deux faits coexistent sans se confondre.
    assert step.tier == STEP_TIERS["retrouver"]
