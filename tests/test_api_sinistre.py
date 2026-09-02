"""Matrice d'E/S HTTP de l'outil sinistre (spec 1.9) : AD-11, AD-16, AD-13, AD-10, AD-7, AD-8.

Le pipeline est remplacé par un **double** (`etat.pipeline_sinistre`) : ces tests portent sur les
trois routes, pas sur les cinq étapes, qui ont les leurs (`tests/test_pipeline_sinistre.py`). Aucun
réseau, aucune clé — le client Claude est construit mais jamais appelé.

Le corpus est **réel** (`data/`, chargé par le lifespan) pour `/documents` et `/report` — c'est le
seul endroit où l'AC parle du contrat AXA nommément ; un mini-contrat le remplace dès qu'un test
doit exhiber une clause aux offsets, à la page et à la bbox connus.
"""

from __future__ import annotations

import pathlib

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.app.api.errors import MESSAGE_INTERNE
from server.app.api.main import create_app
from server.app.api.schemas import SinistreRequest
from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
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
from server.app.domain.document import Document, Node
from server.app.domain.errors import (
    BudgetExceeded,
    CorpusUnavailable,
    LlmUnavailable,
    Timeout,
)
from server.app.domain.ingest import ManifestEntry
from server.app.domain.question import Faits, QuestionScope
from server.app.domain.trace import StepTrace, Trace
from server.app.domain.verdict import MissingPackage, Verdict
from server.app.pipelines import sinistre

XFF = {"X-Forwarded-For": "198.51.100.9"}
DOC_ID = "cg-mini"
GUIDE_ID = "guide-mini"
AXA = "axa-lu-optihome-2017"
BALOISE = "baloise-lu-home-2-2024"

GARANTIE = ("Les dégâts occasionnés au mobilier assuré par un événement soudain, résultant de "
            "l'action subite de la chaleur, sont couverts.")
EXCLUSION = "Sont exclus les dommages causés intentionnellement par l'assuré."
Q_GARANTIE = "événement soudain, résultant de l'action subite de la chaleur"

QUESTION = "Ce sinistre est-il couvert par les conditions générales du contrat ?"
FAITS = {
    "date": "2026-08-01", "lieu": "salon du domicile assuré", "montant_eur": 1200.0,
    "description": "Une bougie allumée posée sur une table basse est tombée sur le canapé.",
}


# --- fabrique du corpus miniature ----------------------------------------

def _mini_corpus() -> tuple[Corpus, Index]:
    """Un contrat (avec pages, bbox et lignes) **et** un guide : `kind` décide de ce que la route accepte."""
    blocs = [
        {"block_id": f"{DOC_ID}:p9:1", "loc": "p9", "seq": 1, "kind": "heading", "page": 9,
         "text": "Incendie et action de la chaleur"},
        {"block_id": f"{DOC_ID}:p9:2", "loc": "p9", "seq": 2, "kind": "garantie", "page": 9,
         "text": GARANTIE, "kind_source": "manual", "bbox": [70.0, 120.5, 520.0, 160.0],
         "lines": [{"line_id": "p9:2:l1", "text": GARANTIE, "bbox": [70.0, 120.5, 520.0, 140.0]}]},
        {"block_id": f"{DOC_ID}:p46:1", "loc": "p46", "seq": 1, "kind": "exclusion", "page": 46,
         "text": EXCLUSION, "kind_confidence": 0.4},  # sans `kind_source` : typage **non** confirmé
    ]
    contrat = Document(
        doc_id=DOC_ID, kind="contrat", title="Mini conditions générales", edition="juin 2017",
        source_url="https://example.invalid/cg.pdf",
        nodes=[Node(node_id=f"{DOC_ID}:socle", level=1, title="Socle",
                    items=[{"block_id": f"{DOC_ID}:p9:1"}, {"block_id": f"{DOC_ID}:p9:2"}]),
               Node(node_id=f"{DOC_ID}:ext", level=1, title="Extensions", scope={"kind": "extension"},
                    items=[{"block_id": f"{DOC_ID}:p46:1"}]),
               Node(node_id=f"{DOC_ID}:root", level=0, title="Contrat",
                    items=[{"node_id": f"{DOC_ID}:socle"}, {"node_id": f"{DOC_ID}:ext"}])],
        blocks=blocs)
    guide = Document(
        doc_id=GUIDE_ID, kind="guide", title="Mini guide", edition="git:test",
        nodes=[Node(node_id=f"{GUIDE_ID}:f1", level=2, title="Arrivée",
                    items=[{"block_id": f"{GUIDE_ID}:f1:1"}])],
        blocks=[{"block_id": f"{GUIDE_ID}:f1:1", "loc": "f1", "seq": 1, "kind": "para",
                 "text": "Vous disposez de huit jours pour vous déclarer à la commune."}])
    for doc in (contrat, guide):
        for b in doc.blocks:
            b.text_norm = normalize(b.text)
    manifest = {
        DOC_ID: ManifestEntry(status="servi", source_hash="s1", ingest_fingerprint="f1",
                              document_hash="d1", edition="juin 2017"),
        GUIDE_ID: ManifestEntry(status="servi", source_hash="s2", ingest_fingerprint="f2",
                                document_hash="d2", edition="git:test"),
    }
    corpus = Corpus(documents={DOC_ID: contrat, GUIDE_ID: guide}, manifest=manifest,
                    summaries={DOC_ID: "# Mini CG", GUIDE_ID: "# Mini guide"})
    return corpus, Index(corpus)


def _citation(corpus: Corpus, block_id: str, extrait: str, *, line_ids: list[str] | None = None) -> VerifiedQuote:
    """Une citation **relue du corpus** : `quote` est exactement `Block.text[text_start:text_end]`."""
    texte = corpus.documents[DOC_ID].block(block_id).text
    debut = texte.index(extrait)
    return VerifiedQuote(block_id=block_id, quote=extrait, start=0, end=len(extrait),
                         text_start=debut, text_end=debut + len(extrait),
                         line_ids=line_ids or [])


def _verdict(value: str = "sous_conditions", **kw: Any) -> Verdict:
    defauts: dict[str, Any] = {
        "reason": "Une clause conditionne la garantie (au regard des conditions générales seules)",
        "missing": MissingPackage(faits=["caractère subit de l'action de la chaleur"]),
        "ask_client": ["Le contrat porte-t-il l'option correspondante ?",
                       "Le caractère subit de l'action de la chaleur est-il établi ?"],
        "escalate": ["Faire relire la clause par un gestionnaire."],
    }
    return Verdict(value=value, **{**defauts, **kw})


def _reponse(corpus: Corpus) -> Answer:
    """Le cas nominal : une affirmation, une clause citée, un verdict conservateur."""
    quote = _citation(corpus, f"{DOC_ID}:p9:2", Q_GARANTIE, line_ids=["p9:2:l1"])
    claim = VerifiedClaim(
        claim_id="c1", text="La garantie vise l'action subite de la chaleur.", quotes=[quote],
        line_ids=["p9:2:l1"],
        status=ClaimStatus(retrouvee=True, pertinente=True, applicable="humain", edition="juin 2017"))
    segment = AnswerSegment(text="La garantie vise l'action subite de la chaleur.", kind="factuel",
                            claim_ids=["c1"])
    return Answer(
        found=True, complete=False, lang="fr", texte=segment.text, segments=[segment],
        claims=[claim],
        rejected_claims=[RejectedClaim(
            claim_id="c2", text="Une exclusion écarte les dommages du canapé.",
            quotes=[Quote(block_id=f"{DOC_ID}:p46:1", quote="phrase que le modèle a inventée")],
            status=ClaimStatus(retrouvee=False, edition="juin 2017"),
            rejection_kind="non_retrouvee", motif="citation introuvable")],
        verdict=_verdict(),
        faits_compris=QuestionScope(bien="mobilier de salon", evenement="brûlure sans embrasement",
                                    lieu="domicile", cause="bougie", moment="2026-08-01"),
        unknown=["La franchise applicable n'est pas dite."])


def _refus(kind: str = "zero_hit") -> Answer:
    """AD-16 : un refus sinistre **porte** un verdict `ne_tranche_pas`, jamais rien."""
    phrase = "Je n'ai trouvé aucune clause du contrat qui traite du sinistre décrit."
    return Answer(found=False, complete=False, texte=phrase,
                  segments=[AnswerSegment(text=phrase, kind="limite")],
                  reason=AbsenceProof(kind=kind, terms_searched=["mobilier"], variants_count=0,
                                      blocks_scanned=3, documents=[DOC_ID]),
                  verdict=_verdict("ne_tranche_pas", ask_client=[], escalate=[]),
                  faits_compris=QuestionScope(bien="mobilier de salon"))


def _trace(**kw: Any) -> Trace:
    defauts: dict[str, Any] = {
        # La variante que la route peut réellement produire : elle ne transporte pas `variant`, donc
        # c'est le défaut du pipeline (story 4.2d), lu sur lui et jamais recopié ici.
        "request_id": "à-remplacer", "pipeline": "sinistre", "variant": sinistre.VARIANT,
        "intent": "question", "total_cost_eur": 0.0336,
        "steps": [StepTrace(name="comprendre", tier="micro"), StepTrace(name="restituer")],
    }
    return Trace(**{**defauts, **kw})


class Double:
    """Double de `pipelines.sinistre.run` : enregistre ce qu'il reçoit, rend ou lève ce qu'on lui donne."""

    def __init__(self, resultat: tuple[Answer, Trace] | None = None,
                 erreur: BaseException | None = None) -> None:
        self.resultat = resultat
        self.erreur = erreur
        self.appels: list[dict[str, Any]] = []

    async def __call__(self, doc_id: str, question: str, faits: Any, **kw: Any) -> tuple[Answer, Trace]:
        self.appels.append({"doc_id": doc_id, "question": question, "faits": faits, **kw})
        if self.erreur is not None:
            raise self.erreur
        assert self.resultat is not None
        answer, trace = self.resultat
        return answer, trace.model_copy(update={"request_id": kw["request_id"]})


