"""Matrice I/O de `python -m server.ingest.enrich_dictionary` (spec 2.1) — **sans réseau**.

Un double du client de batch rejoue des réponses enregistrées : le module ne connaît du SDK que
`messages.batches.create / retrieve / results`, et c'est exactement la surface doublée ici.

Ce que ces cas gardent, et pourquoi c'est ici que ça se garde : le code ne fait **pas confiance** au
modèle d'ingestion (AD-5 / AD-7 / FR29 — « il ne renvoie jamais de texte de bloc »). Les bornes, le
rejet d'une chaîne recopiée d'un bloc, l'agrégation par `custom_id` et le refus de démarrer au-delà
du majorant de coût sont du **code**, donc testables hors ligne — et c'est la seule façon de savoir
qu'ils tiendront le jour où le vrai modèle rendra n'importe quoi.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from server.app.config import Settings
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.dictionary import DictionaryFile
from server.app.domain.document import Block, BlockRef, Document, Node, NodeRef
from server.app.domain.ingest import ManifestEntry
from server.ingest import enrich_dictionary as ed

DOC_ID = "mini"
SOURCE_HASH = "sha-source"
FICHE = f"{DOC_ID}:farrivee"
CAT = f"{DOC_ID}:cat:administratif"
FICHE2 = f"{DOC_ID}:fbail"
CAT2 = f"{DOC_ID}:cat:logement"
PASSAGE = "Vous disposez de huit jours après votre arrivée pour vous déclarer au Biergercenter."


# --- corpus miniature écrit sur disque (le CLI charge `data/` lui-même) ----

def _document(source_hash: str = SOURCE_HASH, *, deux_categories: bool = False) -> Document:
    """Le corpus miniature. `deux_categories` ajoute « Logement » : c'est ce qui permet d'exercer un
    lot **partiellement** perdu (revue Codex 2.1, B2) — une catégorie tombe, l'autre revient."""
    blocks = [Block(block_id=f"{DOC_ID}:farrivee:1", text=PASSAGE, loc="farrivee", seq=1)]
    nodes = [Node(node_id=f"{DOC_ID}:root", title="Mini", items=[NodeRef(node_id=CAT)]),
             Node(node_id=CAT, level=1, title="Administratif", items=[NodeRef(node_id=FICHE)]),
             Node(node_id=FICHE, level=2, title="Les huit premiers jours",
                  items=[BlockRef(block_id=f"{DOC_ID}:farrivee:1")])]
    if deux_categories:
        blocks.append(Block(block_id=f"{DOC_ID}:fbail:1", text="Le bail se signe pour trois ans.",
                            loc="fbail", seq=1))
        nodes[0].items.append(NodeRef(node_id=CAT2))
        nodes += [Node(node_id=CAT2, level=1, title="Logement", items=[NodeRef(node_id=FICHE2)]),
                  Node(node_id=FICHE2, level=2, title="Signer un bail",
                       items=[BlockRef(block_id=f"{DOC_ID}:fbail:1")])]
    return Document(doc_id=DOC_ID, kind="guide", title="Mini", edition="git:test",
                    source_hash=source_hash, ingest_fingerprint="fp", nodes=nodes, blocks=blocks)


SUMMARY = (f"# Mini\n\n## Administratif\n\n- `{FICHE}` · Les huit premiers jours · "
           "Tout part de la commune. · tags : arrivée, commune\n")
SUMMARY_2 = (f"\n## Logement\n\n- `{FICHE2}` · Signer un bail · "
             "Trois ans, résiliation annuelle. · tags : bail, logement\n")


def _ecrire_data(tmp_path: Path, *, deux_categories: bool = False) -> Path:
    """Un `data/` minimal que `load_corpus` sert (hashes cohérents, gate absent + `allow_ungated`)."""
    import hashlib

    from server.ingest.artifacts import document_json

    doc_dir = tmp_path / DOC_ID
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "summary.md").write_text(
        SUMMARY + (SUMMARY_2 if deux_categories else ""), "utf-8")
    (doc_dir / "source.js").write_text("kb = {}\n", "utf-8")
    source_hash = hashlib.sha256((doc_dir / "source.js").read_bytes()).hexdigest()
    texte = document_json(_document(source_hash, deux_categories=deux_categories))
    (doc_dir / "document.json").write_text(texte, "utf-8")
    entree = ManifestEntry(
        status="servi", source_hash=source_hash, ingest_fingerprint="fp",
        document_hash=hashlib.sha256(texte.encode("utf-8")).hexdigest(), edition="git:test")
    (tmp_path / "manifest.json").write_text(
        json.dumps({DOC_ID: entree.model_dump()}, indent=2, ensure_ascii=False), "utf-8")
    return tmp_path


def _source_hash(data: Path) -> str:
    return json.loads((data / "manifest.json").read_text("utf-8"))[DOC_ID]["source_hash"]


def _corpus_en_memoire() -> Corpus:
    doc = _document()
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    return Corpus(documents={DOC_ID: doc},
                  manifest={DOC_ID: ManifestEntry(status="servi", source_hash=SOURCE_HASH,
                                                  ingest_fingerprint="fp", document_hash="d",
                                                  edition="git:test")},
                  summaries={DOC_ID: SUMMARY})


# --- double du client de batch --------------------------------------------

@dataclass
class _Resultat:
    type: str
    message: Any = None


@dataclass
class _Entree:
    custom_id: str
    result: _Resultat


@dataclass
class _Lot:
    id: str = "batch_test"
    processing_status: str = "ended"


