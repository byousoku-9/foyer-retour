"""Matrice d'E/S de la couche HTTP (spec 1.6) : AD-11, AD-16, AD-13, AD-10, AD-12.

Le pipeline est remplacé par un **double** (`etat.pipeline`) : ces tests portent sur la route, pas
sur les cinq étapes, qui ont les leurs (`tests/test_pipeline_guide.py`). Aucun réseau, aucune clé —
le client Claude est construit mais jamais appelé.

Le corpus, lui, est **réel** (`data/`, chargé par le lifespan) pour tout ce qui touche à `/sante` et
aux pages ; un mini-corpus le remplace quand un test doit exhiber une citation aux offsets connus.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from server.app.api import etat as etat_module
from server.app.api.errors import MESSAGE_INTERNE, gestionnaire_http, gestionnaire_pipeline
from server.app.api.main import create_app
from server.app.api.request_id import CHAMPS_DE_LOG, RequestIdMiddleware
from server.app.api.routes.chat import comparateur_de
from server.app.config import REPO_ROOT, Settings
from server.app.corpus.dictionary import Dictionnaire, load_dictionary
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.corpus.text import normalize
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.answer import (
    AbsenceProof,
    Answer,
    AnswerSegment,
    ClaimStatus,
    LecturePartielle,
    Quote,
    RejectedClaim,
    VerifiedClaim,
    VerifiedQuote,
)
from server.app.domain.document import Document, Node, Source
from server.app.domain.errors import BudgetExceeded, CorpusUnavailable, LlmUnavailable, Timeout
from server.app.domain.ingest import Gate, ManifestEntry
from server.app.domain.profil import Profil
from server.app.domain.trace import BlocTrace, GateTrace, StepTrace, Trace
from server.app.llm.budget import RequestBudget
from server.app.pipelines import guide

XFF = {"X-Forwarded-For": "203.0.113.7"}
DOC_ID = "mini"
ARRIVEE = ("Vous disposez de huit jours après votre arrivée pour vous déclarer au Biergercenter de "
           "votre commune de résidence.")
FAQ_R = "L'inscription se fait auprès de la commune, sur présentation du certificat de résidence."


# --- fabrique du corpus miniature ----------------------------------------

def _mini_corpus() -> tuple[Corpus, Index]:
    """Une fiche (avec son lien officiel) et une FAQ : de quoi éprouver `fiche_id`, `titre`, `url`."""
    blocs = [
        {"block_id": f"{DOC_ID}:farrivee:1", "loc": "farrivee", "seq": 1, "kind": "heading",
         "text": "Déclarer son arrivée"},
        {"block_id": f"{DOC_ID}:farrivee:2", "loc": "farrivee", "seq": 2, "kind": "para", "text": ARRIVEE},
        {"block_id": f"{DOC_ID}:q1:1", "loc": "q1", "seq": 1, "kind": "para", "text": "Où inscrire mon enfant ?"},
        {"block_id": f"{DOC_ID}:q1:2", "loc": "q1", "seq": 2, "kind": "para", "text": FAQ_R},
    ]
    doc = Document(
        doc_id=DOC_ID, kind="guide", title="Mini guide", edition="git:test",
        nodes=[Node(node_id=f"{DOC_ID}:farrivee", level=2, title="Déclarer son arrivée",
                    items=[{"block_id": f"{DOC_ID}:farrivee:1"}, {"block_id": f"{DOC_ID}:farrivee:2"}],
                    sources=[Source(titre="Guichet.lu", url="https://guichet.public.lu/arrivee")]),
              Node(node_id=f"{DOC_ID}:q1", level=2, title="Où inscrire mon enfant ?",
                   items=[{"block_id": f"{DOC_ID}:q1:1"}, {"block_id": f"{DOC_ID}:q1:2"}]),
              Node(node_id=DOC_ID, level=0, title="Mini",
                   items=[{"node_id": f"{DOC_ID}:farrivee"}, {"node_id": f"{DOC_ID}:q1"}])],
        blocks=blocs)
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    manifest = {DOC_ID: ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                                      document_hash="d", edition="git:test")}
    corpus = Corpus(documents={DOC_ID: doc}, manifest=manifest, summaries={DOC_ID: "# Mini"})
    return corpus, Index(corpus)


def _citation(corpus: Corpus, block_id: str, extrait: str) -> VerifiedQuote:
    """Une citation **relue du corpus** : `quote` est exactement `Block.text[text_start:text_end]`."""
    texte = corpus.documents[DOC_ID].block(block_id).text
    debut = texte.index(extrait)
    return VerifiedQuote(block_id=block_id, quote=extrait, start=0, end=len(extrait),
                         text_start=debut, text_end=debut + len(extrait))


def _claim(claim_id: str, texte: str, quote: VerifiedQuote) -> VerifiedClaim:
    return VerifiedClaim(claim_id=claim_id, text=texte, quotes=[quote],
                         status=ClaimStatus(retrouvee=True, pertinente=True, edition="git:test"))


def _trace(**kw: Any) -> Trace:
    defauts: dict[str, Any] = {"request_id": "à-remplacer", "pipeline": "guide", "intent": "question",
                               "total_cost_eur": 0.0123, "steps": [StepTrace(name="comprendre", tier="micro")]}
    return Trace(**{**defauts, **kw})


def _refus(kind: str, **kw: Any) -> Answer:
    return Answer(found=False, complete=False, texte="Refus composé par le code.",
                  segments=[AnswerSegment(text="Refus composé par le code.", kind="limite")],
                  reason=AbsenceProof(kind=kind, terms_searched=["arrivee"], variants_count=0,
                                      blocks_scanned=4, documents=[DOC_ID]), **kw)


class Double:
    """Double de `repondre_guide` : enregistre ce qu'il reçoit, rend ou lève ce qu'on lui donne."""

    def __init__(self, resultat: tuple[Answer, Trace] | None = None, erreur: BaseException | None = None) -> None:
        self.resultat = resultat
        self.erreur = erreur
        self.appels: list[dict[str, Any]] = []

    async def __call__(self, question: str, historique: list, profil: Any, **kw: Any) -> tuple[Answer, Trace]:
        self.appels.append({"question": question, "historique": historique, "profil": profil, **kw})
        if self.erreur is not None:
            raise self.erreur
        assert self.resultat is not None
        answer, trace = self.resultat
        # Le `request_id` du middleware traverse la route : la trace le porte tel quel (AD-10).
        return answer, trace.model_copy(update={"request_id": kw["request_id"]})


# --- fixtures -------------------------------------------------------------

def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _serveur(settings: Settings) -> Any:
    """Une application démarrée une fois pour le module (le lifespan charge le corpus réel)."""
    app = create_app(settings)
    with TestClient(app) as client:
        etat = app.state.foyer
        # `alerts` en fait partie depuis la story 2.1 : un test qui les remplace (quarantaine)
        # laissait sinon l'application sans les alertes du démarrage pour tous les suivants —
        # une dépendance à l'ordre d'exécution, invisible jusqu'à ce qu'une alerte nouvelle
        # soit attendue par un test placé après lui.
        client.origine = (etat.corpus, etat.index, etat.pipeline,  # type: ignore[attr-defined]
                          list(etat.alerts))
        yield client


@pytest.fixture(scope="module")
def prod() -> Any:
    yield from _serveur(_settings(env="prod", allow_ungated=True))


@pytest.fixture(scope="module")
def dev() -> Any:
    yield from _serveur(_settings(env="dev"))


@pytest.fixture(autouse=True)
def _etat_propre(request: pytest.FixtureRequest) -> Any:
    """L'application est partagée par le module ; son état, non — chaque test repart du réel."""
    yield
    for nom in ("prod", "dev"):
        client = request.node.funcargs.get(nom)
        if client is None:
            continue
        etat = client.app.state.foyer
        etat.corpus, etat.index, etat.pipeline, alertes = client.origine
        etat.alerts = list(alertes)
        etat.limiter.reset()


def _brancher(client: TestClient, double: Double, *, mini: bool = False) -> Double:
    etat = client.app.state.foyer
    etat.pipeline = double
    if mini:
        etat.corpus, etat.index = _mini_corpus()
    return double


# --- réponse sourcée ------------------------------------------------------

def test_reponse_sourcee_rend_200_avec_le_contrat_ad11(prod: TestClient) -> None:
    corpus, index = _mini_corpus()
    extrait = "huit jours après votre arrivée"
    claim = _claim("c1", "Le délai est de huit jours.", _citation(corpus, f"{DOC_ID}:farrivee:2", extrait))
    answer = Answer(found=True, complete=True, texte="Le délai est de huit jours.",
                    segments=[AnswerSegment(text="Le délai est de huit jours.", kind="factuel",
                                            claim_ids=["c1"])],
                    claims=[claim])
    trace_http = _trace(
        blocs=[BlocTrace(block_id=f"{DOC_ID}:farrivee:2", doc_id=DOC_ID,
                         node_id=f"{DOC_ID}:farrivee", fiche_id="arrivee",
                         titre="Déclarer son arrivée")],
        gate=GateTrace(profile="vertical", cases=2, countersigned=False, alerts=[]),
    )
    double = _brancher(prod, Double((answer, trace_http)), mini=True)

    r = prod.post("/api/v1/chat", json={"question": "Quel délai après mon arrivée ?", "profil": {},
                                        "historique": []}, headers=XFF)

    assert r.status_code == 200
    j = r.json()
    assert j["via"] == "api/v1"
    assert j["texte"] == answer.texte
    assert j["trace"]["request_id"] == r.headers["X-Request-Id"]
    # Story 2.5 : le corps réellement sérialisé par FastAPI publie les résolutions dans `trace`,
    # sans élargir le contrat HTTP de premier niveau.
    assert j["trace"]["blocs"] == [{
        "block_id": f"{DOC_ID}:farrivee:2", "doc_id": DOC_ID,
        "node_id": f"{DOC_ID}:farrivee", "fiche_id": "arrivee",
        "titre": "Déclarer son arrivée",
    }]
    assert j["trace"]["gate"]["profile"] == "vertical"
    assert "blocs" not in j and "gate" not in j
    assert [s["text"] for s in j["segments"]] == ["Le délai est de huit jours."]
    assert len(j["sources"]) == 1
    source = j["sources"][0]
    assert source == {"block_id": f"{DOC_ID}:farrivee:2", "fiche_id": "arrivee",
                      "titre": "Déclarer son arrivée", "url": "https://guichet.public.lu/arrivee",
                      "quote": extrait, "status": "verifiee"}
    # AD-3, mot pour mot : le texte affiché comme source est relu depuis `corpus`.
    bloc = corpus.documents[DOC_ID].block(f"{DOC_ID}:farrivee:2")
    quote = claim.quotes[0]
    assert source["quote"] == bloc.text[quote.text_start:quote.text_end]
    assert j["fiches"] == ["arrivee"]
    assert j["answer"]["found"] is True
    assert double.appels[0]["question"] == "Quel délai après mon arrivée ?"


def test_les_fiches_sont_distinctes_et_dans_lordre_de_citation(prod: TestClient) -> None:
    corpus, _ = _mini_corpus()
    c1 = _claim("c1", "Délai.", _citation(corpus, f"{DOC_ID}:farrivee:2", "huit jours"))
    c2 = _claim("c2", "Inscription.", _citation(corpus, f"{DOC_ID}:q1:2", "auprès de la commune"))
    c3 = _claim("c3", "Encore le délai.", _citation(corpus, f"{DOC_ID}:farrivee:2", "Biergercenter"))
    answer = Answer(found=True, complete=True, texte="A B C",
                    segments=[AnswerSegment(text="A B C", kind="factuel", claim_ids=["c1", "c2", "c3"])],
                    claims=[c1, c2, c3])
    _brancher(prod, Double((answer, _trace())), mini=True)

    j = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF).json()

    assert [s["block_id"] for s in j["sources"]] == [f"{DOC_ID}:farrivee:2", f"{DOC_ID}:q1:2",
                                                     f"{DOC_ID}:farrivee:2"]
    # Un bloc de FAQ a pour parent `{doc_id}:q{i}` : pas de fiche, et le titre est la question.
    assert j["sources"][1]["fiche_id"] is None
    assert j["sources"][1]["titre"] == "Où inscrire mon enfant ?"
    assert j["sources"][1]["url"] is None
    assert j["fiches"] == ["arrivee"]  # distincts, dans l'ordre, jamais de `null`


