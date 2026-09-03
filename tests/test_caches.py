"""Story 5.6 (T5) — les deux caches de la facture, décidés par Lancelot le 03/09/2026.

Ce que ces témoins tiennent, et rien d'autre : la clé du cache de réponses est **exacte** (hit sur
la même question, miss sur la moindre variation de mot), elle est invalidée par **chacune** des
empreintes qui décident de la réponse, un document en quarantaine ou sans gate n'est jamais servi
du cache, une réponse re-servie porte `via: cache` avec la trace et le coût d'origine, et le
maintien des préfixes ne chauffe que ce qui a été servi, sous son plafond de coût quotidien.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from server.app.api.schemas import VIA_CACHE
from server.app.cache import (
    CacheDeReponses,
    EntreeDeCache,
    MaintienDesPrefixes,
    RegistreDesPrefixes,
    composantes_de_cle,
    document_cachable,
    normaliser_question,
)
from server.app.cache.reponses import CACHE_SCHEMA_VERSION
from server.app.config import PREFIX_CACHE_TTL_S, Settings
from server.app.domain.answer import AbsenceProof, Answer, AnswerSegment
from server.app.domain.trace import Trace
from server.app.llm.models import CACHE_TTL_S, MODEL_CAPS, TIERS


# --- la forme de la question, et rien de plus -----------------------------

@pytest.mark.parametrize("brute", [
    "La foudre est-elle couverte ?",
    "  la   foudre est-elle couverte  ",
    "LA FOUDRE EST-ELLE COUVERTE...",
    "la foudre est-elle couverte\n",
])
def test_la_normalisation_ne_touche_que_les_espaces_la_casse_et_la_ponctuation_finale(brute: str) -> None:
    assert normaliser_question(brute) == "la foudre est-elle couverte"


@pytest.mark.parametrize("a, b", [
    # Les accents distinguent : « ou » et « où » ne sont pas la même question.
    ("où déclarer", "ou declarer"),
    # La ponctuation **interne** distingue : elle porte du sens.
    ("l'eau, la fumée", "l'eau la fumée"),
    # Une faute de frappe est une requête payée. C'est la règle, écrite comme témoin.
    ("la foudre est-elle couverte", "la foudre est-elle couvete"),
    # L'ordre distingue : aucun rapprochement sémantique n'est fait, jamais.
    ("le vol de la voiture", "la voiture du vol"),
])
def test_deux_questions_differentes_restent_deux_questions(a: str, b: str) -> None:
    assert normaliser_question(a) != normaliser_question(b)


# --- doubles minimaux d'état ----------------------------------------------

class _Entree:
    def __init__(self, source_hash: str = "s", ingest_fingerprint: str = "f",
                 overlay_hash: str = "o") -> None:
        self.source_hash = source_hash
        self.ingest_fingerprint = ingest_fingerprint
        self.overlay_hash = overlay_hash


class _Corpus:
    def __init__(self, doc_id: str, *, alerts: tuple[str, ...] = (),
                 quarantine: bool = False, servi: bool = True,
                 entree: _Entree | None = None) -> None:
        self.served = [doc_id] if servi else []
        self.quarantine = {doc_id: "raison"} if quarantine else {}
        self.alerts = {doc_id: alerts} if alerts else {}
        self.manifest = {doc_id: entree or _Entree()}


class _Etat:
    def __init__(self, corpus: _Corpus, *, pipeline: str = "p", prompts: str = "q",
                 settings: Settings | None = None) -> None:
        self.corpus = corpus
        self.pipeline_digest_hex = pipeline
        self.prompts_digest_hex = prompts
        self.settings = settings or Settings(_env_file=None, anthropic_api_key="")


DOC = "mini"


def _etat(**kw: Any) -> _Etat:
    return _Etat(_Corpus(DOC, **kw))


def _composantes(etat: _Etat, question: str = "la foudre est-elle couverte ?",
                 **kw: Any) -> dict[str, Any] | None:
    defauts: dict[str, Any] = {"route": "chat", "doc_id": DOC, "lang": "fr",
                               "variant": "outils", "entree": {"faits": None},
                               "dictionnaire": None}
    return composantes_de_cle(etat=etat, question=question, **{**defauts, **kw})


def _reponse(texte: str = "Une réponse.", cout: float = 0.1234) -> EntreeDeCache:
    # Un refus sourcé : la forme la plus petite d'`Answer` qui soit valide sans corpus. Ce que ces
    # témoins éprouvent est la **clé** et le stockage, jamais le contenu de la réponse.
    answer = Answer(found=False, complete=False, texte=texte,
                    segments=[AnswerSegment(text=texte, kind="limite")],
                    reason=AbsenceProof(kind="zero_hit", terms_searched=["foudre"],
                                        variants_count=0, blocks_scanned=4, documents=[DOC]))
    trace = Trace(request_id="r-origine", pipeline="guide", intent="question",
                  total_cost_eur=cout)
    return EntreeDeCache(answer=answer, trace=trace)


def _cache(tmp_path: Path, **kw: Any) -> CacheDeReponses:
    defauts: dict[str, Any] = {"ttl_s": 604800.0, "max_entries": 200, "max_bytes": 33_554_432}
    return CacheDeReponses(tmp_path / "reponses", **{**defauts, **kw})


# --- hit exact, miss sur variation ----------------------------------------

def test_la_meme_question_rend_la_reponse_deja_payee_avec_sa_trace_dorigine(tmp_path: Path) -> None:
    cache, etat = _cache(tmp_path), _etat()
    cle = cache.cle(_composantes(etat))
    assert cache.lire(cle) is None  # premier passage : miss
    cache.ecrire(cle, _reponse())

    relu = cache.lire(cache.cle(_composantes(etat, question="  LA FOUDRE EST-ELLE COUVERTE  ")))
    assert relu is not None
    assert relu.answer.texte == "Une réponse."
    # La trace **d'origine** : son `request_id` désigne la requête qui a payé, et son
    # `total_cost_eur` est celui de cette requête-là, pas zéro.
    assert relu.trace.request_id == "r-origine" and relu.trace.total_cost_eur == 0.1234
    assert cache.compteurs.hits == 1 and cache.compteurs.misses == 1


def test_une_faute_de_frappe_est_une_requete_payee(tmp_path: Path) -> None:
    cache, etat = _cache(tmp_path), _etat()
    cache.ecrire(cache.cle(_composantes(etat)), _reponse())
    assert cache.lire(cache.cle(_composantes(etat, question="la foudre est-elle couvete ?"))) is None


# --- invalidation par chaque empreinte ------------------------------------

@pytest.mark.parametrize("variation", [
    pytest.param({"entree": {"faits": {"cause": "foudre"}}}, id="faits"),
    pytest.param({"lang": "en"}, id="langue"),
    pytest.param({"variant": "deterministe"}, id="variante"),
    pytest.param({"route": "sinistre"}, id="route"),
    pytest.param({"dictionnaire": object()}, id="dictionnaire"),
])
def test_chaque_composante_de_la_requete_invalide_la_cle(tmp_path: Path,
                                                         variation: dict[str, Any]) -> None:
    cache, etat = _cache(tmp_path), _etat()
    cache.ecrire(cache.cle(_composantes(etat)), _reponse())
    assert cache.lire(cache.cle(_composantes(etat, **variation))) is None


@pytest.mark.parametrize("champ", ["source_hash", "ingest_fingerprint", "overlay_hash"])
def test_chaque_empreinte_du_document_invalide_la_cle(tmp_path: Path, champ: str) -> None:
    cache = _cache(tmp_path)
    cache.ecrire(cache.cle(_composantes(_etat())), _reponse())
    autre = _Etat(_Corpus(DOC, entree=_Entree(**{champ: "different"})))
    assert cache.lire(cache.cle(_composantes(autre))) is None


@pytest.mark.parametrize("digests", [{"pipeline": "autre"}, {"prompts": "autre"}])
def test_le_code_et_les_prompts_invalident_la_cle(tmp_path: Path, digests: dict[str, str]) -> None:
    cache = _cache(tmp_path)
    cache.ecrire(cache.cle(_composantes(_etat())), _reponse())
    autre = _Etat(_Corpus(DOC), **digests)
    assert cache.lire(cache.cle(_composantes(autre))) is None


def test_un_seuil_publie_qui_bouge_invalide_la_cle(tmp_path: Path) -> None:
    """Les seuils entrent dans la clé : un réglage qui change la réponse périme le stock."""
    cache = _cache(tmp_path)
    cache.ecrire(cache.cle(_composantes(_etat())), _reponse())
    autre = _Etat(_Corpus(DOC),
                  settings=Settings(_env_file=None, anthropic_api_key="", quote_min_chars=30))
    assert cache.lire(cache.cle(_composantes(autre))) is None


# --- quarantaine et gate rouge --------------------------------------------

@pytest.mark.parametrize("etat_du_document", [
    pytest.param({"quarantine": True}, id="quarantaine"),
    pytest.param({"alerts": ("sans_gate",)}, id="sans_gate"),
    pytest.param({"alerts": ("gate_perime",)}, id="gate_perime"),
    pytest.param({"alerts": ("source_absente",)}, id="source_absente"),
    pytest.param({"alerts": ("bloquant_statique",)}, id="bloquant_statique"),
    pytest.param({"servi": False}, id="non_servi"),
])
def test_un_document_quon_ne_valide_plus_nest_ni_lu_ni_ecrit(etat_du_document: dict[str, Any]) -> None:
    """`None` vaut « aucun cache » : la route ne lit rien et n'écrit rien pour ce document."""
    assert _composantes(_etat(**etat_du_document)) is None
    assert not document_cachable(_etat(**etat_du_document), DOC)