def _message(charge: dict, *, output_tokens: int = 500, stop_reason: str = "end_turn") -> Any:
    """Assez d'un `Message` pour ce que le module en lit : `content[].text`, `usage`, `stop_reason`."""
    @dataclass
    class _Bloc:
        type: str
        text: str

    @dataclass
    class _Message:
        content: list
        usage: dict
        stop_reason: str

    return _Message(content=[_Bloc("text", json.dumps(charge, ensure_ascii=False))],
                    usage={"input_tokens": 2000, "output_tokens": output_tokens,
                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                    stop_reason=stop_reason)


@dataclass
class FauxBatches:
    """`create` / `retrieve` / `results` / `cancel` — la seule surface du SDK que le module touche."""

    reponses: dict[str, Any]
    etats: list[str] = field(default_factory=lambda: ["ended"])
    requetes: list[Any] = field(default_factory=list)
    retrieves: int = 0
    # Les `custom_id` dont la réponse est **coupée** (`stop_reason="max_tokens"`) : le modèle a
    # répondu, l'appel est facturé, et le JSON est inutilisable.
    tronquees: set[str] = field(default_factory=set)
    annulations: list[str] = field(default_factory=list)
    annulation_leve: bool = False

    def create(self, *, requests: list) -> _Lot:
        self.requetes = list(requests)
        return _Lot()

    def retrieve(self, batch_id: str) -> _Lot:
        self.retrieves += 1
        etat = self.etats[min(self.retrieves - 1, len(self.etats) - 1)]
        return _Lot(id=batch_id, processing_status=etat)

    def cancel(self, batch_id: str) -> None:
        if self.annulation_leve:
            raise RuntimeError("annulation refusée par l'API")
        self.annulations.append(batch_id)

    def results(self, batch_id: str) -> list[_Entree]:
        out = []
        for custom_id, valeur in self.reponses.items():
            if isinstance(valeur, str):  # un statut d'échec
                out.append(_Entree(custom_id, _Resultat(valeur)))
            else:
                arret = "max_tokens" if custom_id in self.tronquees else "end_turn"
                out.append(_Entree(custom_id,
                                   _Resultat("succeeded", _message(valeur, stop_reason=arret))))
        return out


class FauxClient:
    def __init__(self, batches: FauxBatches) -> None:
        self.messages = type("_M", (), {"batches": batches})()


def _sortie_categorie(**over: Any) -> dict:
    base = {
        "termes": [{"fiche_id": FICHE, "canonique": "déclaration d'arrivée",
                    "variantes": ["Anmeldung", "residence registration"]}],
        "questions": [{"fiche_id": FICHE, "question": "Combien de temps pour me déclarer ?"}],
    }
    return base | over


SORTIE_INTENTS = {"intents": [{"intent": "meteo", "declencheurs": ["météo", "weather"]},
                              {"intent": "inconnu", "declencheurs": ["x"]}]}


def _settings(**kw: Any) -> Settings:
    kw.setdefault("guide_doc_id", DOC_ID)
    kw.setdefault("anthropic_api_key", "sk-test")  # le double du client ne l'emploie jamais
    return Settings(_env_file=None, **kw)


def _lancer(tmp_path: Path, reponses: dict, *, args: list[str] | None = None,
            settings: Settings | None = None, etats: list[str] | None = None,
            dors: list[float] | None = None,
            deux_categories: bool = False) -> tuple[int, str, FauxBatches]:
    """`reponses` = `{custom_id: sortie | statut d'échec}`.

    La requête des intentions répond **par défaut** : depuis la revue Codex 2.1 (B2), un lot dont une
    requête n'a rien donné n'écrit plus rien, et la plupart des cas d'ici portent sur autre chose.
    Pour l'omettre exprès — c'est un cas à part entière —, passer `{"intents": None}`.
    """
    reponses = {ed.CUSTOM_ID_INTENTS: SORTIE_INTENTS} | reponses
    reponses = {k: v for k, v in reponses.items() if v is not None}
    data = _ecrire_data(tmp_path, deux_categories=deux_categories)
    batches = FauxBatches(reponses, etats=etats or ["ended"])
    flux = io.StringIO()
    horloge = iter(range(0, 100000, 30))
    code = ed.main(["--data", str(data), *(args or [])], client=FauxClient(batches),
                   settings=settings or _settings(), sortie=flux,
                   dormir=(dors.append if dors is not None else (lambda s: None)),
                   maintenant=lambda: next(horloge))
    return code, flux.getvalue(), batches


def _lu(tmp_path: Path) -> DictionaryFile:
    return DictionaryFile.model_validate_json((tmp_path / "dictionary.json").read_bytes())


# --- nominal ---------------------------------------------------------------

def test_le_run_nominal_ecrit_un_dictionnaire_conforme_et_jamais_valide(tmp_path: Path) -> None:
    code, texte, batches = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie(),
                                              "intents": SORTIE_INTENTS})

    assert code == 0
    fichier = _lu(tmp_path)
    assert fichier.corpus_source_hashes == {DOC_ID: _source_hash(tmp_path)}
    # AD-5 : la signature est un acte humain. L'ingestion ne l'écrit **jamais**, sous aucune forme.
    assert (fichier.validated, fichier.validated_by, fichier.validated_at) == (False, None, None)
    assert fichier.corpus["déclaration d'arrivée"] == ["Anmeldung", "residence registration"]
    assert fichier.candidate_questions[FICHE] == ["Combien de temps pour me déclarer ?"]
    assert fichier.intents == {"meteo": ["météo", "weather"]}  # `inconnu` écarté par le code
    # La sortie CLI dit ce que l'AC demande : les comptes, le coût réel, les écarts.
    assert "1 canonique(s), 2 variante(s)" in texte
    assert "coût réel :" in texte and "validated=false" in texte
    # Une requête par catégorie, plus une pour les intentions ; `custom_id` = la clé d'agrégation.
    assert [r["custom_id"] for r in batches.requetes] == [ed.custom_id(CAT), ed.CUSTOM_ID_INTENTS]


