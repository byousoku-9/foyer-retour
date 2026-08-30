"""Matrice I/O de `pipelines/sinistre.py` (spec 1.8), LLM mocké : les cinq étapes, le verdict d'AD-6,
la relance unique d'AD-3, les bornes d'entrée et la trace d'AD-10.

Comme pour le guide, `FakeAnthropic` lève sur tout appel non scripté : la **longueur du script est
une assertion**. C'est ainsi que « *vérifier* n'a fait qu'**un** appel `micro` » (AD-9 amendé) se
vérifie sans compter les appels à la main.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, NamedTuple

import anthropic
import httpx
import pytest

from server.app.config import Settings
from server.app.corpus.dictionary import Dictionnaire, forme
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.answer import AnswerDraft, Verification
from server.app.domain.document import Document, Node
from server.app.domain.errors import (
    BudgetExceeded,
    CorpusUnavailable,
    InvalidRequest,
    LlmUnavailable,
)
from server.app.domain.ingest import Gate, ManifestEntry
from server.app.domain.question import Faits
from server.app.domain.retrieval import RetrievalResult
from server.app.domain.trace import CheckResult, StepTrace
from server.app.domain.verdict import (
    KINDS_DECISIONNELS,
    ChampsApplicabilite,
    ClaimJugee,
    ClauseCitee,
    MissingPackage,
    decider,
)
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines import sinistre
from server.app.pipelines.commun import retrieval_budget
from server.app.steps.restituer import PHRASES_DE_LACUNE, PHRASES_DE_REFUS_SINISTRE
from tests.llm_fake import FakeAnthropic, fake_message

DOC_ID = "cg"


GARANTIE = ("Les dégâts occasionnés au mobilier assuré et au bâtiment désigné par un événement "
            "soudain, résultant de l'action subite de la chaleur, sont couverts.")
EXCLUSION = ("Pour les extensions mentionnées aux points 2.1 et 2.2, les dégâts occasionnés au "
             "bâtiment par l'action subite de la chaleur sont exclus.")
# Revue Codex 1.8 (B3, tour 3) : l'exclusion du socle exigeait « intentionnellement », une qualité
# qu'aucun fait de la bougie n'établit — depuis que le **texte de la clause** est relu, elle ne
# pouvait donc plus s'appliquer, et la fixture `non_couvert` de l'AC devenait injouable. Elle exige
# maintenant la même qualité que la garantie (« action subite de la chaleur »), que les faits
# déclarés établissent : l'exclusion mord parce que les faits la portent, pas par défaut.
EXCLUSION_SOCLE = ("Sont exclus, en toute circonstance, les dommages de brûlure résultant de "
                   "l'action subite de la chaleur.")
CONDITION = "La garantie n'est acquise que si le bien est occupé de manière permanente."
DEFINITION = "Le contenu comprend le mobilier de jardin et les objets confiés à un Assuré."

Q_GARANTIE = "dégâts occasionnés au mobilier assuré et au bâtiment désigné par un événement soudain"
Q_EXCLUSION = "dégâts occasionnés au bâtiment par l'action subite de la chaleur sont exclus"
Q_EXCLUSION_SOCLE = "dommages de brûlure résultant de l'action subite de la chaleur"
Q_CONDITION = "n'est acquise que si le bien est occupé de manière permanente"
Q_DEFINITION = "contenu comprend le mobilier de jardin et les objets confiés"

QUESTION = "Ce sinistre est-il couvert par le contrat ?"
FAITS = Faits(date="2026-08-01", lieu="salon du domicile", montant_eur=1200.0,
              description="Une bougie posée sur une table a brûlé le mobilier de salon, "
                          "sans embrasement ni commencement d'incendie. La chute a été soudaine "
                          "et la chaleur a agi de façon subite.")


@pytest.fixture(scope="module")
def index() -> Index:
    """Mini-contrat à l'image du contrat AXA : un socle `commun`, une branche `extension`."""
    blocs = [
        {"block_id": f"{DOC_ID}:p1:1", "loc": "p1", "seq": 1, "kind": "heading", "text": "Incendie"},
        {"block_id": f"{DOC_ID}:p1:2", "loc": "p1", "seq": 2, "kind": "garantie", "text": GARANTIE,
         "kind_source": "manual", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p1:3", "loc": "p1", "seq": 3, "kind": "condition", "text": CONDITION,
         "kind_source": "manual", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p1:4", "loc": "p1", "seq": 4, "kind": "definition", "text": DEFINITION,
         "kind_source": "manual", "defines": "contenu", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p1:5", "loc": "p1", "seq": 5, "kind": "exclusion",
         "text": EXCLUSION_SOCLE, "kind_source": "manual", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p2:1", "loc": "p2", "seq": 1, "kind": "exclusion", "text": EXCLUSION,
         "kind_source": "manual", "scope_node_id": f"{DOC_ID}:ext"},
    ]
    doc = Document(
        doc_id=DOC_ID, kind="contrat", title="Mini contrat", edition="juin 2017",
        nodes=[Node(node_id=f"{DOC_ID}:socle", level=1, title="Socle commun",
                    items=[{"block_id": f"{DOC_ID}:p1:1"}, {"block_id": f"{DOC_ID}:p1:2"},
                           {"block_id": f"{DOC_ID}:p1:3"}, {"block_id": f"{DOC_ID}:p1:4"},
                           {"block_id": f"{DOC_ID}:p1:5"}]),
               Node(node_id=f"{DOC_ID}:ext", level=1, title="Extensions", scope={"kind": "extension"},
                    items=[{"block_id": f"{DOC_ID}:p2:1"}]),
               Node(node_id=f"{DOC_ID}:root", level=0, title="Contrat",
                    items=[{"node_id": f"{DOC_ID}:socle"}, {"node_id": f"{DOC_ID}:ext"}])],
        blocks=blocs)
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    manifest = {DOC_ID: ManifestEntry(status="servi", source_hash="sha-source", ingest_fingerprint="fp-1",
                                      document_hash="sha-doc", edition="juin 2017")}
    return Index(Corpus(documents={DOC_ID: doc}, manifest=manifest,
                        summaries={DOC_ID: "# Mini contrat\n- socle Socle commun\n- ext Extensions"}))


@pytest.fixture
def gate_du_mini_contrat(index: Index):
    """Gate du document interrogé, restauré après le cas nominal partagé par le module."""
    entree = index.corpus.manifest[DOC_ID]
    entree.gate = Gate(
        profile="vertical", source_hash=entree.source_hash,
        ingest_fingerprint=entree.ingest_fingerprint, cases_hash="cas-bougie",
        pipeline_digest="pipeline", prompts_digest="prompts", evals_ok=True,
        date="2026-08-25", cases=1, countersigned=False,
    )
    yield entree.gate
    entree.gate = None


def _settings(**kw) -> Settings:
    kw.setdefault("sinistre_doc_id", DOC_ID)
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget(deadline_s: float = 30.0) -> RequestBudget:
    return RequestBudget(deadline_s=deadline_s, max_attempts=6, max_cost_eur=0.10)


def _comprendre(intent: str = "question", *, terms: list[str] | None = None,
                clarification: str | None = None, language: str = "fr", **champs) -> dict:
    """`champs` surcharge la portée rendue (`bien`, `evenement`, `lieu`, `cause`, `moment`, `themes`).

    Ce sont des libellés **du modèle**, et la story 1.9 les affiche (`Answer.faits_compris`, D4) :
    pouvoir en scripter un hors borne est ce qui rend la règle « borner, jamais tronquer » testable.
    """
    resolue = None if clarification else "Le mobilier de salon brûlé par une bougie est-il couvert ?"
    return fake_message(model=TIERS["micro"], text=json.dumps({
        "intent": intent, "question_resolue": resolue, "clarification": clarification,
        "language": language,
        "terms": terms if terms is not None else ["mobilier", "chaleur", "contenu"],
        "themes": [], "facettes": ["couverture du sinistre"], "bien": "mobilier de salon",
        "evenement": "incendie sans embrasement", "lieu": "domicile", "cause": "bougie",
        "moment": "2026-08-01",
        # `champs` en dernier : ce que le test surcharge l'emporte sur le défaut.
        **champs}))


def _outils(*, termes: list[str] | None = None, node_id: str | None = None,
            stop_reason: str = "tool_use", **fenetre) -> dict:
    """Un tour de navigation par outils sur le mini-contrat témoin de ce fichier.

    Le même motif de scriptage qu'au guide (`tests/test_pipeline_guide.py`) : `FakeAnthropic` rend
    un message `tool_use`, et *retrouver* exécute réellement les outils sur l'index. Il ne sert
    qu'aux tests de route déjà écrits, que le changement de défaut de la story 4.2d oblige à
    scripter une navigation ; les preuves de 4.2d, elles, ont leur corpus neutre et leur propre
    `_navigation`.
    """
    contenu: list[dict] = []
    if termes is not None:
        contenu.append({"type": "tool_use", "id": "toolu_chercher", "name": "chercher",
                        "input": {"termes": termes}})
    if node_id is not None:
        contenu.append({"type": "tool_use", "id": "toolu_ouvrir", "name": "ouvrir_noeud",
                        "input": {"node_id": node_id, **fenetre}})
    return fake_message(model=TIERS["micro"], stop_reason=stop_reason, content=contenu)


def _rediger(*claims: tuple[str, str, list[tuple[str, str]]]) -> dict:
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": [{"text": f"Clause {cid}.", "kind": "factuel", "claim_ids": [cid]}
                     for cid, _, _ in claims],
        "claims": [{"claim_id": cid, "text": texte,
                    "quotes": [{"block_id": b, "quote": q} for b, q in quotes]}
                   for cid, texte, quotes in claims]}))


# Deux fragments **mot pour mot** des faits déclarés, chacun employant les mots d'une qualité : les
# deux conditions pour qu'elle soit tenue pour établie (revue Codex 1.8, B3, tour 2).
FRAGMENT = "la chaleur a agi de façon subite"
FRAGMENT_SOUDAIN = "La chute a été soudaine"
SOUDAIN, SUBITE = "caractère soudain de l'événement", "action subite de la chaleur"
# L'énumération **fidèle** des clauses de ce contrat de test : les deux qualités que leur texte écrit
# (« soudain », « subite »), chacune établie par un fragment des faits déclarés. C'est le défaut du
# script depuis la revue Codex 1.8 (B3, tour 3) : le code relit le texte de la clause et ajoute
# lui-même toute qualité que le modèle n'a pas nommée, si bien que deux listes vides ne valent plus
# « aucune qualité exigée ». Les tests qui ne portent pas sur ce contrôle n'ont donc pas à la
# réécrire ; ceux qui portent dessus donnent leurs listes explicitement.
QUALITES_FIDELES = ([SOUDAIN, SUBITE], [(SOUDAIN, FRAGMENT_SOUDAIN), (SUBITE, FRAGMENT)])


def _verifier(*entrees: tuple, nb_segments: int = 8, enumere: bool = True,
              facettes: list[list[str]] | None = None,
              segments: dict[int, bool] | None = None) -> dict:
    """`(claim_id, pertinente, fait_requis_present, option_requise, cp_requise, fait_manquant)`,
    éventuellement suivi de `(qualites_exigees, qualites_etablies)` — revue Codex 1.8 (B3).

    Une qualité établie est un libellé (le fragment cité est alors `FRAGMENT`) ou un couple
    `(qualite, fait_cite)`. `enumere=False` omet les deux listes, comme un modèle qui n'énumère rien.
    """
    applicabilite = []
    for entree in entrees:
        cid, _p, present, option, cp, manquant = entree[:6]
        bloc = {"claim_id": cid, "fait_requis_present": present, "option_requise": option,
                "cp_requise": cp, "fait_manquant": manquant}
        if enumere:
            # Défaut : l'énumération fidèle de la clause quand le modèle dit le fait requis présent,
            # les deux listes vides sinon — ce que le prompt exige (« si le périmètre n'est pas bon,
            # les deux listes sont vides »), et ce que le contrôle du tour 3 lit.
            fidele = QUALITES_FIDELES if present else ([], [])
            bloc["qualites_exigees"] = list(entree[6]) if len(entree) > 6 else list(fidele[0])
            bloc["qualites_etablies"] = [
                {"qualite": q, "fait_cite": FRAGMENT} if isinstance(q, str)
                else {"qualite": q[0], "fait_cite": q[1]}
                for q in (entree[7] if len(entree) > 7 else fidele[1])]
        applicabilite.append(bloc)
    return fake_message(model=TIERS["micro"], text=json.dumps({
        "verdicts": [{"claim_id": entree[0], "pertinente": entree[1],
                       "raison": entree[8] if len(entree) > 8 else None}
                      for entree in entrees],
        "facettes": [{"facette": rang, "claim_ids": ids} for rang, ids in enumerate(
            facettes if facettes is not None else [[c for c, p, *_ in entrees if p]])],
        "segments": [{"segment": i, "soutenu": ok}
                      for i, ok in sorted(({i: True for i in range(nb_segments)}
                                           | (segments or {})).items())],
        "applicabilite": applicabilite}))


# Sentinelle : « n'envoie pas `variant` du tout » — le seul moyen d'éprouver le **défaut** du
# pipeline (story 4.2d). Le défaut du helper reste `deterministe` : tous les tests écrits avant
# cette story demandent donc explicitement la baseline, et continuent de mesurer ce qu'ils mesuraient.
SANS_VARIANTE = object()


async def _run(index: Index, script: list, *, settings: Settings | None = None,
               budget: RequestBudget | None = None, faits=FAITS, doc_id: str | None = None,
               variant: object = "deterministe", dossier: MissingPackage | None = None,
               lang: str | None = None, question: str = QUESTION,
               dictionnaire: Dictionnaire | None = None):
    settings = settings or _settings()
    fake = FakeAnthropic(script)
    client = LlmClient(settings, anthropic_client=fake)
    variante = {} if variant is SANS_VARIANTE else {"variant": variant}
    answer, trace = await sinistre.run(doc_id, question, faits, corpus=index.corpus, index=index,
                                       client=client, settings=settings, request_id="req-sinistre",
                                       budget=budget or _budget(), dossier=dossier,
                                       lang=lang, dictionnaire=dictionnaire, **variante)
    return answer, trace, fake


def _questions_attendues(verdict) -> bool:
    """AD-6 : « toujours avec le paquet manquant **et les questions à poser** ».

    Les quatre pièces annoncées manquantes doivent chacune avoir sa question — sans quoi le
    gestionnaire lit quatre absences et se voit demander deux choses (revue 1.8).
    """
    questions = " ".join(verdict.ask_client).lower()
    return ("options" in questions and "conditions particulières" in questions
            and "avenant" in questions and "date" in questions)


GAR = ("c1", "Les dégâts au mobilier par action subite de la chaleur sont couverts.",
       [(f"{DOC_ID}:p1:2", Q_GARANTIE)])
