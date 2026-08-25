"""Contrôle réel par retraduction des six réponses multilingues de la story 2.4.

Chaque cas passe d'abord par le pipeline du guide sans `lang` forcé. Un second appel `micro`
identifie la langue de la réponse, la retraduit mentalement en français et juge sa fidélité aux
passages français cités. Avec une clé, les réponses sont enregistrées ; sans clé, elles sont
rejouées depuis `tests/llm_fixtures/`, sans réseau.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.domain.profil import Profil
from server.app.domain.question import ParsedQuestion
from server.app.domain.trace import StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.prompting import untrusted
from server.app.pipelines import guide
from server.app.pipelines.guide import repondre_guide
from tests.fixtures import LLMRecorder
from tests.llm_fake import RecordedAnthropic

ROOT = Path(__file__).resolve().parents[1]

# `attendus` : au moins un des termes rendus par *comprendre* doit porter l'une de ces formes
# **françaises**. C'est l'AC « `terms[]` en français avant le court-circuit » rendue mesurable sur
# des questions réellement écrites en anglais, en allemand et en portugais (revue Codex 2.4, I2) :
# sans elle, le pipeline restait vert avec des termes anglais, que les variantes du dictionnaire
# rattrapent — et la régression de traduction des termes passait inaperçue.
CAS = [
    ("en-arrivee", "en", "How many days do I have to register my arrival with the commune?",
     ("arrivee", "commune", "declaration", "domicile", "residence")),
    ("en-ecole", "en", "Where do I register my children for school in Luxembourg?",
     ("ecole", "scolaire", "scolarisation", "inscription")),
    ("de-arrivee", "de", "Wie viele Tage habe ich, um meine Ankunft bei der Gemeinde anzumelden?",
     ("arrivee", "commune", "declaration", "domicile", "residence")),
    ("de-adem", "de", "Welche Vorteile bietet die Anmeldung bei der ADEM?",
     ("adem", "emploi", "chomage", "inscription")),
    ("pt-arrivee", "pt", "Quanto tempo tenho para declarar a minha chegada à comuna?",
     ("arrivee", "commune", "declaration", "domicile", "residence")),
    ("pt-ecole", "pt", "Onde devo matricular os meus filhos na escola no Luxemburgo?",
     ("ecole", "scolaire", "scolarisation", "inscription")),
]

# Formes qui n'existent que dans les trois langues de départ : aucun terme de recherche ne doit en
# porter une. Aucune n'est le préfixe d'un mot français (« ecole » ≠ « escola », « arrivee » ≠
# « arrival », « commune » ≠ « comuna »).
MOTS_ETRANGERS = (
    "register", "registration", "arrival", "school", "children", "employment", "benefits",
    "anmeldung", "ankunft", "gemeinde", "schule", "kinder", "vorteile", "arbeitsamt",
    "chegada", "comuna", "matricula", "escola", "filhos", "declarar",
)


def _sans_accent(texte: str) -> str:
    """Comparaison de formes, pas de sens : « école » et « ecole » sont le même terme ici."""
    decompose = unicodedata.normalize("NFD", texte.lower())
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn")


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
        "chaque réserve aux passages sources français. Le bloc `reserves` porte les limites "
        "affichées sous la réponse (« ce que je ne sais pas ») : juge-les sur deux points, et deux "
        "seulement — elles sont écrites dans la même langue que la réponse, et elles n'affirment "
        "aucun fait que les passages contredisent. Une limite n'a pas à être soutenue par un "
        "passage : c'est une absence, pas une affirmation. "
        "`fidele` vaut vrai uniquement si le sens, "
        "les nombres, les conditions et les limites sont préservés. Une formulation naturelle "
        "différente n'est pas un écart. Réponds seulement selon le schéma JSON demandé."
    )


@pytest.mark.parametrize(("cas", "langue", "question", "attendus"), CAS, ids=[c[0] for c in CAS])
async def test_six_reponses_sont_fideles_apres_retraduction(cas: str, langue: str, question: str,
                                                             attendus: tuple[str, ...], index: Index,
                                                             monkeypatch: pytest.MonkeyPatch,
                                                             llm_recorder: LLMRecorder) -> None:
    settings = _settings()
    client = _client(llm_recorder)

    # La sortie **réelle** de *comprendre* est relevée au passage, sans appel supplémentaire : c'est
    # celle que le pipeline consomme, et l'AC porte sur elle (« `terms[]` en français **avant** le
    # court-circuit »). Un second appel direct aurait mesuré une autre requête (revue Codex 2.4, I2).
    vues: list[object] = []
    reel = guide.comprendre

    async def _relever(*args, **kw):
        sortie, step = await reel(*args, **kw)
        vues.append(sortie)
        return sortie, step

    monkeypatch.setattr(guide, "comprendre", _relever)

    answer, trace = await repondre_guide(
        question, [], Profil(), corpus=index.corpus, index=index, client=client, settings=settings,
        request_id=f"live-langue-{cas}", budget=_budget())

    (parsed,) = vues
    assert isinstance(parsed, ParsedQuestion), "la question devait être autonome"
    termes = [_sans_accent(terme) for terme in parsed.terms]
    assert termes, "aucun terme cherché : l'AC ne serait pas exercée"
    assert any(any(forme in terme for forme in attendus) for terme in termes), parsed.terms
    for terme in termes:
        for etranger in MOTS_ETRANGERS:
            assert etranger not in terme, f"terme non traduit : {parsed.terms}"

    assert answer.lang == langue and answer.lang_fallback is False
    assert answer.found is True and answer.claims
    passages: list[str] = []
    for claim in answer.claims:
        for quote in claim.quotes:
            bloc = index.corpus.documents[index.doc_of(quote.block_id)].block(quote.block_id)
            assert quote.quote == bloc.text[quote.text_start:quote.text_end]
            passages.append(quote.quote)

    step = StepTrace(name="controle_retraduction", tier="micro")
    # `unknown[]` entre dans le contrôle (revue Codex 2.4, I1) : ce sont des **réserves affichées**,
    # composées soit par le modèle soit par les registres traduits de *restituer*, et leur langue
    # relève de la même AC que le corps de la réponse. Sans elles, une réserve mal traduite laissait
    # les six cas verts. Bloc séparé : elles ne sont pas la réponse, et le juge doit pouvoir dire
    # laquelle s'écarte.
    contenu = "\n\n".join([
        untrusted("question", question),
        untrusted("reponse", answer.texte),
        untrusted("reserves", "\n".join(answer.unknown) if answer.unknown else "(aucune)"),
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
