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


@pytest.mark.parametrize("cle", [
    f"{DOC_ID}:p1",       # une page de contrat n'a pas de questions candidates
    f"{DOC_ID}:f1:1",     # un `block_id` (trois segments) n'est pas un nœud
    "f1",                 # pas de document nommé
    DOC_ID,               # le document entier n'est pas une fiche
    f"{DOC_ID}:f fiche",  # un libellé, pas un identifiant
    f"{DOC_ID}:f1\n",     # `$` acceptait une nouvelle ligne finale ; `fullmatch` ne le fait pas
    "Logement",
])
def test_candidate_questions_ne_peut_nommer_quun_node_id_de_fiche(cle: str) -> None:
    """Story 2.5 : « refuser une clé qui ne serait pas un `node_id` de fiche ».

    De la donnée générée que rien ne lit doit au moins ne pas pouvoir mentir sur ce qu'elle décrit :
    une clé « Logement » ou « lux-guide:f1:1 » traverserait le schéma, se chargerait, et le jour où
    une page proposerait ces questions elle les rattacherait à une fiche qui n'existe pas.
    """
    with pytest.raises(ValueError, match="node_id de fiche"):
        DictionaryFile.model_validate(_fichier(candidate_questions={cle: ["Question ?"]}))


@pytest.mark.parametrize("cle", [f"{DOC_ID}:farrivee", f"{DOC_ID}:q1", f"{DOC_ID}:q41"])
def test_les_deux_sortes_dunites_repondantes_du_guide_sont_des_cles_valides(cle: str) -> None:
    """La fiche **et** l'entrée de FAQ : ce sont les deux seules sortes de nœuds qu'`enrich_dictionary`
    puisse écrire ici (il n'énumère que les enfants directs des catégories de niveau 1), et le fichier
    livré porte 77 clés de la première sorte et 41 de la seconde.

    Refuser les secondes rendrait le dictionnaire **livré** inerte — donc plus de variantes
    multilingues (AC 2.4), plus de court-circuit, `/api/v1/sante` en alerte — pour une clé qui décrit
    pourtant un nœud parfaitement réel du guide. Le contrôle garde ce qu'il doit garder : une clé qui
    ne désigne aucune unité répondante.
    """
    valide = DictionaryFile.model_validate(_fichier(candidate_questions={cle: ["Question ?"]}))
    assert list(valide.candidate_questions) == [cle]


# --- le chargement (corpus) ------------------------------------------------

def test_le_chargement_nominal_arme_les_variantes_mais_pas_le_refus(tmp_path: Path) -> None:
    d = load_dictionary(_ecrire(tmp_path), _corpus(), DOC_ID)
    assert (d.charge, d.corpus_ok, d.validated) == (True, True, False)
    assert d.utilisable is True          # les variantes servent : élargir n'affirme rien (AD-3)
    assert d.court_circuit_actif is False  # le **refus**, lui, attend une signature (AD-5)
    assert d.canoniques == 2


@pytest.mark.parametrize(("cle", "raison"), [
    ("autre:f1", "appartient au document 'autre'"),
    (f"{DOC_ID}:f999", "nœud inexistant"),
])
def test_une_question_candidate_doit_nommer_un_noeud_reel_du_document_demande(
        tmp_path: Path, cle: str, raison: str) -> None:
    """Le schéma garde la forme ; le loader possède le corpus et garde l'existence/la propriété.

    Dans les deux cas le dictionnaire devient inerte avec une raison exploitable par `/sante`, sans
    empêcher le serveur de démarrer ni inventer du contenu pour ce champ encore dormant.
    """
    d = load_dictionary(
        _ecrire(tmp_path, candidate_questions={cle: ["Question ?"]}), _corpus(), DOC_ID)
    assert (d.charge, d.validated, d.corpus_ok, d.court_circuit_actif) == (
        False, False, False, False)
    assert raison in d.raison and cle in d.raison and DOC_ID in d.raison


def test_une_question_candidate_sur_un_noeud_reel_du_document_est_chargee(tmp_path: Path) -> None:
    d = load_dictionary(
        _ecrire(tmp_path, candidate_questions={f"{DOC_ID}:f1": ["Question ?"]}),
        _corpus(), DOC_ID)
    assert d.charge is True and d.corpus_ok is True


def test_un_document_absent_explique_que_le_dictionnaire_nest_pas_applicable(
        tmp_path: Path) -> None:
    """M2, revue 2.5 : l'absence du document précède le contrôle de ses nœuds candidats."""
    d = load_dictionary(_ecrire(tmp_path), Corpus(), DOC_ID)
    assert d.charge is True and d.corpus_ok is False
    assert f"{DOC_ID!r}, qui n'est pas servi" in d.raison
    assert "nœud inexistant" not in d.raison