def test_sources_enumere_les_claims_puis_leurs_quotes_dans_cet_ordre(prod: TestClient) -> None:
    """L'invariant dont dépend l'appariement du front (story 1.7).

    `SourceItem` ne porte **pas** de `claim_id` : le front reçoit une liste plate. Pour placer chaque
    citation sous la phrase qui la cite, `web/app/chat.js` (`citationsParSegment`) refait la **même**
    énumération que `presenter.sources_de()` — `for claim in answer.claims for quote in claim.quotes`
    — et lit `sources[]` dans l'ordre en vérifiant chaque `block_id`. Changer cet ordre côté serveur
    (trier par fiche, dédupliquer, regrouper) casserait l'appariement en silence : le front
    abandonnerait et peindrait la liste plate, sans que rien ne le dise. Ce test le rend bruyant.
    """
    corpus, _ = _mini_corpus()
    # Deux claims, la première à deux quotes : l'ordre attendu est c1.q1, c1.q2, c2.q1.
    q11 = _citation(corpus, f"{DOC_ID}:farrivee:2", "huit jours")
    q12 = _citation(corpus, f"{DOC_ID}:q1:2", "auprès de la commune")
    q21 = _citation(corpus, f"{DOC_ID}:farrivee:2", "Biergercenter")
    c1 = VerifiedClaim(claim_id="c1", text="Délai et lieu.", quotes=[q11, q12],
                       status=ClaimStatus(retrouvee=True, pertinente=True, edition="git:test"))
    c2 = _claim("c2", "Le guichet.", q21)
    answer = Answer(found=True, complete=True, texte="A B",
                    segments=[AnswerSegment(text="A", kind="factuel", claim_ids=["c1"]),
                              AnswerSegment(text="B", kind="factuel", claim_ids=["c2"])],
                    claims=[c1, c2])
    _brancher(prod, Double((answer, _trace())), mini=True)

    j = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF).json()

    attendu = [q.block_id for claim in (c1, c2) for q in claim.quotes]
    assert [s["block_id"] for s in j["sources"]] == attendu
    assert [s["quote"] for s in j["sources"]] == ["huit jours", "auprès de la commune", "Biergercenter"]
    # Aucune déduplication : le même bloc cité par deux claims paraît deux fois, à son rang.
    assert len(j["sources"]) == sum(len(c["quotes"]) for c in j["answer"]["claims"])


def test_une_citation_jamais_retrouvee_nentre_pas_dans_sources(prod: TestClient) -> None:
    """AD-3 : une quote non retrouvée est la chaîne du **modèle** — elle n'est pas affichée."""
    corpus, _ = _mini_corpus()
    verifiee = _claim("c1", "Délai.", _citation(corpus, f"{DOC_ID}:farrivee:2", "huit jours"))
    echo = RejectedClaim(claim_id="c2", text="Inventé.",
                         quotes=[Quote(block_id=f"{DOC_ID}:farrivee:2", quote="phrase que le modèle a rêvée")],
                         status=ClaimStatus(retrouvee=False, edition="git:test"),
                         rejection_kind="non_retrouvee")
    retrouvee_mais_ecartee = RejectedClaim(
        claim_id="c3", text="Hors sujet.",
        quotes=[_citation(corpus, f"{DOC_ID}:q1:2", "certificat de résidence")],
        status=ClaimStatus(retrouvee=True, pertinente=False, edition="git:test"),
        rejection_kind="non_pertinente")
    answer = Answer(found=True, complete=True, texte="Délai.",
                    segments=[AnswerSegment(text="Délai.", kind="factuel", claim_ids=["c1"])],
                    claims=[verifiee], rejected_claims=[echo, retrouvee_mais_ecartee])
    _brancher(prod, Double((answer, _trace())), mini=True)

    j = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF).json()

    # Revue Codex 1.6, B2 : `sources[]` = les citations de la réponse **affichée**, rien d'autre. Une
    # claim écartée n'est pas une source, quelle que soit la raison du rejet — et `SourceItem` ne
    # porte pas de `claim_id`, donc rien ne permettrait au front de la remettre à sa place.
    assert [s["status"] for s in j["sources"]] == ["verifiee"]
    assert [s["block_id"] for s in j["sources"]] == [f"{DOC_ID}:farrivee:2"]
    assert all("rêvée" not in s["quote"] for s in j["sources"])
    assert all("certificat de résidence" not in s["quote"] for s in j["sources"])
    assert j["fiches"] == ["arrivee"]  # la fiche de la claim écartée n'est pas proposée non plus
    # Elles ne sont pas perdues pour autant : le front les lit dans `rejected_claims`, avec leur nature.
    assert [c["rejection_kind"] for c in j["answer"]["rejected_claims"]] == ["non_retrouvee", "non_pertinente"]
    assert j["answer"]["rejected_claims"][1]["quotes"][0]["text_start"] >= 0  # offsets conservés (AD-3)


def test_une_claim_retrouvee_que_nul_segment_ne_cite_nentre_pas_dans_sources(prod: TestClient) -> None:
    """AD-4 : une claim `non_citee` « n'a nulle part où paraître » — `sources[]` non plus.

    C'est la lettre de l'amendement de la story 1.5 : la claim est retrouvée **et** pertinente, mais
    aucun segment factuel affiché ne la cite. La publier dans `sources[]` lui donnerait exactement
    l'endroit où paraître que l'amendement lui refuse, et l'utilisateur lirait sous la réponse un
    passage qu'aucune de ses phrases n'avance.
    """
    corpus, _ = _mini_corpus()
    affichee = _claim("c1", "Délai.", _citation(corpus, f"{DOC_ID}:farrivee:2", "huit jours"))
    orpheline = RejectedClaim(
        claim_id="c2", text="Inscription.",
        quotes=[_citation(corpus, f"{DOC_ID}:q1:2", "certificat de résidence")],
        status=ClaimStatus(retrouvee=True, pertinente=True, edition="git:test"),
        rejection_kind="non_citee")
    answer = Answer(found=True, complete=True, texte="Délai.",
                    segments=[AnswerSegment(text="Délai.", kind="factuel", claim_ids=["c1"])],
                    claims=[affichee], rejected_claims=[orpheline])
    _brancher(prod, Double((answer, _trace())), mini=True)

    j = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF).json()

    assert [s["block_id"] for s in j["sources"]] == [f"{DOC_ID}:farrivee:2"]
    assert j["fiches"] == ["arrivee"]
    assert j["answer"]["rejected_claims"][0]["rejection_kind"] == "non_citee"


def test_la_citation_publiee_est_relue_du_bloc_et_non_la_chaine_recue(prod: TestClient) -> None:
    """AD-3, à l'endroit de l'affichage : « le texte affiché comme source est toujours relu depuis
    `corpus` » (revue Codex 1.6, B1).

    *vérifier* construit déjà `VerifiedQuote.quote` depuis `Block.text`, mais le presenter le croyait
    sur parole : une `VerifiedQuote` fabriquée ailleurs, avec des offsets valides et une chaîne
    inventée, publiait la chaîne inventée. La sonde de la revue est reproduite ici telle quelle.
    """
    corpus, _ = _mini_corpus()
    bloc = corpus.documents[DOC_ID].block(f"{DOC_ID}:farrivee:2")
    debut = bloc.text.index("huit jours après votre arrivée")
    menteuse = VerifiedQuote(block_id=f"{DOC_ID}:farrivee:2", quote="TEXTE INVENTÉ", start=0, end=5,
                             text_start=debut, text_end=debut + len("huit jours après votre arrivée"))
    claim = VerifiedClaim(claim_id="c1", text="Délai.", quotes=[menteuse],
                          status=ClaimStatus(retrouvee=True, pertinente=True, edition="git:test"))
    answer = Answer(found=True, complete=True, texte="Délai.",
                    segments=[AnswerSegment(text="Délai.", kind="factuel", claim_ids=["c1"])],
                    claims=[claim])
    _brancher(prod, Double((answer, _trace())), mini=True)

    j = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF).json()

    assert j["sources"][0]["quote"] == "huit jours après votre arrivée"
    assert "INVENTÉ" not in json.dumps(j["sources"], ensure_ascii=False)


def test_une_citation_dont_les_offsets_sortent_du_bloc_est_un_echec_terminal(prod: TestClient) -> None:
    """AD-3/AD-16 (revue Codex 1.6, B1, tour 2) : jamais la phrase servie sans la source qu'elle cite.

    Le premier correctif retirait la citation illisible et servait quand même la réponse en 200 —
    donc un segment `factuel` affiché sans aucune source, ce qu'AD-3 empêche (« phrase sans source »)
    et ce qu'AD-16 nomme un dégradé en silence. C'est maintenant un échec terminal enveloppé.
    """
    corpus, _ = _mini_corpus()
    bloc = corpus.documents[DOC_ID].block(f"{DOC_ID}:farrivee:2")
    hors_bloc = VerifiedQuote(block_id=f"{DOC_ID}:farrivee:2", quote="peu importe", start=0, end=5,
                              text_start=len(bloc.text) - 2, text_end=len(bloc.text) + 500)
    claim = VerifiedClaim(claim_id="c1", text="Délai.", quotes=[hors_bloc],
                          status=ClaimStatus(retrouvee=True, pertinente=True, edition="git:test"))
    answer = Answer(found=True, complete=True, texte="Délai.",
                    segments=[AnswerSegment(text="Délai.", kind="factuel", claim_ids=["c1"])],
                    claims=[claim])
    _brancher(prod, Double((answer, _trace())), mini=True)

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    assert r.status_code == 500
    j = r.json()
    assert j["error"]["code"] == "internal"
    assert j["error"]["request_id"] == r.headers["X-Request-Id"]
    # AD-10 : la trace partielle suit l'échec, le coût déjà engagé reste mesurable.
    assert j["trace"]["request_id"] == j["error"]["request_id"]
    # Et rien de la réponse n'est servi : ni le texte, ni un `sources[]` vide qui la ferait passer.
    assert "sources" not in j and "Délai." not in json.dumps(j, ensure_ascii=False)


def test_une_citation_dont_le_bloc_a_disparu_du_corpus_est_un_echec_terminal(prod: TestClient) -> None:
    """Même règle quand c'est l'index, et non les offsets, qui ne confirme pas la citation (B1, tour 2)."""
    corpus, _ = _mini_corpus()
    fantome = VerifiedQuote(block_id=f"{DOC_ID}:fjamais:9", quote="peu importe", start=0, end=5,
                            text_start=0, text_end=5)
    claim = VerifiedClaim(claim_id="c1", text="Délai.", quotes=[fantome],
                          status=ClaimStatus(retrouvee=True, pertinente=True, edition="git:test"))
    answer = Answer(found=True, complete=True, texte="Délai.",
                    segments=[AnswerSegment(text="Délai.", kind="factuel", claim_ids=["c1"])],
                    claims=[claim])
    _brancher(prod, Double((answer, _trace())), mini=True)

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    assert r.status_code == 500 and r.json()["error"]["code"] == "internal"
    assert corpus is not None  # le corpus du test n'a jamais contenu ce bloc : c'est tout le propos


