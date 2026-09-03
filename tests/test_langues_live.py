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
# liste blanche, et elle rejetait du français fidèle (voir `_mots_non_attestes`).
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


def _mots_non_attestes(termes: Sequence[str], question: str,
                       vocabulaire: frozenset[str]) -> list[str]:
    """Les mots cherchés que le corpus français n'atteste pas et que rien n'excuse.

    **Le nom dit ce que la fonction mesure, et pas davantage (T7, 03/09/2026).** Elle s'appelait
    `_mots_non_traduits` et concluait donc, de l'absence d'un mot du corpus, qu'il était resté dans
    la langue de la question. C'est un pas que la mesure ne permet pas : le corpus du guide est un
    échantillon de français, pas un lexique. Le cas `en-ecole` l'a montré en réel — le modèle a
    cherché « scolarisation », du français que le corpus n'emploie pas, et le contrôle l'a compté
    comme un mot anglais. La fonction est inchangée ; ce qu'on lui fait dire l'est.

    AD-5 exige `terms[]` **toujours en français**. L'autorité invoquée ici est le vocabulaire du
    corpus servi (`vocabulaire_francais`) : du français écrit, disponible hors ligne, et aucune
    liste rédigée pour ce test. Un mot qu'il n'atteste pas est donc **suspect**, jamais convaincu —
    et le contrôle mesure **chaque** mot de **chaque** terme, jamais un échantillon (revue Codex
    2.4, tour 2, I2). La preuve positive, elle, est rendue par
    `_mots_de_la_langue_de_la_question`.

    Deux excuses, et deux seulement :

    - **Le terme est majoritairement français.** Un mot inconnu n'est excusé que si le corpus atteste
      **strictement plus de la moitié** des mots de son terme : « scolarisation des enfants » est
      excusé par *des* **et** *enfants*, pas par l'un des deux. C'est la correction du cognat : la
      règle d'avant excusait tous les inconnus d'un terme dès qu'**un** mot attesté d'au moins cinq
      caractères y figurait, si bien que « residence permit » et « school registration in the
      commune » passaient entiers — *residence* et *commune* sont du français, et adoubaient le
      reste. Une majorité ne se transporte pas d'un mot à l'autre : dans un terme de deux mots, un
      seul mot attesté n'excuse rien, et il faut que le terme soit **français dans son ensemble**
      pour qu'une forme dérivée y soit tolérée.
    - **Sauf report littéral.** Un mot repris tel quel de la question source n'est jamais excusé,
      même dans un terme par ailleurs majoritairement français : c'est le contre-exemple de la
      revue 2.4 (« inscription à l'escola »).

    Aucun seuil numérique n'entre ici : « strictement plus de la moitié » est une propriété de
    composition, pas une valeur à régler — la borne de longueur qu'employait la version d'avant
    (`qualite_mot_min_chars`) était précisément ce qui rendait un cognat suffisant.

    **Le résidu, mesuré et assumé.** Un mot français que le corpus servi n'atteste pas et qui se
    tient **seul** dans son terme est signalé — le contrôle ne peut pas le distinguer d'un mot
    étranger seul dans son terme, puisque `["scolarité"]` et `["escolas"]` ont exactement la même
    forme pour qui n'a que le corpus. Les sondes de la revue exigent que cette classe-là soit
    attrapée ; c'est donc ce côté de l'arbitrage qui est pris ici, et le témoin
    `test_le_residu_du_controle_est_nomme_et_mesure` le rend visible plutôt que tacite. Ce que le
    résidu ne peut **pas** faire, c'est décider d'un run réel : le témoin de fidélité ne l'exige
    donc plus vide (voir `test_six_reponses_sont_fideles_apres_retraduction`).
    """
    de_la_question = frozenset(_mots(question))
    fautifs: list[str] = []
    for terme in termes:
        mots_du_terme = _mots(terme)
        if not mots_du_terme:
            continue
        attestes = sum(1 for m in mots_du_terme if m in vocabulaire)
        majoritairement_francais = attestes * 2 > len(mots_du_terme)
        fautifs += [m for m in mots_du_terme
                    if m not in vocabulaire
                    and (m in de_la_question or not majoritairement_francais)]
    return fautifs


