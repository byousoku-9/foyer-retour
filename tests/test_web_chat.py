"""Matrice d'E/S du front du guide (spec 1.7) : AD-11 (a–f), AD-15, AD-16, AD-4, FR11, NFR4.

Le harnais `tests/js/chat_cases.mjs` charge `web/app/kb.js` puis `web/app/chat.js` dans un `node:vm`
avec `window`, `location` et `fetch` **doublés**, exécute les cas et écrit du JSON. Il ne juge rien :
tout ce qui est affirmé l'est ici. Aucun navigateur, aucun réseau, aucune dépendance ajoutée à
`pyproject.toml` — le harnais n'utilise que la bibliothèque standard de Node.

`ui.js` n'est pas couvert : depuis la story 1.7 il ne fait plus que peindre ce que `chat.js`
compose, et la peinture se vérifie dans un Chrome headless (`docs/tests-live.md`). Ce qui est
vérifié ici est tout ce qui décide : le corps de la requête, le mapping de l'historique, la lecture
du contrat, la classification des erreurs, l'appariement des citations et les textes composés.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from server.app.config import REPO_ROOT, Settings
from server.app.domain.question import Turn

HARNAIS = REPO_ROOT / "tests" / "js" / "chat_cases.mjs"


@pytest.fixture(scope="module")
def cas() -> dict[str, Any]:
    """Le harnais est exécuté **une fois** pour tout le module ; chaque test lit un relevé."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent de la machine : les cas du front ne peuvent pas tourner "
                    "(l'image de CI de la story 1.11 en a un)")
    fini = subprocess.run([node, str(HARNAIS)], capture_output=True, text=True, timeout=120,
                          cwd=str(REPO_ROOT))
    charge = json.loads(fini.stdout) if fini.stdout.strip() else {"ok": False, "erreur": fini.stderr}
    assert charge.get("ok") is True, charge.get("erreur")
    assert fini.stderr == "", f"le harnais a écrit sur stderr : {fini.stderr}"
    return charge["cas"]


# --- AD-11 (a) : l'origine courante ---------------------------------------

def test_api_base_est_lorigine_de_la_page(cas: dict[str, Any]) -> None:
    """AD-12 : une seule origine. `localhost:8787` en dur sortait le front de cette origine."""
    assert cas["api_base"] == "https://foyer-retour.example"
    assert cas["corps_url"] == "https://foyer-retour.example/chat"
    assert cas["sonde_url"] == "https://foyer-retour.example/sante"


def test_les_chemins_historiques_sont_conserves(cas: dict[str, Any]) -> None:
    """AD-11 a créé `/sante` et `/chat` « pour un minimum de changements » : on ne les change pas."""
    assert cas["corps_url"].endswith("/chat")
    assert cas["sonde_url"].endswith("/sante")


# --- AD-11 (b) : la sonde tourne sur toute origine ------------------------

def test_la_sonde_interroge_le_serveur_hors_localhost(cas: dict[str, Any]) -> None:
    """La garde « hors localhost, inutile d'essayer » éteignait le mode api en production."""
    assert cas["sonde_methode"] == "GET"
    assert cas["sonde_ok"] is True


# --- AD-11 (c) et (d) : le corps envoyé -----------------------------------

def test_le_corps_porte_le_profil_brut_et_pas_sa_description(cas: dict[str, Any]) -> None:
    corps = cas["corps_envoye"]
    assert set(corps) == {"question", "profil", "historique"}, "ni `contexte`, ni champ en trop"
    assert isinstance(corps["profil"], dict)
    assert corps["profil"]["horizon"] == "Je viens d'arriver"
    assert corps["question"] == "Quel délai ai-je pour déclarer mon arrivée à la commune ?"
    assert cas["corps_methode"] == "POST"
    assert cas["corps_entetes"] == {"Content-Type": "application/json"}


def test_le_profil_envoye_passe_entier_le_filtre_du_serveur(cas: dict[str, Any]) -> None:
    """Les six champs du questionnaire du site franchissent `PROFIL_KEYS` — `horizon` compris."""
    from server.app.domain.profil import Profil

    envoye = cas["corps_envoye"]["profil"]
    assert Profil(**envoye).filtered() == envoye


def test_lhistorique_est_mappe_en_role_texte(cas: dict[str, Any]) -> None:
    for tour in cas["corps_envoye"]["historique"]:
        assert set(tour) == {"role", "texte"}
        Turn(**tour)  # le contrat du serveur l'accepte tel quel


