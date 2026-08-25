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
from typing import Any

import pytest

from server.app.api.schemas import SinistreRequest
from server.app.config import REPO_ROOT, Settings
from server.app.domain.question import Faits
from server.app.domain.verdict import PORTEE, VerdictValue

HARNAIS = REPO_ROOT / "tests" / "js" / "sinistre_cases.mjs"
PAGE = REPO_ROOT / "tools" / "sinistre" / "index.html"
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
    for identifiant, valeur in [("question", attendues["question_max"]),
                                ("description", attendues["description_max"]),
                                ("lieu", attendues["lieu_max"])]:
        assert f'id="{identifiant}"' in page
        assert f'maxlength="{valeur}"' in page, identifiant
    # `date` n'en porte **pas**, et c'est volontaire : `maxlength` ne s'applique pas à
    # `<input type="date">`, le navigateur l'ignore, et l'écrire ferait croire à une borne posée là
    # où il n'y en a aucune. La borne du domaine reste le contrôle réel, y compris pour un appelant
    # qui n'est pas un navigateur — et elle est assertée ci-dessus comme les trois autres.
    champ_date = re.search(r'<input id="date"[^>]*>', page)
    assert champ_date is not None and "maxlength" not in champ_date.group(0)


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
    assert "édition non précisée" in options[1]["texte"]
    assert cas["formulaire"]["actif"] is True
    assert cas["formulaire"]["message"] is None


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

def test_le_badge_et_la_portee_sont_en_tete(cas: dict[str, Any]) -> None:
    """AC : le badge du verdict et la mention de portée, tous deux posés par `textContent`."""
    badge = cas["verdict"]["badge"]
    assert len(badge) == 1
    assert badge[0]["texte"] == "sous conditions"
    assert badge[0]["cls"] == "badge verdict-sous_conditions"
    assert cas["verdict"]["portee"] == [
        "au regard des conditions générales seules — verdict non validé par un expert assurance"]