def _mots_de_la_langue_de_la_question(termes: Sequence[str], question: str,
                                      vocabulaire: frozenset[str]) -> list[str]:
    """Les mots cherchés qu'une **preuve positive** rattache à la langue de la question.

    C'est la propriété que l'AC énonce — « `terms[]` toujours en français », donc *aucun terme resté
    dans la langue de la question* — et non celle que le corpus permet de trancher. Un terme est
    resté dans la langue de la question s'il appartient au vocabulaire de **cette langue-là** ; il
    ne l'est pas du seul fait que le corpus du guide l'ignore. Le seul vocabulaire de cette
    langue-là dont on dispose hors ligne est **la question elle-même**, flexions et dérivations
    comprises : c'est ce que ce contrôle emploie, et rien d'autre.

    Deux conditions, toutes deux nécessaires :

    - **Le corpus français ne l'atteste pas.** Un mot que le corpus emploie est du français, quelle
      que soit sa ressemblance avec la question — c'est le cas de `commune`, que la question
      anglaise emploie elle aussi et qui n'a jamais cessé d'être français.
    - **La question l'ancre, elle ou l'un de ses radicaux.** Le report n'est pas seulement littéral :
      une lettre de flexion suffisait à le désarmer (`escola` → `escolas`, sonde de la revue 2.4).
      L'ancrage est donc la relation de préfixe entre le mot cherché et un mot de la question, dans
      un sens ou dans l'autre — `escola` ancre `escolas` et `escolar`, `matricular` ancre
      `matricula`. Aucune longueur minimale n'entre ici : le mot d'ancrage doit lui-même être
      **absent du corpus français**, ce qui écarte les mots courts que les deux langues partagent
      (`i` et `commune` de la question anglaise, `a` de la question portugaise, tous attestés).

    Ce que ce contrôle ne prouve pas, et qui reste au contrôle strict : un syntagme étranger
    qu'aucun mot de la question n'ancre (`Wohnsitz`, `registo`). Il est attrapé autrement — voir les
    deux assertions de `test_six_reponses_sont_fideles_apres_retraduction`.
    """
    ancres = frozenset(m for m in _mots(question) if m not in vocabulaire)
    fautifs: list[str] = []
    for terme in termes:
        for mot in _mots(terme):
            if mot in vocabulaire:
                continue
            if any(mot.startswith(ancre) or ancre.startswith(mot) for ancre in ancres):
                fautifs.append(mot)
    return fautifs


def _mots_seuls_dans_leur_terme(termes: Sequence[str]) -> frozenset[str]:
    """Les mots qui composent à eux seuls un terme : la classe exacte du résidu nommé plus haut."""
    return frozenset(mots[0] for terme in termes if len(mots := _mots(terme)) == 1)


def _termes_majoritairement_attestes(termes: Sequence[str], vocabulaire: frozenset[str]) -> bool:
    """Les termes cherchés, **pris ensemble**, sont-ils majoritairement du français attesté ?

    La même propriété de composition que `_mots_non_attestes` applique à un terme, portée à l'unité
    dont AD-5 parle : `terms[]`. Un mot que le corpus ignore, seul dans son terme et minoritaire
    dans la liste, est le résidu assumé ; deux termes étrangers isolés qui composent l'essentiel de
    la recherche n'en sont plus un.
    """
    mots = [mot for terme in termes for mot in _mots(terme)]
    return sum(1 for mot in mots if mot in vocabulaire) * 2 > len(mots)


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
    # --- ce qu'un cognat français adoubait (sondes de la revue, tour final) ---
    # `residence` est du français attesté : il excusait `permit` à lui seul. Une majorité ne se
    # laisse pas conférer par un voisin — un mot attesté sur deux n'est pas une majorité.
    (["residence permit"], _question("en-arrivee"), ["permit"]),
    # `commune` adoubait quatre mots anglais d'un coup
    (["school registration in the commune"], _question("en-arrivee"),
     ["school", "registration", "in", "the"]),
    # même classe en allemand : `social` est du français attesté et adoubait `Wohnsitz`, que la
    # question n'emploie pas — le report littéral ne pouvait donc pas l'attraper
    (["Wohnsitz social"], _question("de-arrivee"), ["wohnsitz"]),
    # et en portugais : `nacional` n'est pas attesté, `national` l'est, et il adoubait `registo`
    (["registo national"], _question("pt-arrivee"), ["registo"]),
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
    assert _mots_non_attestes(termes, question, vocabulaire_francais(index)) == fautifs