def test_un_document_servi_et_gate_est_cachable() -> None:
    assert _composantes(_etat()) is not None
    # Une alerte qui ne parle pas du gate n'empêche rien : le document reste validé.
    assert _composantes(_etat(alerts=("dictionnaire_non_valide",))) is not None


# --- TTL, bornes, entrées douteuses ---------------------------------------

def test_une_entree_expiree_est_un_miss_et_disparait(tmp_path: Path) -> None:
    cache = _cache(tmp_path, ttl_s=0.0)
    cle = cache.cle(_composantes(_etat()))
    cache.ecrire(cle, _reponse())
    time.sleep(0.01)
    assert cache.lire(cle) is None
    assert cache.compte_des_entrees() == 0


def test_le_magasin_reste_sous_sa_borne_dentrees(tmp_path: Path) -> None:
    cache = _cache(tmp_path, max_entries=3)
    for i in range(6):
        cache.ecrire(cache.cle(_composantes(_etat(), question=f"question numéro {i}")),
                     _reponse(texte=f"réponse {i}"))
        time.sleep(0.002)  # les entrées s'ordonnent par leur date d'écriture
    assert cache.compte_des_entrees() == 3
    assert cache.compteurs.evictions == 3
    # La plus récente est là, la plus ancienne est partie.
    assert cache.lire(cache.cle(_composantes(_etat(), question="question numéro 5"))) is not None
    assert cache.lire(cache.cle(_composantes(_etat(), question="question numéro 0"))) is None


