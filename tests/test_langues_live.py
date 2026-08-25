"""Contrôle réel par retraduction des six réponses multilingues de la story 2.4.

Chaque cas passe d'abord par le pipeline du guide sans `lang` forcé. Un second appel `micro`
identifie la langue de la réponse, la retraduit mentalement en français et juge sa fidélité aux
passages français cités. Avec une clé, les réponses sont enregistrées ; sans clé, elles sont
rejouées depuis `tests/llm_fixtures/`, sans réseau.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.domain.profil import Profil
from server.app.domain.trace import StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.prompting import untrusted
from server.app.pipelines.guide import repondre_guide
from tests.fixtures import LLMRecorder
from tests.llm_fake import RecordedAnthropic

ROOT = Path(__file__).resolve().parents[1]

CAS = [
    ("en-arrivee", "en", "How many days do I have to register my arrival with the commune?"),
    ("en-ecole", "en", "Where do I register my children for school in Luxembourg?"),
    ("de-arrivee", "de", "Wie viele Tage habe ich, um meine Ankunft bei der Gemeinde anzumelden?"),
    ("de-adem", "de", "Welche Vorteile bietet die Anmeldung bei der ADEM?"),
    ("pt-arrivee", "pt", "Quanto tempo tenho para declarar a minha chegada à comuna?"),
    ("pt-ecole", "pt", "Onde devo matricular os meus filhos na escola no Luxemburgo?"),
]


class JugementRetraduction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    langue_detectee: Literal["fr", "en", "de", "pt"]
    fidele: bool
    ecarts: list[str] = Field(default_factory=list)


@pytest.fixture(scope="module")
def index() -> Index:
    return Index(load_corpus(ROOT / "data", allow_ungated=True))


def _settings() -> Settings:
    return Settings(_env_file=None, anthropic_api_key="")


def _budget() -> RequestBudget:
    settings = _settings()
    return RequestBudget(deadline_s=settings.deadline_s,
                         max_attempts=settings.max_llm_attempts,
                         max_cost_eur=settings.max_cost_eur_per_request)


def _client(recorder: LLMRecorder) -> LlmClient:
    return LlmClient(_settings(), anthropic_client=RecordedAnthropic(recorder))


def _prefix() -> str:
    """Préfixe local au contrôle : ce jugement n'est pas une étape du pipeline ni un prompt livré."""
    return (
        "Tu contrôles une traduction d'une réponse administrative. Identifie la langue de la "
        "réponse, retraduis-la en français pour ton analyse, puis compare chaque affirmation et "
        "chaque réserve aux passages sources français. `fidele` vaut vrai uniquement si le sens, "
        "les nombres, les conditions et les limites sont préservés. Une formulation naturelle "
        "différente n'est pas un écart. Réponds seulement selon le schéma JSON demandé."
    )


@pytest.mark.parametrize(("cas", "langue", "question"), CAS, ids=[c[0] for c in CAS])
async def test_six_reponses_sont_fideles_apres_retraduction(cas: str, langue: str, question: str,
                                                             index: Index,
                                                             llm_recorder: LLMRecorder) -> None:
    settings = _settings()
    client = _client(llm_recorder)
    answer, trace = await repondre_guide(
        question, [], Profil(), corpus=index.corpus, index=index, client=client, settings=settings,
        request_id=f"live-langue-{cas}", budget=_budget())

    assert answer.lang == langue and answer.lang_fallback is False
    assert answer.found is True and answer.claims
    passages: list[str] = []
    for claim in answer.claims:
        for quote in claim.quotes:
            bloc = index.corpus.documents[index.doc_of(quote.block_id)].block(quote.block_id)
            assert quote.quote == bloc.text[quote.text_start:quote.text_end]
            passages.append(quote.quote)

    step = StepTrace(name="controle_retraduction", tier="micro")
    contenu = "\n\n".join([
        untrusted("question", question),
        untrusted("reponse", answer.texte),
        untrusted("passages_sources", "\n---\n".join(passages)),
    ])
    controle = await client.parse(
        tier="micro", system_prefix=_prefix(), messages=[{"role": "user", "content": contenu}],
        output_model=JugementRetraduction, budget=_budget(), step=step,
        max_tokens=settings.comprendre_max_tokens)

    assert controle.parsed.langue_detectee == langue
    assert controle.parsed.fidele is True, controle.parsed.ecarts
    assert controle.parsed.ecarts == []
    cout = trace.total_cost_eur + step.usage.cost_eur
    print(f"2.4 | {cas} | {langue} | fidèle | {cout:.4f} €")