DEF = ("c2", "Le contenu comprend le mobilier.", [(f"{DOC_ID}:p1:4", Q_DEFINITION)])
EXC_EXT = ("c3", "Les extensions excluent les dégâts au bâtiment.", [(f"{DOC_ID}:p2:1", Q_EXCLUSION)])
EXC_SOCLE = ("c4", "Le dommage de brûlure est exclu.", [(f"{DOC_ID}:p1:5", Q_EXCLUSION_SOCLE)])
COND = ("c5", "Le bien doit être occupé de manière permanente.", [(f"{DOC_ID}:p1:3", Q_CONDITION)])
MAUVAISE = ("c9", "Le sinistre est couvert à 100 %.", [(f"{DOC_ID}:p1:2", "couvert à cent pour cent")])


@pytest.mark.parametrize("lang", ["es", "eng", "XX"])
async def test_une_langue_forcee_non_servie_est_refusee_avant_tout_appel(index: Index,
                                                                         lang: str) -> None:
    with pytest.raises(InvalidRequest, match="français.*anglais.*allemand.*portugais"):
        await _run(index, [], lang=lang)


# --- nominal : le cas bougie -------------------------------------------------
async def test_the_candle_case_runs_the_five_steps_and_carries_its_verdict(
        index: Index, gate_du_mini_contrat) -> None:
    """AC : cinq étapes, `pipeline="sinistre"`, un seul appel `micro` dans *vérifier*, verdict complet."""
    answer, trace, fake = await _run(index, [
        _comprendre(),
        _rediger(GAR, DEF, EXC_EXT),
        _verifier(("c1", True, False, False, False, "caractère subit de l'action de la chaleur"),
                  ("c2", True, False, False, False, None),
                  ("c3", True, False, False, False, None))])
    assert fake.remaining_script == 0 and len(fake.requests) == 3  # micro, reason, micro
    assert fake.requests[1]["max_tokens"] == _settings().rediger_max_tokens == 2048
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier", "restituer"]
    assert trace.pipeline == "sinistre" and trace.variant == "deterministe"
    assert len(next(s for s in trace.steps if s.name == "verifier").calls) == 1
    # Story 2.5 : la même trace que le guide sait nommer les clauses du contrat et son gate ; le
    # pipeline sinistre n'a pas de dictionnaire et ne prétend donc pas en avoir consulté un.
    assert trace.gate is not None and trace.gate.profile == "vertical"
    assert trace.gate.cases == 1 and trace.gate.countersigned is False
    assert trace.dictionnaire is None
    par_id = {bloc.block_id: bloc for bloc in trace.blocs}
    assert par_id[f"{DOC_ID}:p1:2"].node_id == f"{DOC_ID}:socle"
    assert par_id[f"{DOC_ID}:p1:2"].fiche_id is None
    assert par_id[f"{DOC_ID}:p1:2"].titre == "Socle commun"

    assert answer.found is True
    verdict = answer.verdict
    assert verdict is not None
    # Le modèle est **intégralement scripté** : la valeur est déterministe, elle se fixe. Exclusion
    # p2:1 écartée (`non` : elle vise les extensions), garantie incertaine (`humain` par son fait
    # manquant), aucune garantie `oui` ni exclusion `oui` ⇒ règle (4) de la table.
    assert verdict.value == "ne_tranche_pas"
    statuts = {c.claim_id: c.status.applicable for c in answer.claims}
    assert statuts == {"c1": "humain", "c2": None, "c3": "non"}
    raisons = {c.claim_id: c.status.applicable_reason for c in answer.claims}
    assert raisons == {"c1": None, "c2": None, "c3": "hors_portee"}
    rendered_claims = {claim["claim_id"]: claim for claim in answer.model_dump(mode="json")["claims"]}
    assert rendered_claims["c3"]["status"]["applicable_reason"] == "hors_portee"
    assert verdict.missing.faits == ["caractère subit de l'action de la chaleur"]
    # matrice I/O : `ask_client` cite les options / conditions particulières **et** la nature « subite »
    assert any("caractère subit" in q for q in verdict.ask_client)
    assert any("options" in q for q in verdict.ask_client)
    assert any("conditions particulières" in q for q in verdict.ask_client)
    assert verdict.reason and "conditions générales seules" in verdict.reason
    # AD-6 : le paquet manquant est toujours là, et le verdict n'est jamais une décision d'indemnisation
    assert verdict.missing.conditions_particulieres and verdict.missing.options_souscrites


def test_la_trace_riche_du_vrai_pipeline_traverse_la_route_http(
        index: Index, gate_du_mini_contrat) -> None:
    """Même seam que le guide : pipeline réel scripté → FastAPI → JSON, sans service externe."""
    from fastapi.testclient import TestClient

    from server.app.api.main import create_app

    # La route ne transporte pas `variant` : c'est le **défaut du pipeline** qui est servi, et
    # depuis la story 4.2d c'est la navigation par outils — d'où le tour d'outils dans le script.
    script = [
        _comprendre(), _outils(termes=["mobilier", "chaleur", "contenu"],
                               node_id=f"{DOC_ID}:socle"),
        _rediger(GAR),
        _verifier(("c1", True, True, False, False, None)),
    ]
    fake = FakeAnthropic(script)

    async def pipeline_http(doc_id: str, question: str, faits: Faits, **kw):
        settings = kw["settings"]
        kw["client"] = LlmClient(settings, anthropic_client=fake)
        return await sinistre.run(doc_id, question, faits, **kw)

    app = create_app(_settings(env="dev", allow_ungated=True))
    with TestClient(app) as client:
        etat = app.state.foyer
        etat.corpus, etat.index = index.corpus, index
        etat.pipeline_sinistre = pipeline_http
        reponse = client.post("/api/v1/sinistre", json={
            "doc_id": DOC_ID, "question": QUESTION, "faits": FAITS.model_dump(),
        })

    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert fake.remaining_script == 0
    assert corps["trace"]["pipeline"] == "sinistre"
    assert corps["trace"]["gate"] == {
        "profile": "vertical", "cases": 1, "countersigned": False, "alerts": []}
    resolus = {bloc["block_id"]: bloc for bloc in corps["trace"]["blocs"]}
    assert resolus[f"{DOC_ID}:p1:2"] == {
        "block_id": f"{DOC_ID}:p1:2", "doc_id": DOC_ID, "node_id": f"{DOC_ID}:socle",
        "fiche_id": None, "titre": "Socle commun"}
    assert corps["sources"][0]["block_id"] == f"{DOC_ID}:p1:2"


def test_la_chronologie_structuree_traverse_le_pipeline_et_la_route_http(
        index: Index, gate_du_mini_contrat) -> None:
    """A9 : retirer le forwarding ou la projection fait disparaître le premier segment servi."""
    from fastapi.testclient import TestClient

    from server.app.api.main import create_app

    script = [
        _comprendre(cause="fuite progressive depuis des mois",
                    evenement="effondrement soudain du plafond", moment="hier"),
        _outils(termes=["mobilier", "chaleur", "contenu"], node_id=f"{DOC_ID}:socle"),
        _rediger(GAR),
        _verifier(("c1", True, True, False, False, None)),
    ]
    fake = FakeAnthropic(script)

    async def pipeline_http(doc_id: str, question: str, faits: Faits, **kw):
        settings = kw["settings"]
        kw["client"] = LlmClient(settings, anthropic_client=fake)
        return await sinistre.run(doc_id, question, faits, **kw)

    app = create_app(_settings(env="dev", allow_ungated=True))
    description_brute = ("DESCRIPTION BRUTE À NE PAS RÉINJECTER. " * 20).strip()
    with TestClient(app) as client:
        etat = app.state.foyer
        etat.corpus, etat.index = index.corpus, index
        etat.pipeline_sinistre = pipeline_http
        reponse = client.post("/api/v1/sinistre", json={
            "doc_id": DOC_ID, "question": QUESTION,
            "faits": {"description": description_brute},
        })

    assert reponse.status_code == 200, reponse.text
    answer = reponse.json()["answer"]
    repere = answer["segments"][0]
    assert repere == {
        "text": ("Faits compris — cause : fuite progressive depuis des mois ; puis événement : "
                 "effondrement soudain du plafond ; moment : hier."),
        "kind": "transition", "claim_ids": [],
    }
    assert answer["texte"].index("fuite progressive") < answer["texte"].index("effondrement soudain")
    assert description_brute not in answer["texte"]


async def test_un_autre_contrat_ne_recoit_pas_les_aliases_lexicaux_axa(
        index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    terme = "dommages causés par un animal"
    recus: list[list[str]] = []

    def retrouver_capture(parsed, **_kw):
        recus.append(list(parsed.terms))
        return RetrievalResult(), StepTrace(name="retrouver", tier="reason")

    monkeypatch.setattr(sinistre, "retrouver_deterministe", retrouver_capture)
    answer, trace, fake = await _run(index, [_comprendre(terms=[terme])])

    assert fake.remaining_script == 0
    assert recus == [[terme]]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "zero_hit" and answer.reason.terms_searched == [terme]
    assert [step.name for step in trace.steps] == ["comprendre", "retrouver", "restituer"]


async def test_une_variante_du_contrat_change_la_recherche_sans_fuite_inter_contrat(
        index: Index) -> None:
    alias = "sofa visiteur"
    canonique = "mobilier"
    dictionnaire = Dictionnaire(
        charge=True, doc_id=DOC_ID, validated=True, corpus_ok=True, canoniques=1,
        _groupes={forme(alias): (forme(alias), forme(canonique)),
                  forme(canonique): (forme(alias), forme(canonique))},
        _canoniques={forme(alias): (canonique,), forme(canonique): (canonique,)},
    )
    seconde_mauvaise = (
        "c9", "Autre tentative fausse.",
        [(f"{DOC_ID}:p1:2", "couvert à quatre-vingt pour cent")],
    )

    answer, trace, fake = await _run(index, [
        _comprendre(terms=[alias]), _rediger(MAUVAISE), _rediger(seconde_mauvaise),
    ], dictionnaire=dictionnaire)
    retrouver = next(step for step in trace.steps if step.name == "retrouver")
    assert fake.remaining_script == 0
    assert retrouver.opened_block_ids, "la variante devait ouvrir les blocs portant le canonique"
    assert f"{DOC_ID}:p1:2" in retrouver.opened_block_ids
    assert answer.reason is not None and answer.reason.kind == "claims_rejetes"
    assert answer.reason.terms_searched == [canonique]
    assert answer.reason.variants_count == 1
    assert trace.dictionnaire is not None
    assert trace.dictionnaire.model_dump() == {
        "charge": True, "validated": True, "corpus_ok": True, "court_circuit_actif": False}
    controle = next(c for c in retrouver.checks if c.name == "dictionnaire")
    assert controle.detail == "1 variante(s) ajoutée(s) à 1 terme(s)"

    dictionnaire_autre = Dictionnaire(
        charge=True, doc_id="autre-contrat", validated=True, corpus_ok=True, canoniques=1,
        _groupes=dictionnaire._groupes, _canoniques=dictionnaire._canoniques,
    )
    sans_fuite, trace_sans_fuite, fake_sans_fuite = await _run(
        index, [_comprendre(terms=[alias])], dictionnaire=dictionnaire_autre)
    retrouver_sans_fuite = next(step for step in trace_sans_fuite.steps if step.name == "retrouver")
    assert fake_sans_fuite.remaining_script == 0
    assert retrouver_sans_fuite.opened_block_ids == []
    assert all(c.name != "dictionnaire" for c in retrouver_sans_fuite.checks)
    assert sans_fuite.reason is not None and sans_fuite.reason.kind == "zero_hit"
    assert sans_fuite.reason.terms_searched == [alias]
    assert sans_fuite.reason.variants_count == 0


async def test_every_displayed_claim_carries_a_typed_applicability(index: Index) -> None:
    """AC : chaque claim affichée porte `oui|non|humain`, ou `None` si elle ne cite aucune clause."""
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, DEF),
        _verifier(("c1", True, True, False, False, None), ("c2", True, False, False, False, None))])
    for claim in answer.claims:
        assert claim.status.applicable in ("oui", "non", "humain", None)
        assert claim.status.retrouvee is True and claim.status.pertinente is True


# --- les autres lignes de la table -------------------------------------------
async def test_an_applicable_exclusion_over_the_case_is_not_covered(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, EXC_SOCLE),
        _verifier(("c1", True, True, False, False, None), ("c4", True, True, False, False, None))])
    assert answer.verdict is not None and answer.verdict.value == "non_couvert"


async def test_a_baseline_guarantee_alone_is_covered(index: Index) -> None:
    """La fixture que l'AC exige nommément — « `couvert` (garantie socle) » —, **jouée par le pipeline**.

    Revue Codex 1.8 (B1, tour 2). Le tour 1 lisait la seconde branche de la règle (2) d'AD-6 sur
    `MissingPackage` — le dossier **global** —, si bien que `couvert` réclamait un argument `dossier`
    que l'AC ne mentionne nulle part. L'AC dit : « (2) garantie `oui` et (condition/franchise/exclusion
    `humain` ou **garantie hors socle / dépendant d'une option**) », puis « (3) garantie du socle `oui`
    sans condition ouverte ⇒ `couvert` ». La dépendance se lit donc sur la clause, et la fixture est
    jouée telle que l'AC l'écrit : la garantie du socle seule, sans rien de plus.

    Ce qui empêche un `couvert` de complaisance n'est plus une politique globale mais le contrôle des
    qualités (B3) : la clause exige « un événement soudain » et « l'action subite de la chaleur », le
    modèle les énumère et cite pour chacune un fragment des faits **relu mot pour mot** par le code.
    Le test suivant retire cette corroboration et le verdict retombe.
    """
    soudain, subite = "caractère soudain de l'événement", "action subite de la chaleur"
    answer, trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [soudain, subite],
                   [(soudain, FRAGMENT_SOUDAIN), (subite, FRAGMENT)]))])
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "couvert"
    assert "socle commun" in verdict.reason and "conditions générales seules" in verdict.reason
    assert verdict.escalate == [] and verdict.missing.faits == []
    assert [c.status.applicable for c in answer.claims] == ["oui"]
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "restituer"]
    # AD-6 : le paquet manquant accompagne **aussi** un `couvert` — le verdict ne vaut qu'au regard
    # des conditions générales, et il le dit en réclamant les quatre pièces qu'il n'a pas lues.
    assert verdict.missing.conditions_particulieres and verdict.missing.options_souscrites
    assert _questions_attendues(verdict)
    # les deux qualités exigées restent à faire confirmer par le client (B3)
    assert sum("confirmer" in q for q in verdict.ask_client) == 2

    # et la table dit la même chose hors pipeline, sur la clause relue dans le corpus
    (claim,) = answer.claims
    (quote,) = claim.quotes
    document = index.corpus.documents[DOC_ID]
    bloc = document.block(quote.block_id)
    noeud = document.node_of(bloc.block_id)
    jugee = ClaimJugee(
        claim_id=claim.claim_id, champs=ChampsApplicabilite(fait_requis_present=True),
        clauses=[ClauseCitee(block_id=bloc.block_id, kind=bloc.kind, kind_confirmed=bloc.kind_confirmed,
                             portee=document.scope_nodes(bloc.block_id), node_id=noeud,
                             socle=document.node_scope_kind(noeud) == "commun")])
    assert decider([jugee], ask_client_max=_settings().ask_client_max).value == "couvert"


