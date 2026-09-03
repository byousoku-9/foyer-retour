"""Story 5.6 (L1) — les deux routes de progression : le même résultat, dit pendant le calcul.

Ce que ces témoins tiennent, et c'est tout le contrat de la story :

1. les événements arrivent **dans l'ordre** de la chaîne, chacun nommé par la table close de
   `api/progression`, avec son rang, son total, son temps écoulé et son libellé ;
2. `resultat` porte **exactement** le JSON que la route classique rend sur la **même** fixture —
   comparé octet à octet après désérialisation, pas « à peu près » ;
3. un échec terminal du pipeline devient un événement `erreur` qui porte le corps et le statut
   qu'`errors.py` aurait rendus, et non une réponse tronquée ;
4. rien de la requête ni du modèle ne fuit dans un événement d'avancement.

Aucun réseau : le pipeline est un double qui appelle les rappels comme la vraie chaîne, puis rend
l'`Answer` et la `Trace` que la route classique aurait reçues.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from server.app.api.progression import ORDRE
from server.app.domain.errors import LlmUnavailable
from tests.test_api import (
    DOC_ID as GUIDE_DOC_ID,
    Double as DoubleGuide,
    _brancher as brancher_guide,
    _citation,
    _claim,
    _mini_corpus as mini_guide,
    _trace as trace_guide,
    prod,  # noqa: F401 — fixture
)
from tests.test_api_sinistre import (
    FAITS,
    QUESTION,
    XFF,
    Double as DoubleSinistre,
    _brancher as brancher_sinistre,
    _corps,
    _mini_corpus as mini_sinistre,
    _reponse as reponse_sinistre,
    _trace as trace_sinistre,
    prod as prod_sinistre,  # noqa: F401 — fixture
)
from server.app.domain.answer import Answer, AnswerSegment

# --- lecture d'un flux SSE ---------------------------------------------------------------------

def evenements(texte: str) -> list[tuple[str, Any]]:
    """`(nom, données)` de chaque événement du flux ; les commentaires `:` sont ignorés."""
    lus: list[tuple[str, Any]] = []
    nom: str | None = None
    for ligne in texte.splitlines():
        if ligne.startswith(":") or not ligne.strip():
            continue
        if ligne.startswith("event: "):
            nom = ligne[len("event: "):]
        elif ligne.startswith("data: "):
            assert nom is not None, "une ligne `data:` sans `event:` la précédant"
            lus.append((nom, json.loads(ligne[len("data: "):])))
            nom = None
    return lus


class DoubleQuiAvance:
    """Un double qui appelle les rappels comme la vraie chaîne, puis délègue au double nominal.

    Les noms émis sont ceux des pipelines (`retrouver`, pas `naviguer`) : c'est la table de
    `api/progression` qui traduit, et ce témoin la traverse au lieu de la contourner.
    """

    def __init__(self, delegue: Any) -> None:
        self.delegue = delegue

    async def __call__(self, *args: Any, **kw: Any) -> Any:
        on_etape = kw.pop("on_etape", None)
        on_tour = kw.pop("on_tour", None)
        for nom in ("comprendre", "retrouver"):
            if on_etape is not None:
                on_etape(nom)
        for tour in (1, 2):
            if on_tour is not None:
                on_tour(tour)
        for nom in ("rediger", "verifier", "restituer"):
            if on_etape is not None:
                on_etape(nom)
        return await self.delegue(*args, **kw)


def _controler_avancement(lus: list[tuple[str, Any]], *, libelle_navigation: str) -> None:
    """Les invariants communs aux deux routes : ordre, bornes, libellés, absence de fuite."""
    etapes = [d for nom, d in lus if nom == "etape"]
    assert [e["nom"] for e in etapes] == list(ORDRE)
    assert [e["rang"] for e in etapes] == [1, 2, 3, 4, 5]
    assert {e["total"] for e in etapes} == {len(ORDRE)}
    assert all(isinstance(e["ms_ecoule"], int) and e["ms_ecoule"] >= 0 for e in etapes)
    assert etapes[1]["libelle"] == libelle_navigation
    assert etapes[3]["libelle"] == "Je vérifie chaque citation"
    # Les tours de navigation, dans l'ordre, et rien de ce qui a été lu.
    tours = [d for nom, d in lus if nom == "tour"]
    assert [t["tour"] for t in tours] == [1, 2]
    assert all(set(t) == {"tour", "ms_ecoule"} for t in tours)
    # AD-10/AD-15 : un événement d'avancement ne porte que des champs, jamais du texte reçu ou rendu.
    assert all(set(e) == {"nom", "rang", "total", "ms_ecoule", "libelle"} for e in etapes)
    # Le flux se ferme sur un terminal, et un seul.
    assert len([nom for nom, _ in lus if nom in ("resultat", "erreur")]) == 1
    assert lus[-1][0] in ("resultat", "erreur")


# --- guide -------------------------------------------------------------------------------------

def _reponse_guide() -> tuple[Answer, Any]:
    corpus, _ = mini_guide()
    claim = _claim("c1", "Le délai est de huit jours.",
                   _citation(corpus, f"{GUIDE_DOC_ID}:farrivee:2", "huit jours"))
    answer = Answer(found=True, complete=True, texte="Le délai est de huit jours.",
                    segments=[AnswerSegment(text="Le délai est de huit jours.", kind="factuel",
                                            claim_ids=["c1"])],
                    claims=[claim])
    return answer, trace_guide()


def test_le_flux_du_guide_rend_le_json_de_la_route_classique(prod: TestClient) -> None:  # noqa: F811
    """Le contrat de la story : `resultat` **est** la réponse de `/chat`, sur la même fixture."""
    corps = {"question": "Quel délai après mon arrivée ?", "profil": {}, "historique": []}
    resultat = _reponse_guide()

    brancher_guide(prod, DoubleGuide(resultat), mini=True)
    attendu = prod.post("/api/v1/chat", json=corps, headers=XFF).json()

    brancher_guide(prod, DoubleQuiAvance(DoubleGuide(resultat)), mini=True)  # type: ignore[arg-type]
    flux = prod.post("/api/v1/chat/progression", json=corps, headers=XFF)
    assert flux.status_code == 200
    assert flux.headers["content-type"].startswith("text/event-stream")

    lus = evenements(flux.text)
    _controler_avancement(lus, libelle_navigation="Je lis les fiches du guide")
    nom, servi = lus[-1]
    assert nom == "resultat"
    # Le `request_id` est le seul champ qui diffère entre deux requêtes : il est aligné avant la
    # comparaison, tout le reste doit être identique — texte, segments, sources, fiches, trace.
    servi["trace"]["request_id"] = attendu["trace"]["request_id"]
    assert servi == attendu


def test_une_panne_du_fournisseur_devient_un_evenement_erreur(prod: TestClient) -> None:  # noqa: F811
    """AD-16 : le flux est déjà parti en 200, l'échec terminal se dit donc dans un événement.

    Le corps et le statut sont ceux qu'`errors.py` aurait mis sur le réseau — la route classique et
    la route de progression n'ont pas deux enveloppes d'erreur.
    """
    corps = {"question": "Quel délai après mon arrivée ?", "profil": {}, "historique": []}
    brancher_guide(prod, DoubleGuide(erreur=LlmUnavailable("fournisseur indisponible")), mini=True)
    attendu = prod.post("/api/v1/chat", json=corps, headers=XFF)

    brancher_guide(prod, DoubleQuiAvance(  # type: ignore[arg-type]
        DoubleGuide(erreur=LlmUnavailable("fournisseur indisponible"))), mini=True)
    flux = prod.post("/api/v1/chat/progression", json=corps, headers=XFF)

    assert flux.status_code == 200  # le statut est parti avec le premier octet du flux
    lus = evenements(flux.text)
    _controler_avancement(lus, libelle_navigation="Je lis les fiches du guide")
    nom, servi = lus[-1]
    assert nom == "erreur"
    assert servi["status"] == attendu.status_code == 503
    servi["corps"]["error"]["request_id"] = attendu.json()["error"]["request_id"]
    assert servi["corps"] == attendu.json()


def test_une_borne_du_contrat_reste_un_vrai_400(prod: TestClient) -> None:  # noqa: F811
    """`historique_max_turns` est appliqué **avant** le flux : le client reçoit un 400, pas un 200."""
    corps = {"question": "q", "profil": {},
             "historique": [{"role": "user", "texte": f"t{i}"} for i in range(40)]}
    reponse = prod.post("/api/v1/chat/progression", json=corps, headers=XFF)
    assert reponse.status_code == 400
    assert reponse.json()["error"]["code"] == "invalid_request"


# --- sinistre ----------------------------------------------------------------------------------

def test_le_flux_du_sinistre_rend_le_json_de_la_route_classique(
        prod_sinistre: TestClient) -> None:  # noqa: F811
    """Le même contrat côté sinistre, verdict compris."""
    corps = _corps()
    corpus, _ = mini_sinistre()
    resultat = (reponse_sinistre(corpus), trace_sinistre())

    brancher_sinistre(prod_sinistre, DoubleSinistre(resultat))
    attendu = prod_sinistre.post("/api/v1/sinistre", json=corps, headers=XFF).json()

    brancher_sinistre(prod_sinistre, DoubleQuiAvance(DoubleSinistre(resultat)))  # type: ignore[arg-type]
    flux = prod_sinistre.post("/api/v1/sinistre/progression", json=corps, headers=XFF)
    assert flux.status_code == 200
    assert flux.headers["content-type"].startswith("text/event-stream")

    lus = evenements(flux.text)
    _controler_avancement(lus, libelle_navigation="Je lis le contrat")
    nom, servi = lus[-1]
    assert nom == "resultat"
    servi["trace"]["request_id"] = attendu["trace"]["request_id"]
    assert servi == attendu
    # La description du sinistre ne paraît nulle part dans les événements d'avancement (AD-10).
    avancement = json.dumps([d for n, d in lus if n in ("etape", "tour")], ensure_ascii=False)
    assert FAITS["description"] not in avancement and QUESTION not in avancement


def test_un_document_qui_nest_pas_un_contrat_reste_un_vrai_400(
        prod_sinistre: TestClient) -> None:  # noqa: F811
    """Les deux refus 400 d'AD-11 sont levés avant le flux : ils ne coûtent aucun appel."""
    reponse = prod_sinistre.post("/api/v1/sinistre/progression",
                                 json=_corps(doc_id="document-qui-nexiste-pas"), headers=XFF)
    assert reponse.status_code == 400
    assert reponse.json()["error"]["code"] == "invalid_request"
