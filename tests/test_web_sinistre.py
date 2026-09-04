"""Matrice d'E/S du front de l'outil sinistre (spec 1.9) : AD-11, AD-15, AD-16, AD-6, AD-4, D5–D8.

Le harnais `tests/js/sinistre_cases.mjs` charge `tools/sinistre/sinistre.js` dans un `node:vm` avec
`window`, `location`, `document` (le DOM minimal) et `fetch` **doublés**, exécute les cas et écrit du
JSON. Il ne juge rien : tout ce qui est affirmé l'est ici. Aucun navigateur, aucun réseau, aucune
dépendance ajoutée à `pyproject.toml` — le harnais n'utilise que la bibliothèque standard de Node.

Le front du guide (1.7) n'était vérifié par rien avant sa revue ; celui-ci l'est dès l'écriture, et
sur les deux moitiés à la fois — la **composition** (l'arbre décidé) et la **matérialisation** (le
DOM produit). Ce que même un DOM en mémoire ne voit pas — tailles, contrastes, débordements, un vrai
lecteur d'écran — reste vérifié en Chrome headless (`docs/tests-live.md`) et en 1.11.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, get_args

import pytest

from server.app.api.schemas import SinistreRequest
from server.app.config import REPO_ROOT, Settings
from server.app.domain.question import Faits
from server.app.domain.verdict import PORTEE, VerdictValue

HARNAIS = REPO_ROOT / "tests" / "js" / "sinistre_cases.mjs"
HARNAIS_INGESTION = REPO_ROOT / "tests" / "js" / "ingestion_cases.mjs"
PAGE = REPO_ROOT / "tools" / "sinistre" / "index.html"
PAGE_INGESTION = REPO_ROOT / "tools" / "sinistre" / "ingestion.html"
SCRIPT = REPO_ROOT / "tools" / "sinistre" / "sinistre.js"

# Comme pour le front du guide : sans `node`, ces cas passent en `skip`, et un `skip` est
# indiscernable d'un succès dans un `pytest -q`. `FRONT_TESTS_REQUIS=1` (CI, story 1.11) en fait un
# échec — une image sans `node` ne peut plus faire passer la suite au vert en taisant le front.
REQUIS = os.environ.get("FRONT_TESTS_REQUIS", "") not in ("", "0")


def _lancer(harnais: Path) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        motif = ("node absent de la machine : les cas du front sinistre ne peuvent pas tourner "
                 "(l'image de CI de la story 1.11 en a un)")
        if REQUIS:
            pytest.fail("FRONT_TESTS_REQUIS=1 mais " + motif)
        pytest.skip(motif)
    # `check=False` explicite : le code de retour est **une assertion du test** deux lignes plus bas,
    # avec le `stderr` du harnais en message. Une `CalledProcessError` nue le perdrait.
    fini = subprocess.run([node, str(harnais)], capture_output=True, text=True, timeout=120,
                          cwd=str(REPO_ROOT), check=False)
    assert fini.returncode == 0, f"le harnais a échoué ({fini.returncode}) :\n{fini.stderr}"
    charge = json.loads(fini.stdout) if fini.stdout.strip() else {"ok": False, "erreur": fini.stderr}
    assert charge.get("ok") is True, charge.get("erreur")
    return charge["cas"]


@pytest.fixture(scope="module")
def cas() -> dict[str, Any]:
    """Le harnais est exécuté **une fois** pour tout le module ; chaque test lit un relevé."""
    return _lancer(HARNAIS)


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cas_ingestion() -> dict[str, Any]:
    return _lancer(HARNAIS_INGESTION)


@pytest.fixture(scope="module")
def page_ingestion() -> str:
    return PAGE_INGESTION.read_text(encoding="utf-8")


# --- story 3.5 : audit d'ingestion ---------------------------------------

def test_audit_affiche_checks_gate_empreintes_et_json_dans_lordre(
        cas_ingestion: dict[str, Any]) -> None:
    audit = cas_ingestion["servi"]
    assert audit["appels"] == [
        "/api/v1/sante", "/api/v1/documents", "/api/v1/documents/cg-mini/report"]
    assert audit["borne_ms"] == 7000
    assert audit["lignes"] == [
        "premierinfo<b>détail littéral</b>", "secondbloquantfin"]
    for fait in ("source-123", "ingest-456", "doc-789", "vertical", "2026-08-26"):
        assert fait in audit["texte"]
    assert audit["brut"] == "/api/v1/documents/cg-mini/report"
    assert audit["busy"] == "false"
    assert audit["avertissements"] == [
        "Ce gate est périmé : il ne valide pas le code actuellement servi."]


def test_audit_affiche_alerte_gate_globale_et_indisponibilite_de_sante(
        cas_ingestion: dict[str, Any]) -> None:
    assert cas_ingestion["alerte_gate_globale"] == [
        "Le service refuse globalement la dérogation sans gate en production."]
    assert cas_ingestion["etat_gate_indisponible"] == [
        "L'état courant des gates n'a pas pu être vérifié ; ce gate reste un fait historique."]


def test_audit_rend_les_chaines_hostiles_comme_texte_et_filtre_la_source(
        cas_ingestion: dict[str, Any]) -> None:
    servi = cas_ingestion["servi"]
    assert "<img onerror=alert(1)>" in servi["titre"]
    assert "<b>détail littéral</b>" in servi["texte"]
    assert servi["scripts"] == 0
    assert servi["source"] == {
        "href": "https://example.invalid/cg.pdf",
        "target": "_blank",
        "rel": "noopener noreferrer",
    }
    quarantaine = cas_ingestion["quarantaine"]
    assert "<script>alerte</script>" in quarantaine["texte"]
    assert all("gs://" not in lien for lien in quarantaine["liens"])


def test_audit_distingue_absence_liste_vide_et_valeur_vide(
        cas_ingestion: dict[str, Any]) -> None:
    quarantaine = cas_ingestion["quarantaine"]
    assert quarantaine["appels"] == ["/api/v1/sante", "/api/v1/documents"]
    assert "rapport d'ingestion est absent" in quarantaine["texte"]
    assert "Aucun contrôle dans ce rapport" in cas_ingestion["rapport_vide"]
    assert cas_ingestion["valeurs"] == {
        "absente": "indisponible", "vide": "valeur vide", "faux": "non",
        "doc_id": "cg-mini", "doc_id_invalide": None,
        "timeout_max": 2_147_483_647,
    }


def test_audit_distingue_rapport_illisible_et_etranger(cas_ingestion: dict[str, Any]) -> None:
    assert cas_ingestion["illisible"]["appels"] == ["/api/v1/sante", "/api/v1/documents"]
    assert "présent mais illisible" in cas_ingestion["illisible"]["texte"]
    assert cas_ingestion["etranger"]["appels"] == ["/api/v1/sante", "/api/v1/documents"]
    assert "décrit un autre document" in cas_ingestion["etranger"]["texte"]


def test_audit_termine_tous_les_etats_derreur_et_de_vide_dans_le_dom(
        cas_ingestion: dict[str, Any]) -> None:
    attendus = {
        "rapport_vide_dom": "Aucun contrôle dans ce rapport",
        "rapport_erreur_dom": "rapport reçu est illisible",
        "document_inconnu": "n'est pas connu du loader",
        "rapport_inaccessible": "validé au démarrage n'a pas pu être consulté",
        "documents_non_tableau": "liste des documents n'a pas pu être chargée",
    }
    for nom, texte in attendus.items():
        releve = cas_ingestion[nom]
        assert texte in releve["texte"], nom
        assert releve["busy"] == "false", nom


def test_audit_reserve_lactualite_pour_edition_valeur_vide_ou_absente(
        cas_ingestion: dict[str, Any]) -> None:
    editions = cas_ingestion["editions_page"]
    assert len(editions) == 3
    assert "juin 2017 — actualité non vérifiée" in editions[0]
    assert "valeur vide — actualité non vérifiée" in editions[1]
    assert "indisponible — actualité non vérifiée" in editions[2]


def test_quarantaine_ne_presente_pas_le_manifest_comme_validation_courante(
        cas_ingestion: dict[str, Any]) -> None:
    texte = cas_ingestion["quarantaine"]["texte"]
    assert "manifest observé au démarrage" in texte
    assert "ne constituent pas une validation courante" in texte
    assert "loader ne les a pas corroborés" not in texte


def test_audit_ne_materialise_aucune_url_locale_privee_ou_malformee(
        cas_ingestion: dict[str, Any]) -> None:
    assert cas_ingestion["urls"] == [
        "https://example.invalid/cg.pdf", None, None, None, None, None, None, None, None]
    assert cas_ingestion["liens_urls"] == [1, 0, 0, 0, 0, 0, 0, 0, 0]


def test_audit_distingue_echec_de_rendu_et_echec_de_liste(
        cas_ingestion: dict[str, Any]) -> None:
    rendu = cas_ingestion["rendu_en_echec"]
    assert "données ont été chargées" in rendu["texte"]
    assert "liste des documents" not in rendu["texte"]
    assert rendu["busy"] == "false"


def test_page_audit_demarre_reellement_immediate_ou_sur_domcontentloaded(
        cas_ingestion: dict[str, Any]) -> None:
    immediat = cas_ingestion["autostart_complete"]
    differe = cas_ingestion["autostart_loading"]
    assert immediat["avant"] == 1
    assert differe["avant"] == 0
    attendus = ["/api/v1/sante", "/api/v1/documents", "/api/v1/documents/cg-mini/report"]
    assert immediat["appels"] == attendus
    assert differe["appels"] == attendus
    assert immediat["busy"] == differe["busy"] == "false"
    assert "premierinfo" in immediat["texte"] and "premierinfo" in differe["texte"]


def test_audit_borne_une_requete_sans_fin_et_quitte_letat_charge(
        cas_ingestion: dict[str, Any]) -> None:
    timeout = cas_ingestion["timeout"]
    assert timeout["borne_ms"] == 0  # injection du harnais : aucune attente réelle
    assert timeout["appels"] == 1
    assert timeout["busy"] == "false"
    assert "n'a pas pu être chargée" in timeout["texte"]


def test_audit_replie_sur_la_marge_par_defaut_si_sante_ne_la_fournit_pas(
        cas_ingestion: dict[str, Any]) -> None:
    """M2 : sonde en échec, seuil absent ou invalide gardent une borne unique et testée.

    Story 5.6 (T3) : le seuil lu est `client_probe_timeout_s` et non plus `client_abort_margin_s`.
    Trois GET qui répondent en millisecondes n'ont jamais eu à emprunter la patience qu'un appel de
    pipeline mérite — l'emprunt tenait parce que les deux valaient 10 s, et il a cessé de tenir
    quand la marge d'abandon est devenue une fonction du `--timeout` de Cloud Run (150 s).
    """
    attendu = round(Settings.model_fields["client_probe_timeout_s"].default * 1000)
    assert attendu == 10000
    appels = ["/api/v1/sante", "/api/v1/documents", "/api/v1/documents/cg-mini/report"]
    for nom in ("sante_echec", "seuil_absent", "seuil_invalide"):
        cas = cas_ingestion[nom]
        assert cas == {"borne_ms": attendu, "appels": appels, "busy": "false"}


def test_page_audit_est_autonome_responsive_et_semantique(page_ingestion: str) -> None:
    assert '<meta name="viewport"' in page_ingestion
    assert '<main id="rapport" aria-live="polite"' in page_ingestion
    assert 'src="/sinistre/ingestion.js?v=1"' in page_ingestion
    assert "web/app/" not in page_ingestion
    assert "<noscript>" in page_ingestion
    assert "Ce rapport ne peut pas être chargé sans JavaScript" in page_ingestion


# --- AD-12 : une seule origine -------------------------------------------

def test_lapi_est_lorigine_de_la_page(cas: dict[str, Any]) -> None:
    """Aucune URL en dur : le serveur qui sert la page sert l'API (AD-12, aucun CORS)."""
    assert cas["api_base"] == "https://foyer-retour.example"
    assert cas["corps_url"] == "https://foyer-retour.example/api/v1/sinistre"
    assert cas["documents_url"] == "https://foyer-retour.example/api/v1/documents"


def test_les_routes_sont_celles_de_la_v1_sans_alias(cas: dict[str, Any]) -> None:
    """AD-11 : l'outil sinistre n'a pas d'alias historique — il poste sur `/api/v1/`."""
    assert "/api/v1/" in cas["corps_url"]
    assert "/api/v1/" in cas["documents_url"]


def test_une_page_hors_ligne_ne_poste_rien(cas: dict[str, Any]) -> None:
    """`file://` : aucun serveur à joindre, donc aucune requête — et la page le dit."""
    assert cas["hors_ligne"]["api_base"] == ""
    assert cas["hors_ligne"]["appels_reseau"] == 0
    assert cas["hors_ligne"]["code"] == "hors_ligne"
    assert "fichier local" in cas["hors_ligne"]["message"]


# --- AD-11 : le corps posté ----------------------------------------------

def test_le_corps_porte_exactement_les_champs_du_contrat(cas: dict[str, Any]) -> None:
    """AD-11 : `{doc_id, question, faits{date?, lieu?, montant_eur?, description}}`, et rien d'autre."""
    corps = cas["corps"]
    assert set(corps) == {"doc_id", "question", "faits"}
    assert set(corps["faits"]) == {"description", "date", "lieu", "montant_eur"}
    assert corps["faits"]["montant_eur"] == 1200
    assert cas["corps_methode"] == "POST"
    assert cas["corps_entetes"]["Content-Type"] == "application/json"
    # Les champs du corps sont **exactement** ceux que le schéma du serveur accepte — depuis la
    # revue Codex 1.9 (I3), il les **exige** (`extra="forbid"`) : un champ en trop est un 400, et
    # ce test est ce qui empêche la page de le découvrir en production. `lang` reste à son défaut.
    assert set(corps) <= set(SinistreRequest.model_fields)


def test_le_dossier_nest_jamais_envoye(cas: dict[str, Any]) -> None:
    """D1 : la page ne déclare rien du paquet contractuel — c'est une auto-déclaration invérifiable."""
    for cle in ("corps", "corps_minimal", "corps_montant_illisible", "corps_montant_virgule"):
        assert "dossier" not in cas[cle]
        assert "dossier" not in cas[cle]["faits"]


def test_les_champs_facultatifs_vides_ne_partent_pas(cas: dict[str, Any]) -> None:
    """`Faits.date`/`lieu` sont `str | None` : une chaîne vide n'est pas l'absence."""
    assert cas["corps_minimal"]["faits"] == {"description": "Deux mots."}


def test_un_montant_illisible_nest_pas_envoye_a_zero(cas: dict[str, Any]) -> None:
    """Un sinistre à zéro euro n'est pas la même chose qu'un montant non renseigné."""
    assert "montant_eur" not in cas["corps_montant_illisible"]["faits"]
    # La virgule décimale française est lue, elle : c'est ce que le champ affiche.
    assert cas["corps_montant_virgule"]["faits"]["montant_eur"] == 1200.5


def test_les_bornes_du_front_sont_celles_du_serveur(cas: dict[str, Any], page: str) -> None:
    """UX-DR10 : les `maxlength` de la page sont **lus** sur les schémas, jamais devinés.

    Ce test est ce qui empêche la divergence : si `SinistreRequest.question` ou `Faits.description`
    changent de borne, la page devient rouge au lieu de laisser l'utilisateur écrire un texte que le
    serveur refusera après dix secondes d'attente.
    """
    def borne(champ: Any) -> int:
        return next(m.max_length for m in champ.metadata if getattr(m, "max_length", None))

    attendues = {
        "question_max": borne(SinistreRequest.model_fields["question"]),
        "description_max": borne(Faits.model_fields["description"]),
        # Ajoutées au tour 2 de la revue : `date` et `lieu` partent au modèle dans le même bloc
        # `untrusted()` que la description, et seul `request_max_bytes` (le corps entier) les
        # bornait — un `lieu` de 60 ko entrait dans le prompt de *comprendre*, donc dans des appels
        # facturés. AD-11 : « bornes d'entrée strictes ».
        "date_max": borne(Faits.model_fields["date"]),
        "lieu_max": borne(Faits.model_fields["lieu"]),
    }
    assert attendues == {"question_max": 1000, "description_max": 2000, "date_max": 64,
                         "lieu_max": 200}
    for nom, valeur in attendues.items():
        assert cas["bornes"][nom] == valeur, nom
    for identifiant, valeur in [("description", attendues["description_max"])]:
        assert f'id="{identifiant}"' in page
        assert f'maxlength="{valeur}"' in page, identifiant
    # `date` n'en porte **pas**, et c'est volontaire : `maxlength` ne s'applique pas à
    # `<input type="date">`, le navigateur l'ignore, et l'écrire ferait croire à une borne posée là
    # où il n'y en a aucune. La borne du domaine reste le contrôle réel, y compris pour un appelant
    # qui n'est pas un navigateur — et elle est assertée ci-dessus comme les trois autres.
    assert all(f'id="{identifiant}"' not in page
               for identifiant in ("question", "date", "lieu", "montant"))


# --- le sélecteur de contrat ---------------------------------------------

def test_le_selecteur_ne_propose_que_les_contrats(cas: dict[str, Any]) -> None:
    """AC : `lux-guide` est listé par la route (il est servi) mais **pas** proposé par le sélecteur."""
    options = cas["formulaire"]["options"]
    assert [o["valeur"] for o in options] == ["cg-mini", "cg-second", "cg-privee"]
    assert "édition juin 2017" in options[0]["texte"]
    # AD-4 : l'édition s'affiche avec sa réserve, jamais comme un statut vert — **même** quand elle
    # est vide (`Document.edition` est un `str` sans `min_length`), et c'est justement là qu'on sait
    # le moins de quoi on parle (revue 1.9).
    assert all("actualité non vérifiée" in o["texte"] for o in options)
    assert "édition valeur vide" in options[1]["texte"]
    assert cas["formulaire"]["actif"] is True
    assert cas["formulaire"]["message"] is None


def test_les_fixtures_exercent_la_branche_selectionnable_de_production(cas: dict[str, Any]) -> None:
    assert cas["selectionnable_prod"] == [
        {"doc_id": "cg-mini", "selectionnable": True},
        {"doc_id": "cg-second", "selectionnable": True},
        {"doc_id": "cg-privee", "selectionnable": True},
    ]


def test_compatibilite_selectionnable_absent_ne_sert_jamais_une_quarantaine(
        cas: dict[str, Any]) -> None:
    assert cas["selectionnable_compat"] == ["ancien-servi"]