def test_le_dernier_tour_utilisateur_egal_a_la_question_est_exclu(cas: dict[str, Any]) -> None:
    """Le site pousse la question dans l'historique **avant** l'appel : l'envoyer deux fois la
    ferait résoudre contre elle-même."""
    envoye = cas["corps_envoye"]["historique"]
    assert envoye[-1]["texte"] == "Bonjour, posez votre question."
    assert cas["corps_envoye"]["question"] not in [t["texte"] for t in envoye]
    # Et le même historique, une fois l'écho retiré, se termine bien sur le tour précédent.
    assert cas["historique_dernier_tour_exclu"][-1]["texte"] == "tour 9"


def test_lhistorique_long_est_borne_aux_six_derniers_tours(cas: dict[str, Any]) -> None:
    """`historique_max_turns` : au-delà le serveur rend 400, et il ne tronque jamais lui-même."""
    envoye = cas["historique_long"]
    assert len(envoye) == Settings(_env_file=None, anthropic_api_key="").historique_max_turns == 6
    assert [t["texte"] for t in envoye] == ["tour %d" % i for i in range(4, 10)]


def test_un_tour_surdimensionne_est_ecarte_jamais_coupe(cas: dict[str, Any]) -> None:
    """`Turn.texte ≤ 2 000` : couper changerait ce qui a été dit, écarter ne fabrique rien."""
    envoye = cas["historique_tour_surdimensionne"]
    textes = [t["texte"] for t in envoye]
    assert "x" * 2400 not in textes
    assert not any(len(t) > 2000 for t in textes)
    assert "petit 1" in textes and "petit 2" in textes  # les autres tours partent
    assert "y" * 2000 in textes                          # 2 000 pile est admis par le contrat
    for tour in envoye:
        Turn(**tour)


def test_un_historique_absent_ne_casse_rien(cas: dict[str, Any]) -> None:
    assert cas["historique_vide"] == []


# --- AD-11 (e) et (f) : la lecture du contrat -----------------------------

def test_la_reponse_est_lue_sur_le_contrat_servi(cas: dict[str, Any]) -> None:
    lue = cas["reponse_lue"]
    assert lue["via"] == "api/v1"
    assert lue["texte"].startswith("Vous avez huit jours")
    assert [s["kind"] for s in lue["segments"]] == ["factuel", "factuel"]
    assert lue["fiches"] == ["arrivee"]
    assert lue["unknown"] == []
    assert lue["comparateur"] is False


def test_le_champ_reponse_de_lancien_contrat_nest_plus_lu(cas: dict[str, Any]) -> None:
    """`j.reponse` était le champ du relais d'origine ; le contrat d'AD-11 rend `texte`."""
    ancien = cas["ancien_contrat"]
    assert ancien["a_champ_reponse"] is False
    assert ancien["texte"] == ""  # rien n'est repêché sur `reponse`


def test_les_sources_affichees_sont_celles_du_serveur(cas: dict[str, Any]) -> None:
    """AD-3 : ce qui est montré comme source est le passage **relu du corpus** par le serveur."""
    lue = cas["reponse_lue"]
    assert [s["block_id"] for s in lue["sources"]] == [
        "lux-guide:farrivee:2", "lux-guide:farrivee:3", "lux-guide:farrivee:5"]
    assert [s["quote"] for s in lue["sources"]][0] == "huit jours pour déclarer votre arrivée"
    assert all(s["status"] == "verifiee" for s in lue["sources"])
    # Le moteur lexical n'a pas tourné : les sources ne viennent pas de `rechercher()`.
    assert lue["lectures_du_moteur_lexical"] == 0
    # Et sur un corps qui n'a que des sources serveur, ce sont bien elles qu'on lit.
    assert cas["ancien_contrat"]["sources"] == ["lux-guide:fbanque:1"]


# --- l'appariement citation ↔ segment -------------------------------------

def test_chaque_citation_est_placee_sous_le_segment_qui_la_cite(cas: dict[str, Any]) -> None:
    """Deux segments factuels, trois claims : l'énumération du serveur est refaite à l'identique."""
    par_segment = cas["citations_nominal"]
    assert [[c["block_id"] for c in seg] for seg in par_segment] == [
        ["lux-guide:farrivee:2", "lux-guide:farrivee:3"],
        ["lux-guide:farrivee:5"],
    ]
    assert [[c["claim_id"] for c in seg] for seg in par_segment] == [["c1", "c2"], ["c3"]]