def test_une_reponse_plus_grande_que_le_magasin_nest_pas_conservee(tmp_path: Path) -> None:
    cache = _cache(tmp_path, max_bytes=4096)
    cle = cache.cle(_composantes(_etat()))
    cache.ecrire(cle, _reponse(texte="x" * 8192))
    assert cache.lire(cle) is None and cache.compteurs.ecritures == 0


@pytest.mark.parametrize("saboter", [
    pytest.param(lambda doc: doc.__setitem__("schema_version", CACHE_SCHEMA_VERSION + 1),
                 id="version_inconnue"),
    pytest.param(lambda doc: doc.__setitem__("cle", "0" * 64), id="cle_incoherente"),
    pytest.param(lambda doc: doc["valeur"].pop("trace"), id="trace_absente"),
    pytest.param(lambda doc: doc.__setitem__("cree_le", "hier"), id="date_non_finie"),
])
def test_une_entree_douteuse_est_un_miss_jamais_une_reponse(tmp_path: Path, saboter: Any) -> None:
    """Fail-closed : on ne lit jamais « au mieux » ce qui ne satisfait pas le contrat persistant."""
    cache = _cache(tmp_path)
    cle = cache.cle(_composantes(_etat()))
    cache.ecrire(cle, _reponse())
    chemin = next((tmp_path / "reponses").rglob("*.json"))
    doc = json.loads(chemin.read_text("utf-8"))
    saboter(doc)
    chemin.write_text(json.dumps(doc), encoding="utf-8")
    assert cache.lire(cle) is None
    assert cache.compteurs.invalides == 1
    assert cache.compte_des_entrees() == 0  # l'entrée douteuse est retirée, pas relue au tour suivant


