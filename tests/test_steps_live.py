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
from server.app.domain.retrieval import RetrievalBudget
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.steps.comprendre import comprendre
from server.app.steps.rediger import rediger
from server.app.steps.retrouver import retrouver_deterministe
from tests.fixtures import LLMRecorder
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
                           max_llm_turns=s.max_llm_turns, max_blocks=s.retrieval_max_blocks)


def _covers(themes: list[str], *needles: str) -> bool:
    """Un thème (normalisé) contient l'un des mots attendus — le modèle choisit ses mots."""
    normalised = [normalize(t) for t in themes]
    return any(needle in n for n in normalised for needle in needles)


async def test_famille_profile_widens_the_scope_to_school_and_allocations(llm_recorder: LLMRecorder) -> None:
    parsed, step = await comprendre(FAMILLE, [], Profil(enfants=True, famille="couple avec enfants"),
                                    client=_client(llm_recorder), budget=_budget(), settings=_settings())
    assert parsed.intent in ("question", "suivi")
    assert parsed.language == "fr"
    assert parsed.terms and all(t.strip() for t in parsed.terms)
    assert _covers(parsed.scope.themes, "ecole", "scolar"), parsed.scope.themes
    assert _covers(parsed.scope.themes, "allocation"), parsed.scope.themes
    assert step.name == "comprendre" and step.tier == "micro"
    assert len(step.calls) == 1 and step.calls[0].model.startswith("claude-haiku")
    assert step.usage.cost_eur > 0


async def test_meteo_question_is_settled_by_the_intent_alone(llm_recorder: LLMRecorder) -> None:
    budget = _budget()
    parsed, step = await comprendre("quel temps fera-t-il demain à Luxembourg-Ville ?", [], Profil(),
                                    client=_client(llm_recorder), budget=budget, settings=_settings())
    assert parsed.intent == "meteo"  # l'intent seul décide le refus : aucune autre étape n'est requise
    assert len(step.calls) == 1 and step.tier == "micro"  # jamais d'appel `reason`
    assert budget.attempts == 1


async def test_full_chain_draft_is_sourced_on_at_least_two_fiches(index: Index, llm_recorder: LLMRecorder) -> None:
    client, budget = _client(llm_recorder), _budget()
    parsed, step_c = await comprendre(DEUX_SUJETS, [], Profil(enfants=True), client=client, budget=budget,
                                      settings=_settings())

    retrieval, step_r = retrouver_deterministe(parsed, corpus=index.corpus, index=index,
                                               budget=_retrieval_budget(), doc_id=DOC_ID)
    assert retrieval.blocs, "aucun bloc retrouvé pour une question du périmètre du guide"
    assert step_r.tier is None and step_r.calls == []  # variante déterministe : zéro appel modèle

    draft, step_d = await rediger(parsed, retrieval, [], client=client, budget=budget, index=index,
                                  doc_id=DOC_ID, settings=_settings())

    # AD-3 : chaque segment factuel cite une claim, chaque quote est recopiée depuis un bloc fourni
    fournis = {b.block_id: b for b in retrieval.blocs}
    assert len(draft.claims) >= 2, [c.text for c in draft.claims]
    fiches = set()
    for claim in draft.claims:
        for quote in claim.quotes:
            bloc = fournis.get(quote.block_id)
            assert bloc is not None, f"{quote.block_id} n'était pas dans les blocs fournis"
            assert normalize(quote.quote) in bloc.text_norm, (quote.block_id, quote.quote)
            fiches.add(quote.block_id.split(":")[1])
    assert len(fiches) >= 2, fiches
    assert [s for s in draft.segments if s.kind == "factuel"]

    assert step_d.name == "rediger" and step_d.tier == "reason"
    # 1 appel, 2 si le premier a buté sur `rediger_max_tokens` (les tokens de réflexion comptent dans
    # la sortie) : le client relance alors avec le motif « réponse tronquée ». Cette relance ne passe
    # sous le plafond que parce que le fournisseur a confirmé avoir caché le préfixe (reprise B5).
    assert 1 <= len(step_d.calls) <= 2
    assert all(c.model.startswith("claude-sonnet") for c in step_d.calls)
    # le plafond par requête (0,10 €) tient pour la chaîne entière, majorant compris. `cost_eur` cumule
    # des arrondis appel par appel : la comparaison tolère un centième de centime par appel.
    assert budget.cost_eur == pytest.approx(step_c.usage.cost_eur + step_d.usage.cost_eur,
                                            abs=1e-4 * len(step_c.calls + step_d.calls))
    assert budget.cost_eur < _settings().max_cost_eur_per_request
