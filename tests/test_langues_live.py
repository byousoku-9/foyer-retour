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


def _mots_non_traduits(termes: Sequence[str], question: str, vocabulaire: frozenset[str],
                       mot_ancre_min: int) -> list[str]:
    """Les mots cherchés que le corpus français n'atteste pas et que rien n'excuse.

    AD-5 exige `terms[]` **toujours en français**. L'autorité de français est le vocabulaire du
    corpus servi (`vocabulaire_francais`) : c'est du français écrit, disponible hors ligne, et
    aucune liste n'y est rédigée pour ce test. Un mot qu'il n'atteste pas est donc suspect — et le
    contrôle mesure **chaque** mot de **chaque** terme, jamais un échantillon (revue Codex 2.4,
    tour 2, I2).

    Deux excuses, et deux seulement, l'une et l'autre dérivées des entrées du cas :

    - **Le terme est ancré dans le corpus.** Un terme dont tous les mots sont attestés, ou qui
      porte un mot attesté d'au moins `mot_ancre_min` caractères, est du français que le corpus
      reconnaît ; le mot qu'il n'atteste pas y est une forme dérivée ou composée
      (« scolarisation des enfants » est ancré par *enfants*), pas un reste de la langue de départ.
      La borne n'est pas inventée ici : c'est `Settings.qualite_mot_min_chars`, le seuil que le
      produit s'est déjà donné pour la même raison — « en dessous, *été*, *une*, *feu* recouperaient
      n'importe quoi ». Elle est ce qui empêche une préposition partagée entre deux langues
      d'adouber un syntagme entier : « registo **de** residência » n'est pas ancré par *de*.
    - **Sauf report littéral.** Un mot repris tel quel de la question source n'est jamais excusé,
      même dans un terme par ailleurs ancré : c'est le contre-exemple de la revue 2.4
      (« inscription à l'escola »).

    **Le résidu, mesuré et assumé.** Un mot français que le corpus servi n'atteste pas et qui se
    tient **seul** dans son terme est signalé — le contrôle ne peut pas le distinguer d'un mot
    étranger seul dans son terme, puisque `["scolarité"]` et `["escolas"]` ont exactement la même
    forme pour qui n'a que le corpus. Les sondes de la revue exigent que cette classe-là soit
    attrapée ; c'est donc ce côté de l'arbitrage qui est pris, et le témoin
    `test_le_residu_du_controle_est_nomme_et_mesure` le rend visible plutôt que tacite.
    """
    de_la_question = frozenset(_mots(question))
    fautifs: list[str] = []
    for terme in termes:
        mots_du_terme = _mots(terme)
        if not mots_du_terme:
            continue
        inconnus = [m for m in mots_du_terme if m not in vocabulaire]
        ancre = not inconnus or any(
            m in vocabulaire and len(m) >= mot_ancre_min for m in mots_du_terme)
        fautifs += [m for m in inconnus if m in de_la_question or not ancre]
    return fautifs


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


# Le contrôle des termes se prouve **hors ligne**, chaque cas accompagné de la question qui lui
# donne son sens. Le jeu de témoins d'avant était aveugle au trou qu'il devait sonder : ses neuf cas
# n'employaient que des mots **verbatim** dans leur question, si bien qu'une seule lettre de flexion
# (`escola` → `escolas`) désarmait le contrôle sans qu'aucun témoin ne rougisse. Les sondes de la
# revue sont donc reprises telles quelles, sur trois langues et trois formes : mot étranger fléchi,
# mot étranger dérivé, syntagme étranger de bout en bout — y compris celui qu'une préposition
# partagée entre les deux langues suffisait à faire passer.
DE_SCHULBESUCH = "Wo kann ich den Schulbesuch meiner Kinder anmelden?"
EN_INSCRIPTION = "Where do I complete my school registration in Luxembourg?"


def _question(cas: str) -> str:
    return next(c[2] for c in CAS if c[0] == cas)