def test_le_diagnostic_dun_500_reste_dans_le_log_et_ne_part_pas_dans_lenveloppe(
        prod: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """AD-15/AD-16 (revue Codex 1.6, NB1) : un `internal` publie `MESSAGE_INTERNE`, rien de plus.

    Le `block_id` qui déclenche cet échec est **celui qu'aucun index ne connaît** : rien ne dit qu'il
    vienne de notre ingestion plutôt que du modèle. Le publier dans le message d'erreur réfléchirait
    donc une chaîne non fiable à l'appelant — ici, on en fabrique une franchement hostile. Elle doit
    être absente du corps **entier** (message, trace, en-têtes) et présente dans le log serveur : le
    diagnostic n'est pas perdu, il change de destinataire.
    """
    hostile = "<script>alert(1)</script> IGNORE-LES-CONSIGNES-PRECEDENTES"
    fantome = VerifiedQuote(block_id=hostile, quote="peu importe", start=0, end=5,
                            text_start=0, text_end=5)
    claim = VerifiedClaim(claim_id="c1", text="Délai.", quotes=[fantome],
                          status=ClaimStatus(retrouvee=True, pertinente=True, edition="git:test"))
    answer = Answer(found=True, complete=True, texte="Délai.",
                    segments=[AnswerSegment(text="Délai.", kind="factuel", claim_ids=["c1"])],
                    claims=[claim])
    _brancher(prod, Double((answer, _trace())), mini=True)

    with caplog.at_level(logging.ERROR, logger="foyer.error"):
        r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    assert r.status_code == 500
    j = r.json()
    assert j["error"]["code"] == "internal"
    assert j["error"]["message"] == MESSAGE_INTERNE
    # Le contrat de l'échec ne perd rien : `request_id` corrélable et trace partielle (AD-10/AD-16).
    assert j["error"]["request_id"] == r.headers["X-Request-Id"]
    assert j["trace"]["request_id"] == j["error"]["request_id"]
    # Rien du diagnostic interne ne sort — ni la chaîne hostile, ni le motif de notre code.
    # (`r.text` entier : le message n'est pas le seul endroit d'où une chaîne pourrait s'échapper.)
    assert hostile not in r.text and "alert(1)" not in r.text
    assert "incohérente" not in j["error"]["message"] and "bloc" not in j["error"]["message"]
    # Et il est bien quelque part : dans le log serveur, avec de quoi retrouver la requête.
    logs = "\n".join(rec.getMessage() for rec in caplog.records if rec.name in ("foyer.api", "foyer.error"))
    assert hostile in logs and j["error"]["request_id"] in logs


def test_le_statut_structure_de_la_claim_reste_publie_par_answer(prod: TestClient) -> None:
    """AD-11 fixe `sources[{block_id, fiche_id, titre, url, quote, status}]` : `status` y est un
    champ plat, pas le `ClaimStatus` d'AD-4.

    Ce qu'AD-4 exige — `retrouvee`, `pertinente`, `edition` — voyage dans `answer.claims[].status`,
    parce qu'AD-11 publie l'`Answer` **intégral** dans la réponse. Rien du contrat n'est perdu ; ce
    test l'atteste, et fait échouer toute projection qui viendrait rogner `answer`.
    """
    corpus, _ = _mini_corpus()
    claim = _claim("c1", "Délai.", _citation(corpus, f"{DOC_ID}:farrivee:2", "huit jours"))
    answer = Answer(found=True, complete=True, texte="Délai.",
                    segments=[AnswerSegment(text="Délai.", kind="factuel", claim_ids=["c1"])],
                    claims=[claim])
    _brancher(prod, Double((answer, _trace())), mini=True)

    j = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF).json()

    assert set(j["sources"][0]) == {"block_id", "fiche_id", "titre", "url", "quote", "status"}
    statut = j["answer"]["claims"][0]["status"]
    assert statut["retrouvee"] is True and statut["pertinente"] is True
    assert statut["edition"] == "git:test"


# --- refus, clarification : des 200 -------------------------------------

def test_un_refus_hors_perimetre_est_un_200(prod: TestClient) -> None:
    _brancher(prod, Double((_refus("hors_perimetre"), _trace(intent="meteo"))))

    r = prod.post("/api/v1/chat", json={"question": "Quel temps fera-t-il ?", "profil": {}}, headers=XFF)

    assert r.status_code == 200
    j = r.json()
    assert j["sources"] == [] and j["fiches"] == []
    assert j["answer"]["found"] is False
    assert j["answer"]["reason"]["kind"] == "hors_perimetre"
    assert j["answer"]["reason"]["blocks_scanned"] == 4


def test_une_clarification_est_un_200(prod: TestClient) -> None:
    answer = _refus("clarification_requise", clarification="Vous parlez de quel contrat ?")
    _brancher(prod, Double((answer, _trace())))

    r = prod.post("/api/v1/chat", json={"question": "Et pour eux ?", "profil": {}}, headers=XFF)

    assert r.status_code == 200
    assert r.json()["answer"]["clarification"] == "Vous parlez de quel contrat ?"


# --- bornes d'entrée : 400, jamais de troncature -------------------------

def test_historique_trop_long_est_refuse_sans_appeler_le_pipeline(prod: TestClient) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    historique = [{"role": "user" if i % 2 == 0 else "assistant", "texte": f"t{i}"} for i in range(7)]

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}, "historique": historique},
                  headers=XFF)

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "jamais tronqué" in r.json()["error"]["message"]
    assert double.appels == []  # rien n'a été tronqué, rien n'a été appelé


def test_question_trop_longue_est_refusee(prod: TestClient) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))

    r = prod.post("/api/v1/chat", json={"question": "a" * 1001, "profil": {}}, headers=XFF)

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    # AD-15 : le message dit *où* est la faute, jamais la valeur reçue.
    assert "a" * 20 not in r.json()["error"]["message"]
    assert double.appels == []


@pytest.mark.parametrize("lang", ["es", "eng", "XX"])
def test_une_langue_non_servie_est_un_400_avant_le_pipeline(prod: TestClient, lang: str) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}, "lang": lang}, headers=XFF)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    message = r.json()["error"]["message"]
    assert all(nom in message for nom in ("français", "anglais", "allemand", "portugais"))
    assert double.appels == []


def test_mille_caracteres_pile_passent(prod: TestClient) -> None:
    _brancher(prod, Double((_refus("zero_hit"), _trace())))
    r = prod.post("/api/v1/chat", json={"question": "a" * 1000, "profil": {}}, headers=XFF)
    assert r.status_code == 200


def test_un_tour_de_plus_de_deux_mille_caracteres_est_refuse(prod: TestClient) -> None:
    _brancher(prod, Double((_refus("zero_hit"), _trace())))
    r = prod.post("/api/v1/chat",
                  json={"question": "q", "profil": {}, "historique": [{"role": "user", "texte": "x" * 2001}]},
                  headers=XFF)
    assert r.status_code == 400


def test_corps_geant_est_refuse_en_413(prod: TestClient) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    maximum = prod.app.state.foyer.settings.request_max_bytes

    r = prod.post("/api/v1/chat", content=b'{"question":"' + b"a" * (maximum + 10) + b'"}',
                  headers={**XFF, "content-type": "application/json"})

    assert r.status_code == 413
    assert r.json()["error"]["code"] == "input_too_long"
    assert r.headers["X-Request-Id"]
    assert double.appels == []


def test_corps_geant_sans_content_length_est_refuse_en_413(prod: TestClient) -> None:
    """La **seconde** branche de `LimiteDeCorps` : rien n'annonce la taille, on compte à la lecture.

    C'est le cas réel derrière le front de Cloud Run, qui parle HTTP/2 (pas de `Content-Length`). Le
    pari du module s'y joue en entier : le 413 est signalé par une `HTTPException` **pendant** la
    lecture du corps, et il ne doit pas se faire ré-emballer en 400 « error parsing the body ».
    Passer `content=` un générateur fait basculer httpx en transfert par morceaux.
    """
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    maximum = prod.app.state.foyer.settings.request_max_bytes

    def morceaux():
        yield b'{"question":"'
        for _ in range((maximum // 1024) + 2):
            yield b"a" * 1024
        yield b'"}'

    r = prod.post("/api/v1/chat", content=morceaux(),
                  headers={**XFF, "content-type": "application/json"})

    assert r.status_code == 413
    assert r.json()["error"]["code"] == "input_too_long"
    assert r.headers["X-Request-Id"]
    assert double.appels == []


def test_un_json_malforme_est_un_400_enveloppe(prod: TestClient) -> None:
    # FastAPI lève une `HTTPException(400)` avant même la validation : elle doit sortir par
    # `gestionnaire_http` dans l'enveloppe d'AD-16, pas en JSON de Starlette.
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    r = prod.post("/api/v1/chat", content=b'{"question": ', headers={**XFF, "content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert r.json()["error"]["request_id"] == r.headers["X-Request-Id"]
    assert double.appels == []


def test_une_question_faite_despaces_est_refusee(prod: TestClient) -> None:
    # `min_length=1` la laisse passer ; elle coûterait un appel modèle pour rien.
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    r = prod.post("/api/v1/chat", json={"question": "   \n ", "profil": {}}, headers=XFF)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert double.appels == []


def test_variante_inconnue_est_refusee(prod: TestClient) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}, "variant": "inconnue"}, headers=XFF)
    assert r.status_code == 400
    assert double.appels == []  # refus HTTP avant le pipeline, donc avant tout appel facturé
    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}, "variant": "deterministe"},
                  headers=XFF)
    assert r.status_code == 200
    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}, "variant": "outils"}, headers=XFF)
    assert r.status_code == 200 and double.appels[-1]["variant"] == "outils"
    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}, "variant": None}, headers=XFF)
    assert r.status_code == 200 and double.appels[-1]["variant"] == "outils"
    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)
    assert r.status_code == 200 and double.appels[-1]["variant"] == "outils"


def test_le_champ_contexte_de_lancien_contrat_est_ignore(prod: TestClient) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}, "contexte": "x" * 500}, headers=XFF)
    assert r.status_code == 200
    assert "contexte" not in double.appels[0]


def test_le_profil_est_filtre_par_profil_keys(prod: TestClient) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    r = prod.post("/api/v1/chat",
                  json={"question": "q", "profil": {"enfants": "2", "mot_de_passe": "secret"}}, headers=XFF)
    assert r.status_code == 200
    filtre = double.appels[0]["profil"].filtered()
    assert filtre == {"enfants": "2"}  # `extra="allow"` reçoit tout, `filtered()` ne laisse passer que PROFIL_KEYS


# --- identité et quotas (AD-13) -----------------------------------------

def test_sans_x_forwarded_for_en_prod_le_pipeline_nest_jamais_appele(prod: TestClient) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}})

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert double.appels == []


def test_sans_x_forwarded_for_en_dev_la_requete_passe(dev: TestClient) -> None:
    _brancher(dev, Double((_refus("zero_hit"), _trace())))
    assert dev.post("/api/v1/chat", json={"question": "q", "profil": {}}).status_code == 200


def test_quota_depasse_rend_429_avec_retry_after_et_naappelle_pas_le_pipeline(prod: TestClient) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    quota = prod.app.state.foyer.settings.rate_limit_per_minute

    for _ in range(quota):
        assert prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF).status_code == 200
    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert r.headers["Retry-After"].isdigit()
    assert len(double.appels) == quota


def test_un_corps_invalide_est_compte_par_le_limiteur(prod: TestClient) -> None:
    """AD-13 — le limiteur est le garde-fou du **volume**, pas seulement du coût modèle.

    S'il ne s'exécutait qu'une fois le corps validé (c'est-à-dire dans la fonction de route), un flot
    de corps malformés passerait entièrement hors quota. La dépendance de routeur le fait tourner
    avant la validation : ici, les 10 requêtes invalides remplissent la fenêtre, et la 11ᵉ — valide,
    cette fois — est refusée en 429.
    """
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    quota = prod.app.state.foyer.settings.rate_limit_per_minute
    for _ in range(quota):
        assert prod.post("/api/v1/chat", json={"pas_de_question": 1}, headers=XFF).status_code == 400

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert double.appels == []


def test_sante_nest_jamais_limitee(prod: TestClient) -> None:
    quota = prod.app.state.foyer.settings.rate_limit_per_minute
    for _ in range(quota + 5):
        assert prod.get("/api/v1/sante", headers=XFF).status_code == 200


# --- échecs terminaux (AD-16) --------------------------------------------

def test_panne_fournisseur_rend_503_avec_la_trace_partielle(prod: TestClient) -> None:
    erreur = LlmUnavailable("le fournisseur a répondu 529")
    erreur.trace = _trace(request_id="r-partiel", total_cost_eur=0.0412)
    _brancher(prod, Double(erreur=erreur))

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    assert r.status_code == 503
    j = r.json()
    assert j["error"]["code"] == "llm_unavailable"
    assert j["error"]["request_id"] == r.headers["X-Request-Id"]
    # AD-16 : « 503 avec trace partielle » — le coût déjà engagé reste publié.
    assert j["trace"]["total_cost_eur"] == 0.0412


def test_plafond_de_cout_rend_503_budget_exceeded(prod: TestClient) -> None:
    erreur = BudgetExceeded("plafond de 0,10 € atteint")
    erreur.trace = _trace()
    _brancher(prod, Double(erreur=erreur))

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "budget_exceeded"
    assert "trace" in r.json()


def test_document_non_servi_rend_503_corpus_unavailable(prod: TestClient) -> None:
    _brancher(prod, Double(erreur=CorpusUnavailable("document 'lux-guide' non servi")))

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "corpus_unavailable"
    assert "trace" not in r.json()  # rien n'avait commencé : aucune trace à publier


def test_un_bug_non_prevu_rend_500_generique(prod: TestClient) -> None:
    _brancher(prod, Double(erreur=ZeroDivisionError("division par zéro dans une projection")))

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    assert r.status_code == 500
    j = r.json()
    assert j["error"]["code"] == "internal"
    assert j["error"]["request_id"] == r.headers["X-Request-Id"]
    # AD-16 : aucune trace technique publiée.
    assert "ZeroDivision" not in r.text and "division par zéro" not in r.text


# --- /sante (AD-11, FR12) -------------------------------------------------

