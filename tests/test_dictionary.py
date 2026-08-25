"""Matrice I/O du dictionnaire enrichi (spec 2.1, AD-5 / AD-7) : le schéma du fichier et son chargement.

Deux objets, deux responsabilités : `domain/dictionary.DictionaryFile` dit ce qu'est un fichier
valide (et il est partagé par l'ingestion qui écrit et par `corpus` qui lit) ; `corpus/dictionary`
en fait l'objet d'exécution, avec ses **deux verrous** — `corpus_ok` commande les variantes,
`validated ∧ corpus_ok` commande le court-circuit d'AD-5.

Rien ici ne touche au réseau ni à `data/dictionary.json` : tout est écrit dans un `tmp_path`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.app.corpus.dictionary import Dictionnaire, forme, load_dictionary
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.dictionary import INTENTS_DU_DICTIONNAIRE, DictionaryFile
from server.app.domain.document import Block, BlockRef, Document, Node
from server.app.domain.ingest import ManifestEntry
from server.app.pipelines.commun import INTENTS_REFUSES

DOC_ID = "mini"
SOURCE_HASH = "sha-source"


def _corpus(source_hash: str = SOURCE_HASH) -> Corpus:
    blocks = [Block(block_id=f"{DOC_ID}:f1:1", text="Le matricule est délivré par la commune.",
                    loc="f1", seq=1)]
    doc = Document(doc_id=DOC_ID, kind="guide", title="Mini", edition="git:test",
                   nodes=[Node(node_id=f"{DOC_ID}:f1", level=1, title="Administratif",
                               items=[BlockRef(block_id=f"{DOC_ID}:f1:1")])],
                   blocks=blocks)
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    manifest = {DOC_ID: ManifestEntry(status="servi", source_hash=source_hash,
                                      ingest_fingerprint="fp", document_hash="sha-doc",
                                      edition="git:test")}
    return Corpus(documents={DOC_ID: doc}, manifest=manifest, summaries={DOC_ID: "# Mini"})


def _fichier(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1",
        "corpus_source_hashes": {DOC_ID: SOURCE_HASH},
        "corpus": {"déclaration d'arrivée": ["Anmeldung", "residence registration", "déclarer arrivée"],
                   "matricule": ["numéro national", "Sozialversicherungsnummer"]},
        "intents": {"meteo": ["météo", "weather"]},
        "candidate_questions": {f"{DOC_ID}:f1": ["Combien de temps pour me déclarer ?"]},
        "validated": False, "validated_by": None, "validated_at": None,
    }
    return base | over


def _ecrire(tmp_path: Path, **over: Any) -> Path:
    (tmp_path / "dictionary.json").write_text(
        json.dumps(_fichier(**over), ensure_ascii=False), encoding="utf-8")
    return tmp_path


# --- le schéma du fichier (domain) ----------------------------------------

def test_le_schema_accepte_exactement_les_huit_champs_dad5() -> None:
    fichier = DictionaryFile.model_validate(_fichier())
    assert set(DictionaryFile.model_fields) == {
        "schema_version", "corpus_source_hashes", "corpus", "intents", "candidate_questions",
        "validated", "validated_by", "validated_at"}
    assert fichier.validated is False


def test_un_champ_de_plus_nest_pas_un_dictionnaire_un_peu_enrichi() -> None:
    """`extra="forbid"` : la spec interdit d'ajouter au schéma ce qu'AD-5 et l'AC n'énumèrent pas.

    Lire à moitié un fichier qu'on ne comprend pas armerait un refus sur des règles qu'on n'a pas
    lues — et le refus est ce qu'AD-5 protège par une signature humaine.
    """
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        DictionaryFile.model_validate(_fichier(gate="oui"))


@pytest.mark.parametrize("valeur", ["true", "false", 1, 0, None])
def test_seul_le_true_json_strict_compte_pour_validated(valeur: Any) -> None:
    """Revue Codex 1.6, M1, portée jusqu'au schéma : `bool("false")` vaut `True`.

    Sans `StrictBool`, pydantic convertirait la chaîne `"true"` d'un fichier bricolé à la main en
    `True` et ré-armerait le court-circuit « zéro hit » qu'AD-5 tient désactivé sans signature.
    """
    with pytest.raises(ValueError):
        DictionaryFile.model_validate(_fichier(validated=valeur))


def test_un_valide_par_personne_est_une_contradiction() -> None:
    with pytest.raises(ValueError, match="validated_by"):
        DictionaryFile.model_validate(_fichier(validated=True))
    with pytest.raises(ValueError, match="validated_by"):
        DictionaryFile.model_validate(_fichier(validated=True, validated_by="  ",
                                               validated_at="2026-08-25T10:00:00Z"))
    signe = DictionaryFile.model_validate(_fichier(validated=True, validated_by="Lancelot Oudin",
                                                   validated_at="2026-08-25T10:00:00Z"))
    assert signe.validated is True


@pytest.mark.parametrize("valeur", ["hier", "2026-08-25", "25/08/2026", "2026-08-25T10:00:00",
                                    "2026-08-25T10:00:00+02:00", "maintenant"])
def test_validated_at_doit_etre_une_vraie_date_utc(valeur: str) -> None:
    """AD-5 nomme une date **UTC ISO 8601** ; « non vide » n'en fait pas une date.

    Sans ce contrôle, `validated_at: "hier"` traversait le schéma, le serveur et le smoke : la
    signature humaine aurait porté une date que rien ne sait relire, donc rien ne sait recouper —
    alors que c'est le rôle des trois champs de rendre le geste vérifiable. Une heure **locale**
    (`+02:00`) est refusée pour la même raison : elle ne se compare à rien sans savoir d'où elle
    vient (revue coordonnée 2.1).
    """
    with pytest.raises(ValueError, match="validated_at"):
        DictionaryFile.model_validate(_fichier(validated=True, validated_by="Nom",
                                               validated_at=valeur))


@pytest.mark.parametrize("valeur", ["2026-08-25T10:00:00Z", "2026-08-25T10:00:00+00:00",
                                    "2026-08-25T10:00:00.123456Z"])
def test_les_formes_utc_iso_8601_sont_acceptees(valeur: str) -> None:
    signe = DictionaryFile.model_validate(_fichier(validated=True, validated_by="Nom",
                                                   validated_at=valeur))
    assert signe.validated_at == valeur


def test_les_intents_sont_bornes_aux_trois_que_le_pipeline_refuse() -> None:
    """AD-5 : `question` et `suivi` ne se refusent pas — leur donner des déclencheurs n'a aucun sens.

    Et les deux tables — celle du schéma et celle de la **décision** (`pipelines/commun`) — doivent
    dire la même chose : le domaine ne peut pas importer un pipeline (table des couches), c'est donc
    ce test qui les tient ensemble.
    """
    assert INTENTS_DU_DICTIONNAIRE == INTENTS_REFUSES
    with pytest.raises(ValueError, match="intents"):
        DictionaryFile.model_validate(_fichier(intents={"question": ["école"]}))


# --- le chargement (corpus) ------------------------------------------------

def test_le_chargement_nominal_arme_les_variantes_mais_pas_le_refus(tmp_path: Path) -> None:
    d = load_dictionary(_ecrire(tmp_path), _corpus())
    assert (d.charge, d.corpus_ok, d.validated) == (True, True, False)
    assert d.utilisable is True          # les variantes servent : élargir n'affirme rien (AD-3)
    assert d.court_circuit_actif is False  # le **refus**, lui, attend une signature (AD-5)
    assert d.canoniques == 2


def test_le_court_circuit_exige_la_signature_et_le_bon_corpus(tmp_path: Path) -> None:
    signe = {"validated": True, "validated_by": "Lancelot Oudin",
             "validated_at": "2026-08-25T10:00:00Z"}
    d = load_dictionary(_ecrire(tmp_path, **signe), _corpus())
    assert d.court_circuit_actif is True

    # Même signature, autre corpus : ni variantes, ni court-circuit. Un dictionnaire qui décrit un
    # autre corpus ne dit rien de celui-ci.
    perime = load_dictionary(_ecrire(tmp_path, **signe), _corpus(source_hash="autre"))
    assert (perime.charge, perime.corpus_ok, perime.utilisable, perime.court_circuit_actif) == (
        True, False, False, False)
    assert "source_hash" in perime.raison


@pytest.mark.parametrize("hashes, raison", [
    ({}, "vide"),
    ({"inconnu": SOURCE_HASH}, "n'est pas servi"),
    ({DOC_ID: "autre"}, "source_hash"),
])
def test_corpus_ok_dit_pourquoi_il_est_faux(tmp_path: Path, hashes: dict, raison: str) -> None:
    d = load_dictionary(_ecrire(tmp_path, corpus_source_hashes=hashes), _corpus())
    assert d.corpus_ok is False and raison in d.raison


def test_expand_garde_les_termes_de_la_question_comme_cles(tmp_path: Path) -> None:
    """AD-4 : `terms_searched` dit ce que *comprendre* a produit, jamais les canoniques du fichier.

    Publier les clés du dictionnaire ferait fuir, terme par terme, une partie de ce qu'AD-4 interdit
    d'exposer, et dirait à l'utilisateur des mots qu'il n'a pas employés.
    """
    d = load_dictionary(_ecrire(tmp_path), _corpus())
    expansion = d.expand(["matricule", "inconnu"])
    assert list(expansion) == ["matricule", "inconnu"]
    assert expansion["inconnu"] == []
    assert set(expansion["matricule"]) == {forme("numéro national"), forme("Sozialversicherungsnummer")}
    # Les formes rendues sont **normalisées** : ce sont celles qu'`Index.chercher` comparera.
    assert all(f == forme(f) for f in expansion["matricule"])


def test_une_variante_retrouve_le_canonique_et_ses_soeurs(tmp_path: Path) -> None:
    """Le terme de la question est une variante, pas le canonique — dans les quatre langues.

    Le cas **anglais** est écrit à part et en toutes lettres : c'est celui que l'AC nomme, et les
    trois autres pourraient passer sans lui (revue coordonnée 2.1).
    """
    d = load_dictionary(_ecrire(tmp_path), _corpus())
    for depart in ("Anmeldung", "residence registration", "déclarer arrivée"):
        ajoutees = d.expand([depart])[depart]
        assert forme("déclaration d'arrivée") in ajoutees, depart
        assert forme(depart) not in ajoutees, depart  # le terme cherché n'est pas sa propre variante
    # Le chemin de l'AC, littéralement : une forme **anglaise** ramène le canonique français et ses
    # sœurs des autres langues, donc tout ce qui permet d'ouvrir la fiche.
    anglais = d.expand(["residence registration"])["residence registration"]
    assert set(anglais) == {forme("déclaration d'arrivée"), forme("Anmeldung"),
                            forme("déclarer arrivée")}


def test_variants_count_compte_des_formes_ajoutees_distinctes(tmp_path: Path) -> None:
    d = load_dictionary(_ecrire(tmp_path), _corpus())
    assert d.variants_count(["matricule"]) == 2
    assert d.variants_count(["inconnu"]) == 0
    # Une variante qui est déjà l'un des termes cherchés n'ajoute rien : elle serait cherchée de
    # toute façon, et l'annoncer gonflerait le chiffre montré à l'utilisateur.
    assert d.variants_count(["matricule", "numéro national"]) == 1
    # Les deux canoniques ensemble : 3 + 2 formes ajoutées, aucune partagée.
    assert d.variants_count(["matricule", "déclaration d'arrivée"]) == 5


def test_un_dictionnaire_inutilisable_nelargit_rien_et_ne_leve_jamais(tmp_path: Path) -> None:
    """AD-7 : absent, illisible ou non conforme ⇒ inerte. `expand` rend alors les termes seuls,
    donc `chercher` fait exactement ce qu'il faisait avant cette story."""
    cas = {
        "absent": None,
        "illisible": b"pas du json",
        "non conforme": b'{"schema_version": 1}',
        "champ en trop": json.dumps(_fichier(gate="oui")).encode(),
        "validated en chaine": json.dumps(_fichier(validated="true")).encode(),
    }
    for nom, contenu in cas.items():
        dossier = tmp_path / nom.replace(" ", "_")
        dossier.mkdir()
        if contenu is not None:
            (dossier / "dictionary.json").write_bytes(contenu)
        d = load_dictionary(dossier, _corpus())
        assert d.utilisable is False and d.court_circuit_actif is False, nom
        assert d.raison, nom
        assert d.expand(["matricule"]) == {"matricule": []}, nom
        assert d.variants_count(["matricule"]) == 0, nom


