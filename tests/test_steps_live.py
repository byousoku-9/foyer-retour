"""Trois scénarios réels des étapes de la story 1.4, enregistrés avec la clé, rejoués sans (AC 1.4).

Avec `ANTHROPIC_API_KEY` : appels réels sur le corpus `lux-guide` réel, réponses brutes sérialisées
dans `tests/llm_fixtures/`. Sans (variable vide) : mêmes assertions, réponses rejouées — zéro réseau.

1. Une question de famille avec un profil `enfants` donne un `scope.themes` qui couvre l'école et les
   allocations : c'est ce qui élargit le rappel de *retrouver* sans dictionnaire (story 2.1).
2. Une question météo est tranchée par le seul `intent` de *comprendre* — aucun appel `reason` n'est
   nécessaire pour la refuser (le court-circuit lui-même est le pipeline de la story 1.5).
3. La chaîne complète *comprendre → retrouver → rédiger* sur le guide rend une ébauche sourcée :
   au moins deux claims portant sur au moins deux fiches distinctes, dont chaque quote est retrouvée
   mot pour mot (`normalize(quote) ⊂ text_norm`) dans le bloc cité, relu depuis le corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
from server.app.domain.profil import Profil
from server.app.domain.question import ParsedQuestion
from server.app.domain.retrieval import RetrievalBudget
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.steps.comprendre import comprendre
from tests.fixtures import LLMRecorder
from tests.helpers_tiers import verifier_etage
from tests.llm_fake import RecordedAnthropic

ROOT = Path(__file__).resolve().parents[1]
DOC_ID = "lux-guide"
FAMILLE = "Je viens d'arriver avec mes enfants : comment les inscrire à l'école et quelles allocations demander ?"
# Question de la chaîne complète : deux sujets du guide traités par deux fiches différentes
# (« Scolariser ses enfants », « Allocations familiales »), pour que l'ébauche ait à citer deux
# endroits distincts. Le rappel de la variante déterministe reste sensible au vocabulaire employé —
# `chercher` n'a ni dictionnaire ni pondération des termes fréquents (stories 2.1 et 4.2) ; 1.4 ne
# mesure pas le rappel, elle prouve la chaîne et ses citations.
DEUX_SUJETS = "Comment inscrire mes enfants à l'école, et quel est le montant des allocations familiales ?"


@pytest.fixture(scope="module")
def index() -> Index:
    return Index(load_corpus(ROOT / "data", allow_ungated=True))


def _settings() -> Settings:
    # Seuils par défaut de `config.py`, jamais ceux du `.env` du poste : les seuils de retrouver
    # décident des blocs envoyés à *rédiger*, donc de la clé de requête — un `.env` local qui les
    # surcharge rendrait le rejeu hors ligne impossible (revue 1.4). La clé, elle, ne sert qu'au vrai
    # client, côté recorder (comme dans `test_client_live`).
    return Settings(_env_file=None, anthropic_api_key="")


def _client(llm_recorder: LLMRecorder) -> LlmClient:
    return LlmClient(_settings(), anthropic_client=RecordedAnthropic(llm_recorder))


def _budget() -> RequestBudget:
    s = _settings()
    return RequestBudget(deadline_s=s.deadline_s, max_attempts=s.max_llm_attempts,
                         max_cost_eur=s.max_cost_eur_per_request)


def _retrieval_budget() -> RetrievalBudget:
    s = _settings()
    return RetrievalBudget(max_opens=s.max_opens, node_window=s.node_window, search_limit=s.search_limit,
                           max_llm_turns=s.max_llm_turns, max_blocks=s.retrieval_max_blocks,
                           max_tokens=s.retrieval_max_tokens)


def _covers(themes: list[str], *needles: str) -> bool:
    """Un thème (normalisé) contient l'un des mots attendus — le modèle choisit ses mots."""
    normalised = [normalize(t) for t in themes]
    return any(needle in n for n in normalised for needle in needles)


async def test_famille_profile_widens_the_scope_to_school_and_allocations(llm_recorder: LLMRecorder) -> None:
    parsed, step = await comprendre(FAMILLE, [], Profil(enfants=True, famille="couple avec enfants"),
                                    client=_client(llm_recorder), budget=_budget(), settings=_settings())
    # AD-5 : une question autonome donne une `ParsedQuestion`, jamais une clarification
    assert isinstance(parsed, ParsedQuestion)
    assert parsed.intent in ("question", "suivi")
    assert parsed.language == "fr"
    assert parsed.terms and all(t.strip() for t in parsed.terms)
    assert _covers(parsed.scope.themes, "ecole", "scolar"), parsed.scope.themes
    assert _covers(parsed.scope.themes, "allocation"), parsed.scope.themes
    # AD-9 : l'étage de *comprendre* est celui que la configuration lui affecte — lu, jamais recopié.
    assert step.name == "comprendre"
    verifier_etage(step, _settings(), appels=1)
    assert step.usage.cost_eur > 0


async def test_meteo_question_is_settled_by_the_intent_alone(llm_recorder: LLMRecorder) -> None:
    budget = _budget()
    parsed, step = await comprendre("quel temps fera-t-il demain à Luxembourg-Ville ?", [], Profil(),
                                    client=_client(llm_recorder), budget=budget, settings=_settings())
    assert isinstance(parsed, ParsedQuestion)
    assert parsed.intent == "meteo"  # l'intent seul décide le refus : aucune autre étape n'est requise
    # L'intent seul tranche : un unique appel, à l'étage configuré, et aucune étape au-delà.
    verifier_etage(step, _settings(), appels=1)
    assert budget.attempts == 1
