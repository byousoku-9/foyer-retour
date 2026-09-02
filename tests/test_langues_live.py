"""Contrôle réel par retraduction des six réponses multilingues de la story 2.4.

Chaque cas passe d'abord par le pipeline du guide sans `lang` forcé. Un second appel `micro`
identifie la langue de la réponse, la retraduit mentalement en français et juge sa fidélité aux
passages français cités. Avec une clé, les réponses sont enregistrées ; sans clé, elles sont
rejouées depuis `tests/llm_fixtures/`, sans réseau.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
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

# Les six cas : identifiant, langue attendue de la réponse, question **réellement écrite** dans cette
# langue. La quatrième colonne — un vocabulaire français admis par cas — a été retirée : c'était une
# liste blanche, et elle rejetait du français fidèle (voir `_mots_non_traduits`).
CAS = [
    ("en-arrivee", "en", "How many days do I have to register my arrival with the commune?"),
    ("en-ecole", "en", "Where do I register my children for school in Luxembourg?"),
    ("de-arrivee", "de", "Wie viele Tage habe ich, um meine Ankunft bei der Gemeinde anzumelden?"),
    ("de-adem", "de", "Welche Vorteile bietet die Anmeldung bei der ADEM?"),
    ("pt-arrivee", "pt", "Quanto tempo tenho para declarar a minha chegada à comuna?"),
    ("pt-ecole", "pt", "Onde devo matricular os meus filhos na escola no Luxemburgo?"),
]


def _sans_accent(texte: str) -> str:
    """Comparaison de formes, pas de sens : « école » et « ecole » sont le même terme ici."""
    decompose = unicodedata.normalize("NFD", texte.lower())
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn")


def _mots(terme: str) -> list[str]:
    """Les mots d'un terme, sans accent ni ponctuation : « déclaration d'arrivée » → 3 mots."""
    return [m for m in re.split(r"[^0-9a-z]+", _sans_accent(terme)) if m]


def vocabulaire_francais(index: Index) -> frozenset[str]:
    """Les mots du **corpus servi** : la seule autorité de français disponible hors ligne.

    Ni le dictionnaire (il porte les variantes multilingues — `school`, `escola`, `anmeldung`,
    `gemeinde` y figurent, il ne peut donc pas arbitrer le français) ni une parenté morphologique
    (`matricular` partage huit caractères avec `matricule`) ne séparent proprement. Le corpus, lui,
    est écrit en français : ce qu'il emploie est français, et c'est tout ce qu'on lui demande.
    """
    return frozenset(
        mot
        for document in index.corpus.documents.values()
        for bloc in document.blocks
        for mot in _mots(bloc.text)
    )


def _mots_non_traduits(termes: Sequence[str], question: str,
                       vocabulaire: frozenset[str]) -> list[str]:
    """Les mots des `termes` **repris de la question source** que le français ne connaît pas.

    AD-5 exige `terms[]` toujours en français, et la régression que l'AC vise est un terme resté
    dans la langue de la question. C'est donc le **report** qui se mesure — un mot du terme qui est
    déjà un mot de la question —, recoupé par le vocabulaire français du corpus servi pour ne pas
    condamner ce que les deux langues partagent légitimement (`ADEM`, `Luxembourg`, `commune`).

    Ce que la règle abandonne, dit franchement : un mot étranger **inventé**, absent de la question,
    passe désormais ce contrôle lexical. Il reste jugé par la seconde moitié du test — le juge de
    retraduction (`JugementRetraduction.fidele` / `ecarts`) et l'assertion sur `answer.lang`. Ce
    qu'elle gagne : elle ne rejette plus une formulation française qu'on n'avait pas prévue, et
    c'est ce que la liste blanche faisait sur `scolariser`, `ses`, `chercher`, `scolarisation`.

    Juge **chaque** terme de la liste, pas un échantillon : la propriété du tour 2 (I2) est gardée.
    """
    de_la_question = frozenset(_mots(question))
    return [mot for terme in termes for mot in _mots(terme)
            if mot in de_la_question and mot not in vocabulaire]


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


# Le contrôle de langue des termes se prouve **hors ligne**, chaque mot fautif accompagné de la
# question qui le porte — c'est le report qui est jugé, il n'a aucun sens hors de sa source. Le
# contre-exemple que la revue Codex 2.4 (tour 2, I2) opposait à la première rédaction est conservé,
# réancré sur une question allemande qui emploie réellement le mot. Sans ce témoin, la seule preuve
# que le contrôle mord serait une mutation faite à la main sur six fixtures — donc aucune preuve
# rejouée par la CI.
DE_SCHULBESUCH = "Wo kann ich den Schulbesuch meiner Kinder anmelden?"
EN_INSCRIPTION = "Where do I complete my school registration in Luxembourg?"


@pytest.mark.parametrize(("termes", "question", "fautifs"), [
    # le contre-exemple de la revue : un seul terme resté dans la langue de départ
    (["école", "Schulbesuch"], DE_SCHULBESUCH, ["schulbesuch"]),
    # aucun mot traduit : les deux sont repris de la question et inconnus du français
    (["school registration"], EN_INSCRIPTION, ["school", "registration"]),
    # un mot portugais glissé dans un terme par ailleurs français
    (["inscription à l'escola"], next(c[2] for c in CAS if c[0] == "pt-ecole"), ["escola"]),
    # les termes que Sonnet rend réellement, avec **leur** question : du français fidèle que la
    # liste blanche rejetait (`scolariser`, `ses`, `chercher`, `scolarisation`)
    (["inscription scolaire", "scolariser ses enfants"],
     next(c[2] for c in CAS if c[0] == "en-ecole"), []),
    (["chercher un emploi", "ADEM", "inscription demandeur d'emploi"],
     next(c[2] for c in CAS if c[0] == "de-adem"), []),
    (["inscription scolaire", "scolarisation des enfants", "école"],
     next(c[2] for c in CAS if c[0] == "pt-ecole"), []),
    # le recoupement au corpus n'est pas décoratif : ces deux mots-là, la question étrangère les
    # partage légitimement avec le français, et les condamner serait le faux rejet d'hier
    (["ADEM"], next(c[2] for c in CAS if c[0] == "de-adem"), []),
    (["déclaration à la commune"], next(c[2] for c in CAS if c[0] == "en-arrivee"), []),
    # un terme vide n'invente pas de faute
    (["inscription scolaire", ""], next(c[2] for c in CAS if c[0] == "en-ecole"), []),
])
def test_le_controle_des_termes_juge_chaque_terme(termes: list[str], question: str,
                                                  fautifs: list[str], index: Index) -> None:
    assert _mots_non_traduits(termes, question, vocabulaire_francais(index)) == fautifs


@pytest.mark.parametrize(("cas", "langue", "question"), CAS, ids=[c[0] for c in CAS])
async def test_six_reponses_sont_fideles_apres_retraduction(cas: str, langue: str, question: str,
                                                             index: Index,
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
        request_id=f"live-langue-{cas}", budget=_budget(), variant="deterministe")

    (parsed,) = vues
    assert isinstance(parsed, ParsedQuestion), "la question devait être autonome"
    assert parsed.terms, "aucun terme cherché : l'AC ne serait pas exercée"
    # `termes_de_recherche()` et non `terms` seul : c'est ce que *retrouver* cherche réellement et ce
    # que l'`AbsenceProof` publie (`terms_searched`), donc `terms[]` **et** `scope.themes[]` — le
    # prompt exige le français des deux, et un thème non traduit relèverait de la même régression.
    cherches = parsed.termes_de_recherche()
    assert _mots_non_traduits(cherches, question, vocabulaire_francais(index)) == [], (
        f"termes restés dans la langue de la question : {cherches}")

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