def test_les_declencheurs_expliquent_une_intention_sans_jamais_la_decider(tmp_path: Path) -> None:
    d = load_dictionary(
        _ecrire(tmp_path, intents={
            "meteo": ["météo", "il fera beau", "weather"],
            "bavardage": ["bonjour"],
        }),
        _corpus(), DOC_ID,
    )

    assert d.declencheurs("meteo") == ("météo", "il fera beau", "weather")
    assert d.declencheurs("hors_perimetre") == ()
    # `normalize()` neutralise accents et casse ; les expressions restent des mots entiers.
    assert d.confirme("meteo", "METEO demain : il fera beau ?") == (2, 3)
    assert d.confirme("meteo", "La météorologie est une science.") == (0, 3)
    assert d.confirme("inconnu", "météo") == (0, 0)


def test_le_court_circuit_exige_la_signature_et_le_bon_corpus(tmp_path: Path) -> None:
    signe = {"validated": True, "validated_by": "Lancelot Oudin",
             "validated_at": "2026-08-25T10:00:00Z"}
    d = load_dictionary(_ecrire(tmp_path, **signe), _corpus(), DOC_ID)
    assert d.court_circuit_actif is True

    # Même signature, autre corpus : ni variantes, ni court-circuit. Un dictionnaire qui décrit un
    # autre corpus ne dit rien de celui-ci.
    perime = load_dictionary(_ecrire(tmp_path, **signe), _corpus(source_hash="autre"), DOC_ID)
    assert (perime.charge, perime.corpus_ok, perime.utilisable, perime.court_circuit_actif) == (
        True, False, False, False)
    assert "source_hash" in perime.raison


@pytest.mark.parametrize("hashes, raison", [
    ({}, "vide"),
    ({"inconnu": SOURCE_HASH}, "ne décrit pas"),
    ({DOC_ID: SOURCE_HASH, "inconnu": SOURCE_HASH}, "n'est pas servi"),
    ({DOC_ID: "autre"}, "source_hash"),
])
def test_corpus_ok_dit_pourquoi_il_est_faux(tmp_path: Path, hashes: dict, raison: str) -> None:
    d = load_dictionary(_ecrire(tmp_path, corpus_source_hashes=hashes), _corpus(), DOC_ID)
    assert d.corpus_ok is False and raison in d.raison


def test_expand_garde_les_termes_de_la_question_comme_cles(tmp_path: Path) -> None:
    """AD-4 : `terms_searched` dit ce que *comprendre* a produit, jamais les canoniques du fichier.

    Publier les clés du dictionnaire ferait fuir, terme par terme, une partie de ce qu'AD-4 interdit
    d'exposer, et dirait à l'utilisateur des mots qu'il n'a pas employés.
    """
    d = load_dictionary(_ecrire(tmp_path), _corpus(), DOC_ID)
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
    d = load_dictionary(_ecrire(tmp_path), _corpus(), DOC_ID)
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
    d = load_dictionary(_ecrire(tmp_path), _corpus(), DOC_ID)
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
        d = load_dictionary(dossier, _corpus(), DOC_ID)
        assert d.utilisable is False and d.court_circuit_actif is False, nom
        assert d.raison, nom
        assert d.expand(["matricule"]) == {"matricule": []}, nom
        assert d.variants_count(["matricule"]) == 0, nom


def test_le_dictionnaire_inerte_est_le_defaut(tmp_path: Path) -> None:
    inerte = Dictionnaire()
    assert (inerte.charge, inerte.validated, inerte.corpus_ok) == (False, False, False)
    assert inerte.utilisable is False and inerte.court_circuit_actif is False
    assert inerte.expand(["x"]) == {"x": []} and inerte.variants_count(["x"]) == 0