async def test_the_caller_who_holds_the_file_is_not_asked_for_it_again(index: Index) -> None:
    """`run(..., dossier=…)` ne change pas la valeur, il change ce qu'on réclame (B1, tour 2).

    Le paquet contractuel que l'appelant détient reste une **entrée** du pipeline — jamais une
    déduction — et il alimente `MissingPackage` : ce qui est au dossier n'est plus annoncé manquant
    ni redemandé. La table, elle, a déjà tranché sans lui.
    """
    soudain, subite = "caractère soudain de l'événement", "action subite de la chaleur"
    complet = MissingPackage(conditions_particulieres=False, options_souscrites=False,
                             avenants=False, date_effet=False)
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [soudain, subite],
                   [(soudain, FRAGMENT_SOUDAIN), (subite, FRAGMENT)]))],
        dossier=complet)
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "couvert"
    assert verdict.missing.conditions_particulieres is False
    assert all("options" not in q for q in verdict.ask_client)


async def test_a_guarantee_whose_qualities_are_not_enumerated_is_never_covered(index: Index) -> None:
    """Revue Codex 1.8 (B3, tour 2) : le silence du modèle ne vaut pas « aucune qualité exigée ».

    Même chaîne, mêmes clauses, même `fait_requis_present=true` que la fixture `couvert` — mais le
    modèle **n'énumère pas** les qualités. C'est le trou par lequel un `couvert` passait : le défaut
    vide des deux listes rendait le contrôle sans prise sur une clause qui exige pourtant « un
    événement soudain ». Le jeu de champs est désormais inexploitable et la claim vaut `humain`.
    """
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None), enumere=False)])
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "ne_tranche_pas"
    assert [c.status.applicable for c in answer.claims] == ["humain"]


async def test_a_guarantee_whose_qualities_are_declared_empty_is_never_covered(index: Index) -> None:
    """Revue Codex 1.8 (B3), tour 3 : la liste vide **écrite** n'est pas une preuve non plus.

    Même chaîne et même `fait_requis_present=true` que la fixture `couvert` de l'AC, mais le modèle
    rend `"qualites_exigees": []` sur une garantie dont le texte écrit « par un événement soudain,
    résultant de l'action subite de la chaleur ». Le tour 2 l'acceptait — « une clause qui n'exige
    réellement aucune qualité se dit `[]` » —, c'est-à-dire encore sur la parole du modèle. Le texte
    de la clause tranche désormais : les deux qualités qu'il écrit deviennent des qualités non
    établies, la garantie vaut `humain` et le verdict ne peut plus être `couvert`.
    """
    answer, trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [], []))])
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "ne_tranche_pas"
    assert [c.status.applicable for c in answer.claims] == ["humain"]
    assert verdict.missing.faits == ["caractère « soudain » exigé par la clause citée",
                                     "caractère « subite » exigé par la clause citée"]
    controles = [c for etape in trace.steps for c in etape.checks
                 if c.name == "qualite_de_la_clause_non_enumeree"]
    assert len(controles) == 2
    assert all(c.detail.startswith("1 qualité exigée") for c in controles)
    # La trace est l'objet que les deux interfaces rendent : ni le texte de la clause, ni les
    # libellés déduits de ses qualificatifs ne doivent y fuir.
    trace_publiee = json.dumps(trace.model_dump(), ensure_ascii=False)
    for secret in (GARANTIE, "soudain", "subite"):
        assert secret not in trace_publiee


async def test_facts_that_deny_the_quality_never_yield_a_covered_verdict(index: Index) -> None:
    """Revue Codex 1.8 (B3), tour 3 : un fait qui **contredit** la qualité ne l'établit pas.

    Contre-exemple de la revue : « action subite de la chaleur » tenue pour établie par « La chaleur a
    agi lentement », parce que le seul mot *chaleur* recoupait la qualité. Joué de bout en bout, avec
    des faits déclarés qui disent le contraire de ce que la clause exige.
    """
    faits = Faits(date="2026-08-01", lieu="salon du domicile", montant_eur=1200.0,
                  description="Une bougie posée sur une table a brûlé le mobilier de salon. "
                              "La chaleur a agi lentement, sur plusieurs heures.")
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [SOUDAIN, SUBITE],
                   [(SOUDAIN, "La chaleur a agi lentement"),
                    (SUBITE, "La chaleur a agi lentement")]))], faits=faits)
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "ne_tranche_pas"
    assert [c.status.applicable for c in answer.claims] == ["humain"]
    assert verdict.missing.faits == [SOUDAIN, SUBITE]


async def test_a_quality_corroborated_by_nothing_in_the_file_is_never_covered(index: Index) -> None:
    """Revue Codex 1.8 (B3, tour 2) : une auto-déclaration ne vaut pas corroboration par les faits.

    Le modèle recopie les qualités exigées dans les qualités établies — ce que le run réel du 24/08 a
    fait — mais le fragment qu'il cite pour chacune est repris de la **clause**, pas des faits
    déclarés. Le code le relit dans les faits soumis (AD-3 appliqué aux faits), ne l'y trouve pas, et
    les qualités retombent en « non établies ».
    """
    soudain = "caractère soudain de l'événement"
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [soudain],
                   [(soudain, "un événement soudain, résultant de l'action subite de la chaleur")]))])
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "ne_tranche_pas"
    assert [c.status.applicable for c in answer.claims] == ["humain"]
    # Tour 3 : le modèle n'avait énuméré **qu'**une des deux qualités que la clause écrit — le code
    # relit son texte et ajoute la seconde, qui part elle aussi en question au client.
    assert verdict.missing.faits == [soudain, "caractère « subite » exigé par la clause citée"]


async def test_a_refusal_keeps_the_file_the_caller_already_has(index: Index) -> None:
    """Le `dossier` accompagne aussi un refus : on ne réclame pas au gestionnaire ce qu'il a déjà."""
    complet = MissingPackage(conditions_particulieres=False, options_souscrites=False,
                             avenants=False, date_effet=False)
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(MAUVAISE), _rediger(("c9", "Autre tentative, aussi fausse.",
                                                     [(f"{DOC_ID}:p1:2", "couvert à quatre-vingt pour cent")]))],
        dossier=complet)
    assert answer.found is False and answer.verdict is not None
    assert answer.verdict.value == "ne_tranche_pas"
    assert answer.verdict.missing.conditions_particulieres is False
    assert answer.verdict.ask_client == []


async def test_the_same_guarantee_with_an_unestablished_quality_is_not_covered(index: Index) -> None:
    """Revue Codex 1.8 (B3), le pendant du test précédent : ce qui sépare `couvert` de `ne_tranche_pas`.

    La garantie de la fixture exige « l'action **subite** de la chaleur ». Le modèle la nomme et ne la
    retrouve pas dans les faits déclarés — puis coche quand même `fait_requis_present`, exactement
    comme le run réel qui a motivé le finding. Le code fait la différence des deux listes, la claim
    vaut `humain`, et la qualité manquante part en question au client.
    """
    subite = "caractère subit de l'action de la chaleur"
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [subite], []))])
    verdict = answer.verdict
    assert [c.status.applicable for c in answer.claims] == ["humain"]
    assert verdict is not None and verdict.value == "ne_tranche_pas"
    # Tour 3 : « soudain », que la clause écrit et que le modèle n'a pas nommé, s'ajoute par le code.
    assert verdict.missing.faits == [subite, "caractère « soudain » exigé par la clause citée"]
    assert any(subite in q for q in verdict.ask_client)


async def test_a_surviving_bounded_dependency_never_masks_a_required_quality(
        index: Index) -> None:
    """Revue 3.3 post-suite : une définition survivante ne vaut pas une claim fondatrice.

    Avec deux places pour une facette et sa dépendance, la première rédaction emploie bien le
    budget mais surinterprète la garantie. *Vérifier* la rejette et conserve la définition : cette
    claim auxiliaire ne doit pas masquer la relance qui rendra la clause fondatrice vérifiable et
    fera demander explicitement la qualité exigée mais non établie.
    """
    appliquee = (
        "c1", "Cette garantie s'applique au sinistre décrit.",
        [(f"{DOC_ID}:p1:2", Q_GARANTIE)],
    )
    neutre = (
        "c1", "La clause vise les dégâts au mobilier causés par une action subite de la chaleur.",
        [(f"{DOC_ID}:p1:2", Q_GARANTIE)],
    )
    settings = _settings(draft_max_claims=2, verifier_max_claims=2)

    answer, trace, fake = await _run(index, [
        _comprendre(),
        _rediger(appliquee, DEF),
        _verifier(("c1", False, False, False, False, SUBITE, [], [], "conclusion_ajoutee"),
                  ("c2", True, False, False, False, None)),
        # Même si le modèle omet la définition acquise, le pipeline la reconduit depuis la première
        # ébauche avant de revérifier l'ensemble : la relance peut dominer sans prime de kind.
        _rediger(neutre),
        _verifier(("c1", True, True, False, False, None, [SUBITE], []),
                  ("c2", True, False, False, False, None)),
    ], settings=settings)

    assert fake.remaining_script == 0
    redactions = [request for request in fake.requests if request["model"] == TIERS["reason"]]
    assert len(redactions) == 2
    premiere_consigne = redactions[0]["messages"][-1]["content"]
    assert "Plan de sortie concis : 1 facette(s)" in premiere_consigne
    assert f"Définitions applicables à rendre vérifiables : {DOC_ID}:p1:4" in premiere_consigne
    consigne_relance = redactions[1]["messages"][-1]["content"]
    assert "règle conditionnelle que le passage énonce" in consigne_relance
    assert f"Acquis à reconduire pendant la relance : {DOC_ID}:p1:4" in consigne_relance
    assert [step.name for step in trace.steps].count("rediger") == 2
    assert [claim.claim_id for claim in answer.claims] == ["c2", "c1"]
    assert answer.verdict is not None
    assert any(SUBITE in question for question in answer.verdict.ask_client)


async def test_une_fondatrice_non_confirmee_ne_remplace_jamais_plusieurs_acquis(
        index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    """La relance ne perd ni claims, ni facettes, ni blocs pour la seule apparition d'un kind."""
    monkeypatch.setattr(index.corpus.documents[DOC_ID].block(f"{DOC_ID}:p2:1"),
                        "kind_source", None)
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=["définition du bien", "condition d'occupation"]),
        _rediger(GAR, DEF, COND),
        _verifier(
            ("c1", False, False, False, False, None, [], [], "conclusion_ajoutee"),
            ("c2", True, False, False, False, None),
            ("c5", True, False, False, False, "occupation permanente du bien"),
            facettes=[["c2"], ["c5"]],
        ),
        _rediger(EXC_EXT),
        _verifier(("c3", True, True, False, False, None), facettes=[["c3"], []]),
    ])

    assert fake.remaining_script == 0
    assert [claim.claim_id for claim in answer.claims] == ["c2", "c5"]
    assert {quote.block_id for claim in answer.claims for quote in claim.quotes} == {
        f"{DOC_ID}:p1:4", f"{DOC_ID}:p1:3"}
    assert any(check.name == "relance_moins_bonne" and not check.ok
               for step in trace.steps for check in step.checks)
    assert not hasattr(sinistre, "_fondatrice_retenue")


async def test_an_open_condition_keeps_the_verdict_conditional(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, COND),
        _verifier(("c1", True, True, False, False, None),
                  ("c5", True, False, False, False, "occupation permanente du bien"))])
    assert answer.verdict is not None and answer.verdict.value == "sous_conditions"


async def test_a_condition_not_met_never_yields_a_covered_verdict(index: Index) -> None:
    """Revue 1.8 (P1) : une condition citée dont le fait exigé n'est **pas** établi ouvre le verdict.

    Le modèle rend ici `fait_requis_present=false` sans nommer de fait manquant — la lecture naturelle
    de « le bien n'est pas occupé de manière permanente ». Traiter cela comme `non` sortirait la
    condition de la règle (2) et rendrait `couvert` sur un dossier dont une condition est en défaut.
    """
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, COND),
        _verifier(("c1", True, True, False, False, None), ("c5", True, False, False, False, None))])
    assert [c.status.applicable for c in answer.claims] == ["oui", "humain"]
    assert answer.verdict is not None and answer.verdict.value == "sous_conditions"


async def test_a_guarantee_depending_on_an_option_is_conditional(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR), _verifier(("c1", True, True, True, False, None))])
    assert answer.verdict is not None and answer.verdict.value == "sous_conditions"
    assert any("options" in q for q in answer.verdict.ask_client)


# --- claims rejetées, relance, refus ------------------------------------------
async def test_a_rejected_claim_triggers_one_retry_then_refuses_with_a_verdict(index: Index) -> None:
    """AD-3 puis AD-16 : relance unique, puis refus motivé — **avec** `ne_tranche_pas`, jamais rien."""
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(MAUVAISE), _rediger(("c9", "Autre tentative, aussi fausse.",
                                                     [(f"{DOC_ID}:p1:2", "couvert à quatre-vingt pour cent")]))])
    assert fake.remaining_script == 0 and len(fake.requests) == 3  # comprendre, rédiger, rédiger
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "rediger", "verifier", "restituer"]
    assert answer.found is False
    assert answer.reason is not None and answer.reason.kind == "claims_rejetes"
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert answer.rejected_claims and all(c.rejection_kind == "non_retrouvee"
                                          for c in answer.rejected_claims)
    # le dossier qui a le plus besoin d'être complété est aussi celui qu'on questionne (revue 1.8)
    assert _questions_attendues(answer.verdict)
    # et la phrase servie est celle du **sinistre**, pas celle du guide
    assert "clause" in answer.texte and "guide" not in answer.texte
    # D4, sur la **troisième** branche de *restituer* — celle où toutes les claims sont rejetées
    # après la relance. Les deux autres (chemin nominal, `refuser()`) étaient couvertes ; retirer
    # `faits_compris=compris` de ce seul appel laissait la suite verte, et la section « Ce que j'ai
    # compris du sinistre » disparaissait précisément de l'écran le plus démuni : un
    # « ne tranche pas » sans clause, où il ne reste que ce que le système a cru comprendre.
    assert answer.faits_compris is not None
    assert answer.faits_compris.bien == "mobilier de salon"
    assert answer.faits_compris.cause == "bougie"


