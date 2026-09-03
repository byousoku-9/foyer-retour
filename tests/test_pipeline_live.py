"""Quatre scénarios réels du pipeline du guide, enregistrés avec la clé et rejoués sans.

Avec `ANTHROPIC_API_KEY` : la chaîne des cinq étapes tourne pour de vrai sur le corpus `lux-guide`
réel, réponses brutes sérialisées dans `tests/llm_fixtures/`. Sans (variable vide) : mêmes
assertions, réponses rejouées — zéro réseau.

1. Une question du guide donne une réponse dont **chaque phrase affichée** est soutenue : toute
   claim retenue est retrouvée mot pour mot dans le bloc relu depuis le corpus, et jugée pertinente
   par le contrôle groupé. Le coût de la requête entière reste sous le plafond par requête, et la
   question à deux sujets se source sur **deux fiches distinctes**.
2. Une question météo est refusée après le **seul** appel de *comprendre* : aucune étape au-delà
   n'est facturée pour un refus (AD-5), et le refus explique pourquoi. L'étage de chaque étape est
   lu sur la configuration (AD-9), jamais recopié ici — c'est le tier qui a changé, pas l'AC.

Les deux scénarios que la story 5.6 (T2) a emportés — le diagnostic « déterministe » et le parcours
de la variante « outils » — ne sont pas remplacés : ils nommaient des variantes retirées. Le premier
scénario, lui, mesure une propriété que le chemin servi tient toujours ; il est donc réécrit sur la
navigation, **sans épingler de variante** (`variant=None` ⇒ le défaut de `Settings`).
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
from server.app.llm.models import TIERS
from tests.fixtures import LLMRecorder
from tests.helpers_tiers import verifier_etage
from tests.llm_fake import RecordedAnthropic

ROOT = Path(__file__).resolve().parents[1]
DEUX_SUJETS = "Comment inscrire mes enfants à l'école, et quel est le montant des allocations familiales ?"
METEO = "quel temps fera-t-il demain à Luxembourg-Ville ?"


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
    """AD-3 sur le chemin servi : rien n'est affiché qu'une citation relue ne soutienne.

    Le témoin épinglait `variant="deterministe"`. La variante est partie (story 5.6, T2) ; la
    propriété, elle, est servie telle quelle par la navigation — c'est même là qu'elle compte, le
    modèle choisissant désormais lui-même ce qu'il lit. Il est donc réécrit ici sans nommer de
    variante : ce qui est mesuré est ce qu'un utilisateur reçoit.

    Il reprend au passage la propriété du témoin `test_full_chain_draft_is_sourced_on_at_least_two_fiches`
    (retiré avec `retrouver_deterministe`, qu'il appelait directement) : sur une question à deux
    sujets, l'ébauche se source sur **deux fiches distinctes**. Même question, même enregistrement —
    une seconde fixture pour la même conversation aurait été une seconde dépense pour rien.

    Fixture **à réenregistrer** : le corps de requête n'a rien de commun avec celui de la variante
    retirée.
    """
    settings, budget = _settings(), _budget()
    answer, trace = await repondre_guide(DEUX_SUJETS, [], Profil(enfants=True), corpus=index.corpus,
                                         index=index, client=_client(llm_recorder), settings=settings,
                                         request_id="live-1", budget=budget)
    assert trace.variant == settings.retrieval_variant
    assert answer.found is True and answer.reason is None
    assert answer.claims, "aucune affirmation n'a survécu à la vérification"

    documents = index.corpus.documents
    fiches: set[str] = set()
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
            fiches.add(q.block_id.split(":")[1])
    # La question porte sur deux sujets (école, allocations) : une réponse sourcée sur une seule
    # fiche répondrait à moitié. C'est ce que l'ancien témoin de la chaîne complète mesurait.
    assert len(fiches) >= 2, fiches

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

    # AD-1 : la chaîne des cinq étapes, dans l'ordre, avec l'affectation de tiers d'AD-9 — lue sur
    # `Settings`/`STEP_TIERS`, jamais recopiée : un oracle qui épingle un tier en littéral cesse de
    # mesurer AD-9 dès que la configuration bouge, et se met à mentir sans rougir.
    etapes = [s.name for s in trace.steps]
    # Les premières apparitions, dans l'ordre : la relance d'AD-3 peut insérer un second couple
    # `rediger`/`verifier` entre le premier et *restituer*, et c'est un chemin nominal, pas un
    # accident. Épingler les cinq **positions** faisait dépendre l'invariant d'ordre d'une propriété
    # incidente de la fixture — le jour où le modèle rédige une ébauche que le contrôle relance, un
    # témoin d'ordre rougissait pour une raison qui n'est pas l'ordre.
    assert list(dict.fromkeys(etapes)) == ["comprendre", "retrouver", "rediger", "verifier",
                                           "restituer"]
    for step in trace.steps:
        if step.name in ("retrouver", "rediger"):
            # Les deux étapes que la navigation porte dans **une** conversation : leur étage est
            # `navigation_tier`, lu sur la configuration comme le reste (AD-9, Convention Seuils).
            assert step.tier == settings.navigation_tier
            assert [c.model for c in step.calls if c.model != TIERS[settings.navigation_tier]] == []
        else:
            verifier_etage(step, settings)
    # C'est le modèle qui a lu : *retrouver* a des tours d'outils, et l'ouverture seule rend citable.
    retrouver = trace.steps[1]
    assert retrouver.calls, "aucun tour de navigation : le modèle n'a rien lu lui-même"
    assert retrouver.opened_block_ids
    assert {q.block_id for c in answer.claims for q in c.quotes} <= set(retrouver.opened_block_ids)
    # AD-4 : **un seul** appel groupé de pertinence — par vérification, relance comprise.
    assert [len(s.calls) for s in trace.steps if s.name == "verifier"] == [
        1 for s in trace.steps if s.name == "verifier"]
    # NFR4 : la chaîne entière — vérification comprise — tient sous le plafond par requête
    assert budget.cost_eur < settings.max_cost_eur_per_request
    assert trace.total_cost_eur == pytest.approx(budget.cost_eur, abs=1e-4)


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