def test_sante_dit_ce_qui_est_servi_et_ce_qui_ne_lest_pas(prod: TestClient) -> None:
    r = prod.get("/api/v1/sante", headers=XFF)

    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["version"] == "dev"
    assert j["documents_servis"] == [
        "axa-lu-optihome-2017", "baloise-lu-home-2-2024", "lux-guide"]
    # Story 3.6 : les trois documents portent un gate `vertical` écrit par `evals run --gate`, donc
    # `/sante` annonce leurs cinq cas — et il n'y a aucune alerte `sans_gate` à publier.
    assert j["gate_profile"] == "vertical"
    assert j["gate_cases"] == 5
    assert j["gate_countersigned"] is False
    assert "sans_gate" not in [a["alerte"] for a in j["alerts"]]
    assert j["dictionary"]["validated"] is False  # AD-5 : le court-circuit « zéro hit » dort
    assert j["thresholds"]["rate_limit_per_minute"] == 10


def test_sante_publie_les_seuils_ajoutes_par_la_story(prod: TestClient) -> None:
    # Convention Seuils : une valeur numérique nouvelle vit dans `config.py` **et** se lit.
    seuils = prod.get("/api/v1/sante", headers=XFF).json()["thresholds"]
    reglages = prod.app.state.foyer.settings
    assert seuils["request_max_bytes"] == reglages.request_max_bytes
    assert seuils["rate_limit_max_clients"] == reglages.rate_limit_max_clients
    assert seuils["retry_after_s"] == reglages.retry_after_s
    # Revue Codex 1.6, M2 : la borne du `cloud_trace` est un seuil comme les autres.
    assert seuils["cloud_trace_max_chars"] == reglages.cloud_trace_max_chars
    assert {
        "mixed_page_image_density": reglages.mixed_page_image_density,
        "ocr_dpi": reglages.ocr_dpi,
        "quality_min_words": reglages.quality_min_words,
        "foreign_signal_min": reglages.foreign_signal_min,
        "french_signal_ratio_min": reglages.french_signal_ratio_min,
        "gibberish_ratio_max": reglages.gibberish_ratio_max,
        "residual_header_min_pages_ratio": reglages.residual_header_min_pages_ratio,
    }.items() <= seuils.items()


def _avec_gate(corpus: Corpus, doc_id: str, profil: str, *, coherent: bool = True,
               cases: int = 1, countersigned: bool = True) -> None:
    """Pose un gate sur une entrée du manifest. `coherent=False` = un gate que le loader neutralise.

    Story 4.5 : un gate `full` porte son protocole, sa révision et son rapport (`Gate`
    l'exige au schéma) — un gate `full` sans eux n'est plus une entrée de manifest valide.
    """
    entree = corpus.manifest[doc_id]
    complet = {"plancher_digest": "0" * 64, "candidate_revision": "1" * 40,
               "report_digest": "2" * 64} if profil == "full" else {}
    entree.gate = Gate(
        profile=profil, evals_ok=True, cases_hash="c", date="2026-08-24", cases=cases,
        countersigned=countersigned,
        source_hash=entree.source_hash if coherent else "autre",
        ingest_fingerprint=entree.ingest_fingerprint, overlay_hash=entree.overlay_hash,
        pipeline_digest="p", prompts_digest="q", model_ids={}, **complet)


def test_sante_publie_le_profil_quand_tous_les_documents_servis_en_ont_un(prod: TestClient) -> None:
    corpus, index = _mini_corpus()
    _avec_gate(corpus, DOC_ID, "vertical", cases=3)
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index

    j = prod.get("/api/v1/sante", headers=XFF).json()
    assert j["gate_profile"] == "vertical"
    # `gate_cases` est la **somme** des cas des documents servis : l'accueil affiche ce nombre.
    assert j["gate_cases"] == 3


def test_sante_tait_le_profil_quand_le_loader_a_neutralise_le_gate(prod: TestClient) -> None:
    """Un gate dont les empreintes ne correspondent plus est **invalide** : le loader sert le document
    avec l'alerte `sans_gate` mais laisse `entry.gate` renseigné. Publier son profil à côté de cette
    alerte serait la bascule silencieuse qu'AD-11 interdit."""
    corpus, index = _mini_corpus()
    _avec_gate(corpus, DOC_ID, "vertical", coherent=False)
    corpus.alerts[DOC_ID] = ["sans_gate"]  # ce que `loader._gate_alerts` aurait produit
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index

    assert prod.get("/api/v1/sante", headers=XFF).json()["gate_profile"] is None


def test_sante_tait_le_compte_de_cas_des_que_le_profil_est_tu(prod: TestClient) -> None:
    """`gate_cases` est strictement adossé à `gate_profile` (story 1.10).

    Publier « 2 cas » à côté d'un `gate_profile: null` laisserait l'accueil écrire un compte sous un
    système dont un document n'est validé par rien : c'est la même bascule silencieuse, chiffrée.
    """
    corpus, index = _mini_corpus()
    _avec_gate(corpus, DOC_ID, "vertical", coherent=False, cases=2)
    corpus.alerts[DOC_ID] = ["sans_gate"]
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index

    j = prod.get("/api/v1/sante", headers=XFF).json()
    assert j["gate_profile"] is None and j["gate_cases"] is None


def test_sante_dit_null_sur_les_deux_quand_aucun_document_nest_servi(prod: TestClient) -> None:
    corpus, index = _mini_corpus()
    corpus.documents.clear()
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index

    j = prod.get("/api/v1/sante", headers=XFF).json()
    assert j["documents_servis"] == [] and j["gate_profile"] is None and j["gate_cases"] is None


def test_une_derogation_posee_en_production_est_refusee_pas_seulement_annoncee() -> None:
    """AC 1.10 : « `ALLOW_UNGATED` est **désactivé** en production à la fin de cette story ».

    Avant (revue Codex 1.10, B3) : la dérivation à `False` ne jouait que si la variable était
    **absente**. `ENV=prod ALLOW_UNGATED=true` — un `--set-env-vars` au déploiement, la seule surface
    réelle — produisait `allow_ungated=True`, et un document sans gate valide était servi en
    production avec une simple alerte. Retirer la ligne du `Dockerfile` n'y changeait rien.
    """
    prod = Settings(_env_file=None, env="prod", allow_ungated=True, anthropic_api_key="x")
    assert prod.allow_ungated is False, "la dérogation reste armable en production"
    assert prod.ungated_demande_en_prod is True, "le refus doit rester dicible"

    # Et la conséquence à l'endroit qui compte : un document sans gate n'est pas servi.
    corpus = load_corpus(REPO_ROOT / "data", allow_ungated=prod.allow_ungated)
    sans_gate = [d for d in corpus.documents if corpus.manifest[d].gate is None]
    assert sans_gate == [], sans_gate

    # En `dev`, rien ne bouge : la dérogation y est le mode de travail normal (AD-7).
    dev = Settings(_env_file=None, env="dev", allow_ungated=True, anthropic_api_key="x")
    assert dev.allow_ungated is True and dev.ungated_demande_en_prod is False
    defaut = Settings(_env_file=None, env="dev", anthropic_api_key="x")
    assert defaut.allow_ungated is True


def test_une_derogation_refusee_en_production_est_annoncee_au_journal_de_demarrage(
        caplog: pytest.LogCaptureFixture) -> None:
    """La moitié de la promesse qui atteint l'opérateur au moment du déploiement (D7).

    Trois documents l'annoncent — `Dockerfile`, `.env.example`, README — et l'alerte de `/sante` ne
    la couvre pas : elle est lue par la page d'accueil, pas par celui qui vient de pousser une
    révision. Sans cette assertion, supprimer l'avertissement laissait la suite verte.
    """
    with caplog.at_level(logging.WARNING, logger="foyer.etat"):
        with TestClient(create_app(_settings(env="prod", allow_ungated=True))):
            pass
    messages = [r.message for r in caplog.records if r.name == "foyer.etat"]
    assert any("ungated_refuse_en_production" in m and "ALLOW_UNGATED" in m for m in messages), messages

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="foyer.etat"):
        with TestClient(create_app(_settings(env="dev", allow_ungated=True))):
            pass
    assert not [r for r in caplog.records if "ungated" in r.message], \
        "en dev, la dérogation est le mode de travail normal : l'annoncer serait du bruit"


def test_une_derogation_refusee_en_production_est_annoncee_sur_sante() -> None:
    """Refuser en silence serait le défaut symétrique de servir en silence.

    Celui qui a posé `ALLOW_UNGATED=true` sur le service croirait servir des documents sans gate,
    alors qu'ils sont en quarantaine. L'alerte le dit là où l'état du système se lit.
    """
    with TestClient(create_app(_settings(env="prod", allow_ungated=True))) as client:
        alertes = client.get("/api/v1/sante", headers=XFF).json()["alerts"]
    noms = [(a["doc_id"], a["alerte"]) for a in alertes]
    assert ("*", "ungated_refuse_en_production") in noms
    detail = [a["detail"] for a in alertes if a["alerte"] == "ungated_refuse_en_production"][0]
    assert "ALLOW_UNGATED" in detail and "refus" in detail


@pytest.mark.parametrize("env,allow", [("prod", False), ("dev", True), ("dev", False)])
def test_lalerte_de_derogation_ne_se_leve_que_en_production(env: str, allow: bool) -> None:
    """En `dev`, `ALLOW_UNGATED` est le mode de travail normal : l'annoncer serait du bruit."""
    with TestClient(create_app(_settings(env=env, allow_ungated=allow))) as client:
        alertes = client.get("/api/v1/sante", headers=XFF).json()["alerts"]
    assert "ungated_refuse_en_production" not in [a["alerte"] for a in alertes]


def test_limage_de_production_sert_les_trois_documents_sans_derogation() -> None:
    """Après 3.6, `ENV=prod` sans `ALLOW_UNGATED` sert les trois documents gatés.

    C'est exactement la configuration du `Dockerfile`. Sans l'un des gates, le document concerné
    partirait en quarantaine plutôt que d'être servi par dérogation.
    """
    with TestClient(create_app(_settings(env="prod"))) as client:
        j = client.get("/api/v1/sante", headers=XFF).json()
    assert j["documents_servis"] == [
        "axa-lu-optihome-2017", "baloise-lu-home-2-2024", "lux-guide"]
    assert j["gate_profile"] == "vertical" and j["gate_cases"] == 5
    assert j["gate_countersigned"] is False
    assert [a["alerte"] for a in j["alerts"] if a["alerte"] == "sans_gate"] == []


def test_sante_tait_le_profil_quand_deux_documents_nont_pas_le_meme(prod: TestClient) -> None:
    corpus, index = _mini_corpus()
    _avec_gate(corpus, DOC_ID, "vertical")
    # Un second document servi, avec un autre profil. Seul `corpus.served` compte ici : `/sante` ne
    # consulte pas l'index, on ne le reconstruit donc pas (les node_id seraient en doublon).
    corpus.manifest["autre"] = corpus.manifest[DOC_ID].model_copy(deep=True)
    corpus.documents["autre"] = corpus.documents[DOC_ID].model_copy(deep=True)
    _avec_gate(corpus, "autre", "full")
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index

    assert prod.get("/api/v1/sante", headers=XFF).json()["gate_profile"] is None


def test_sante_dit_ok_false_et_publie_la_quarantaine_quand_le_guide_nest_pas_servi(prod: TestClient) -> None:
    """`testerApi()` ne lit **que** `ok` pour choisir entre l'API et le repli local du site : dire
    `true` alors que le guide est en quarantaine ferait poser des questions qui finiraient en 503."""
    corpus, index = _mini_corpus()  # ne contient pas `guide_doc_id`
    corpus.quarantine["lux-guide"] = "sans_gate"
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index
    etat.alerts = etat_module._alertes(corpus)

    j = prod.get("/api/v1/sante", headers=XFF).json()

    assert j["ok"] is False
    quarantaine = [a for a in j["alerts"] if a["alerte"] == "quarantaine"]
    assert quarantaine == [{"doc_id": "lux-guide", "alerte": "quarantaine", "detail": "sans_gate"}]