def test_aucune_citation_napparait_deux_fois_sous_le_meme_segment(cas: dict[str, Any]) -> None:
    seg = cas["citations_sans_doublon"][0]
    rangs = [c["rang"] for c in seg]
    assert rangs == sorted(set(rangs)) == [0, 1]


@pytest.mark.parametrize("nom", ["citations_block_id_decale", "citations_source_en_trop",
                                 "citations_source_manquante", "citations_claim_absente"])
def test_un_appariement_qui_ne_concorde_pas_est_abandonne(cas: dict[str, Any], nom: str) -> None:
    """Dégradation visible (la liste plate sous la réponse), jamais une citation sous la mauvaise
    phrase. Le test serveur `…sources_enumere_les_claims_puis_leurs_quotes…` rend la casse bruyante."""
    assert cas[nom] is None


def test_un_refus_naapparie_rien_et_ne_montre_aucune_source(cas: dict[str, Any]) -> None:
    assert cas["citations_refus"] == [[]]
    assert cas["refus"]["sources"] == 0


# --- AD-4 : statuts, preuve d'absence, états, coût ------------------------

def test_le_statut_dit_ledition_avec_sa_reserve(cas: dict[str, Any]) -> None:
    """AD-4 : « édition X — actualité non vérifiée », jamais comme statut vert."""
    textes = cas["statut_textes"]
    assert textes["complet"] == "retrouvée · pertinente · édition git:a8e8593 — actualité non vérifiée"
    assert textes["edition_pdf"].endswith("édition juin 2017 — actualité non vérifiée")
    assert "applicable" not in textes["edition_pdf"]  # dérivé pour le sinistre, jamais affiché en guide
    assert textes["sans_statut"] == ""


def test_le_refus_affiche_la_phrase_du_serveur_et_la_preuve_chiffree(cas: dict[str, Any]) -> None:
    """AD-3 : la seule phrase d'absence lue est celle de `restituer.PHRASES_DE_REFUS`."""
    from server.app.steps.restituer import PHRASES_DE_REFUS

    refus = cas["refus"]
    assert refus["texte"] == PHRASES_DE_REFUS["hors_perimetre"]
    assert refus["segments_kind"] == ["limite"]
    assert refus["preuve"] == "Termes cherchés : météo — 4 variantes essayées, 312 passages parcourus"
    assert refus["etat"] == {"cle": "inconnu", "texte": "inconnu"}


def test_la_preuve_dabsence_saccorde_en_nombre(cas: dict[str, Any]) -> None:
    assert cas["preuve_singuliers"] == "Termes cherchés : bail — 1 variante essayée, 1 passage parcouru"


def test_une_clarification_na_rien_cherche_donc_rien_a_prouver(cas: dict[str, Any]) -> None:
    """AD-4 : sur `clarification_requise`, `terms_searched` est vide et `blocks_scanned` nul."""
    assert cas["preuve_clarification"] == ""
    assert cas["preuve_absente"] == ""
    clar = cas["clarification"]
    assert clar["clarification"].startswith("Parlez-vous")
    assert clar["preuve"] == ""


def test_une_reponse_amputee_est_dite_partielle(cas: dict[str, Any]) -> None:
    assert cas["partielle"]["unknown"] == ["montant exact"]
    assert cas["partielle"]["etat"] == {"cle": "partiel", "texte": "partiel"}


def test_les_trois_etats_viennent_des_deux_booleens(cas: dict[str, Any]) -> None:
    etats = cas["etats"]
    assert etats["sur"]["texte"] == "sûr"
    assert etats["partiel"]["texte"] == "partiel"
    assert etats["inconnu"]["texte"] == "inconnu"
    assert etats["absent"]["texte"] == "inconnu"  # pas d'`answer` ⇒ rien n'est affirmé


def test_le_cout_vient_de_la_trace(cas: dict[str, Any]) -> None:
    """NFR4 : le coût affiché est celui que l'usage rendu par l'API a produit, pas une estimation."""
    couts = cas["cout_textes"]
    assert couts["nominal"] == "cette réponse a coûté 0,0278 €"
    assert cas["reponse_lue"]["cout"] == "cette réponse a coûté 0,0278 €"
    assert couts["absent"] == "" and couts["sans_champ"] == ""


def test_le_comparateur_du_serveur_est_relaye(cas: dict[str, Any]) -> None:
    """AD-11 conserve `comparateur` « pour l'UI existante » : la puce du site doit réapparaître."""
    assert cas["comparateur"] is True


# --- FR11 / AD-16 : aucun repli silencieux --------------------------------