@pytest.mark.parametrize(("termes", "question", "fautifs"), [
    # --- ce que la question ancre : le report, littéral ou fléchi ---
    # le contre-exemple de la revue 2.4, verbatim dans la question
    (["école", "Schulbesuch"], DE_SCHULBESUCH, ["schulbesuch"]),
    (["school registration"], EN_INSCRIPTION, ["school", "registration"]),
    # une lettre de flexion : `escola` de la question ancre `escolas`
    (["escolas"], _question("pt-ecole"), ["escolas"]),
    # deux dérivés des mots de la question, jamais verbatim
    (["matrícula escolar"], _question("pt-ecole"), ["matricula", "escolar"]),
    # l'ancrage n'excuse rien : un mot portugais dans un terme par ailleurs français
    (["inscription à l'escola"], _question("pt-ecole"), ["escola"]),
    # --- ce que le corpus atteste n'est jamais « resté dans la langue de la question » ---
    # `commune` est dans la question anglaise **et** dans le corpus : c'est du français
    (["déclaration à la commune"], _question("en-arrivee"), []),
    (["ADEM"], _question("de-adem"), []),
    # `i` de la question anglaise est attesté par le corpus : il n'ancre donc rien, et
    # `inscription` ne devient pas un mot anglais parce qu'il commence par la même lettre
    (["inscription scolaire", "scolarisation des enfants"], _question("en-ecole"), []),
    # --- le faux positif que ce contrôle existe pour ne plus produire (mesuré le 03/09) ---
    # « scolarisation » : du français que le corpus n'emploie pas, que la question anglaise n'ancre
    # pas. Le contrôle strict le signale — c'est son résidu nommé —, celui-ci non.
    (["scolarisation"], _question("en-ecole"), []),
    (["scolarité"], _question("pt-ecole"), []),
    # --- ce que ce contrôle ne prouve pas, et qu'il ne prétend pas prouver ---
    # aucun mot de la question de-arrivee n'ancre `Wohnsitz` : la preuve positive manque, et c'est
    # le contrôle strict qui l'attrape (témoin ci-dessus, et les deux assertions du témoin live)
    (["Wohnsitz social"], _question("de-arrivee"), []),
    (["registo de residência"], _question("pt-arrivee"), []),
    # un terme vide n'invente pas de faute
    (["inscription scolaire", ""], _question("en-ecole"), []),
])
def test_la_preuve_positive_nomme_la_langue_de_la_question(termes: list[str], question: str,
                                                           fautifs: list[str],
                                                           index: Index) -> None:
    """Ce qu'on peut prouver — et, autant que le reste, ce qu'on ne prouve pas."""
    assert _mots_de_la_langue_de_la_question(
        termes, question, vocabulaire_francais(index)) == fautifs


def test_ce_que_le_temoin_live_exige_du_residu(index: Index) -> None:
    """Les deux garde-fous qui empêchent le résidu de devenir une porte de sortie.

    Sans eux, porter le témoin live sur la seule preuve positive laisserait passer un syntagme
    étranger qu'aucun mot de la question n'ancre. Avec eux, un tel syntagme rougit encore : ou bien
    ses mots ne sont pas seuls dans leur terme, ou bien la liste cherchée cesse d'être
    majoritairement du français attesté.
    """
    vocabulaire = vocabulaire_francais(index)
    # le cas réel qui a motivé le correctif : un seul mot du résidu, minoritaire, les deux gardes tiennent
    reel = ["scolarisation", "inscription scolaire", "enfants", "école"]
    residu = _mots_non_attestes(reel, _question("en-ecole"), vocabulaire)
    assert residu == ["scolarisation"]
    assert set(residu) <= _mots_seuls_dans_leur_terme(reel)
    assert _termes_majoritairement_attestes(reel, vocabulaire)
    # un syntagme étranger non ancré : ses mots ne sont pas seuls dans leur terme
    syntagme = ["Wohnsitz social"]
    assert not set(_mots_non_attestes(syntagme, _question("de-arrivee"), vocabulaire)) \
        <= _mots_seuls_dans_leur_terme(syntagme)
    # deux termes étrangers isolés : chacun est seul dans son terme, mais la liste n'est plus
    # majoritairement française — c'est le second garde qui les attrape
    isoles = ["Arbeitslosigkeit", "Arbeitssuche"]
    assert set(_mots_non_attestes(isoles, _question("de-adem"), vocabulaire)) \
        <= _mots_seuls_dans_leur_terme(isoles)
    assert not _termes_majoritairement_attestes(isoles, vocabulaire)


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
    assert _mots_non_attestes(["escolas"], _question("pt-ecole"), vocabulaire) == ["escolas"]
    assert _mots_non_attestes(["scolarité"], _question("pt-ecole"), vocabulaire) == ["scolarite"]
    # Et la porte de sortie est la même pour les deux : dès que le terme est **majoritairement**
    # français, la forme dérivée y passe — jamais parce qu'un seul voisin l'adoube.
    assert _mots_non_attestes(["scolarité des enfants"], _question("pt-ecole"), vocabulaire) == []