# --- fixtures -------------------------------------------------------------

def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _serveur(settings: Settings) -> Any:
    app = create_app(settings)
    with TestClient(app) as client:
        etat = app.state.foyer
        client.origine = {  # type: ignore[attr-defined]
            nom: getattr(etat, nom)
            for nom in (
                "settings", "corpus", "index", "pipeline_sinistre", "reports", "report_errors",
                "source_urls", "alerts", "dictionnaire", "dictionnaires",
            )
        }
        client.origine["dictionnaires"] = dict(etat.dictionnaires)  # type: ignore[attr-defined]
        yield client


@pytest.fixture(scope="module")
def prod() -> Any:
    # `env="dev"` : ce module sert un corpus doublé **sans gate**, et depuis la revue Codex 1.10 (B3)
    # la dérogation est refusée en `prod` — un document sans gate y part en quarantaine. Ce que ces
    # tests vérifient (routes, sources, rapports) ne dépend pas de `env` ; ce qui en dépend est
    # asserté dans `tests/test_api.py`.
    yield from _serveur(_settings(env="dev", allow_ungated=True))


@pytest.fixture(scope="module")
def production_reelle() -> Any:
    """Le vrai manifest sous la règle production : aucun passe-droit pour un gate absent."""
    yield from _serveur(_settings(env="prod", allow_ungated=False))


@pytest.fixture(autouse=True)
def _etat_propre(request: pytest.FixtureRequest) -> Any:
    """L'application est partagée par le module ; son état, non — chaque test repart du réel."""
    yield
    client = request.node.funcargs.get("prod")
    if client is not None:
        etat = client.app.state.foyer
        for nom, valeur in client.origine.items():
            setattr(etat, nom, valeur)
        etat.limiter.reset()


def _brancher(client: TestClient, double: Double, *, mini: bool = True) -> Double:
    etat = client.app.state.foyer
    etat.pipeline_sinistre = double
    if mini:
        etat.corpus, etat.index = _mini_corpus()
    return double


def _corps(**kw: Any) -> dict[str, Any]:
    return {"doc_id": DOC_ID, "question": QUESTION, "faits": dict(FAITS), **kw}


def _poster(client: TestClient, corps: dict[str, Any] | None = None, **kw: Any) -> Any:
    return client.post("/api/v1/sinistre", json=corps if corps is not None else _corps(),
                       headers=XFF, **kw)


# --- ligne « Liste des contrats » ----------------------------------------

def test_documents_liste_le_corpus_reel_avec_edition_et_source(prod: TestClient) -> None:
    """AC : le contrat AXA est listé `kind="contrat"`, `status="servi"`, édition « juin 2017 », source.

    Le guide y figure **aussi** : il est servi, et taire un document servi ferait de cette route une
    vue partielle de ce que le service expose. C'est la page qui n'en propose que les contrats.
    """
    r = prod.get("/api/v1/documents", headers=XFF)
    assert r.status_code == 200
    items = r.json()
    assert [d["doc_id"] for d in items] == sorted(d["doc_id"] for d in items)  # triés par doc_id
    par_id = {d["doc_id"]: d for d in items}
    axa = par_id[AXA]
    assert axa["kind"] == "contrat"
    assert axa["status"] == "servi"
    assert axa["selectionnable"] is True
    assert axa["edition"] == "juin 2017"
    entree = prod.app.state.foyer.corpus.manifest[AXA]
    assert axa["source_hash"] == entree.source_hash
    assert axa["ingest_fingerprint"] == entree.ingest_fingerprint
    assert axa["document_hash"] == entree.document_hash
    assert axa["overlay_hash"] == entree.overlay_hash
    assert entree.gate is not None
    assert axa["gate"] == entree.gate.model_dump(mode="json")
    assert axa["report_status"] == "disponible"
    # AD-7 : le PDF n'est pas redistribué ; sa source publique est la seule chose qu'on puisse offrir,
    # et c'est ce qui rend « édition juin 2017 » vérifiable par celui à qui on l'annonce.
    assert axa["source_url"] and axa["source_url"].startswith("https://")
    assert "lux-guide" in par_id and par_id["lux-guide"]["kind"] == "guide"
    baloise = par_id["baloise-lu-home-2-2024"]
    # Le gate final rend ce statut indépendant de la dérogation de la fixture.
    assert baloise["status"] == "servi" and baloise["selectionnable"] is True
    assert baloise["edition"] == "CG-HOME(2)-LUFR-09-24"
    assert baloise["source_url"].startswith("https://www.baloise.lu/")
    assert baloise["report_status"] == "disponible"


def test_baloise_gate_est_servi_et_selectionnable_en_production(
        production_reelle: TestClient) -> None:
    reponse = production_reelle.get("/api/v1/documents", headers=XFF)
    assert reponse.status_code == 200
    baloise = {item["doc_id"]: item for item in reponse.json()}[BALOISE]
    assert baloise["status"] == "servi"
    assert baloise["raison"] is None
    assert baloise["selectionnable"] is True
    assert baloise["gate"]["profile"] == "vertical"
    assert baloise["gate"]["cases"] == 3
    assert baloise["gate"]["evals_ok"] is True
    assert baloise["gate"]["countersigned"] is False
    assert BALOISE in production_reelle.app.state.foyer.corpus.documents
    assert BALOISE not in production_reelle.app.state.foyer.corpus.quarantine


def test_documents_ne_coute_rien_et_nest_pas_limitee(prod: TestClient) -> None:
    """AD-13 : le limiteur protège les routes qui appellent un modèle. `/documents` n'en appelle aucun."""
    quota = prod.app.state.foyer.settings.rate_limit_per_minute
    for _ in range(quota + 3):
        assert prod.get("/api/v1/documents", headers=XFF).status_code == 200
    assert prod.get(f"/api/v1/documents/{AXA}/report", headers=XFF).status_code == 200


def test_documents_na_pas_dalias_historique(prod: TestClient) -> None:
    """AD-11 ne nomme d'alias que `/sante` et `/chat` : un alias de plus serait une route inventée."""
    assert prod.get("/documents").status_code == 404
    assert prod.post("/sinistre", json=_corps(), headers=XFF).status_code in (404, 405)


# --- ligne « Rapport » et « Rapport d'un doc inconnu » --------------------

def test_report_rend_le_rapport_dingestion_tel_quel(prod: TestClient) -> None:
    """AD-8 : « le rapport est exposé tel quel ». Chargé au démarrage, jamais relu (AD-7)."""
    r = prod.get(f"/api/v1/documents/{AXA}/report")
    assert r.status_code == 200
    corps = r.json()
    assert corps["doc_id"] == AXA
    assert corps["checks"] and {"name", "level", "detail"} <= set(corps["checks"][0])
    assert corps["stats"]["pages"] > 0


def test_report_dun_document_inconnu_est_un_400_sans_echo(prod: TestClient) -> None:
    """AD-16 n'a pas de code 404 : lui en inventer un est exactement ce qu'il interdit.

    Et le message ne recopie pas le `doc_id` reçu (AD-15) : c'est une chaîne de l'appelant.
    """
    r = prod.get("/api/v1/documents/pas-un-document/report", headers=XFF)
    assert r.status_code == 400
    erreur = r.json()["error"]
    assert erreur["code"] == "invalid_request"
    assert "pas-un-document" not in erreur["message"]
    assert erreur["request_id"]


def test_un_rapport_illisible_est_une_alerte_et_un_400(tmp_path: Any) -> None:
    """D9 : un `report.json` présent mais invalide s'annonce sur `/sante` — jamais muet (AD-7)."""
    from server.app.api.etat import _rapports

    (tmp_path / "doc-a").mkdir()
    (tmp_path / "doc-a" / "report.json").write_text("{ceci n'est pas du JSON", encoding="utf-8")
    (tmp_path / "doc-b").mkdir()  # rapport **absent** : toléré, aucune alerte (comme dictionary.json)
    rapports, alertes = _rapports(tmp_path, ["doc-a", "doc-b"])
    assert rapports == {}
    assert [(a.doc_id, a.alerte) for a in alertes] == [("doc-a", "rapport_illisible")]
    assert "report.json" in alertes[0].detail


def _corpus_sur_disque(racine: Any, *, rapport: str | None, source: str | None,
                       quarantaine: bool = False) -> None:
    """Un corpus minimal **sur disque**, pour exercer `construire_etat` de bout en bout.

    Les tests qui appellent `_rapports`/`_sources` directement ne prouvent pas que leur résultat
    atteint `/sante` ni les routes (revue 1.9) : retirer la concaténation des alertes dans
    `construire_etat` les laissait tous verts.
    """
    import json as _json

    doc = Document(doc_id="doc-mini", kind="contrat", title="Mini", edition="2020",
                   source_hash="s", ingest_fingerprint="f",
                   nodes=[Node(node_id="doc-mini:n1", level=1, title="N1",
                               items=[{"block_id": "doc-mini:p1:1"}])],
                   blocks=[{"block_id": "doc-mini:p1:1", "loc": "p1", "seq": 1, "kind": "para",
                            "page": 1, "text": "Un paragraphe du contrat miniature."}])
    dossier = racine / "doc-mini"
    dossier.mkdir()
    charge = doc.model_dump(mode="json", exclude_defaults=True)
    octets = _json.dumps(charge, ensure_ascii=False, sort_keys=True).encode("utf-8")
    (dossier / "document.json").write_bytes(octets)
    (dossier / "summary.md").write_text("# Mini", encoding="utf-8")
    if rapport is not None:
        (dossier / "report.json").write_text(rapport, encoding="utf-8")
    if source is not None:
        (dossier / "source.url").write_text(source, encoding="utf-8")
    import hashlib
    entree = {"status": "quarantaine" if quarantaine else "servi", "source_hash": "s",
              "ingest_fingerprint": "f",
              "document_hash": hashlib.sha256(octets).hexdigest(), "edition": "2020",
              "overlay_hash": None, "gate": None}
    (racine / "manifest.json").write_text(_json.dumps({"doc-mini": entree}), encoding="utf-8")
    # Story 4.5, N3 (patch croisé 2/3) : le démarrage du service est un entrypoint de lecture de
    # production ; il exige une racine **installée** et refuse avant de lire sinon. La disposition
    # se pose ici, comme un opérateur la pose. Aucune attente de ce fichier ne change : le décor
    # rejoint la forme que la production a.
    from tests.helpers_espace import poser_espace

    poser_espace(racine.parent, data_dir=racine)