def test_relancer_ne_produit_aucun_diff_dordre(tmp_path: Path) -> None:
    """AD-7 : l'artefact est committé. Un ordre instable ferait un diff à chaque régénération."""
    reponses = {ed.custom_id(CAT): _sortie_categorie(termes=[
        {"fiche_id": FICHE, "canonique": "matricule", "variantes": ["zzz", "aaa"]},
        {"fiche_id": FICHE, "canonique": "arrivée", "variantes": []}]), "intents": SORTIE_INTENTS}
    _lancer(tmp_path, reponses)
    premier = (tmp_path / "dictionary.json").read_text("utf-8")
    _lancer(tmp_path, reponses)
    assert (tmp_path / "dictionary.json").read_text("utf-8") == premier
    assert list(_lu(tmp_path).corpus) == ["arrivée", "matricule"]
    assert _lu(tmp_path).corpus["matricule"] == ["aaa", "zzz"]


def test_lagregation_se_fait_par_custom_id_jamais_par_position(tmp_path: Path) -> None:
    """L'API ne promet pas l'ordre : apparier par le rang attribuerait les termes d'une catégorie à
    une autre — une erreur silencieuse et indétectable dans le fichier produit."""
    # Les résultats reviennent dans l'ordre inverse des requêtes, et un `custom_id` inconnu s'y glisse.
    code, texte, _ = _lancer(tmp_path, {"intents": SORTIE_INTENTS,
                                        ed.custom_id("mini:cat:fantome"): _sortie_categorie(),
                                        ed.custom_id(CAT): _sortie_categorie()})
    assert code == 0
    assert _lu(tmp_path).intents == {"meteo": ["météo", "weather"]}
    assert "déclaration d'arrivée" in _lu(tmp_path).corpus


# --- les contrôles que le code applique (AD-5 / AD-7 / FR29) ---------------

def test_une_chaine_hors_bornes_est_ecartee_jamais_tronquee(tmp_path: Path) -> None:
    long_terme = "x" * 300
    trop_de_mots = "un terme beaucoup trop long en mots"
    code, texte, _ = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie(termes=[
        {"fiche_id": FICHE, "canonique": long_terme, "variantes": []},
        {"fiche_id": FICHE, "canonique": trop_de_mots, "variantes": []},
        {"fiche_id": FICHE, "canonique": "matricule", "variantes": [long_terme, "numéro national"]},
    ], questions=[{"fiche_id": FICHE, "question": "q" * 500}])})

    assert code == 0  # le reste tient : un écart n'annule pas le run
    fichier = _lu(tmp_path)
    assert list(fichier.corpus) == ["matricule"]
    assert fichier.corpus["matricule"] == ["numéro national"]  # le terme long est **absent**, pas coupé
    assert long_terme[:60] not in json.dumps(fichier.model_dump(), ensure_ascii=False)
    assert fichier.candidate_questions == {}
    assert "terme_trop_long=2" in texte and "terme_trop_de_mots=1" in texte
    assert "question_trop_longue=1" in texte


def test_une_chaine_recopiee_dun_bloc_est_ecartee(tmp_path: Path) -> None:
    """AD-5 / AD-7 / FR29 : le modèle d'ingestion ne renvoie **jamais** de texte de bloc.

    La ligne de partage est `dictionary_term_max_words` : au-dessous, une chaîne présente dans un
    bloc est normale — « allocations familiales » est précisément ce qu'on cherche, et c'est dans le
    guide. Au-dessus, une chaîne qui figure telle quelle dans un bloc est un **passage**.
    """
    code, texte, _ = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie(
        termes=[{"fiche_id": FICHE, "canonique": "arrivée", "variantes": []}],
        questions=[{"fiche_id": FICHE, "question": PASSAGE[:120]},
                   {"fiche_id": FICHE, "question": "Combien de temps pour me déclarer ?"}])})

    assert code == 0
    fichier = _lu(tmp_path)
    assert fichier.candidate_questions[FICHE] == ["Combien de temps pour me déclarer ?"]
    assert "question_recopiee_dun_bloc=1" in texte
    # Le contrôle ne mord pas sur un terme court, même s'il figure mot pour mot dans un bloc.
    assert "arrivée" in fichier.corpus


def test_un_fiche_id_que_la_categorie_ne_contient_pas_est_ecarte(tmp_path: Path) -> None:
    code, texte, _ = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie(
        termes=[{"fiche_id": "mini:finventee", "canonique": "matricule", "variantes": []}],
        questions=[{"fiche_id": "mini:finventee", "question": "Et ça ?"}])})
    assert code == 4  # aucun terme n'a passé les contrôles : rien n'est écrit
    assert not (tmp_path / "dictionary.json").exists()


def test_les_bornes_de_nombre_sappliquent_apres_filtrage(tmp_path: Path) -> None:
    """Sinon une entrée **écartée** aurait consommé un rang, et la borne aurait mordu deux fois."""
    settings = _settings(dictionary_max_variants_per_term=2, dictionary_max_questions_per_fiche=1,
                         dictionary_max_terms_per_fiche=1)
    code, _texte, _ = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie(
        termes=[{"fiche_id": FICHE, "canonique": "matricule",
                 "variantes": ["x" * 300, "a", "b", "c"]},
                {"fiche_id": FICHE, "canonique": "arrivée", "variantes": []}],
        questions=[{"fiche_id": FICHE, "question": "q1"}, {"fiche_id": FICHE, "question": "q2"}])},
        settings=settings)
    assert code == 0
    fichier = _lu(tmp_path)
    assert fichier.corpus == {"matricule": ["a", "b"]}
    assert fichier.candidate_questions == {FICHE: ["q1"]}