def test_un_503_ne_calcule_aucun_resultat_local(cas: dict[str, Any]) -> None:
    e = cas["erreur_503"]
    assert e["a_repondu"] is False
    assert (e["nom"], e["kind"], e["code"], e["statut"]) == ("ErreurChat", "indisponible",
                                                             "llm_unavailable", 503)
    assert e["request_id"] == "req-503"
    assert e["message"] == "L'assistant est indisponible pour le moment."
    assert e["lectures_du_moteur_lexical_avant_clic"] == 0, "le moteur lexical a tourné sans clic"


def test_le_clic_seul_produit_une_reponse_locale(cas: dict[str, Any]) -> None:
    locale = cas["recherche_simple"]
    assert locale["via"] == "local"
    assert locale["texte_non_vide"] is True
    assert locale["fiches"]
    assert locale["lectures_du_moteur_lexical_apres_clic"] > 0


def test_une_panne_reseau_ouvre_la_meme_porte_et_pas_une_autre(cas: dict[str, Any]) -> None:
    e = cas["erreur_reseau"]
    assert (e["kind"], e["code"], e["statut"]) == ("indisponible", "reseau", 0)
    assert e["message"] == "L'assistant est injoignable : la page n'a pas pu joindre le serveur."
    assert e["lectures_du_moteur_lexical"] == 0


def test_une_sonde_en_echec_ne_sert_pas_une_reponse_locale_doffice(cas: dict[str, Any]) -> None:
    """Design note : la conséquence assumée est un clic de plus, jamais une recherche de mots-clés
    servie comme si elle avait été vérifiée."""
    assert cas["sonde_en_echec"] is False
    apres = cas["apres_sonde_en_echec"]
    assert apres["a_repondu_en_local"] is False
    assert apres["kind"] == "indisponible"
    assert apres["lectures_du_moteur_lexical"] == 0


@pytest.mark.parametrize("nom", ["erreur_400", "erreur_413", "erreur_429", "erreur_500",
                                 "erreur_code_inconnu", "corps_illisible"])
def test_une_requete_refusee_naouvre_jamais_le_mode_local(cas: dict[str, Any], nom: str) -> None:
    """AD-16 : « repli local sur une erreur 4xx » est nommément ce que la décision empêche."""
    e = cas[nom]
    assert e["kind"] == "requete", "un 4xx/5xx non-503 ne doit pas proposer la recherche simple"
    assert e["message"]
    assert e.get("lectures_du_moteur_lexical", 0) == 0


def test_le_429_dit_le_delai_en_francais(cas: dict[str, Any]) -> None:
    e = cas["erreur_429"]
    assert e["retry_after"] == 60
    assert e["message"] == "Trop de questions en peu de temps : réessayez dans 60 secondes."
    # Sans en-tête, le message reste lisible plutôt que de mentir sur un délai.
    assert cas["erreur_429_sans_entete"]["message"].endswith("réessayez dans un instant.")


def test_le_message_brut_du_serveur_nest_jamais_affiche(cas: dict[str, Any]) -> None:
    """`gestionnaire_validation` compose un message avec le chemin pydantic, en anglais : utile à un
    développeur, illisible pour un arrivant. Le front compose le sien à partir du `code`."""
    for nom in ("erreur_400", "erreur_413", "erreur_429", "erreur_500", "erreur_code_inconnu"):
        assert "body.historique" not in cas[nom]["message"]
        assert "List should have" not in cas[nom]["message"]


def test_un_code_inconnu_ne_produit_pas_une_page_muette(cas: dict[str, Any]) -> None:
    e = cas["erreur_code_inconnu"]
    assert e["code"] == "theiere"
    assert e["message"] == "Le serveur n'a pas pu répondre à cette question. Réessayez plus tard."


@pytest.mark.parametrize("code", ["invalid_request", "input_too_long", "rate_limited",
                                  "llm_unavailable", "internal"])
def test_les_codes_traduits_sont_ceux_de_lenum_dad16(code: str) -> None:
    """Le front traduit des `code` : si l'`Enum` du serveur en perd un, ce test le dit."""
    from server.app.domain.errors import ErrorCode

    assert code in {c.value for c in ErrorCode}


# --- AD-15 : ce que le navigateur garde, et ce qu'il ne garde plus --------

def test_la_persistance_des_conversations_est_retiree() -> None:
    """UX-DR5 : `localStorage` porte le profil et les préférences, jamais un texte de conversation."""
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    assert "historique: historique.slice" not in ui
    assert "chargerConversation" not in ui and "sauverConversation" not in ui
    assert "Conversation reprise" not in ui
    assert "function sauverProfil" in ui and "function chargerProfil" in ui