def test_sante_publie_les_trois_booleens_du_dictionnaire_et_son_alerte(prod: TestClient) -> None:
    """AD-5 : les deux faits, la règle qu'ils décident, et l'alerte qui dit que le refus dort.

    `data/dictionary.json` n'est pas encore validé à la main : les trois booléens sont donc faux et
    `dictionnaire_non_valide` est publiée avec `doc_id: "*"` — c'est une propriété du **service**,
    pas d'un document. La page d'accueil lit `refus_zero_hit_actif` au lieu de refaire la
    conjonction : la règle n'a qu'une autorité.
    """
    j = prod.get("/api/v1/sante", headers=XFF).json()

    assert set(j["dictionary"]) == {"validated", "corpus_ok", "refus_zero_hit_actif"}
    assert j["dictionary"]["validated"] is False
    # Le second verrou, sur le corps **réel** : le dictionnaire est livré depuis la story 2.1 et ses
    # empreintes décrivent le corpus servi, donc les variantes sont armées — c'est ce qui distingue
    # « la signature est due » de « il n'y a pas de dictionnaire dans cette image ». Sans cette
    # assertion, les deux états passaient pour le même (revue coordonnée 2.1).
    assert j["dictionary"]["corpus_ok"] is True
    # `validated ∧ corpus_ok` : le refus reste désarmé tant que personne n'a signé.
    assert j["dictionary"]["refus_zero_hit_actif"] is False
    alertes = {a["alerte"]: a for a in j["alerts"]}
    assert "dictionnaire_non_valide" in alertes
    assert alertes["dictionnaire_non_valide"]["doc_id"] == "*"
    # Le refus « zéro hit » est nommé dans le détail : l'alerte dit ce qui est **désarmé**, pas
    # seulement ce qui manque (AD-16 — dit, jamais tu).
    assert "zéro hit" in alertes["dictionnaire_non_valide"]["detail"]


def _ecrire_dictionnaire(tmp_path: Any, corpus: Corpus, **over: Any) -> Any:
    """Un `dictionary.json` cohérent avec le corpus donné, sur lequel `over` prend le dessus."""
    hashes = {d: corpus.manifest[d].source_hash for d in corpus.served}
    base: dict[str, Any] = {"schema_version": "1", "corpus_source_hashes": hashes,
                            "corpus": {"matricule": ["numero national"]}, "intents": {},
                            "candidate_questions": {}, "validated": False,
                            "validated_by": None, "validated_at": None}
    (tmp_path / "dictionary.json").write_text(
        json.dumps(base | over, ensure_ascii=False), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("over, attendus", [
    ({}, (False, True, False)),
    # AD-5 : `validated ∧ corpus_ok` — et rien d'autre — arme le refus.
    ({"validated": True, "validated_by": "Lancelot Oudin", "validated_at": "2026-08-25T10:00:00Z"},
     (True, True, True)),
    # Un dictionnaire d'un **autre** corpus ne dit rien de celui-ci : ni variantes, ni court-circuit.
    ({"corpus_source_hashes": {"lux-guide": "autre"}}, (False, False, False)),
    ({"corpus_source_hashes": {}}, (False, False, False)),
    # Revue Codex 1.6, M1 : `bool("false")` vaut `True`, et pydantic convertirait `"true"` en `True`
    # sans `StrictBool`. Un `validated` rendu en **chaîne** — fichier bricolé à la main — aurait
    # ré-armé le court-circuit « zéro hit » qu'AD-5 tient désactivé sans signature humaine.
    ({"validated": "true"}, (False, False, False)),
    ({"validated": "false"}, (False, False, False)),
    ({"validated": 1}, (False, False, False)),
    # Un « validé par personne » est une contradiction : le fichier entier est refusé.
    ({"validated": True}, (False, False, False)),
    # Un champ de plus qu'AD-5 n'énumère pas : ce n'est pas un dictionnaire enrichi, c'est un
    # fichier qu'on ne sait pas lire.
    ({"inconnu": 1}, (False, False, False)),
])
def test_les_deux_verrous_du_dictionnaire_se_lisent_ou_restent_faux(
        tmp_path: Any, over: dict, attendus: tuple) -> None:
    corpus = load_corpus(REPO_ROOT / "data", allow_ungated=True)
    _ecrire_dictionnaire(tmp_path, corpus, **over)
    from server.app.corpus.racine import _lecture_interne_sans_racine
    with _lecture_interne_sans_racine(tmp_path) as lecture:
        d = load_dictionary(tmp_path, corpus, "lux-guide", lecture=lecture)
    assert (d.validated, d.corpus_ok, d.court_circuit_actif) == attendus


@pytest.mark.parametrize("contenu", [b"pas du json", b"[]", b"{}"])
def test_un_dictionnaire_illisible_ou_non_conforme_nempeche_jamais_de_servir(
        tmp_path: Any, contenu: bytes) -> None:
    """AD-7 : « un fichier absent, illisible ou non conforme désactive une optimisation, il
    n'empêche jamais de servir et ne lève jamais au démarrage »."""
    corpus = load_corpus(REPO_ROOT / "data", allow_ungated=True)
    (tmp_path / "dictionary.json").write_bytes(contenu)
    from server.app.corpus.racine import _lecture_interne_sans_racine
    with _lecture_interne_sans_racine(tmp_path) as lecture:
        d = load_dictionary(tmp_path, corpus, "lux-guide", lecture=lecture)
    assert d.court_circuit_actif is False and d.utilisable is False and d.raison


def test_les_deux_alertes_du_dictionnaire_disent_deux_causes_distinctes(tmp_path: Any) -> None:
    """AD-16 : deux causes, deux correctifs — un dictionnaire d'un autre corpus n'est pas seulement
    « non validé », et taire l'une des deux raisons ferait chercher la mauvaise."""
    corpus = load_corpus(REPO_ROOT / "data", allow_ungated=True)
    _ecrire_dictionnaire(tmp_path, corpus, corpus_source_hashes={"lux-guide": "autre"})
    from server.app.corpus.racine import _lecture_interne_sans_racine
    with _lecture_interne_sans_racine(tmp_path) as lecture:
        dictionnaire = load_dictionary(tmp_path, corpus, "lux-guide", lecture=lecture)
    alertes = etat_module._alertes_dictionnaire(dictionnaire)
    noms = [a.alerte for a in alertes]
    assert noms == ["dictionnaire_non_valide", "dictionnaire_corpus_perime"]
    assert all(a.doc_id == "*" for a in alertes)
    # Le dictionnaire **absent** ne porte que la première : rien n'est périmé, rien n'est là.
    with _lecture_interne_sans_racine(tmp_path / "vide") as lecture:
        dictionnaire_absent = load_dictionary(
            tmp_path / "vide", corpus, "lux-guide", lecture=lecture)
    absent = etat_module._alertes_dictionnaire(dictionnaire_absent)
    assert [a.alerte for a in absent] == ["dictionnaire_non_valide"]


def test_alerte_dictionnaire_masque_le_chemin_http_et_le_garde_au_log(
        prod: TestClient, caplog: Any) -> None:
    """M7 : la cause complète reste exploitable en interne, jamais réfléchie par `/sante`."""
    secret = "/srv/data/dictionary.json"
    dictionnaire = Dictionnaire(raison=f"lecture impossible : {secret}")
    with caplog.at_level(logging.WARNING, logger="foyer.etat"):
        alertes = etat_module._alertes_dictionnaire(dictionnaire)
        assert caplog.records == []  # projection pure : aucun double log
        etat_module._journaliser_dictionnaire(dictionnaire)
    prod.app.state.foyer.alerts = alertes

    reponse = prod.get("/api/v1/sante", headers=XFF)
    assert reponse.status_code == 200
    assert secret not in reponse.text and "[emplacement masqué]" in reponse.text
    assert secret in caplog.text
    assert len([record for record in caplog.records
                if record.name == "foyer.etat" and record.levelno == logging.WARNING]) == 1


def test_la_route_passe_le_dictionnaire_charge_au_pipeline(prod: TestClient) -> None:
    """AD-5/AD-7 : chargé une fois au démarrage, il voyage comme `corpus`, `index` et `client`.

    Le pipeline ne voit pas `corpus` (table des couches) : il ne peut pas le charger lui-même, et
    sans ce passage le court-circuit d'AD-5 n'aurait aucune chance d'être armé un jour.
    """
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)
    assert double.appels[0]["dictionnaire"] is prod.app.state.foyer.dictionnaire


def test_letat_na_quun_seul_porteur_de_letat_du_dictionnaire(prod: TestClient) -> None:
    """Un seul fait, une seule autorité : l'objet chargé, et rien à côté de lui.

    `EtatApp` a porté un `dictionary_validated` — d'abord un champ (1.6), puis une propriété dérivée
    (2.1). La propriété se justifiait par « plusieurs lecteurs s'y adossent » ; il n'en restait plus
    aucun, `routes/sante.py` lisant `etat.dictionnaire.validated` en direct. Un accesseur que seul
    son test appelle n'est pas une compatibilité, c'est un second nom pour le même fait — et deux
    noms finissent par diverger (revue coordonnée 2.1).
    """
    etat = prod.app.state.foyer
    assert not hasattr(etat, "dictionary_validated")
    assert etat.dictionnaire.validated is False


def test_lalias_sante_rend_exactement_la_meme_chose(prod: TestClient) -> None:
    assert prod.get("/sante", headers=XFF).json() == prod.get("/api/v1/sante", headers=XFF).json()


def test_lalias_chat_rend_exactement_le_meme_contrat(prod: TestClient) -> None:
    _brancher(prod, Double((_refus("zero_hit"), _trace())))
    corps = {"question": "q", "profil": {}}
    a = prod.post("/chat", json=corps, headers=XFF).json()
    b = prod.post("/api/v1/chat", json=corps, headers=XFF).json()
    a["trace"]["request_id"] = b["trace"]["request_id"] = "-"  # seul l'identifiant diffère
    assert a == b


def test_limage_narme_plus_allow_ungated_maintenant_que_les_gates_existent() -> None:
    """Garde-fou de la ligne `ENV` du `Dockerfile` (AD-7), **inversé** par la story 1.10.

    Posé en 1.6, il échouait le jour où le premier gate apparaîtrait alors que la ligne
    `ENV ALLOW_UNGATED=true` était encore là. Ce jour est arrivé, la ligne est partie, et l'invariant
    s'est retourné : tant qu'un gate existe, l'image ne doit **plus** armer la dérogation. Le laisser
    en `skip` aurait fait d'un test devenu vert par disparition de son objet un test qui ne dit plus
    rien — et rien n'empêcherait de remettre la ligne demain.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_bytes())
    gates = [doc_id for doc_id, e in manifest.items() if e.get("gate") is not None]
    assert gates, "aucun gate : relancer `python -m server.evals.run --gate {doc_id}`"
    lignes_env = [l for l in dockerfile.splitlines() if l.startswith("ENV ")]
    assert not any("ALLOW_UNGATED" in l for l in lignes_env), (
        f"un gate existe ({gates}) et le Dockerfile arme encore ALLOW_UNGATED : l'image servirait "
        "des documents non validés avec une simple alerte que personne ne regarde")


# --- pages statiques (AD-12) ---------------------------------------------

@pytest.mark.parametrize("chemin", ["/", "/accueil.js", "/guide/", "/guide/app/kb.js",
                                    "/guide/app/styles.css", "/sinistre/", "/sinistre/sinistre.js"])
def test_les_pages_sont_servies_sur_la_meme_origine(prod: TestClient, chemin: str) -> None:
    assert prod.get(chemin).status_code == 200


def test_les_fichiers_du_site_ne_sont_pas_renommes(prod: TestClient) -> None:
    # `web/` a des chemins relatifs (`app/kb.js`) : un renommage casserait le site sans bruit.
    assert "window.KB" in prod.get("/guide/app/kb.js").text


def test_la_page_daudit_dynamique_passe_avant_le_montage_statique(prod: TestClient) -> None:
    """Story 3.5 : le segment ``doc_id`` sert l'hôte HTML, pas un faux chemin de fichier."""
    r = prod.get("/sinistre/ingestion/axa-lu-optihome-2017")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'id="rapport"' in r.text
    assert 'src="/sinistre/ingestion.js?v=1"' in r.text

    invalide = prod.get("/sinistre/ingestion/doc_id-invalide")
    assert invalide.status_code == 400
    assert invalide.json()["error"]["code"] == "invalid_request"


def test_un_identifiant_quarantine_invalide_nentre_jamais_dans_la_liste_publique(
        prod: TestClient) -> None:
    etat = prod.app.state.foyer
    invalides = ("../secret", "/root/secret", "doc_id-invalide")
    try:
        for doc_id in invalides:
            etat.corpus.quarantine[doc_id] = "quarantaine de test"
        publies = {item["doc_id"] for item in prod.get("/api/v1/documents").json()}
        assert publies.isdisjoint(invalides)
    finally:
        for doc_id in invalides:
            etat.corpus.quarantine.pop(doc_id, None)


def test_laccueil_provisoire_donne_les_trois_entrees(prod: TestClient) -> None:
    page = prod.get("/").text
    assert '"/guide/"' in page and '"/sinistre/"' in page and "github.com/byousoku-9/foyer-retour" in page


def test_un_fichier_statique_absent_reste_un_404_nu(prod: TestClient) -> None:
    r = prod.get("/guide/app/inexistant.js")
    assert r.status_code == 404
    assert "error" not in r.text  # pas de code d'AD-16 inventé pour un fichier manquant