def test_la_defense_web_refuse_les_sources_locales_privees_et_malformees(
        cas: dict[str, Any]) -> None:
    assert cas["sources_non_publiques"] == [
        {"doc_id": "public", "url": "https://example.invalid/cg.pdf"},
        {"doc_id": "localhost", "url": None},
        {"doc_id": "prive", "url": None},
        {"doc_id": "malforme", "url": None},
    ]


@pytest.mark.parametrize("cle", ["formulaire_sans_contrat", "formulaire_vide"])
def test_sans_contrat_le_formulaire_est_desactive_et_le_dit(cas: dict[str, Any], cle: str) -> None:
    """Ligne « Contrat unique / aucun contrat » de la matrice : le sélecteur le dit, le formulaire dort."""
    vue = cas[cle]
    assert vue["actif"] is False
    assert vue["options"] == []
    assert "Aucun contrat" in vue["message"]


def test_la_source_publique_du_contrat_est_offerte(cas: dict[str, Any]) -> None:
    """AD-7 : le PDF n'est pas redistribué ; son lien public rend l'édition vérifiable."""
    sources = {s["doc_id"]: s["url"] for s in cas["formulaire"]["sources"]}
    assert sources["cg-mini"].startswith("https://")
    # AD-7 : `source.url` peut porter l'URL `gs://` de la copie **privée** de secours. Le serveur la
    # filtre ; la page la refuse aussi, parce que deux étages valent mieux qu'un sur un lien qui
    # part dans un `href` d'une page publique (revue 1.9).
    assert sources["cg-privee"] is None


def test_le_lien_de_source_est_materialise_et_suit_le_contrat_choisi(cas: dict[str, Any]) -> None:
    """La seule chose qui rende « édition juin 2017 » vérifiable par celui à qui on l'annonce.

    Assertée sur le **DOM**, pas sur la valeur de retour d'une fonction pure : c'est l'ancre posée
    dans `#contrat-source` qui compte, avec `target`/`rel` (revue 1.9).
    """
    source = cas["demarrage"]["source"]
    assert source["href"] == "https://example.invalid/cg.pdf"
    assert source["target"] == "_blank"
    assert source["rel"] == "noopener noreferrer"
    assert source["texte"] == "voir le contrat à sa source publique"
    # Après un changement de contrat, le lien suit — et le reste du sélecteur ne bouge pas.
    assert cas["changement"]["source"]["href"] == "https://example.invalid/second.pdf"


def test_changer_de_contrat_ne_reinitialise_pas_le_selecteur(cas: dict[str, Any]) -> None:
    """AD-14 prévoit « ≥ 2 contrats » : le choix de l'utilisateur doit tenir (revue 1.9).

    Reconstruire les `<option>` à chaque `change` remettait `select.value` sur la première option :
    le choix était annulé en silence, le lien de source affichait le mauvais contrat, et le sinistre
    partait contre celui qu'on n'avait pas choisi.
    """
    assert cas["changement"]["valeur"] == "cg-second"
    assert cas["changement"]["options"] == ["cg-mini", "cg-second", "cg-privee"]


def test_la_liste_daudit_montre_la_quarantaine_sans_la_rendre_soumettable(
        cas: dict[str, Any]) -> None:
    audits = cas["demarrage"]["audits"]
    par_href = {a["href"]: a["texte"] for a in audits}
    href = "/sinistre/ingestion/cg-quarantaine"
    assert href in par_href
    assert "quarantaine" in par_href[href]
    assert "bloquant_statique : <script>page_sans_texte</script>" in par_href[href]
    assert "cg-quarantaine" not in cas["changement"]["options"]
    internes = [a for a in audits if a["href"].startswith("/sinistre/ingestion/")]
    assert internes and all(a["target"] == "" and a["rel"] == "" for a in internes)
    # AD-4 : la réserve accompagne la valeur, la chaîne vide et `null` dans la liste elle-même.
    assert all("actualité non vérifiée" in a["texte"] for a in audits)
    assert "édition juin 2017" in par_href["/sinistre/ingestion/cg-mini"]
    assert "édition valeur vide" in par_href["/sinistre/ingestion/cg-second"]
    assert "édition indisponible" in par_href["/sinistre/ingestion/cg-privee"]


def test_chaque_contrat_servi_garde_selection_et_lien_de_rapport_exact(
        cas: dict[str, Any]) -> None:
    audits = {a["href"]: a["texte"] for a in cas["demarrage"]["audits"]}
    contrats = ["cg-mini", "cg-second", "cg-privee"]
    assert cas["changement"]["options"] == contrats
    for doc_id in contrats:
        href = "/sinistre/ingestion/" + doc_id
        assert href in audits
        assert "statut effectif : servi" in audits[href]


# --- story 3.4 : lecteur PDF paresseux -----------------------------------

def test_les_clauses_ouvrent_le_lecteur_sans_aucune_requete_avant_clic(cas: dict[str, Any]) -> None:
    lecteur = cas["lecteur"]
    assert cas["demarrage"]["ordre_des_appels"] == ["/api/v1/sante", "/api/v1/documents"]
    assert cas["soumission"]["commandes_pdf"] == 2
    assert lecteur["appels_avant_clic"] == 0
    assert lecteur["urls"] == [
        "/api/v1/documents/cg-mini/pages/9.png?blocks=cg-mini%3Ap9%3A2&line_ids=p9%3A2%3Al1"]
    assert lecteur["statut"] == "Page 9 sur 12 chargée."
    assert lecteur["image_src"].startswith("blob:lecteur-")
    assert lecteur["image_alt"] == "Page 9 sur 12 du contrat"


def test_le_lecteur_est_accessible_et_restaure_le_focus(cas: dict[str, Any], page: str) -> None:
    lecteur = cas["lecteur"]
    assert lecteur["focus_ouverture"] == "lecteur-fermer"
    assert lecteur["focus_fermeture"] == "cl-ouvrir"
    assert lecteur["source"] == {
        "href": "https://example.invalid/cg.pdf",
        "target": "_blank",
        "rel": "noopener noreferrer",
    }
    assert 'id="lecteur-pdf" aria-labelledby="lecteur-titre"' in page
    assert 'id="lecteur-statut" role="status" aria-live="polite"' in page
    assert 'aria-label="Navigation dans le PDF"' in page


def test_la_navigation_reste_bornee_et_ne_transporte_pas_un_surlignage_etranger(
        cas: dict[str, Any]) -> None:
    lecteur = cas["lecteur"]
    assert lecteur["precedent_desactive"] is False
    assert lecteur["suivant_desactive"] is False
    assert lecteur["navigation_url"] == "/api/v1/documents/cg-mini/pages/10.png"
    assert "sans surlignage" in lecteur["sans_surlignage"]


def test_une_citation_sans_lignes_ouvre_la_page_sans_faux_surlignage(cas: dict[str, Any]) -> None:
    lecteur = cas["lecteur_sans_lignes"]
    assert lecteur["url"] == (
        "/api/v1/documents/cg-mini/pages/12.png?blocks=cg-mini%3Ap12%3A3&line_ids=")
    assert lecteur["visible"] is True
    assert lecteur["suivant_desactive"] is True
    assert "aucune ligne" in lecteur["explication"]


def test_les_identifiants_de_lignes_sont_encodes_dans_lurl(cas: dict[str, Any]) -> None:
    assert cas["url_page_encodee"] == (
        "/api/v1/documents/cg-mini/pages/9.png?"
        "blocks=cg-mini%3Ap9%3A2,cg-mini%3Ap9%3A3&"
        "line_ids=p9%3A2%3Al%201%2F%C3%A9&line_ids=p9%3A2%3Al2")


def test_une_image_indisponible_conserve_verdict_et_source_publique(cas: dict[str, Any]) -> None:
    echec = cas["lecteur_en_echec"]
    assert "indisponible" in echec["statut"]
    assert echec["badge"] == "Sous conditions"
    assert echec["source"] == "https://example.invalid/cg.pdf"
    assert echec["image_cachee"] is True


def test_une_navigation_annule_la_page_precedente_et_ignore_sa_reponse_tardive(
        cas: dict[str, Any]) -> None:
    inversees = cas["lecteur_reponses_inversees"]
    assert inversees["premiere_annulee"] is True
    assert inversees["urls"] == [
        "/api/v1/documents/cg-mini/pages/9.png?blocks=cg-mini%3Ap9%3A2&line_ids=p9%3A2%3Al1",
        "/api/v1/documents/cg-mini/pages/10.png",
    ]
    assert inversees["src_recent"] == inversees["src_final"]
    assert inversees["statut"] == "Page 10 sur 12 chargée."


def test_fermer_annule_aussi_un_corps_blob_encore_bloque(cas: dict[str, Any]) -> None:
    annule = cas["lecteur_blob_annule"]
    assert annule == {"blob_commence": True, "signal_annule": True, "blob_annule": True}


# --- D6 : l'appariement clause ↔ affirmation ------------------------------

def test_lappariement_suit_lenumeration_du_serveur(cas: dict[str, Any]) -> None:
    """D6 : `ClauseSource` n'a pas de `claim_id` ; l'appariement est positionnel, comme au guide."""
    apparie = cas["appariement"]
    assert [e["claim_id"] for e in apparie] == ["c1", "c2"]
    assert [e["blocs"] for e in apparie] == [["cg-mini:p9:2"], ["cg-mini:p12:3"]]
    assert [e["applicable"] for e in apparie] == ["humain", "oui"]


@pytest.mark.parametrize("cle", ["appariement_trop_de_sources", "appariement_desordre",
                                  "appariement_sans_source"])
def test_un_appariement_qui_ne_retombe_pas_est_abandonne(cas: dict[str, Any], cle: str) -> None:
    """D6 : jamais un rattachement deviné — une clause sous la mauvaise affirmation est pire."""
    assert cas[cle] is None


def test_lappariement_abandonne_se_dit_et_ne_retire_rien(cas: dict[str, Any]) -> None:
    """Dégradation **visible** : la page le dit, puis affiche la liste plate — toutes les clauses.

    Et **avec leurs statuts** (D6, mot pour mot) : le mode dégradé serait le dernier endroit où
    taire l'applicabilité d'une clause et la réserve d'actualité de son édition (revue 1.9).
    """
    degrade = cas["verdict_degrade"]
    assert degrade["degrade"], "la page doit dire qu'elle n'a pas pu rattacher les clauses"
    assert "rattacher" in degrade["degrade"][0]
    assert degrade["clauses"] == 3  # les trois clauses restent affichées
    assert degrade["affirmations"] == []  # aucune affirmation n'en porte, puisque rien n'est apparié
    statuts = degrade["statuts"]
    assert len(statuts) == 2  # les deux clauses que des claims affichées citent
    assert "applicabilité à confirmer par un humain" in statuts[0]
    assert all("actualité non vérifiée" in s for s in statuts)


# --- AD-6 / AC : ce que la page affiche ----------------------------------

def test_le_badge_est_en_tete_et_la_portee_dite_une_fois(cas: dict[str, Any]) -> None:
    """AC : le badge du verdict et la mention de portée, tous deux posés par `textContent`.

    Story 5.6 (L2) : le libellé est celui qu'un assuré lit — « Sous conditions », pas la valeur du
    domaine. La classe, elle, reste la valeur : c'est elle qui porte la couleur sémantique, et un
    test de contraste s'y adosse.
    """
    badge = cas["verdict"]["badge"]
    assert len(badge) == 1
    assert badge[0]["texte"] == "Sous conditions"
    assert badge[0]["cls"] == "badge verdict-sous_conditions"
    # Story 5.6 (L2b) : la portée est dite **une fois**, dans les garde-fous. Elle était écrite deux
    # fois sur le même écran — sous la pastille et dans la ligne de la règle, en deux formulations
    # de la même réserve. Ce qui compte reste vrai : elle est là, et une seule fois.
    assert cas["verdict"]["portee"] == [
        "au regard des conditions générales seules — verdict non validé par un expert assurance"]
    assert cas["verdict"]["portee_dans_gardefous"] is True


def test_la_portee_du_front_contient_celle_du_domaine(cas: dict[str, Any]) -> None:
    """AD-6 : `PORTEE` est invariable côté serveur ; la page en dit autant, plus la réserve d'expert."""
    assert PORTEE in cas["portee"]
    assert "non validé par un expert" in cas["portee"]


def test_une_clause_hors_portee_explique_son_statut_non(cas: dict[str, Any]) -> None:
    statut = cas["statuts"]["non_hors_portee"]
    assert "non applicable" in statut
    assert "portée contractuelle ne couvre pas le cas déclaré" in statut


def test_les_quatre_valeurs_du_verdict_ont_un_libelle(cas: dict[str, Any]) -> None:
    """AD-6 : quatre valeurs. Le front ne connaît que celles-là, et le dit quand il en reçoit une autre."""
    connues = {v["cle"] for v in cas["verdicts"] if v["cle"] != "inconnu"}
    assert connues == set(VerdictValue.__args__)
    inconnus = [v for v in cas["verdicts"] if v["cle"] == "inconnu"]
    assert len(inconnus) == 2  # « inventé » et la chaîne vide
    assert all(v["texte"] == "verdict non reconnu" for v in inconnus)


def test_un_verdict_hors_contrat_nest_pas_traduit_en_valeur_connue(cas: dict[str, Any]) -> None:
    """AD-16 : le ranger dans la case la plus proche serait le dégradé silencieux."""
    assert cas["verdict_inconnu"] == [{"cls": "badge verdict-inconnu", "texte": "verdict non reconnu"}]


def test_la_raison_et_le_texte_du_serveur_sont_affiches(cas: dict[str, Any]) -> None:
    """AD-6 : « l'UI affiche `value` + `reason` ». Le texte est rendu par le serveur (AD-3)."""
    # Story 5.6 (L2b) : la raison composée par la table dit *par quelle règle* le verdict est
    # tombé, jamais un fait sur le sinistre — et la première phrase porte déjà la conclusion en
    # français. Elle n'est donc plus une quatrième ligne grise sous la pastille : elle se replie
    # dans « Comment cette réponse a été obtenue », en tête du panneau. Affichée, jamais perdue.
    assert cas["verdict"]["raison"] == []
    rubriques = {r["titre"]: [l["texte"] for l in r["lignes"]]
                 for r in cas["verdict"]["trace"]["rubriques"]}
    assert rubriques["Ce qui a décidé le verdict"] == [
        "Une clause conditionne la garantie (au regard des conditions générales seules)"]
    assert cas["verdict"]["analyse"] == [
        "La garantie vise l'action subite de la chaleur. Une condition reste ouverte."]


def test_les_faits_compris_sont_affiches_champ_par_champ(cas: dict[str, Any]) -> None:
    """AC/D4 : « les faits compris » — bien, événement, lieu, cause, moment —, pas la description en écho."""
    lignes = dict(cas["verdict"]["faits_compris"])
    assert lignes == {
        "Bien concerné": "mobilier de salon",
        "Événement": "brûlure sans embrasement",
        "Lieu": "domicile",
        "Cause": "bougie",
        "Moment": "2026-08-01",
        # `themes` était fixé à `[]` par les deux fixtures : la ligne n'était rendue par aucun test,
        # et la supprimer ne faisait rien rougir (revue 1.9, tour 2).
        "Thèmes": "habitation, incendie",
    }


def test_le_paquet_manquant_est_annonce_et_reclame(cas: dict[str, Any]) -> None:
    """AD-6 : `MissingPackage` accompagne **toujours** le verdict, et alimente `ask_client`.

    Story 5.6 (L2) : les quatre pièces non lues tiennent en **une ligne** grise, et les faits que
    les clauses exigent gardent leur section — ce sont deux choses différentes, l'une est une
    limite constante du service, l'autre une lacune de ce dossier-ci.
    """
    pieces = " ".join(cas["verdict"]["pieces"])
    for piece in ("conditions particulières", "options souscrites", "avenants", "date d'effet"):
        assert piece in pieces
    assert len(cas["verdict"]["pieces"]) == 1, "les pièces non lues tiennent en une seule ligne"
    assert "caractère subit de l'action de la chaleur" in " ".join(cas["verdict"]["paquet"])
    assert cas["verdict"]["ask"] == [
        "Le contrat porte-t-il l'option correspondante ?",
        "Le caractère subit de l'action de la chaleur est-il établi ?"]
    assert cas["verdict"]["escalate"] == ["Faire relire la clause par un gestionnaire."]


def test_chaque_clause_affiche_son_type_sa_page_et_ses_statuts(cas: dict[str, Any]) -> None:
    """UX-DR6 (c) / D5 : le type vient de `Block.kind` (AD-6, seule source), la page du bloc."""
    clauses = cas["verdict"]["clauses"]
    assert len(clauses) == 2
    assert clauses[0]["quote"].startswith("« événement soudain")
    # Story 5.6 (L2) : l'entête d'une clause se lit chemin › page › type. Sans `chemin` publié par
    # le moteur et sans `trace.blocs` pour le résoudre, elle commence à la page.
    assert clauses[0]["meta"][0] == "page 9"
    assert clauses[0]["meta"][1] == "garantie"
    statut = cas["verdict"]["statuts"][0]
    assert "applicabilité à confirmer par un humain" in statut
    assert "actualité non vérifiée" in statut


def test_un_typage_non_confirme_est_dit(cas: dict[str, Any]) -> None:
    """D5 : un `kind` non confirmé vaut `applicable="humain"` — l'afficher sans le dire mentirait."""
    condition = cas["verdict"]["clauses"][1]
    assert condition["meta"][1] == "condition"
    assert condition["meta"][2] == "typage non confirmé"


def test_chaque_clause_est_placee_sous_laffirmation_quelle_soutient(cas: dict[str, Any]) -> None:
    assert cas["verdict"]["affirmations"] == [
        "La garantie vise l'action subite de la chaleur.", "Une condition reste ouverte."]
    assert cas["verdict"]["degrade"] == []  # l'appariement a retombé : aucune réserve affichée


def test_les_clauses_non_retrouvees_sont_affichees_sans_leur_citation(cas: dict[str, Any]) -> None:
    """D7 : « le texte de l'affirmation, le motif du rejet en français **et le kind du rejet** ».

    La quote, elle, ne sort pas : sur une claim `non_retrouvee` elle est restée la chaîne du modèle.
    """
    assert cas["verdict"]["rejetees"] == [["Une exclusion écarte les dommages du canapé.",
                                           "citation introuvable dans le contrat",
                                           "non_retrouvee"]]
    # Ni là, ni ailleurs dans l'arbre : la vérification porte sur le texte **entier** de la vue.
    assert "CETTE QUOTE NE DOIT JAMAIS S'AFFICHER" not in cas["verdict"]["texte_entier"]