def test_une_sortie_hors_schema_est_ignoree_et_dite(tmp_path: Path, capsys: Any) -> None:
    code, _texte, _ = _lancer(tmp_path, {ed.custom_id(CAT): {"pas": "le schéma"},
                                         "intents": SORTIE_INTENTS})
    assert code == 4  # aucun terme n'a survécu : rien n'est écrit
    assert "non conforme au schéma" in capsys.readouterr().err


# --- les échecs de batch (AD-16 : dit, jamais tu) --------------------------

@pytest.mark.parametrize("statut", ["errored", "expired", "canceled"])
def test_une_requete_en_echec_nannule_pas_les_autres(tmp_path: Path, capsys: Any, statut: str) -> None:
    """L'échec est **dit** (AD-16) et n'annule pas les résultats des autres requêtes…

    …mais le fichier, lui, n'est **pas** écrit : voir
    `test_un_lot_incomplet_nest_jamais_ecrit_comme_un_dictionnaire_complet`. Ce cas garde la moitié
    « les autres sont conservées » : l'agrégation a bien tourné sur ce qui est revenu.
    """
    code, _texte, _ = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie(), "intents": statut})
    assert code == 4
    erreur = capsys.readouterr().err
    assert f"intents : {statut}" in erreur
    assert "lot incomplet" in erreur and ed.CUSTOM_ID_INTENTS in erreur


@pytest.mark.parametrize("panne", [
    "errored",                                  # la requête a échoué côté API
    None,                                       # aucun résultat pour ce `custom_id`
    {"pas": "le schéma"},                       # sortie non conforme
    {"termes": [], "questions": []},            # conforme, mais aucun canonique pour la catégorie
])
def test_un_lot_incomplet_nest_jamais_ecrit_comme_un_dictionnaire_complet(
        tmp_path: Path, capsys: Any, panne: Any) -> None:
    """Revue Codex 2.1 (B2) : le seul garde-fou était `--limit`.

    Une catégorie perdue — requête `errored`/`expired`, réponse absente, tronquée, hors schéma, ou
    dont aucun terme ne passe les contrôles — laissait écrire un dictionnaire portant l'empreinte
    **entière** du corpus, avec un code 0. Ce fichier était signable, et armait alors le refus
    « zéro hit » sur les fiches des catégories absentes : un faux refus par construction, et
    exactement ce que l'écriture inerte d'un run `--limit` évite depuis la revue coordonnée.

    Rien n'est écrit : le dictionnaire déjà commité — éventuellement signé par une main — reste en
    place, et relancer est la seule suite. Le cas est ici exercé **sur une catégorie**, pas seulement
    sur la requête des intentions.
    """
    data = _ecrire_data(tmp_path, deux_categories=True)
    (data / "dictionary.json").write_text("dictionnaire précédent", "utf-8")

    # « Logement » revient normalement ; « Administratif » tombe. Sans le contrôle, le fichier
    # s'écrivait avec l'empreinte entière du corpus et un code 0.
    code, _texte, _ = _lancer(tmp_path, {
        ed.custom_id(CAT): panne,
        ed.custom_id(CAT2): _sortie_categorie(
            termes=[{"fiche_id": FICHE2, "canonique": "bail", "variantes": ["lease"]}],
            questions=[])}, deux_categories=True)

    assert code == 4
    assert (data / "dictionary.json").read_text("utf-8") == "dictionnaire précédent"
    erreur = capsys.readouterr().err
    assert "lot incomplet" in erreur and ed.custom_id(CAT) in erreur
    assert ed.custom_id(CAT2) not in erreur  # celle qui a répondu n'est pas mise en cause


def test_aucun_resultat_exploitable_sort_en_code_4_sans_rien_ecrire(tmp_path: Path) -> None:
    code, _texte, _ = _lancer(tmp_path, {ed.custom_id(CAT): "errored", "intents": "expired"})
    assert code == 4 and not (tmp_path / "dictionary.json").exists()


def test_le_lot_qui_ne_termine_jamais_sort_en_code_4(tmp_path: Path) -> None:
    dors: list[float] = []
    code, _texte, batches = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie()},
                                    etats=["in_progress"], dors=dors,
                                    settings=_settings(dictionary_batch_timeout_s=60.0))
    assert code == 4 and not (tmp_path / "dictionary.json").exists()
    assert dors and dors[0] == _settings().dictionary_batch_poll_s
    assert batches.retrieves >= 2  # il a bien attendu avant d'abandonner


def test_lattente_sarrete_des_que_le_lot_est_termine(tmp_path: Path) -> None:
    dors: list[float] = []
    code, _texte, batches = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie()},
                                    etats=["in_progress", "in_progress", "ended"], dors=dors)
    assert code == 0 and batches.retrieves == 3 and len(dors) == 2


# --- le majorant de coût (AD-1, AD-9) --------------------------------------

def test_le_majorant_depasse_ne_soumet_aucun_batch(tmp_path: Path) -> None:
    data = _ecrire_data(tmp_path)
    batches = FauxBatches({})
    flux = io.StringIO()
    code = ed.main(["--data", str(data), "--max-cost", "0.0001"], client=FauxClient(batches),
                   settings=_settings(), sortie=flux)
    assert code == 3
    assert batches.requetes == [] and not (tmp_path / "dictionary.json").exists()


def test_le_majorant_se_regle_aussi_par_config(tmp_path: Path) -> None:
    """Convention Seuils : `--max-cost` ne fait que **surcharger** `dictionary_max_cost_eur`."""
    data = _ecrire_data(tmp_path)
    batches = FauxBatches({})
    code = ed.main(["--data", str(data)], client=FauxClient(batches),
                   settings=_settings(dictionary_max_cost_eur=0.0001), sortie=io.StringIO())
    assert code == 3 and batches.requetes == []