@pytest.mark.asyncio
async def test_un_5xx_qui_ne_vient_pas_du_pipeline_sort_enveloppe_en_internal() -> None:
    """AD-16 : l'enveloppe est unique, et elle n'a pas de trou côté 5xx (revue Codex 1.6, I1).

    Deux choses à la fois. D'abord, un 503 levé hors `PipelineError` ne prend pas le code
    `llm_unavailable` : `HTTP_STATUS` n'est pas injectif, cinq codes mènent à 503, et en choisir un
    ferait passer pour une panne du fournisseur de modèle un échec de n'importe quel composant.
    Ensuite, il ne ressort pas nu pour autant : `internal` décrit exactement une panne du serveur que
    personne n'a su nommer, et l'enveloppe reste la même pour tout le monde.
    """
    requete = Request({"type": "http", "method": "GET", "path": "/x", "headers": [],
                       "state": {"request_id": "r-5xx"}})
    reponse = await gestionnaire_http(requete, StarletteHTTPException(503, "indisponible"))
    assert reponse.status_code == 500
    corps = json.loads(reponse.body)
    assert corps["error"] == {"code": "internal", "message": MESSAGE_INTERNE, "request_id": "r-5xx"}
    assert b"llm_unavailable" not in reponse.body
    assert b"indisponible" not in reponse.body  # le détail part au log serveur, pas sur le réseau


@pytest.mark.asyncio
async def test_nos_propres_503_gardent_leur_statut_et_leur_code() -> None:
    """Le correctif d'I1 ne touche pas le chemin normal : un `PipelineError` reste un 503 nommé."""
    requete = Request({"type": "http", "method": "POST", "path": "/api/v1/chat", "headers": [],
                       "state": {"request_id": "r-pipe", "log_fields": {}}})
    reponse = await gestionnaire_pipeline(requete, LlmUnavailable("fournisseur injoignable"))
    assert reponse.status_code == 503
    assert json.loads(reponse.body)["error"]["code"] == "llm_unavailable"


def test_aucune_configuration_cors(prod: TestClient) -> None:
    # AD-12 : une seule origine, donc rien à autoriser.
    r = prod.get("/api/v1/sante", headers={**XFF, "Origin": "https://ailleurs.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# --- journal (AD-10, AD-15) ----------------------------------------------

def test_la_ligne_de_log_porte_les_champs_dad10_et_aucun_texte(prod: TestClient,
                                                               caplog: pytest.LogCaptureFixture) -> None:
    answer = _refus("zero_hit")
    _brancher(prod, Double((answer, _trace(intent="question", total_cost_eur=0.021))))
    secret = "mon enfant est malade et je gagne 3000 euros"

    with caplog.at_level(logging.INFO, logger="foyer.request"):
        prod.post("/api/v1/chat", json={"question": secret, "profil": {"enfants": "2"}},
                  headers={**XFF, "X-Cloud-Trace-Context": "abc123/1;o=1"})

    lignes = [json.loads(r.message) for r in caplog.records if r.name == "foyer.request"]
    assert len(lignes) == 1
    ligne = lignes[0]
    assert ligne["intent"] == "question" and ligne["found"] is False
    assert ligne["reason_kind"] == "zero_hit" and ligne["cost_eur"] == 0.021
    assert ligne["variants_count"] == 0 and ligne["blocks_scanned"] == 4
    assert ligne["cloud_trace"] == "abc123/1;o=1"  # AD-10 : dans son propre champ
    assert ligne["status"] == 200 and ligne["request_id"]
    brut = json.dumps(ligne, ensure_ascii=False)
    assert secret not in brut and "terms_searched" not in brut and "arrivee" not in brut


def test_le_journal_nomme_une_lecture_partielle_au_lieu_de_la_taire(
        prod: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """Story 4.2f : la bascule 503 → 200 ne doit pas rendre ce chemin invisible au journal.

    Avant le diff, ces requêtes sortaient en lignes d'erreur `budget_exceeded` : parfaitement
    repérables. Servies en 200 avec `reason_kind=null`, elles devenaient indistinguables de
    n'importe quelle autre réponse, alors qu'AD-10 fait précisément de ces champs le support de
    « l'analytique des échecs ». Le mot posé est celui du harness d'évals (`lecture_tronquee`) : le
    vocabulaire ne change ni avec le code HTTP, ni avec le lecteur.
    """
    answer = Answer(found=False, complete=False, texte="Ma lecture s'est arrêtée avant de conclure.",
                    lecture_partielle=LecturePartielle(nodes_read=2, blocks_read=5,
                                                       documents=[DOC_ID]),
                    unknown=["Je n'ai pas pu lire tout ce qui pouvait concerner votre question."])
    _brancher(prod, Double((answer, _trace(intent="question", total_cost_eur=0.018))), mini=True)

    with caplog.at_level(logging.INFO, logger="foyer.request"):
        r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    assert r.status_code == 200
    ligne = [json.loads(rec.message) for rec in caplog.records
             if rec.name == "foyer.request"][-1]
    assert ligne["found"] is False and ligne["reason_kind"] == "lecture_tronquee"
    # Les deux compteurs d'un balayage restent nuls : ils mesurent autre chose que ce qui a été lu,
    # et les renseigner ici mélangerait deux grandeurs dans la même colonne.
    assert ligne["variants_count"] is None and ligne["blocks_scanned"] is None
    assert ligne["cost_eur"] == 0.018 and ligne["status"] == 200


def test_un_champ_hors_liste_ne_peut_pas_sortir_par_le_journal(caplog: pytest.LogCaptureFixture) -> None:
    """AD-10 clôt la liste des champs logués : une route future ne peut pas la contourner.

    Le filtre est éprouvé sur son entrée directe : une route qui poserait `terms_searched` ou le
    texte de la question dans `request.state.log_fields` — par distraction ou par zèle — ne pourrait
    pas les faire sortir par ici. C'est le seul endroit où la règle d'AD-10 est applicable en code.
    """
    scope = {"type": "http", "method": "POST", "path": "/api/v1/chat", "headers": [],
             "state": {"log_fields": {"intent": "question", "found": True,
                                      "terms_searched": ["sinistre", "divorce"],
                                      "question": "ma question personnelle"}}}
    with caplog.at_level(logging.INFO, logger="foyer.request"):
        RequestIdMiddleware._journaliser(scope, request_id="r-1", statut=200, ms=12,
                                         cloud_trace_max=128)

    ligne = json.loads(caplog.records[-1].message)
    assert ligne["intent"] == "question" and ligne["found"] is True
    assert "terms_searched" not in ligne and "question" not in ligne
    assert "divorce" not in json.dumps(ligne, ensure_ascii=False)
    techniques = {"request_id", "method", "path", "status", "ms", "cloud_trace", "severity"}
    assert set(ligne) <= set(CHAMPS_DE_LOG) | techniques


def test_le_journal_est_cable_sur_la_sortie_standard(capsys: pytest.CaptureFixture) -> None:
    """`caplog` capte par la racine, donc il ne voit pas si `configurer_journal()` a fait son travail.

    La panne est déjà arrivée une fois pendant la story : uvicorn laisse la racine sans gestionnaire,
    un `INFO` applicatif est alors jeté en silence, et pas une ligne d'AD-10 n'atteint stdout. Ce
    test-ci regarde donc le gestionnaire lui-même, et la sortie réelle.
    """
    journal = logging.getLogger("foyer.request")
    for h in list(journal.handlers):
        journal.removeHandler(h)

    create_app(_settings(env="prod", allow_ungated=True))

    flux = [h for h in journal.handlers
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout]
    assert flux, "aucun gestionnaire vers stdout : la ligne d'AD-10 n'existerait pas sous uvicorn"
    assert journal.getEffectiveLevel() <= logging.INFO
    journal.propagate = False  # sans propagation, seul le gestionnaire installé peut parler
    try:
        RequestIdMiddleware._journaliser({"type": "http", "method": "GET", "path": "/sante",
                                          "headers": [], "state": {"log_fields": {}}},
                                         request_id="r-2", statut=200, ms=3, cloud_trace_max=128)
    finally:
        journal.propagate = True
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["request_id"] == "r-2"


def test_le_cloud_trace_recu_est_borne_par_le_seuil_de_config(caplog: pytest.LogCaptureFixture) -> None:
    """Valeur cliente : sans borne, n'importe qui ferait grossir le journal à notre place.

    La borne est un seuil de `config.py` (revue Codex 1.6, M2), pas une constante du module : le test
    la prend d'un `Settings` réglé à une autre valeur que le défaut, ce qui échouerait si le
    middleware la recodait en dur.
    """
    entete = [(b"x-cloud-trace-context", b"z" * 4000)]
    reglages = _settings(cloud_trace_max_chars=37)
    middleware = RequestIdMiddleware(None, settings=reglages)
    assert middleware.cloud_trace_max == 37
    with caplog.at_level(logging.INFO, logger="foyer.request"):
        RequestIdMiddleware._journaliser({"type": "http", "method": "GET", "path": "/sante",
                                          "headers": entete, "state": {"log_fields": {}}},
                                         request_id="r-3", statut=200, ms=3,
                                         cloud_trace_max=middleware.cloud_trace_max)
    assert len(json.loads(caplog.records[-1].message)["cloud_trace"]) == 37


def test_la_ligne_de_log_porte_une_severite_lisible_par_cloud_logging(
        caplog: pytest.LogCaptureFixture) -> None:
    scope = {"type": "http", "method": "GET", "path": "/sante", "headers": [],
             "state": {"log_fields": {}}}
    with caplog.at_level(logging.INFO, logger="foyer.request"):
        for statut in (200, 400, 503):
            RequestIdMiddleware._journaliser(scope, request_id="r", statut=statut, ms=1,
                                             cloud_trace_max=128)
    severites = [json.loads(r.message)["severity"] for r in caplog.records[-3:]]
    assert severites == ["INFO", "WARNING", "ERROR"]


def test_lerreur_est_loguee_avec_son_code(prod: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    _brancher(prod, Double(erreur=BudgetExceeded("plafond")))
    with caplog.at_level(logging.INFO, logger="foyer.request"):
        prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)
    ligne = json.loads([r for r in caplog.records if r.name == "foyer.request"][0].message)
    assert ligne["error_code"] == "budget_exceeded" and ligne["status"] == 503


# --- digests du démarrage (reprise 1.6) ----------------------------------

def test_les_digests_viennent_du_demarrage_et_pas_du_repli_du_pipeline(prod: TestClient) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))

    prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)

    appel = double.appels[0]
    assert appel["pipeline_digest_hex"] == pipeline_digest() != ""
    assert appel["prompts_digest_hex"] == prompts_digest() != ""
    etat = prod.app.state.foyer
    assert (appel["pipeline_digest_hex"], appel["prompts_digest_hex"]) == (etat.pipeline_digest_hex,
                                                                          etat.prompts_digest_hex)