def test_le_titre_des_affirmations_ecartees_vaut_pour_les_quatre_kinds(cas: dict[str, Any]) -> None:
    """AD-16 : jamais une affirmation que rien n'établit (revue 1.9).

    « Clauses citées non retrouvées » était faux pour deux des quatre `rejection_kind` :
    `non_pertinente` désigne un **passage réel** (la page le dit deux lignes plus bas, et se
    contredisait donc dans le même écran) et `non_citee` une affirmation *vérifiée* qu'aucune phrase
    affichée ne reprend. Ce qui leur est commun, et seulement cela, c'est qu'elles ont été écartées
    et que leur citation n'est pas montrée.
    """
    titre = cas["verdict"]["rejetees_titre"]
    assert titre == ["Affirmations écartées par la vérification"]
    # Story 5.6 (L2) : elles restent consultables **derrière** les garde-fous, dépliables, et plus
    # jamais au-dessus de la réponse.
    assert cas["verdict"]["rejetees_tag"] == ["details"]
    note = cas["verdict"]["rejetees_note"][0]
    assert "non retrouvée" not in note and "n'a pas été retrouvée" not in note
    assert "écartées" in note


def test_les_quatre_motifs_de_rejet_ont_une_phrase_en_francais(cas: dict[str, Any]) -> None:
    """AD-4 : quatre `rejection_kind`. Un cinquième inconnu ne rend pas la page muette."""
    assert len(cas["rejets"]) == 5
    assert len(set(cas["rejets"][:4])) == 4  # quatre motifs distincts
    assert all(m and m == m.strip() for m in cas["rejets"])


def test_ce_que_je_ne_sais_pas_est_affiche(cas: dict[str, Any]) -> None:
    """Story 5.6 (L2) : une seule inconnue tient en **une ligne** sous la réponse ; au-delà, la
    liste reste compacte, jamais tronquée."""
    assert cas["verdict"]["inconnu"] == []
    assert cas["verdict"]["inconnu_ligne"] == [
        "Ce que je ne sais pas : La franchise applicable n'est pas dite."]


def test_la_trace_est_depliable_et_porte_la_reference_les_etapes_et_le_cout(cas: dict[str, Any]) -> None:
    """AD-10 : « la trace est consultable ». `<details>` natif : dépliable sans une ligne de JS."""
    assert cas["verdict"]["trace_tags"] == ["details"]
    panneau = cas["verdict"]["trace"]
    rubriques = {r["titre"]: r["lignes"] for r in panneau["rubriques"]}
    assert panneau["summary"] == "Comment cette réponse a été obtenue"
    assert [l["texte"] for l in rubriques["Cette requête"]] == [
        "pipeline : sinistre · variante deterministe", "référence de requête : r-1"]
    assert any("comprendre · micro" in l["texte"] for l in rubriques["Étapes"])
    assert any("applicabilité non rendue" in l["texte"] for l in rubriques["Contrôles"])
    assert rubriques["Ce que l'analyse a coûté"][-1]["texte"] == (
        "cette analyse a coûté 0,0336 €")
    # AD-10 : la trace ne porte jamais le texte des blocs — le harnais n'en fournit pas, et rien
    # dans ce qu'on relève ne pourrait en faire apparaître.
    assert "bougie" not in json.dumps(panneau, ensure_ascii=False)



# --- Story 2.5 : la trace enrichie ---------------------------------------
#
# Elle ne portait que trois lignes plates : la référence, le pipeline, une ligne par étape avec les
# **noms bruts** de ses contrôles, et le coût. Tout le reste voyageait sans être montré — les
# clauses ouvertes et écartées, l'issue de chaque contrôle, les relances, les seuils, le gate.

