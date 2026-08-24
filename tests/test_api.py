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
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from server.app.api import etat as etat_module
from server.app.api.errors import MESSAGE_INTERNE, gestionnaire_http, gestionnaire_pipeline
from server.app.api.main import create_app
from server.app.api.request_id import CHAMPS_DE_LOG, RequestIdMiddleware
from server.app.api.routes.chat import comparateur_de
from server.app.config import REPO_ROOT, Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.answer import (
    AbsenceProof,
    Answer,
    AnswerSegment,
    ClaimStatus,
    Quote,
    RejectedClaim,
    VerifiedClaim,
    VerifiedQuote,
)
from server.app.domain.document import Document, Node, Source
from server.app.domain.errors import BudgetExceeded, CorpusUnavailable, LlmUnavailable, Timeout
from server.app.domain.ingest import Gate, ManifestEntry
from server.app.domain.profil import Profil
from server.app.domain.trace import StepTrace, Trace
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
        client.origine = (etat.corpus, etat.index, etat.pipeline)  # type: ignore[attr-defined]
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
        etat.corpus, etat.index, etat.pipeline = client.origine
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
    double = _brancher(prod, Double((answer, _trace())), mini=True)

    r = prod.post("/api/v1/chat", json={"question": "Quel délai après mon arrivée ?", "profil": {},
                                        "historique": []}, headers=XFF)

    assert r.status_code == 200
    j = r.json()
    assert j["via"] == "api/v1"
    assert j["texte"] == answer.texte
    assert j["trace"]["request_id"] == r.headers["X-Request-Id"]
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
    answer = Answer(found=True, complete=False, texte="A B C",
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
    answer = Answer(found=True, complete=False, texte="A B",
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
    answer = Answer(found=True, complete=False, texte="Délai.",
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
    answer = Answer(found=True, complete=False, texte="Délai.",
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
    answer = Answer(found=True, complete=False, texte="Délai.",
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
    answer = Answer(found=True, complete=False, texte="Délai.",
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
    answer = Answer(found=True, complete=False, texte="Délai.",
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
    answer = Answer(found=True, complete=False, texte="Délai.",
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
    answer = Answer(found=True, complete=False, texte="Délai.",
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
    _brancher(prod, Double((_refus("zero_hit"), _trace())))
    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}, "variant": "outils"}, headers=XFF)
    assert r.status_code == 400
    r = prod.post("/api/v1/chat", json={"question": "q", "profil": {}, "variant": "deterministe"},
                  headers=XFF)
    assert r.status_code == 200


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
    assert j["documents_servis"] == ["axa-lu-optihome-2017", "lux-guide"]
    # Aucun gate n'est encore écrit (story 1.10) : annoncer un profil serait une bascule silencieuse.
    assert j["gate_profile"] is None
    assert "sans_gate" in [a["alerte"] for a in j["alerts"]]
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


def _avec_gate(corpus: Corpus, doc_id: str, profil: str, *, coherent: bool = True) -> None:
    """Pose un gate sur une entrée du manifest. `coherent=False` = un gate que le loader neutralise."""
    entree = corpus.manifest[doc_id]
    entree.gate = Gate(
        profile=profil, evals_ok=True, cases_hash="c", date="2026-08-24",
        source_hash=entree.source_hash if coherent else "autre",
        ingest_fingerprint=entree.ingest_fingerprint, overlay_hash=entree.overlay_hash,
        pipeline_digest="p", prompts_digest="q", model_ids={})


def test_sante_publie_le_profil_quand_tous_les_documents_servis_en_ont_un(prod: TestClient) -> None:
    corpus, index = _mini_corpus()
    _avec_gate(corpus, DOC_ID, "vertical")
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index

    assert prod.get("/api/v1/sante", headers=XFF).json()["gate_profile"] == "vertical"


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


@pytest.mark.parametrize("contenu, attendu", [
    (b'{"validated": true}', True),
    (b'{"validated": false}', False),
    (b'{}', False),
    (b'pas du json', False),
    # Revue Codex 1.6, M1 : `bool("false")` vaut `True`. Un `validated` rendu en **chaîne** par le
    # générateur du dictionnaire (story 2.1) aurait fait annoncer à `/sante` un dictionnaire validé
    # et ré-armé le court-circuit « zéro hit » qu'AD-5 tient désactivé sans signature humaine.
    (b'{"validated": "false"}', False),
    (b'{"validated": "true"}', False),
    (b'{"validated": 1}', False),
    (b'[]', False),
])
def test_letat_du_dictionnaire_se_lit_ou_reste_faux(tmp_path: Any, contenu: bytes, attendu: bool) -> None:
    # AD-5 : un dictionnaire absent ou illisible désactive une optimisation, il n'empêche pas de servir.
    (tmp_path / "dictionary.json").write_bytes(contenu)
    assert etat_module._dictionnaire_valide(tmp_path) is attendu


def test_letat_du_dictionnaire_est_faux_quand_le_fichier_manque(tmp_path: Any) -> None:
    assert etat_module._dictionnaire_valide(tmp_path) is False


def test_lalias_sante_rend_exactement_la_meme_chose(prod: TestClient) -> None:
    assert prod.get("/sante", headers=XFF).json() == prod.get("/api/v1/sante", headers=XFF).json()


def test_lalias_chat_rend_exactement_le_meme_contrat(prod: TestClient) -> None:
    _brancher(prod, Double((_refus("zero_hit"), _trace())))
    corps = {"question": "q", "profil": {}}
    a = prod.post("/chat", json=corps, headers=XFF).json()
    b = prod.post("/api/v1/chat", json=corps, headers=XFF).json()
    a["trace"]["request_id"] = b["trace"]["request_id"] = "-"  # seul l'identifiant diffère
    assert a == b


def test_allow_ungated_de_limage_se_retire_des_quun_gate_existe() -> None:
    """Garde-fou de la ligne `ENV ALLOW_UNGATED=true` du `Dockerfile` (AD-7).

    Elle n'y est que parce qu'aucun document n'a encore de gate : sans elle, l'image de production
    mettrait les deux documents en quarantaine et répondrait 503 à toute question. Le commentaire du
    `Dockerfile` dit qu'elle se retire à la fin de la story 1.10 — un commentaire ne garantit rien.
    Ce test échoue le jour où le premier gate est écrit : c'est le rappel, en dur.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    if "ALLOW_UNGATED=true" not in dockerfile:
        pytest.skip("la ligne a été retirée : le garde-fou n'a plus d'objet")
    manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_bytes())
    gates = [doc_id for doc_id, e in manifest.items() if e.get("gate") is not None]
    assert not gates, (
        f"un gate existe désormais ({gates}) : retirer `ENV ALLOW_UNGATED=true` du Dockerfile, "
        "sinon l'image continuerait de servir des documents non validés avec une simple alerte")


# --- pages statiques (AD-12) ---------------------------------------------

@pytest.mark.parametrize("chemin", ["/", "/guide/", "/guide/app/kb.js", "/guide/app/styles.css",
                                    "/sinistre/"])
def test_les_pages_sont_servies_sur_la_meme_origine(prod: TestClient, chemin: str) -> None:
    assert prod.get(chemin).status_code == 200


def test_les_fichiers_du_site_ne_sont_pas_renommes(prod: TestClient) -> None:
    # `web/` a des chemins relatifs (`app/kb.js`) : un renommage casserait le site sans bruit.
    assert "window.KB" in prod.get("/guide/app/kb.js").text


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
