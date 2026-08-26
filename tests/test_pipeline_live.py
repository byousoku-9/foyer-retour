"""Quatre scénarios réels du pipeline du guide, enregistrés avec la clé et rejoués sans.

Avec `ANTHROPIC_API_KEY` : la chaîne des cinq étapes tourne pour de vrai sur le corpus `lux-guide`
réel, réponses brutes sérialisées dans `tests/llm_fixtures/`. Sans (variable vide) : mêmes
assertions, réponses rejouées — zéro réseau.

1. Une question du guide donne une réponse dont **chaque phrase affichée** est soutenue : toute
   claim retenue est retrouvée mot pour mot dans le bloc relu depuis le corpus, et jugée pertinente
   par le contrôle groupé. Le coût de la requête entière reste sous le plafond par requête.
2. Une question météo est refusée après le **seul** appel `micro` de *comprendre* : l'étage `reason`
   n'est jamais atteint pour un refus (AD-5), et le refus explique pourquoi.
3. La question de référence de la story 2.6 est rejouée en déterministe, comme diagnostic.
4. La même question traverse la variante outils ; son chemin froid et son coût restent observables.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
from server.app.domain.profil import Profil
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.pipelines.guide import repondre_guide
from tests.fixtures import LLMRecorder
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


async def test_every_displayed_sentence_is_backed_by_a_verified_quote(index: Index,
                                                                      llm_recorder: LLMRecorder) -> None:
    settings, budget = _settings(), _budget()
    answer, trace = await repondre_guide(DEUX_SUJETS, [], Profil(enfants=True), corpus=index.corpus,
                                         index=index, client=_client(llm_recorder), settings=settings,
                                         request_id="live-1", budget=budget,
                                         variant="deterministe")
    assert answer.found is True and answer.reason is None
    assert answer.claims, "aucune affirmation n'a survécu à la vérification"

    documents = index.corpus.documents
    for claim in answer.claims:
        assert claim.status.retrouvee is True and claim.status.pertinente is True
        assert claim.status.edition  # AD-4 : l'édition est affichée, jamais comme statut vert
        for q in claim.quotes:
            bloc = documents[index.doc_of(q.block_id)].block(q.block_id)
            assert bloc.kind != "heading"
            # AD-3 : le texte affiché comme source est **relu depuis le corpus**, dans sa forme
            # d'origine — jamais la chaîne rendue par le modèle (revue Codex 1.5, B2)
            assert q.quote == bloc.text[q.text_start:q.text_end]
            # et c'est bien l'occurrence dont l'inclusion a été prouvée dans la forme normalisée
            assert bloc.text_norm[q.start:q.end] == normalize(q.quote)

    # AD-3 : aucun segment factuel affiché sans claim survivante, et le texte n'est que ces segments
    survivantes = {c.claim_id for c in answer.claims}
    factuels = [s for s in answer.segments if s.kind == "factuel"]
    assert factuels and all(set(s.claim_ids) & survivantes for s in factuels)
    # AD-16 : réciproquement, aucune affirmation retenue que plus aucune phrase n'affiche
    citees = {cid for s in factuels for cid in s.claim_ids}
    assert survivantes <= citees
    assert answer.texte == " ".join(s.text.strip() for s in answer.segments if s.text.strip())
    # Tour 3, B1 : rien de ce que la réponse déclare **ne pas** savoir n'est affiché comme réponse —
    # une absence n'est soutenue par aucun passage, elle se dit dans `unknown[]`.
    assert all(s.kind != "limite" for s in answer.segments)
    assert all(u.strip() not in answer.texte for u in answer.unknown)

    # AD-1 : la chaîne des cinq étapes, dans l'ordre, avec l'affectation de tiers d'AD-9
    assert [s.name for s in trace.steps][:5] == ["comprendre", "retrouver", "rediger", "verifier", "restituer"]
    assert [s.tier for s in trace.steps][:5] == ["micro", "reason", "reason", "micro", None]
    assert trace.steps[1].calls == []  # *retrouver* déterministe : aucun modèle
    verifier = next(s for s in trace.steps if s.name == "verifier")
    assert len(verifier.calls) == 1  # AD-4 : **un seul** appel groupé de pertinence
    # NFR4 : la chaîne entière — vérification comprise — tient sous le plafond par requête
    assert budget.cost_eur < settings.max_cost_eur_per_request
    assert trace.total_cost_eur == pytest.approx(budget.cost_eur, abs=1e-4)


async def test_a_weather_question_is_refused_before_the_reason_tier(index: Index,
                                                                    llm_recorder: LLMRecorder) -> None:
    budget = _budget()
    answer, trace = await repondre_guide(METEO, [], Profil(), corpus=index.corpus, index=index,
                                         client=_client(llm_recorder), settings=_settings(),
                                         request_id="live-2", budget=budget)
    assert answer.found is False and answer.claims == []
    assert answer.reason is not None and answer.reason.kind == "hors_perimetre"
    assert answer.texte and answer.segments[0].kind == "limite"  # le refus explique pourquoi
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert budget.attempts == 1 and all(c.model.startswith("claude-haiku") for c in trace.steps[0].calls)


async def test_deterministe_diagnostic_for_outils_question(
        index: Index, llm_recorder: LLMRecorder) -> None:
    """Même question, témoin diagnostique seulement — jamais une baseline de la story 4.1."""
    settings, budget = _settings(), _budget()
    answer, trace = await repondre_guide(
        OUTILS_QUESTION, [], Profil(), corpus=index.corpus, index=index,
        client=_client(llm_recorder), settings=settings, request_id="live-deterministe-diagnostic",
        budget=budget, variant="deterministe")
    assert trace.variant == "deterministe" and trace.steps[1].calls == []
    assert answer.found and answer.claims
    assert budget.cost_eur <= settings.max_cost_eur_per_request


async def test_outils_navigates_the_real_summary_and_returns_a_sourced_answer(
        index: Index, llm_recorder: LLMRecorder) -> None:
    """Story 2.6 : preuve fournisseur enregistrable puis rejouable sans clé."""
    settings, budget = _settings(), _budget()
    answer, trace = await repondre_guide(
        OUTILS_QUESTION, [], Profil(), corpus=index.corpus, index=index,
        client=_client(llm_recorder), settings=settings, request_id="live-outils",
        budget=budget, variant="outils")
    assert trace.variant == "outils"
    assert [s.name for s in trace.steps] == [
        "comprendre", "retrouver", "rediger", "verifier", "restituer"]
    retrieval = trace.steps[1]
    assert 1 <= len(retrieval.calls) <= settings.max_llm_turns
    assert all(call.tools == ["sommaire", "ouvrir_noeud", "chercher", "definitions"]
               for call in retrieval.calls)
    assert retrieval.opened_block_ids and answer.found and answer.claims
    assert all(claim.status.retrouvee and claim.status.pertinente for claim in answer.claims)
    assert trace.total_cost_eur == pytest.approx(budget.cost_eur, abs=1e-4)
    assert budget.cost_eur <= settings.max_cost_eur_per_request


async def test_outils_cold_path_preflights_every_real_request_under_the_cap(
        index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    """M3 : la fixture fournisseur ne court-circuite pas le majorant des requêtes froides.

    Le rejeu emploie le sommaire, les prompts, les schémas, les outils et les plafonds réels. On
    relève chaque appel à `estimate_cost()` au moment où le client le confronte au coût déjà engagé :
    une dérive de prompt ou de seuil fait donc tomber ce test sans réseau avant l'appel suivant.
    """
    import server.app.llm.client as client_module

    settings, budget = _settings(), _budget()
    recorder = LLMRecorder(
        "tests/test_pipeline_live.py::"
        "test_outils_navigates_the_real_summary_and_returns_a_sourced_answer",
        api_key="",
    )
    original = client_module.estimate_cost
    preflights: list[tuple[float, float, int]] = []
    appels_estimes: list[tuple[tuple, dict]] = []

    def relever(*args, **kwargs):
        estimate = original(*args, **kwargs)
        max_tokens = int(args[3] if len(args) > 3 else kwargs["max_tokens"])
        preflights.append((budget.cost_eur, estimate, max_tokens))
        appels_estimes.append((args, kwargs))
        return estimate

    monkeypatch.setattr(client_module, "estimate_cost", relever)
    answer, _trace = await repondre_guide(
        OUTILS_QUESTION, [], Profil(), corpus=index.corpus, index=index,
        client=LlmClient(settings, anthropic_client=RecordedAnthropic(recorder)), settings=settings,
        request_id="preflight-outils", budget=budget, variant="outils")

    assert answer.found and preflights
    assert settings.retrouver_outils_max_tokens in {m for _, _, m in preflights}
    assert settings.outils_rediger_max_tokens in {m for _, _, m in preflights}
    assert settings.verifier_max_tokens in {m for _, _, m in preflights}
    assert all(engage + estimate <= settings.max_cost_eur_per_request
               for engage, estimate, _ in preflights)
    # Contrôle négatif reproduisant le chemin froid qui avait bloqué la première tentative 2.6 :
    # 0,0477 € engagés par la navigation Sonnet, puis l'ancien plafond commun de *rédiger*.
    args_rediger, kwargs_rediger = next(
        (args, kwargs) for args, kwargs in appels_estimes
        if int(args[3] if len(args) > 3 else kwargs["max_tokens"])
        == settings.outils_rediger_max_tokens
    )
    ancien_majorant = original(
        *args_rediger[:3], settings.rediger_max_tokens, *args_rediger[4:], **kwargs_rediger)
    assert 0.0477 + ancien_majorant > settings.max_cost_eur_per_request