def test_dry_run_montre_le_plan_et_ne_soumet_rien(tmp_path: Path) -> None:
    data = _ecrire_data(tmp_path)
    batches = FauxBatches({})
    flux = io.StringIO()
    code = ed.main(["--data", str(data), "--dry-run"], client=FauxClient(batches),
                   settings=_settings(), sortie=flux)
    assert code == 0
    assert batches.requetes == [] and not (tmp_path / "dictionary.json").exists()
    texte = flux.getvalue()
    assert ed.custom_id(CAT) in texte and ed.CUSTOM_ID_INTENTS in texte and "majorant" in texte


def test_le_majorant_applique_lescompte_du_batch() -> None:
    corpus = _corpus_en_memoire()
    settings = _settings()
    reqs = ed.requetes(corpus, DOC_ID, settings)
    from server.app.llm.pricing import BATCH_DISCOUNT, estimate_cost

    plein = sum(estimate_cost(r["params"]["model"], r["params"]["system"], r["params"]["messages"],
                              r["params"]["max_tokens"], settings,
                              output_schema=r["params"]["output_config"]["format"])
                for r in reqs)
    assert ed.majorant_eur(reqs, settings) == pytest.approx(plein * BATCH_DISCOUNT, rel=1e-3)


def test_limit_borne_le_nombre_de_categories() -> None:
    corpus = _corpus_en_memoire()
    reqs = ed.requetes(corpus, DOC_ID, _settings(), limit=0)
    assert [r["custom_id"] for r in reqs] == [ed.CUSTOM_ID_INTENTS]  # les intentions restent : elles ne
    # dépendent d'aucune catégorie, et les retirer ferait un dictionnaire sans déclencheurs


# --- la forme des requêtes (AD-9) ------------------------------------------

def test_les_requetes_partent_au_tier_ingest_avec_son_effort_et_sa_borne_de_sortie() -> None:
    settings = _settings()
    reqs = ed.requetes(_corpus_en_memoire(), DOC_ID, settings)
    from server.app.llm.models import EFFORT, TIERS

    for r in reqs:
        p = r["params"]
        assert p["model"] == TIERS["ingest"]
        assert p["output_config"]["effort"] == EFFORT["ingest"]
        assert p["max_tokens"] == settings.dictionary_max_output_tokens
        assert p["output_config"]["format"]["type"] == "json_schema"


def test_le_prompt_de_categorie_annonce_les_seuils_de_config() -> None:
    """Convention Seuils : une borne annoncée au modèle est un seuil de `config.py`, jamais un
    nombre écrit dans le prompt — sinon la surcharge ne change que la moitié du système."""
    settings = _settings(dictionary_term_max_chars=42, dictionary_max_variants_per_term=3)
    systeme = ed.requetes(_corpus_en_memoire(), DOC_ID, settings)[0]["params"]["system"][0]["text"]
    assert "42 caractères" in systeme and "jusqu'à 3 formes" in systeme


def test_le_prompt_des_intentions_recoit_le_perimetre_du_corpus() -> None:
    """Un déclencheur d'intention n'est pas un mot du corpus (AD-5) : le modèle doit savoir ce qu'il
    ne faut **pas** déclencher, et cette liste vient du corpus, pas d'une phrase écrite à la main."""
    systeme = ed.requetes(_corpus_en_memoire(), DOC_ID, _settings())[-1]["params"]["system"][0]["text"]
    assert "- Administratif : Les huit premiers jours" in systeme


def test_aucun_texte_de_bloc_ne_part_au_modele() -> None:
    """AD-5 / FR29 : le modèle d'ingestion ne voit que des titres, des résumés et des tags."""
    for r in ed.requetes(_corpus_en_memoire(), DOC_ID, _settings()):
        envoye = json.dumps(r["params"], ensure_ascii=False)
        assert PASSAGE not in envoye


# --- la validation humaine (AD-5) ------------------------------------------

def _dictionnaire_ecrit(tmp_path: Path) -> None:
    """Un run nominal, donc un `dictionary.json` **non** validé : c'est ce que `--valider` reprend."""
    _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie(), "intents": SORTIE_INTENTS})


def test_valider_ecrit_trois_champs_et_rien_dautre(tmp_path: Path) -> None:
    _dictionnaire_ecrit(tmp_path)
    avant = _lu(tmp_path)
    data = _ecrire_data(tmp_path)
    code = ed.main(["--data", str(data), "--valider", "Lancelot Oudin"],
                   client=None, settings=_settings(), sortie=io.StringIO())

    assert code == 0
    apres = _lu(tmp_path)
    assert apres.validated is True and apres.validated_by == "Lancelot Oudin"
    assert apres.validated_at.endswith("Z") and len(apres.validated_at) == 20
    # « trois champs, et rien d'autre » : tout le reste est identique, octet pour octet.
    assert apres.model_dump(exclude={"validated", "validated_by", "validated_at"}) == \
        avant.model_dump(exclude={"validated", "validated_by", "validated_at"})


@pytest.mark.parametrize("hashes", [{DOC_ID: "empreinte-dun-autre-corpus"}, {}, {"autre": "x"}])
def test_valider_sur_un_corpus_perime_necrit_rien(tmp_path: Path, hashes: dict) -> None:
    """Signer un dictionnaire périmé armerait un refus sur un vocabulaire qui ne décrit pas ce qui
    est servi. Le corpus, lui, reste parfaitement sain : c'est le **fichier** qui a vieilli."""
    _dictionnaire_ecrit(tmp_path)
    fichier = json.loads((tmp_path / "dictionary.json").read_text("utf-8"))
    fichier["corpus_source_hashes"] = hashes
    avant = json.dumps(fichier, ensure_ascii=False)
    (tmp_path / "dictionary.json").write_text(avant, "utf-8")

    code = ed.main(["--data", str(tmp_path), "--valider", "Lancelot Oudin"],
                   client=None, settings=_settings(), sortie=io.StringIO())

    assert code == 5
    assert (tmp_path / "dictionary.json").read_text("utf-8") == avant