def test_aucun_innerhtml_ne_sert_a_poser_du_texte_venu_du_serveur() -> None:
    """AD-15 : tout texte reçu du serveur est rendu par `textContent` (c'est ce que fait `el()`)."""
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    for i, ligne in enumerate(ui.splitlines(), start=1):
        if "innerHTML" not in ligne or ligne.strip().startswith("//"):
            continue
        # Les seuls `innerHTML` tolérés vident un conteneur : ils n'insèrent aucun texte.
        assert 'innerHTML = ""' in ligne, f"ui.js:{i} insère du HTML : {ligne.strip()}"


def test_la_mention_de_confidentialite_est_pres_des_deux_saisies() -> None:
    """AD-15 : la mention vit sous la saisie — onglet **et** widget flottant."""
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    assert html.count('class="hint confid"') == 2
    assert html.count("Anthropic") == 2
    # Les deux mentions disent la même chose : où va le contenu, et ce qui n'est pas conservé.
    assert "n'enregistre aucune conversation" in html          # onglet Assistant
    assert "Aucune conversation n'est\n      enregistrée" in html  # panneau flottant


def test_les_deux_journaux_sont_annonces_aux_lecteurs_decran() -> None:
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    assert html.count('aria-live="polite"') == 2


# --- UX-DR10 : ni frappe simulée, ni délai artificiel ---------------------

def test_le_delai_artificiel_et_la_frappe_mot_a_mot_ont_disparu() -> None:
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    assert "function taper(" not in ui
    assert "500 + Math.random() * 600" not in ui
    assert "function bulleAttente" in ui and "verrouillerSaisie" in ui


# --- le cache du navigateur ----------------------------------------------

def test_toutes_les_balises_du_site_passent_a_la_version_18() -> None:
    """Un navigateur qui a visité le guide avant le raccord servirait un `chat.js` de la 1.6 contre
    un serveur de la 1.7 : profil en chaîne, donc 400."""
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    assert "?v=17" not in html
    assert html.count("?v=18") >= 10


def test_communaute_js_nest_charge_quune_fois() -> None:
    """FR1b : la seconde balise, sans version, faisait poser les gestionnaires d'astuces en double."""
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    assert html.count("app/communaute.js") == 1


# --- ce que la story n'a pas fait ----------------------------------------

def test_aucun_fichier_du_site_nest_renomme_ni_supprime() -> None:
    for chemin in ("index.html", "app/chat.js", "app/ui.js", "app/kb.js", "app/styles.css",
                   "app/communaute.js", "app/simulateur.js", "app/bareme.js"):
        assert (REPO_ROOT / "web" / chemin).exists(), chemin


def test_le_contexte_rag_et_ses_vingt_kilo_octets_ont_disparu() -> None:
    """`request_max_bytes` = 65 536 ; `contexteRag()` recopiait le corps de quatre fiches pour un
    champ que le serveur, `extra="ignore"`, ne lit jamais."""
    chat = (REPO_ROOT / "web" / "app" / "chat.js").read_text("utf-8")
    assert "contexteRag" not in chat
    assert "contexte:" not in chat


def test_le_repli_silencieux_a_disparu_du_point_dentree() -> None:
    chat = (REPO_ROOT / "web" / "app" / "chat.js").read_text("utf-8")
    assert "local (api indisponible)" not in chat
    # `reponseLocale` reste, mais une seule porte y mène, et elle demande un clic.
    assert chat.count("reponseLocale(") == 2  # sa définition, et l'appel de `rechercheSimple`


def test_toutes_les_fonctions_pures_de_la_story_sont_exportees(cas: dict[str, Any]) -> None:
    attendues = {"historiquePourApi", "citationsParSegment", "statutTexte", "preuveAbsence",
                 "coutTexte", "etatReponse", "messageErreur", "rechercheSimple"}
    assert attendues <= set(cas["exporte"])


def test_le_harnais_vit_dans_le_depot_et_ne_depend_de_rien() -> None:
    """Aucune dépendance ajoutée : le harnais n'utilise que `node:fs`, `node:path`, `node:vm`."""
    source = Path(HARNAIS).read_text("utf-8")
    imports = {ligne.split('"')[1] for ligne in source.splitlines()
               if ligne.startswith("import ") and '"' in ligne}
    assert imports <= {"node:fs", "node:path", "node:url", "node:vm"}, imports