async def test_a_truncated_read_with_no_surviving_clause_never_proves_an_absence(index: Index) -> None:
    """La même garde que le guide, sur le pipeline qui en a le plus besoin (revue Codex 2.3, B3).

    « Aucune clause du contrat n'a été retrouvée » lu au terme d'une lecture **bornée** est une
    affirmation d'assureur que rien n'appuie : NFR2 et AD-1 l'interdisent (« budget épuisé ou
    troncature non résolue ⇒ jamais d'`AbsenceProof` »). Cette interdiction-là ne bouge pas.

    **Story 4.2f : ce n'est plus un 503 pour autant.** Le gestionnaire recevait « L'analyse est
    indisponible pour le moment » alors que rien n'était en panne — la lecture avait eu lieu, elle
    était insuffisante. La réponse est désormais un 200 typé : `found=false`, aucune preuve
    d'absence, une `LecturePartielle` chiffrée, la lacune `lecture_bornee`, les affirmations
    écartées, et le `ne_tranche_pas` calculé par la règle (0bis) d'AD-6 — jamais un verdict de
    remplacement.
    """
    settings = _settings(retrieval_max_blocks=1)
    answer, trace, _fake = await _run(
        index, [_comprendre(), _rediger(MAUVAISE),
                _rediger(("c9", "Autre tentative, aussi fausse.",
                          [(f"{DOC_ID}:p1:2", "couvert à quatre-vingt pour cent")]))],
        settings=settings)
    assert trace.truncations == 1
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "rediger", "verifier", "restituer"]
    assert answer.found is False and answer.complete is False and answer.reason is None
    assert answer.lecture_partielle is not None
    assert answer.lecture_partielle.blocks_read == 1 and answer.lecture_partielle.nodes_read == 1
    assert answer.lecture_partielle.documents == [DOC_ID]
    assert PHRASES_DE_LACUNE["fr"]["lecture_bornee"] in answer.unknown
    # AD-6/AD-16 : jamais un sinistre sans verdict, et jamais un verdict de remplacement.
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert answer.faits_compris is not None and answer.faits_compris.bien == "mobilier de salon"
    assert answer.rejected_claims
    # La phrase servie est celle du **sinistre**, jamais celle du guide.
    assert "contrat" in answer.texte and "guide" not in answer.texte


async def test_une_contradiction_sur_un_segment_identique_ne_vide_plus_une_lecture_tronquee(
        index: Index) -> None:
    """Story 4.2a-bis, la surface de l'incident : sous lecture tronquée, un `soutenu=false` scripté
    sur un segment byte-identique à sa claim retenue ne vide plus la réponse — le segment est dérivé
    de la pertinence, affiché, `found=true`, et ni 503 ni refus `claims_rejetes`.
    Le vrai zéro-claim tronqué, lui, rend depuis la story 4.2f un 200 typé portant une
    `LecturePartielle` (test ci-dessus) — et non plus un `TruncatedRead`.

    `retrieval_max_blocks=4` : la lecture est bien tronquée (l'exclusion `p2:1` reste fermée,
    `trace.truncations == 1`) **et** la clause citée `p1:2` est fournie — c'est la configuration de
    l'incident : la seule anomalie est la contradiction, pas la citation."""
    settings = _settings(retrieval_max_blocks=4)
    texte = "Les dégâts au mobilier par action subite de la chaleur sont couverts."
    identique = fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": [{"text": texte, "kind": "factuel", "claim_ids": ["c1"]}],
        "claims": [{"claim_id": "c1", "text": texte,
                    "quotes": [{"block_id": f"{DOC_ID}:p1:2", "quote": Q_GARANTIE}]}]}))
    answer, trace, fake = await _run(index, [
        _comprendre(), identique,
        _verifier(("c1", True, True, False, False, None), segments={0: False})],
        settings=settings)

    assert fake.remaining_script == 0
    assert answer.found is True and answer.reason is None
    assert [c.claim_id for c in answer.claims] == ["c1"]
    assert answer.rejected_claims == []
    assert texte in answer.texte
    assert answer.verdict is not None and answer.verdict.value == "couvert"
    # la lecture reste bornée : la réponse est servie, mais jamais donnée pour complète
    assert answer.complete is False and trace.truncations >= 1
    step_verifier = next(s for s in trace.steps if s.name == "verifier")
    assert any(c.name == "segments_derives" for c in step_verifier.checks)


async def test_the_rejected_claims_branch_bounds_the_understood_facts_too(index: Index) -> None:
    """Et elle les **borne** : la troisième branche n'échappe pas à la règle de D8 (revue 1.9)."""
    settings = _settings()
    trop_long = "x" * (settings.fait_manquant_max_chars + 1)
    answer, trace, _fake = await _run(index, [
        _comprendre(cause=trop_long),
        _rediger(MAUVAISE),
        _rediger(("c9", "Autre tentative, aussi fausse.",
                  [(f"{DOC_ID}:p1:2", "couvert à quatre-vingt pour cent")]))], settings=settings)
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "claims_rejetes"
    assert answer.faits_compris is not None and answer.faits_compris.cause is None
    assert "xxxx" not in answer.model_dump_json()
    restituer_step = next(s for s in trace.steps if s.name == "restituer")
    assert any(c.name == "faits_compris_hors_borne" for c in restituer_step.checks)


async def test_a_claim_mixing_two_clauses_is_sent_back_to_the_writer(index: Index) -> None:
    """D6 : la claim ambiguë déclenche la relance, et la seconde ébauche éclatée passe."""
    melangee = ("c1", "La garantie joue sauf pour les extensions.",
                [(f"{DOC_ID}:p1:2", Q_GARANTIE), (f"{DOC_ID}:p2:1", Q_EXCLUSION)])
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(melangee), _rediger(GAR, EXC_EXT),
        _verifier(("c1", True, True, False, False, None), ("c3", True, False, False, False, None))])
    assert fake.remaining_script == 0
    # la première vérification n'a fait **aucun** appel : rien n'avait survécu au contrôle de citation
    assert [s.name for s in trace.steps].count("verifier") == 2
    assert answer.found is True and answer.verdict is not None
    # l'exclusion des extensions ne couvre pas le cas : la garantie du socle reprend la main
    assert answer.verdict.value == "couvert"
    assert "socle commun" in answer.verdict.reason
    # La claim mêlée a bien été rejetée par la **première** vérification — c'est elle qui a nourri la
    # relance. Elle ne figure plus dans la réponse servie parce que la seconde vérification domine et
    # la remplace en entier (AD-3) ; c'est la trace qui garde la preuve du rejet.
    premiere = next(s for s in trace.steps if s.name == "verifier")
    citations = next(c for c in premiere.checks if c.name == "citations")
    assert citations.ok is False and "1 rejetée(s) sur 1" in citations.detail
    assert answer.rejected_claims == []  # la seconde ébauche, éclatée, ne rejette rien


async def test_a_retry_finding_an_untyped_passage_replaces_an_empty_first_draft(index: Index) -> None:
    """Campagne B 2.7 : le paquet manquant ne doit pas faire préférer zéro preuve à une preuve.

    Une claim non typée ne fonde aucun verdict AD-6 et porte donc le paquet contractuel manquant ;
    cela ne la rend pas moins bonne que la première ébauche, dont rien n'avait survécu.
    """
    answer, _trace, fake = await _run(index, [
        _comprendre(), _rediger(MAUVAISE), _rediger(DEF),
        _verifier(("c2", True, False, False, False, None))])

    assert fake.remaining_script == 0
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c2"]
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert "passages ont été retrouvés et affichés" in answer.verdict.reason


async def test_une_definition_seule_declenche_la_relance_vers_la_fondatrice_retrouvee(
        index: Index) -> None:
    """4.2a, preuve finale A16 : sans rejet il n'y avait aucun motif, donc aucune relance, alors que
    le retrieval portait une clause décisionnelle confirmée jamais citée. Le code compose désormais
    le motif — identifiants relus du corpus typé, applicabilité laissée au calcul — et la relance
    unique tente de rendre la règle vérifiable.
    """
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(DEF),
        _verifier(("c2", True, False, False, False, None)),
        _rediger(GAR, DEF),
        _verifier(("c1", True, True, False, False, None),
                  ("c2", True, False, False, False, None))])

    assert fake.remaining_script == 0
    relance = fake.requests[3]["messages"][-1]["content"]
    assert "clause décisionnelle confirmée pourtant retrouvée" in relance
    assert f"{DOC_ID}:p1:2" in relance
    assert "sans décider de son applicabilité" in relance
    assert {c.claim_id for c in answer.claims} == {"c1", "c2"}
    assert any(q.block_id == f"{DOC_ID}:p1:2" for c in answer.claims for q in c.quotes)


async def test_une_fondatrice_retrouvee_est_adoptee_par_dominance_pleine(index: Index) -> None:
    """Réécrit après la revue Codex 4.2a (B3) : l'ancienne version de ce test épinglait le
    contournement de `domine` par `_fondatrice_survivante` — une adoption qui perdait l'acquis c2
    et son bloc. L'arbitrage corrigé passe par la fusion : les acquis sont reconduits dans
    l'ébauche relancée, la seconde vérification les retient avec la fondatrice, et l'adoption est
    une dominance réelle sur les six axes — jamais un raccourci par kind."""
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(DEF),
        _verifier(("c2", True, False, False, False, None)),
        _rediger(GAR),
        _verifier(("c1", True, True, False, False, None),
                  ("c2", True, False, False, False, None), facettes=[["c1", "c2"]])])

    assert fake.remaining_script == 0
    assert {c.claim_id for c in answer.claims} == {"c1", "c2"}
    assert any(q.block_id == f"{DOC_ID}:p1:2" for c in answer.claims for q in c.quotes)
    assert not any(check.name == "relance_moins_bonne"
                   for step in trace.steps for check in step.checks)


async def test_une_relance_qui_perd_lacquis_nest_pas_adoptee_meme_avec_fondatrice(
        index: Index) -> None:
    """Revue Codex 4.2a (B3), reproduction fermée : la seconde vérification cite une fondatrice
    confirmée mais rejette l'acquis c2 — elle perd son bloc (`p1:4`). Aucun kind ne contourne la
    dominance : l'acquise fait foi, l'écart est nommé."""
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(DEF),
        _verifier(("c2", True, False, False, False, None)),
        _rediger(GAR),
        _verifier(("c1", True, True, False, False, None),
                  ("c2", False, False, False, False, None))])

    assert fake.remaining_script == 0
    assert [c.claim_id for c in answer.claims] == ["c2"]
    assert any(check.name == "relance_moins_bonne"
               for step in trace.steps for check in step.checks)


async def test_une_relance_qui_perd_une_facette_nest_pas_adoptee(index: Index) -> None:
    """B3 : deux facettes couvertes par l'acquise ; la seconde n'en couvre qu'une — conservée."""
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=["définition du bien", "condition d'occupation"]),
        _rediger(DEF, COND),
        _verifier(("c2", True, False, False, False, None),
                  ("c5", True, False, False, False, "occupation permanente du bien"),
                  facettes=[["c2"], ["c5"]]),
        _rediger(GAR),
        _verifier(("c1", True, True, False, False, None),
                  ("c2", True, False, False, False, None),
                  ("c5", False, False, False, False, None),
                  facettes=[["c1", "c2"], []])])

    assert fake.remaining_script == 0
    assert {c.claim_id for c in answer.claims} == {"c2", "c5"}
    assert any(check.name == "relance_moins_bonne"
               for step in trace.steps for check in step.checks)


async def test_une_relance_qui_ajoute_des_manques_nest_pas_adoptee(index: Index) -> None:
    """B3 : la seconde conserve les acquis mais déclare trois réserves de plus — non dominante,
    l'acquise fait foi (la garde 2.7 ne joue que sur zéro claim acquise)."""
    limites = ["Le contrat ne précise pas la franchise applicable.",
               "Le plafond annuel d'indemnisation n'est pas indiqué dans les passages lus.",
               "La procédure de déclaration n'est pas décrite dans les passages lus."]
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(DEF),
        _verifier(("c2", True, False, False, False, None)),
        _rediger_avec_limites(GAR, limites=limites),
        _verifier(("c1", True, True, False, False, None),
                  ("c2", True, False, False, False, None), facettes=[["c1", "c2"]])])

    assert fake.remaining_script == 0
    assert [c.claim_id for c in answer.claims] == ["c2"]
    assert any(check.name == "relance_moins_bonne"
               for step in trace.steps for check in step.checks)


async def test_une_fondatrice_omise_sans_place_ne_lance_pas_de_relance_tronquante(
        index: Index) -> None:
    """Revue Codex 4.2a (B1), « borne entièrement occupée » : les affirmations retenues occupent
    déjà draft_max_claims — une relance fondatrice ne pourrait que tronquer. Elle n'est pas
    lancée : l'état est nommé, l'acquise est servie avec la lacune de relance abandonnée, jamais
    donnée pour complète. La longueur du script est l'assertion (aucun 4e appel)."""
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(DEF),
        _verifier(("c2", True, False, False, False, None))],
        settings=_settings(draft_max_claims=1))

    assert fake.remaining_script == 0 and len(fake.requests) == 3
    assert [c.claim_id for c in answer.claims] == ["c2"]
    assert any(check.name == "relance_fondatrice_sans_place"
               for step in trace.steps for check in step.checks)
    assert answer.complete is False


async def test_une_relance_saturee_nest_pas_lancee_quand_une_limite_acquise_tomberait(
        index: Index) -> None:
    """Recheck Codex (B2), cas saturé : acquis sous la borne de claims, mais acquis + limite
    acquise + une correction ne tiennent pas sous draft_max_segments. La relance n'est pas lancée —
    la dominance ne voit jamais un candidat amputé, la réserve reste dans `unknown` et `nb_manques`
    ne baisse pas. La longueur du script est l'assertion (aucun 4e appel)."""
    limite = "Le contrat ne précise pas la franchise applicable."
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=["définition du bien", "condition d'occupation"]),
        _rediger_avec_limites(DEF, COND, limites=[limite]),
        _verifier(("c2", True, False, False, False, None),
                  ("c5", True, False, False, False, "occupation permanente du bien"),
                  facettes=[["c2"], ["c5"]])],
        settings=_settings(draft_max_claims=3, draft_max_segments=3))

    assert fake.remaining_script == 0 and len(fake.requests) == 3
    assert {c.claim_id for c in answer.claims} == {"c2", "c5"}
    assert any("franchise" in u for u in answer.unknown)
    assert any(check.name == "relance_sans_place_pour_les_limites"
               for step in trace.steps for check in step.checks)
    assert answer.complete is False


async def test_la_reproduction_codex_deux_acquis_bornes_et_une_limite_reste_entiere(
        index: Index) -> None:
    """Recheck Codex (B2), reproduction du verdict : deux acquis sous une borne de deux claims et
    une limite acquise. Aucune relance ne tronque : la garde « borne occupée » arrête avant, la
    réserve reste comptée."""
    limite = "Le contrat ne précise pas la franchise applicable."
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=["définition du bien", "condition d'occupation"]),
        _rediger_avec_limites(DEF, COND, limites=[limite]),
        _verifier(("c2", True, False, False, False, None),
                  ("c5", True, False, False, False, "occupation permanente du bien"),
                  facettes=[["c2"], ["c5"]])],
        settings=_settings(draft_max_claims=2, draft_max_segments=4))

    assert fake.remaining_script == 0 and len(fake.requests) == 3
    assert {c.claim_id for c in answer.claims} == {"c2", "c5"}
    assert any("franchise" in u for u in answer.unknown)
    assert any(check.name == "relance_fondatrice_sans_place"
               for step in trace.steps for check in step.checks)