def test_valider_refuse_un_dictionnaire_qui_decrit_un_autre_document_servi(tmp_path: Path,
                                                                           capsys: Any) -> None:
    """Revue Codex 2.1 (B3), versant CLI : « les empreintes sont valides » n'est pas « la bonne ».

    Le contrôle comparait les empreintes déclarées à celles des documents servis **qu'elles
    nomment** : un dictionnaire ne décrivant que le contrat AXA passait donc, et `--valider` sortait
    en code 0 avec « le refus « zéro hit » est armé » — alors que le serveur, lui, le refuse
    (`corpus_ok`). Deux lecteurs, deux réponses, et la CLI affirmait la fausse.
    """
    corpus = _corpus_en_memoire()
    corpus.documents["axa"] = corpus.documents[DOC_ID]
    corpus.manifest["axa"] = ManifestEntry(status="servi", source_hash="sha-du-contrat",
                                           ingest_fingerprint="fp", document_hash="d",
                                           edition="git:test")
    chemin = tmp_path / "dictionary.json"
    fichier = {"schema_version": "1", "corpus_source_hashes": {"axa": "sha-du-contrat"},
               "corpus": {"franchise": ["deductible"]}, "intents": {}, "candidate_questions": {},
               "validated": False, "validated_by": None, "validated_at": None}
    avant = json.dumps(fichier, ensure_ascii=False)
    chemin.write_text(avant, "utf-8")

    code = ed.valider_a_la_main(chemin, corpus, "Lancelot Oudin", DOC_ID, sortie=io.StringIO())

    assert code == 5 and chemin.read_text("utf-8") == avant
    assert "ne décrit pas le corpus servi" in capsys.readouterr().err
    # …et sur le document qu'il décrit vraiment, la signature est due et acceptée.
    assert ed.valider_a_la_main(chemin, corpus, "Lancelot Oudin", "axa", sortie=io.StringIO()) == 0


def test_valider_sans_dictionnaire_sort_en_code_5(tmp_path: Path) -> None:
    data = _ecrire_data(tmp_path)
    code = ed.main(["--data", str(data), "--valider", "Nom"], client=None, settings=_settings(),
                   sortie=io.StringIO())
    assert code == 5 and not (tmp_path / "dictionary.json").exists()


# --- pas de clé, pas de document -------------------------------------------

def test_sans_cle_rien_nest_ecrit(tmp_path: Path, capsys: Any) -> None:
    data = _ecrire_data(tmp_path)
    code = ed.main(["--data", str(data)], client=None, settings=_settings(anthropic_api_key=""),
                   sortie=io.StringIO())
    assert code == 2 and not (tmp_path / "dictionary.json").exists()
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_un_document_non_servi_refuse_avant_toute_soumission(tmp_path: Path, capsys: Any) -> None:
    data = _ecrire_data(tmp_path)
    batches = FauxBatches({})
    code = ed.main(["--data", str(data), "--doc-id", "absent"], client=FauxClient(batches),
                   settings=_settings(), sortie=io.StringIO())
    assert code == 2 and batches.requetes == []
    assert "non servi" in capsys.readouterr().err


def test_les_prompts_dingestion_vivent_hors_de_prompts_digest(tmp_path: Path) -> None:
    """Design Note 2.1 : les mettre sous `server/app/llm/prompts/` les ferait entrer dans
    `prompts_digest`, et modifier une consigne d'ingestion rendrait les **deux** gates `gate_perime`
    pour un prompt que le serveur n'exécute jamais.

    La preuve se fait sur une arborescence **jetable** : la version précédente faisait un `.touch()`
    sur un fichier source suivi par git, ce qui modifie son `mtime` dans l'arbre de travail — un test
    n'a rien à écrire dans le dépôt, fût-ce une date (revue coordonnée 2.1). Ce qui compte n'est de
    toute façon pas le fichier réel mais la **règle** : `prompts_digest` ne couvre que
    `llm/prompts/`, et les consignes d'ingestion sont ailleurs. Les deux moitiés sont vérifiées —
    que le dossier réel est bien hors périmètre, et que le périmètre est bien ce qu'on croit.
    """
    from server.app.digests import PROMPTS_DIR, prompts_digest

    # 1. Le dossier réel des consignes d'ingestion n'est pas sous celui des prompts du serveur.
    assert ed.PROMPTS_DIR.is_dir() and not ed.PROMPTS_DIR.is_relative_to(PROMPTS_DIR)
    assert list(ed.PROMPTS_DIR.glob("*.md"))  # …et il porte bien les consignes

    # 2. `prompts_digest` ne regarde que son propre dossier : un `.md` ajouté **à côté** ne le change
    #    pas, un `.md` modifié **dedans** le change. Joué sur `tmp_path`, jamais sur le dépôt.
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "commun.md").write_text("consigne du serveur\n", encoding="utf-8")
    avant = prompts_digest(prompts)
    voisin = tmp_path / "ingest_prompts"
    voisin.mkdir()
    (voisin / "enrich_dictionary.md").write_text("consigne d'ingestion\n", encoding="utf-8")
    assert prompts_digest(prompts) == avant
    (prompts / "commun.md").write_text("consigne du serveur, modifiée\n", encoding="utf-8")
    assert prompts_digest(prompts) != avant