def _rubriques(trace: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {r["titre"]: r["lignes"] for r in trace["rubriques"]}


def test_la_trace_dit_les_clauses_ouvertes_et_celles_qui_ont_ete_ecartees(
        cas: dict[str, Any]) -> None:
    """AD-10 distingue « blocs effectivement passés au modèle » et « candidats non ouverts ». Sur un
    contrat, c'est la différence entre une clause lue et une clause que la recherche a proposée."""
    rubriques = _rubriques(cas["trace_riche"])
    assert [ligne["texte"] for ligne in rubriques["Clauses ouvertes"]] == [
        "cg-mini:p9:2 — Les garanties incendie", "cg-mini:p12:3"]
    assert [ligne["texte"] for ligne in
            rubriques["Clauses écartées, non lues par le modèle"]] == [
        "cg-mini:p46:1 — Les exclusions"]


def test_une_clause_que_la_trace_ne_resout_pas_porte_son_id_seul(cas: dict[str, Any]) -> None:
    """Jamais un titre deviné sous un verdict : la page n'a aucun moyen de retrouver le titre d'une
    clause, et en inventer un serait pire que l'absence."""
    rubriques = _rubriques(cas["trace_bloc_non_resolu"])
    assert [ligne["texte"] for ligne in rubriques["Clauses ouvertes"]] == [
        "cg-mini:p9:2", "cg-mini:p12:3"]


def test_la_trace_dit_lissue_de_chaque_controle_avec_son_libelle(cas: dict[str, Any]) -> None:
    """« applicabilite_incomplete » n'apprenait rien à personne, et la ligne plate ne disait pas si
    le contrôle avait **échoué**. Le libellé français, le détail composé par le serveur, l'issue."""
    lignes = _rubriques(cas["trace_riche"])["Contrôles"]
    assert [ligne["etat"] for ligne in lignes] == ["ko", "ok"]
    assert lignes[0]["texte"] == ("applicabilité non rendue pour une clause décisionnelle — "
                                  "1 affirmation(s) sans champs typés")
    assert lignes[1]["texte"].startswith("verdict rendu sur les affirmations affichées")
    # Le pictogramme est décoratif : l'état est écrit à côté, en toutes lettres.
    for ligne in lignes:
        assert ligne["picto"] == {"aria-hidden": "true"}


def test_un_controle_inconnu_de_la_page_saffiche_tel_quel(cas: dict[str, Any]) -> None:
    lignes = _rubriques(cas["trace_controle_inconnu"])["Contrôles"]
    assert [ligne["texte"] for ligne in lignes] == ["controle_de_demain — détail"]


def test_la_trace_dit_les_relances_et_les_seuils_actifs(cas: dict[str, Any]) -> None:
    """Convention Seuils du spine : « exposés dans `Trace.thresholds` ». Repliés, parce qu'ils sont
    nombreux et rarement lus — mais à portée."""
    lignes = [ligne["texte"] for ligne in _rubriques(cas["trace_riche"])["Ce que l'analyse a coûté"]]
    assert lignes == ["1 relance", "0 troncature", "cette analyse a coûté 0,0336 €"]
    seuils = cas["trace_riche"]["seuils"]
    assert seuils["tag"] == "details" and seuils["summary"] == "Seuils actifs (2)"
    assert seuils["lignes"] == ["max_opens : 8", "quote_min_chars : 24"]


def test_la_trace_dit_le_gate_du_contrat_et_ses_alertes(cas: dict[str, Any]) -> None:
    """AD-7 / AD-14 : le profil, le nombre de cas réellement exécutés, la contresignature humaine —
    et l'alerte que le serveur pose sur ce document, dans le même objet."""
    lignes = _rubriques(cas["trace_riche"])["Validation du contrat interrogé"]
    assert lignes[0]["texte"] == "profil de validation : vertical (1 cas)"
    assert lignes[1]["etat"] == "ko"
    assert "non contresignée" in lignes[1]["texte"]
    assert lignes[2]["texte"] == ("le fichier source n'est pas présent à côté des artefacts "
                                  "(source_absente)")


def test_une_trace_pauvre_ne_fait_apparaitre_aucune_rubrique_inventee(cas: dict[str, Any]) -> None:
    """AD-16 : ce que la trace ne dit pas, l'écran ne le dit pas. Les deux lots de cette story sont
    écrits en parallèle : la page doit se lire aussi bien avant qu'après que le serveur ait posé
    ses champs, sans rien remplir entre-temps."""
    titres = [r["titre"] for r in cas["trace_pauvre"]["rubriques"]]
    # « Ce qui a décidé le verdict » n'est pas une rubrique inventée : c'est la raison que le
    # serveur a composée, repliée ici depuis le bloc 1 (story 5.6, L2b).
    assert titres == ["Ce qui a décidé le verdict", "Ce que l'analyse a coûté", "Cette requête"]
    assert cas["trace_pauvre"]["seuils"] is None
    for rubrique in cas["trace_pauvre"]["rubriques"]:
        for ligne in rubrique["lignes"]:
            assert ligne["texte"].strip()


def test_une_trace_qui_na_rien_a_dire_nouvre_pas_un_panneau_vide(cas: dict[str, Any]) -> None:
    """Un `<details>` qui n'a que son `<summary>` promet une explication qu'il ne donne pas."""
    assert cas["trace_muette"] is None
    assert cas["trace_absente"] is None
    # Un coût **nul** n'est pas un silence : aucun appel n'a été facturé, et c'est une mesure.
    assert [r["titre"] for r in cas["trace_cout_nul"]["rubriques"]] == [
        "Ce qui a décidé le verdict", "Ce que l'analyse a coûté"]
    assert cas["trace_cout_nul"]["rubriques"][1]["lignes"][0]["texte"] == (
        "cette analyse n'a rien coûté (aucun appel facturé)")


# --- Story 2.5 (M15) : la preuve d'absence et les trois états -------------

def test_un_refus_porte_sa_preuve_chiffree(cas: dict[str, Any]) -> None:
    """Reprise différée de 1.9. Le contrat transporte `answer.reason` depuis toujours et le guide
    l'affiche depuis 1.7 ; ici, seule la phrase composée par le serveur était rendue. « Rien trouvé
    sur 1 457 passages » et « rien n'a été cherché » sont deux refus différents."""
    assert cas["refus_preuve"]["preuve"] == [
        "Termes cherchés : mobilier — 0 variante essayée, 3 passages parcourus"]
    # La preuve est peinte **avant** le pied qui la commente, et le pied avant la trace.
    ordre = cas["refus_preuve"]["ordre"]
    assert ordre.index("preuve") < ordre.index("pied") < ordre.index("trace")


def test_un_refus_porte_un_badge_detat_et_sa_phrase(cas: dict[str, Any]) -> None:
    """Reprise différée de 2.3 : la page rendait les phrases de lacune sans le cadre des trois
    états. Le badge d'état n'est pas le badge de **verdict** — l'un dit ce que le contrat prévoit,
    l'autre ce que la vérification a pu établir — et les deux se lisent."""
    assert cas["refus_preuve"]["etat"] == [{"cls": "etat etat-inconnu", "texte": "inconnu"}]
    assert cas["refus_preuve"]["phrase"] == [
        "rien n'a été retenu : la preuve de cette absence est ci-dessus"]


def test_les_trois_etats_sont_dits_sur_la_page_sinistre(cas: dict[str, Any]) -> None:
    assert cas["etat_partiel"]["etat"] == [{"cls": "etat etat-partiel", "texte": "partiel"}]
    assert cas["etat_partiel"]["phrase"] == [
        "il manque des éléments : ils sont listés sous « Ce que je ne sais pas »"]
    assert cas["etat_partiel"]["preuve"] == [], "une réponse trouvée n'a pas de preuve d'absence"
    assert cas["etat_sur"]["etat"] == ["sûr"]
    assert cas["etat_sur"]["phrase"] == [
        "tout ce qui est affirmé ci-dessus est appuyé par un passage cité, "
        "et la question est couverte"]


def test_une_clarification_na_rien_cherche_donc_rien_a_prouver(cas: dict[str, Any]) -> None:
    """AD-4 : « `terms_searched` est alors vide et `blocks_scanned` nul : rien n'a été cherché ».
    Lui accrocher « 0 variante essayée » répondrait à une question que personne ne pose."""
    assert cas["clarification_preuve"]["preuve"] == []
    assert cas["clarification_preuve"]["etat"] == ["inconnu"]
    assert cas["clarification_preuve"]["phrase"] == [
        "rien n'a été cherché : la question doit d'abord être précisée"]


def test_sans_reason_ni_preuve_ni_badge_ne_sont_fabriques(cas: dict[str, Any]) -> None:
    """M15 : « `reason` absent ⇒ ni preuve ni badge ». Un corps sans `reason` sous `found=false`
    n'a pas été écrit par la route, et le badge dirait « inconnu » sur rien."""
    assert cas["sans_reason"]["preuve"] == []
    assert cas["sans_reason"]["etat"] == []


@pytest.mark.parametrize(("nom", "champ"), [
    ("reason_vide", "answer.reason.kind"),
    ("reason_kind_inconnu", "answer.reason.kind"),
    ("reason_termes_nuls", "answer.reason.terms_searched"),
    ("reason_compteur_chaine", "answer.reason.blocks_scanned"),
    ("reason_non_objet", "answer.reason"),
])
def test_la_preuve_dabsence_est_lue_strictement(cas: dict[str, Any], nom: str, champ: str) -> None:
    """Un `reason: {}` passait pour un refus muni d'une preuve « 0 variante essayée, 0 passage
    parcouru » que rien n'avait calculée, et les compteurs sont **affichés** : un objet ou une
    chaîne à leur place arriveraient jusqu'au rendu. Même défaut, même remède que côté guide."""
    vu = cas["reason_illisible"][nom]
    assert vu["a_repondu"] is False
    assert (vu["code"], vu["champ"]) == ("reponse_illisible", champ)


def test_un_reason_nul_reste_une_valeur_du_contrat(cas: dict[str, Any]) -> None:
    """`reason` est `AbsenceProof | None` : `null` est une **valeur**, celle d'une réponse trouvée.
    Le refuser transformerait chaque verdict rendu en « réponse illisible »."""
    assert cas["reason_nul_est_lisible"] is True


def test_le_cout_vient_de_lusage_rendu_par_lapi(cas: dict[str, Any]) -> None:
    """NFR4 : jamais une estimation du front. Un total nul se dit, il ne s'arrondit pas à 0,0000 €."""
    assert cas["couts"]["nominal"] == "cette analyse a coûté 0,0336 €"
    assert cas["couts"]["zero"] == "cette analyse n'a rien coûté (aucun appel facturé)"
    assert cas["couts"]["absent"] == ""
    assert cas["couts"]["sans_trace"] == ""


# --- le refus : un verdict, pas une absence ------------------------------

def test_un_bloc_cite_par_deux_affirmations_ne_recoit_pas_le_statut_de_la_premiere(
        cas: dict[str, Any]) -> None:
    """D6, dans le repli qui était censé l'honorer (revue 1.9, tour 2).

    Le mode dégradé rattachait le statut par `block_id` en rendant **le premier trouvé** : deux
    affirmations citant la même clause avec des statuts différents — applicable pour l'une, à
    confirmer pour l'autre — et la page affichait celui de la première sous les deux. C'est
    exactement « une clause attribuée à la mauvaise affirmation sous un verdict », réintroduit par
    le repli. Elle n'attribue plus rien, et elle le dit.
    """
    releve = cas["verdict_statut_ambigu"]
    assert releve["appariement"] is None  # on est bien dans le mode dégradé
    assert releve["statut_pour_bloc_partage"] is None
    assert releve["statut_pour_bloc_unique"] is not None
    assert releve["ambigu_partage"] is True
    assert releve["ambigu_unique"] is False
    # Un bloc qu'aucune affirmation affichée ne cite n'est pas « ambigu » : il n'a pas de statut,
    # et compter les deux ensemble aurait fait annoncer une ambiguïté qui n'existe pas.
    assert releve["ambigu_non_cite"] is False
    assert releve["statuts"] == []  # rien n'est deviné
    dit = " ".join(releve["degrade"])
    assert "n'est pas affiché" in dit and "je ne devine pas" in dit
    assert "l'une de ces clauses" in dit


def test_la_clarification_est_affichee_avec_sa_question(cas: dict[str, Any]) -> None:
    """AD-5 : le seul chemin où le système **pose une question** en retour (revue 1.9, tour 2).

    Les deux fixtures fixaient `clarification: null` : supprimer la branche de `vueVerdict` ne
    faisait rien rougir, et la perdre à l'écran laisse l'utilisateur devant un « ne tranche pas »
    sans issue — alors que la seule chose à faire est de reformuler.
    """
    releve = cas["verdict_clarification"]
    assert releve["question"] == ["De quel bien parlez-vous : le mobilier, ou le bâtiment ?"]
    assert releve["titre"] == ["Une précision, pour chercher au bon endroit"]
    # AD-16 : même là, un verdict est porté — un refus sinistre n'est jamais vide.
    assert releve["badge"] == ["Pas de clause qui s'applique"]
    assert releve["clauses"] == 0
    # AD-5 : `ClarificationRequise` n'a pas de portée. Rien n'est inventé pour remplir la section.
    assert releve["faits_compris"] == 0


def test_un_corps_de_documents_illisible_nest_pas_un_service_vide(cas: dict[str, Any]) -> None:
    """P4 : « aucun contrat servi » est une affirmation sur le **service**, pas sur une lecture ratée.

    Un 200 non-tableau était réduit à `[]`, et la page affirmait alors quelque chose qu'elle ne
    savait pas — la distinction que le commentaire de `vueFormulaire` dit précisément ne pas devoir
    brouiller (revue 1.9, tour 2).
    """
    releve = cas["documents_illisibles"]
    assert "n'a pas pu être chargée" in releve["message"]
    assert "Aucun contrat n'est servi" not in releve["message"]
    assert "ne sait pas lire" in releve["texte"]
    assert releve["bouton_desactive"] is True
    assert releve["badges"] == 0


def test_un_refus_affiche_ne_tranche_pas_sa_portee_et_les_faits_compris(cas: dict[str, Any]) -> None:
    """AD-16 : un refus sinistre **porte** un verdict — sans lui la page n'aurait qu'une absence."""
    refus = cas["verdict_refus"]
    assert refus["badge"] == [
        {"cls": "badge verdict-ne_tranche_pas", "texte": "Pas de clause qui s'applique"}]
    assert refus["clauses"] == 0
    assert refus["portee"] == [
        "au regard des conditions générales seules — verdict non validé par un expert assurance"]
    # La phrase de refus vient du serveur (`restituer.PHRASES_DE_REFUS_SINISTRE`), pas de la page.
    assert refus["analyse"] == [
        "Je n'ai trouvé aucune clause du contrat qui traite du sinistre décrit."]
    # D4 : c'est sur un refus que « ce que j'ai compris » compte le plus.
    assert dict(refus["faits_compris"]) == {"Bien concerné": "mobilier de salon"}


# --- AD-16 : les erreurs -------------------------------------------------

@pytest.mark.parametrize("code", ["invalid_request", "input_too_long", "rate_limited",
                                   "llm_unavailable", "timeout", "budget_exceeded",
                                   "corpus_unavailable", "internal", "reponse_illisible",
                                   "reseau", "timeout_client", "hors_ligne", "sans_code"])
def test_aucune_erreur_ne_propose_de_repli_ni_de_verdict(cas: dict[str, Any], code: str) -> None:
    """AC : ni bouton de repli, ni verdict de remplacement, quelle que soit l'erreur (AD-16)."""
    vue = cas["erreurs"][code]
    assert vue["boutons"] == 0
    assert vue["actions"] == 0
    assert vue["badges"] == 0
    assert vue["titre"] == "Aucun verdict n'a été rendu"
    assert vue["message"] and vue["message"] == vue["message"].strip()
    assert "recherche simple" not in vue["texte_entier"]
    assert PORTEE not in vue["texte_entier"]


def test_chaque_erreur_affiche_sa_reference_de_requete(cas: dict[str, Any]) -> None:
    """AC : « elle affiche l'erreur **et sa référence de requête** » — c'est le lien avec le log."""
    for code, vue in cas["erreurs"].items():
        assert vue["reference"] == "référence de requête : r-err", code
    # Sans `request_id`, aucune ligne fantôme n'est peinte.
    assert cas["erreur_sans_reference"] == []


def test_les_messages_derreur_sont_distincts_et_en_francais(cas: dict[str, Any]) -> None:
    """FR11 : le `message` du serveur (pydantic, anglais, chemin du champ) n'est jamais affiché."""
    messages = {v["message"] for v in cas["erreurs"].values()}
    assert len(messages) >= 7
    assert not any(re.search(r"\bbody\.|should have|List should", m) for m in messages)


@pytest.mark.parametrize("nom, attendu", [
    ("invalid_request", {"kind": "requete", "statut": 400}),
    ("rate_limited", {"kind": "requete", "statut": 429}),
    ("llm_unavailable", {"kind": "indisponible", "statut": 503}),
    ("internal", {"kind": "requete", "statut": 500}),
])
def test_les_codes_http_remontent_types(cas: dict[str, Any], nom: str, attendu: dict) -> None:
    """AD-16 : le code de l'enveloppe est lu tel quel, jamais inventé côté page."""
    obtenu = cas["http"][nom]
    assert obtenu["code"] == nom
    assert obtenu["kind"] == attendu["kind"]
    assert obtenu["statut"] == attendu["statut"]
    assert obtenu["request_id"] == "r-9"
    assert "message serveur en anglais" not in obtenu["message"]


def test_le_retry_after_du_429_est_lu_et_affiche(cas: dict[str, Any]) -> None:
    """AD-13 : « 429 → enveloppe `rate_limited` + `Retry-After` », affiché sans repli."""
    assert cas["http"]["rate_limited"]["retry_after"] == 42
    assert "42 secondes" in cas["http"]["rate_limited"]["message"]


def test_une_panne_reseau_ne_fabrique_rien(cas: dict[str, Any]) -> None:
    assert cas["http"]["reseau"]["kind"] == "indisponible"
    assert cas["http"]["reseau"]["code"] == "reseau"
    assert "rien n'a été analysé" in cas["http"]["reseau"]["message"]


@pytest.mark.parametrize("nom, champ", [
    ("sans_answer", "answer"),
    ("sans_verdict", "answer.verdict"),
    ("verdict_null", "answer.verdict"),
    ("sans_valeur", "answer.verdict.value"),
    ("sans_trace", "trace"),
    ("sources_null", "sources"),
    ("clause_sans_kind", "sources[0].kind"),
    # Revue Codex 1.9 (tour 1, I2) : tout ce qu'UX-DR6 fait afficher, et qu'aucun défaut de schéma
    # ne rend facultatif sur le fil. Un 200 amputé de sa raison, de son paquet manquant ou de ses
    # questions donnait un verdict **plus assuré** que celui que le serveur a rendu.
    ("sans_sources", "sources"),
    ("sans_raison", "answer.verdict.reason"),
    ("sans_paquet", "answer.verdict.missing"),
    ("paquet_null", "answer.verdict.missing"),
    ("sans_ask_client", "answer.verdict.ask_client"),
    ("sans_escalate", "answer.verdict.escalate"),
    ("sans_claims", "answer.claims"),
    ("sans_rejetees", "answer.rejected_claims"),
    # Revue Codex 1.9 (tour 2, I2) : la lecture descend jusqu'aux **feuilles**. Un conteneur bien
    # formé aux feuilles fausses fabrique ou omet une réserve tout aussi sûrement qu'un conteneur
    # absent — `missing: {}` faisait annoncer les quatre pièces du contrat comme non lues, un
    # `ask_client` d'objets posait « [object Object] » en question à poser au client, une claim sans
    # `status` affichait sa clause sans applicabilité ni réserve d'édition.
    ("paquet_vide", "answer.verdict.missing.conditions_particulieres"),
    ("piece_absente", "answer.verdict.missing.avenants"),
    ("piece_en_chaine", "answer.verdict.missing.date_effet"),
    ("fait_manquant_objet", "answer.verdict.missing.faits[0]"),
    ("question_objet", "answer.verdict.ask_client[1]"),
    ("escalade_nombre", "answer.verdict.escalate[0]"),
    ("claim_null", "answer.claims[1]"),
    ("claim_sans_texte", "answer.claims[0].text"),
    ("claim_sans_statut", "answer.claims[1].status"),
    ("claim_statut_sans_edition", "answer.claims[0].status.edition"),
    ("claim_statut_applicable_objet", "answer.claims[0].status.applicable"),
    ("claim_sans_quotes", "answer.claims[1].quotes"),
    ("claim_quote_sans_bloc", "answer.claims[0].quotes[0].block_id"),
    ("rejetee_sans_kind", "answer.rejected_claims[0].rejection_kind"),
    ("clause_null", "sources[1]"),
    ("clause_page_en_chaine", "sources[0].page"),
    ("clause_sans_lignes", "sources[0].line_ids"),
    ("clause_lignes_non_liste", "sources[0].line_ids"),
    ("clause_ligne_nombre", "sources[0].line_ids[0]"),
    ("clause_sans_statut", "sources[1].status"),
    ("clause_typage_non_booleen", "sources[0].kind_confirmed"),
    ("inconnu_objet", "answer.unknown[0]"),
    ("sans_inconnus", "answer.unknown"),
    ("faits_compris_en_chaine", "answer.faits_compris"),
    ("fait_compris_objet", "answer.faits_compris.evenement"),
    ("themes_non_liste", "answer.faits_compris.themes"),
    ("theme_objet", "answer.faits_compris.themes[1]"),
    ("clarification_objet", "answer.clarification"),
    # Revue Codex 1.9 (tour 3, I2), premier volet : `null` est une **valeur**, l'absence de la clé
    # n'en est pas une. Les routes publient avec `response_model_exclude_none=False`, donc pydantic
    # écrit toujours la clé. Tolérer son absence faisait **retrancher** en silence — le symétrique
    # exact de la réserve fabriquée du tour 2 : une clause sans son numéro de page (indiscernable
    # d'une clause du guide, qui n'en a légitimement pas), une clarification escamotée alors qu'elle
    # est la seule question que le système pose en retour, une ligne de « ce que j'ai compris »
    # disparue de l'endroit même où l'utilisateur vérifie qu'il a été compris.
    ("sans_clarification", "answer.clarification"),
    ("sans_faits_compris", "answer.faits_compris"),
    ("fait_compris_absent", "answer.faits_compris.cause"),
    ("clause_sans_cle_page", "sources[0].page"),
    ("claim_statut_sans_cle_applicable", "answer.claims[0].status.applicable"),
    # Second volet : la trace est **affichée** (un `<details>` que l'utilisateur déplie), et le
    # tour 2 ne l'avait durcie que sur son `request_id`. Un coût absent ne se peignait pas en « coût
    # inconnu » mais en **rien du tout**, alors que NFR4 veut le coût réel à l'écran ; un pipeline
    # ou une étape mal typés se peignaient en « étape [object Object] » sous le titre qui répond de
    # l'honnêteté de tout le reste.
    ("trace_sans_pipeline", "trace.pipeline"),
    ("trace_pipeline_objet", "trace.pipeline"),
    ("trace_sans_variante", "trace.variant"),
    ("trace_sans_cout", "trace.total_cost_eur"),
    ("trace_cout_en_chaine", "trace.total_cost_eur"),
    ("trace_cout_infini", "trace.total_cost_eur"),
    ("trace_sans_etapes", "trace.steps"),
    ("trace_etape_null", "trace.steps[0]"),
    ("trace_etape_sans_nom", "trace.steps[1].name"),
    ("trace_etape_nom_objet", "trace.steps[0].name"),
    ("trace_etape_tier_objet", "trace.steps[0].tier"),
    ("trace_etape_ms_en_chaine", "trace.steps[1].ms"),
    ("trace_etape_ms_negatif", "trace.steps[1].ms"),
    ("trace_etape_ms_fractionnaire", "trace.steps[1].ms"),
    ("trace_etape_sans_controles", "trace.steps[0].checks"),
    ("trace_controle_en_chaine", "trace.steps[1].checks[0]"),
    ("trace_controle_sans_nom", "trace.steps[1].checks[0].name"),
    ("trace_controle_sans_ok", "trace.steps[1].checks[0].ok"),
    ("trace_bloc_titre_nombre", "trace.blocs[0].titre"),
    ("trace_seuil_chaine", "trace.thresholds.max_opens"),
    ("trace_seuil_booleen", "trace.thresholds.max_opens"),
    ("trace_seuil_infini", "trace.thresholds.max_opens"),
    # Story 4.2f : le second porteur d'un `found=false` est lu aussi strictement que le premier.
    # Ses compteurs sont **affichés** au gestionnaire ; absents, négatifs ou non entiers, la page
    # chiffrerait une lecture que rien n'a mesurée. Et « exactement un porteur » vaut ici comme dans
    # le domaine : ni deux comptes rendus du même refus, ni aucun.
    ("lecture_sans_compteur", "answer.lecture_partielle.nodes_read"),
    ("lecture_compteur_negatif", "answer.lecture_partielle.blocks_read"),
    ("lecture_compteur_chaine", "answer.lecture_partielle.nodes_read"),
    ("lecture_non_objet", "answer.lecture_partielle"),
    ("lecture_documents_nuls", "answer.lecture_partielle.documents"),
    ("deux_porteurs", "answer.lecture_partielle"),
    ("aucun_porteur", "answer.reason"),
    ("lecture_sans_manque", "answer.unknown"),
    ("reason_sur_reponse_trouvee", "answer.reason"),
    ("lecture_sans_section", "answer.lecture_partielle.nodes_read"),
    ("lecture_sans_passage", "answer.lecture_partielle.blocks_read"),
    ("lecture_sur_reponse_trouvee", "answer.lecture_partielle"),
])
def test_un_200_incomplet_nest_pas_un_verdict(cas: dict[str, Any], nom: str, champ: str) -> None:
    """AD-16 : « réponse vide présentée comme réponse ». Un corps qu'aucune route ne peut écrire.

    `answer.verdict` en particulier : la route en publie **toujours** un (AD-16, un refus le porte).
    Peindre un corps sans verdict afficherait « verdict non reconnu » — c'est-à-dire un verdict de
    remplacement, exactement ce que l'AC interdit.
    """
    obtenu = cas["illisibles"][nom]
    assert obtenu is not None, "le corps aurait dû être refusé"
    assert obtenu["code"] == "reponse_illisible"
    assert obtenu["champ"] == champ


@pytest.mark.parametrize("nom", [
    "refus", "clarification", "faits_compris_null", "faits_compris_partiel",
    "clause_sans_page", "statut_sans_applicable", "listes_vides",
    # Tour 3 : ce que la trace a de légitimement creux. `restituer` n'appelle aucun modèle
    # (`tier: null`), une trace peut n'avoir aucune étape, et une analyse qui n'a rien coûté vaut
    # `0` — un zéro qui doit s'afficher (« cette analyse n'a rien coûté »), pas être refusé.
    "trace_sans_etape", "trace_cout_nul", "trace_etape_sans_appel",
    # Story 4.2f : le corps que la story fait naître est un corps **servable**. Un lecteur qui le
    # refuserait remplacerait le 503 par un écran illisible — une régression pire encore.
    "lecture_partielle",
])
def test_un_200_conforme_nest_jamais_refuse(cas: dict[str, Any], nom: str) -> None:
    """Le garde-fou du durcissement (revue Codex 1.9, tour 2, I2) : strict, pas inutilisable.

    Un lecteur qui descend jusqu'aux feuilles peut, d'un caractère, refuser des réponses
    parfaitement conformes — et l'outil n'afficherait alors plus jamais de verdict. Les champs
    `X | None` du contrat (`faits_compris`, `clarification`, `page`, `pertinente`, `applicable`)
    valent `null` de plein droit ; une liste vide est une réponse ordinaire (un refus n'a ni clause,
    ni question, ni thème) ; une trace sans étape et un coût nul en sont d'autres. Ces dix corps-là
    doivent passer, et c'est ce qui rend le relevé précédent lisible : les refus qu'il contient sont
    des refus **choisis**.
    """
    releve = cas["lisibles"][nom]
    assert releve["refuse"] is False, f"corps conforme refusé sur {releve['champ']}"
    assert releve["verdict"] in {"couvert", "non_couvert", "sous_conditions", "ne_tranche_pas"}


# --- AD-15 : la matérialisation ------------------------------------------

def test_tout_ce_qui_vient_du_serveur_est_pose_par_textcontent(cas: dict[str, Any]) -> None:
    """AD-15 : rendu **littéral**. Le DOM minimal lève sur toute pose d'`innerHTML` non vide.

    Arriver jusqu'à ces assertions le démontre : ni la citation, ni les faits compris n'ont pu
    passer par un chemin qui interpréterait du balisage.
    """
    dom = cas["dom"]
    assert dom["citation"] == "« <script>alert(1)</script> action subite »"
    assert dom["fait_compris"] == "<img onerror=alert(1)>"
    assert dom["badge"] == "Sous conditions"
    assert dom["badge_cls"] == "badge verdict-sous_conditions"


def test_la_page_ne_peint_aucun_bouton_dans_le_resultat(cas: dict[str, Any]) -> None:
    """AD-16 : aucun repli. Le seul bouton de la page est « Confronter », qui est dans le formulaire."""
    assert cas["dom"]["boutons"] == 0


def test_la_trace_est_un_details_natif(cas: dict[str, Any]) -> None:
    assert cas["dom"]["details"] >= 2
    # Story 5.6 (L2) : le premier `<details>` de la carte est celui des affirmations écartées, en
    # queue du bloc « Garde-fous ». Plus rien n'est replié **au-dessus** de la réponse.
    assert cas["dom"]["summary"] == "Affirmations écartées par la vérification"


def test_rien_du_sinistre_natteint_le_navigateur(cas: dict[str, Any]) -> None:
    """AD-15 : « aucune donnée utilisateur persistée » — ni conversation, ni sinistre en localStorage."""
    assert cas["dom"]["stockage"] == {}
    assert cas["soumission"]["stockage"] == {}


def test_une_erreur_efface_le_verdict_precedent(cas: dict[str, Any]) -> None:
    """AC : « sans conserver le verdict précédent à l'écran ». Le conteneur est vidé avant de peindre."""
    apres = cas["dom_apres_erreur"]
    assert apres["badges"] == 0
    assert apres["clauses"] == 0
    assert apres["portees"] == 0
    assert apres["boutons"] == 0
    assert apres["msg_dans_le_document"] == 1  # une seule carte : l'erreur a remplacé le verdict
    assert "sous conditions" not in apres["texte"]
    assert "indisponible" in apres["texte"]


# --- le formulaire piloté -------------------------------------------------

def test_le_selecteur_est_rempli_au_demarrage(cas: dict[str, Any]) -> None:
    """`GET /api/v1/sante` puis `GET /api/v1/documents` au chargement ; le formulaire s'ouvre ensuite."""
    demarrage = cas["demarrage"]
    assert [o["valeur"] for o in demarrage["options"]] == ["cg-mini", "cg-second", "cg-privee"]
    assert demarrage["select_desactive"] is False
    assert demarrage["bouton_desactive"] is False
    assert demarrage["message"] == ""
    # La sonde d'abord : elle porte les seuils, donc la borne d'abandon de tout ce qui suit.
    assert demarrage["ordre_des_appels"] == ["/api/v1/sante", "/api/v1/documents"]


def test_les_maxlength_sont_poses_par_le_script(cas: dict[str, Any]) -> None:
    """Une seule source à l'exécution (revue 1.9) ; l'attribut HTML reste le repli sans JavaScript."""
    poses = cas["demarrage"]["maxlength"]
    assert poses["description"] == 2000
    assert not poses["question"] and not poses["lieu"] and not poses["date"]


def test_la_description_libre_suffit_au_parcours_conversationnel(page: str) -> None:
    assert '<textarea id="description"' in page
    assert all(f'id="{identifiant}"' not in page
               for identifiant in ("question", "date", "lieu", "montant"))


def test_la_page_desarme_la_validation_native(page: str) -> None:
    """P16 : deux systèmes de messages ne peuvent pas coexister — la page garde le sien.

    `required` sans `novalidate` bloque l'événement `submit` avant que `manquant()` ne tourne : le
    message « Décrivez les faits… » que la page compose n'était vu que par le harnais, et le test
    qui l'asserte vérifiait un texte qu'aucun utilisateur ne recevait (revue 1.9, tour 2).
    """
    formulaire = re.search(r'<form[^>]*id="formulaire"[^>]*>', page)
    assert formulaire is not None and "novalidate" in formulaire.group(0)


def test_la_borne_dabandon_vient_des_seuils_du_serveur(cas: dict[str, Any]) -> None:
    """Convention Seuils : « tout seuil numérique dans `config.py` », pas recopié dans un front.

    `deadline_s` et `client_abort_margin_s` sont publiés par `thresholds()` et lus sur `/sante`,
    exactement comme `web/app/chat.js` le fait depuis 1.7. Les littéraux du script ne sont qu'un
    repli pour la première requête si la sonde n'a pas répondu — figer la somme aurait fait couper
    par le navigateur une requête à laquelle le serveur aurait répondu, le jour où `deadline_s` monte.
    """
    settings = Settings(_env_file=None, anthropic_api_key="")
    seuils = settings.thresholds()
    lus = cas["bornes"]["seuils_du_serveur"]
    assert lus["deadline_s"] == seuils["deadline_s"]
    assert lus["client_abort_margin_s"] == seuils["client_abort_margin_s"]
    assert cas["bornes"]["abandon_ms"] == round(
        (seuils["deadline_s"] + seuils["client_abort_margin_s"]) * 1000)
    # Avant la sonde, et si elle échoue, le repli tient — et la page reste utilisable.
    assert cas["bornes_avant_sonde"]["seuils_du_serveur"] is None
    assert cas["sonde_en_echec"]["bornes"]["seuils_du_serveur"] is None
    assert cas["sonde_en_echec"]["options"] == 3
    assert cas["sonde_en_echec"]["bouton_desactive"] is False
    # Une valeur absurde publiée par un serveur cassé ne déplace pas la borne : le repli reprend.
    assert cas["seuils_absurdes"]["abandon_ms"] == cas["bornes_avant_sonde"]["abandon_ms"]


def test_la_soumission_ne_recharge_pas_la_page_et_verrouille_la_saisie(cas: dict[str, Any]) -> None:
    assert cas["soumission_defaut_empeche"] is True
    assert cas["attente_peinte"] == 1  # l'attente est peinte **avant** l'appel
    assert cas["verrouille_pendant"] == [True, True, True]
    assert cas["soumission"]["verrouille_apres"] == [False, False, False]
    assert cas["soumission"]["attente_restante"] == 0


def test_la_soumission_poste_la_saisie_et_peint_le_verdict(cas: dict[str, Any]) -> None:
    corps = cas["soumission"]["corps"]
    assert corps["doc_id"] == "cg-mini"
    assert corps["faits"]["description"] == "Une bougie allumée est tombée sur le canapé."
    assert cas["soumission"]["badge"] == "Sous conditions"
    assert cas["soumission"]["cartes"] == 1


@pytest.mark.parametrize("cle, attendu", [("description_vide", "Décrivez les faits")])
def test_une_saisie_incomplete_ne_part_jamais_et_le_dit(cas: dict[str, Any], cle: str,
                                                        attendu: str) -> None:
    """Un appel modèle coûte : une saisie incomplète n'en déclenche aucun — **et le bouton parle**.

    Le retour silencieux de la première version (revue 1.9) donnait un bouton mort : le formulaire
    est `novalidate`… il ne l'est plus, mais le garde reste, et il compose un message.
    """
    releve = cas["saisie_incomplete"][cle]
    assert releve["appels"] == 0
    assert attendu in releve["texte"]
    assert releve["cartes_erreur"] == 1
    # AD-16 : même sur un refus local, aucun badge de verdict et aucun bouton.
    assert releve["badges"] == 0 and releve["boutons"] == 0


def test_manquant_nomme_ce_qui_manque_et_rien_dautre(cas: dict[str, Any]) -> None:
    assert cas["manquant"]["complet"] is None
    assert cas["manquant"]["sans_description"] and cas["manquant"]["sans_contrat"]
    assert cas["manquant"]["sans_question"] is None


def test_le_montant_a_trois_issues_et_non_deux(cas: dict[str, Any]) -> None:
    """Revue Codex 1.9 (tour 1, I1) : vide ≠ illisible.

    `Faits.montant_eur` est un `float | None` : le champ est **facultatif**, mais une saisie non
    vide qui n'est pas un nombre fini positif ou nul est une erreur de saisie, pas une absence.
    """
    m = cas["montant_saisi"]
    assert m["vide"] is None and m["blancs"] is None
    assert m["zero"] == 0 and m["entier"] == 1200
    assert m["virgule"] == 1200.5 and m["point"] == 1200.5  # la page lit « 1200,50 »
    for illisible in ("negatif", "mots", "infini", "nan"):
        assert m[illisible] == "illisible", illisible


def test_un_montant_illisible_ne_part_pas_ampute_mais_refuse(cas: dict[str, Any]) -> None:
    """Revue Codex 1.9 (tour 1, I1) : le supprimer du corps, c'était analyser d'autres faits.

    `novalidate` (revue 1.9, tour 2) a retiré au navigateur la validation de `min="0"` : `-100`
    atteignait donc `corpsSinistre()`, qui **supprimait** le champ. Le sinistre partait alors —
    quatre appels payés — sur des faits amputés de ce que l'utilisateur avait écrit, et l'écran ne
    disait rien. La page le refuse maintenant avant tout appel, et dit pourquoi.
    """
    assert cas["manquant"]["montant_vide"] is None  # facultatif : vide reste vide
    for cle in ("montant_negatif", "montant_mots"):
        assert "nombre en euros" in cas["manquant"][cle]
    releve = cas["montant_negatif_soumis"]
    assert releve["appels"] == 1  # l'ancien champ n'appartient plus à la surface conversationnelle


def test_une_liste_de_documents_en_echec_le_dit_sans_mentir(cas: dict[str, Any]) -> None:
    """« Aucun contrat servi » et « le serveur n'a pas répondu » sont deux choses (revue 1.9).

    Servir la première pour la seconde ferait affirmer à la page quelque chose qu'elle ne sait pas,
    et laissait un formulaire inerte sans un mot sur la cause.
    """
    echec = cas["documents_en_echec"]
    assert "n'a pas pu être chargée" in echec["message"]
    assert "Aucun contrat n'est servi" not in echec["message"]
    assert echec["bouton_desactive"] is True
    assert echec["cartes_erreur"] == 1  # l'erreur est peinte, avec sa référence de requête
    assert "r-503" in echec["texte"]
    assert echec["badges"] == 0


# --- la page elle-même ----------------------------------------------------

def test_lavertissement_demonstrateur_est_pres_du_formulaire(page: str) -> None:
    """AD-15, mot pour mot : « Démonstrateur public : ne saisissez aucune donnée réelle ou nominative »."""
    assert "Démonstrateur public : ne saisissez aucune donnée réelle ou nominative de sinistre." in page
    # « près du formulaire » : l'avertissement précède le formulaire dans le document.
    assert page.index("Démonstrateur public") < page.index('id="formulaire"')


def test_la_mention_de_confidentialite_dit_ce_que_la_politique_dit(page: str) -> None:
    """AD-15 amendé (revue 1.7) : durée, exceptions, lien et date de lecture — jamais plus généreux."""
    assert "sous 30 jours" in page
    # Les quatre réserves de la politique, pas deux : AD-15 nomme « accord de rétention différent »,
    # et la page n'en disait rien — la promesse y était plus généreuse que celle du tiers qui exécute
    # la requête (revue Codex 1.11). La liste est celle du guide, qui reste l'autorité.
    for reserve in ("service à rétention plus longue que vous contrôlez",
                    "accord de rétention différent", "politique d'usage",
                    "obligation légale", "deux ans"):
        assert reserve in page, reserve
    # **La date du guide, pas une date recopiée ici.** AD-15 : « trois formulations d'une même
    # promesse font trois promesses » — et deux dates de lecture différentes pour une même politique
    # en font deux. Les deux littéraux identiques que portaient les deux fichiers de tests ne les
    # couplaient que par accident : le jour où l'un est mis à jour et pas l'autre, la suite restait
    # verte avec deux fronts qui se contredisent. On lit donc la date **sur la page du guide**, qui
    # est l'autorité, et on exige que la page sinistre porte la même (revue 1.11).
    guide = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    dates_guide = set(re.findall(r"lue le \d{2}/\d{2}/\d{4}", guide))
    assert len(dates_guide) == 1, f"le guide doit porter une seule date de lecture : {dates_guide}"
    assert dates_guide.pop() in page, (
        "la page sinistre cite la politique à une autre date que le guide : une même politique, "
        "deux dates de lecture")
    assert "https://privacy.claude.com/" in page
    # Aucune promesse plus généreuse que celle du tiers qui exécute la requête.
    assert "non retenu par défaut" not in page
    assert "jamais conservé" not in page


def test_la_page_na_ni_build_ni_requete_tierce(page: str) -> None:
    """D8 : sans framework, sans requête tierce, et **sans** dépendance à `web/app/styles.css`."""
    scripts = re.findall(r'<script[^>]*src="([^"]+)"', page)
    assert scripts == ["sinistre.js?v=6"]
    # Aucune feuille de style externe : les styles sont dans la page, et surtout pas ceux du guide
    # (`web/app/styles.css`, 1 328 lignes taillées pour un autre DOM — une classe renommée là-bas
    # casserait celui-ci). Le commentaire d'en-tête l'explique ; ce qu'on vérifie, ce sont les
    # `<link>` réellement posés.
    assert re.findall(r"<link[^>]*>", page) == []
    # Le seul lien sortant est la politique de conservation du fournisseur, qu'AD-15 **exige**.
    liens = re.findall(r'(?:href|src)="(https?://[^"]+)"', page)
    politique = ("https://privacy.claude.com/en/articles/"
                 "7996866-how-long-do-you-store-my-organization-s-data")
    assert liens == [politique]


def test_les_identifiants_de_la_page_sont_ceux_que_le_script_cherche(page: str) -> None:
    """Un renommage dans la page ne doit pas laisser le script piloter un formulaire fantôme."""
    for identifiant in ("formulaire", "contrat", "contrats-message", "contrat-source",
                        "documents-audit", "description", "analyser", "resultat",
                        "lecteur-pdf", "lecteur-titre", "lecteur-statut", "lecteur-image",
                        "lecteur-sans-surlignage", "lecteur-precedent", "lecteur-suivant",
                        "lecteur-source", "lecteur-fermer"):
        assert f'id="{identifiant}"' in page, identifiant


def test_toute_classe_composee_par_le_script_est_stylee_dans_la_page(page: str) -> None:
    """La page est **autonome** (D8) : ce que le script compose, elle seule peut le styler.

    Une classe posée par `sinistre.js` sans règle dans le `<style>` inline ne se voit pas — c'est
    précisément ce qui rend la duplication dangereuse, et ce que la story 2.5 amarre. Les classes
    purement structurelles (celles qui n'existent que pour être relevées par le harnais) sont
    déclarées ici : elles doivent l'être une par une, jamais par une exception large.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    # Les classes littérales que `noeud()` reçoit — deuxième argument, toujours une chaîne.
    composees = set()
    for brut in re.findall(r'noeud\(\s*"[a-z0-9]+"\s*,\s*"([^"]+)"', source):
        composees |= {c for c in brut.split() if not c.startswith("verdict-")}
    for brut in re.findall(r'(?:section|liste)\(\s*"([a-z0-9-]+)"', source):
        composees.add(brut)
    # Structurelles : relevées par le harnais, sans rendu propre (leur parent les porte).
    # Sans rendu propre : ce sont des repères de structure (une `<section>`, un `<ul>`, un `<p>`
    # dont la mise en forme vient de son conteneur ou du style par défaut du navigateur), relevés
    # par le harnais et par rien d'autre. La liste est **explicite** : une classe nouvelle doit y
    # être ajoutée à la main, jamais couverte par une exception large.
    structurelles = {
        "resultat", "attente", "carte", "trace", "fc",
        # conteneurs de section, stylés par la règle `section { … }`
        "analyse", "ask", "clarif", "clauses", "escalate", "faits-compris", "inconnu", "paquet",
        "rejetees",
        # listes, stylées par `ul { … }` et `li { … }`
        "ask-liste", "escalate-liste", "inconnu-liste", "paquet-faits", "rejetees-liste",
        # paragraphes et fragments dont la mise en forme vient de leur conteneur
        "analyse-txt", "clarif-q", "cl-page", "cl-statut", "err-txt", "fc-val", "pq-txt",
        "source-lien",
        # Story 5.6 (L2) : `appui-extrait` est une variante de `.appui-texte`, `bloc-manques` et
        # `bloc-gardefous` n'ont que la règle commune `.bloc`, `dossier` que `.details-preuves`.
        "appui-extrait", "bloc-manques", "bloc-gardefous", "dossier",
    }
    for classe in sorted(composees - structurelles):
        assert re.search(rf"[.#][\w -]*\b{re.escape(classe)}\b[^{{]*\{{", page), (
            f"la classe « {classe} » est composée par le script et n'est stylée nulle part")


def test_le_fil_affiche_faits_questions_historique_et_copie(cas: dict[str, Any]) -> None:
    vue = cas["conversation_vue"]

    def textes(noeud: Any) -> list[str]:
        if not isinstance(noeud, dict):
            return []
        return ([noeud["texte"]] if isinstance(noeud.get("texte"), str) else []) + [
            texte for enfant in noeud.get("enfants", []) for texte in textes(enfant)]

    rendu = "\n".join(textes(vue))
    assert "Faits et provenance" in rendu
    assert "source : extrait par le modèle au premier tour" in rendu
    assert "Pour affiner le verdict" in rendu
    assert "Historique causal du verdict" in rendu
    assert "Copier le dossier" in rendu
    copie = cas["dossier_copie"]
    for fragment in ("Verdict :", "Raison :", "Faits retenus et provenances :",
                     "Clauses et pages :", "Paquet manquant :", "Questions restantes :",
                     "Référence de requête :"):
        assert fragment in copie
    assert "subite" in [fragment.casefold() for fragment in cas["fragments_decisifs"]]


def test_le_suivi_poste_le_jeton_et_l_identifiant_de_question(cas: dict[str, Any]) -> None:
    assert cas["suivi"]["url"].endswith("/api/v1/sinistre/suivi")
    assert cas["suivi"]["corps"] == {
        "doc_id": "cg-mini", "token": "etat.signe", "action": "reponse",
        "question_id": "q-1", "value": "oui",
    }


def test_les_handlers_rendus_selectionnent_verrouillent_et_ignorent_l_obsolete(
        cas: dict[str, Any]) -> None:
    handlers = cas["handlers_conversation"]
    assert handlers["questions"] == 3
    assert handlers["choix_corps"]["question_id"] == "q-2"
    assert handlers["choix_corps"]["value"] == "oui"
    # Story 5.6 (L2e) — le fait libre part **sans** `question_id` : il n'est la réponse d'aucune
    # question, et le coller à celle qui était sélectionnée l'affichait sous son intitulé.
    assert "question_id" not in handlers["libre_corps"]
    assert handlers["libre_corps"]["fait_libre"] == "preuve jointe"
    assert handlers["appels_apres_double_clic"] == 1
    assert all(handlers["verrouilles"])
    assert handlers["mises_a_jour"] == 3
    assert handlers["correction_corps"] == {
        "doc_id": "cg-mini", "token": "etat.signe", "action": "correction",
        "fact_key": "cause", "replaces_event_id": "f-1", "value": "nouvelle cause",
    }
    assert handlers["resolution_corps"] == {
        "doc_id": "cg-mini", "token": "etat.signe", "action": "resolution",
        "conflict_id": "conflit-1", "chosen_event_id": "f-1",
    }
    assert "copié" in handlers["copie_succes"]
    assert "échoué" in handlers["copie_echec"]
    assert cas["suivi_obsolete"]["mises_a_jour"] == 0
    refuse = cas["suivi_refuse"]
    assert refuse["mises_a_jour"] == 0 and refuse["faits_restants"] == 2
    assert refuse["deverrouille"] is True
    assert "dernier verdict valide reste affiché" in refuse["statut"]


def test_le_dossier_exclut_toutes_les_versions_d_un_conflit_ouvert(cas: dict[str, Any]) -> None:
    conflit = cas["dossier_conflit"]
    assert conflit == {
        "ouvert_bougie": False,
        "ouvert_court_circuit": False,
        "resolu_bougie": False,
        "resolu_court_circuit": True,
    }


def test_les_couleurs_de_la_page_ont_toutes_leur_variante_sombre(page: str) -> None:
    """La dette de contraste du rouge en dur (`#be123c`, aucune variante sombre, 2,88:1 sur fond
    sombre) ne doit pas rouvrir par une autre porte : toute couleur littérale posée en thème clair
    a sa contrepartie dans un bloc `prefers-color-scheme: dark`."""
    style = page[page.index("<style>"):page.index("</style>")]
    # Les blocs sombres, quel que soit leur formatage (sur une ligne ou sur dix) : on suit les
    # accolades plutôt que de deviner l'indentation.
    sombres = []
    for depart in [m.end() for m in re.finditer(r"@media \(prefers-color-scheme: dark\)\s*\{",
                                                style)]:
        profondeur, i = 1, depart
        while profondeur:
            profondeur += {"{": 1, "}": -1}.get(style[i], 0)
            i += 1
        sombres.append(style[depart:i - 1])
    sombres = "\n".join(sombres)
    assert sombres, "aucun bloc de thème sombre dans la page"
    # Les sélecteurs qui posent une couleur littérale en clair.
    for regle in re.findall(r"^  ([.#][^\n{]+)\{([^}]*)\}", style, flags=re.M):
        selecteur, corps = regle[0].strip(), regle[1]
        if not re.search(r"(?:^|[^-\w])color\s*:\s*#", corps) and "border-color: #" not in corps:
            continue
        classe = selecteur.split()[-1]
        assert classe in sombres, (
            f"{selecteur} pose une couleur en dur sans variante sombre : c'est exactement la dette "
            "que la story 2.5 referme")
    # Le rouge du site copié (`#be123c`) vit aussi ici, sur `.verdict-non_couvert` et `.erreur` —
    # mais **avec** sa variante sombre depuis la 1.9, ce qui est justement la règle ci-dessus. Ce
    # qui n'a pas de variante n'a pas le droit de rester, quelle que soit sa valeur.


def test_le_script_ne_pose_jamais_de_html(cas: dict[str, Any]) -> None:
    """AD-15, vérifié dans le source **et** à l'exécution : `innerHTML` ne sert qu'à vider."""
    source = SCRIPT.read_text(encoding="utf-8")
    poses = re.findall(r'\.innerHTML\s*=\s*(.+)', source)
    assert poses == ['"";'], poses
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source
    assert cas["dom"]["stockage"] == {}


# --- story 4.2f : la lecture partielle, sur l'écran d'un gestionnaire -----

def test_une_lecture_partielle_affiche_son_chiffre_et_ses_clauses_ecartees(
        cas: dict[str, Any]) -> None:
    """AC : « une réponse partielle chiffrée avec ses affirmations écartées, sans message
    d'indisponibilité, sans bouton de repli et sans état sûr ».

    Le bloc « Affirmations écartées par la vérification » était écrit depuis la story 1.9 et n'avait
    jamais pu être atteint sous troncature : la page ne recevait qu'un 503.
    """
    vu = cas["verdict_lecture_partielle"]
    assert vu["badge"] == [
        {"cls": "badge verdict-ne_tranche_pas", "texte": "Pas de clause qui s'applique"}]
    assert vu["clauses"] == 0
    assert vu["preuve"] == []  # aucune absence du contrat n'est affirmée
    assert vu["lecture"] == [
        "Lecture partielle : 1 section lue, 4 passages transmis au modèle "
        "— le reste n'a pas été lu, et rien n'en est affirmé"]
    assert vu["etat"] == ["etat etat-lecture-partielle"]
    assert vu["etat_texte"] == ["lecture partielle"]
    assert vu["inconnu_ligne"] and vu["faits_compris"] == [["Bien concerné", "mobilier de salon"]]
    # La clause écartée est enfin montrée — avec son motif, jamais avec sa citation (AD-3/D7).
    assert vu["rejetees"] == [["Une clause que la vérification a écartée.",
                               "citation introuvable dans le contrat", "non_retrouvee"]]
    assert "CETTE QUOTE" not in vu["texte_entier"]
    # Ni repli, ni message d'indisponibilité : rien n'est en panne.
    assert vu["boutons"] == 0 and vu["actions"] == 0
    assert "indisponible" not in vu["texte_entier"]


def test_letat_de_lecture_partielle_ne_se_confond_ni_avec_inconnu_ni_avec_sur(
        cas: dict[str, Any]) -> None:
    etats = cas["etats_lecture_partielle"]
    assert etats["porteur"]["texte"] == "lecture partielle"
    assert etats["sans_porteur"]["texte"] == "inconnu"
    phrases = cas["phrases_lecture_partielle"]
    assert phrases["avec_liste"].startswith("ma lecture s'est arrêtée avant de conclure")
    assert "Ce que je ne sais pas" in phrases["avec_liste"]
    assert "Ce que je ne sais pas" not in phrases["sans_liste"]
    assert "chiffré ci-dessus" in phrases["sans_liste"]
    assert "chiffré ci-dessus" not in phrases["sans_chiffre"]
    assert len(set(phrases.values())) == 4
    for phrase in phrases.values():
        assert "indisponible" not in phrase and "rien n'a été retenu" not in phrase


def test_le_chiffre_de_la_lecture_est_le_meme_texte_que_sur_le_guide(cas: dict[str, Any]) -> None:
    """Les deux fronts rendent la même donnée : deux phrases divergentes auraient dérivé."""
    textes = cas["lecture_textes"]
    assert textes["pluriel"].startswith("Lecture partielle : 3 sections lues, 12 passages")
    assert textes["singulier"].startswith("Lecture partielle : 1 section lue, 1 passage")
    assert textes["plancher"] == textes["singulier"]
    assert textes["absente"] == ""

# =========================================================================
# Story 5.6 (L2) — les quatre blocs, le surlignage, la progression.
# =========================================================================


def test_les_quatre_blocs_sont_dans_lordre_et_aucun_nest_replie(cas: dict[str, Any]) -> None:
    """AC : « quatre blocs fixes, jamais repliés, dans cet ordre ».

    C'est le défaut que Lancelot a vu en prod : en vue conversationnelle, « Clauses citées » était
    un `<details>` fermé, et la seule chose lisible était « ne tranche pas » suivi de questions.
    Le test porte donc sur l'ordre **et** sur le tag : un bloc peint en `<details>` repasserait.
    """
    vu = cas["verdict"]
    # Story 5.6 (L2b) : « Comment cette réponse a été obtenue » ne flotte plus après les blocs —
    # il ferme le bloc 4, celui qui parle déjà de ce que la vérification a établi.
    assert vu["blocs"] == ["bloc bloc-reponse", "bloc bloc-appuis", "bloc bloc-manques",
                           "bloc bloc-gardefous", "dossier details-preuves"]
    assert vu["blocs_titres"] == ["La réponse", "Sur quoi je m'appuie",
                                  "Ce qu'il me manque pour aller plus loin", "Garde-fous"]
    # Le numéro est devenu un pictogramme, posé par la feuille : le script pose un porteur vide et
    # décoratif, l'intitulé reste écrit à côté de lui.
    assert vu["blocs_icones"] == [{"tag": "span", "cls": "bloc-icone", "aria": "true"}] * 4
    assert vu["trace_dans_gardefous"] is True
    # Le dossier conversationnel vient **après** les quatre blocs, replié, et il s'appelle « Dossier ».
    assert vu["dossier_tag"] == ["details"] and vu["dossier_titre"] == ["Dossier"]


def test_la_reponse_souvre_par_sa_premiere_phrase(cas: dict[str, Any]) -> None:
    """AC : la première phrase en grand, puis l'explication. Aucune phrase n'est perdue."""
    vu = cas["verdict"]
    assert vu["reponse_phrase"] == ["La garantie vise l'action subite de la chaleur."]
    assert vu["reponse_suite"] == ["Une condition reste ouverte."]


def test_les_questions_decisives_ne_sont_posees_quune_fois(cas: dict[str, Any]) -> None:
    """Elles sont le bloc 3. Les laisser aussi dans le dossier doublerait les boutons de réponse
    et le champ libre sous une seule question sélectionnée."""
    # La fixture nominale n'a pas de conversation : aucune question active, donc aucune sélection.
    assert cas["verdict"]["questions"] == 0
    fil = cas["fil"]
    assert fil["questions"] == ["Le caractère subit est-il établi ?",
                                "Le dommage est-il accidentel ?",
                                "Une option a-t-elle été souscrite ?"]
    assert fil["bloc_porteur"] == ["bloc bloc-manques"]
    assert fil["dossier_porte_des_questions"] is False
    # Un seul champ libre et un seul jeu de boutons : la mécanique existante, déplacée, pas doublée.
    assert fil["champs_libres"] == 1
    assert fil["boutons_oui_non"] == ["Oui", "Non", "Je ne sais pas"]


# =========================================================================
# Story 5.6 (L2c) — la réponse en français, jamais un libellé technique à sa place.
#
# Ce que Lancelot a lu en prod sur `/sinistre/` : la réponse s'ouvrait sur « Verdict recalculé :
# sous conditions. », suivie d'une raison portant `axa-lu-optihome-2017:p37:11 : « … »` ; sous
# chaque clause, « Clause vérifiée conservée pour le recalcul du verdict. » ; et les questions
# s'appelaient « Les conditions particulières établissent-elles le point exigé ? » avec un
# « Envoyer la réponse libre » dont personne ne savait à quoi il répondait.
#
# Le tour moteur retire ces chaînes du contrat. Ces tests-ci disent que la page ne les afficherait
# pas même si elles revenaient : c'est la défense en profondeur, et elle porte sur les deux
# signatures qu'on peut reconnaître sans deviner — un `block_id` `document:pNN:MM`, et le préfixe
# « Verdict recalculé ».
# =========================================================================


def test_aucune_chaine_technique_natteint_laffichage_principal(cas: dict[str, Any]) -> None:
    """AC : filtrée de l'affichage principal, rangée dans « Comment cette réponse a été obtenue »."""
    vu = cas["l2c_technique"]
    # Bloc 1 : la transition « Faits compris — … » et les deux segments techniques sont écartés ;
    # il ne reste que la phrase que le modèle a écrite sur le contrat.
    assert vu["phrase"] == ["La garantie vise l'action subite de la chaleur."]
    assert vu["suite"] == []
    assert "cg-mini:p9:2" not in vu["bloc1_entier"]
    assert "Verdict recalculé" not in vu["bloc1_entier"]
    assert "Faits compris" not in vu["bloc1_entier"]
    # La raison n'est pas perdue : elle est rangée, entière, là où l'on documente la décision.
    assert vu["raison"] == []
    assert vu["raison_dans_la_trace"] == [
        "Clause vérifiée cg-mini:p9:2 : « … » conservée pour le recalcul du verdict."]
    # Bloc 2 : une affirmation technique n'est pas énoncée, et son rattachement s'en va avec elle ;
    # un rattachement technique tombe seul.
    assert vu["en_clair"] == ["Une condition reste ouverte."]
    assert vu["rattachement"] == []
    # « Ce que je ne sais pas » ne s'affiche que si le moteur l'a écrit : ni la ligne générique
    # (« Le verdict conserve des conditions… »), ni un identifiant de bloc.
    assert vu["inconnu_ligne"] == [] and vu["inconnu_liste"] == 0


def test_une_raison_technique_ne_remonte_pas_faute_de_trace_ou_la_ranger(
        cas: dict[str, Any]) -> None:
    """La seule exception à « elle ne disparaît jamais des deux » (L2b), et elle est délibérée.

    Sans `trace`, il n'y a nulle part où replier la raison, et une raison **en clair** reste donc
    sous la pastille. Une raison technique, non : la laisser remonter « au cas où » rouvrirait
    exactement la porte que ce tour ferme.
    """
    vu = cas["l2c_technique"]
    assert vu["raison_sans_trace"] == []
    assert vu["raison_propre_sans_trace"] == [
        "Une clause conditionne la garantie (au regard des conditions générales seules)"]


def test_sans_phrase_factuelle_le_bloc_dit_le_constat_et_le_verdict(cas: dict[str, Any]) -> None:
    """AC : « Aucune clause n'a pu être retenue » et le verdict — jamais une chaîne technique."""
    vu = cas["l2c_technique"]
    assert vu["muet_phrase"] == ["Aucune clause n'a pu être retenue"]
    assert vu["muet_badge"] == ["Sous conditions"]


def test_les_questions_sont_nommees_par_ce_quelles_servent_et_expliquent_leur_effet(
        cas: dict[str, Any]) -> None:
    """AC : titre « Pour affiner le verdict », la phrase d'explication en tête, les libellés.

    « Envoyer la réponse libre » ne disait pas à quoi ce champ répondait, et rien ne disait si la
    soumission rappelait le modèle. Les deux se lisent maintenant sur l'écran.
    """
    vu = cas["l2c_questions"]
    assert vu["titre"] == ["Pour affiner le verdict"]
    assert vu["explication"] == [
        "Vos réponses sont ajoutées au dossier comme des faits et le verdict est recalculé, "
        "sans relire le contrat ni rappeler le modèle."]
    # Chaque question s'affiche telle que le moteur l'écrit (`TargetQuestion.text`).
    assert vu["questions"] == ["Le caractère subit est-il établi ?",
                               "Le dommage est-il accidentel ?",
                               "Une option a-t-elle été souscrite ?"]
    assert vu["boutons"] == ["Oui", "Non", "Je ne sais pas"]
    assert vu["nom_du_champ"] == ["Préciser un fait"] and vu["aria"] == "Préciser un fait"
    assert vu["placeholder"] == ("ex. : la garantie dégâts des eaux figure dans mes conditions "
                                 "particulières")
    assert vu["envoyer"] == ["Ajouter ce fait"]


def test_apres_une_reponse_le_bloc_1_dit_le_recalcul_et_garde_la_reponse_lisible(
        cas: dict[str, Any]) -> None:
    """AC : « Verdict mis à jour avec vos réponses », les faits ajoutés, « Corriger » — et la
    réponse du modèle **reste** en dessous. C'est ce que « Verdict recalculé : sous conditions. »
    prétendait dire en occupant la place de la réponse."""
    vu = cas["l2c_maj"]
    assert vu["tete"] == ["Verdict mis à jour avec vos réponses"]
    # Seuls les faits que l'assuré a apportés : ceux extraits de sa description sont déjà dits par
    # « Ce que j'ai compris du sinistre ».
    assert vu["faits"] == ["caractère subit : oui"]
    assert vu["corriger"] == [["Corriger", "f-r"]]
    assert vu["phrase"] == ["La garantie vise l'action subite de la chaleur."]
    assert vu["suite"] == ["Une condition reste ouverte."]
    # Tant que rien n'a été répondu, aucun bandeau de recalcul : il n'y aurait rien à y mettre.
    assert vu["avant"] == []


# =========================================================================
# Story 5.6 (L2b) — l'aspect : ce qui se lit en premier, et ce qui se replie.
#
# Le tour L2 avait la bonne structure et le mauvais aspect : sept lignes d'avertissement avant le
# premier champ, un rapport d'ingestion au milieu de la carte de saisie, trois lignes grises
# redondantes sous le verdict, deux objets orphelins en bas d'écran. Rien n'est supprimé ici —
# tout est déplacé ou replié, et ces tests disent **où**.
# =========================================================================


def test_lavertissement_tient_en_une_ligne_et_garde_son_texte_entier(page: str) -> None:
    """AD-15 : l'obligation d'information ne se résume pas. Elle se replie.

    La ligne visible dit l'essentiel ; le `<details>` « Détails » porte le texte intégral, mot pour
    mot, y compris les quatre réserves de la politique du fournisseur et son lien. Un `<details>`
    **fermé par défaut** : aucun `open` sur la page.
    """
    tete = page[:page.index('id="formulaire"')]
    assert ("Démonstrateur public : ne saisissez aucune donnée réelle. Vos textes sont envoyés à "
            "Anthropic, rien n'est conservé ici." in tete)
    avert = page[page.index('class="avert" id="avertissement"'):page.index("<noscript>")]
    assert '<details class="avert-details">' in avert
    assert "<summary>Détails</summary>" in avert
    # Le texte long vit **après** le `<summary>`, donc dans le repli — jamais au-dessus.
    replie = avert[avert.index("<summary>Détails</summary>"):]
    for morceau in ("ne saisissez aucune donnée réelle ou nominative de sinistre",
                    "sous 30 jours", "accord de rétention différent", "jusqu'à deux ans",
                    "privacy.claude.com"):
        assert morceau in replie, morceau
    # Aucun repli de la page n'est ouvert au chargement : un `<details open>` est un bloc déplié
    # qui prétend être replié.
    assert "<details" in page and re.search(r"<details[^>]*\bopen\b", page) is None


def test_les_rapports_dingestion_sont_replies_sous_le_resultat(page: str) -> None:
    """L2b : la liste des contrats servis documente, elle ne décide pas.

    Elle occupait le milieu de la carte de saisie, entre le sélecteur de contrat et la description.
    Elle passe sous `#resultat`, dans un `<details>` fermé. Le sélecteur, lui, ne bouge pas : c'est
    lui qui décide, et il reste dans le formulaire.
    """
    assert page.index('id="resultat"') < page.index('id="documents-audit"')
    assert page.index('id="documents-audit"') > page.index("</form>")
    audits = page[page.index('<details class="audits">'):]
    assert audits.index('id="documents-audit"') < audits.index("</details>")
    assert ">Contrats servis et rapports d'ingestion<" in page
    # Le contrat, lui, se choisit toujours dans la carte de saisie.
    formulaire = page[page.index('id="formulaire"'):page.index("</form>")]
    assert 'id="contrat"' in formulaire and 'id="documents-audit"' not in formulaire


def test_la_pastille_de_verdict_se_lit_a_cote_de_la_premiere_phrase(cas: dict[str, Any]) -> None:
    """L2b : le verdict et la phrase qui l'énonce sont **une** information.

    Trois paragraphes les séparaient — la phrase, l'explication, puis la pastille : le lecteur
    lisait deux fois la même conclusion, à deux endroits. Ils partagent maintenant la tête du
    bloc 1, sur la même ligne dès que la largeur le permet.
    """
    assert cas["verdict"]["reponse_tete"] == [["reponse-phrase", "badge"]]


def test_les_quatre_blocs_portent_un_pictogramme_decoratif(cas: dict[str, Any]) -> None:
    """L2b : un numéro dit le rang d'un bloc, pas sa nature. Le pictogramme vient de la feuille
    (un masque SVG par classe de bloc) ; le script ne pose qu'un porteur vide et `aria-hidden`,
    parce que `className` n'est pas assignable sur un élément SVG et que l'intitulé, lui, est
    écrit à côté."""
    assert cas["verdict"]["blocs_icones"] == [
        {"tag": "span", "cls": "bloc-icone", "aria": "true"}] * 4
    assert "bloc-num" not in SCRIPT.read_text(encoding="utf-8")


def test_le_bord_gauche_dune_clause_dit_son_role(cas: dict[str, Any]) -> None:
    """L2b : garantie = accent, exclusion = rouge, condition = ambre, définition = gris.

    La classe vient du `kind` d'AD-2, jamais du modèle, et **seuls** les kinds de la table en
    reçoivent une : un kind inconnu garde l'accent par défaut plutôt qu'une couleur inventée.
    """
    roles = cas["roles_de_clause"]
    assert roles["garantie"] == "appui appui-role-garantie"
    assert roles["exclusion"] == "appui appui-role-exclusion"
    assert roles["condition"] == "appui appui-role-condition"
    assert roles["definition"] == "appui appui-role-definition"
    assert roles["invente"] == "appui", "un kind hors table n'a pas de couleur de rôle"
    page = PAGE.read_text(encoding="utf-8")
    for role, teinte in (("garantie", "--accent"), ("exclusion", "--rouge"),
                         ("condition", "--ambre"), ("definition", "--gris")):
        regle = re.search(rf"\.appui-role-{role}[^{{]*\{{([^}}]*)\}}", page)
        assert regle and teinte in regle.group(1), role


def test_ce_qui_documente_ne_flotte_plus_apres_les_blocs(cas: dict[str, Any]) -> None:
    """L2b : la pastille d'état et « Comment cette réponse a été obtenue » pendaient après les
    quatre blocs, séparées d'eux par un filet — deux objets dont rien ne disait à quoi ils se
    rapportaient. Ils vivent dans le bloc 4, celui qui parle déjà de ce que la vérification a
    établi. La portée, elle, n'est plus dite qu'une fois, au même endroit."""
    vu = cas["verdict"]
    assert vu["trace_dans_gardefous"] is True
    assert vu["portee_dans_gardefous"] is True
    assert vu["etat"] and all(e.startswith("etat ") for e in vu["etat"])


@pytest.mark.parametrize("valeur", sorted(get_args(VerdictValue)))
def test_chaque_valeur_du_domaine_a_un_libelle_lisible(cas: dict[str, Any], valeur: str) -> None:
    """AC : « traduis toutes, ne laisse aucune valeur brute ».

    Le paramétrage vient de `VerdictValue` lui-même (`domain/verdict.py`) : une cinquième valeur
    ajoutée au domaine fera rougir ce test tant que la page ne saura pas la dire.
    """
    libelles = cas["libelles_verdict"][valeur]
    for forme in ("avec_clause", "sans_clause"):
        texte = libelles[forme]["texte"]
        assert libelles[forme]["cle"] == valeur, (valeur, forme)
        assert texte and texte != valeur and "_" not in texte, (valeur, forme)
        assert texte[0].isupper(), (valeur, forme)


def test_les_libelles_de_verdict_sont_ceux_valides_par_la_maquette(cas: dict[str, Any]) -> None:
    assert cas["verdicts_table"] == {
        "couvert": "Couvert",
        "non_couvert": "Exclu",
        "sous_conditions": "Sous conditions",
        "ne_tranche_pas": "Je ne peux pas trancher",
    }
    ne_tranche = cas["libelles_verdict"]["ne_tranche_pas"]
    assert ne_tranche["sans_clause"]["texte"] == "Pas de clause qui s'applique"


def test_une_valeur_de_verdict_inconnue_nest_jamais_traduite(cas: dict[str, Any]) -> None:
    """AD-16 : le serveur aurait rendu quelque chose que le contrat ne prévoit pas. L'afficher comme
    un verdict connu serait le dégradé silencieux que cet AD interdit."""
    inconnu = cas["libelles_verdict"]["farfelu"]
    assert inconnu["avec_clause"] == {"cle": "inconnu", "texte": "verdict non reconnu"}
    assert inconnu["sans_clause"] == {"cle": "inconnu", "texte": "verdict non reconnu"}


def test_pas_de_clause_qui_sapplique_se_dit_sur_labsence_daffirmation(cas: dict[str, Any]) -> None:
    """La distinction porte sur les **affirmations retenues**, pas sur `sources[]`.

    Une claim retenue dont l'appariement a échoué reste une claim retenue : dire « pas de clause qui
    s'applique » au-dessus d'un verdict fondé sur une affirmation serait un mensonge d'affichage.
    """
    vu = cas["verdict_sans_clause"]
    assert vu["aucune_claim"] == ["Pas de clause qui s'applique"]
    assert vu["claims_sans_source"] == ["Je ne peux pas trancher"]


# --- le bloc 2 : le paragraphe entier, citation surlignée ----------------

def test_la_quote_est_retrouvee_malgre_apostrophes_et_espaces(cas: dict[str, Any]) -> None:
    """AD-3 : la quote est ré-extraite du corpus aux offsets prouvés — mais le fil transporte des
    apostrophes typographiques, des espaces insécables et les retours à la ligne de la mise en page
    du PDF. Un `indexOf` nu échouait sur des paragraphes où la citation est pourtant là."""
    vu = cas["surlignage"]
    assert vu["bornes"] == {"debut": 26, "fin": 87}
    # Le surlignage porte sur le texte **original** : apostrophe typographique, espace insécable et
    # retour à la ligne sont conservés tels quels.
    assert vu["extrait"] == "l’écoulement de l’eau\ndes installations, par suite de rupture"
    assert vu["introuvable"] is None
    assert vu["quote_vide"] is None


def test_une_clause_affiche_son_chemin_son_paragraphe_et_sa_phrase_en_clair(
        cas: dict[str, Any]) -> None:
    vu = cas["appui_avec_bloc"]
    assert vu["chemin"] == ["3.1.4 Dégâts des eaux › Étendue de la garantie"]
    assert vu["paragraphe"].startswith("3.1.4.1 Le contrat couvre")
    assert vu["paragraphe"].endswith("l’oubli d’un robinet.")
    assert vu["marques"] == [
        "l’écoulement de l’eau\ndes installations, par suite de rupture"]
    assert "La garantie vise l'action subite de la chaleur." in vu["en_clair"]
    # Sous le seuil : le paragraphe entier est affiché d'emblée, sans bouton.
    assert vu["extraits"] == 0 and vu["boutons_plus"] == 0


def test_le_rattachement_aux_faits_se_lit_sous_la_clause_et_seulement_sil_existe(
        cas: dict[str, Any]) -> None:
    """Story 5.6 (L1c) : deux lignes, deux natures, dans cet ordre.

    La première est ce que le contrat écrit, et la citation au-dessus la soutient. La seconde dit
    que ce qui est arrivé au déclarant est ce que cette clause nomme — aucune citation ne peut le
    prouver, et c'est pourquoi elle ne se fond pas dans la première. Une clause qui ne nomme aucun
    fait déclaré n'affiche rien de plus qu'avant.
    """
    vu = cas["appui_rattachement"]
    assert vu["en_clair"][0] == "La garantie vise l'action subite de la chaleur."
    assert vu["rattachement"] == [
        "Une bougie tombée sur le canapé est une action subite de la chaleur."]
    assert vu["ordre"] == ["appui-clair", "appui-rattachement"]
    assert vu["sans"] == []


def test_une_citation_introuvable_dans_son_bloc_saffiche_seule(cas: dict[str, Any]) -> None:
    """Le corpus servi et le bloc publié peuvent diverger : la page ne surligne rien au jugé."""
    vu = cas["appui_citation_introuvable"]
    assert vu["marques"] == 0
    assert vu["note"] and "sans surlignage inventé" in vu["note"][0]
    assert vu["texte"][0].startswith("« événement soudain")


def test_un_bloc_long_montre_la_phrase_et_ses_voisines_avec_un_bouton(cas: dict[str, Any]) -> None:
    """AC : « phrase citée + une phrase avant et après, et un bouton "Voir le paragraphe entier" »."""
    vu = cas["appui_bloc_long"]
    assert vu["seuil_depasse"] is True
    assert vu["bouton"] == ["Voir le paragraphe entier"]
    assert vu["extrait"].count("Une phrase de remplissage") == 2, "une phrase avant, une après"
    assert "La phrase qui décide est ici." in vu["extrait"]
    # Le paragraphe entier est **posé masqué**, pas absent : aucun aller-retour serveur au clic.
    assert vu["entier_masque"] == ["hidden"]


def test_le_depliage_est_a_la_demande_et_jamais_linverse(cas: dict[str, Any]) -> None:
    """Un contenu qui se referme sous le lecteur est exactement ce que cette refonte retire."""
    vu = cas["depliage"]
    assert vu["avant"] == {"entier": "hidden", "extrait": None, "bouton": None}
    assert vu["apres"] == {"entier": None, "extrait": "hidden", "bouton": "hidden"}


def test_une_clause_ecartee_passe_en_retrait_apres_les_autres(cas: dict[str, Any]) -> None:
    """AC : « les clauses citées comme ne s'appliquant pas sont affichées en retrait, plus
    discrètes ». L'ordre de lecture suit la décision, pas l'ordre de publication du serveur."""
    vu = cas["appuis_ecartes"]
    assert vu["ordre"] == ["appui appui-role-condition",
                           "appui appui-role-garantie appui-ecarte"]
    # La clause écartée est publiée **en premier** par le serveur et affichée en second.
    assert vu["chemins"] == ["Une condition reste ouverte.",
                             "La garantie vise l'action subite de la chaleur."]


def test_un_claim_id_sur_la_clause_rattache_sans_appariement_positionnel(
        cas: dict[str, Any]) -> None:
    """Contrat du tour moteur : `sources[i].claim_id` dit qui cite ce bloc. L'appariement de D6
    tombait ici — les sources sont dans l'ordre inverse des claims —, et le `claim_id` le rattrape
    sans rien deviner."""
    assert cas["appuis_par_claim_id"] == [
        {"bloc": "cg-mini:p12:3", "texte": "Une condition reste ouverte."},
        {"bloc": "cg-mini:p9:2", "texte": "La garantie vise l'action subite de la chaleur."}]


# --- le bloc 4 : les garde-fous -----------------------------------------

def test_les_garde_fous_chiffrent_ce_qui_a_ete_relu_et_retire(cas: dict[str, Any]) -> None:
    """AC : « N citations relues », « M phrases retirées » (0 affiché aussi), et la règle."""
    vu = cas["phrases_retirees"]
    assert vu["gf_zero"][0] == "2 citations relues mot pour mot dans le contrat"
    assert vu["gf_zero"][1] == "0 phrase retirée"
    assert vu["gf_trois"][1] == "3 phrases retirées : aucun passage ne les soutenait"
    assert vu["gf_zero"][2] == "Verdict calculé par des règles fixes, pas par le modèle"


def test_le_compte_des_phrases_retirees_vient_du_controle_et_jamais_dun_defaut(
        cas: dict[str, Any]) -> None:
    """`segments_retires` est aujourd'hui le **seul** porteur de ce nombre sur le fil.

    Contrôle absent ⇒ aucune phrase retirée, donc zéro. Contrôle présent mais détail non chiffré ⇒
    la page dit la chose **sans** le nombre, plutôt que d'en inventer un. Une reprise différée
    demande au moteur de publier ce compteur typé.
    """
    vu = cas["phrases_retirees"]
    assert vu["aucun_controle"] == 0
    assert vu["trois"] == 3
    assert vu["sans_detail"] is None
    assert vu["gf_sans_detail"][1] == "Des phrases ont été retirées : aucun passage ne les soutenait"


# --- la progression -----------------------------------------------------

def test_le_flux_de_progression_annonce_ses_etapes_et_rend_le_resultat(
        cas: dict[str, Any]) -> None:
    vu = cas["progression_flux"]
    assert vu["urls"] == ["/api/v1/sinistre/progression"], "un seul appel, aucun repli"
    assert [e["libelle"] for e in vu["etapes"]] == ["Je lis le contrat", "J'écris la réponse"]
    assert [e["rang"] for e in vu["etapes"]] == [0, 1]
    assert vu["verdict"] == "sous_conditions"


def test_une_route_de_progression_absente_bascule_puis_nest_plus_sondee(
        cas: dict[str, Any]) -> None:
    """404 : la route n'existe pas, rien n'a tourné, rien n'a coûté — l'appel classique qui suit est
    le premier et le seul. Et la sonder à chaque soumission ajouterait un aller-retour par
    soumission : le 404 vaut pour la page."""
    assert cas["progression_absente"]["urls"] == [
        "/api/v1/sinistre/progression", "/api/v1/sinistre", "/api/v1/sinistre"]


def test_un_flux_coupe_avant_le_resultat_bascule_une_seule_fois(cas: dict[str, Any]) -> None:
    vu = cas["progression_coupee"]
    assert vu["urls"] == ["/api/v1/sinistre/progression", "/api/v1/sinistre"]
    assert vu["verdict"] == "sous_conditions"


def test_un_flux_coupe_apres_le_resultat_ne_renvoie_jamais_la_requete(
        cas: dict[str, Any]) -> None:
    """**La** garde contre le double appel payant : le serveur a déjà fait, et facturé, le travail."""
    vu = cas["progression_coupee_apres_resultat"]
    assert vu["urls"] == ["/api/v1/sinistre/progression"]
    assert vu["verdict"] == "sous_conditions"


def test_un_503_sur_le_flux_nouvre_aucun_repli(cas: dict[str, Any]) -> None:
    """AD-16 : « aucun repli pour le sinistre ». Un serveur qui refuse n'est pas un serveur absent."""
    vu = cas["progression_503"]
    assert vu["urls"] == ["/api/v1/sinistre/progression"]
    assert vu["code"] == "llm_unavailable" and vu["kind"] == "indisponible"


def test_un_corps_lisible_seulement_dun_bloc_nest_pas_redemande(cas: dict[str, Any]) -> None:
    """Sans `ReadableStream`, on perd l'avancement en direct — pas la réponse. Jeter un corps déjà
    payé pour redemander la même chose à la route classique le paierait deux fois."""
    vu = cas["progression_sans_flux"]
    assert vu["urls"] == ["/api/v1/sinistre/progression"]
    assert vu["verdict"] == "sous_conditions"


def test_lattente_nomme_ses_etapes_et_porte_un_chronometre(cas: dict[str, Any]) -> None:
    """AC : « jamais d'écran figé sans mouvement »."""
    vu = cas["attente"]
    assert vu["etapes_nommees"] == ["Je lis le contrat", "J'écris la réponse",
                                    "Je vérifie chaque citation"]
    depart = vu["depart"]
    assert [e["etat"] for e in depart["etapes"]] == ["en cours", "à venir", "à venir"]
    assert depart["chrono"] == ["0:00"]
    milieu = vu["milieu"]
    assert [e["etat"] for e in milieu["etapes"]] == ["terminé", "en cours", "à venir"]
    assert milieu["chrono"] == ["0:41"]


def test_lavancement_estime_se_dit_comme_une_estimation(cas: dict[str, Any]) -> None:
    """Une barre qui avance toute seule ne doit pas se faire passer pour une mesure."""
    vu = cas["attente"]
    assert "estimé sur le temps écoulé" in vu["depart"]["note"][0]
    assert "estimé" not in vu["milieu"]["note"][0]
    assert "Le serveur annonce l'étape en cours" in vu["milieu"]["note"][0]
    # La dernière étape reste « en cours » indéfiniment : une barre qui atteint la fin avant la
    # réponse ment sur ce qui se passe.
    assert vu["rangs"] == [0, 0, 1, 1, 2, 2]
    assert vu["duree_annoncee"] == 60


def test_la_duree_annoncee_na_pas_de_seuil_serveur_correspondant() -> None:
    """AC : « lue depuis les seuils de `/sante` si un tel seuil existe, sinon constante nommée ».

    Il n'y en a pas : `Settings.thresholds()` publie des **bornes** (`deadline_s`, les plafonds de
    jetons), pas une durée typique. La constante est donc nommée dans le script, et ce test tient la
    prémisse — le jour où `thresholds()` publiera une durée attendue, il rougira.
    """
    publies = set(Settings().thresholds())
    attendus = {n for n in publies if "duree" in n or "typique" in n or "attendu" in n}
    assert not attendus, (
        f"le serveur publie désormais {sorted(attendus)} : la phrase d'attente doit s'y adosser "
        "plutôt que sur la constante `DUREE_ANNONCEE_S`")


# --- les corps figés, rendus bloc par bloc -------------------------------
#
# `tests/data/front/*.json` porte cinq réponses **complètes** — un cas couvert, un cas sous
# conditions, un cas où aucune clause ne s'applique, la réponse que la production a rendue sur le
# robinet oublié, et ce même corps au **tour 1** —, construites sur les blocs réels du contrat AXA
# (`tests/data/axa/*.txt`).
# `tests/js/sinistre_rendus.mjs` les fait traverser la lecture stricte d'AD-11 puis la peinture, et
# relève ce que chacun des quatre blocs montre.
#
# Story 5.6 (L2e) — `sinistre-robinet-suivi.json` est ce même corps augmenté du dossier d'un tour 1
# (deux faits apportés, dont un fait libre sans `question_id`, et la mention de recalcul publiée
# deux fois). Les trois défauts du parcours du 03/09 s'y voient ensemble.
#
# Story 5.6 (L2d) — `sinistre-robinet-reel.json` est le corps **réel** de la route de production
# (`automation/runs/20260903-lisibilite/prod-final/s2-robinet.json`), copié sans retouche. Les deux
# défauts lus à l'écran s'y voient tous les deux, et rien d'autre que ce corps ne les porte : les
# fixtures taillées à la main n'ont ni clause d'ouverture de section citée avant la garantie, ni
# énumération coupée en deux blocs par la mise en page du PDF.
#
# Ce que ces cas prouvent et que les fixtures minimales ne prouvent pas : la refonte tient sur des
# réponses entières, avec des paragraphes de contrat réels — dont un qui dépasse le seuil de
# dépliage —, et l'ordre des blocs ne dépend pas de la forme de la réponse.

HARNAIS_RENDUS = REPO_ROOT / "tests" / "js" / "sinistre_rendus.mjs"


@pytest.fixture(scope="module")
def rendus() -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        motif = "node absent de la machine : les rendus figés ne peuvent pas être produits"
        if REQUIS:
            pytest.fail("FRONT_TESTS_REQUIS=1 mais " + motif)
        pytest.skip(motif)
    fini = subprocess.run([node, str(HARNAIS_RENDUS)], capture_output=True, text=True, timeout=120,
                          cwd=str(REPO_ROOT), check=False)
    assert fini.returncode == 0, f"le harnais a échoué ({fini.returncode}) :\n{fini.stderr}"
    charge = json.loads(fini.stdout)
    assert charge.get("ok") is True, charge.get("erreur")
    return charge["rendus"]


@pytest.mark.parametrize("nom", ["sinistre-couvert", "sinistre-sous-conditions",
                                 "sinistre-sans-clause", "sinistre-robinet-reel",
                                 "sinistre-robinet-suivi"])
def test_les_quatre_blocs_tiennent_sur_une_reponse_entiere(rendus: dict[str, Any],
                                                           nom: str) -> None:
    """L'ordre est le même quelle que soit la forme de la réponse, et rien n'est replié avant les
    garde-fous — c'est exactement ce qui manquait en production."""
    vu = rendus[nom]
    attendu = ["bloc bloc-reponse", "bloc bloc-appuis", "bloc bloc-manques", "bloc bloc-gardefous",
               "dossier details-preuves"]
    if vu["bloc2"] is None:  # un refus n'a aucune clause à montrer : le bloc 2 n'existe pas
        attendu.remove("bloc bloc-appuis")
    assert vu["ordre"] == attendu
    assert vu["replies_avant_les_gardefous"] == 0
    assert vu["bloc1"]["phrase"], "la réponse s'ouvre toujours par une phrase"
    assert vu["bloc3"]["pieces"], "les pièces non lues tiennent en une ligne, sur les trois cas"


def test_le_cas_couvert_montre_sa_garantie_et_le_paragraphe_entier(
        rendus: dict[str, Any]) -> None:
    vu = rendus["sinistre-couvert"]
    assert vu["bloc1"]["verdict"] == "Couvert"
    assert vu["bloc1"]["verdict_cls"] == "badge verdict-couvert"
    assert vu["bloc1"]["inconnu"] == []
    garantie, definition = vu["bloc2"]
    assert garantie["chemin"] == ("3.1.1 Incendie et périls assimilés › "
                                  "3.1.1.1 Étendue de la garantie › 3.1.1.1.6 Dégâts au mobilier")
    assert garantie["page"] == "page 34" and garantie["type"] == "garantie"
    # Le paragraphe du contrat est rendu **entier**, et la partie citée y est surlignée.
    assert garantie["paragraphe"].startswith("3.1.1.1.6 Les dégâts occasionnés au mobilier")
    assert garantie["paragraphe"].endswith("ni commencement d'incendie.")
    assert garantie["surligne"] == [
        "par un événement soudain, résultant de l'action subite de la chaleur"]
    assert garantie["en_clair"].startswith("La garantie vise l'action subite")
    assert garantie["bouton_entier"] == [], "ce bloc tient sous le seuil"
    # La définition du « contenu » dépasse le seuil : extrait + bouton, entier posé masqué.
    assert definition["bouton_entier"] == ["Voir le paragraphe entier"]
    assert definition["paragraphe_entier_masque"] == ["hidden"]
    assert vu["bloc4"]["lignes"][0] == "2 citations relues mot pour mot dans le contrat"
    assert vu["bloc4"]["etat"] == ["sûr"] and vu["bloc4"]["ecartees"] == []


def test_le_cas_sous_conditions_met_la_clause_ecartee_en_retrait(rendus: dict[str, Any]) -> None:
    vu = rendus["sinistre-sous-conditions"]
    assert vu["bloc1"]["verdict"] == "Sous conditions"
    assert vu["bloc1"]["inconnu"][0].startswith("Ce que je ne sais pas :")
    garantie, exclusion = vu["bloc2"]
    assert garantie["ecartee"] is False and exclusion["ecartee"] is True
    assert exclusion["chemin"] == "3.1.8 Extensions de garantie › Exclusions propres aux extensions"
    assert exclusion["surligne"] == [
        "sont exclus en l'absence d'embrasement ou de commencement d'incendie"]
    assert vu["bloc3"]["demandes"] == [
        "Vos conditions particulières mentionnent-elles une extension aux points 3.1.8.3 à 3.1.8.6 ?"]
    assert vu["bloc3"]["faits_exiges"] == ["l'extension souscrite au titre du bâtiment"]
    assert vu["bloc4"]["lignes"][1] == "1 phrase retirée : aucun passage ne la soutenait"
    # L'affirmation écartée reste consultable, derrière les garde-fous, dépliable.
    assert vu["bloc4"]["ecartees_repliees"] == ["details"]
    assert "franchise de 150" in vu["bloc4"]["ecartees"][0]
    # Et sa citation n'est **jamais** montrée (D7) : elle est restée la chaîne du modèle.
    assert "franchise de 150 EUR" not in vu["bloc4"]["ecartees"][0]


def test_le_cas_sans_clause_dit_pas_de_clause_qui_sapplique(rendus: dict[str, Any]) -> None:
    """Le cas que Lancelot a vu en prod : « ne tranche pas », des questions, et rien d'autre."""
    vu = rendus["sinistre-sans-clause"]
    assert vu["bloc1"]["verdict"] == "Pas de clause qui s'applique"
    # Story 5.6 (L2c) : le serveur ne rend ici **aucun** segment `factuel` — il n'y a pas de clause
    # dont dire quoi que ce soit. Le bloc s'ouvre alors sur le constat, jamais sur une chaîne de
    # service ; ce que le modèle a écrit de sa lecture reste juste en dessous, mot pour mot.
    assert vu["bloc1"]["phrase"] == "Aucune clause n'a pu être retenue"
    assert vu["bloc1"]["explications"][0].startswith("Aucune garantie des conditions générales")
    assert vu["bloc2"] is None
    assert vu["bloc4"]["lignes"][0] == "0 citation relue mot pour mot dans le contrat"
    assert vu["bloc4"]["lignes"][1] == "0 phrase retirée"
    assert vu["bloc4"]["preuve"] == [
        "Termes cherchés : souillure, déjection, animal domestique, canapé — "
        "12 variantes essayées, 1457 passages parcourus"]
    assert vu["bloc4"]["etat"] == ["inconnu"]


# --- story 5.6 (L2d) : le titre répond, l'amorce reste avec son item ------
#
# Lancelot a lu la capture de la réponse robinet rendue par le code de L2c
# (`automation/runs/20260903-lisibilite/reel-l2c/sinistre-light-1200.png`). Deux défauts, tous deux
# de lecture et non de contrat :
#
#   1. le titre du bloc 1 était « Les présentes conditions spéciales sont applicables si… » —
#      la première phrase factuelle rendue, qui est la **condition d'application** de la section ;
#      un assuré qui cherche s'il est couvert lisait d'abord à quelle condition la section vaut ;
#   2. sept cartes pour quatre appuis : le PDF coupe chaque énumération entre la phrase qui
#      l'appelle (« … c'est-à-dire : ») et ses items, et la page en faisait deux cartes portant le
#      même chemin, la même phrase en clair et deux boutons « Voir la page ».


def test_lamorce_ne_se_fond_que_si_les_trois_conditions_tiennent(cas: dict[str, Any]) -> None:
    """La règle est déterministe, et chacune de ses trois conditions est nécessaire.

    Une fusion devinée est plus grave que la carte de trop qu'elle évite : elle rangerait un appui
    distinct sous un autre, sous un verdict. Le cas fait donc varier **un seul point à la fois** —
    l'affirmation, le nœud, ce qui atteste que le bloc en appelle un autre, l'ordre du document.

    Story 5.6 (L2e) : l'adjacence `n → n+1` n'en fait plus partie. Le contrat introduit une
    *section* (« Ne sont pas assurés, les dommages causés : ») et ses items sont des nœuds enfants,
    plusieurs blocs plus loin — les exiger voisins faisait une carte de plus par item.
    """
    vu = cas["l2d_amorce"]
    assert vu["fondue"] == {
        "cartes": 1,
        "amorces": ["La Compagnie assure les biens désignés c'est-à-dire :"],
        "blocs": [["cg-mini:p9:2", "cg-mini:p9:3"]]}
    # Un bloc sauté, puis un nœud enfant sur une autre page : la fusion tient dans les deux cas.
    assert vu["distants"]["blocs"] == [["cg-mini:p9:2", "cg-mini:p9:5"]]
    assert vu["noeud_enfant"]["blocs"] == [["cg-mini:p9:2", "cg-mini:p10:4"]]
    for fondu in ("distants", "noeud_enfant"):
        assert vu[fondu]["cartes"] == 1, fondu
        assert vu[fondu]["amorces"] == [
            "La Compagnie assure les biens désignés c'est-à-dire :"], fondu
    # « contexte » est ce que le moteur dit d'un bloc qui n'est pas décisif seul : il vaut le
    # deux-points, et une amorce sans ponctuation d'appel se fond quand même.
    assert vu["statut_contexte"]["cartes"] == 1
    assert vu["statut_contexte"]["blocs"] == [["cg-mini:p9:2", "cg-mini:p9:3"]]
    # Et les quatre gardes tiennent : une autre affirmation, un autre nœud, rien qui appelle une
    # suite, ou un item que l'amorce ne peut pas annoncer parce qu'il la précède dans le contrat.
    for garde in ("claims_differentes", "chemin_different", "sans_appel", "item_avant_lamorce"):
        assert vu[garde]["cartes"] == 2, garde
        assert vu[garde]["amorces"] == [], garde
        assert all(len(b) == 1 for b in vu[garde]["blocs"]), garde


def test_une_amorce_partagee_se_fond_dans_chacun_de_ses_items(cas: dict[str, Any]) -> None:
    """Le défaut lu sur le vrai serveur le 03/09 : huit cartes pour cinq appuis.

    Le contrat n'introduit ses exclusions qu'une fois — « Ne sont pas assurés, les dommages
    causés : », nœud `3.1.4.2 Exclusions` — et les trois exclusions citées sont des nœuds enfants.
    Le moteur cite donc **le même bloc** sous trois affirmations ; la page en faisait trois cartes
    de plus, chacune sans un mot à elle, portant la phrase en clair de sa voisine.
    """
    vu = cas["l2e_amorce_partagee"]
    assert vu["sources"] == 8 and vu["cartes"] == 5
    # L'amorce partagée se lit une fois par exclusion, au-dessus du paragraphe qu'elle ouvre.
    assert vu["amorces"] == ["Ne sont pas assurés, les dommages causés :"] * 3
    # Les deux garanties gardent leur carte, et chaque exclusion ouvre **les deux** blocs.
    assert vu["blocs"] == [
        ["cg-mini:p37:13"],
        ["cg-mini:p38:2"],
        ["cg-mini:p38:5", "cg-mini:p38:6"],
        ["cg-mini:p38:5", "cg-mini:p38:12"],
        ["cg-mini:p38:5", "cg-mini:p38:17"]]


def test_le_titre_de_la_reponse_est_la_phrase_qui_repond_pas_la_condition(
        rendus: dict[str, Any]) -> None:
    """La phrase en grand est la première rattachée à une affirmation dont la citation principale
    est une `garantie`. La condition n'est pas perdue pour autant : elle reste dans le corps, à sa
    place — la première des explications, comme le modèle l'a écrite."""
    vu = rendus["sinistre-robinet-reel"]["bloc1"]
    assert vu["phrase"].startswith(
        "Le contrat assure les biens désignés contre les dégâts des eaux, c'est-à-dire notamment")
    assert vu["explications"][0].startswith(
        "Les présentes conditions spéciales sont applicables si les conditions particulières")
    # Aucune phrase du modèle ne disparaît : les dix segments factuels et la limite sont tous à
    # l'écran, le titre plus les explications groupées par trois.
    assert vu["explications"] and all(vu["explications"])


def test_le_titre_reste_la_premiere_phrase_quand_rien_ne_la_designe(
        rendus: dict[str, Any]) -> None:
    """La règle ne déplace jamais une réponse dont la première phrase répond déjà, et elle ne
    s'applique pas du tout à un refus : sans affirmation, il n'y a pas de citation principale à
    lire, et le constat garde sa place."""
    assert rendus["sinistre-couvert"]["bloc1"]["phrase"].startswith(
        "Le contrat couvre les brûlures faites au mobilier")
    assert rendus["sinistre-sous-conditions"]["bloc1"]["phrase"].startswith(
        "Le contrat peut couvrir ce dégât")
    assert rendus["sinistre-sans-clause"]["bloc1"]["phrase"] == "Aucune clause n'a pu être retenue"


def test_lamorce_dune_enumeration_se_fond_dans_la_carte_de_son_item(
        rendus: dict[str, Any]) -> None:
    """Neuf citations, six cartes : les trois amorces rejoignent l'item qu'elles annoncent.

    Chaque fusion se prouve sur les trois choses qui étaient doublées à l'écran — une seule phrase
    en clair, un seul bouton « Voir la page », et l'amorce en ligne au-dessus du paragraphe — et sur
    la seule chose qui devait grandir : le bouton ouvre désormais **les deux** blocs.
    """
    cartes = rendus["sinistre-robinet-reel"]["bloc2"]
    assert len(cartes) == 6
    assert [c["amorce"] for c in cartes] == [
        "",
        "La Compagnie assure les biens désignés contre les dégâts des eaux c\u2019est-à-dire :",
        "La Compagnie étend sans supplément de prime la portée de la garantie :",
        "",
        "La Compagnie garantit la responsabilité civile qu\u2019un Assuré pourrait encourir sur la "
        "base des articles 1382 à 1386 du Code civil, à l\u2019égard d\u2019un tiers, en raison de "
        "dommages causés par le fait :",
        "",
    ]
    fondue = cartes[1]
    assert fondue["paragraphe"].startswith("• l\u2019écoulement de l\u2019eau des installations")
    assert fondue["ouvre_le_pdf"] == ["Voir la page 37 dans le PDF"]
    assert fondue["blocs_ouverts"] == [["axa-lu-optihome-2017:p37:13",
                                        "axa-lu-optihome-2017:p37:14"]]
    # Une carte, une phrase en clair : c'était la répétition la plus visible de la capture.
    assert fondue["en_clair"].startswith("Le contrat assure les biens désignés")
    assert cartes[2]["blocs_ouverts"] == [["axa-lu-optihome-2017:p38:3",
                                           "axa-lu-optihome-2017:p38:4"]]
    assert cartes[4]["blocs_ouverts"] == [["axa-lu-optihome-2017:p50:2",
                                           "axa-lu-optihome-2017:p50:3"]]
    # Ce qui n'est pas une amorce reste une carte à soi : la condition d'ouverture de la section
    # (p37:11) et l'exclusion de responsabilité civile (p50:18) ne se fondent dans rien.
    assert cartes[0]["type"] == "condition" and cartes[0]["blocs_ouverts"] == [
        ["axa-lu-optihome-2017:p37:11"]]
    assert cartes[5]["type"] == "exclusion"
    # Et le compte des citations relues ne bouge pas : neuf passages ont bien été vérifiés.
    assert rendus["sinistre-robinet-reel"]["bloc4"]["lignes"][0] == (
        "9 citations relues mot pour mot dans le contrat")


def test_aucune_fusion_sur_les_corps_figes_sans_enumeration_coupee(
        rendus: dict[str, Any]) -> None:
    """Les trois corps d'avant n'ont aucune amorce : leurs cartes sont inchangées, aucune n'a
    absorbé sa voisine. Deux blocs qu'aucune adjacence ne relie ne se fondent jamais."""
    for nom in ("sinistre-couvert", "sinistre-sous-conditions"):
        cartes = rendus[nom]["bloc2"]
        assert len(cartes) == 2
        assert [c["amorce"] for c in cartes] == ["", ""]
        assert all(len(c["blocs_ouverts"][0]) == 1 for c in cartes)


def test_lattente_se_met_a_jour_en_place_et_non_par_repeinture(cas: dict[str, Any]) -> None:
    """`#resultat` porte `aria-live="polite"`.

    Repeindre la carte entière chaque seconde était deux fautes en une : un lecteur d'écran aurait
    relu la barre et son chronomètre à chaque seconde pendant une minute, et un nœud remplacé perd
    le focus qu'il portait. Le repère posé sur la première étape survit à la mise à jour : c'est la
    preuve qu'aucun `replaceChild` ni aucun vidage de conteneur n'a eu lieu.
    """
    vu = cas["attente_en_place"]
    assert vu["maj"] is True
    assert vu["repere_survivant"] == "1"
    assert vu["cartes"] == 1
    assert vu["classes"] == ["prog-etape prog-faite", "prog-etape prog-faite",
                             "prog-etape prog-encours"]
    assert vu["mots"] == ["terminé", "terminé", "en cours"]
    assert vu["chrono"] == "0:41"
    assert "Le serveur annonce l'étape en cours" in vu["note"]
    # Le serveur annonce un autre nombre d'étapes : la structure a changé, il faut repeindre.
    assert vu["maj_structure_changee"] is False


def test_aria_busy_est_pose_avant_la_premiere_peinture_de_lattente() -> None:
    """Une région `aria-live` qui reçoit la barre **avant** d'être marquée occupée la ferait
    annoncer, puis relire à chaque mise à jour."""
    source = SCRIPT.read_text(encoding="utf-8")
    verrou = source.index("verrouiller(true);\n        var attente = suivreAttente(null);")
    assert verrou > 0


# --- story 5.6 (L2e) : ce qui s'applique, ce qui est écarté, le fait libre ----
#
# L'orchestrateur a piloté la vraie page sur le vrai serveur sur trois tours (candidat `a565a8d`,
# transcriptions dans `automation/runs/20260903-lisibilite/parcours/`). Trois défauts de front, tous
# dans le bloc 1, et tous du même genre : la page disait quelque chose que le code n'avait pas
# décidé.
#
#   1. le corps mêlait ce qui s'applique et ce qui a été écarté. « Le robinet laissé ouvert […] est
#      une installation ou un appareil hydraulique visé par cette exclusion » se lisait au milieu de
#      la réponse alors que la table avait jugé cette exclusion `applicable = non` ;
#   2. « Préciser un fait » partait comme la réponse à la question sélectionnée, et s'affichait sous
#      son intitulé : « défaut de réparation ou d'entretien à l'origine du sinistre : Le voisin du
#      dessous a fait constater… » ;
#   3. « Verdict mis à jour avec vos réponses, sans relire le contrat : sous conditions. » était
#      écrit deux fois de suite en bas du corps après deux tours.
#
# `sinistre-robinet-suivi.json` est le corps réel du robinet, augmenté du dossier d'un tour 1 : les
# trois défauts s'y voient ensemble, ce qu'aucune fixture minimale ne fait.


def test_les_phrases_des_clauses_ecartees_sortent_du_corps(rendus: dict[str, Any]) -> None:
    """Le corps ne garde que les affirmations `oui` / `humain` ; les `non` se lisent après lui.

    L'appariement passe par `claim_ids` des segments, jamais par le texte : c'est `c6`
    (`applicable = non`, `hors_portee`) qui porte les deux phrases sorties, et rien d'autre du
    corps ne bouge — la condition d'application de la section (`c1`, `humain`) reste à sa place.
    """
    vu = rendus["sinistre-robinet-reel"]["bloc1"]
    assert vu["ecartees_tete"] == ["Clauses examinées qui ne s'appliquent pas ici"]
    raisons = [r for r, _ in vu["ecartees"]]
    phrases = [t for _, t in vu["ecartees"]]
    assert raisons == ["portée", "portée"], "la raison courte vient d'`applicable_reason`"
    assert phrases[0].startswith("La garantie de responsabilité civile immeuble exclut")
    assert phrases[1].startswith("L'inondation subie par le voisin")
    # Elles ont bien **quitté** le corps : ni le titre, ni les explications ne les portent.
    corps = " ".join([vu["phrase"]] + vu["explications"])
    for phrase in phrases:
        assert phrase not in corps
    # Et la section vient après le corps, jamais dedans.
    assert vu["apres_le_corps"] == len(vu["explications"])
    # Le titre reste la première phrase d'une garantie applicable.
    assert vu["phrase"].startswith(
        "Le contrat assure les biens désignés contre les dégâts des eaux, c'est-à-dire notamment")


def test_une_reponse_sans_clause_ecartee_nouvre_aucune_section(rendus: dict[str, Any]) -> None:
    """Rien n'est inventé quand il n'y a rien à écarter — et une phrase que **deux** affirmations
    citent, dont une seule est écartée, reste dans le corps : la sortir en dirait plus que la table
    n'a décidé. C'est le cas `sous-conditions` (`cl1` applicable, `cl2` hors portée)."""
    for nom in ("sinistre-couvert", "sinistre-sous-conditions", "sinistre-sans-clause"):
        vu = rendus[nom]["bloc1"]
        assert vu["ecartees_tete"] == [], nom
        assert vu["ecartees"] == [], nom
        assert vu["apres_le_corps"] is None, nom
    assert rendus["sinistre-sous-conditions"]["bloc1"]["phrase"].startswith(
        "Le contrat peut couvrir ce dégât")


def test_un_fait_libre_se_lit_comme_une_precision_pas_comme_une_reponse(
        rendus: dict[str, Any]) -> None:
    """Un fait sans `question_id` n'est la réponse d'aucune question : il se dit tel quel.

    La réponse à une question garde, elle, son intitulé — c'est ce qui la rend corrigeable et
    traçable jusqu'à la question qui l'a posée.
    """
    vu = rendus["sinistre-robinet-suivi"]["bloc1"]
    assert vu["maj_tete"] == ["Verdict mis à jour avec vos réponses"]
    assert vu["maj_faits"] == [
        "garantie dégâts des eaux : oui",
        "Vous avez précisé : Le voisin du dessous a fait constater des dégâts sur son plafond "
        "pour 2 500 euros"]


def test_une_phrase_identique_nest_jamais_ecrite_deux_fois_dans_le_corps(
        rendus: dict[str, Any]) -> None:
    """Le serveur publiait la mention de recalcul une fois par tour ; il corrige l'accumulation, et
    la page ne redit de toute façon jamais mot pour mot ce qu'elle vient d'écrire."""
    mention = "Verdict mis à jour avec vos réponses, sans relire le contrat : sous conditions."
    vu = rendus["sinistre-robinet-suivi"]["bloc1"]
    corps = " ".join([vu["phrase"]] + vu["explications"])
    assert corps.count(mention) == 1
    for nom in ("sinistre-couvert", "sinistre-sous-conditions", "sinistre-sans-clause",
                "sinistre-robinet-reel", "sinistre-robinet-suivi"):
        assert rendus[nom]["bloc1"]["corps_sans_doublon"] is True, nom