def _client_sur(racine: Any, *, raison_max_chars: int = 500) -> Any:
    """Une application dont le lifespan charge **ce** `data_dir` (monkeypatché sur `etat.DATA_DIR`)."""
    from contextlib import contextmanager

    from server.app.api import etat as etat_module

    @contextmanager
    def _ouvrir() -> Any:
        ancien = etat_module.DATA_DIR
        etat_module.DATA_DIR = racine
        try:
            # `env="dev"` : voir la fixture `prod` — un document sans gate n'est plus servi en
            # production, et ces `data/` minimaux n'en portent pas.
            with TestClient(create_app(_settings(
                    env="dev", allow_ungated=True,
                    raison_publiable_max_chars=raison_max_chars))) as c:
                yield c
        finally:
            etat_module.DATA_DIR = ancien

    return _ouvrir()


def test_un_rapport_illisible_remonte_jusqua_sante_et_a_la_route(tmp_path: Any) -> None:
    """D1/AD-7 : présent mais invalide ⇒ alerte, 400 et quarantaine fail-closed."""
    _corpus_sur_disque(tmp_path, rapport="{pas du JSON", source="https://exemple.invalid/cg.pdf")
    with _client_sur(tmp_path) as client:
        alertes = client.get("/api/v1/sante").json()["alerts"]
        assert ("doc-mini", "rapport_illisible") in [(a["doc_id"], a["alerte"]) for a in alertes]
        r = client.get("/api/v1/documents/doc-mini/report", headers=XFF)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
        assert r.json()["error"]["message"] == "le rapport d'ingestion est illisible"
        audit = client.get("/api/v1/documents", headers=XFF).json()[0]
        assert audit["doc_id"] == "doc-mini"
        assert audit["status"] == "quarantaine"
        assert audit["selectionnable"] is False
        assert "rapport_statique_illisible" in audit["raison"]
        assert audit["report_status"] == "illisible"


def test_une_source_privee_nest_jamais_publiee(tmp_path: Any) -> None:
    """AD-7 : `source.url` peut porter l'URI `gs://` de la copie privée. Elle ne sort pas.

    Deux pièges couverts d'un coup (revue 1.9) : le schéma `gs://`, et le fichier à **deux lignes**
    (URL publique puis copie privée) qu'un simple `strip()` publiait en entier.
    """
    _corpus_sur_disque(tmp_path, rapport='{"doc_id": "doc-mini", "checks": [], "stats": {}}',
                       source="gs://foyer-retour-sources/doc-mini.pdf\n")
    with _client_sur(tmp_path) as client:
        assert client.get("/api/v1/documents", headers=XFF).json()[0]["source_url"] is None


@pytest.mark.parametrize("fichier", [
    "https://exemple.invalid/cg.pdf\ngs://prive/doc-mini.pdf\n",
    # L'ordre inverse : rien ne garantit que l'URL publique vienne en premier, et un `gs://` en
    # tête ne doit ni sortir, ni faire disparaître la source publique qui le suit.
    "gs://prive/doc-mini.pdf\nhttps://exemple.invalid/cg.pdf\n",
])
def test_une_source_sur_deux_lignes_publie_la_ligne_publiable(tmp_path: Any, fichier: str) -> None:
    _corpus_sur_disque(tmp_path, rapport='{"doc_id": "doc-mini", "checks": [], "stats": {}}',
                       source=fichier)
    with _client_sur(tmp_path) as client:
        publiee = client.get("/api/v1/documents", headers=XFF).json()[0]["source_url"]
        assert publiee == "https://exemple.invalid/cg.pdf"
        assert "gs://" not in publiee


@pytest.mark.parametrize("brut, attendu", [
    ("https://exemple.invalid/cg.pdf", "https://exemple.invalid/cg.pdf"),
    ("  https://exemple.invalid/cg.pdf \n", "https://exemple.invalid/cg.pdf"),
    ("gs://foyer-retour-sources/cg.pdf", None),
    ("file:///tmp/cg.pdf", None),
    ("https://exemple.invalid/a b.pdf", None),   # un blanc : ce n'est plus une URL
    ("https://exemple.invalid/" + "x" * 3000, None),
    ("", None), ("   ", None), (None, None),
    # L'ordre des lignes n'est garanti par rien dans l'ingestion (revue 1.9, tour 2). Ne regarder
    # que la première rendait le filtre dépendant de cet ordre : un fichier qui écrit la copie
    # privée d'abord ne publiait plus **aucune** source, en silence, et la page perdait le lien qui
    # rend l'édition vérifiable. La ligne retenue est la première **publiable**, pas la première.
    ("gs://prive/cg.pdf\nhttps://exemple.invalid/cg.pdf\n", "https://exemple.invalid/cg.pdf"),
    ("https://exemple.invalid/cg.pdf\ngs://prive/cg.pdf\n", "https://exemple.invalid/cg.pdf"),
    ("gs://prive/a.pdf\ngs://prive/b.pdf\n", None),   # aucune ligne publiable : rien ne sort
    ("\n\n  \nhttps://exemple.invalid/cg.pdf", "https://exemple.invalid/cg.pdf"),
    # RFC 3986 : le schéma est **insensible à la casse**. Un `startswith` strict rejetait une URL
    # parfaitement valide comme il rejette un `gs://`. On filtre, on ne réécrit pas : la casse
    # d'origine est conservée dans la valeur rendue.
    ("HTTPS://Exemple.invalid/CG.pdf", "HTTPS://Exemple.invalid/CG.pdf"),
    ("Http://exemple.invalid/cg.pdf", "Http://exemple.invalid/cg.pdf"),
    ("GS://prive/cg.pdf", None),
    ("https://localhost/secret.pdf", None),
    ("https://service.localhost/secret.pdf", None),
    ("https://service.local/secret.pdf", None),
    ("https://metadata.google.internal/secret", None),
    ("http://127.0.0.1/secret.pdf", None),
    ("http://127.1/secret.pdf", None),
    ("http://127.0.1/secret.pdf", None),
    ("http://0x7f.0.0.1/secret.pdf", None),
    ("http://0177.0.0.1/secret.pdf", None),
    ("http://10.0.0.8/secret.pdf", None),
    ("http://169.254.169.254/latest/meta-data", None),
    ("http://[::1]/secret.pdf", None),
    ("https:///sans-hote.pdf", None),
    ("https://exemple.invalid:99999/cg.pdf", None),
    ("https://user:secret@exemple.invalid/cg.pdf", None),
    # Une ligne hors borne n'empoisonne pas le fichier : elle est sautée, la suivante est lue.
    ("https://exemple.invalid/" + "x" * 3000 + "\nhttps://exemple.invalid/cg.pdf",
     "https://exemple.invalid/cg.pdf"),
])
def test_url_publiable_est_lunique_decision_de_ce_qui_sort(brut: Any, attendu: Any) -> None:
    """Un seul filtre pour les deux chemins de publication (`source.url` **et** `Document.source_url`)."""
    from server.app.api.etat import url_publiable

    assert url_publiable(brut) == attendu


def test_un_source_url_de_document_non_publiable_est_filtre_aussi(prod: TestClient) -> None:
    """La branche « champ du document » passe par le même filtre que la branche « fichier »."""
    corpus, index = _mini_corpus()
    corpus.documents[DOC_ID].source_url = "gs://foyer-retour-sources/cg-mini.pdf"
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index
    par_id = {d["doc_id"]: d for d in prod.get("/api/v1/documents", headers=XFF).json()}
    assert par_id[DOC_ID]["source_url"] is None


def test_un_rapport_qui_decrit_un_autre_document_nest_pas_publie(tmp_path: Any) -> None:
    """AD-8 : le rapport « exposé tel quel » est celui **de ce document**, pas d'un autre.

    Un `report.json` conforme au schéma peut décrire un autre `doc_id` — copie de dossier, ingestion
    relancée ailleurs, document renommé sans réingestion. Rien ne le comparait au dossier qui le
    porte (revue 1.9, tour 2) : un humain lisait les checks et les statistiques d'un document qu'il
    n'avait pas demandé, sur la route qui sert précisément à juger si un contrat est lisible.
    """
    _corpus_sur_disque(tmp_path, rapport='{"doc_id": "un-autre-contrat", "checks": [], "stats": {}}',
                       source="https://exemple.invalid/cg.pdf")
    with _client_sur(tmp_path) as client:
        alertes = [(a["doc_id"], a["alerte"]) for a in client.get("/api/v1/sante").json()["alerts"]]
        assert ("doc-mini", "rapport_etranger") in alertes
        # Le nom de l'alerte est distinct de `rapport_illisible` : le fichier n'est pas illisible,
        # il est étranger, et ce n'est pas le même correctif.
        assert ("doc-mini", "rapport_illisible") not in alertes
        erreur = client.get("/api/v1/documents/doc-mini/report", headers=XFF)
        assert erreur.status_code == 400
        assert erreur.json()["error"]["message"] == (
            "le rapport d'ingestion décrit un autre document")
        # Le document reste servi : un rapport étranger n'est pas une incohérence du document.
        audit = client.get("/api/v1/documents", headers=XFF).json()[0]
        assert audit["doc_id"] == "doc-mini"
        assert audit["report_status"] == "etranger"