# --- ce que l'API exige, et que seul un appel réel avait dit ----------------

def test_chaque_custom_id_respecte_le_motif_de_lapi() -> None:
    """Mesuré : `cat:lux-guide:cat:administratif` ressort en 400 `String should match pattern
    '^[a-zA-Z0-9_-]{1,64}$'`. Les `node_id` du corpus portent des deux-points — ils ne peuvent pas
    servir de `custom_id` tels quels, et rien hors ligne ne le disait."""
    from server.app.config import REPO_ROOT
    from server.app.corpus.loader import load_corpus

    settings = Settings(_env_file=None)
    corpus = load_corpus(REPO_ROOT / "data", allow_ungated=True)
    reqs = ed.requetes(corpus, settings.guide_doc_id, settings)
    assert len(reqs) > 1
    for r in reqs:
        assert ed.CUSTOM_ID_RE.match(r["custom_id"]), r["custom_id"]


def test_les_custom_id_sont_uniques_et_deterministes() -> None:
    """Deux requêtes de même `custom_id` rendraient l'agrégation ambiguë — donc les termes d'une
    catégorie attribuables à une autre, en silence."""
    reqs = ed.requetes(_corpus_en_memoire(), DOC_ID, _settings())
    ids = [r["custom_id"] for r in reqs]
    assert len(set(ids)) == len(ids)
    assert ed.custom_id(CAT) == ed.custom_id(CAT)
    # Le slug seul ne suffit pas : deux `node_id` distincts peuvent le partager, l'empreinte les sépare.
    assert ed.custom_id("a:b") != ed.custom_id("a-b")
    assert ed.CUSTOM_ID_RE.match(ed.custom_id("x" * 200))


def test_une_variable_posee_mais_vide_empeche_toute_soumission(tmp_path: Path,
                                                               monkeypatch: Any, capsys: Any) -> None:
    """Mesuré : `ANTHROPIC_API_KEY= uv run python -m server.ingest.enrich_dictionary` retombait sur le
    `.env` du poste et **a réellement soumis** un lot. `Settings` laisse tomber une variable vide
    (`env_ignore_empty=True`) ; la règle d'AD-14 — la variable posée fait foi, vide comprise — vit
    désormais dans `config.cle_absente` et vaut pour les deux commandes qui la promettent."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    data = _ecrire_data(tmp_path)
    # Les réglages, eux, portent une clé (comme `.env` en porterait une) : c'est bien la variable
    # posée qui doit trancher, pas le repli.
    code = ed.main(["--data", str(data)], client=None, settings=_settings(anthropic_api_key="sk-x"),
                   sortie=io.StringIO())
    assert code == 2 and not (tmp_path / "dictionary.json").exists()
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


# --- revue coordonnée 2.1 : ce que le premier tour laissait passer ----------

@pytest.mark.parametrize("nom", ["", "   ", "\t"])
def test_valider_refuse_un_nom_vide_et_necrit_rien(tmp_path: Path, nom: str, capsys: Any) -> None:
    """`model_copy(update=…)` **ne rejoue aucun validateur** : `--valider ""` écrivait
    `validated: true, validated_by: ""` — que `DictionaryFile` interdit et que `load_dictionary`
    refusait ensuite en bloc. Le dépôt se retrouvait avec un dictionnaire signé et illisible, donc un
    serveur qui n'élargissait plus rien, pour un espace de trop dans une commande."""
    _dictionnaire_ecrit(tmp_path)
    avant = (tmp_path / "dictionary.json").read_text("utf-8")

    code = ed.main(["--data", str(tmp_path), "--valider", nom], client=None,
                   settings=_settings(), sortie=io.StringIO())

    assert code == 5
    assert (tmp_path / "dictionary.json").read_text("utf-8") == avant
    assert "validé par personne" in capsys.readouterr().err


def test_ce_que_valider_ecrit_est_relisible_par_le_serveur(tmp_path: Path) -> None:
    """Rien d'invalide n'atteint le disque : la copie signée est revalidée avant l'écriture."""
    _dictionnaire_ecrit(tmp_path)
    code = ed.main(["--data", str(tmp_path), "--valider", "  Lancelot Oudin  "], client=None,
                   settings=_settings(), sortie=io.StringIO())
    assert code == 0
    fichier = _lu(tmp_path)                       # `DictionaryFile` relit ce qui a été écrit
    assert fichier.validated_by == "Lancelot Oudin"  # le nom est nettoyé, pas recopié tel quel
    # …et le serveur le lit comme un dictionnaire armé, sur le corpus de ce dossier.
    from server.app.corpus.dictionary import load_dictionary
    from server.app.corpus.loader import load_corpus

    corpus = load_corpus(tmp_path, allow_ungated=True)
    assert load_dictionary(tmp_path, corpus, DOC_ID).court_circuit_actif is True


def test_une_reponse_tronquee_est_une_requete_en_echec(tmp_path: Path, capsys: Any) -> None:
    """`stop_reason == "max_tokens"` : la sortie est **coupée**, donc le JSON est invalide.

    Sans ce contrôle, `agreger` se contentait d'une plainte sur stderr et une catégorie entière
    disparaissait du dictionnaire avec un code de sortie 0. Le cas n'est pas théorique : la
    catégorie « Questions fréquentes » du guide porte 41 fiches. Depuis la revue Codex 2.1 (B2), la
    catégorie tronquée manque au lot, donc **rien n'est écrit** : c'est la seconde moitié du même
    défaut — nommer l'échec ne suffisait pas si le fichier partait quand même.
    """
    data = _ecrire_data(tmp_path, deux_categories=True)
    batches = FauxBatches({ed.custom_id(CAT): _sortie_categorie(),
                           ed.custom_id(CAT2): _sortie_categorie(
                               termes=[{"fiche_id": FICHE2, "canonique": "bail", "variantes": []}],
                               questions=[]),
                           "intents": SORTIE_INTENTS})
    batches.tronquees = {ed.custom_id(CAT)}
    code = ed.main(["--data", str(data)], client=FauxClient(batches), settings=_settings(),
                   sortie=io.StringIO())

    assert code == 4 and not (data / "dictionary.json").exists()
    erreur = capsys.readouterr().err
    assert "stop_reason" in erreur and "max_tokens" in erreur
    assert "lot incomplet" in erreur and ed.custom_id(CAT) in erreur