def test_la_portee_du_front_contient_celle_du_domaine(cas: dict[str, Any]) -> None:
    """AD-6 : `PORTEE` est invariable côté serveur ; la page en dit autant, plus la réserve d'expert."""
    assert PORTEE in cas["portee"]
    assert "non validé par un expert" in cas["portee"]


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
    assert cas["verdict"]["raison"] == [
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
    """AD-6 : `MissingPackage` accompagne **toujours** le verdict, et alimente `ask_client`."""
    paquet = " ".join(cas["verdict"]["paquet"])
    for piece in ("conditions particulières", "options souscrites", "avenants", "date d'effet"):
        assert piece in paquet
    assert "caractère subit de l'action de la chaleur" in paquet
    assert cas["verdict"]["ask"] == [
        "Le contrat porte-t-il l'option correspondante ?",
        "Le caractère subit de l'action de la chaleur est-il établi ?"]
    assert cas["verdict"]["escalate"] == ["Faire relire la clause par un gestionnaire."]


def test_chaque_clause_affiche_son_type_sa_page_et_ses_statuts(cas: dict[str, Any]) -> None:
    """UX-DR6 (c) / D5 : le type vient de `Block.kind` (AD-6, seule source), la page du bloc."""
    clauses = cas["verdict"]["clauses"]
    assert len(clauses) == 2
    assert clauses[0]["quote"].startswith("« événement soudain")
    assert clauses[0]["meta"][0] == "garantie"
    assert clauses[0]["meta"][1] == "page 9"
    assert "applicabilité à confirmer par un humain" in clauses[0]["meta"][2]
    assert "actualité non vérifiée" in clauses[0]["meta"][2]


def test_un_typage_non_confirme_est_dit(cas: dict[str, Any]) -> None:
    """D5 : un `kind` non confirmé vaut `applicable="humain"` — l'afficher sans le dire mentirait."""
    condition = cas["verdict"]["clauses"][1]
    assert condition["meta"][0] == "condition"
    assert condition["meta"][1] == "typage non confirmé"


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
    note = cas["verdict"]["rejetees_note"][0]
    assert "non retrouvée" not in note and "n'a pas été retrouvée" not in note
    assert "écartées" in note


def test_les_quatre_motifs_de_rejet_ont_une_phrase_en_francais(cas: dict[str, Any]) -> None:
    """AD-4 : quatre `rejection_kind`. Un cinquième inconnu ne rend pas la page muette."""
    assert len(cas["rejets"]) == 5
    assert len(set(cas["rejets"][:4])) == 4  # quatre motifs distincts
    assert all(m and m == m.strip() for m in cas["rejets"])


def test_ce_que_je_ne_sais_pas_est_affiche(cas: dict[str, Any]) -> None:
    assert cas["verdict"]["inconnu"] == ["La franchise applicable n'est pas dite."]


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
    assert titres == ["Ce que l'analyse a coûté", "Cette requête"]
    assert cas["trace_pauvre"]["seuils"] is None
    for rubrique in cas["trace_pauvre"]["rubriques"]:
        for ligne in rubrique["lignes"]:
            assert ligne["texte"].strip()


def test_une_trace_qui_na_rien_a_dire_nouvre_pas_un_panneau_vide(cas: dict[str, Any]) -> None:
    """Un `<details>` qui n'a que son `<summary>` promet une explication qu'il ne donne pas."""
    assert cas["trace_muette"] is None
    assert cas["trace_absente"] is None
    # Un coût **nul** n'est pas un silence : aucun appel n'a été facturé, et c'est une mesure.
    assert [r["titre"] for r in cas["trace_cout_nul"]["rubriques"]] == ["Ce que l'analyse a coûté"]
    assert cas["trace_cout_nul"]["rubriques"][0]["lignes"][0]["texte"] == (
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
    assert releve["badge"] == ["ne tranche pas"]
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
    assert refus["badge"] == [{"cls": "badge verdict-ne_tranche_pas", "texte": "ne tranche pas"}]
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
    assert dom["badge"] == "sous conditions"
    assert dom["badge_cls"] == "badge verdict-sous_conditions"


def test_la_page_ne_peint_aucun_bouton_dans_le_resultat(cas: dict[str, Any]) -> None:
    """AD-16 : aucun repli. Le seul bouton de la page est « Confronter », qui est dans le formulaire."""
    assert cas["dom"]["boutons"] == 0


def test_la_trace_est_un_details_natif(cas: dict[str, Any]) -> None:
    assert cas["dom"]["details"] == 1
    assert cas["dom"]["summary"] == "Comment cette réponse a été obtenue"


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
    assert poses["question"] == 1000
    assert poses["description"] == 2000
    assert poses["lieu"] == 200
    # Rien sur `date` : le script ne pose pas un attribut que le navigateur ignore.
    assert not poses["date"]


def test_les_centimes_sont_saisissables_dans_un_navigateur(page: str) -> None:
    """`Faits.montant_eur` est un `float` et la page lit « 1200,50 » — encore faut-il pouvoir l'écrire.

    Avec `step="1"`, la validation native du navigateur bloque la soumission **entière** dès qu'un
    montant porte des centimes : le chemin décimal n'était joué que par le harnais Node (revue 1.9,
    tour 2). `inputmode="decimal"` disait déjà le contraire de `step="1"` dans le même attribut.
    """
    montant = re.search(r'<input id="montant"[^>]*>', page)
    assert montant is not None
    assert 'step="any"' in montant.group(0)
    assert 'step="1"' not in montant.group(0)


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
    assert cas["soumission"]["badge"] == "sous conditions"
    assert cas["soumission"]["cartes"] == 1


@pytest.mark.parametrize("cle, attendu", [
    ("description_vide", "Décrivez les faits"),
    ("question_vide", "ne peut pas être vide"),
])
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
    assert all(cas["manquant"][c] for c in ("sans_description", "sans_question", "sans_contrat"))
    assert len({cas["manquant"][c] for c in ("sans_description", "sans_question", "sans_contrat")}) == 3


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
    assert releve["appels"] == 0
    assert "nombre en euros" in releve["texte"]
    assert releve["cartes_erreur"] == 1 and releve["badges"] == 0


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
    assert scripts == ["sinistre.js"]
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
    for identifiant in ("formulaire", "contrat", "contrats-message", "contrat-source", "question",
                        "date", "lieu", "montant", "description", "analyser", "resultat"):
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
    }
    for classe in sorted(composees - structurelles):
        assert re.search(rf"[.#][\w -]*\b{re.escape(classe)}\b[^{{]*\{{", page), (
            f"la classe « {classe} » est composée par le script et n'est stylée nulle part")


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