def test_letat_interne_du_verdict_survit_au_cache(tmp_path: Path) -> None:
    """`Answer._decision_claims` fonde le fil de conversation 3.7 : sans lui, un hit ouvrirait un
    fil sans aucune claim à continuer. Il est conservé et reposé à la relecture."""
    from server.app.domain.verdict import ClaimJugee

    cache = _cache(tmp_path)
    entree = _reponse()
    claim = ClaimJugee(claim_id="c1")
    entree.answer._decision_claims = [claim]
    cle = cache.cle(_composantes(_etat()))
    cache.ecrire(cle, EntreeDeCache(answer=entree.answer, trace=entree.trace,
                                    decision_claims=[claim]))
    relu = cache.lire(cle)
    assert relu is not None
    assert [c.claim_id for c in relu.answer._decision_claims] == ["c1"]


# --- le maintien des préfixes ---------------------------------------------

class _PrefixeFactice:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.model = "m"
        self.system: list[dict[str, Any]] = []
        self.tools = None
        self.output_config = None


class _ClientFactice:
    def __init__(self, cout: float = 0.015, erreur: BaseException | None = None) -> None:
        self.cout = cout
        self.erreur = erreur
        self.appels: list[str] = []

    async def maintenir_prefixe(self, entree: Any, *, timeout_s: float) -> float:
        del timeout_s
        if self.erreur is not None:
            raise self.erreur
        self.appels.append(entree.digest)
        return self.cout


def _maintien(client: _ClientFactice, registre: RegistreDesPrefixes,
              plafond: float = 1.0) -> MaintienDesPrefixes:
    return MaintienDesPrefixes(client=client, registre=registre, intervalle_s=3000.0,
                               max_cout_eur_par_jour=plafond, timeout_s=55.0)


def test_seuls_les_prefixes_deja_servis_sont_maintenus_au_chaud() -> None:
    registre = RegistreDesPrefixes(12)
    client = _ClientFactice()
    maintien = _maintien(client, registre)
    asyncio.run(maintien.un_tour())
    assert client.appels == []  # rien n'a été servi : rien n'est chauffé

    registre.noter("d1", model="m", system=[], tools=None)
    registre.noter("d2", model="m", system=[], tools=None)
    asyncio.run(maintien.un_tour())
    assert client.appels == ["d1", "d2"]
    assert maintien.etat.maintiens == 2 and maintien.etat.prefixes == 2
    assert maintien.etat.cout_cumule_eur == pytest.approx(0.03)


def test_le_registre_refuse_le_nouveau_venu_au_lieu_devincer_un_ancien() -> None:
    registre = RegistreDesPrefixes(2)
    for i in range(5):
        registre.noter(f"d{i}", model="m", system=[], tools=None)
    assert [e.digest for e in registre.entrees()] == ["d0", "d1"]
    assert registre.refuses == 3


def test_le_plafond_du_jour_arrete_le_maintien_et_se_publie() -> None:
    registre = RegistreDesPrefixes(12)
    for i in range(5):
        registre.noter(f"d{i}", model="m", system=[], tools=None)
    client = _ClientFactice(cout=0.4)
    maintien = _maintien(client, registre, plafond=1.0)
    asyncio.run(maintien.un_tour())
    # Trois maintiens à 0,40 € franchissent 1,00 € ; le quatrième n'est pas envoyé.
    assert maintien.etat.maintiens == 3 and maintien.etat.plafond_du_jour_atteint is True
    assert len(client.appels) == 3


def test_un_prefixe_dont_le_ttl_est_plus_court_que_lintervalle_nest_pas_maintenu() -> None:
    """Tous les modèles ne déclarent pas la même durée : « 1h » pour `reason`, « 5m » ailleurs.

    Maintenir un préfixe de cinq minutes toutes les cinquante paierait une **écriture** à chaque
    tour — l'inverse exact de ce que le maintien existe pour éviter. Il est ignoré, et compté comme
    tel : `/sante` ne doit pas laisser croire qu'il est chaud.
    """
    registre = RegistreDesPrefixes(12)
    registre.noter("chaud", model="m", system=[], tools=None, cache_ttl_s=3600.0)
    registre.noter("froid", model="h", system=[], tools=None, cache_ttl_s=300.0)
    client = _ClientFactice()
    maintien = _maintien(client, registre)
    asyncio.run(maintien.un_tour())
    assert client.appels == ["chaud"]
    assert maintien.etat.prefixes == 2 and maintien.etat.ignores == 1


