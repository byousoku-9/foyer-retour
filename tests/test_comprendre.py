"""Matrice I/O de *comprendre* (spec 1.4) : conversion en `ParsedQuestion`, langue, requête envoyée —
préfixe statique 5 min, `temperature=0`, pas d'`effort`, `max_tokens=comprendre_max_tokens`, question /
historique / profil chacun sous `untrusted()` et aucun texte utilisateur hors balises (reprise (c))."""

from __future__ import annotations

import json
import re

import pytest

from server.app.config import Settings
from server.app.domain.errors import Timeout
from server.app.domain.profil import Profil
from server.app.domain.question import Turn
from server.app.domain.trace import StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.llm.prompting import load_prompt
from server.app.steps.comprendre import comprendre
from tests.llm_fake import FakeAnthropic, fake_message

HAIKU = TIERS["micro"]
UNTRUSTED = re.compile(r'<untrusted kind="([a-z0-9_]+)">\n(.*?)\n</untrusted>', re.DOTALL)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget(deadline_s: float = 30.0) -> RequestBudget:
    return RequestBudget(deadline_s=deadline_s, max_attempts=4, max_cost_eur=0.10)


def _sortie(**over) -> str:
    base = {"intent": "question", "question_resolue": "À quelle école inscrire mes enfants ?",
            "language": "fr", "terms": ["école", "inscription scolaire"],
            "themes": ["école", "allocations"], "bien": None, "evenement": None, "lieu": None,
            "cause": None, "moment": None}
    return json.dumps(base | over, ensure_ascii=False)


def _client(script: list) -> tuple[LlmClient, FakeAnthropic]:
    fake = FakeAnthropic(script)
    return LlmClient(_settings(), anthropic_client=fake), fake


async def _comprendre(client: LlmClient, question: str = "à quelle école inscrire mes enfants ?",
                      historique: list[Turn] | None = None, profil: Profil | None = None,
                      budget: RequestBudget | None = None, **kw):
    return await comprendre(question, historique or [], profil or Profil(), client=client,
                            budget=budget or _budget(), settings=_settings(), **kw)


def _sections(content: str) -> dict[str, str]:
    return {kind: text for kind, text in UNTRUSTED.findall(content)}


async def test_nominal_builds_parsed_question_and_its_own_step_trace() -> None:
    client, fake = _client([fake_message(text=_sortie(), model=HAIKU)])
    parsed, step = await _comprendre(client, profil=Profil(enfants=True))
    assert parsed.intent == "question" and parsed.language == "fr"
    assert parsed.question_resolue == "À quelle école inscrire mes enfants ?"
    assert parsed.terms == ["école", "inscription scolaire"]  # toujours en français
    assert {"école", "allocations"} <= set(parsed.scope.themes)  # profil enfants → école/allocations
    assert step.name == "comprendre" and step.tier == "micro" and step.ms >= 0
    assert len(step.calls) == 1 and step.calls[0].model == HAIKU
    assert step.opened_block_ids == [] and step.discarded_block_ids == []


async def test_meteo_intent_alone_is_enough_to_decide_the_short_circuit() -> None:
    client, fake = _client([fake_message(text=_sortie(intent="meteo", terms=[], themes=[]), model=HAIKU)])
    parsed, step = await _comprendre(client, question="quel temps fera-t-il demain ?")
    assert parsed.intent == "meteo"
    assert len(step.calls) == 1  # un seul appel micro : aucune autre étape requise pour le refus


async def test_forced_lang_wins_over_detection() -> None:
    client, _ = _client([fake_message(text=_sortie(language="fr"), model=HAIKU)])
    parsed, _step = await _comprendre(client, lang="en")
    assert parsed.language == "en"


@pytest.mark.parametrize("detected, expected", [("EN", "en"), ("", "fr"), ("anglais", "fr"), ("fr-LU", "fr")])
async def test_detected_language_is_normalized_with_fr_fallback(detected: str, expected: str) -> None:
    client, _ = _client([fake_message(text=_sortie(language=detected), model=HAIKU)])
    parsed, _step = await _comprendre(client)
    assert parsed.language == expected


async def test_scope_fields_and_term_cleanup_are_converted() -> None:
    client, _ = _client([fake_message(text=_sortie(terms=[" école ", "", "cantine"], themes=["", " auto "],
                                                   bien="vélo", evenement="vol", lieu="cave",
                                                   cause="", moment=None), model=HAIKU)])
    parsed, _step = await _comprendre(client)
    assert parsed.terms == ["école", "cantine"] and parsed.scope.themes == ["auto"]
    assert parsed.scope.bien == "vélo" and parsed.scope.evenement == "vol" and parsed.scope.lieu == "cave"
    assert parsed.scope.cause is None and parsed.scope.moment is None


async def test_request_shape_static_prefix_untrusted_sections_and_thresholds() -> None:
    client, fake = _client([fake_message(text=_sortie(), model=HAIKU)])
    historique = [Turn(role="user", texte="on arrive en mars"), Turn(role="assistant", texte="bien noté")]
    question = "et pour l'école des enfants ?"
    await _comprendre(client, question=question, historique=historique, profil=Profil(enfants=True, autre="x"))
    (req,) = fake.requests
    s = _settings()
    # préfixe statique byte-identique (pas de sommaire : micro, cache 5 min), tier micro
    assert req["model"] == HAIKU
    assert req["system"] == [{"type": "text", "text": load_prompt("commun") + "\n\n" + load_prompt("comprendre"),
                              "cache_control": {"type": "ephemeral"}}]
    assert req["extra_body"] == {"temperature": 0}
    assert "effort" not in req["output_config"]
    assert req["max_tokens"] == s.comprendre_max_tokens == 1024
    # le schéma dédié est plat et tout est requis (aucun défaut)
    assert set(req["output_config"]["format"]["schema"]["required"]) == {
        "intent", "question_resolue", "language", "terms", "themes", "bien", "evenement", "lieu", "cause", "moment"}
    # question, historique et profil chacun sous untrusted() ; rien hors balises
    (msg,) = req["messages"]
    sections = _sections(msg["content"])
    assert set(sections) == {"historique", "profil", "question"}
    assert sections["question"] == question
    assert json.loads(sections["profil"]) == {"enfants": True}  # profil filtré (PROFIL_KEYS)
    assert json.loads(sections["historique"]) == [{"role": "user", "texte": "on arrive en mars"},
                                                  {"role": "assistant", "texte": "bien noté"}]
    outside = UNTRUSTED.sub("", msg["content"])
    assert outside.strip() == ""
    for fragment in (question, "on arrive en mars", "bien noté", "enfants"):
        assert fragment not in outside


async def test_budget_errors_from_the_client_bubble_up_unchanged() -> None:
    client, fake = _client([])
    with pytest.raises(Timeout):
        await _comprendre(client, budget=RequestBudget(deadline_s=0, max_attempts=4, max_cost_eur=0.10))
    assert fake.requests == []