async def test_le_repli_memoise_du_pipeline_nest_jamais_atteint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reprise 1.6 : les digests passés par l'API rendent le repli `commun.digests()` inutile.

    Le repli relit toute l'arborescence du code à chaque fois qu'il est reconstruit ; surtout, une
    image dont le code changerait à chaud servirait des empreintes calculées sur *autre chose* que
    ce qui a démarré, sans que rien ne le dise. On le rend donc explosif et on vérifie que la trace
    porte quand même les deux valeurs reçues.
    """
    def interdit() -> tuple[str, str]:
        raise AssertionError("le repli mémoïsé des digests ne doit jamais être atteint")

    monkeypatch.setattr(guide, "digests", interdit)
    corpus, index = _mini_corpus()
    # Deadline déjà épuisée : le pipeline lève avant tout appel modèle, et assemble sa trace partielle.
    budget = RequestBudget(deadline_s=0.0, max_attempts=6, max_cost_eur=0.10)

    with pytest.raises(Timeout) as exc:
        await guide.repondre_guide("q", [], Profil(), corpus=corpus, index=index, client=None,
                                   settings=_settings(guide_doc_id=DOC_ID), request_id="r-1",
                                   budget=budget, pipeline_digest_hex="digest-pipeline",
                                   prompts_digest_hex="digest-prompts")

    assert exc.value.trace is not None
    assert exc.value.trace.pipeline_digest == "digest-pipeline"
    assert exc.value.trace.prompts_digest == "digest-prompts"


def test_un_budget_neuf_est_cree_par_requete(prod: TestClient) -> None:
    double = _brancher(prod, Double((_refus("zero_hit"), _trace())))
    for _ in range(2):
        prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)
    budgets = [a["budget"] for a in double.appels]
    assert budgets[0] is not budgets[1]
    assert budgets[0].deadline_s == prod.app.state.foyer.settings.deadline_s


# --- `comparateur` (règle du site) ---------------------------------------

@pytest.mark.parametrize("question", ["Mon contrat d'assurance couvre-t-il le dégât des eaux ?",
                                      "Quelle franchise en cas de sinistre ?", "Suis-je couvert ?",
                                      "On m'a volé mon vélo", "Comment être indemnisé ?"])
def test_comparateur_vrai_sur_les_questions_de_contrat(question: str) -> None:
    assert comparateur_de(question) is True


@pytest.mark.parametrize("question", ["Quel délai pour déclarer mon arrivée ?",
                                      "Comment inscrire mon enfant à l'école ?"])
def test_comparateur_faux_ailleurs(question: str) -> None:
    assert comparateur_de(question) is False


def test_le_comparateur_est_publie_dans_la_reponse(prod: TestClient) -> None:
    _brancher(prod, Double((_refus("zero_hit"), _trace())))
    j = prod.post("/api/v1/chat", json={"question": "Mon contrat couvre-t-il le vol ?", "profil": {}},
                  headers=XFF).json()
    assert j["comparateur"] is True


# --- l'état « partiel » traverse l'enveloppe (story 2.3, revue coordonnée A8) ---
def test_une_reponse_partielle_traverse_lenveloppe_avec_ce_qui_lui_manque(prod: TestClient) -> None:
    """AD-11 : le contrat publie `unknown[]` **et** l'`Answer` entier — c'est de là que le front tire
    ses trois états (sûr / partiel / inconnu) et la section « Ce que je ne sais pas ».

    Aucun test d'API ne le vérifiait : tous les cas servis étaient complets, si bien que la moitié
    « partiel » de l'AC n'était couverte qu'en amont de la couche HTTP. Le champ plat et le champ
    structuré doivent dire la même chose — le front lit `j["unknown"]`, le panneau de trace lit
    `j["answer"]["unknown"]`, et deux listes qui divergeraient donneraient deux écrans contradictoires.
    """
    corpus, _ = _mini_corpus()
    manques = ["Il reste 1 sous-question sans réponse dans ce que vous m'avez demandé.",
               "Un passage que je cite renvoie à un autre passage que je n'ai pas pu retrouver."]
    c1 = _claim("c1", "Délai.", _citation(corpus, f"{DOC_ID}:farrivee:2", "huit jours"))
    answer = Answer(found=True, complete=False, texte="A",
                    segments=[AnswerSegment(text="A", kind="factuel", claim_ids=["c1"])],
                    claims=[c1], unknown=manques)
    _brancher(prod, Double((answer, _trace())), mini=True)

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)
    j = r.json()

    assert r.status_code == 200  # AD-11 : toute sortie du pipeline est un 200
    assert j["unknown"] == manques
    assert j["answer"]["complete"] is False and j["answer"]["found"] is True
    assert j["answer"]["unknown"] == manques  # le champ plat et le structuré ne divergent pas
    assert j["sources"], "une réponse partielle reste sourcée : elle est amputée, pas inventée"


# --- story 4.2f : une lecture partielle traverse l'enveloppe en 200 typé ---
def test_une_lecture_partielle_est_publiee_en_200_avec_ses_compteurs(prod: TestClient) -> None:
    """AD-11 : « toute sortie du pipeline est un 200 avec `Answer` complet ». La docstring des routes
    le disait déjà ; sur ce chemin, elle redevient exacte.

    L'AC nomme la sérialisation séparément parce que rien n'a été ajouté aux schémas : `ChatResponse`
    publie `answer` entier, donc le nouveau porteur traverse sans modification de contrat. C'est
    précisément ce que ce test garde — le jour où quelqu'un projetterait `Answer` champ par champ,
    `lecture_partielle` disparaîtrait en silence et le front peindrait `reponse_illisible`.
    """
    manques = ["Je n'ai pas pu lire tout ce qui pouvait concerner votre question."]
    rejetee = RejectedClaim(
        claim_id="c9", text="Une affirmation que la vérification a écartée.",
        quotes=[{"block_id": f"{DOC_ID}:farrivee:2", "quote": "chaîne rendue par le modèle"}],
        status=ClaimStatus(retrouvee=False, pertinente=None, edition="git:test"),
        rejection_kind="non_retrouvee", motif="citation introuvable")
    answer = Answer(found=False, complete=False, texte="Ma lecture s'est arrêtée avant de conclure.",
                    segments=[AnswerSegment(text="Ma lecture s'est arrêtée avant de conclure.",
                                            kind="limite")],
                    rejected_claims=[rejetee],
                    lecture_partielle=LecturePartielle(nodes_read=2, blocks_read=5,
                                                       documents=[DOC_ID]),
                    unknown=manques)
    _brancher(prod, Double((answer, _trace())), mini=True)

    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF)
    j = r.json()

    assert r.status_code == 200
    assert j["answer"]["found"] is False and j["answer"]["complete"] is False
    # Aucune absence affirmée : c'est l'invariant que le 503 protégeait, tenu sans la panne.
    assert j["answer"]["reason"] is None
    assert j["answer"]["lecture_partielle"] == {"nodes_read": 2, "blocks_read": 5,
                                                "documents": [DOC_ID]}
    # `sources[]` n'énumère que `answer.claims` (AD-11) : il n'y en a aucune, et les affirmations
    # écartées voyagent dans `answer`, où le front les lit.
    assert j["sources"] == []
    assert [c["rejection_kind"] for c in j["answer"]["rejected_claims"]] == ["non_retrouvee"]
    assert j["unknown"] == manques and j["answer"]["unknown"] == manques


def test_lenveloppe_dune_reponse_trouvee_ne_porte_aucun_porteur(prod: TestClient) -> None:
    """AD-4 amendé : `found=True` n'a **rien** à porter — ni preuve d'absence, ni lecture partielle.

    Servi, le corps d'une réponse trouvée doit publier les deux champs à `null` : c'est de là que les
    deux fronts tirent leur état, et un `reason` sous un `found=true` leur ferait peindre en même
    temps une réponse « sûre » et la preuve chiffrée d'une absence.
    """
    corpus, _ = _mini_corpus()
    claim = _claim("c1", "Délai.", _citation(corpus, f"{DOC_ID}:farrivee:2", "huit jours"))
    answer = Answer(found=True, complete=True, texte="A",
                    segments=[AnswerSegment(text="A", kind="factuel", claim_ids=["c1"])],
                    claims=[claim])
    _brancher(prod, Double((answer, _trace())), mini=True)

    j = prod.post("/api/v1/chat", json={"question": "q", "profil": {}}, headers=XFF).json()

    assert j["answer"]["found"] is True
    assert j["answer"]["reason"] is None and j["answer"]["lecture_partielle"] is None


def test_la_mutation_dune_reponse_trouvee_portant_une_preuve_natteint_pas_la_serialisation() -> None:
    """Le contre-exemple, opposé au bord du contrat HTTP : il ne doit **jamais** se sérialiser.

    Ni par le domaine (l'`Answer` ne se monte pas), ni par l'enveloppe (`ChatResponse` la valide en
    la montant). C'est cette seconde moitié qui compte ici : `sources[]` n'énumère que
    `answer.claims`, donc rien dans la projection ne remarquerait la contradiction.
    """
    from server.app.api.schemas import ChatResponse

    corpus, _ = _mini_corpus()
    claim = _claim("c1", "Délai.", _citation(corpus, f"{DOC_ID}:farrivee:2", "huit jours"))
    with pytest.raises(ValidationError, match="found=True n'admet aucun porteur"):
        Answer(found=True, complete=True, texte="A",
               segments=[AnswerSegment(text="A", kind="factuel", claim_ids=["c1"])],
               claims=[claim], reason=AbsenceProof(kind="claims_rejetes"))
    # Et le corps équivalent, écrit à la main comme un serveur cassé l'écrirait, ne passe pas non
    # plus l'enveloppe : le front n'a donc aucun corps servable à peindre sur ce chemin.
    corps = {
        "texte": "A",
        "answer": {"found": True, "complete": True,
                   "claims": [claim.model_dump(mode="json")],
                   "reason": {"kind": "claims_rejetes", "terms_searched": [],
                              "variants_count": 0, "blocks_scanned": 0, "documents": []}},
        "trace": {"request_id": "r-x", "pipeline": "guide"},
    }
    with pytest.raises(ValidationError):
        ChatResponse.model_validate(corps)


def test_lenveloppe_refuse_les_deux_porteurs_a_la_fois() -> None:
    """L'invariant d'AD-4 amendé, vérifié au bord du contrat HTTP : rien ne peut publier un refus qui
    dise à la fois « le corpus n'en parle pas » et « je n'ai pas fini de le lire »."""
    with pytest.raises(ValidationError, match="exactement un porteur"):
        Answer(found=False, complete=False, texte="x",
               reason=AbsenceProof(kind="claims_rejetes"),
               lecture_partielle=LecturePartielle(nodes_read=1, blocks_read=1),
               unknown=["il manque des passages"])


def test_le_domaine_refuse_une_reponse_partielle_muette() -> None:
    """L'invariant d'AD-4 tel que la story 2.3 le complète, vérifié au bord du contrat HTTP : rien
    ne peut publier un « partiel » sans dire ce qui manque."""
    with pytest.raises(ValidationError, match="au moins une lacune"):
        Answer(found=True, complete=False, texte="A",
               segments=[AnswerSegment(text="A", kind="factuel", claim_ids=["c1"])],
               claims=[_claim("c1", "Délai.", _citation(_mini_corpus()[0], f"{DOC_ID}:farrivee:2",
                                                        "huit jours"))])


# --- story 2.5 : les résolutions de trace, sur une réponse réellement servie ---

def _script_du_mini_guide() -> list[dict]:
    """Les trois appels de la chaîne, scriptés sur les blocs du mini-corpus.

    Le modèle est **intégralement** scripté (`FakeAnthropic` lève sur tout appel non prévu) : ce qui
    est vérifié en dessous est donc bien le travail du **pipeline**, pas celui d'un double.
    """
    from server.app.llm.models import TIERS
    from tests.llm_fake import fake_message

    extrait = "huit jours après votre arrivée pour vous déclarer"
    return [
        fake_message(model=TIERS["micro"], text=json.dumps({
            "intent": "question", "question_resolue": "Quel délai après mon arrivée ?",
            "clarification": None, "language": "fr", "terms": ["arrivée"], "themes": [],
            "facettes": ["délai de déclaration"], "bien": None, "evenement": None, "lieu": None,
            "cause": None, "moment": None})),
        fake_message(model=TIERS["reason"], text=json.dumps({
            "segments": [{"text": "Le délai est de huit jours.", "kind": "factuel",
                          "claim_ids": ["c1"]}],
            "claims": [{"claim_id": "c1", "text": "Le délai est de huit jours.",
                        "quotes": [{"block_id": f"{DOC_ID}:farrivee:2", "quote": extrait}]}]})),
        fake_message(model=TIERS["micro"], text=json.dumps({
            "verdicts": [{"claim_id": "c1", "pertinente": True}],
            "facettes": [{"facette": 0, "claim_ids": ["c1"]}],
            "segments": [{"segment": i, "soutenu": True} for i in range(4)]})),
    ]


def _pipeline_reel(script: list[dict], *, fake: Any = None) -> Any:
    """Le **vrai** `repondre_guide`, avec un modèle scripté à la place du réseau.

    La route ne passe pas de `doc_id` (elle laisse le pipeline prendre `settings.guide_doc_id`, qui
    vaut `lux-guide` dans l'image) : on le lui donne ici, pour interroger le mini-corpus sans toucher
    aux réglages de l'application partagée par le module.
    """
    from server.app.llm.client import LlmClient
    from tests.llm_fake import FakeAnthropic

    anthropic_fake = fake or FakeAnthropic(script)

    async def appeler(question: str, historique: list, profil: Any, **kw: Any):
        settings = kw.pop("settings")
        kw["client"] = LlmClient(settings, anthropic_client=anthropic_fake)
        return await guide.repondre_guide(question, historique, profil, settings=settings,
                                          doc_id=DOC_ID, **kw)

    return appeler


def _pipeline_reel_borne(script: list[dict], **reglages: Any) -> Any:
    """`_pipeline_reel`, avec des seuils resserrés : c'est la borne qui fait la lecture partielle.

    Les réglages passent par `Settings.model_copy` plutôt que par l'application partagée du module :
    le pipeline reste le vrai, la route reste la vraie, et seul le budget de retrieval de **cette**
    requête change.
    """
    from server.app.llm.client import LlmClient
    from tests.llm_fake import FakeAnthropic

    anthropic_fake = FakeAnthropic(script)

    async def appeler(question: str, historique: list, profil: Any, **kw: Any):
        settings = kw.pop("settings").model_copy(update=reglages)
        kw["client"] = LlmClient(settings, anthropic_client=anthropic_fake)
        return await guide.repondre_guide(question, historique, profil, settings=settings,
                                          doc_id=DOC_ID, **kw)

    return appeler


def test_une_lecture_partielle_traverse_le_vrai_pipeline_puis_la_route(prod: TestClient) -> None:
    """Story 4.2f, la surface **composée** : pipeline réel → route → 200 typé.

    La bascule était prouvée à deux endroits disjoints — le pipeline sans HTTP, l'enveloppe avec un
    double à la place du pipeline et une `Answer` écrite à la main. Ni l'un ni l'autre ne dit que les
    deux se rejoignent : c'est **ici** que la borne du retrieval, la vérification qui ne retient
    rien, la fabrique de *restituer* et la projection HTTP se rencontrent sur une seule requête.
    """
    from server.app.llm.models import TIERS
    from tests.llm_fake import fake_message

    corpus, index = _mini_corpus()
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index
    # L'ébauche cite un bloc **réel mais non transmis** : la citation ne peut pas être retenue, et
    # la relance rejoue la même — c'est la configuration exacte de la story.
    ebauche = {"segments": [{"text": "Une affirmation.", "kind": "factuel", "claim_ids": ["c1"]}],
               "claims": [{"claim_id": "c1", "text": "Une affirmation.",
                           "quotes": [{"block_id": f"{DOC_ID}:q1:2", "quote": FAQ_R[:40]}]}]}
    script = [_script_du_mini_guide()[0],
              fake_message(model=TIERS["reason"], text=json.dumps(ebauche)),
              fake_message(model=TIERS["reason"], text=json.dumps(ebauche))]
    etat.pipeline = _pipeline_reel_borne(script, retrieval_max_blocks=1)

    r = prod.post("/api/v1/chat", json={"question": "Quel délai après mon arrivée ?", "profil": {},
                                        "variant": "deterministe"}, headers=XFF)

    assert r.status_code == 200, r.text
    j = r.json()
    assert j["answer"]["found"] is False and j["answer"]["complete"] is False
    assert j["answer"]["reason"] is None  # aucune absence affirmée, et aucune 503
    lue = j["answer"]["lecture_partielle"]
    # Les compteurs viennent du **vrai** retrieval, pas d'un objet écrit à la main : ils doivent
    # retomber sur ce que la trace de la même requête dit avoir ouvert.
    ouverts = [b for s in j["trace"]["steps"] for b in s.get("opened_block_ids", [])]
    assert lue["blocks_read"] == len(set(ouverts)) >= 1
    assert 1 <= lue["nodes_read"] <= lue["blocks_read"]
    assert lue["documents"] == [DOC_ID]
    assert j["trace"]["truncations"] == 1
    assert j["answer"]["rejected_claims"] and j["sources"] == []
    assert j["unknown"] and j["answer"]["unknown"] == j["unknown"]
    assert [s["name"] for s in j["trace"]["steps"]][-1] == "restituer"


@pytest.mark.parametrize("variant", [None, "outils"])
def test_chat_reel_uses_the_four_navigation_tools_for_default_and_explicit_variant(
        prod: TestClient, monkeypatch: pytest.MonkeyPatch, variant: str | None) -> None:
    from server.app.llm.models import TIERS
    from tests.llm_fake import FakeAnthropic, fake_message

    corpus, index = _mini_corpus()
    calls = {name: 0 for name in ("sommaire", "ouvrir_noeud", "chercher", "definitions")}
    for name in calls:
        original = getattr(index, name)

        def record(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(index, name, record)

    tools = fake_message(
        model=TIERS["micro"], stop_reason="tool_use",
        content=[
            {"type": "tool_use", "id": "toolu_summary", "name": "sommaire",
             "input": {"doc_id": DOC_ID}},
            {"type": "tool_use", "id": "toolu_search", "name": "chercher",
             "input": {"termes": ["arrivée"]}},
            {"type": "tool_use", "id": "toolu_open", "name": "ouvrir_noeud",
             "input": {"node_id": f"{DOC_ID}:farrivee",
                       "focus_block_id": f"{DOC_ID}:farrivee:2"}},
            {"type": "tool_use", "id": "toolu_defs", "name": "definitions",
             "input": {"termes": ["arrivée"],
                       "blocs_ouverts": [f"{DOC_ID}:farrivee:2"]}},
        ])
    script = _script_du_mini_guide()
    script.insert(1, tools)
    fake = FakeAnthropic(script)
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index
    etat.pipeline = _pipeline_reel([], fake=fake)
    body: dict[str, Any] = {"question": "Quel délai après mon arrivée ?", "profil": {}}
    if variant is not None:
        body["variant"] = variant

    response = prod.post("/api/v1/chat", json=body, headers=XFF)

    assert response.status_code == 200 and response.json()["sources"]
    assert response.json()["trace"]["variant"] == "outils"
    # `sommaire` est lu une première fois pour le prompt puis une seconde par l'outil lui-même ;
    # les trois autres compteurs ne peuvent provenir que de l'exécution des tool_use scriptés.
    assert calls["sommaire"] >= 2
    assert all(calls[name] > 0 for name in ("ouvrir_noeud", "chercher", "definitions"))
    assert [tool["name"] for tool in fake.requests[1]["tools"]] == list(calls)


def test_une_reponse_servie_porte_ses_blocs_resolus_et_le_profil_de_son_gate(
        prod: TestClient) -> None:
    """AC 2.5, sur le corps que **FastAPI écrit**, au bout du **vrai** pipeline.

    Les tests de `tests/test_pipeline_guide.py` vérifient que le pipeline résout ; celui de
    `test_reponse_sourcee_rend_200_avec_le_contrat_ad11` vérifie qu'une trace donnée traverse
    l'enveloppe. Ni l'un ni l'autre ne dit que les deux se rejoignent : c'est **ici** que le
    `block_id` qu'une étape a réellement ouvert ressort du serveur avec le titre de sa fiche.

    Et le contrat de premier niveau n'acquiert rien : tout vit dans `trace` (Design Notes 2.5), ce qui
    est exactement ce qui rend les deux lots de la story indépendants.
    """
    corpus, index = _mini_corpus()
    _avec_gate(corpus, DOC_ID, "vertical", cases=2, countersigned=False)
    # Une vraie alerte du corpus chargé doit traverser le pipeline puis le contrat HTTP avec le
    # gate. Un objet de trace construit à la main ne prouverait pas cette jonction.
    corpus.alerts[DOC_ID] = ["source_absente"]
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index
    etat.pipeline = _pipeline_reel(_script_du_mini_guide())

    r = prod.post("/api/v1/chat", json={"question": "Quel délai après mon arrivée ?",
                                        "profil": {}, "historique": [],
                                        "variant": "deterministe"}, headers=XFF)

    assert r.status_code == 200
    trace = r.json()["trace"]
    # Résolus : l'identifiant **et** le titre de la fiche, avec le `fiche_id` que `sources[]` publie.
    par_id = {b["block_id"]: b for b in trace["blocs"]}
    assert par_id[f"{DOC_ID}:farrivee:2"] == {
        "block_id": f"{DOC_ID}:farrivee:2", "doc_id": DOC_ID, "node_id": f"{DOC_ID}:farrivee",
        "fiche_id": "arrivee", "titre": "Déclarer son arrivée"}
    assert par_id[f"{DOC_ID}:farrivee:2"]["fiche_id"] == r.json()["sources"][0]["fiche_id"]
    assert par_id[f"{DOC_ID}:farrivee:2"]["titre"] == r.json()["sources"][0]["titre"]
    # Ce que la trace nomme est **ce qui a été ouvert**, pas une liste écrite à la main.
    ouverts = [b for s in trace["steps"] for b in s["opened_block_ids"]]
    assert f"{DOC_ID}:farrivee:2" in ouverts
    assert set(par_id) >= set(ouverts)

    assert trace["gate"] == {"profile": "vertical", "cases": 2, "countersigned": False,
                             "alerts": ["source_absente"]}
    # Le dictionnaire de l'image est chargé au démarrage : le pipeline du guide en publie l'état, et
    # le refus « zéro hit » y est désarmé tant qu'aucune main n'a signé (AD-5, M12).
    assert trace["dictionnaire"]["court_circuit_actif"] is False

    # AD-11 : `ChatResponse` n'acquiert aucun champ — tout est dans `trace`.
    assert {"blocs", "gate", "dictionnaire"}.isdisjoint(r.json())


async def test_une_trace_partielle_derreur_garde_le_gate_de_son_document() -> None:
    """M10 : « une enveloppe d'erreur portant `trace` » porte le panneau comme une réponse.

    AD-16 : « 503 avec trace partielle » — et une trace partielle qui perdrait ce qu'elle sait du
    document serait précisément inutile là où on en a le plus besoin. Rien n'a tourné ici (deadline
    épuisée avant *comprendre*) : `blocs` est **vide**, ce qui est un fait, et le gate est là, parce
    qu'il ne dépend d'aucune étape.
    """
    corpus, index = _mini_corpus()
    _avec_gate(corpus, DOC_ID, "vertical", cases=2, countersigned=False)
    budget = RequestBudget(deadline_s=0.0, max_attempts=6, max_cost_eur=0.10)

    with pytest.raises(Timeout) as exc:
        await guide.repondre_guide("q", [], Profil(), corpus=corpus, index=index, client=None,
                                   settings=_settings(guide_doc_id=DOC_ID), request_id="r-1",
                                   budget=budget, pipeline_digest_hex="p", prompts_digest_hex="q")

    trace = exc.value.trace
    assert trace is not None
    assert trace.gate is not None and trace.gate.profile == "vertical"
    assert trace.blocs == []  # aucune étape n'a ouvert de bloc : vide, jamais deviné (AD-16)
    assert trace.dictionnaire is None  # aucun dictionnaire ne lui a été passé


# --- story 4.5 : les trois réserves et la route des résultats -----------------

def test_sante_publie_la_troisieme_reserve_a_cote_des_deux_autres(prod: TestClient) -> None:
    """AC 4.5 : « les trois sont lisibles dans `/sante` ».

    `gate_countersigned` (la relecture humaine), `gate_validated_by_expert` (AD-14 : jamais vraie)
    et `dictionary.validated` (AD-5). Les trois disent ce que le service **ne** garantit pas ; les
    taire serait exactement l'invention qu'AD-16 interdit, et les rendre bloquantes ferait refuser
    de servir pour une signature manquante.
    """
    j = prod.get("/api/v1/sante", headers=XFF).json()
    assert j["gate_countersigned"] is False
    assert j["gate_validated_by_expert"] is False
    assert j["dictionary"]["validated"] is False
    # Strictement adossé à `gate_profile`, comme `gate_cases` et `gate_countersigned`.
    assert (j["gate_profile"] is None) == (j["gate_validated_by_expert"] is None)


def test_gate_validated_by_expert_est_nul_sans_profil_et_jamais_vrai(prod: TestClient) -> None:
    """AD-14 : rien dans ce dépôt ne peut établir une validation par un expert assurance."""
    etat = prod.app.state.foyer
    corpus, index = _mini_corpus()
    etat.corpus, etat.index = corpus, index
    # Aucun gate : le profil est nul, la réserve aussi — il n'y a aucun verdict à qualifier.
    assert etat.gate_profile is None and etat.gate_validated_by_expert is None
    _avec_gate(corpus, DOC_ID, "vertical")
    assert etat.gate_validated_by_expert is False


def test_la_route_des_resultats_publie_un_etat_type_sans_run(prod: TestClient) -> None:
    """FR41 : `publie: false` est un état **typé**, jamais un 5xx et jamais un chiffre inventé."""
    r = prod.get("/api/v1/evals/latest", headers=XFF)
    assert r.status_code == 200
    corps = r.json()
    assert set(corps) == {"publie", "raison", "publication"}
    assert corps["publie"] is False and corps["publication"] is None
    assert corps["raison"] in ("absent", "illisible", "hors_schema")


def test_la_route_des_resultats_ne_lit_pas_data_par_requete(prod: TestClient) -> None:
    """AD-7 : chargé une fois au démarrage, comme `report.json` et `dictionary.json`.

    Le contrôle est direct : la réponse suit l'état du process, pas le disque. On remplace l'état
    en mémoire et la route change ; le fichier, lui, n'a pas bougé.
    """
    from server.app.domain.evals import EtatPublication

    etat = prod.app.state.foyer
    avant = etat.publication_evals
    etat.publication_evals = EtatPublication(publie=False, raison="illisible")
    try:
        assert prod.get("/api/v1/evals/latest", headers=XFF).json()["raison"] == "illisible"
    finally:
        etat.publication_evals = avant