def test_le_dictionnaire_inerte_est_le_defaut(tmp_path: Path) -> None:
    inerte = Dictionnaire()
    assert (inerte.charge, inerte.validated, inerte.corpus_ok) == (False, False, False)
    assert inerte.utilisable is False and inerte.court_circuit_actif is False
    assert inerte.expand(["x"]) == {"x": []} and inerte.variants_count(["x"]) == 0


def test_une_forme_partagee_par_deux_canoniques_garde_le_premier_groupe(tmp_path: Path) -> None:
    """Élargir vers les deux mêlerait deux sens et ferait ouvrir des fiches que la question ne vise
    pas. L'ordre est celui du fichier, que l'ingestion écrit trié : la décision est déterministe."""
    d = load_dictionary(_ecrire(tmp_path, corpus={"a": ["partagee"], "b": ["partagee"]}), _corpus())
    assert d.expand(["partagee"])["partagee"] == ["a"]


def test_forme_est_exactement_celle_que_lindex_compare() -> None:
    """Code Map 2.1 : « toute comparaison de terme passe par `normalize()` et `words()`, et par elles
    seules ». Deux tables qui ne produiraient pas la même clé feraient annoncer des variantes que
    rien n'a cherchées."""
    from server.app.corpus.index import words

    for texte in ("Déclaration d'ARRIVÉE", "  numéro   national ", "L'Anmeldung !"):
        assert forme(texte) == " ".join(words(normalize(texte)))


