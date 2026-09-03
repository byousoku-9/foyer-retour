"""Quatre scénarios réels du pipeline du guide, enregistrés avec la clé et rejoués sans.

Avec `ANTHROPIC_API_KEY` : la chaîne des cinq étapes tourne pour de vrai sur le corpus `lux-guide`
réel, réponses brutes sérialisées dans `tests/llm_fixtures/`. Sans (variable vide) : mêmes
assertions, réponses rejouées — zéro réseau.

1. Une question du guide donne une réponse dont **chaque phrase affichée** est soutenue : toute
   claim retenue est retrouvée mot pour mot dans le bloc relu depuis le corpus, et jugée pertinente
   par le contrôle groupé. Le coût de la requête entière reste sous le plafond par requête.
2. Une question météo est refusée après le **seul** appel de *comprendre* : aucune étape au-delà
   n'est facturée pour un refus (AD-5), et le refus explique pourquoi. L'étage de chaque étape est
   lu sur la configuration (AD-9), jamais recopié ici — c'est le tier qui a changé, pas l'AC.
3. La question de référence de la story 2.6 est rejouée en déterministe, comme diagnostic.
4. La même question traverse la variante outils ; son chemin froid et son coût restent observables.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.domain.profil import Profil
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.pipelines.guide import repondre_guide
from tests.fixtures import LLMRecorder
from tests.helpers_tiers import verifier_etage
from tests.llm_fake import RecordedAnthropic

ROOT = Path(__file__).resolve().parents[1]
DEUX_SUJETS = "Comment inscrire mes enfants à l'école, et quel est le montant des allocations familiales ?"
METEO = "quel temps fera-t-il demain à Luxembourg-Ville ?"
OUTILS_QUESTION = "Quel délai ai-je pour déclarer mon arrivée au Luxembourg ?"


@pytest.fixture(scope="module")
def index() -> Index:
    return Index(load_corpus(ROOT / "data", allow_ungated=True))


def _settings() -> Settings:
    # Seuils par défaut de `config.py`, jamais ceux du `.env` du poste : ils décident des blocs
    # envoyés à *rédiger*, donc de la clé de requête — un `.env` local qui les surcharge rendrait le
    # rejeu hors ligne impossible (revue 1.4). La clé ne sert qu'au vrai client, côté recorder.
    return Settings(_env_file=None, anthropic_api_key="")


def _client(llm_recorder: LLMRecorder) -> LlmClient:
    return LlmClient(_settings(), anthropic_client=RecordedAnthropic(llm_recorder))


def _budget() -> RequestBudget:
    s = _settings()
    return RequestBudget(deadline_s=s.deadline_s, max_attempts=s.max_llm_attempts,
                         max_cost_eur=s.max_cost_eur_per_request)


async def test_a_weather_question_is_refused_before_the_reason_tier(index: Index,
                                                                    llm_recorder: LLMRecorder) -> None:
    settings, budget = _settings(), _budget()
    answer, trace = await repondre_guide(METEO, [], Profil(), corpus=index.corpus, index=index,
                                         client=_client(llm_recorder), settings=settings,
                                         request_id="live-2", budget=budget)
    assert answer.found is False and answer.claims == []
    assert answer.reason is not None and answer.reason.kind == "hors_perimetre"
    assert answer.texte and answer.segments[0].kind == "limite"  # le refus explique pourquoi
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    # Un seul appel, à l'étage que la configuration affecte à *comprendre* : le compte d'appels
    # **est** l'assertion « aucune étape au-delà n'est facturée » (AD-5).
    assert budget.attempts == 1
    verifier_etage(trace.steps[0], settings, appels=1)