async def test_une_limite_rejetee_ne_bloque_pas_la_relance_fondatrice(index: Index) -> None:
    """Recheck Codex tour 2 (N1) : les limites acquises se dérivent de `Verification.unknown`.

    La limite initiale est rejetée par le contrôle (`soutenu=false`) : elle n'a **pas** survécu,
    ne compte pas dans le pré-contrôle et libère sa place — la relance fondatrice qui tient
    réellement sous `draft_max_segments=3` part et aboutit. La fusion ne la ressuscite pas.
    """
    limite = "Le contrat ne précise pas la franchise applicable."
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=["définition du bien", "condition d'occupation"]),
        _rediger_avec_limites(DEF, COND, limites=[limite]),
        _verifier(("c2", True, False, False, False, None),
                  ("c5", True, False, False, False, "occupation permanente du bien"),
                  facettes=[["c2"], ["c5"]], segments={2: False}),
        _rediger(GAR),
        _verifier(("c1", True, True, False, False, None),
                  ("c2", True, False, False, False, None),
                  ("c5", True, False, False, False, "occupation permanente du bien"),
                  facettes=[["c1", "c2"], ["c5"]])],
        settings=_settings(draft_max_claims=3, draft_max_segments=3))

    assert fake.remaining_script == 0 and len(fake.requests) == 5
    assert {c.claim_id for c in answer.claims} == {"c1", "c2", "c5"}
    assert not any(check.name == "relance_sans_place_pour_les_limites"
                   for step in trace.steps for check in step.checks)
    assert answer.unknown == []  # la limite rejetée n'est pas ressuscitée par la fusion


async def test_des_limites_dupliquees_sont_normalisees_avant_la_premiere_verification(
        index: Index) -> None:
    """Recheck Codex tour 2 (B2), cas saturé avec doublons : `nb_manques` est stable.

    Deux limites byte-identiques sont normalisées **une seule fois, à la sortie de *rédiger*** —
    avant la première `Verification`. Le pré-contrôle et la fusion comptent donc la même réserve
    que la dominance : rien ne baisse artificiellement, et le cas saturé reste refusé avec son
    état nommé.
    """
    limite = "Le contrat ne précise pas la franchise applicable."
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=["définition du bien", "condition d'occupation"]),
        _rediger_avec_limites(DEF, COND, limites=[limite, limite]),
        _verifier(("c2", True, False, False, False, None),
                  ("c5", True, False, False, False, "occupation permanente du bien"),
                  facettes=[["c2"], ["c5"]])],
        settings=_settings(draft_max_claims=3, draft_max_segments=3))

    assert fake.remaining_script == 0 and len(fake.requests) == 3
    assert {c.claim_id for c in answer.claims} == {"c2", "c5"}
    # Normalisée à la source : une seule réserve, des deux côtés de toute comparaison (le second
    # élément d'`unknown` est la lacune projetée de la relance abandonnée, pas un doublon).
    assert answer.unknown.count(limite) == 1
    assert any(check.name == "relance_sans_place_pour_les_limites"
               for step in trace.steps for check in step.checks)
    assert answer.complete is False


def test_la_fusion_reserve_la_place_des_limites_acquises() -> None:
    """Recheck Codex (B2) : une correction de plus ne vaut jamais une réserve acquise de moins.

    La borne effective des factuels réserve structurellement la place des limites de la première
    ébauche : les corrections excédentaires sont tracées, la réserve acquise reste dans la fusion.
    """
    settings = _settings(draft_max_claims=4, draft_max_segments=4)
    limite = "La franchise applicable n'est pas précisée."
    draft = AnswerDraft(
        segments=[{"text": "Clause a.", "kind": "factuel", "claim_ids": ["a"]},
                  {"text": "Clause b.", "kind": "factuel", "claim_ids": ["b"]},
                  {"text": limite, "kind": "limite", "claim_ids": []}],
        claims=[{"claim_id": "a", "text": "Clause a.",
                 "quotes": [{"block_id": f"{DOC_ID}:p1:2", "quote": Q_GARANTIE}]},
                {"claim_id": "b", "text": "Clause b.",
                 "quotes": [{"block_id": f"{DOC_ID}:p1:3", "quote": Q_CONDITION}]}])
    relance = AnswerDraft(
        segments=[{"text": f"Clause {cid}.", "kind": "factuel", "claim_ids": [cid]}
                  for cid in ("c", "d", "e")],
        claims=[{"claim_id": cid, "text": f"Clause {cid}.",
                 "quotes": [{"block_id": f"{DOC_ID}:p1:2", "quote": Q_GARANTIE}]}
                for cid in ("c", "d", "e")])
    # N1 : l'autorité des limites acquises est `Verification.unknown`, pas le draft brut.
    acquise = Verification.model_construct(claims=list(draft.claims), unknown=[limite])
    step = StepTrace(name="rediger")

    fusion = sinistre._reconduire_acquis(draft, relance, acquise, settings, step=step)

    assert [c.claim_id for c in fusion.claims] == ["a", "b", "c"]
    assert [s.text for s in fusion.segments if s.kind == "limite"] == [limite]
    assert any(c.name == "corrections_non_retenues" and not c.ok for c in step.checks)
    assert not any(c.name == "limites_non_reconduites" for c in step.checks)


async def test_une_limite_acquise_survit_a_une_relance_qui_lomet(index: Index) -> None:
    """Revue Codex 4.2a (B2) : la première ébauche porte une limite que la relance ne répète pas.

    La fusion conserve les segments non factuels des deux ébauches : la réserve acquise reste dans
    `unknown`, `nb_manques` ne baisse pas artificiellement, et l'adoption reste une dominance
    honnête (manques égaux, claims strictement supérieures)."""
    limite = "Le contrat ne précise pas la franchise applicable."
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger_avec_limites(DEF, limites=[limite]),
        _verifier(("c2", True, False, False, False, None)),
        _rediger(GAR),
        _verifier(("c1", True, True, False, False, None),
                  ("c2", True, False, False, False, None), facettes=[["c1", "c2"]])])

    assert fake.remaining_script == 0
    assert {c.claim_id for c in answer.claims} == {"c1", "c2"}
    assert any("franchise" in u for u in answer.unknown)


async def test_une_fondatrice_citee_ne_declenche_aucune_relance_supplementaire(index: Index) -> None:
    """Dès qu'une claim survivante cite une fondatrice confirmée, la base décisionnelle existe :
    le déclencheur reste muet et la longueur du script reste l'assertion."""
    answer, _trace, fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None))])

    assert fake.remaining_script == 0 and len(fake.requests) == 3
    assert [c.claim_id for c in answer.claims] == ["c1"]