def test_toutes_les_requetes_tronquees_ne_laissent_rien_a_ecrire(tmp_path: Path) -> None:
    batches = FauxBatches({ed.custom_id(CAT): _sortie_categorie()})
    batches.tronquees = {ed.custom_id(CAT)}
    data = _ecrire_data(tmp_path)
    code = ed.main(["--data", str(data)], client=FauxClient(batches), settings=_settings(),
                   sortie=io.StringIO())
    assert code == 4 and not (tmp_path / "dictionary.json").exists()


def test_un_lot_abandonne_est_annule(tmp_path: Path, capsys: Any) -> None:
    """Un lot qu'on cesse d'attendre continue de tourner **et d'être facturé**."""
    batches = FauxBatches({ed.custom_id(CAT): _sortie_categorie()}, etats=["in_progress"])
    data = _ecrire_data(tmp_path)
    code = ed.main(["--data", str(data)], client=FauxClient(batches),
                   settings=_settings(dictionary_batch_timeout_s=60.0), sortie=io.StringIO(),
                   dormir=lambda s: None, maintenant=iter(range(0, 100000, 30)).__next__)

    assert code == 4 and batches.annulations == ["batch_test"]
    erreur = capsys.readouterr().err
    assert "annulé" in erreur and "results(" in erreur   # …et de quoi rattraper ce qui existe


def test_une_annulation_qui_echoue_nefface_pas_la_cause(tmp_path: Path, capsys: Any) -> None:
    """L'annulation est un secours : son échec se dit, il ne remplace pas la raison de l'abandon."""
    batches = FauxBatches({ed.custom_id(CAT): _sortie_categorie()}, etats=["in_progress"])
    batches.annulation_leve = True
    data = _ecrire_data(tmp_path)
    code = ed.main(["--data", str(data)], client=FauxClient(batches),
                   settings=_settings(dictionary_batch_timeout_s=60.0), sortie=io.StringIO(),
                   dormir=lambda s: None, maintenant=iter(range(0, 100000, 30)).__next__)

    assert code == 4
    erreur = capsys.readouterr().err
    assert "n'a pas terminé" in erreur and "annulation a échoué" in erreur
    assert "il est encore facturé" in erreur


def test_un_run_limit_produit_un_dictionnaire_inerte(tmp_path: Path) -> None:
    """**Un run partiel ne se déclare pas complet.** `corpus_source_hashes` écrit en entier après un
    `--limit` produisait un dictionnaire de deux catégories sur dix qui passait `_corpus_ok`, passait
    `--valider`, et armait le refus « zéro hit » sur les huit autres — un faux refus par
    construction, sur un fichier que rien ne distinguait d'un dictionnaire complet."""
    code, texte, _ = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie()},
                             args=["--limit", "1"])

    assert code == 0
    fichier = _lu(tmp_path)
    assert fichier.corpus_source_hashes == {}
    assert fichier.corpus  # les termes sont bien là : le fichier est utilisable pour une mise au point
    assert "run **partiel**" in texte and "inerte" in texte

    # Inerte pour le serveur…
    from server.app.corpus.dictionary import load_dictionary
    from server.app.corpus.loader import load_corpus

    corpus = load_corpus(tmp_path, allow_ungated=True)
    d = load_dictionary(tmp_path, corpus, DOC_ID)
    assert d.charge is True and d.corpus_ok is False and d.utilisable is False
    # …et `--valider` le refuse, donc il ne peut pas armer un refus par mégarde.
    assert ed.main(["--data", str(tmp_path), "--valider", "Nom"], client=None,
                   settings=_settings(), sortie=io.StringIO()) == 5


def test_laide_de_limit_dit_que_le_fichier_est_inerte(capsys: Any) -> None:
    """Le comportement se lit dans `--help` : personne ne devrait le découvrir sur un run servi."""
    with pytest.raises(SystemExit):
        ed.main(["--help"], client=None, settings=_settings(), sortie=io.StringIO())
    assert "inerte" in capsys.readouterr().out


def test_un_canonique_duplique_est_compte_comme_un_ecart(tmp_path: Path) -> None:
    """Le total des écarts est ce que `docs/tests-live.md` consigne comme preuve que les contrôles
    ont écarté quelque chose : un rejet qui n'y entre pas rend cette preuve fausse par omission."""
    code, texte, _ = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie(termes=[
        {"fiche_id": FICHE, "canonique": "matricule", "variantes": ["a"]},
        {"fiche_id": FICHE, "canonique": "matricule", "variantes": ["b"]}])},
        settings=_settings(dictionary_max_terms_per_fiche=5))
    assert code == 0
    assert _lu(tmp_path).corpus == {"matricule": ["a"]}   # le premier gagne, l'ordre est déterministe
    assert "canonique_duplique=1" in texte


def test_un_intent_inconnu_est_compte_comme_un_ecart(tmp_path: Path) -> None:
    code, texte, _ = _lancer(tmp_path, {ed.custom_id(CAT): _sortie_categorie(),
                                        "intents": SORTIE_INTENTS})
    assert code == 0 and "intent_inconnu=1" in texte