def test_un_maintien_qui_echoue_ne_tue_ni_la_boucle_ni_le_service() -> None:
    registre = RegistreDesPrefixes(12)
    registre.noter("d1", model="m", system=[], tools=None)
    maintien = _maintien(_ClientFactice(erreur=RuntimeError("fournisseur indisponible")), registre)
    asyncio.run(maintien.un_tour())  # ne lève pas
    assert maintien.etat.echecs == 1 and maintien.etat.maintiens == 0


# --- les deux textes qui bornent le maintien ------------------------------

def test_lintervalle_de_maintien_est_derive_du_ttl_reel_du_cache_de_prefixe() -> None:
    """`PREFIX_CACHE_TTL_S` est le `"1h"` de `MODEL_CAPS`, exprimé en secondes.

    `config` ne peut pas importer `llm` (table des couches), donc la valeur y est recopiée. Ce
    témoin est ce qui empêche les deux textes de diverger — et il tient aussi la dérivation :
    l'intervalle par défaut laisse une marge sous l'expiration, il ne la frôle pas.
    """
    assert PREFIX_CACHE_TTL_S == CACHE_TTL_S[str(MODEL_CAPS[TIERS["reason"]]["cache_ttl"])]
    defauts = Settings(_env_file=None, anthropic_api_key="")
    assert defauts.prefix_keepalive_s == 3000.0
    assert PREFIX_CACHE_TTL_S - defauts.prefix_keepalive_s >= 600.0


def test_un_intervalle_qui_depasse_le_ttl_est_refuse_au_demarrage() -> None:
    """Il paierait l'écriture qu'il prétend éviter, à chaque tour. Le refus est avant la facture."""
    with pytest.raises(ValueError, match="prefix_keepalive_s"):
        Settings(_env_file=None, anthropic_api_key="", prefix_keepalive_s=PREFIX_CACHE_TTL_S)