async def test_une_fondatrice_non_confirmee_ne_declenche_pas_la_relance_fondatrice(
        index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le déclencheur lit le typage confirmé de l'ingestion : sans confirmation, rien n'est exigé."""
    doc = index.corpus.documents[DOC_ID]
    for block in doc.blocks:
        if block.kind in {"garantie", "exclusion"}:
            monkeypatch.setattr(block, "kind_source", None)
    answer, _trace, fake = await _run(index, [
        _comprendre(), _rediger(DEF),
        _verifier(("c2", True, False, False, False, None))])

    assert fake.remaining_script == 0 and len(fake.requests) == 3
    assert [c.claim_id for c in answer.claims] == ["c2"]


def _rediger_avec_limites(*claims: tuple[str, str, list[tuple[str, str]]],
                          limites: list[str]) -> dict:
    """Comme `_rediger`, avec des segments `limite` déclarés par le modèle après les factuels."""
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": ([{"text": f"Clause {cid}.", "kind": "factuel", "claim_ids": [cid]}
                      for cid, _, _ in claims]
                     + [{"text": texte, "kind": "limite", "claim_ids": []} for texte in limites]),
        "claims": [{"claim_id": cid, "text": texte,
                    "quotes": [{"block_id": b, "quote": q} for b, q in quotes]}
                   for cid, texte, quotes in claims]}))


async def test_un_segment_limite_de_la_relance_atterrit_dans_unknown(index: Index) -> None:
    """AD-4 (4.2a, revue I2) : la fusion de relance conserve les limites déclarées par le modèle.

    `Answer.unknown` est rempli depuis les seuls segments `limite` survivants ; une fusion qui les
    supprimerait ferait taire « Ce que je ne sais pas » après toute relance et rendrait `complete`
    atteignable là où il ne l'était pas.
    """
    limite = "Le contrat ne précise pas la franchise applicable."
    answer, _trace, fake = await _run(index, [
        _comprendre(), _rediger(MAUVAISE),
        _rediger_avec_limites(GAR, limites=[limite]),
        _verifier(("c1", True, True, False, False, None))])

    assert fake.remaining_script == 0
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert any("franchise" in u for u in answer.unknown)
    assert answer.complete is False


def test_la_reconduction_refuse_de_tronquer_des_claims_verifiees() -> None:
    """Revue post-4.2a : aucune claim vérifiée ne disparaît sans trace de la fusion de relance.

    `Settings._coherence` garantit draft_max_claims <= draft_max_segments ; si une borne effective
    plus basse rendait la fusion tronquante, `_reconduire_acquis` refuse — même invariant et même
    message que `_rattacher_claims_sinistre` — plutôt que de faire disparaître un acquis vérifié.
    """
    from types import SimpleNamespace

    draft = AnswerDraft.model_validate({
        "segments": [{"text": f"Clause c{i}.", "kind": "factuel", "claim_ids": [f"c{i}"]}
                     for i in range(1, 4)],
        "claims": [{"claim_id": f"c{i}", "text": f"Clause c{i}.",
                    "quotes": [{"block_id": f"{DOC_ID}:p1:2", "quote": Q_GARANTIE}]}
                   for i in range(1, 4)]})
    acquise = SimpleNamespace(claims=[SimpleNamespace(claim_id=f"c{i}") for i in range(1, 4)],
                              unknown=[])
    bornes = SimpleNamespace(draft_max_claims=3, draft_max_segments=2)
    with pytest.raises(ValueError, match="draft_max_claims <= draft_max_segments"):
        sinistre._reconduire_acquis(draft, draft, acquise, bornes, step=StepTrace(name="rediger"))


async def test_une_relance_qui_trouve_la_clause_nest_pas_annulee_par_ses_manques(index: Index) -> None:
    """Campagne B 2.7, garde reconduite en 4.2a : une clause vérifiée bat toujours zéro clause.

    La relance qui retrouve la clause décisionnelle peut déclarer **plus** de manques que le vide
    (ici trois limites) : la dominance générale conserverait alors le vide, qui devient un 503 sur
    une lecture tronquée. Le cas est celui que le retrait de `relance_trouve_clause` rendait à
    nouveau possible.
    """
    limites = ["Le contrat ne précise pas la franchise applicable.",
               "Le plafond annuel d'indemnisation n'est pas indiqué dans les passages lus.",
               "La procédure de déclaration n'est pas décrite dans les passages lus."]
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(MAUVAISE),
        _rediger_avec_limites(GAR, limites=limites),
        _verifier(("c1", True, True, False, False, None))])

    assert fake.remaining_script == 0
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert not any(check.name == "relance_moins_bonne"
                   for step in trace.steps for check in step.checks)


async def test_une_relance_identique_arrete_sur_la_premiere_verification(index: Index) -> None:
    """AD-3 : une relance qui reconduit l'ébauche à l'identique — limites comprises — ne paie pas
    une seconde vérification. La fusion doit préserver les segments non factuels pour que cette
    égalité reste observable."""
    limite = "Le contrat ne précise pas la franchise applicable."
    answer, trace, fake = await _run(index, [
        _comprendre(),
        _rediger_avec_limites(MAUVAISE, limites=[limite]),
        _rediger_avec_limites(MAUVAISE, limites=[limite])])

    assert fake.remaining_script == 0
    assert [s.name for s in trace.steps].count("verifier") == 1
    assert any(check.name == "relance_sans_effet" and not check.ok
               for step in trace.steps for check in step.checks)
    assert answer.found is False


def test_la_fusion_renomme_une_correction_qui_garde_un_identifiant_acquis() -> None:
    """Un contenu différent sous un identifiant déjà vérifié reste contrôlable sous `rN`."""
    settings = _settings()
    draft = AnswerDraft(
        segments=[{"text": "Clause acquise.", "kind": "factuel", "claim_ids": ["c1"]}],
        claims=[{"claim_id": "c1", "text": "Clause acquise.",
                 "quotes": [{"block_id": f"{DOC_ID}:p1:2", "quote": Q_GARANTIE}]}])
    relance = AnswerDraft(
        segments=[{"text": "Clause corrigée.", "kind": "factuel", "claim_ids": ["c1"]}],
        claims=[{"claim_id": "c1", "text": "Clause corrigée.",
                 "quotes": [{"block_id": f"{DOC_ID}:p1:2", "quote": Q_GARANTIE}]}])
    acquise = Verification.model_construct(claims=list(draft.claims))

    fusion = sinistre._reconduire_acquis(draft, relance, acquise, settings, step=StepTrace(name="rediger"))

    assert [c.claim_id for c in fusion.claims] == ["c1", "r1"]
    assert fusion.claims[0].text == "Clause acquise."
    assert fusion.claims[1].text == "Clause corrigée."


def test_la_fusion_saute_un_acquis_reconduit_a_lidentique() -> None:
    """Le modèle qui reconduit fidèlement l'acquis ne le duplique pas dans la fusion."""
    settings = _settings()
    draft = AnswerDraft(
        segments=[{"text": "Clause acquise.", "kind": "factuel", "claim_ids": ["c1"]}],
        claims=[{"claim_id": "c1", "text": "Clause acquise.",
                 "quotes": [{"block_id": f"{DOC_ID}:p1:2", "quote": Q_GARANTIE}]}])
    acquise = Verification.model_construct(claims=list(draft.claims))

    fusion = sinistre._reconduire_acquis(draft, draft, acquise, settings, step=StepTrace(name="rediger"))

    assert [c.claim_id for c in fusion.claims] == ["c1"]
    assert len(fusion.segments) == 1


# --- variantes de *retrouver* (story 4.2d) -------------------------------------
# AD-1, amendement du 25/08/2026 : « la navigation par outils est le mode par défaut de *retrouver* ;
# la variante `deterministe` devient la baseline de comparaison des évals et le repli ». Le sinistre
# ne connaissait qu'une variante — donc **toute** requête `POST /api/v1/sinistre`, dont le corps ne
# porte pas de `variant`, empruntait encore le chemin déterministe.
#
# **Corpus propre à la story, volontairement neutre** (revue croisée 4.2d, I1). Les preuves qui
# portent les AC de 4.2d ne lisent ni `DOC_ID`, ni la fixture `index`, ni `QUESTION`, `FAITS`, `GAR`
# ou les `Q_*` du mini-contrat témoin : aucun assureur, aucun cas du golden set, aucun mot du cas
# témoin (chaleur, mobilier, bougie, subite, incendie, contenu, brûlure). La story ne câble pas un
# document ni un sinistre, elle câble une **variante** : ses preuves ne doivent donc rien devoir à
# l'identité de ce qui est lu. Les tests des histoires antérieures gardent, eux, le mini-contrat
# témoin qui les a écrits — il est leur sujet, pas un décor.
#
# L'idiome — corpus synthétique, puis **permutation** de ses identifiants — est celui de
# `tests/test_metamorphique.py` (story 4.2b) : une décision qui bougerait sous permutation prouverait
# un branchement sur l'identité, et c'est la garde que
# `test_les_decisions_de_variante_survivent_a_la_permutation_du_corpus` tient plus bas.

TERMES_NEUTRES = ("objet", "registre", "structure")
QUESTION_NEUTRE = "La situation décrite relève-t-elle du texte applicable ?"
QUESTION_RESOLUE_NEUTRE = "Le dossier décrit relève-t-il du texte applicable ?"
FAITS_NEUTRES = Faits(
    date="2030-01-02", lieu="local déclaré", montant_eur=100.0,
    description="Le dossier décrit un épisode répertorié ayant atteint l'objet inventorié dans le "
                "local déclaré ; aucun élément nominatif n'est joint.")

# `(clé, kind, texte)` dans l'ordre de lecture de référence. La **clé** est le rôle du bloc : c'est
# elle que les assertions comparent d'un corpus à l'autre, jamais l'identifiant. Aucun texte n'emploie
# un mot du lexique fermé des qualificatifs (`steps/verifier.QUALIFICATIFS`) : la clause n'exige
# aucune qualité, et ces preuves-ci portent sur *retrouver*, pas sur la table d'AD-6.
CLAUSES_DU_SOCLE_NEUTRE = (
    ("titre", "heading", "Titre du chapitre"),
    ("prise_en_charge", "garantie",
     "Les dommages atteignant l'objet inventorié et la structure porteuse lors d'un épisode "
     "répertorié sont pris en charge."),
    ("inscription", "condition",
     "La prise en charge n'est acquise que si le local reste inscrit au registre déclaré."),
    ("definition_objet", "definition",
     "L'objet inventorié désigne tout élément porté au registre annexé."),
    ("ecart_socle", "exclusion",
     "Sont écartés les dommages qui portent sur un registre non déclaré."),
)
CLAUSES_DE_L_ANNEXE_NEUTRE = (
    ("ecart_annexe", "exclusion",
     "Pour les rubriques annexées, les dommages atteignant la structure porteuse sont écartés."),
)
# Citation contiguë de la clause de prise en charge : plus longue que `quote_min_chars` et présente
# dans ce seul bloc — AD-3 exige une occurrence non ambiguë.
CITATION_NEUTRE = "atteignant l'objet inventorié et la structure porteuse"


class IdentiteNeutre(NamedTuple):
    """Tout ce qu'une permutation déplace : le document, ses pages, ses `seq`, ses nœuds, l'ordre.

    Rien de ce que cette structure porte n'est censé peser sur une décision du pipeline : c'est
    exactement l'hypothèse que la garde métamorphique met à l'épreuve.
    """

    doc_id: str
    page_socle: int
    page_annexe: int
    seq_depart: int
    socle: str
    annexe: str
    racine: str
    # L'ordre des termes de la question : une contingence de formulation, jamais un champ typé.
    termes_inverses: bool = False
    # L'ordre de lecture **déclaré** du document (`Node.items`). Il n'est pas de la même nature que
    # les précédents : AD-2 en fait « la source unique de l'ordre de lecture », donc une donnée du
    # corpus. Il a le droit de déplacer l'ordre des blocs transmis — et rien d'autre.
    ordre_lecture_inverse: bool = False

    def noeud(self, nom: str) -> str:
        return f"{self.doc_id}:{nom}"

    def termes(self) -> list[str]:
        return list(reversed(TERMES_NEUTRES)) if self.termes_inverses else list(TERMES_NEUTRES)


IDENTITE_NEUTRE = IdentiteNeutre(doc_id="texte-neutre-a", page_socle=1, page_annexe=2, seq_depart=1,
                                 socle="n1", annexe="n2", racine="n0")
# Permutation de pure **identité** : bijection du `doc_id`, renumérotation des pages, décalage des
# `seq`, renommage des nœuds, inversion de l'ordre des termes. Rien de ce que le corpus déclare ne
# bouge — donc rien du tout ne doit bouger, pas même l'ordre des blocs transmis.
IDENTITE_PERMUTEE = IdentiteNeutre(doc_id="texte-neutre-b", page_socle=7, page_annexe=8,
                                   seq_depart=6, socle="m4", annexe="m5", racine="m3",
                                   termes_inverses=True)
# La même permutation, **plus** l'inversion de l'ordre de lecture déclaré : les décisions restent
# les mêmes, l'ordre des blocs suit le document.
IDENTITE_RELUE = IDENTITE_PERMUTEE._replace(doc_id="texte-neutre-c", page_socle=12, page_annexe=13,
                                            seq_depart=30, socle="k9", annexe="k8", racine="k7",
                                            ordre_lecture_inverse=True)


class CorpusNeutre(NamedTuple):
    index: Index
    identite: IdentiteNeutre
    par_cle: dict[str, str]   # rôle du bloc → block_id sous cette identité

    def bloc(self, cle: str) -> str:
        return self.par_cle[cle]

    def cles(self, block_ids: Iterable[str]) -> list[str]:
        """Les identifiants relus comme des **rôles** : la seule lecture comparable entre corpus."""
        inverse = {block_id: cle for cle, block_id in self.par_cle.items()}
        return [inverse[block_id] for block_id in block_ids]


def _corpus_neutre(identite: IdentiteNeutre) -> CorpusNeutre:
    """Le corpus synthétique de 4.2d sous une identité donnée : un socle commun, une annexe."""
    par_cle: dict[str, str] = {}
    blocs: list[dict[str, Any]] = []
    noeuds: list[Node] = []
    for nom, page, scope, clauses in (
            (identite.socle, identite.page_socle, None, CLAUSES_DU_SOCLE_NEUTRE),
            (identite.annexe, identite.page_annexe, {"kind": "extension"},
             CLAUSES_DE_L_ANNEXE_NEUTRE)):
        items: list[dict[str, str]] = []
        for rang, (cle, kind, texte) in enumerate(clauses):
            loc, seq = f"p{page}", identite.seq_depart + rang
            block_id = f"{identite.doc_id}:{loc}:{seq}"
            par_cle[cle] = block_id
            bloc: dict[str, Any] = {"block_id": block_id, "loc": loc, "seq": seq, "kind": kind,
                                    "text": texte}
            if kind != "heading":
                bloc["kind_source"] = "manual"
                bloc["scope_node_id"] = identite.noeud(nom)
            if kind == "definition":
                bloc["defines"] = "objet inventorié"
            blocs.append(bloc)
            items.append({"block_id": block_id})
        if identite.ordre_lecture_inverse:
            items.reverse()
        noeuds.append(Node(node_id=identite.noeud(nom), level=1, title=f"Section {nom}",
                           items=items, **({"scope": scope} if scope else {})))
    branches = [{"node_id": identite.noeud(identite.socle)},
                {"node_id": identite.noeud(identite.annexe)}]
    if identite.ordre_lecture_inverse:
        branches.reverse()
    noeuds.append(Node(node_id=identite.noeud(identite.racine), level=0, title="Texte applicable",
                       items=branches))
    document = Document(doc_id=identite.doc_id, kind="contrat", title="Texte neutre",
                        edition="2030", nodes=noeuds, blocks=blocs)
    for b in document.blocks:
        b.text_norm = normalize(b.text)
    manifest = {identite.doc_id: ManifestEntry(
        status="servi", source_hash=f"sha-{identite.doc_id}",
        ingest_fingerprint=f"fp-{identite.doc_id}", document_hash="sha-doc", edition="2030")}
    sommaire = "\n".join([f"# {document.title}",
                          f"- {identite.socle} Section {identite.socle}",
                          f"- {identite.annexe} Section {identite.annexe}"])
    return CorpusNeutre(
        index=Index(Corpus(documents={identite.doc_id: document}, manifest=manifest,
                           summaries={identite.doc_id: sommaire})),
        identite=identite, par_cle=par_cle)


@pytest.fixture
def neutre() -> CorpusNeutre:
    return _corpus_neutre(IDENTITE_NEUTRE)


def _settings_neutre(identite: IdentiteNeutre, **kw) -> Settings:
    """Les réglages par défaut, pointés sur le document neutre — jamais sur le contrat témoin."""
    kw.setdefault("sinistre_doc_id", identite.doc_id)
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _tier_de_navigation() -> str:
    """Le tier de navigation vient de la configuration (AD-9), jamais d'un littéral recopié ici."""
    return Settings(_env_file=None, anthropic_api_key="").retrouver_outils_tier


def _comprendre_neutre(corpus: CorpusNeutre, **champs) -> dict:
    """La sortie de *comprendre*, à vocabulaire entièrement neutre : rien du cas témoin n'y entre."""
    return _comprendre(terms=corpus.identite.termes(),
                       question_resolue=QUESTION_RESOLUE_NEUTRE,
                       facettes=["prise en charge"],
                       bien="objet inventorié", evenement="épisode répertorié",
                       lieu="local déclaré", cause="agent externe", moment="période déclarée",
                       **champs)


def _rediger_neutre(corpus: CorpusNeutre) -> dict:
    return _rediger(("k1", "Le texte pris en charge nomme l'objet inventorié.",
                     [(corpus.bloc("prise_en_charge"), CITATION_NEUTRE)]))


def _verifier_neutre() -> dict:
    """Deux listes de qualités **vides**, et c'est fidèle : le texte de la clause n'en écrit aucune.

    Le contrôle du tour 3 de la story 1.8 relit la clause dans le corpus ; sur un texte sans mot du
    lexique fermé, il n'a rien à ajouter. Ces preuves portent sur *retrouver*, pas sur AD-6.
    """
    return _verifier(("k1", True, True, False, False, None, [], []))


def _navigation(corpus: CorpusNeutre, *, noeuds: tuple[str, ...] = (), chercher: bool = True,
                stop_reason: str = "tool_use") -> dict:
    """Un tour de navigation par outils sur le corpus neutre : une recherche, puis des ouvertures.

    Sans recherche ni ouverture, le tour ne demande rien : la navigation est alors tronquée sans
    aucun bloc, l'état exact qui arme le repli déterministe.
    """
    demandes: list[dict[str, Any]] = []
    if chercher:
        demandes.append({"type": "tool_use", "id": "toolu_chercher", "name": "chercher",
                         "input": {"termes": corpus.identite.termes()}})
    for rang, nom in enumerate(noeuds):
        demandes.append({"type": "tool_use", "id": f"toolu_ouvrir_{rang}", "name": "ouvrir_noeud",
                         "input": {"node_id": corpus.identite.noeud(nom)}})
    return fake_message(model=TIERS["micro"], stop_reason=stop_reason, content=demandes)


def _tous_les_noeuds(corpus: CorpusNeutre) -> tuple[str, ...]:
    return (corpus.identite.socle, corpus.identite.annexe)


def _script_outils(corpus: CorpusNeutre, **navigation) -> list:
    """Le script nominal : comprendre, un tour d'outils qui lit tout, rédiger, vérifier."""
    navigation.setdefault("noeuds", _tous_les_noeuds(corpus))
    return [_comprendre_neutre(corpus), _navigation(corpus, **navigation),
            _rediger_neutre(corpus), _verifier_neutre()]


async def _run_neutre(corpus: CorpusNeutre, script: list, *, variant: object = SANS_VARIANTE,
                      settings: Settings | None = None, **kw):
    return await _run(corpus.index, script,
                      settings=settings or _settings_neutre(corpus.identite),
                      question=QUESTION_NEUTRE, faits=FAITS_NEUTRES, variant=variant, **kw)


async def test_sans_variante_le_sinistre_navigue_par_outils(neutre: CorpusNeutre) -> None:
    """AC : `run` **sans** `variant` fait tourner la navigation par outils, chaîne inchangée."""
    answer, trace, fake = await _run_neutre(neutre, _script_outils(neutre))

    assert fake.remaining_script == 0
    assert trace.pipeline == "sinistre" and trace.variant == "outils"
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "restituer"]
    retrouver = trace.steps[1]
    # Le tier **réellement** appelé est publié (AD-10) : la navigation a bien eu lieu ici, là où le
    # chemin déterministe n'appelait aucun modèle.
    assert retrouver.tier == _tier_de_navigation() and len(retrouver.calls) == 1
    assert all(call.tools == ["sommaire", "ouvrir_noeud", "chercher", "definitions"]
               for call in retrouver.calls)
    assert "prise_en_charge" in neutre.cles(retrouver.opened_block_ids)
    assert answer.found and answer.verdict is not None


def test_une_requete_http_sans_variante_sert_la_navigation_par_outils(
        neutre: CorpusNeutre) -> None:
    """AC centrale : c'est parce que le corps ne nomme aucune variante que le défaut est **servi**.

    `POST /api/v1/sinistre` ne transporte pas `variant` (`api/schemas.py`, `extra="forbid"`) : la
    variante servie en HTTP est donc, littéralement, le défaut du pipeline. Le seam est celui des
    autres tests de route de ce fichier — pipeline réel scripté → FastAPI → JSON, sans service
    externe — mais sur le corpus neutre : la story câble une variante, pas un document.
    """
    from fastapi.testclient import TestClient

    from server.app.api.main import create_app

    fake = FakeAnthropic(_script_outils(neutre))
    reglages = _settings_neutre(neutre.identite, env="dev", allow_ungated=True)

    async def pipeline_http(doc_id: str, question: str, faits: Faits, **kw):
        kw["client"] = LlmClient(kw["settings"], anthropic_client=fake)
        return await sinistre.run(doc_id, question, faits, **kw)

    app = create_app(reglages)
    with TestClient(app) as client:
        etat = app.state.foyer
        etat.corpus, etat.index = neutre.index.corpus, neutre.index
        etat.pipeline_sinistre = pipeline_http
        reponse = client.post("/api/v1/sinistre", json={
            "doc_id": neutre.identite.doc_id, "question": QUESTION_NEUTRE,
            "faits": FAITS_NEUTRES.model_dump()})

    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert fake.remaining_script == 0
    assert corps["trace"]["pipeline"] == "sinistre" and corps["trace"]["variant"] == "outils"
    navigation = corps["trace"]["steps"][1]
    assert navigation["name"] == "retrouver" and len(navigation["calls"]) == 1
    assert corps["sources"][0]["block_id"] == neutre.bloc("prise_en_charge")