def test_une_forme_partagee_par_deux_canoniques_elargit_vers_les_deux(tmp_path: Path) -> None:
    """Revue Codex 2.1 (I1) : garder le premier groupe rendait le second **inatteignable**.

    L'artefact livré porte 62 formes ambiguës. Sous l'ancienne règle, une question employant l'une
    d'elles n'ouvrait jamais la fiche du second canonique — et, dictionnaire signé, ressortait en
    refus « zéro hit » sur un sujet que le guide traite : le « faux refus » qu'AD-5 dit prévenir.
    Élargir vers les deux n'affirme rien (AD-3 vérifie chaque phrase affichée contre le corpus) ;
    l'ordre reste celui du fichier, que l'ingestion écrit trié, donc la réunion est déterministe.
    """
    d = load_dictionary(_ecrire(tmp_path, corpus={"a": ["partagee", "sœur-a"],
                                                  "b": ["partagee", "sœur-b"]}),
                        _corpus(), DOC_ID)
    assert d.expand(["partagee"])["partagee"] == ["a", forme("sœur-a"), "b", forme("sœur-b")]
    # Une forme **non** ambiguë ne gagne rien au passage : seule celle qui relève des deux groupes
    # les réunit.
    assert d.expand(["sœur-a"])["sœur-a"] == ["a", "partagee"]  # non ambiguë : son groupe, seul
    # AD-4 : la preuve d'absence nomme alors les **deux** canoniques, et jamais une variante.
    assert d.canoniser(["partagee"]) == ["a", "b"]


def test_le_verrou_de_corpus_nomme_le_document_quon_va_servir(tmp_path: Path) -> None:
    """Revue Codex 2.1 (B3) : « au moins une empreinte valide » n'est pas « la bonne empreinte ».

    Le corpus sert deux documents ; le dictionnaire ne décrit que le **second**. Sous l'ancienne
    règle il était `corpus_ok`, publié armé par `/sante`, et ses variantes élargissaient la recherche
    du **premier** — un vocabulaire qui ne le décrit pas, et, signé, un refus « zéro hit » armé
    dessus.
    """
    corpus = _corpus()
    autre = _corpus(source_hash="sha-autre")
    corpus.documents["autre"] = autre.documents[DOC_ID].model_copy(update={"doc_id": "autre"})
    corpus.manifest["autre"] = autre.manifest[DOC_ID]

    signe = {"validated": True, "validated_by": "Lancelot Oudin",
             "validated_at": "2026-08-25T10:00:00Z"}
    dossier = _ecrire(tmp_path, corpus_source_hashes={"autre": "sha-autre"},
                      candidate_questions={}, **signe)
    d = load_dictionary(dossier, corpus, DOC_ID)
    assert d.charge is True and d.corpus_ok is False
    assert d.utilisable is False and d.court_circuit_actif is False
    assert "ne décrit pas" in d.raison and DOC_ID in d.raison

    # Le même fichier, appliqué au document qu'il décrit **vraiment**, se charge et arme.
    sien = load_dictionary(dossier, corpus, "autre")
    assert sien.corpus_ok is True and sien.court_circuit_actif is True
    # …et il ne vaut que pour celui-là : un pipeline qui interrogerait un autre document, ou tout le
    # corpus (`doc_id=None`), n'élargit rien et n'arme rien.
    assert sien.utilisable_pour("autre") is True and sien.court_circuit_pour("autre") is True
    for ailleurs in (DOC_ID, None, ""):
        assert sien.utilisable_pour(ailleurs) is False, ailleurs
        assert sien.court_circuit_pour(ailleurs) is False, ailleurs


def test_canoniser_rend_les_canoniques_jamais_la_variante_employee(tmp_path: Path) -> None:
    """AD-4, mot pour mot : `terms_searched[] (canoniques)`.

    Revue Codex 2.1 (B5) : `terms_searched` recopiait les termes de *comprendre*, si bien qu'un terme
    reconnu comme **variante** était publié tel quel — mesuré sur l'artefact livré avec
    « Arbeitsamt », du groupe « ADEM ».
    """
    d = load_dictionary(_ecrire(tmp_path), _corpus(), DOC_ID)
    # Une variante sort sous son canonique…
    assert d.canoniser(["Anmeldung"]) == ["déclaration d'arrivée"]
    # …le canonique reste lui-même, et un terme inconnu du dictionnaire est son propre canonique.
    assert d.canoniser(["matricule", "hippopotame"]) == ["matricule", "hippopotame"]
    # Deux termes du même groupe ne nomment le canonique qu'une fois : la preuve ne bégaie pas.
    assert d.canoniser(["Anmeldung", "residence registration"]) == ["déclaration d'arrivée"]
    # Aucune **variante** ne sort jamais, quel que soit le terme d'entrée.
    variantes = {v for vs in _fichier()["corpus"].values() for v in vs}
    for depart in ("Anmeldung", "residence registration", "numéro national", "hippopotame"):
        assert not (set(d.canoniser([depart])) & variantes), depart