# --- le dictionnaire **livré**, confronté au corpus **livré** ----------------

def test_le_dictionnaire_du_depot_decrit_le_corpus_du_depot() -> None:
    """Le seul test qui regarde `data/dictionary.json` et `data/manifest.json` tels qu'ils sont commités.

    **Ce qu'il attrape, et que rien d'autre ne voyait** (revue coordonnée 2.1) : tous les autres cas
    de `corpus_ok` fabriquent leur fichier dans un `tmp_path`. Vérifié en remplaçant les
    `corpus_source_hashes` du fichier livré par une empreinte bidon : la suite restait entièrement
    verte. C'est exactement ce que produit une réingestion du guide sans relance de l'enrichissement
    — et l'élargissement d'AD-5 s'éteindrait alors sans laisser de trace, puisque le
    `CheckResult(dictionnaire)` de *retrouver* n'est posé **que** si le dictionnaire est utilisable.

    Il est du même genre que `test_les_seuils_cites_en_commentaire_valent_les_defauts_de_settings` :
    deux textes du dépôt font autorité sur le même fait, et rien ne les tenait ensemble.
    """
    from server.app.config import REPO_ROOT, Settings
    from server.app.corpus.loader import load_corpus

    reglages = Settings(_env_file=None)
    corpus = load_corpus(REPO_ROOT / "data", allow_ungated=True,
                         perimetre_max_chars=reglages.perimetre_max_chars)
    d = load_dictionary(REPO_ROOT / "data", corpus)

    assert d.charge is True, (
        f"data/dictionary.json ne se charge pas : {d.raison} — relancer "
        "`uv run python -m server.ingest.enrich_dictionary`")
    assert d.corpus_ok is True, (
        f"data/dictionary.json décrit un autre corpus que celui qui est livré : {d.raison} — "
        "relancer `uv run python -m server.ingest.enrich_dictionary` (le guide a été réingéré sans "
        "que l'enrichissement le suive : les variantes s'éteindraient en silence)")
    assert d.utilisable is True
    assert d.canoniques > 0
    # La signature humaine, elle, reste due — c'est un input attendu **après** la story (AD-5), et
    # ce test ne doit pas se mettre à l'exiger : il vérifie l'accord des artefacts, pas le geste.
    assert d.court_circuit_actif is d.validated