async def test_la_variante_outils_explicite_est_le_meme_chemin_que_le_defaut(
        neutre: CorpusNeutre) -> None:
    """AC : `variant="outils"` et l'absence de variante ne sont pas deux chemins."""
    _defaut, trace_defaut, _f1 = await _run_neutre(neutre, _script_outils(neutre))
    _explicite, trace_explicite, _f2 = await _run_neutre(neutre, _script_outils(neutre),
                                                         variant="outils")

    assert trace_defaut.variant == trace_explicite.variant == "outils"
    assert [s.name for s in trace_defaut.steps] == [s.name for s in trace_explicite.steps]
    assert (trace_defaut.steps[1].opened_block_ids
            == trace_explicite.steps[1].opened_block_ids)
    assert [c.name for c in trace_defaut.steps[1].checks] == [
        c.name for c in trace_explicite.steps[1].checks]


async def test_la_variante_deterministe_reste_la_baseline_en_code_pur(
        neutre: CorpusNeutre, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC : `variant="deterministe"` garde le chemin code pur **et** son départage de la story 1.8."""
    recus: list[dict[str, Any]] = []
    reel = sinistre.retrouver_deterministe

    def capture(*args: Any, **kw: Any):
        recus.append(kw)
        return reel(*args, **kw)

    monkeypatch.setattr(sinistre, "retrouver_deterministe", capture)
    _answer, trace, fake = await _run_neutre(
        neutre, [_comprendre_neutre(neutre), _rediger_neutre(neutre), _verifier_neutre()],
        variant="deterministe")

    assert fake.remaining_script == 0
    assert trace.variant == "deterministe"
    assert trace.steps[1].calls == []  # aucun appel modèle : la baseline reste comparable
    assert len(recus) == 1 and recus[0]["kinds_prioritaires"] == KINDS_DECISIONNELS


async def test_les_deux_variantes_rendent_le_meme_contrat_sous_le_meme_budget(
        neutre: CorpusNeutre, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC : même `RetrievalResult`, mêmes blocs atteignables, même `RetrievalBudget`."""
    resultats: dict[str, RetrievalResult] = {}
    bornes: dict[str, Any] = {}
    reel_outils, reel_deterministe = sinistre.retrouver_outils, sinistre.retrouver_deterministe

    async def capture_outils(*args: Any, **kw: Any):
        resultat, step = await reel_outils(*args, **kw)
        resultats["outils"], bornes["outils"] = resultat, kw["budget"]
        return resultat, step

    def capture_deterministe(*args: Any, **kw: Any):
        resultat, step = reel_deterministe(*args, **kw)
        resultats["deterministe"], bornes["deterministe"] = resultat, kw["budget"]
        return resultat, step

    monkeypatch.setattr(sinistre, "retrouver_outils", capture_outils)
    monkeypatch.setattr(sinistre, "retrouver_deterministe", capture_deterministe)

    await _run_neutre(neutre, _script_outils(neutre))
    await _run_neutre(neutre, [_comprendre_neutre(neutre), _rediger_neutre(neutre),
                               _verifier_neutre()], variant="deterministe")

    outils, deterministe = resultats["outils"], resultats["deterministe"]
    # Même contrat : mêmes champs, même type — `RetrievalResult` n'a pas de variante d'un côté.
    assert set(outils.model_dump()) == set(deterministe.model_dump())
    # Même borne, à l'octet des seuils près : c'est `retrieval_budget(settings)` des deux côtés (deux
    # requêtes distinctes ici, donc deux objets — l'identité se prouve dans le test du repli).
    attendue = retrieval_budget(_settings_neutre(neutre.identite))
    assert bornes["outils"] == bornes["deterministe"] == attendue
    # Mêmes blocs atteignables : sur ce corpus, la navigation ouvre ce que l'index classe.
    assert sorted(neutre.cles(outils.opened_block_ids)) == sorted(
        neutre.cles(deterministe.opened_block_ids))
    assert (sorted(neutre.cles(b.block_id for b in outils.blocs))
            == sorted(neutre.cles(b.block_id for b in deterministe.blocs)))
    assert outils.truncated is deterministe.truncated is False


async def test_une_navigation_tronquee_sans_bloc_se_replie_une_seule_fois(
        neutre: CorpusNeutre, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC : repli **unique**, borné, sous le même budget, et nommé dans la trace."""
    appels: list[dict[str, Any]] = []
    bornes_outils: list[Any] = []
    reel_deterministe, reel_outils = sinistre.retrouver_deterministe, sinistre.retrouver_outils

    def capture(*args: Any, **kw: Any):
        appels.append(kw)
        return reel_deterministe(*args, **kw)

    async def capture_outils(*args: Any, **kw: Any):
        bornes_outils.append(kw["budget"])
        return await reel_outils(*args, **kw)

    monkeypatch.setattr(sinistre, "retrouver_deterministe", capture)
    monkeypatch.setattr(sinistre, "retrouver_outils", capture_outils)
    answer, trace, fake = await _run_neutre(
        neutre, _script_outils(neutre, noeuds=(), chercher=False))

    assert fake.remaining_script == 0 and answer.found
    retrouver = trace.steps[1]
    # Une seule tentative de repli, et elle garde le départage décisionnel de la story 1.8.
    assert len(appels) == 1 and appels[0]["kinds_prioritaires"] == KINDS_DECISIONNELS
    # Sous **la même** borne, littéralement le même objet : un repli qui reconstruirait un budget de
    # même valeur satisferait une égalité, pas une identité — et AD-1 borne l'étape, pas chaque passe.
    assert len(bornes_outils) == 1 and appels[0]["budget"] is bornes_outils[0]
    assert any(c.name == "repli_deterministe" and not c.ok for c in retrouver.checks)
    # Un seul appel modèle : le repli est du code pur, il n'ajoute aucun tour.
    assert len(retrouver.calls) == 1 and retrouver.tier == _tier_de_navigation()
    assert "prise_en_charge" in neutre.cles(retrouver.opened_block_ids)


async def test_le_repli_fusionne_les_checks_et_les_candidats_des_deux_passes(
        neutre: CorpusNeutre, monkeypatch: pytest.MonkeyPatch) -> None:
    """AD-10 : les candidats écartés des **deux** passes sont publiés, et les checks fusionnés."""
    reel = sinistre.retrouver_deterministe

    def repli_marque(*args: Any, **kw: Any):
        resultat, step = reel(*args, **kw)
        step.checks.append(CheckResult(name="controle_deterministe", ok=True, detail="fusionné"))
        return resultat, step

    monkeypatch.setattr(sinistre, "retrouver_deterministe", repli_marque)
    reglages = _settings_neutre(neutre.identite, retrieval_max_blocks=4)
    candidats_outils = [b for b, _ in neutre.index.chercher(
        neutre.identite.termes(), limit=reglages.search_limit, doc_id=neutre.identite.doc_id)]
    # Un premier tour qui **cherche sans ouvrir**, puis un tour qui conclut : la navigation a des
    # candidats et aucun bloc admis, et le repli hérite donc de candidats à publier.
    answer, trace, fake = await _run_neutre(
        neutre, [_comprendre_neutre(neutre), _navigation(neutre),
                 _navigation(neutre, chercher=False, stop_reason="end_turn"),
                 _rediger_neutre(neutre), _verifier_neutre()], settings=reglages)

    assert fake.remaining_script == 0 and answer.found
    retrouver = trace.steps[1]
    assert [c.name for c in retrouver.checks] == [
        "candidats_non_ouverts", "repli_deterministe", "controle_deterministe"]
    ouverts = set(retrouver.opened_block_ids)
    assert retrouver.discarded_block_ids == [b for b in candidats_outils if b not in ouverts]
    assert retrouver.discarded_block_ids and not answer.complete


async def test_des_blocs_outils_partiels_ne_declenchent_aucun_repli(neutre: CorpusNeutre) -> None:
    """AC : `truncated` avec au moins un bloc admis reste un contexte honnête, publié `complete=False`."""
    # Une seule branche ouverte, tour coupé par `max_tokens` : des blocs utiles, et une lecture bornée.
    answer, trace, fake = await _run_neutre(neutre, _script_outils(
        neutre, noeuds=(neutre.identite.socle,), stop_reason="max_tokens"))

    assert fake.remaining_script == 0
    retrouver = trace.steps[1]
    assert retrouver.opened_block_ids  # des blocs partiels, donc un contexte
    assert not any(c.name == "repli_deterministe" for c in retrouver.checks)
    assert trace.truncations == 1 and answer.found and not answer.complete


async def test_un_repli_lui_aussi_vide_laisse_remonter_le_budget_exceeded(
        neutre: CorpusNeutre) -> None:
    """AC : aucune absence du contrat n'est affirmée à partir d'une borne qui est la nôtre."""
    with pytest.raises(BudgetExceeded, match="aucune absence du contrat n'est affirmée") as capture:
        await _run_neutre(neutre, [_comprendre_neutre(neutre), _navigation(neutre, chercher=False)],
                          settings=_settings_neutre(neutre.identite, retrieval_max_tokens=1))

    trace = capture.value.trace  # AD-16 : la trace partielle voyage avec l'erreur
    assert trace is not None and trace.variant == "outils"
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver"]
    assert any(c.name == "repli_deterministe" for c in trace.steps[1].checks)


async def test_un_echec_de_navigation_voyage_avec_son_etape_partielle(
        neutre: CorpusNeutre) -> None:
    """AC : `PipelineError` pendant *retrouver* ⇒ 503 typé et trace partielle, comme au guide."""
    panne = anthropic.APIStatusError("529", response=httpx.Response(
        529, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
    with pytest.raises(LlmUnavailable) as capture:
        await _run_neutre(neutre, [_comprendre_neutre(neutre), panne])

    trace = capture.value.trace
    assert trace is not None and trace.variant == "outils"
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver"]
    assert len(trace.steps[-1].calls) == 1


async def test_une_variante_inconnue_est_refusee_avant_tout_appel_facture(
        neutre: CorpusNeutre) -> None:
    """AD-1 : « un `pipeline.variant` inconnu ⇒ 400 », et le message énumère les variantes connues."""
    with pytest.raises(InvalidRequest, match="variante") as capture:
        await _run_neutre(neutre, [], variant="agentique")
    assert all(connue in capture.value.message for connue in sinistre.VARIANTES)
    assert sinistre.VARIANTES == {"outils", "deterministe"} and sinistre.VARIANT == "outils"


# --- la garde métamorphique : aucune décision ne tient à l'identité de ce qui est lu -------------

async def _decisions_de_variante(corpus: CorpusNeutre, scenario: str,
                                 monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Ce que 4.2d **décide** sur un corpus donné, lu en rôles et non en identifiants.

    Variante retenue, dispatch effectif (navigation appelée ou non, nombre de replis), champs du
    `RetrievalResult`, checks de la trace, blocs ouverts et écartés — tout ce dont la story répond.
    """
    replis: list[dict[str, Any]] = []
    navigations: list[dict[str, Any]] = []
    champs: dict[str, set[str]] = {}
    reel_deterministe, reel_outils = sinistre.retrouver_deterministe, sinistre.retrouver_outils

    def capture_deterministe(*args: Any, **kw: Any):
        resultat, step = reel_deterministe(*args, **kw)
        replis.append(kw)
        champs["resultat"] = set(resultat.model_dump())
        return resultat, step

    async def capture_outils(*args: Any, **kw: Any):
        resultat, step = await reel_outils(*args, **kw)
        navigations.append(kw)
        champs["resultat"] = set(resultat.model_dump())
        return resultat, step

    monkeypatch.setattr(sinistre, "retrouver_deterministe", capture_deterministe)
    monkeypatch.setattr(sinistre, "retrouver_outils", capture_outils)

    if scenario == "defaut":
        script, variant = _script_outils(corpus), SANS_VARIANTE
    elif scenario == "repli":
        script, variant = _script_outils(corpus, noeuds=(), chercher=False), SANS_VARIANTE
    else:  # baseline explicite
        script = [_comprendre_neutre(corpus), _rediger_neutre(corpus), _verifier_neutre()]
        variant = "deterministe"
    answer, trace, fake = await _run_neutre(corpus, script, variant=variant)

    retrouver = trace.steps[1]
    return {
        "variant": trace.variant,
        "etapes": [s.name for s in trace.steps],
        "navigations": len(navigations),
        "replis": len(replis),
        # `kinds_prioritaires` du départage décisionnel : passé, ou non, aux mêmes endroits.
        "kinds": [kw.get("kinds_prioritaires") for kw in replis],
        "champs_du_resultat": champs["resultat"],
        "checks": [(c.name, c.ok) for c in retrouver.checks],
        "tier": retrouver.tier,
        "appels_modele": len(retrouver.calls),
        "ouverts": corpus.cles(retrouver.opened_block_ids),
        "ecartes": corpus.cles(retrouver.discarded_block_ids),
        "found": answer.found,
        "script_epuise": fake.remaining_script,
    }


@pytest.mark.parametrize("scenario", ["defaut", "repli", "baseline"])
async def test_les_decisions_de_variante_survivent_a_la_permutation_du_corpus(
        scenario: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Story 4.2b, idiome métamorphique appliqué aux décisions de 4.2d.

    Le second corpus est le premier permuté : autre `doc_id`, autres pages, `seq` décalés, nœuds
    renommés, ordre des termes inversé. Aucune de ces choses n'est un champ typé ; aucune ne doit
    donc peser sur la variante retenue, sur le dispatch, sur l'unicité du repli, sur les champs du
    `RetrievalResult`, sur les checks de la trace, ni même sur l'ordre des blocs transmis — le
    corpus déclare exactement le même ordre de lecture des deux côtés. L'égalité est donc exigée
    **entière** : une seule décision qui bougerait ici prouverait un branchement sur l'identité,
    c'est-à-dire la garde qui manquait à 4.2d.

    Les blocs sont comparés par leur **rôle** (`CorpusNeutre.cles`) et non par leur identifiant :
    comparer les identifiants bruts serait exiger que la permutation n'ait pas eu lieu.
    """
    base = await _decisions_de_variante(_corpus_neutre(IDENTITE_NEUTRE), scenario, monkeypatch)
    permute = await _decisions_de_variante(_corpus_neutre(IDENTITE_PERMUTEE), scenario, monkeypatch)

    assert base == permute
    # …et la garde ne serait pas une garde si elle comparait deux fois rien : le scénario a bien
    # exercé le dispatch qu'il annonce, sur deux corpus qui ne partagent aucun identifiant.
    assert base["variant"] == ("deterministe" if scenario == "baseline" else "outils")
    assert base["replis"] == (1 if scenario in ("repli", "baseline") else 0)
    assert base["ouverts"], "le scénario doit avoir ouvert des blocs"


@pytest.mark.parametrize("scenario", ["defaut", "repli", "baseline"])
async def test_l_ordre_de_lecture_declare_ne_deplace_que_l_ordre_des_blocs(
        scenario: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """La seule chose que la permutation a le droit de déplacer, et rien d'autre.

    `Node.items` est « la source unique de l'ordre de lecture » (AD-2) : c'est une **donnée du
    corpus**, pas un identifiant, et l'inverser doit changer l'ordre des blocs transmis — sinon le
    pipeline ne lirait pas le document qu'on lui donne. Ce test sépare donc ce qui a le droit de
    bouger de ce qui n'en a pas : mêmes décisions, même **ensemble** de blocs ouverts, seul l'ordre
    diffère. Sans la dernière assertion, la garde pourrait passer en comparant deux fois le même
    ordre et ne prouverait rien.
    """
    base = await _decisions_de_variante(_corpus_neutre(IDENTITE_NEUTRE), scenario, monkeypatch)
    relu = await _decisions_de_variante(_corpus_neutre(IDENTITE_RELUE), scenario, monkeypatch)

    assert {c: v for c, v in base.items() if c not in ("ouverts", "ecartes")} == {
        c: v for c, v in relu.items() if c not in ("ouverts", "ecartes")}
    assert sorted(base["ouverts"]) == sorted(relu["ouverts"])
    assert sorted(base["ecartes"]) == sorted(relu["ecartes"])
    assert base["ouverts"] != relu["ouverts"], "l'ordre de lecture inversé doit se voir"


# --- bornes d'entrée : rien de facturé -----------------------------------------
async def test_an_unknown_document_is_refused_before_any_billed_call(index: Index) -> None:
    with pytest.raises(CorpusUnavailable):
        await _run(index, [], doc_id="inconnu")


async def test_an_unknown_variant_is_refused_before_any_billed_call(index: Index) -> None:
    with pytest.raises(InvalidRequest, match="variante"):
        await _run(index, [], variant="autre")


async def test_an_oversized_description_is_rejected_never_truncated(index: Index) -> None:
    # la borne citée dans le message vient du schéma, pas d'une constante recopiée (revue 1.8)
    assert sinistre._DESCRIPTION_MAX == 2000
    with pytest.raises(InvalidRequest, match="2000 caractères"):
        await _run(index, [], faits={"description": "x" * 2001})
    # le domaine porte la même borne : le pipeline ne fait que lui donner son code d'erreur
    with pytest.raises(ValueError):
        Faits(description="x" * 2001)


@pytest.mark.parametrize("mal_forme", ["une description libre", ["description"], 42])
async def test_malformed_facts_are_a_bad_request_not_an_internal_error(index: Index,
                                                                       mal_forme: object) -> None:
    """AD-16 : un corps que le serveur ne sait pas lire est un 400, jamais un 500 `internal`.

    `dict(faits)` lève `ValueError`/`TypeError` sur une chaîne, une liste ou un entier : hors du
    `try`, ces trois-là ressortaient en erreur interne alors que l'appelant a simplement mal formé
    son corps (revue 1.8).
    """
    with pytest.raises(InvalidRequest, match="objet lisible"):
        await _run(index, [], faits=mal_forme)


async def test_a_budget_and_a_deadline_are_exclusive(index: Index) -> None:
    """Revue 1.8 : `deadline_s` était ignoré en silence quand un budget était fourni.

    Le budget porte sa deadline et son horloge court déjà : accepter les deux laissait l'appelant
    croire qu'il bornait la requête alors qu'il ne bornait rien.
    """
    settings = _settings()
    client = LlmClient(settings, anthropic_client=FakeAnthropic([]))
    with pytest.raises(InvalidRequest, match="exclusifs"):
        await sinistre.run(None, QUESTION, FAITS, corpus=index.corpus, index=index, client=client,
                           settings=settings, request_id="r", budget=_budget(), deadline_s=5.0)


# --- court-circuits d'AD-5, toujours avec un verdict ----------------------------
async def test_an_out_of_scope_request_is_refused_after_one_micro_call(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre("hors_perimetre")])
    assert fake.remaining_script == 0 and len(fake.requests) == 1  # l'étage `reason` n'est pas atteint
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.reason is not None
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert answer.verdict.escalate  # AD-16 : le dossier repart à la main, ce n'est pas un repli
    assert _questions_attendues(answer.verdict)
    # le texte servi parle du **contrat**, pas du guide (revue 1.8)
    assert "assurance habitation" in answer.texte and "guide" not in answer.texte


async def test_un_refus_sinistre_contradictoire_est_relance_puis_refuse_sans_retrieval(
        index: Index) -> None:
    answer, trace, fake = await _run(index, [
        _comprendre("hors_perimetre", clarification="Quel objet désignez-vous ?"),
        _comprendre("hors_perimetre"),
    ])

    assert [step.name for step in trace.steps] == ["comprendre", "restituer"]
    assert len(fake.requests) == 2 and "retrouver" not in [step.name for step in trace.steps]
    assert answer.reason is not None and answer.reason.kind == "hors_perimetre"
    assert answer.clarification is None


async def test_an_english_refusal_produced_by_the_sinistre_pipeline_stays_english(index: Index) -> None:
    answer, _trace, fake = await _run(index, [_comprendre("hors_perimetre", language="en")])
    assert fake.remaining_script == 0
    assert answer.lang == "en" and answer.lang_fallback is False
    assert answer.texte == PHRASES_DE_REFUS_SINISTRE["en"]["hors_perimetre"]


async def test_a_search_without_a_single_block_refuses_with_a_verdict(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre(terms=["zzzz"])])
    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "restituer"]
    assert answer.reason is not None and answer.reason.kind == "zero_hit"
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert _questions_attendues(answer.verdict)
    assert "aucune clause du contrat" in answer.texte.lower() and "guide" not in answer.texte


async def test_a_request_that_cannot_be_made_autonomous_still_carries_a_verdict(index: Index) -> None:
    answer, trace, _fake = await _run(index, [_comprendre(clarification="De quel bien parlez-vous ?")])
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.clarification == "De quel bien parlez-vous ?"
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert _questions_attendues(answer.verdict)
    assert "contrat" in answer.texte and "guide" not in answer.texte


# --- retrieval : les clauses passent devant à score égal (D7) --------------------
async def test_decisional_blocks_come_first_at_equal_score(index: Index) -> None:
    """AC : « cherche les blocs garantie|exclusion|condition|franchise candidats » — priorité, pas filtre."""
    answer, trace, _fake = await _run(index, [
        _comprendre(terms=["mobilier"]), _rediger(GAR), _verifier(("c1", True, True, False, False, None))])
    ouverts = next(s for s in trace.steps if s.name == "rediger").opened_block_ids
    assert f"{DOC_ID}:p1:2" in ouverts  # la garantie est bien transmise
    assert f"{DOC_ID}:p1:4" in ouverts  # la définition aussi : le typage ne **filtre** pas
    assert answer.found is True


# --- `faits_compris` (story 1.9, D4) : ce que *comprendre* a compris, borné, publié ------

async def test_the_understood_facts_reach_the_single_answer(index: Index) -> None:
    """D4 : l'AC de 1.9 exige « les faits compris » à l'écran ; ils voyagent dans l'unique `Answer`.

    Ce sont `ParsedQuestion.scope` — bien, événement, lieu, cause, moment —, c'est-à-dire ce que
    FR15 fait extraire par *comprendre*. Avant 1.9, ils étaient écrits par l'étape et relus par
    personne (reprise différée de 1.8).
    """
    answer, _trace, _fake = await _run(index, [
        _comprendre(),
        _rediger(GAR),
        _verifier(("c1", True, False, False, False, None))])
    compris = answer.faits_compris
    assert compris is not None
    assert compris.bien == "mobilier de salon"
    assert compris.evenement == "incendie sans embrasement"
    assert compris.lieu == "domicile"
    assert compris.cause == "bougie"
    assert compris.moment == "2026-08-01"


@pytest.mark.parametrize("intent, kind", [("hors_perimetre", "hors_perimetre"),
                                          ("question", "zero_hit")])
async def test_a_refusal_bounds_the_understood_facts_too(index: Index, intent: str,
                                                         kind: str) -> None:
    """Le chemin de refus borne comme le chemin nominal — et sa trace le dit (revue 1.9).

    Sans cette assertion, retirer le bornage de `refuser()` laissait toute la suite verte : le test
    hors borne ne passait que par le chemin nominal, et le test de refus n'observait qu'un libellé
    court. Un libellé de cause de longueur arbitraire, produit par le modèle, atteignait alors
    l'écran **entier**, à l'endroit même que D4 désigne comme celui où les faits compris comptent
    le plus.
    """
    settings = _settings()
    trop_long = "x" * (settings.fait_manquant_max_chars + 1)
    termes = ["helicoptere"] if kind == "zero_hit" else None
    answer, trace, _fake = await _run(
        index, [_comprendre(intent, terms=termes, cause=trop_long)], settings=settings)
    assert answer.found is False and answer.reason is not None and answer.reason.kind == kind
    compris = answer.faits_compris
    assert compris is not None
    assert compris.cause is None  # ignoré, jamais tronqué
    assert compris.bien == "mobilier de salon"
    assert "xxxx" not in answer.model_dump_json()
    restituer_step = next(s for s in trace.steps if s.name == "restituer")
    check = next(c for c in restituer_step.checks if c.name == "faits_compris_hors_borne")
    assert check.ok is False and "cause" in check.detail and trop_long not in check.detail


@pytest.mark.parametrize("intent, kind", [("hors_perimetre", "hors_perimetre"),
                                          ("question", "zero_hit")])
async def test_a_refusal_still_publishes_the_understood_facts(index: Index, intent: str,
                                                              kind: str) -> None:
    """C'est sur un refus qu'ils comptent le plus : « je n'ai rien trouvé » se lit autrement à côté.

    `zero_hit` est obtenu par des termes qui ne touchent aucun bloc ; `hors_perimetre` court-circuite
    dès *comprendre* — dans les deux cas, la portée existe déjà et rien ne justifie de la taire.
    """
    termes = ["helicoptere"] if kind == "zero_hit" else None
    answer, _trace, _fake = await _run(index, [_comprendre(intent, terms=termes)])
    assert answer.found is False and answer.reason is not None and answer.reason.kind == kind
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert answer.faits_compris is not None and answer.faits_compris.bien == "mobilier de salon"


async def test_une_langue_forcee_est_demandee_pour_la_clarification_du_sinistre(index: Index) -> None:
    """Revue Codex 2.4, B1 : le registre du sinistre a la même faille et le même remède — la
    clarification est la seule phrase affichée que le modèle écrit (AD-5), et sa langue se décide
    dans l'unique appel de *comprendre*."""
    answer, _trace, fake = await _run(
        index, [_comprendre(clarification="Von welchem Gut sprechen Sie?", language="fr")], lang="de")
    consigne = fake.requests[0]["messages"][0]["content"].split("</untrusted>")[-1]
    assert "Écris `clarification` en de (allemand), quelle que soit la langue de la question." in consigne
    assert answer.lang == "de" and answer.lang_fallback is False
    assert answer.clarification == "Von welchem Gut sprechen Sie?"


async def test_une_detection_non_servie_pose_quand_meme_la_clarification_du_sinistre(index: Index) -> None:
    """NB1, registre sinistre : même règle, même preuve (revue Codex 2.4, tour 2)."""
    answer, trace, _fake = await _run(
        index, [_comprendre(clarification="¿De qué bien habla?", language="es")])
    assert answer.lang == "fr" and answer.lang_fallback is True
    assert answer.clarification == "¿De qué bien habla?"
    comprendre_step = next(s for s in trace.steps if s.name == "comprendre")
    assert [c.name for c in comprendre_step.checks] == ["clarification_langue_non_affirmee"]


async def test_a_clarification_publishes_no_understood_facts(index: Index) -> None:
    """`ClarificationRequise` n'a pas de portée (AD-5 : deux sorties typées exclusives) — rien à publier."""
    answer, _trace, _fake = await _run(index, [
        _comprendre(clarification="De quel bien parlez-vous ?")])
    assert answer.clarification == "De quel bien parlez-vous ?"
    assert answer.faits_compris is None


async def test_an_out_of_bound_understood_fact_is_dropped_and_the_trace_says_so(index: Index) -> None:
    """D8 de 1.8, appliqué aux faits compris : hors borne, le libellé est **ignoré**, jamais tronqué.

    Une demi-phrase de cause ou de lieu induit en erreur plus sûrement qu'une case vide — et c'est
    du texte du modèle qui atteint un écran, donc la même règle que `fait_manquant`.
    """
    settings = _settings()
    trop_long = "x" * (settings.fait_manquant_max_chars + 1)
    answer, trace, _fake = await _run(index, [
        _comprendre(cause=trop_long, themes=["habitation", trop_long]),
        _rediger(GAR),
        _verifier(("c1", True, False, False, False, None))], settings=settings)

    compris = answer.faits_compris
    assert compris is not None
    assert compris.cause is None  # ignoré
    assert compris.themes == ["habitation"]
    assert compris.bien == "mobilier de salon"  # ce qui tenait dans la borne est intact
    assert "xxxx" not in answer.model_dump_json()  # jamais tronqué : aucun préfixe ne subsiste

    # AD-10 : le libellé écarté se **dit**, et le check nomme les champs, jamais leur contenu.
    restituer_step = next(s for s in trace.steps if s.name == "restituer")
    check = next(c for c in restituer_step.checks if c.name == "faits_compris_hors_borne")
    assert check.ok is False
    assert "cause" in check.detail and "themes" in check.detail
    assert trop_long not in check.detail


async def test_facts_that_fit_leave_no_check_behind(index: Index) -> None:
    """Le check ne se pose que s'il y a quelque chose à dire — sinon la trace mentirait par bruit."""
    _answer, trace, _fake = await _run(index, [
        _comprendre(),
        _rediger(GAR),
        _verifier(("c1", True, False, False, False, None))])
    restituer_step = next(s for s in trace.steps if s.name == "restituer")
    assert [c.name for c in restituer_step.checks if c.name == "faits_compris_hors_borne"] == []


async def test_une_relance_non_demarree_dit_ce_quelle_a_coute_a_la_reponse(index: Index) -> None:
    """Story 2.3 : les **deux** pipelines passent par `relance_abandonnee`, et la lacune qu'elle
    dépose est neutre quant au document — la page sinistre rend la même section « Ce que je ne sais
    pas » que celle du guide, et une phrase qui dirait « le guide » y serait fausse.

    Trois appels au plafond : la relance qu'une claim rejetée rendrait utile n'a pas de quoi démarrer
    (AD-1, « aucun retry ne démarre sans marge »), la réponse vérifiée est servie, et elle n'est pas
    donnée pour complète — avec une phrase qui dit pourquoi.
    """
    budget = RequestBudget(deadline_s=30.0, max_attempts=3, max_cost_eur=0.10)
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(GAR, MAUVAISE),
        _verifier(("c1", True, False, False, False, None))], budget=budget)
    assert fake.remaining_script == 0 and trace.retries == 0
    assert answer.found is True and answer.complete is False
    assert PHRASES_DE_LACUNE["fr"]["relance_abandonnee"] in answer.unknown
    assert "guide" not in " ".join(answer.unknown)
    verifier = next(s for s in trace.steps if s.name == "verifier")
    assert [c.name for c in verifier.checks if c.name == "relance_abandonnee"]
