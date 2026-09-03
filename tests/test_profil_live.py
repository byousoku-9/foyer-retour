"""Le profil, mesuré de bout en bout sur le corpus réel (story 2.3), enregistré avec la clé, rejoué sans.

Deux moitiés, qu'aucun mock ne peut tenir ensemble :

1. **Les thèmes** — un profil `enfants` fait sortir de *comprendre* des thèmes scolaires. C'est le
   jugement du modèle sur trois clés du questionnaire, et c'est ce qui **cherche** : les thèmes sont
   fondus dans les termes de recherche (`ParsedQuestion.termes_de_recherche()`).
2. **Le parcours** — le même profil fait ouvrir la fiche `ecole` par le pipeline entier. C'est la
   donnée de la source (`Document.parcours`, écrite par l'auteur du guide), et c'est ce qui
   **classe** : la fiche est promue parmi les `max_opens` nœuds ouverts, et la trace le dit.

Les deux se complètent, et il fallait un appel réel pour le montrer : dans les tests mockés, les
thèmes sont posés par le script, et le rang de la fiche `ecole` parmi les candidats dépend du
vocabulaire que le modèle a réellement choisi. Le témoin négatif — la même question avec un profil
vide — vérifie que le pipeline ne réserve aucune place et ne trace aucune désignation de profil.
Depuis la story 2.7, les mots d'une forme composée contribuent chacun au rappel : la question
scolaire peut donc retrouver `ecole` sans profil, ce qui reste distinct de la réservation pilotée
par le parcours.

Les assertions tolèrent que le modèle choisisse ses mots (`_evoque`, patron de
`tests/test_suivi_live.py`) : un test live qui asserte une chaîne exacte rendue par le modèle rougit
au premier synonyme et cesse de mesurer quoi que ce soit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
from server.app.domain.profil import Profil
from server.app.domain.question import ParsedQuestion
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.steps.comprendre import comprendre
from tests.fixtures import LLMRecorder
from tests.helpers_tiers import verifier_etage
from tests.llm_fake import RecordedAnthropic

ROOT = Path(__file__).resolve().parents[1]
DOC_ID = "lux-guide"
ECOLE = f"{DOC_ID}:fecole"

# Une question scolaire posée **sans** nommer la fiche : c'est le profil qui doit faire le reste.
SCOLAIRE = "Quelles démarches dois-je faire avant la rentrée pour mes enfants ?"
# Le profil du questionnaire du site, avec ses valeurs exactes (`web/app/chat.js::CHAMPS`).
PROFIL_ENFANTS = Profil(situation="En famille", enfants="2", statut="Salarie", logement="Louer",
                        vehicule="Non", horizon="Je prepare mon depart")


@pytest.fixture(scope="module")
def index() -> Index:
    return Index(load_corpus(ROOT / "data", allow_ungated=True))


def _settings() -> Settings:
    # Seuils par défaut de `config.py`, jamais ceux du `.env` du poste : ils décident des blocs
    # envoyés à *rédiger*, donc de la clé de requête — un `.env` local qui les surcharge rendrait le
    # rejeu hors ligne impossible (revue 1.4).
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


async def test_un_profil_enfants_fait_sortir_des_themes_scolaires(index: Index,
                                                                  llm_recorder: LLMRecorder) -> None:
    """AD-5 : « `scope` dérivé du profil : enfants → école/allocations ». La moitié « thèmes » de l'AC.

    Le profil est envoyé **brut** (AD-11) et filtré par `PROFIL_KEYS` ; *comprendre* le voit dans son
    unique appel et en tire des axes de recherche. C'est ce qui **cherche** — le parcours,
    lui, ne fait que classer ce que la recherche a trouvé.
    """
    budget = _budget()
    parsed, step = await comprendre(SCOLAIRE, [], PROFIL_ENFANTS, client=_client(llm_recorder),
                                    budget=budget, settings=_settings(),
                                    perimetre=index.corpus.perimetres.get(DOC_ID, ""),
                                    parcours=index.corpus.documents[DOC_ID].parcours)
    assert isinstance(parsed, ParsedQuestion), getattr(parsed, "clarification", None)
    assert parsed.intent in ("question", "suivi") and parsed.language == "fr"
    assert _evoque(parsed.scope.themes, "ecole", "scolar", "enseign"), parsed.scope.themes
    # Et la seconde moitié, dans le **même** `scope` (revue Codex 2.3, B1) : les nœuds désignés sont
    # construits ici, par du code pur sur `Document.parcours`, et c'est ce que *retrouver* lit.
    assert ECOLE in parsed.scope.noeuds, parsed.scope.noeuds
    # AD-9 : un seul appel, à l'étage que la configuration affecte à *comprendre* — lu sur
    # `Settings`, jamais recopié (le tier a été promu depuis ; l'AC, elle, n'a pas bougé).
    verifier_etage(step, _settings(), appels=1)
    assert budget.attempts == 1


# --- « statut → affiliation, impôts », le troisième terme de l'AC (revue Codex 2.3, B2) ----------
# Les deux premiers termes de l'AC (`enfants` → école/garde/allocations, `vehicule` →
# permis/assurance auto) sont tenus **exactement** par le parcours ingéré. Le troisième ne l'est pas :
# aucune étape de la `timeline` n'attache une fiche fiscale au statut, et le canal ne peut donc être
# que celui des thèmes. La table du prompt écrivait « affiliation, sécurité sociale » et s'arrêtait
# là ; « impôts » n'avait aucun chemin, ni par le parcours ni par le prompt.
#
# Le mot ajouté à la table est **`impôt`**, au singulier, et ce n'est pas une paraphrase de l'AC : la
# recherche est littérale sur des formes normalisées, et `impots` ne touche qu'une seule FAQ du guide
# quand `impot` atteint les fiches `impots_classes`, `deductions`, `conseil_fiscal` et `fachat`. Le
# singulier est strictement plus large que le pluriel qu'il contient.
PROFILS_STATUT = {
    "salarie": Profil(situation="Seul", enfants="Aucun", statut="Salarie", logement="Louer",
                      vehicule="Non", horizon="Je viens d arriver"),
    "independant": Profil(situation="Seul", enfants="Aucun", statut="Independant", logement="Louer",
                          vehicule="Non", horizon="Je viens d arriver"),
}
# Une question qui ne nomme ni l'affiliation ni l'impôt : tout ce qui en sort vient du profil.
PREMIERE_ANNEE = "Je viens d'arriver au Luxembourg, que dois-je prévoir pour ma première année ?"
FISCALES = {f"{DOC_ID}:fimpots_classes", f"{DOC_ID}:fdeductions", f"{DOC_ID}:fconseil_fiscal",
            f"{DOC_ID}:fimpatries", f"{DOC_ID}:finterets"}


@pytest.mark.parametrize("statut", sorted(PROFILS_STATUT))
async def test_le_statut_fait_sortir_des_themes_daffiliation_et_dimpot(
        index: Index, llm_recorder: LLMRecorder, statut: str) -> None:
    """AC 2.3, premier **Then**, troisième terme : « statut → affiliation, impôts ».

    Salarié comme indépendant : la clé `statut` doit ouvrir les deux axes. L'assertion ne s'arrête
    pas au libellé rendu — un thème qui ne trouve rien ne « priorise » aucun nœud —, elle vérifie que
    les termes réellement cherchés (`ParsedQuestion.termes_de_recherche()`, la source unique de
    *retrouver* et de l'`AbsenceProof`) atteignent au moins une fiche fiscale du guide.
    """
    profil = PROFILS_STATUT[statut]
    parsed, step = await comprendre(PREMIERE_ANNEE, [], profil, client=_client(llm_recorder),
                                    budget=_budget(), settings=_settings(),
                                    perimetre=index.corpus.perimetres.get(DOC_ID, ""),
                                    parcours=index.corpus.documents[DOC_ID].parcours)
    assert isinstance(parsed, ParsedQuestion), getattr(parsed, "clarification", None)
    assert _evoque(parsed.scope.themes, "affiliation", "securite sociale"), parsed.scope.themes
    assert _evoque(parsed.scope.themes, "impot", "fiscal"), parsed.scope.themes
    touches = {node_id for _b, node_id in index.chercher(parsed.termes_de_recherche(),
                                                         limit=_settings().search_limit, doc_id=DOC_ID)}
    assert touches & FISCALES, sorted(touches)
    verifier_etage(step, _settings(), appels=1)