def test_un_dictionnaire_inutilisable_ne_canonise_rien(tmp_path: Path) -> None:
    """Le terme sort inchangé : sans dictionnaire, il **est** le canonique (AD-4)."""
    inerte = Dictionnaire()
    assert inerte.canoniser(["Anmeldung"]) == ["Anmeldung"]
    perime = load_dictionary(_ecrire(tmp_path), _corpus(source_hash="autre"), DOC_ID)
    assert perime.canoniser(["Anmeldung"]) == ["Anmeldung"]


def test_une_version_de_schema_inconnue_rend_le_dictionnaire_inerte(tmp_path: Path) -> None:
    """Revue Codex 2.1 (B4) : `schema_version` était une chaîne libre.

    Un fichier de version 999 — écrit par un outil dont le format aurait changé de sens — se
    chargeait, élargissait la recherche et, signé, armait le refus, interprété avec les règles
    d'aujourd'hui. Une version qu'on ne sait pas lire est un fichier qu'on ne sait pas lire.
    """
    with pytest.raises(ValueError, match="schema_version"):
        DictionaryFile.model_validate(_fichier(schema_version="999"))

    signe = {"validated": True, "validated_by": "Lancelot Oudin",
             "validated_at": "2026-08-25T10:00:00Z"}
    d = load_dictionary(_ecrire(tmp_path, schema_version="999", **signe), _corpus(), DOC_ID)
    assert d.charge is False and d.utilisable is False and d.court_circuit_actif is False
    assert "schema_version" in d.raison  # dit, jamais tu (AD-16) : `/sante` en fait une alerte


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

    **Story 2.5 :** c'est aussi lui qui a mesuré ce que le validateur de `candidate_questions` peut
    exiger. Écrit d'abord « `{doc_id}:f…` seul », il rendait le fichier livré inerte — 41 de ses
    118 clés sont des entrées de FAQ — et éteignait les variantes multilingues de l'AC 2.4 sans que
    le contrat de la story ne demande rien de tel.
    """
    from server.app.config import REPO_ROOT, Settings
    from server.app.corpus.loader import load_corpus

    reglages = Settings(_env_file=None)
    corpus = load_corpus(REPO_ROOT / "data", allow_ungated=True,
                         perimetre_max_chars=reglages.perimetre_max_chars)
    d = load_dictionary(REPO_ROOT / "data", corpus, reglages.guide_doc_id)

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


def test_le_dictionnaire_du_depot_porte_les_declencheurs_que_la_trace_compte() -> None:
    """Story 2.5 : `intents` cesse d'être de la donnée générée que rien ne lit.

    Le fichier livré porte 30 déclencheurs par intention refusée ; c'est ce total que le contrôle
    `intention_expliquee` annonce (« … sur 30 la confirment »). Sans ce test, le champ pourrait
    disparaître de l'artefact sans qu'aucune suite ne rougisse — la situation même dont la reprise
    différée de 2.1 disait qu'elle « finit par mentir en silence ».
    """
    from server.app.config import REPO_ROOT, Settings
    from server.app.corpus.loader import load_corpus

    reglages = Settings(_env_file=None)
    corpus = load_corpus(REPO_ROOT / "data", allow_ungated=True,
                         perimetre_max_chars=reglages.perimetre_max_chars)
    d = load_dictionary(REPO_ROOT / "data", corpus, reglages.guide_doc_id)

    for intent in sorted(INTENTS_DU_DICTIONNAIRE):
        assert d.declencheurs(intent), f"aucun déclencheur pour {intent} dans le fichier livré"
    reconnus, total = d.confirme("meteo", "Quel temps fera-t-il demain ? Y aura-t-il de la MÉTÉO ?")
    assert total == len(d.declencheurs("meteo")) and reconnus >= 1


# --- story 2.5 : les déclencheurs expliquent, ils ne décident jamais --------

def _avec_intents(tmp_path: Path, declencheurs: dict[str, list[str]]) -> Dictionnaire:
    return load_dictionary(_ecrire(tmp_path, intents=declencheurs), _corpus(), DOC_ID)


def test_declencheurs_rend_ce_que_le_fichier_ecrit_et_rien_dautre(tmp_path: Path) -> None:
    """AD-5 : le champ `intents` cesse d'être illisible, sans devenir décisionnaire.

    Une intention absente du fichier rend un tuple vide — le compte vaudra « 0 sur 0 », ce qui est
    vrai, plutôt qu'une valeur devinée (AD-16).
    """
    d = _avec_intents(tmp_path, {"meteo": ["météo", "weather"], "bavardage": ["bonjour"]})
    assert d.declencheurs("meteo") == ("météo", "weather")
    assert d.declencheurs("bavardage") == ("bonjour",)
    assert d.declencheurs("hors_perimetre") == ()
    assert Dictionnaire().declencheurs("meteo") == ()  # le dictionnaire inerte n'explique rien


def test_confirme_compte_les_declencheurs_reconnus_dans_la_question(tmp_path: Path) -> None:
    """Le contrat de `confirme()` : deux nombres, jamais les mots (AD-4, AD-10).

    C'est ce couple que le pipeline transforme en `CheckResult(intention_expliquee)` : « 2
    déclencheur(s) du dictionnaire sur 30 la confirment ». Le second terme est le total connu, pas le
    nombre de formes distinctes essayées — les deux doivent parler du même ensemble.
    """
    d = _avec_intents(tmp_path, {"meteo": ["météo", "pluie", "il fera beau", "weather"]})
    assert d.confirme("meteo", "Quelle météo demain, et de la pluie ?") == (2, 4)
    assert d.confirme("meteo", "Il fera beau ce week-end ?") == (1, 4)
    assert d.confirme("meteo", "Quel délai pour déclarer mon arrivée ?") == (0, 4)
    assert d.confirme("hors_perimetre", "n'importe quoi") == (0, 0)


def test_confirme_neutralise_les_accents_et_la_casse(tmp_path: Path) -> None:
    """Convention Texte du spine : `normalize()` fait le travail, ici comme dans l'index.

    Une seule règle de comparaison dans tout le serveur, sans quoi le compte annoncé à l'utilisateur
    serait un chiffre que rien d'autre ne saurait reproduire.
    """
    d = _avec_intents(tmp_path, {"meteo": ["météo"]})
    for question in ("METEO ?", "Météo ?", "meteo ?", "Quelle  MÉTÉO   demain"):
        assert d.confirme("meteo", question) == (1, 1), question


def test_confirme_exige_le_mot_entier_comme_lindex(tmp_path: Path) -> None:
    """`Index._hit` : correspondance en **mots entiers**, jamais en sous-chaîne.

    Sans cette règle, « météorologie » compterait pour « météo » et le refus paraîtrait corroboré par
    un mot que la recherche du corpus, elle, n'aurait pas reconnu.
    """
    d = _avec_intents(tmp_path, {"meteo": ["météo", "il fera beau"]})
    assert d.confirme("meteo", "Un cours de météorologie ?") == (0, 2)
    assert d.confirme("meteo", "Il fera beau demain") == (1, 2)
    assert d.confirme("meteo", "Beau il fera") == (0, 2)  # les mots, mais pas la suite


def test_deux_orthographes_dun_meme_declencheur_ne_comptent_quune_fois(tmp_path: Path) -> None:
    """Un doublon de forme compterait deux fois dans le total annoncé et une seule dans les reconnus :
    le compte se contredirait lui-même. La déduplication a lieu **au chargement**, sur la forme
    normalisée, et l'ordre du fichier est conservé."""
    d = _avec_intents(tmp_path, {"meteo": ["Météo", "météo", "  MÉTÉO  ", "pluie"]})
    assert d.declencheurs("meteo") == ("Météo", "pluie")
    assert d.confirme("meteo", "la météo") == (1, 2)


