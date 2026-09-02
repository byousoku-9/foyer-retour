"""Trois scénarios réels du suivi conversationnel (story 2.2), enregistrés avec la clé, rejoués sans.

La reprise différée de 1.4/1.5 réclamait la **mesure** : le signal de clarification, son
court-circuit et son affichage existaient, mais « aucune fixture live n'exerce encore ce chemin ».
C'est ce que ce module fait, sur le corpus `lux-guide` réel et avec le périmètre que le serveur
rend vraiment à *comprendre* (`Corpus.perimetres`, story 2.1) :

1. **Suivi résolu** — « Et pour la voiture ? » après un tour sur les démarches de logement : AD-5
   demande une `question_resolue` **autonome**, qui nomme la voiture *et* reprend le prédicat de
   l'historique. C'est la moitié de l'AC que seul un appel réel peut montrer : un mock résout ce
   qu'on lui fait dire.
2. **Anaphore irrésoluble** — « Et celui-là, il faut le faire quand ? » sans historique : AD-5,
   mot pour mot, veut une `ClarificationRequise` et **aucune** `question_resolue` reconstituée. Le
   type rendu est l'assertion ; qu'elle se termine par « ? » dit que c'est bien une question posée
   à l'utilisateur, pas un constat d'échec.
3. **Boucle refermée** — la même anaphore, mais la clarification est cette fois dans l'historique,
   suivie de « du permis de conduire » : la question redevient autonome. C'est cette mesure-là qui
   désigne le défaut que la story corrige — elle ne marche **que** parce que le tour assistant porte
   la question posée, ce que la page n'y mettait pas (voir `web/app/chat.js::tourAssistant`).

Les assertions tolèrent que le modèle choisisse ses mots (`_evoque`, patron de
`tests/test_steps_live.py::_covers`) : un test live qui asserte une chaîne exacte rendue par le
modèle rougit au premier synonyme et cesse de mesurer quoi que ce soit.

Un seul appel modèle par scénario : *comprendre* est la seule étape qui voie l'historique avec
*rédiger* (AD-1), et elle tranche seule les trois cas — aucune étape au-delà n'est facturée. Son
étage est celui que la configuration lui affecte (AD-9), lu sur `Settings` et jamais recopié ici.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.loader import Corpus, load_corpus
from server.app.corpus.text import normalize
from server.app.domain.profil import Profil
from server.app.domain.question import ClarificationRequise, ParsedQuestion, Turn
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.steps.comprendre import comprendre
from server.app.steps.restituer import PHRASES_DE_REFUS
from tests.fixtures import LLMRecorder
from tests.helpers_tiers import verifier_etage
from tests.llm_fake import RecordedAnthropic

ROOT = Path(__file__).resolve().parents[1]
DOC_ID = "lux-guide"

# Le tour de logement dont le prédicat — les démarches d'arrivée — est **transférable** à un autre
# sujet. La mesure préalable (§ Design Notes de la spec 2.2) a montré qu'un tour de bail (« la
# garantie locative est plafonnée à trois mois ») donne au contraire une clarification : le prédicat
# ne se transfère pas à une voiture, et c'est le comportement qu'AD-5 demande. On mesure donc l'AC
# sur sa formulation littérale, sans durcir le prompt pour forcer une résolution que le modèle a
# raison de refuser.
LOGEMENT = "Quelles démarches pour mon logement en arrivant ?"
REPONSE_LOGEMENT = ("Vous disposez de huit jours après votre arrivée pour déclarer votre nouvelle "
                    "adresse au Biergercenter de votre commune de résidence, muni d'une pièce "
                    "d'identité et de votre contrat de bail.")
SUIVI = "Et pour la voiture ?"

ANAPHORE = "Et celui-là, il faut le faire quand ?"
# La clarification **littérale** obtenue au scénario 2 (consignée dans `docs/tests-live.md`), écrite
# ici en dur plutôt que chaînée depuis l'appel précédent : la clé d'une fixture est le contenu de la
# requête, et un historique reconstruit à chaque exécution depuis une sortie de modèle ne se
# rejouerait jamais deux fois à l'identique.
CLARIFICATION_POSEE = "De quel document ou démarche parlez-vous ?"
# Exactement ce que `web/app/chat.js::tourAssistant` compose et que la page conserve : la question
# posée, puis la phrase de refus, dans l'ordre où `vueReponse` les peint. C'est « ce que l'assistant
# a dit », et c'est la seule définition non arbitraire du tour assistant.
TOUR_ASSISTANT = CLARIFICATION_POSEE + " " + PHRASES_DE_REFUS["fr"]["clarification_requise"]
REPONSE_COURTE = "du permis de conduire"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(ROOT / "data", allow_ungated=True)


def _settings() -> Settings:
    # Seuils par défaut de `config.py`, jamais ceux du `.env` du poste : ils sont rendus dans le
    # préfixe de *comprendre* (`question_max_terms`…), donc dans la clé de requête — un `.env` local
    # qui les surcharge rendrait le rejeu hors ligne impossible (revue 1.4).
    return Settings(_env_file=None, anthropic_api_key="")


def _client(llm_recorder: LLMRecorder) -> LlmClient:
    return LlmClient(_settings(), anthropic_client=RecordedAnthropic(llm_recorder))


def _budget() -> RequestBudget:
    s = _settings()
    return RequestBudget(deadline_s=s.deadline_s, max_attempts=s.max_llm_attempts,
                         max_cost_eur=s.max_cost_eur_per_request)


def _evoque(libelles: list[str], *attendus: str) -> bool:
    """L'un des libellés (normalisé) contient l'un des mots attendus — le modèle choisit les siens."""
    normalises = [normalize(t) for t in libelles]
    return any(attendu in n for n in normalises for attendu in attendus)


async def _comprendre(corpus: Corpus, llm_recorder: LLMRecorder, question: str,
                      historique: list[Turn]) -> tuple[ParsedQuestion | ClarificationRequise, RequestBudget]:
    """L'étape telle que `pipelines/guide.py` l'appelle : même périmètre, mêmes seuils, un seul appel."""
    budget = _budget()
    sortie, step = await comprendre(question, historique, Profil(), client=_client(llm_recorder),
                                    budget=budget, settings=_settings(),
                                    perimetre=corpus.perimetres.get(DOC_ID, ""))
    # AD-9 : *comprendre* tourne à l'étage que la configuration lui affecte, et c'est le seul appel
    # avant tout court-circuit — le compte d'appels **est** l'assertion « aucune étape au-delà n'est
    # facturée ». Le tier attendu se lit sur `Settings`, il n'est pas recopié : l'AC porte sur
    # l'affectation d'AD-9, pas sur la valeur qu'elle avait le jour où ce test a été écrit.
    assert step.name == "comprendre"
    verifier_etage(step, _settings(), appels=1)
    assert budget.attempts == 1
    assert step.usage.cost_eur > 0
    return sortie, budget


async def test_un_suivi_est_resolu_par_lhistorique(corpus: Corpus, llm_recorder: LLMRecorder) -> None:
    """AC 2.2 : « Et pour la voiture ? » devient une question autonome grâce à l'historique.

    AD-5 exige que la `question_resolue` soit **autonome** : elle doit donc nommer la voiture (que
    seule la question porte) *et* le prédicat repris du tour précédent (que seul l'historique porte).
    Vérifier l'un sans l'autre laisserait passer les deux échecs que la story vise — une résolution
    qui perd le sujet, ou une question recopiée telle quelle.

    Ce test tient la moitié que les tests mockés ne peuvent pas tenir (revue 2.2, P8) : dans
    `tests/test_pipeline_guide.py`, `question_resolue` et `terms` sont posés côte à côte par le
    script, si bien que le lien entre les deux n'y est jamais exercé. Ici ils sortent du **même**
    appel de *comprendre*, et c'est ce qui rend vrai « les termes que le refus consulte sont ceux de la
    question résolue ».
    """
    historique = [Turn(role="user", texte=LOGEMENT), Turn(role="assistant", texte=REPONSE_LOGEMENT)]
    sortie, budget = await _comprendre(corpus, llm_recorder, SUIVI, historique)

    assert isinstance(sortie, ParsedQuestion), getattr(sortie, "clarification", None)
    assert sortie.intent in ("question", "suivi")
    assert sortie.language == "fr"
    resolue = [sortie.question_resolue]
    assert _evoque(resolue, "voiture", "vehicule", "automobile", "auto"), sortie.question_resolue
    assert _evoque(resolue, "demarche", "arriv", "formalite", "declar"), sortie.question_resolue
    # `terms` vise le véhicule : c'est lui qui commande la recherche, pas la question rendue (AD-5).
    assert sortie.terms and _evoque(sortie.terms, "voiture", "vehicule", "auto", "immatricul",
                                    "permis", "conduire"), sortie.terms
    assert budget.cost_eur < _settings().max_cost_eur_per_request


async def test_une_anaphore_sans_historique_donne_une_clarification(corpus: Corpus,
                                                                    llm_recorder: LLMRecorder) -> None:
    """AD-5, mot pour mot : « une anaphore non résoluble avec l'historique produit
    `Answer.clarification` — *comprendre* ne fabrique **jamais** une `question_resolue` ».

    L'exclusivité est portée par le schéma de sortie, donc par la relance motivée : le **type** rendu
    est ici toute l'assertion — un `ClarificationRequise` ne porte aucune `question_resolue`, ni
    inventée ni reprise telle quelle, et rien ne peut donc partir en recherche.
    """
    sortie, _budget = await _comprendre(corpus, llm_recorder, ANAPHORE, [])

    assert isinstance(sortie, ClarificationRequise), getattr(sortie, "question_resolue", None)
    assert sortie.language == "fr"
    # C'est une **question posée à l'utilisateur** (AD-5), pas un constat d'échec : la page la peint
    # comme telle, avant la phrase de refus (story 1.7).
    assert sortie.clarification.strip().endswith("?"), sortie.clarification
    assert "question_resolue" not in sortie.model_dump()


async def test_la_boucle_refermee_rend_la_question_autonome(corpus: Corpus,
                                                            llm_recorder: LLMRecorder) -> None:
    """AC 2.2 : la clarification étant dans l'historique, une réponse de trois mots suffit.

    C'est **la** mesure qui désigne le défaut corrigé par la story : elle ne marche que parce que le
    tour assistant porte la question posée. Tant que la page ne poussait que `Answer.texte` — la
    phrase générique —, *comprendre* recevait un historique où sa propre question ne figurait pas et
    reposait indéfiniment la même. L'historique employé ici est donc **littéralement** ce que
    `tourAssistant` compose.
    """
    historique = [Turn(role="user", texte=ANAPHORE), Turn(role="assistant", texte=TOUR_ASSISTANT)]
    sortie, _budget = await _comprendre(corpus, llm_recorder, REPONSE_COURTE, historique)

    assert isinstance(sortie, ParsedQuestion), getattr(sortie, "clarification", None)
    assert sortie.intent in ("question", "suivi")
    resolue = [sortie.question_resolue]
    assert _evoque(resolue, "permis"), sortie.question_resolue
    # Autonome : plus rien de l'anaphore ne subsiste dans la question qui part en recherche.
    assert "celui" not in normalize(sortie.question_resolue), sortie.question_resolue
    assert sortie.terms and _evoque(sortie.terms, "permis", "conduire", "conduite"), sortie.terms