@pytest.mark.parametrize(("cas", "langue", "question"), CAS, ids=[c[0] for c in CAS])
async def test_six_reponses_sont_fideles_apres_retraduction(cas: str, langue: str, question: str,
                                                             index: Index,
                                                             monkeypatch: pytest.MonkeyPatch,
                                                             llm_recorder: LLMRecorder) -> None:
    """L'échantillon de six réponses de l'AC 2.4, **porté sur le chemin servi**.

    Il épinglait `variant="deterministe"` ; la variante est partie avec les passes de code qui
    choisissaient (story 5.6, T2), et le témoin avec elle — ce qui a fait tomber le scellé du gate
    (`server/evals/reference/retraductions.yaml` lie chaque cas à ce `test_id`). Il est réécrit ici
    **sans épingler aucune variante** : `variant=None` laisse le pipeline servir celle que
    `Settings.retrieval_variant` désigne, c'est-à-dire ce qu'un utilisateur reçoit. Un témoin de
    fidélité qui nomme sa variante mesure une implémentation ; celui-ci mesure la réponse servie, et
    il suivra le défaut sans qu'on ait à le rouvrir.

    Les six fixtures sont **à réenregistrer** : le corps de requête de la navigation n'a rien de
    commun avec celui de la variante retirée, et le rejeu lève `FixtureMissing` tant que la dépense
    n'a pas été faite (nommées dans la passation T6).
    """
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

    # Ce que le témoin rejoue est la variante **servie**, lue sur la configuration : le scellé peut
    # dire « ce test rejoue le chemin servi » sans nommer une implémentation.
    assert trace.variant == settings.retrieval_variant

    (parsed,) = vues
    assert isinstance(parsed, ParsedQuestion), "la question devait être autonome"
    assert parsed.terms, "aucun terme cherché : l'AC ne serait pas exercée"
    # `termes_de_recherche()` et non `terms` seul : c'est ce que la lecture cherche réellement et ce
    # que l'`AbsenceProof` publie (`terms_searched`), donc `terms[]` **et** `scope.themes[]` — le
    # prompt exige le français des deux, et un thème non traduit relèverait de la même régression.
    cherches = parsed.termes_de_recherche()
    vocabulaire = vocabulaire_francais(index)
    # **La propriété est celle de l'AC, pas celle que le corpus permet de trancher (T7).** Ce témoin
    # exigeait `_mots_non_attestes(...) == []`, c'est-à-dire que le modèle ne cherche aucun mot que
    # le corpus du guide n'emploie pas. Le corpus est un échantillon de français, pas un lexique :
    # le 03/09/2026 le cas `en-ecole` est ressorti rouge sur « scolarisation », du français fidèle,
    # et le témoin accusait alors la réponse d'un défaut qui était le sien. Ce qui est exigé ici est
    # donc la **preuve positive** — aucun terme que la question, dans sa langue, ancre.
    assert _mots_de_la_langue_de_la_question(cherches, question, vocabulaire) == [], (
        f"termes restés dans la langue de la question : {cherches}")
    # Et le résidu du contrôle strict ne devient pas une porte : ce qu'il reste doit être exactement
    # la classe qu'il nomme — un mot seul dans son terme —, et les termes cherchés, pris ensemble,
    # doivent rester majoritairement du français attesté. Un syntagme étranger qu'aucun mot de la
    # question n'ancre (`Wohnsitz social`, `registo de residência`) rougit donc toujours ici, sans
    # que le contrôle ait eu à décider si « scolarisation » est du français.
    residu = _mots_non_attestes(cherches, question, vocabulaire)
    assert set(residu) <= _mots_seuls_dans_leur_terme(cherches), (
        f"mots que le corpus n'atteste pas, à l'intérieur d'un terme composé : {residu} "
        f"— ce n'est plus le résidu nommé du contrôle, c'est un syntagme non traduit ({cherches})")
    assert _termes_majoritairement_attestes(cherches, vocabulaire), (
        f"les termes cherchés ne sont pas majoritairement du français attesté : {cherches}")

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