def test_les_declencheurs_ne_dependent_daucun_des_deux_verrous_dad5(tmp_path: Path) -> None:
    """Ils n'ouvrent aucune fiche et ne prononcent aucun refus : rien à garder.

    Les deux verrous d'AD-5 protègent l'un les **variantes du corpus**, l'autre le **refus**. Un
    déclencheur d'intention ne fait ni l'un ni l'autre — il ne décrit d'ailleurs pas un corpus mais
    une façon de poser une question. Le désarmer au motif que le fichier décrit un autre document
    cacherait une explication sans rien protéger.
    """
    d = load_dictionary(_ecrire(tmp_path, intents={"meteo": ["météo"]},
                                corpus_source_hashes={DOC_ID: "empreinte-perimee"}),
                        _corpus(), DOC_ID)
    assert d.charge is True and d.utilisable is False and d.court_circuit_actif is False
    assert d.declencheurs("meteo") == ("météo",)   # l'explication reste lisible
    assert d.expand(["matricule"]) == {"matricule": []}  # les variantes, elles, sont bien désarmées


def test_un_dictionnaire_absent_nexplique_rien_et_ne_leve_jamais(tmp_path: Path) -> None:
    """AD-7 : « un fichier absent … désactive une optimisation, il n'empêche jamais de servir »."""
    d = load_dictionary(tmp_path, _corpus(), DOC_ID)
    assert d.charge is False
    assert d.declencheurs("meteo") == () and d.confirme("meteo", "météo") == (0, 0)