def test_un_document_en_quarantaine_est_auditable_sans_etre_charge(tmp_path: Any) -> None:
    """Story 3.5 : l'audit d'une quarantaine ne la remet jamais dans le working set."""
    _corpus_sur_disque(tmp_path, rapport='{"doc_id": "doc-mini", "checks": [], "stats": {}}',
                       source="https://exemple.invalid/cg.pdf", quarantaine=True)
    with _client_sur(tmp_path) as client:
        items = client.get("/api/v1/documents", headers=XFF).json()
        assert len(items) == 1
        assert items[0]["doc_id"] == "doc-mini"
        assert items[0]["status"] == "quarantaine"
        assert items[0]["selectionnable"] is False
        assert items[0]["raison"] == "quarantaine (manifest)"
        assert items[0]["title"] == "doc-mini"  # aucun document.json quarantiné n'est relu
        assert client.get("/api/v1/documents/doc-mini/report", headers=XFF).json()["checks"] == []
        assert client.app.state.foyer.corpus.documents == {}
        alertes = client.get("/api/v1/sante").json()["alerts"]
        assert "quarantaine" in [a["alerte"] for a in alertes]


def test_ids_quarantaines_invalides_ne_lisent_rien_hors_data_dir(tmp_path: Any) -> None:
    """Une clé absolue ou traversante est filtrée avant ``_rapports`` et ``_sources``."""
    _corpus_sur_disque(tmp_path, rapport=None, source=None, quarantaine=True)
    externe = tmp_path.parent / f"{tmp_path.name}-hors-data"
    externe.mkdir()
    (externe / "report.json").write_text("{rapport externe illisible", encoding="utf-8")
    (externe / "source.url").write_text("https://secret.invalid/contrat.pdf", encoding="utf-8")

    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entree = dict(manifest["doc-mini"])
    entree["status"] = "quarantaine"
    absolu = str(externe)
    traversal = "../" + externe.name
    manifest[absolu] = dict(entree)
    manifest[traversal] = dict(entree)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    from server.app.corpus.racine import EspaceIllisible

    with pytest.raises(EspaceIllisible, match="identifiant de document impropre"):
        with _client_sur(tmp_path):
            pass


def test_un_manifest_illisible_publie_une_quarantaine_anonyme_sur_sante(
        tmp_path: Any) -> None:
    """B1 : même sans clé de document publiable, l'échec global du manifest reste visible."""
    (tmp_path / "manifest.json").write_text("{", encoding="utf-8")
    # Story 4.5, N3 (patch croisé 2/3) : le démarrage exige une racine **installée**. Le manifest
    # reste illisible — c'est ce que ce test éprouve —, mais il est désormais illisible *sous* une
    # disposition, comme en production.
    from server.evals.espace import EspacePublie

    EspacePublie(tmp_path.parent, tmp_path).installer(
        [pathlib.Path(tmp_path.name) / "manifest.json"], migrer=True)

    from server.app.corpus.racine import EspaceIllisible

    with pytest.raises(EspaceIllisible, match="manifest publié absent, illisible ou invalide"):
        with _client_sur(tmp_path):
            pass


