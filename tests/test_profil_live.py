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
from server.app.domain.profil import Profil, noeuds_du_profil
from server.app.domain.question import ParsedQuestion
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.pipelines.guide import repondre_guide
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


async def test_le_profil_fait_ouvrir_la_fiche_ecole_par_le_pipeline(index: Index,
                                                                    llm_recorder: LLMRecorder) -> None:
    """L'AC entière, sur le corpus réel : le pipeline ouvre `lux-guide:fecole` grâce au profil.

    Le parcours du guide conditionne cette fiche sur `{enfants: true}` (donnée de la source), le
    pipeline la résout par code et *retrouver* lui réserve une place parmi `max_opens`. La trace le
    dit — c'est littéralement ce que l'AC exige — et le `CheckResult` ne nomme que des `node_id`,
    produits par l'ingestion : ni clé de profil, ni terme cherché, ni contenu de bloc (AD-10, AD-15).
    """
    budget = _budget()
    answer, trace = await repondre_guide(SCOLAIRE, [], PROFIL_ENFANTS, corpus=index.corpus, index=index,
                                         client=_client(llm_recorder), settings=_settings(),
                                         request_id="live-profil", budget=budget,
                                         variant="deterministe")
    document = index.corpus.documents[DOC_ID]
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    ouverts = {document.node_of(b) for b in retrouver.opened_block_ids}
    assert ECOLE in ouverts, sorted(ouverts)

    # Le profil a bien désigné la fiche : c'est une donnée de la source, pas un jugement du modèle.
    assert ECOLE in noeuds_du_profil(document.parcours, PROFIL_ENFANTS)
    # La trace dit ce que le profil a fait — soit il a réservé une place à la fiche, soit la question
    # la classait déjà dans le quota. L'assertion porte sur les deux issues, parce que le rang de
    # `ecole` parmi les candidats dépend du vocabulaire que le modèle a réellement choisi : forcer
    # l'une des deux ferait rougir le test au premier synonyme, sans qu'aucune règle ait bougé.
    (check,) = [c for c in retrouver.checks if c.name == "noeuds_du_profil"]
    assert check.ok is True
    assert ECOLE in check.detail or check.detail.startswith("aucune place réservée"), check.detail
    # AD-10 / AD-15 : aucune clé de profil, aucun terme cherché, aucun contenu de bloc dans la trace.
    assert "enfants" not in check.detail

    # AD-4 : `found`/`complete` restent calculés par le code, et la réponse tient sous le plafond.
    assert answer.complete == (answer.found and not answer.unknown)
    assert budget.cost_eur < _settings().max_cost_eur_per_request
    assert trace.thresholds["profil_max_opens"] == _settings().profil_max_opens


async def test_le_meme_scenario_sans_profil_ne_reserve_rien(index: Index,
                                                            llm_recorder: LLMRecorder) -> None:
    """Le témoin négatif : même question, profil vide ⇒ aucune place réservée, aucune trace ajoutée.

    Le rappel lexical reste libre de retrouver `ecole` par les termes de la question ; ce test isole
    l'effet propre du profil, qui est l'ajout d'une désignation et d'une place réservée.
    """
    answer, trace = await repondre_guide(SCOLAIRE, [], Profil(), corpus=index.corpus, index=index,
                                         client=_client(llm_recorder), settings=_settings(),
                                         request_id="live-profil-vide", budget=_budget(),
                                         variant="deterministe")
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    assert [c.name for c in retrouver.checks if c.name == "noeuds_du_profil"] == []
    assert noeuds_du_profil(index.corpus.documents[DOC_ID].parcours, Profil()) == []
    assert answer.complete == (answer.found and not answer.unknown)
    # Story 2.7 : « inscription scolaire » touche désormais un bloc qui contient seulement
    # « scolaire ». C'est un rappel lexical normal, pas une réservation cachée du profil.
    ouverts = {index.corpus.documents[DOC_ID].node_of(b) for b in retrouver.opened_block_ids}
    assert ECOLE in ouverts, sorted(ouverts)


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