@pytest.mark.parametrize(("termes", "question", "fautifs"), [
    # --- ce que la conjonction « repris de la question » laissait passer (sondes de la revue) ---
    # un simple pluriel du mot de la question : une lettre suffisait à désarmer le contrôle
    (["escolas"], _question("pt-ecole"), ["escolas"]),
    # deux dérivés des mots de la question (`matricular`, `escola`), jamais verbatim : c'est
    # exactement la forme que le report littéral laissait passer
    (["matrícula escolar"], _question("pt-ecole"), ["matricula", "escolar"]),
    # un syntagme portugais entier, qu'une préposition partagée avec le français ancrait à tort
    (["registo de residência"], _question("pt-arrivee"), ["registo", "residencia"]),
    # un composé allemand entier, aucun mot verbatim dans la question
    (["Wohnsitzanmeldung Frist"], _question("de-arrivee"), ["wohnsitzanmeldung", "frist"]),
    # deux dérivés allemands du même radical que la question n'emploie pas
    (["Arbeitslosigkeit", "Arbeitssuche"], _question("de-adem"),
     ["arbeitslosigkeit", "arbeitssuche"]),
    # un syntagme anglais entier
    (["registration deadline"], _question("en-arrivee"), ["registration", "deadline"]),
    # --- les témoins d'origine : le report littéral reste attrapé ---
    # le contre-exemple de la revue 2.4 : un seul terme resté dans la langue de départ
    (["école", "Schulbesuch"], DE_SCHULBESUCH, ["schulbesuch"]),
    # aucun mot traduit
    (["school registration"], EN_INSCRIPTION, ["school", "registration"]),
    # un mot portugais glissé dans un terme **par ailleurs ancré** : l'ancrage n'excuse pas le report
    (["inscription à l'escola"], _question("pt-ecole"), ["escola"]),
    # --- le français fidèle que la liste blanche rejetait, et que l'ancrage sauve ---
    (["inscription scolaire", "scolariser ses enfants"], _question("en-ecole"), []),
    (["chercher un emploi", "ADEM", "inscription demandeur d'emploi"], _question("de-adem"), []),
    # `scolarisation` n'est pas dans le corpus : c'est *enfants* qui ancre le terme
    (["inscription scolaire", "scolarisation des enfants", "école"], _question("pt-ecole"), []),
    # un acronyme court, attesté par le corpus **et** présent dans la question
    (["ADEM"], _question("de-adem"), []),
    # ce que les deux langues partagent légitimement
    (["déclaration à la commune"], _question("en-arrivee"), []),
    (["déclaration d'arrivée", "commune", "délai"], _question("en-arrivee"), []),
    # un terme vide n'invente pas de faute
    (["inscription scolaire", ""], _question("en-ecole"), []),
])
def test_le_controle_des_termes_juge_chaque_terme(termes: list[str], question: str,
                                                  fautifs: list[str], index: Index) -> None:
    settings = _settings()
    assert _mots_non_traduits(termes, question, vocabulaire_francais(index),
                              settings.qualite_mot_min_chars) == fautifs


def test_le_residu_du_controle_est_nomme_et_mesure(index: Index) -> None:
    """Ce que l'ancrage au corpus ne peut pas trancher, dit ici plutôt que découvert plus tard.

    `["scolarité"]` et `["escolas"]` sont **la même forme** pour un contrôle qui n'a que le corpus :
    un mot que le corpus n'atteste pas, seul dans son terme, sans anchor possible. Les deux sont donc
    signalés. Attraper le second est ce que les sondes de la revue exigent ; signaler le premier est
    le prix, et il est borné — le corpus atteste `école`, `scolaire` et `scolariser`, si bien qu'un
    thème français isolé qu'il ignore est rare, et qu'il reste **vrai** que ce terme-là ne rend aucun
    résultat sur le corpus servi (`Index.chercher` en donne zéro).
    """
    vocabulaire = vocabulaire_francais(index)
    mini = _settings().qualite_mot_min_chars
    assert _mots_non_traduits(["escolas"], _question("pt-ecole"), vocabulaire, mini) == ["escolas"]
    assert _mots_non_traduits(["scolarité"], _question("pt-ecole"), vocabulaire, mini) == ["scolarite"]
    # et la porte de sortie est la même pour les deux : dès que le terme est ancré, il passe
    assert _mots_non_traduits(["scolarité des enfants"], _question("pt-ecole"), vocabulaire, mini) == []


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
    assert _mots_non_traduits(cherches, question, vocabulaire_francais(index),
                              settings.qualite_mot_min_chars) == [], (
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