def test_un_doc_id_trop_long_est_quarantaine_et_alerte_sans_disparaitre(
        tmp_path: Any) -> None:
    _corpus_sur_disque(tmp_path, rapport=None, source=None, quarantaine=True)
    trop_long = "a" * 65
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[trop_long] = dict(manifest["doc-mini"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    from server.app.corpus.racine import EspaceNonInstalle

    with pytest.raises(EspaceNonInstalle, match="disposition est incomplète"):
        with _client_sur(tmp_path):
            pass


def test_rapport_et_metadonnees_restent_ceux_du_demarrage(tmp_path: Any) -> None:
    """Story 3.5 : ni la liste ni le JSON d'audit ne relisent ``data/`` par requête."""
    rapport = ('{"doc_id":"doc-mini","checks":['
               '{"name":"premier","level":"info","detail":"avant"},'
               '{"name":"second","level":"alerte","detail":"ordre conserve"}],"stats":{}}')
    _corpus_sur_disque(tmp_path, rapport=rapport,
                       source="https://exemple.invalid/cg.pdf", quarantaine=True)
    with _client_sur(tmp_path) as client:
        avant = client.get("/api/v1/documents/doc-mini/report", headers=XFF).json()
        (tmp_path / "doc-mini" / "report.json").write_text(
            '{"doc_id":"doc-mini","checks":[],"stats":{}}', encoding="utf-8")
        (tmp_path / "doc-mini" / "source.url").write_text(
            "file:///tmp/secret.pdf", encoding="utf-8")
        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        manifest["doc-mini"]["source_hash"] = "mutation-apres-boot"
        manifest["doc-mini"]["ingest_fingerprint"] = "mutation-apres-boot"
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        apres = client.get("/api/v1/documents/doc-mini/report", headers=XFF).json()
        audit = client.get("/api/v1/documents", headers=XFF).json()[0]

        assert apres == avant
        assert [c["name"] for c in apres["checks"]] == ["premier", "second"]
        assert [c["level"] for c in apres["checks"]] == ["info", "alerte"]
        assert audit["source_url"] == "https://exemple.invalid/cg.pdf"
        assert audit["source_hash"] == "s"
        assert audit["ingest_fingerprint"] == "f"
        assert audit["document_hash"] != "mutation-apres-boot"
        assert audit["selectionnable"] is False


@pytest.mark.parametrize("secret", [
    "gs://bucket-prive/contrat.pdf",
    "file:///root/contrat.pdf",
    "s3://bucket-prive/contrat.pdf",
    "/root/contrat.pdf",
    "/opt/dossier avec espaces/contrat.pdf",
    "/srv/foyer/contrat.pdf",
    "./data/contrat.pdf",
    "../secret/contrat.pdf",
    "secrets/contrats/contrat.pdf",
    "secrets/token",
    "config/privee.json",
    "données/privées",
    "données/privées/contrat-éxécuté.pdf",
    "'/srv/données privées/contrat.pdf'",
    "`secrets/contrats/run.json`",
    "~/.ssh/id_rsa",
    r"C:\Users\privé\contrat.pdf",
    r"\\serveur\partage\contrat.pdf",
    "data:text/plain;base64,U0VDUkVU",
])
def test_raison_publiable_masque_tout_emplacement_et_reste_utile(secret: str) -> None:
    from server.app.api.etat import raison_publiable

    raison = raison_publiable("rapport illisible : " + secret)
    assert raison is not None
    assert raison.startswith("rapport illisible : ")
    assert "[emplacement masqué]" in raison
    assert secret not in raison


def test_raison_publiable_est_bornee_sans_alterer_un_diagnostic_normal() -> None:
    from server.app.api.etat import raison_publiable

    assert raison_publiable("bloquant_statique : page_sans_texte") == (
        "bloquant_statique : page_sans_texte")
    # Un deux-points de diagnostic et une barre oblique au milieu d'une contrainte ne sont pas des
    # emplacements. L'ancien filtre prenait tout ce qui suivait ``invalide:`` pour une URI.
    diagnostics = (
        "Invalid JSON: expected value at line 1 column 1",
        "entrée de manifest invalide : Field required",
        "entrée invalide: String should match pattern '^[a-z0-9-]+$'",
        "regex attendue : `/^[a-z]+/[0-9]+$/`",
        "regex citée : '/^[a-z]+/[0-9]+$/'",
        "types acceptés : application/json et text/plain",
        "date 2026/08/27, fraction 1/2 et choix et/ou",
    )
    assert [raison_publiable(diagnostic) for diagnostic in diagnostics] == list(diagnostics)
    bornee = raison_publiable("diagnostic : " + "x" * 900)
    assert bornee is not None and len(bornee) == 500 and bornee.endswith("…")


def test_les_deux_surfaces_http_filtrent_un_chemin_relatif_avec_la_borne_configuree(
        prod: TestClient) -> None:
    from server.app.api.etat import _alertes, _alertes_dictionnaire
    from server.app.corpus.loader import Corpus
    from server.app.corpus.dictionary import Dictionnaire

    max_chars = 48
    raison = "lecture impossible : secrets/contrats/contrat.json — " + "x" * 100
    etat = prod.app.state.foyer
    etat.settings = Settings(_env_file=None, anthropic_api_key="", env="dev", allow_ungated=True,
                             raison_publiable_max_chars=max_chars)
    etat.corpus = Corpus(quarantine={"doc-prive": raison})
    etat.alerts = _alertes(
        etat.corpus, raison_max_chars=etat.settings.raison_publiable_max_chars)
    sante = prod.get("/api/v1/sante")
    documents = prod.get("/api/v1/documents", headers=XFF)
    assert sante.status_code == documents.status_code == 200
    assert "secrets/contrats" not in sante.text and "[emplacement masqué]" in sante.text
    assert "secrets/contrats" not in documents.text and "[emplacement masqué]" in documents.text
    assert len(sante.json()["alerts"][0]["detail"]) == max_chars
    assert len(documents.json()[0]["raison"]) == max_chars
    assert sante.json()["thresholds"]["raison_publiable_max_chars"] == max_chars

    dictionnaire = Dictionnaire(
        charge=False,
        raison="illisible : données/privées/dictionnaire.json — " + "x" * 100)
    detail = _alertes_dictionnaire(
        dictionnaire, raison_max_chars=etat.settings.raison_publiable_max_chars)[0].detail
    assert "données/privées" not in detail and "[emplacement masqué]" in detail
    assert detail.endswith("…")


def test_un_rapport_publie_masque_les_emplacements_prives_sans_perdre_les_checks(
        tmp_path: Any) -> None:
    rapport = json.dumps({
        "doc_id": "doc-mini",
        "checks": [
            {"name": "/srv/private/check.py", "level": "bloquant",
             "detail": "OSError sur /srv/données privées/source.pdf"},
            {"name": "couverture", "level": "info", "detail": "12 pages vérifiées"},
        ],
        "stats": {"pages": 12, "/srv/private/key": 1,
                  "nested": {"/srv/private/nested": 2},
                  "diagnostic": "cache secrets/rapports/run.json"},
    }, ensure_ascii=False)
    _corpus_sur_disque(tmp_path, rapport=rapport,
                       source="https://exemple.invalid/cg.pdf", quarantaine=True)
    with _client_sur(tmp_path) as client:
        publie = client.get("/api/v1/documents/doc-mini/report", headers=XFF)
        assert publie.status_code == 200
        corps = publie.json()
        assert [(c["name"], c["level"]) for c in corps["checks"]] == [
            ("[emplacement masqué]", "bloquant"), ("couverture", "info")]
        assert corps["checks"][1]["detail"] == "12 pages vérifiées"
        assert "[emplacement masqué]" in corps["checks"][0]["detail"]
        assert "[emplacement masqué]" in corps["stats"]["diagnostic"]
        assert corps["stats"]["[emplacement masqué]"] == 1
        assert corps["stats"]["nested"] == {"[emplacement masqué]": 2}
        assert str(tmp_path) not in publie.text


def test_le_boot_applique_la_borne_configuree_aux_alertes_et_rapports(tmp_path: Any) -> None:
    max_chars = 20
    rapport = json.dumps({
        "doc_id": "doc-mini",
        "checks": [{"name": "diagnostic", "level": "info", "detail": "x" * 120}],
        "stats": {},
    })
    _corpus_sur_disque(tmp_path, rapport=rapport, source="https://exemple.invalid/cg.pdf")
    manifest = json.loads((tmp_path / "manifest.json").read_text("utf-8"))
    manifest["doc-mini"]["source_hash"] = "autre-" + "x" * 100
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with _client_sur(tmp_path, raison_max_chars=max_chars) as client:
        sante = client.get("/api/v1/sante").json()
        rapport_public = client.get("/api/v1/documents/doc-mini/report", headers=XFF).json()
    assert sante["thresholds"]["raison_publiable_max_chars"] == max_chars
    quarantaine = next(a for a in sante["alerts"] if a["alerte"] == "quarantaine")
    dictionnaire = next(a for a in sante["alerts"] if a["alerte"] == "dictionnaire_non_valide")
    assert len(quarantaine["detail"]) == max_chars
    assert dictionnaire["detail"].endswith("…")
    assert len(rapport_public["checks"][0]["detail"]) == max_chars


def test_un_lien_symbolique_daudit_ne_lit_jamais_hors_data(tmp_path: Any) -> None:
    externe = tmp_path / "externe"
    data = tmp_path / "data"
    externe.mkdir()
    data.mkdir()
    _corpus_sur_disque(
        externe, rapport=json.dumps({"doc_id": "doc-mini", "checks": [],
                                     "stats": {"secret": "MARQUEUR_EXTERNE"}}),
        source="https://externe.invalid/secret.pdf")
    (data / "manifest.json").write_bytes((externe / "manifest.json").read_bytes())
    (data / "doc-mini").symlink_to(externe / "doc-mini", target_is_directory=True)
    # Story 4.5, N3 (patch croisé 2/3) : le démarrage exige une racine **installée**. Le lien
    # d'audit vers l'extérieur reste ce que ce test éprouve — la racine ne le couvre pas, et c'est
    # précisément pourquoi il ne doit rien publier.
    from server.evals.espace import EspacePublie

    EspacePublie(data.parent, data).installer(
        [pathlib.Path(data.name) / "manifest.json"], migrer=True)
    from server.app.corpus.racine import EspaceNonInstalle

    with pytest.raises(EspaceNonInstalle, match="disposition est incomplète"):
        with _client_sur(data):
            pass


def test_rapport_absent_est_distingue_sur_la_liste(tmp_path: Any) -> None:
    _corpus_sur_disque(tmp_path, rapport=None, source="https://exemple.invalid/cg.pdf")
    with _client_sur(tmp_path) as client:
        audit = client.get("/api/v1/documents", headers=XFF).json()[0]
        assert audit["status"] == "servi"
        assert audit["report_status"] == "absent"


def test_frontieres_http_doc_id_64_accepte_65_refuse_par_le_schema(prod: TestClient) -> None:
    accepte = prod.get(f"/api/v1/documents/{'a' * 64}/report")
    refuse = prod.get(f"/api/v1/documents/{'a' * 65}/report")
    assert accepte.status_code == refuse.status_code == 400
    assert accepte.json()["error"]["message"] == "aucun rapport d'ingestion n'est publié pour ce document"
    assert refuse.json()["error"]["message"].startswith("requête invalide — path.doc_id:")


# --- ligne « Sinistre nominal (bougie) » ---------------------------------

def test_sinistre_nominal_rend_200_avec_le_contrat_ad11(prod: TestClient) -> None:
    """AC : 200, `sources[]` avec `page`/`bbox`/`line_ids`/`kind` et des citations **relues**."""
    from server.app.corpus.dictionary import Dictionnaire

    corpus, _index = _mini_corpus()
    prod.app.state.foyer.dictionnaires[DOC_ID] = Dictionnaire(doc_id=DOC_ID)
    double = _brancher(prod, Double((_reponse(corpus), _trace())))
    r = _poster(prod)
    assert r.status_code == 200
    corps = r.json()

    assert set(corps) == {"answer", "sources", "via", "trace"}
    assert corps["via"] == "api/v1"
    assert corps["trace"]["pipeline"] == "sinistre"
    assert corps["trace"]["request_id"] == r.headers["X-Request-Id"]

    assert corps["answer"]["verdict"]["value"] == "sous_conditions"
    assert len(corps["sources"]) == 1
    clause = corps["sources"][0]
    assert clause["block_id"] == f"{DOC_ID}:p9:2"
    assert clause["page"] == 9
    assert clause["bbox"] == [70.0, 120.5, 520.0, 160.0]
    assert clause["line_ids"] == ["p9:2:l1"]
    # D5 : `kind` et `kind_confirmed` viennent du **bloc du corpus** (AD-6 : seule source de typage).
    assert clause["kind"] == "garantie"
    assert clause["kind_confirmed"] is True
    assert clause["status"] == "verifiee"
    # AD-3 : la citation publiée est le passage brut du corpus, ré-extrait aux offsets prouvés.
    assert clause["quote"] == Q_GARANTIE
    assert clause["quote"] in GARANTIE

    # Le double a bien reçu ce que la route dit lui passer, et rien d'autre.
    assert double.appels[0]["doc_id"] == DOC_ID
    assert double.appels[0]["faits"].description == FAITS["description"]
    assert double.appels[0]["request_id"] == corps["trace"]["request_id"]
    assert double.appels[0]["dictionnaire"] is prod.app.state.foyer.dictionnaires[DOC_ID]


def test_une_requete_bougie_baloise_recoit_seulement_son_dictionnaire(prod: TestClient) -> None:
    doc_id = "baloise-lu-home-2-2024"
    double = _brancher(prod, Double((_refus(), _trace())), mini=False)
    r = _poster(prod, _corps(doc_id=doc_id, question="Une bougie a brûlé mon canapé."))
    assert r.status_code == 200
    assert double.appels[0]["doc_id"] == doc_id
    assert double.appels[0]["dictionnaire"] is prod.app.state.foyer.dictionnaires[doc_id]
    assert double.appels[0]["dictionnaire"] is not prod.app.state.foyer.dictionnaire


def test_un_typage_non_confirme_ressort_du_serveur(prod: TestClient) -> None:
    """D5 : `kind_confirmed` est **lu sur le bloc**, et sa valeur fausse voyage jusqu'au front.

    Aucun test ne produisait la valeur `False` côté serveur (revue 1.9) : les trois qui traversaient
    `clauses_de` citaient le même bloc typé `manual`, et le test front lisait une fixture écrite à la
    main. Remplacer la lecture par un `True` constant serait passé — et la réserve « typage non
    confirmé » aurait disparu de la page, alors que c'est elle qui empêche d'afficher « exclusion »
    comme une certitude que le pipeline n'a pas (AD-6 : un kind non confirmé vaut `humain`).

    Le même bloc n'a ni `page` ni `bbox` : les deux ressortent `null`, autre valeur que rien
    n'observait.
    """
    corpus, _index = _mini_corpus()
    quote = _citation(corpus, f"{DOC_ID}:p46:1", "dommages causés intentionnellement")
    claim = VerifiedClaim(claim_id="c1", text="Une exclusion vise l'acte intentionnel.",
                          quotes=[quote],
                          status=ClaimStatus(retrouvee=True, pertinente=True, applicable="humain",
                                             edition="juin 2017"))
    answer = Answer(found=True, complete=True, texte=claim.text,
                    segments=[AnswerSegment(text=claim.text, kind="factuel", claim_ids=["c1"])],
                    claims=[claim], verdict=_verdict("ne_tranche_pas"))
    _brancher(prod, Double((answer, _trace())))
    clause = _poster(prod).json()["sources"][0]
    assert clause["kind"] == "exclusion"
    assert clause["kind_confirmed"] is False
    assert clause["page"] == 46
    assert clause["bbox"] is None
    assert clause["line_ids"] == []


def test_les_faits_compris_sont_publies(prod: TestClient) -> None:
    """D4 : « les faits compris » de l'AC sont `ParsedQuestion.scope`, publiés dans l'unique `Answer`."""
    corpus, _ = _mini_corpus()
    _brancher(prod, Double((_reponse(corpus), _trace())))
    compris = _poster(prod).json()["answer"]["faits_compris"]
    assert compris["bien"] == "mobilier de salon"
    assert compris["cause"] == "bougie"


def test_les_clauses_rejetees_voyagent_a_part_jamais_dans_sources(prod: TestClient) -> None:
    """AD-11/D7 : `sources[]` ne porte que les citations de la réponse **affichée**."""
    corpus, _ = _mini_corpus()
    _brancher(prod, Double((_reponse(corpus), _trace())))
    corps = _poster(prod).json()
    blocs_publies = {s["block_id"] for s in corps["sources"]}
    assert f"{DOC_ID}:p46:1" not in blocs_publies
    rejetee = corps["answer"]["rejected_claims"][0]
    assert rejetee["rejection_kind"] == "non_retrouvee"
    assert rejetee["text"]


def test_lordre_de_sources_est_celui_de_lenumeration_des_claims(prod: TestClient) -> None:
    """D6 : la page réapparie par position ; l'ordre de `clauses_de` **est** le contrat."""
    corpus, _index = _mini_corpus()
    q1 = _citation(corpus, f"{DOC_ID}:p9:2", Q_GARANTIE)
    q2 = _citation(corpus, f"{DOC_ID}:p9:2", "Les dégâts occasionnés au mobilier assuré")
    claims = [
        VerifiedClaim(claim_id="c1", text="Première.", quotes=[q1],
                      status=ClaimStatus(retrouvee=True, pertinente=True, applicable="oui",
                                         edition="juin 2017")),
        VerifiedClaim(claim_id="c2", text="Seconde.", quotes=[q2],
                      status=ClaimStatus(retrouvee=True, pertinente=True, applicable="humain",
                                         edition="juin 2017")),
    ]
    answer = Answer(found=True, complete=True, texte="Première. Seconde.",
                    segments=[AnswerSegment(text="Première.", kind="factuel", claim_ids=["c1"]),
                              AnswerSegment(text="Seconde.", kind="factuel", claim_ids=["c2"])],
                    claims=claims, verdict=_verdict())
    _brancher(prod, Double((answer, _trace())))
    sources = _poster(prod).json()["sources"]
    assert [s["quote"] for s in sources] == [q1.quote, q2.quote]


# --- ligne « Refus / ne_tranche_pas » ------------------------------------

@pytest.mark.parametrize("kind", ["hors_perimetre", "zero_hit", "claims_rejetes",
                                  "clarification_requise"])
def test_un_refus_est_un_200_avec_ne_tranche_pas_et_zero_source(prod: TestClient, kind: str) -> None:
    """AD-11 : **toute** sortie du pipeline est un 200. AD-16 : un refus porte son verdict."""
    _brancher(prod, Double((_refus(kind), _trace())))
    r = _poster(prod)
    assert r.status_code == 200
    corps = r.json()
    assert corps["answer"]["found"] is False
    assert corps["answer"]["verdict"]["value"] == "ne_tranche_pas"
    assert corps["answer"]["reason"]["kind"] == kind
    assert corps["sources"] == []
    assert corps["trace"]["request_id"]


# --- ligne « `doc_id` non servi » et « `doc_id` d'un guide » --------------

def test_un_doc_id_non_servi_est_un_400_avant_tout_appel(prod: TestClient) -> None:
    """AC : « `doc_id` non servi ⇒ 400 », et **avant** le premier appel facturé.

    Le pipeline lèverait `CorpusUnavailable` (503), qu'AD-16 réserve à une indisponibilité
    passagère : le corpus est chargé une fois au démarrage et ne bouge plus, un document que ce
    service ne sert pas est une faute d'appel. La route tranche donc la première.
    """
    double = _brancher(prod, Double(erreur=CorpusUnavailable("ne devrait jamais être atteint")))
    r = _poster(prod, _corps(doc_id="contrat-inexistant"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "contrat-inexistant" not in r.json()["error"]["message"]  # AD-15 : aucun écho
    assert double.appels == []  # rien n'a été facturé


def test_un_doc_id_de_guide_est_refuse_avant_tout_appel(prod: TestClient) -> None:
    """D3 : soumettre un sinistre au guide dépenserait quatre appels pour un `ne_tranche_pas` acquis."""
    double = _brancher(prod, Double(erreur=AssertionError("le pipeline ne doit pas être appelé")))
    r = _poster(prod, _corps(doc_id=GUIDE_ID))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "contrat" in r.json()["error"]["message"]
    assert double.appels == []


@pytest.mark.parametrize("lang", ["es", "eng", "XX"])
def test_une_langue_non_servie_est_un_400_avant_le_pipeline(prod: TestClient, lang: str) -> None:
    double = _brancher(prod, Double(erreur=AssertionError("le pipeline ne doit pas être appelé")))
    r = _poster(prod, _corps(lang=lang))
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    message = r.json()["error"]["message"]
    assert all(nom in message for nom in ("français", "anglais", "allemand", "portugais"))
    assert double.appels == []


# --- ligne « Description hors bornes » -----------------------------------

def test_une_description_hors_bornes_est_rejetee_jamais_tronquee(prod: TestClient) -> None:
    """AD-11 : « rejet plutôt que troncature » — la fin d'une description porte souvent la cause."""
    double = _brancher(prod, Double(erreur=AssertionError("le pipeline ne doit pas être appelé")))
    corps = _corps()
    corps["faits"]["description"] = "x" * 2001
    r = _poster(prod, corps)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert double.appels == []
    # La borne du schéma est celle du domaine : elle ne se recopie pas, elle se lit.
    borne = next(m.max_length for m in Faits.model_fields["description"].metadata
                 if getattr(m, "max_length", None) is not None)
    assert borne == 2000


@pytest.mark.parametrize("corps, motif", [
    ({"question": QUESTION, "faits": dict(FAITS)}, "doc_id absent"),
    ({"doc_id": DOC_ID, "faits": dict(FAITS)}, "question absente"),
    ({"doc_id": DOC_ID, "question": QUESTION}, "faits absents"),
    ({"doc_id": DOC_ID, "question": "   ", "faits": dict(FAITS)}, "question blanche"),
    ({"doc_id": DOC_ID, "question": "x" * 1001, "faits": dict(FAITS)}, "question hors borne"),
    ({"doc_id": DOC_ID, "question": QUESTION, "faits": {"description": "d", "montant_eur": -1}},
     "montant négatif"),
    # Le seul garde-fou serveur contre un sinistre sans faits : `Faits.description` n'a qu'un
    # `max_length`, et une description d'espaces atteindrait le pipeline pour y brûler quatre appels
    # facturés. Le supprimer laissait toute la suite verte (revue 1.9, tour 2).
    ({"doc_id": DOC_ID, "question": QUESTION, "faits": {"description": "   "}},
     "description blanche"),
    ({"doc_id": DOC_ID, "question": QUESTION, "faits": {"description": ""}},
     "description vide"),
    # Bornes de `date` et `lieu` (revue 1.9, tour 2) : les trois champs de texte partent au modèle
    # dans le même bloc `untrusted()`, et seul `request_max_bytes` les limitait.
    ({"doc_id": DOC_ID, "question": QUESTION,
      "faits": {"description": "d", "lieu": "x" * 201}}, "lieu hors borne"),
    ({"doc_id": DOC_ID, "question": QUESTION,
      "faits": {"description": "d", "date": "x" * 65}}, "date hors borne"),
])
def test_les_bornes_du_contrat_sont_appliquees_avant_la_route(prod: TestClient, corps: dict,
                                                              motif: str) -> None:
    """AD-11 : les bornes du schéma sont refusées par pydantic, converties en 400 par AD-16."""
    double = _brancher(prod, Double(erreur=AssertionError("le pipeline ne doit pas être appelé")))
    r = _poster(prod, corps)
    assert r.status_code == 400, motif
    assert r.json()["error"]["code"] == "invalid_request"
    assert double.appels == []


# --- ligne « Variante inconnue » -----------------------------------------

@pytest.mark.parametrize("valeur", ["agentique", "deterministe", "outils"])
def test_une_variante_postee_est_refusee_avant_tout_appel(prod: TestClient, valeur: str) -> None:
    """AD-1 : « un `pipeline.variant` inconnu ⇒ 400 » — et AD-11 n'en énumère aucune dans ce corps.

    Le champ a existé (story 1.9) puis a été retiré par la revue Codex tour 1 (I3) : AD-11 énumère
    `doc_id, question, faits, lang?`, et la story refusait `dossier` **en invoquant** cette
    énumération. `extra="forbid"` rend donc les trois formes en 400, avant le premier appel facturé —
    la variante inconnue qu'AD-1 vise, et les **deux** variantes connues qu'aucune règle n'autorise à
    choisir par HTTP (elles se choisissent en éval, `pipelines.sinistre.run(variant=…)`). Depuis la
    story 4.2d, `outils` est la troisième valeur, et c'est celle qu'un client tenterait de poster : le
    défaut du pipeline se sert parce que le corps ne le nomme pas, jamais parce qu'il le demande.
    """
    double = _brancher(prod, Double(erreur=AssertionError("le pipeline ne doit pas être appelé")))
    r = _poster(prod, _corps(variant=valeur))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert double.appels == []


def test_le_serveur_emet_tout_ce_que_la_page_exige_dun_200(prod: TestClient) -> None:
    """Le contrat, pris des **deux** côtés à la fois (revue Codex 1.9, tour 1, I2).

    `tools/sinistre/sinistre.js::lireReponse()` refuse un 200 auquel manque l'un des champs
    qu'UX-DR6 fait afficher, et rend `reponse_illisible` plutôt que de peindre un verdict amputé de
    ses réserves. Les deux moitiés sont testées séparément — le serveur par ce module, la page par
    `tests/test_web_sinistre.py` — et **jamais l'une contre l'autre** : durcir le lecteur sans que
    rien ne le confronte à ce que la route écrit vraiment, c'est se donner le moyen de rendre la
    page inutilisable sans qu'un test rougisse.

    Le test lit donc les chemins exigés **dans le source du front** (les littéraux passés à
    `exiger()` / `exigerListe()` dans `lireReponse`) et les cherche dans la réponse réellement
    sérialisée par FastAPI. Il ne demande pas `node` : ce sont les noms de champs qui sont comparés,
    pas le comportement du script.

    Depuis le tour 2 (I2), la comparaison porte sur la **présence de la clé**, pas sur une valeur non
    nulle : `answer.faits_compris` et `answer.clarification` sont `X | None` du contrat, et un
    `None` sérialisé est ce que la route doit écrire — le compter manquant aurait fait rougir le
    test sur un corps parfaitement conforme. Un chemin dont un ancêtre vaut `null` n'est pas
    atteignable et n'est pas exigible : il est ignoré, pas compté manquant.
    """
    import re
    from server.app.config import REPO_ROOT

    source = (REPO_ROOT / "tools" / "sinistre" / "sinistre.js").read_text(encoding="utf-8")
    # Le corps de `lireReponse()` seul : `documents()` a son propre `illisible("documents")`, qui
    # parle d'une **autre** route. Les chemins des **éléments** de liste (`sources[0].kind`,
    # `answer.claims[0].status`) sont composés à l'exécution dans `lireClause()`/`lireClaim()` : ils
    # ne sont pas des littéraux et ne sont donc pas relevés ici — c'est le harnais JS qui les couvre.
    debut = source.index("function lireReponse(")
    corps_js = source[debut:source.index("\n  }", debut)]
    exiges = sorted(set(re.findall(r'exiger(?:Liste)?\([^;]*?"([^"]+)"\)', corps_js)))
    assert len(exiges) >= 29, f"le lecteur du front n'exige plus que {exiges} : le motif a changé"
    # Le durcissement du tour 2 se lit ici, pas seulement dans un compte : ces quatre booléens sont
    # ceux dont l'absence faisait annoncer une pièce du contrat « non lue » qui ne l'était pas.
    assert "answer.verdict.missing.conditions_particulieres" in exiges
    # Tour 3 : la trace est affichée, donc elle est du contrat affiché. `total_cost_eur` en
    # particulier — NFR4 veut le coût réel à l'écran, et un coût absent ne se peignait pas en
    # « coût inconnu » mais en rien du tout. C'est ici, et seulement ici, qu'on vérifie que la route
    # écrit vraiment ce que la page exige désormais d'elle.
    assert {"trace.pipeline", "trace.variant", "trace.steps", "trace.total_cost_eur"} <= set(exiges)

    corpus, _ = _mini_corpus()
    _brancher(prod, Double((_reponse(corpus), _trace())))
    corps = _poster(prod).json()
    assert corps["sources"], "le cas témoin doit publier au moins une clause"
    # Sans quoi la règle « ancêtre `null` ⇒ chemin non exigible » viderait le test de six chemins
    # sans que rien ne le dise : le cas témoin doit porter des faits compris.
    assert corps["answer"]["faits_compris"], "le cas témoin doit publier des faits compris"
    # Idem pour la trace : une trace sans étape est légitime, mais elle ferait passer les chemins
    # d'étapes sans les éprouver. Le cas témoin en porte, et la page les lit jusqu'à la feuille.
    assert corps["trace"]["steps"], "le cas témoin doit publier des étapes de trace"

    manquants = []
    for chemin in exiges:
        cible: Any = corps
        for partie in chemin.split("."):
            if cible is None:
                break  # ancêtre `null` : le chemin n'est pas atteignable, il n'est pas exigible
            if isinstance(cible, dict) and partie in cible:
                cible = cible[partie]
            else:
                manquants.append(chemin)
                break
    assert not manquants, f"la page exige des champs que la route n'écrit pas : {manquants}"


def test_le_corps_ne_porte_que_les_quatre_champs_dad_11(prod: TestClient) -> None:
    """L'énumération d'AD-11, épinglée : ni plus, ni moins."""
    assert set(SinistreRequest.model_fields) == {"doc_id", "question", "faits", "lang"}
    corpus, _ = _mini_corpus()
    double = _brancher(prod, Double((_reponse(corpus), _trace())))
    assert _poster(prod).status_code == 200
    # Aucune variante n'est imposée par la route : le défaut du pipeline fait foi.
    assert "variant" not in double.appels[0]


# --- D1 : le `dossier` n'est pas exposé ----------------------------------

def test_le_dossier_nest_pas_un_champ_du_corps(prod: TestClient) -> None:
    """D1 : la route n'expose pas `dossier` — le paquet reste réputé inconnu, annoncé et réclamé.

    Un booléen « j'ai les conditions particulières » sur un démonstrateur public non authentifié est
    une auto-déclaration invérifiable qui referme la seule question que l'outil sait poser.
    """
    assert "dossier" not in SinistreRequest.model_fields
    double = _brancher(prod, Double(erreur=AssertionError("le pipeline ne doit pas être appelé")))
    # Envoyé quand même : depuis la revue Codex 1.9 (I3), `extra="forbid"` le **refuse** au lieu de
    # l'ignorer. Ignorer, c'était acquiescer en silence à un corps qu'on n'honore pas : l'appelant
    # repartait avec un verdict « conditions générales seules » en croyant avoir déclaré son paquet.
    r = _poster(prod, _corps(dossier={"conditions_particulieres": False}))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert double.appels == []

    # Et sans le champ, le paquet reste réputé inconnu : c'est ce que `missing` annonce.
    corpus, _ = _mini_corpus()
    _brancher(prod, Double((_reponse(corpus), _trace())))
    r = _poster(prod)
    assert r.status_code == 200
    assert r.json()["answer"]["verdict"]["missing"]["conditions_particulieres"] is True


# --- ligne « Quota dépassé » ---------------------------------------------

def test_le_quota_rend_429_avec_retry_after(prod: TestClient) -> None:
    """AD-13 : le limiteur est une dépendance de **routeur**, comme sur `/chat` — corps invalides compris."""
    corpus, _ = _mini_corpus()
    _brancher(prod, Double((_reponse(corpus), _trace())))
    quota = prod.app.state.foyer.settings.rate_limit_per_minute
    for _ in range(quota):
        assert _poster(prod).status_code == 200
    r = _poster(prod)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) > 0


def test_un_corps_invalide_compte_dans_le_quota(prod: TestClient) -> None:
    """Le limiteur placé dans la fonction n'aurait jamais vu les requêtes que pydantic refuse."""
    _brancher(prod, Double(erreur=AssertionError("jamais appelé")))
    quota = prod.app.state.foyer.settings.rate_limit_per_minute
    for _ in range(quota):
        assert _poster(prod, {"doc_id": DOC_ID}).status_code == 400
    assert _poster(prod, {"doc_id": DOC_ID}).status_code == 429


# --- ligne « Modèle indisponible / deadline » ----------------------------

@pytest.mark.parametrize("erreur, code", [
    (LlmUnavailable("fournisseur en 529"), "llm_unavailable"),
    (Timeout("deadline épuisée"), "timeout"),
    (BudgetExceeded("plafond atteint"), "budget_exceeded"),
    (CorpusUnavailable("document mis en quarantaine pendant la requête"), "corpus_unavailable"),
])
def test_un_echec_terminal_rend_503_avec_sa_trace_partielle(prod: TestClient, erreur: Exception,
                                                            code: str) -> None:
    """AD-16 : échec terminal typé, trace partielle, **aucun repli** — et surtout aucun verdict."""
    exc = erreur
    exc.trace = _trace(request_id="r-partielle", total_cost_eur=0.0122)  # type: ignore[attr-defined]
    _brancher(prod, Double(erreur=exc))
    r = _poster(prod)
    assert r.status_code == 503
    corps = r.json()
    assert corps["error"]["code"] == code
    assert corps["trace"]["total_cost_eur"] == 0.0122  # AD-10 : le coût engagé reste mesurable
    assert "answer" not in corps and "verdict" not in corps


# --- ligne « Citation non confirmée » ------------------------------------

def test_une_citation_que_le_corpus_ne_confirme_pas_est_un_500(prod: TestClient) -> None:
    """AD-11 (revue 1.6, B1 tour 2) : jamais un verdict servi avec ses clauses amputées.

    C'est sous un verdict que « phrase sans source » coûte le plus cher : la clause est ce qui fonde
    la décision affichée.
    """
    quote = VerifiedQuote(block_id=f"{DOC_ID}:p404:1", quote="passage d'un bloc qui n'existe pas",
                          start=0, end=10, text_start=0, text_end=10)
    claim = VerifiedClaim(claim_id="c1", text="Affirmation.", quotes=[quote],
                          status=ClaimStatus(retrouvee=True, pertinente=True, edition="juin 2017"))
    answer = Answer(found=True, complete=True, texte="Affirmation.",
                    segments=[AnswerSegment(text="Affirmation.", kind="factuel", claim_ids=["c1"])],
                    claims=[claim], verdict=_verdict())
    _brancher(prod, Double((answer, _trace())))
    r = _poster(prod)
    assert r.status_code == 500
    corps = r.json()
    assert corps["error"]["code"] == "internal"
    # AD-16 : message générique, toujours le même — le `block_id` d'une citation que le corpus ne
    # confirme pas est justement celui qu'aucun index ne connaît (AD-15).
    assert corps["error"]["message"] == MESSAGE_INTERNE
    assert "p404" not in corps["error"]["message"]
    assert corps["trace"]["pipeline"] == "sinistre"


def test_des_offsets_hors_du_bloc_sont_un_500(prod: TestClient) -> None:
    """`_relire` d'AD-3 : des offsets qui ne retombent pas sur le bloc ne viennent pas du pipeline."""
    quote = VerifiedQuote(block_id=f"{DOC_ID}:p9:2", quote="peu importe", start=0, end=5,
                          text_start=0, text_end=99999)
    claim = VerifiedClaim(claim_id="c1", text="Affirmation.", quotes=[quote],
                          status=ClaimStatus(retrouvee=True, pertinente=True, edition="juin 2017"))
    answer = Answer(found=True, complete=True, texte="Affirmation.",
                    segments=[AnswerSegment(text="Affirmation.", kind="factuel", claim_ids=["c1"])],
                    claims=[claim], verdict=_verdict())
    _brancher(prod, Double((answer, _trace())))
    assert _poster(prod).status_code == 500


# --- AD-10 : la ligne de log ---------------------------------------------

def test_la_ligne_de_log_porte_des_champs_jamais_du_texte(prod: TestClient,
                                                          caplog: pytest.LogCaptureFixture) -> None:
    """AD-10 : ni la question, ni la description du sinistre, ni les termes cherchés."""
    import json
    import logging

    corpus, _ = _mini_corpus()
    _brancher(prod, Double((_reponse(corpus), _trace())))
    with caplog.at_level(logging.INFO, logger="foyer.request"):
        assert _poster(prod).status_code == 200
    lignes = [json.loads(r.message) for r in caplog.records if r.name == "foyer.request"]
    ligne = next(l for l in lignes if l.get("path") == "/api/v1/sinistre")
    assert ligne["verdict"] == "sous_conditions"
    assert ligne["found"] is True
    assert ligne["cost_eur"] == 0.0336
    brut = json.dumps(ligne, ensure_ascii=False)
    assert QUESTION not in brut
    assert "bougie" not in brut
    assert "mobilier" not in brut


def _pipeline_sinistre_reel(script: list[dict], **reglages: Any) -> Any:
    """Le **vrai** `pipelines.sinistre.run`, avec un modèle scripté et des seuils resserrés.

    Le pendant de `_pipeline_reel` du guide : la route reste la vraie, le pipeline reste le vrai, et
    seule la borne de retrieval de cette requête change — c'est elle qui produit la lecture bornée.
    """
    from server.app.llm.client import LlmClient
    from tests.llm_fake import FakeAnthropic

    anthropic_fake = FakeAnthropic(script)

    async def appeler(doc_id: Any, question: str, faits: Any, **kw: Any):
        reglage = kw.pop("settings").model_copy(update=reglages)
        kw["client"] = LlmClient(reglage, anthropic_client=anthropic_fake)
        return await sinistre.run(doc_id, question, faits, settings=reglage, **kw)

    return appeler


def test_une_lecture_partielle_traverse_le_vrai_pipeline_puis_la_route(prod: TestClient) -> None:
    """Story 4.2f, la surface **composée** côté sinistre : pipeline réel → route → 200 typé.

    C'est le pipeline où l'affirmation d'absence coûte le plus cher, et donc celui où la jonction
    mérite d'être prouvée : la borne du retrieval, la vérification qui ne retient rien, le verdict
    calculé par la table d'AD-6 sur zéro clause et la projection HTTP, sur une seule requête.
    """
    import json as _json

    from server.app.llm.models import TIERS
    from tests.llm_fake import fake_message

    corpus, index = _mini_corpus()
    etat = prod.app.state.foyer
    etat.corpus, etat.index = corpus, index
    comprendre = fake_message(model=TIERS["micro"], text=_json.dumps({
        "intent": "question", "question_resolue": QUESTION, "clarification": None, "language": "fr",
        "terms": ["chaleur"], "themes": [], "facettes": ["couverture du sinistre"],
        "bien": "mobilier", "evenement": None, "lieu": None, "cause": None, "moment": None}))
    # Une clause **réelle mais non transmise** : la citation ne peut pas être retenue.
    ebauche = {"segments": [{"text": "Une clause.", "kind": "factuel", "claim_ids": ["c1"]}],
               "claims": [{"claim_id": "c1", "text": "Une clause.",
                           "quotes": [{"block_id": f"{DOC_ID}:p46:1", "quote": EXCLUSION[:40]}]}]}
    # La variante n'est pas choisie par HTTP (`SinistreRequest` est `extra="forbid"`) : la requête
    # emprunte donc le **défaut d'AD-1**, `outils`, c'est-à-dire le chemin réellement servi. Le tour
    # de navigation est scripté ; les outils, eux, s'exécutent pour de bon sur l'index.
    navigation = fake_message(model=TIERS["micro"], stop_reason="tool_use", content=[
        {"type": "tool_use", "id": "t-chercher", "name": "chercher",
         "input": {"termes": ["chaleur"]}},
        {"type": "tool_use", "id": "t-ouvrir", "name": "ouvrir_noeud",
         "input": {"node_id": f"{DOC_ID}:socle", "focus_block_id": f"{DOC_ID}:p9:2"}}])
    navigation_fin = fake_message(model=TIERS["micro"], stop_reason="end_turn", content=[])
    script = [comprendre, navigation, navigation_fin,
              fake_message(model=TIERS["reason"], text=_json.dumps(ebauche)),
              fake_message(model=TIERS["reason"], text=_json.dumps(ebauche))]
    etat.pipeline_sinistre = _pipeline_sinistre_reel(script, retrieval_max_blocks=1)

    r = _poster(prod)

    assert r.status_code == 200, r.text
    j = r.json()
    assert j["answer"]["found"] is False and j["answer"]["complete"] is False
    assert j["answer"]["reason"] is None  # aucune absence du contrat n'est affirmée
    lue = j["answer"]["lecture_partielle"]
    ouverts = [b for s in j["trace"]["steps"] for b in s.get("opened_block_ids", [])]
    assert lue["blocks_read"] == len(set(ouverts)) >= 1
    assert 1 <= lue["nodes_read"] <= lue["blocks_read"]
    assert lue["documents"] == [DOC_ID]
    assert j["trace"]["truncations"] == 1
    assert j["trace"]["variant"] == "outils"  # le chemin réellement servi
    # AD-16 : jamais un sinistre sans verdict — et il vient de la table, pas d'un remplacement.
    assert j["answer"]["verdict"]["value"] == "ne_tranche_pas"
    assert j["sources"] == [] and j["answer"]["rejected_claims"]
    assert j["answer"]["unknown"]


def test_le_journal_du_sinistre_nomme_aussi_une_lecture_partielle(
        prod: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """Le site frère de `routes/chat.py`, sous la même règle : les deux écrivent le même champ."""
    import json
    import logging

    answer = Answer(
        found=False, complete=False, texte="Ma lecture du contrat s'est arrêtée avant de conclure.",
        lecture_partielle=LecturePartielle(nodes_read=1, blocks_read=4, documents=[DOC_ID]),
        verdict=_verdict("ne_tranche_pas", ask_client=[], escalate=[]),
        unknown=["Je n'ai pas pu lire tout ce qui pouvait concerner ce sinistre."])
    _brancher(prod, Double((answer, _trace())))
    with caplog.at_level(logging.INFO, logger="foyer.request"):
        assert _poster(prod).status_code == 200
    lignes = [json.loads(r.message) for r in caplog.records if r.name == "foyer.request"]
    ligne = next(l for l in lignes if l.get("path") == "/api/v1/sinistre")
    assert ligne["found"] is False and ligne["reason_kind"] == "lecture_tronquee"
    assert ligne["variants_count"] is None and ligne["blocks_scanned"] is None
    assert ligne["verdict"] == "ne_tranche_pas"


# --- l'état « partiel » traverse l'enveloppe (story 2.3, revue coordonnée A8) ---
def test_une_reponse_partielle_traverse_lenveloppe_avec_ce_qui_lui_manque(prod: TestClient) -> None:
    """Le contrat sinistre ne publie pas de champ plat `unknown` : c'est l'`Answer` entier qui le
    porte (AD-4, « un seul objet de réponse »), et `tools/sinistre/sinistre.js` y lit sa section
    « Ce que je ne sais pas ». Rien ne le vérifiait au niveau HTTP.

    Le cas est celui du dossier réel : une clause retenue, un verdict `sous_conditions`, et une
    franchise que la réponse ne sait pas dire — servie en 200, badgée partielle, avec sa lacune.
    """
    _brancher(prod, Double((_reponse(_mini_corpus()[0]), _trace())))

    r = _poster(prod)
    j = r.json()

    assert r.status_code == 200
    assert j["answer"]["found"] is True and j["answer"]["complete"] is False
    assert j["answer"]["unknown"] == ["La franchise applicable n'est pas dite."]
    # Partielle ne veut pas dire non sourcée : la clause retenue est publiée, et le verdict avec elle.
    assert j["sources"] and j["answer"]["verdict"] is not None


# --- story 4.2f : une lecture partielle traverse l'enveloppe en 200 typé ---
def test_une_lecture_partielle_est_publiee_en_200_avec_son_verdict(prod: TestClient) -> None:
    """AD-11 : « toute sortie du pipeline est un 200 ». Le gestionnaire recevait « L'analyse est
    indisponible pour le moment » alors que rien n'était en panne.

    Le contrat sinistre publie `answer` entier : le nouveau porteur traverse sans modification de
    schéma, et c'est cela que ce test garde. `sources[]` reste vide — aucune clause n'a été retenue —
    tandis que les clauses écartées voyagent dans `answer`, où la page les affiche.
    """
    phrase = "Ma lecture du contrat s'est arrêtée avant de conclure."
    answer = Answer(
        found=False, complete=False, texte=phrase,
        segments=[AnswerSegment(text=phrase, kind="limite")],
        rejected_claims=[RejectedClaim(
            claim_id="c2", text="Une clause écartée par la vérification.",
            quotes=[Quote(block_id=f"{DOC_ID}:p46:1", quote="phrase que le modèle a inventée")],
            status=ClaimStatus(retrouvee=False, edition="juin 2017"),
            rejection_kind="non_retrouvee", motif="citation introuvable")],
        lecture_partielle=LecturePartielle(nodes_read=1, blocks_read=4, documents=[DOC_ID]),
        verdict=_verdict("ne_tranche_pas", ask_client=[], escalate=[]),
        faits_compris=QuestionScope(bien="mobilier de salon"),
        unknown=["Je n'ai pas pu lire tout ce qui pouvait concerner ce sinistre."])
    _brancher(prod, Double((answer, _trace())))

    r = _poster(prod)
    j = r.json()

    assert r.status_code == 200
    assert j["answer"]["found"] is False and j["answer"]["complete"] is False
    assert j["answer"]["reason"] is None  # aucune absence du contrat n'est affirmée
    assert j["answer"]["lecture_partielle"] == {"nodes_read": 1, "blocks_read": 4,
                                                "documents": [DOC_ID]}
    # AD-16 : jamais un sinistre sans verdict — et jamais un verdict de remplacement.
    assert j["answer"]["verdict"]["value"] == "ne_tranche_pas"
    assert j["sources"] == []
    assert [c["rejection_kind"] for c in j["answer"]["rejected_claims"]] == ["non_retrouvee"]
    assert j["answer"]["unknown"]