def test_les_deux_caches_ne_sont_jamais_armes_sans_cle_fournisseur(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """La règle de `/sante.ok`, réemployée : un service qui ne peut rien payer n'économise rien.

    Elle a un second effet, voulu : la suite hermétique et toute exécution hors ligne ne laissent
    aucun état sur disque, et deux requêtes identiques y restent deux requêtes.
    """
    from server.app.api.etat import _caches

    class _ClientNu:
        prefix_registry = None

    # `cle_absente` fait foi sur la variable d'environnement **posée**, vide comprise : la suite
    # tourne sous `ANTHROPIC_API_KEY=`, ce qui suffirait à tout éteindre. On la retire pour éprouver
    # les deux branches du réglage, pas celle de la commande.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sans_cle = Settings(_env_file=None, anthropic_api_key="", prefix_keepalive_enabled=True)
    assert _caches(sans_cle, _ClientNu()) == (None, None)

    avec_cle = Settings(_env_file=None, anthropic_api_key="cle-de-test",
                        prefix_keepalive_enabled=True)
    client = _ClientNu()
    cache, maintien = _caches(avec_cle, client)
    assert cache is not None and maintien is not None
    assert client.prefix_registry is not None

    # Le maintien exige en plus son propre réglage, faux par défaut (`--min-instances=1`).
    cache, maintien = _caches(Settings(_env_file=None, anthropic_api_key="cle-de-test"), _ClientNu())
    assert cache is not None and maintien is None


# --- de bout en bout : `via: cache`, coût zéro, et un seul appel de pipeline ---

def test_la_route_sert_la_reponse_du_cache_avec_via_cache_et_sans_rappeler_le_pipeline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le témoin `via` de la story : la seconde requête identique ne rappelle personne.

    Ce qu'il tient, dans l'ordre : le pipeline n'est appelé **qu'une fois**, la seconde réponse porte
    `via: cache` et la trace d'origine avec son `total_cost_eur`, la variation d'un mot repasse par
    le pipeline, et `/sante` publie un hit et deux miss.
    """
    from fastapi.testclient import TestClient

    from server.app.api import etat as etat_mod
    from server.app.api.main import create_app
    from server.app.api.schemas import VIA

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(etat_mod, "CACHE_REPONSES_DIR", tmp_path / "reponses")
    reglages = Settings(_env_file=None, anthropic_api_key="cle-de-test", env="dev",
                        allow_ungated=True)

    appels: list[str] = []
    entree = _reponse()

    async def double(question: str, historique: list, profil: Any, **kw: Any) -> Any:
        appels.append(question)
        return entree.answer, entree.trace.model_copy(update={"request_id": kw["request_id"]})

    entetes = {"X-Forwarded-For": "203.0.113.7"}
    with TestClient(create_app(reglages)) as client:
        etat = client.app.state.foyer
        etat.pipeline = double
        assert etat.cache_reponses is not None
        # Le corpus de ce worktree porte `gate_perime` : son gate a été mesuré sur un autre commit.
        # C'est précisément un gate rouge, donc un document non cachable — ce que le témoin suivant
        # éprouve. Ici on décrit l'autre situation, celle d'un document servi **et** validé par un
        # gate courant, qui est celle du service déployé.
        etat.corpus.alerts = {k: v for k, v in etat.corpus.alerts.items()
                              if k != etat.settings.guide_doc_id}

        premiere = client.post("/api/v1/chat", json={"question": "Où déclarer mon arrivée ?"},
                               headers=entetes).json()
        assert premiere["via"] == VIA and len(appels) == 1

        seconde = client.post("/api/v1/chat", json={"question": "  où DÉCLARER mon arrivée  "},
                              headers=entetes).json()
        assert seconde["via"] == VIA_CACHE, "la réponse re-servie doit se nommer"
        assert len(appels) == 1, "le pipeline a été rappelé alors que la réponse était en cache"
        assert seconde["texte"] == premiere["texte"]
        # La trace publiée est celle de la requête qui a **payé** : son coût d'origine, pas zéro.
        assert seconde["trace"]["total_cost_eur"] == premiere["trace"]["total_cost_eur"] == 0.1234
        assert seconde["trace"]["request_id"] == premiere["trace"]["request_id"]

        # Un mot qui change est une autre question, donc une requête payée.
        client.post("/api/v1/chat", json={"question": "Où déclarer mon départ ?"},
                    headers=entetes).json()
        assert len(appels) == 2

        caches = client.get("/api/v1/sante", headers=entetes).json()["caches"]
        assert caches["reponses"] == {"actif": True, "hits": 1, "misses": 2, "ecritures": 2,
                                      "evictions": 0, "invalides": 0, "entrees": 2}
        # Le maintien n'est pas armé : son réglage est faux par défaut (`--min-instances=1`).
        assert caches["prefixes"]["actif"] is False


def test_un_document_en_quarantaine_nest_jamais_servi_du_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La quarantaine tombe **après** que la réponse a été mise en cache : elle n'est pas re-servie.

    C'est le cas qui compte — un cache constitué pendant que tout allait bien ne doit pas survivre à
    la perte de validation du document, sans quoi la quarantaine ne quarantainerait rien.
    """
    from fastapi.testclient import TestClient

    from server.app.api import etat as etat_mod
    from server.app.api.main import create_app
    from server.app.api.schemas import VIA

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(etat_mod, "CACHE_REPONSES_DIR", tmp_path / "reponses")
    reglages = Settings(_env_file=None, anthropic_api_key="cle-de-test", env="dev",
                        allow_ungated=True)

    appels: list[str] = []
    entree = _reponse()

    async def double(question: str, historique: list, profil: Any, **kw: Any) -> Any:
        appels.append(question)
        return entree.answer, entree.trace.model_copy(update={"request_id": kw["request_id"]})

    entetes = {"X-Forwarded-For": "203.0.113.8"}
    corps = {"question": "Où déclarer mon arrivée ?"}
    with TestClient(create_app(reglages)) as client:
        etat = client.app.state.foyer
        etat.pipeline = double
        etat.corpus.alerts = {k: v for k, v in etat.corpus.alerts.items()
                              if k != etat.settings.guide_doc_id}
        assert client.post("/api/v1/chat", json=corps, headers=entetes).json()["via"] == VIA

        doc_id = etat.settings.guide_doc_id
        etat.corpus.alerts = dict(etat.corpus.alerts) | {doc_id: ("gate_perime",)}
        assert client.post("/api/v1/chat", json=corps, headers=entetes).json()["via"] == VIA
        assert len(appels) == 2, "un document dont le gate est rouge a été servi du cache"
