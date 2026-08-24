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
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from server.app.config import REPO_ROOT, Settings
from server.app.domain.question import Turn

HARNAIS = REPO_ROOT / "tests" / "js" / "chat_cases.mjs"
HARNAIS_UI = REPO_ROOT / "tests" / "js" / "ui_cases.mjs"


# Sans `node`, ces cas passent en `skip` — et un `skip` est indiscernable d'un succès dans un
# `pytest -q`. La CI (story 1.11) pose `FRONT_TESTS_REQUIS=1` : le skip devient alors un échec, et
# une image sans `node` ne peut plus faire passer la suite au vert en taisant la moitié du front.
REQUIS = os.environ.get("FRONT_TESTS_REQUIS", "") not in ("", "0")


def _lancer(harnais: Path) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        motif = ("node absent de la machine : les cas du front ne peuvent pas tourner "
                 "(l'image de CI de la story 1.11 en a un)")
        if REQUIS:
            pytest.fail("FRONT_TESTS_REQUIS=1 mais " + motif)
        pytest.skip(motif)
    fini = subprocess.run([node, str(harnais)], capture_output=True, text=True, timeout=120,
                          cwd=str(REPO_ROOT))
    # Le JSON sort sur stdout, les diagnostics sur stderr : un `console.log` oublié dans `chat.js`
    # ne peut plus corrompre le relevé, et le code de sortie tranche.
    assert fini.returncode == 0, f"le harnais a échoué ({fini.returncode}) :\n{fini.stderr}"
    charge = json.loads(fini.stdout) if fini.stdout.strip() else {"ok": False, "erreur": fini.stderr}
    assert charge.get("ok") is True, charge.get("erreur")
    return charge["cas"]


@pytest.fixture(scope="module")
def cas() -> dict[str, Any]:
    """Le harnais est exécuté **une fois** pour tout le module ; chaque test lit un relevé."""
    return _lancer(HARNAIS)


@pytest.fixture(scope="module")
def dom() -> dict[str, Any]:
    """Le matérialiseur de `ui.js`, exécuté contre le DOM minimal de `tests/js/dom_minimal.mjs`."""
    return _lancer(HARNAIS_UI)


def _actions(vue: dict[str, Any], nom: str) -> list[dict[str, Any]]:
    return [a for a in vue["actions"] if a["nom"] == nom]


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


def test_la_borne_de_tours_est_celle_que_le_serveur_publie(cas: dict[str, Any]) -> None:
    """Les seuils vivent dans `config.py` et sont servis par `/sante` : les recopier dans le front
    les ferait diverger en silence. La sonde annonce 3 dans ce cas, et le front s'y tient."""
    assert cas["bornes_avant_sonde"]["seuils_du_serveur"] == {}
    assert cas["bornes_avant_sonde"]["historique_max_tours"] == 6  # repli, avant toute sonde
    assert cas["bornes_apres_sonde"]["historique_max_tours"] == 3
    assert cas["historique_borne_par_le_serveur"] == 3
    assert len(cas["historique_max_explicite"]) == 2  # la borne peut aussi être passée en argument
    # `historique_max_turns` est bien publié par le serveur : sinon le front lirait toujours son repli.
    assert "historique_max_turns" in Settings(_env_file=None, anthropic_api_key="").thresholds()


def test_le_repli_du_front_vaut_ce_que_la_configuration_dit(cas: dict[str, Any]) -> None:
    """Un repli qui diverge du défaut du serveur serait un piège au premier chargement."""
    settings = Settings(_env_file=None, anthropic_api_key="")
    assert cas["bornes_avant_sonde"]["historique_max_tours"] == settings.historique_max_turns


def test_la_premiere_requete_part_deja_sur_les_seuils_du_serveur(cas: dict[str, Any]) -> None:
    """Convention du projet : les seuils vivent dans `config.py`. Ce que le front en garde n'est
    qu'un repli — encore faut-il qu'il s'en serve le moins possible. Sans attente de la sonde, la
    **première** question utilisait les replis écrits dans `chat.js` et ignorait une configuration
    différente : un serveur réglé à 3 tours recevait les 6 du repli, donc un 400."""
    vu = cas["premiere_requete"]
    assert vu["urls"] == ["/sante", "/chat"], "la sonde d'abord, la question ensuite"
    assert vu["bornes"]["historique_max_tours"] == 2
    assert vu["historique_envoye"] == 2, "l'historique est borné par la valeur du serveur"


def test_la_marge_dabandon_du_client_vient_de_config_py(cas: dict[str, Any]) -> None:
    """La marge au-dessus de `deadline_s` était un nombre écrit dans `chat.js` — un seuil numérique
    n'a qu'un domicile. Elle est dans `config.py`, publiée par `/sante`, et le front la lit."""
    settings = Settings(_env_file=None, anthropic_api_key="")
    assert "client_abort_margin_s" in settings.thresholds()
    # Le repli du front vaut le défaut du serveur : sinon, un piège au premier chargement.
    repli = cas["bornes_avant_sonde"]["delai_abandon_ms"]
    assert repli == round((settings.deadline_s + settings.client_abort_margin_s) * 1000)
    # Et une configuration servie (deadline 20 s, marge 4 s) est celle qui s'applique.
    assert cas["marge_du_serveur"] == 24_000


def test_aucun_seuil_du_serveur_nest_recopie_en_dur_dans_le_front() -> None:
    """Les trois seuils que le front connaît sont des **replis** nommés comme tels, et chacun cite la
    clé de `config.py` dont il vient. Un quatrième nombre magique introduit ici ne serait rattaché à
    rien : le test le rend bruyant."""
    chat = (REPO_ROOT / "web" / "app" / "chat.js").read_text("utf-8")
    replis = re.findall(r"var ([A-Z_]+_REPLI) = ([\d.]+);\s*// (config\.\w+)", chat)
    assert {nom for nom, _, _ in replis} == {
        "HISTORIQUE_MAX_TOURS_REPLI", "DEADLINE_SERVEUR_REPLI", "MARGE_ABANDON_S_REPLI"}, replis
    settings = Settings(_env_file=None, anthropic_api_key="")
    for _, valeur, cle in replis:
        attendu = getattr(settings, cle.split(".", 1)[1])
        assert float(valeur) == float(attendu), f"{cle} = {attendu}, le repli dit {valeur}"


def test_la_borne_dun_tour_est_amarree_au_contrat_du_serveur(cas: dict[str, Any]) -> None:
    """`Turn.texte` n'est pas un seuil de `config.py` (donc absent de `thresholds()`) : c'est une
    contrainte de schéma. Le front la porte, ce test l'amarre à sa source."""
    from annotated_types import MaxLen

    maxlen = next(m.max_length for m in Turn.model_fields["texte"].metadata
                  if isinstance(m, MaxLen))
    assert cas["bornes_avant_sonde"]["tour_max_caracteres"] == maxlen
    assert "texte_max_chars" not in Settings(_env_file=None, anthropic_api_key="").thresholds()


def test_un_tour_surdimensionne_est_ecarte_jamais_coupe(cas: dict[str, Any]) -> None:
    """`Turn.texte ≤ 2 000` : couper changerait ce qui a été dit, écarter ne fabrique rien."""
    envoye = cas["historique_tour_surdimensionne"]
    textes = [t["texte"] for t in envoye]
    assert "x" * 2400 not in textes
    assert not any(len(t) > 2000 for t in textes)
    assert "y" * 2000 in textes  # 2 000 pile est admis par le contrat
    for tour in envoye:
        Turn(**tour)


def test_un_tour_qui_ne_part_pas_coupe_lhistorique_au_lieu_de_le_trouer(cas: dict[str, Any]) -> None:
    """Un trou au milieu enverrait une question de suivi sans la réponse qu'elle référence, et
    ferait se suivre deux tours du même rôle. On garde la queue contiguë."""
    assert [t["texte"] for t in cas["historique_queue_contigue"]] == ["tour 5", "tour 6"]
    # Deux trous : seule la queue qui suit le **dernier** part.
    assert [t["texte"] for t in cas["historique_deux_trous"]] == ["tour 5"]
    # Le tour surdimensionné du cas nominal coupe aussi ce qui le précède.
    assert [t["texte"] for t in cas["historique_tour_surdimensionne"]][0] == "petit 2"


def test_la_reponse_de_la_recherche_simple_ne_repart_pas_au_serveur(cas: dict[str, Any]) -> None:
    """Toute la story consiste à ne pas faire passer une comparaison de mots-clés pour une parole
    vérifiée. L'envoyer comme un tour de l'assistant la ferait traiter par *comprendre* comme la
    sienne, et résoudre les anaphores de la question suivante contre elle."""
    envoye = cas["historique_tour_local"]
    assert "Réponse lexicale du guide, non vérifiée." not in [t["texte"] for t in envoye]
    # Et ce qui la précédait ne part pas non plus : la chaîne est cassée là.
    assert [t["texte"] for t in envoye] == ["Et pour l'école ?", "Réponse vérifiée du serveur."]


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
    """`j.reponse` était le champ du relais d'origine ; le contrat d'AD-11 rend `texte`. Un corps qui
    porte les deux se lit sur `texte`, et `reponse` ne ressort nulle part."""
    ancien = cas["ancien_contrat"]
    assert ancien["a_champ_reponse"] is False
    assert ancien["texte"] == "Texte du contrat servi, celui d'après la 1.7."
    # Et un corps qui ne porte **que** l'ancien champ n'est pas une réponse (cf. lecture stricte).
    assert cas["contrat_incomplet"]["ancien_contrat"]["code"] == "reponse_illisible"


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


# --- AD-16 : un 200 incomplet n'est pas une réponse ------------------------

@pytest.mark.parametrize(
    ("nom", "champ"),
    [("vide", "texte"), ("sans_answer", "answer"), ("sans_trace", "trace"),
     ("sans_texte", "texte"), ("answer_nul", "answer"),
     ("answer_sans_found", "answer.found"), ("answer_sans_complete", "answer.complete"),
     ("sources_non_liste", "sources"), ("ancien_contrat", "texte")],
)
def test_un_200_qui_ne_tient_pas_le_contrat_nest_pas_peint(cas: dict[str, Any], nom: str,
                                                           champ: str) -> None:
    """AD-16 prévient « réponse vide présentée comme réponse ». Une valeur par défaut à la place d'un
    champ obligatoire peignait un corps `{}` en réponse « inconnu », l'ajoutait à l'historique et le
    faisait repartir au serveur au tour suivant. Un 200 incomplet est un serveur cassé, pas une
    réponse dégradée : `reponse_illisible`, comme un corps non-JSON."""
    vu = cas["contrat_incomplet"][nom]
    assert vu["a_repondu"] is False, f"{nom} a été peint comme une réponse"
    assert (vu["kind"], vu["code"]) == ("requete", "reponse_illisible")
    assert vu["champ"] == champ
    # « requete » et non « indisponible » : aucun bouton de repli (AD-11/AD-16).
    assert vu["lectures_du_moteur_lexical"] == 0


def test_le_champ_fautif_ne_va_pas_a_lecran(cas: dict[str, Any]) -> None:
    """Le nom du champ manquant sert au développeur ; l'utilisateur lit la phrase du `code`."""
    for vu in cas["contrat_incomplet"].values():
        assert vu["champ"] not in vu["message"]
        assert vu["message"] == cas["corps_illisible"]["message"]


def test_les_champs_a_valeur_par_defaut_restent_facultatifs(cas: dict[str, Any]) -> None:
    """La lecture stricte porte sur les champs **obligatoires** de `ChatResponse` (ceux sans défaut).
    Refuser un corps parce que `sources` est absent inventerait une exigence que le contrat n'a pas."""
    from server.app.api.schemas import ChatResponse

    obligatoires = {n for n, f in ChatResponse.model_fields.items() if f.is_required()}
    assert obligatoires == {"texte", "answer", "trace"}, obligatoires
    minimal = cas["contrat_minimal"]
    assert minimal["texte"] == "Vous avez huit jours."
    assert minimal["sources"] == [] and minimal["segments"] == []
    assert minimal["via"] == "api/v1" and minimal["comparateur"] is False


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


def test_un_claim_id_herite_du_prototype_ne_trompe_pas_lappariement(cas: dict[str, Any]) -> None:
    """Le dictionnaire par `claim_id` est indexé par des chaînes qui viennent du modèle. Sur un objet
    nu, `parClaim["toString"]` trouve un héritage : l'abandon ne se déclenchait pas, et le rendu
    levait au lieu de dégrader."""
    assert cas["citations_claim_id_herite"] is None, cas["citations_claim_id_herite"]
    # Et un `claim_id` réellement nommé « constructor » reste apparié normalement.
    apparie = cas["citations_claim_id_prototype"]
    assert isinstance(apparie, list), apparie
    assert [c["claim_id"] for c in apparie[0]] == ["constructor", "c2"]


def test_la_bulle_est_assemblee_hors_du_dom(cas: dict[str, Any]) -> None:
    """Une région `aria-live` annoncerait sinon un nœud vide, puis chaque mutation, une par une."""
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    corps = _corps(ui, "function peindre(vue) {")
    assert corps.index("materialiser(vue, grp)") < corps.index("appendChild"), corps
    # `materialiser()` n'attache jamais rien au document : il ne connaît que les nœuds qu'il crée.
    fabrique = _corps(ui, "function materialiser(vue, grp) {")
    assert "document.createElement" in fabrique
    assert "log" not in fabrique and "document.body" not in fabrique


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


def test_les_compteurs_a_zero_sont_affiches_car_ils_prouvent(cas: dict[str, Any]) -> None:
    """L'AC demande « N variantes essayées, M passages parcourus ». Zéro **répond** à cette question :
    un `hors_perimetre` court-circuite avant tout retrieval (AD-5) et ses deux compteurs sont nuls —
    c'est précisément ce qui le distingue d'un `zero_hit` qui a lu 506 passages sans rien trouver.
    Les taire rendait les deux refus indiscernables, et le refus météo relevé en live n'affichait
    aucun chiffre du tout."""
    zeros = cas["preuve_zeros"]
    assert zeros["hors_perimetre"] == (
        "Termes cherchés : météo — 0 variante essayée, 0 passage parcouru")
    assert zeros["zero_hit"] == "Termes cherchés : bail — 0 variante essayée, 506 passages parcourus"
    assert zeros["claims_rejetes"] == (
        "Termes cherchés : bail, préavis — 0 variante essayée, 506 passages parcourus")
    # Aucun terme retenu se dit aussi : c'est l'explication des deux zéros qui suivent.
    assert zeros["sans_terme"] == (
        "Aucun terme du guide n'a été retenu — 0 variante essayée, 0 passage parcouru")


def test_une_clarification_na_rien_cherche_donc_rien_a_prouver(cas: dict[str, Any]) -> None:
    """AD-4 : sur `clarification_requise`, `terms_searched` est vide et `blocks_scanned` nul."""
    assert cas["preuve_clarification"] == ""
    assert cas["preuve_zeros"]["clarification"] == ""
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


# --- Les vues : ce que l'AC promet d'afficher ----------------------------
#
# `chat.js` compose un arbre de nœuds ; `ui.js` ne fait que le matérialiser. Ces tests portent donc
# sur ce que l'utilisateur verra, sans navigateur ni faux DOM. C'est ici que vivent les phrases de
# l'AC : « chaque segment factuel suivi de ses citations », « bandeau + bouton », « aucun bouton sur
# un 4xx », « le coût en pied », « la clarification comme une question ».

def test_chaque_segment_factuel_est_suivi_de_ses_citations(cas: dict[str, Any]) -> None:
    vue = cas["vue_nominale"]
    assert vue["ordre_des_blocs"] == ["seg seg-factuel", "seg seg-factuel", "pied", "chips"]
    segments = vue["segments"]
    assert [s["texte"] for s in segments] == [
        "Vous avez huit jours pour déclarer votre arrivée, au Biergercenter de votre commune.",
        "Cette déclaration produit le certificat de résidence."]
    assert [[c["quote"] for c in s["citations"]] for s in segments] == [
        ["« huit jours pour déclarer votre arrivée »",
         "« au bureau de la population, souvent appelé Biergercenter »"],
        ["« le certificat de résidence »"]]
    # Chaque citation porte sa fiche, son lien officiel et son statut avec la réserve d'AD-4.
    for segment in segments:
        for citation in segment["citations"]:
            assert citation["fiche"] == "Les huit premiers jours"
            assert citation["lien"] == "https://guichet.public.lu/arrivee"
            assert citation["statut"].endswith("édition git:a8e8593 — actualité non vérifiée")
    assert vue["citations_plates"] == [] and vue["degrade"] is None


def test_le_pied_porte_letat_et_le_cout(cas: dict[str, Any]) -> None:
    vue = cas["vue_nominale"]
    assert vue["etat"] == "etat etat-sur" and vue["etat_texte"] == "sûr"
    assert vue["cout"] == "cette réponse a coûté 0,0278 €"
    assert vue["ordre_des_blocs"][-2] == "pied"  # en pied de réponse, avant les puces


def test_une_reponse_incomplete_montre_ce_quelle_ne_sait_pas(cas: dict[str, Any]) -> None:
    vue = cas["vue_partielle"]
    assert vue["inconnus"] == ["montant exact", "délai de recours"]
    assert vue["etat_texte"] == "partiel"
    assert vue["ordre_des_blocs"].index("inconnu") < vue["ordre_des_blocs"].index("pied")


def test_un_refus_montre_la_phrase_du_serveur_puis_la_preuve(cas: dict[str, Any]) -> None:
    from server.app.steps.restituer import PHRASES_DE_REFUS

    vue = cas["vue_refus"]
    assert vue["segments"][0]["texte"] == PHRASES_DE_REFUS["hors_perimetre"]
    assert vue["segments"][0]["citations"] == []
    assert vue["preuve"] == "Termes cherchés : météo — 4 variantes essayées, 312 passages parcourus"
    # La preuve **suit** la phrase, elle ne la remplace pas.
    assert vue["ordre_des_blocs"].index("preuve") > vue["ordre_des_blocs"].index("seg")
    assert vue["etat_texte"] == "inconnu"
    assert vue["actions"] == []  # un refus n'est pas une panne : aucun repli proposé


def test_une_clarification_est_posee_comme_une_question_avant_le_refus(cas: dict[str, Any]) -> None:
    vue = cas["vue_clarification"]
    assert vue["clarification"].endswith("?")
    assert vue["ordre_des_blocs"][0] == "clarif"
    assert vue["ordre_des_blocs"].index("clarif") < vue["ordre_des_blocs"].index("seg")
    assert vue["preuve"] is None  # rien n'a été cherché : il n'y a rien à prouver


def test_un_appariement_abandonne_degrade_visiblement_sans_rien_retirer(cas: dict[str, Any]) -> None:
    """C'est le mode où l'on en dit le moins : ce serait le pire endroit où taire la réserve."""
    vue = cas["vue_degradee"]
    assert vue["segments"] == []                     # plus de rattachement phrase par phrase
    assert vue["degrade"] and "je n'ai pas pu rattacher" in vue["degrade"]
    plates = vue["citations_plates"]
    assert len(plates) == 3                          # aucune citation perdue
    for citation in plates:
        assert citation["statut"].endswith("actualité non vérifiée")


def test_les_citations_ne_disparaissent_pas_quand_answer_segments_est_vide(cas: dict[str, Any]) -> None:
    """`segments[]` de premier niveau est la copie du contrat : si `answer.segments` est vide et pas
    lui, l'appariement doit porter sur ceux qu'on peint — sinon les citations tombent des deux côtés."""
    vue = cas["vue_segments_premier_niveau"]
    assert len(vue["segments"]) == 2
    assert sum(len(s["citations"]) for s in vue["segments"]) == 3
    assert vue["citations_plates"] == []


def test_une_fiche_que_le_site_ne_connait_pas_nest_pas_cliquable(cas: dict[str, Any]) -> None:
    """`fiche_id` vient du corpus servi, qui peut diverger de `kb.js` ; un bouton ouvrirait alors la
    liste complète des fiches, sans explication."""
    vue = cas["vue_fiche_inconnue"]
    citations = [c for s in vue["segments"] for c in s["citations"]]
    assert citations and all(c["fiche"] is None for c in citations)
    assert all(c["fiche_texte"] == "Les huit premiers jours" for c in citations)
    assert _actions(vue, "ouvrir_fiche") == []


def test_une_edition_vide_garde_la_reserve_dactualite(cas: dict[str, Any]) -> None:
    """AD-4 : « jamais comme statut vert ». `Document.edition` n'a pas de `min_length`."""
    citations = [c for s in cas["vue_sans_edition"]["segments"] for c in s["citations"]]
    assert citations
    for citation in citations:
        assert citation["statut"] == ("retrouvée · pertinente · "
                                      "édition non précisée — actualité non vérifiée")


def test_la_puce_du_comparateur_reapparait_avec_la_question(cas: dict[str, Any]) -> None:
    actions = _actions(cas["vue_comparateur"], "comparateur")
    assert len(actions) == 1
    assert actions[0]["question"] == "Quel délai ai-je pour déclarer mon arrivée à la commune ?"
    assert _actions(cas["vue_nominale"], "comparateur") == []


def test_letat_de_chargement_est_un_texte_pas_seulement_une_animation(cas: dict[str, Any]) -> None:
    vue = cas["vue_attente"]
    assert vue["ordre_des_blocs"] == ["attente-txt", "points"]
    assert vue["attente"].startswith("Je cherche dans le guide")
    assert vue["actions"] == []


# --- FR11 / AD-16 : le repli est porté par la vue, et par elle seule ------

@pytest.mark.parametrize("nom", ["vue_erreur_503", "vue_erreur_reseau"])
def test_une_indisponibilite_porte_exactement_une_action_de_repli(cas: dict[str, Any],
                                                                  nom: str) -> None:
    """C'est la mutation que la revue a exhibée : offrir le bouton sur autre chose qu'un 503 ou une
    panne réseau est le repli sur 4xx qu'AD-16 interdit, et rien ne le voyait."""
    vue = cas[nom]
    assert vue["cls_racine"] == "msg bot indispo"
    actions = _actions(vue, "recherche_simple")
    assert len(actions) == 1, "une porte, une seule"
    assert actions[0]["question"] == "Quel délai ai-je pour déclarer mon arrivée à la commune ?"
    assert len(vue["actions"]) == 1, "et rien d'autre à cliquer dans le bandeau"


@pytest.mark.parametrize("nom", ["vue_erreur_400", "vue_erreur_429", "vue_erreur_500"])
def test_une_requete_refusee_ne_porte_aucune_action(cas: dict[str, Any], nom: str) -> None:
    vue = cas[nom]
    assert vue["cls_racine"] == "msg bot err"
    assert vue["actions"] == [], "un 4xx/5xx non-503 n'ouvre pas le mode local"


def test_le_badge_ne_contredit_pas_le_bandeau(cas: dict[str, Any]) -> None:
    """Deux indicateurs qui se contredisent dans la même vue : « mode api » pendant que le bandeau
    annonce l'indisponibilité. Sur un 4xx, en revanche, le serveur a bien répondu."""
    assert cas["mode_apres_erreur"]["indisponible"] == "indisponible"
    assert cas["mode_apres_erreur"]["requete"] is None
    assert cas["mode_apres_erreur"]["aucune"] is None


def test_la_reponse_locale_ne_prend_pas_la_forme_dune_reponse_verifiee(cas: dict[str, Any]) -> None:
    vue = cas["vue_locale"]
    assert vue["cls_racine"] == "msg bot locale"
    assert vue["etat_texte"] == "recherche simple"
    assert vue["cout"] is None  # elle n'a rien coûté, et n'emprunte pas le pied d'une réponse servie
    assert vue["sans_verification"].startswith("aucune vérification")


def test_aucune_vue_ne_pose_de_html(cas: dict[str, Any]) -> None:
    """Le contrat de la description : un nœud porte du **texte**, jamais du balisage. `ui.js` le pose
    par `textContent` ; c'est ici que l'on vérifie qu'il n'y a rien d'autre à poser."""
    for nom, vue in cas.items():
        if not nom.startswith("vue_"):
            continue
        assert "<" not in json.dumps(vue["segments"], ensure_ascii=False)
        assert set(vue["tags_porteurs_de_texte"]) <= {"a", "blockquote", "button", "div", "li",
                                                      "p", "span", "strong", "ul"}


# --- une requête qui pend, une page hors ligne ---------------------------

def test_une_requete_qui_pend_est_abandonnee_et_cest_une_indisponibilite(cas: dict[str, Any]) -> None:
    """Sans borne, l'attente et le verrou de saisie resteraient indéfiniment."""
    settings = Settings(_env_file=None, anthropic_api_key="")
    pose = cas["abandon_delai_pose_ms"] / 1000
    assert pose > settings.deadline_s, "couper sous la deadline du serveur perdrait une réponse due"
    assert pose <= settings.deadline_s + 30, "au-delà, l'utilisateur attend pour rien"
    assert cas["abandon"]["kind"] == "indisponible"
    assert cas["abandon"]["code"] == "timeout_client"
    assert cas["abandon"]["message"]


def test_une_page_ouverte_en_fichier_local_ne_sonde_rien(cas: dict[str, Any]) -> None:
    """En `file://`, `window.location.origin` vaut la chaîne « null » : sonder `null/sante` ne
    produirait qu'une erreur console incompréhensible."""
    assert cas["hors_ligne_origine"] == "null"
    assert cas["hors_ligne_sonde"] is False
    assert cas["hors_ligne"]["appels_reseau"] == 0
    assert cas["hors_ligne"]["kind"] == "indisponible"
    assert "fichier local" in cas["hors_ligne"]["message"]


# --- les relevés collectés sont tous assertés ----------------------------

def test_la_reponse_nominale_est_un_200(cas: dict[str, Any]) -> None:
    assert cas["statut_http_nominal"] == 200
    assert cas["reponse_lue"]["etat"] == {"cle": "sur", "texte": "sûr"}


def test_lappariement_reussit_sur_lordre_du_serveur(cas: dict[str, Any]) -> None:
    assert cas["appariement_bon_ordre"] is True


def test_un_cout_nul_se_dit_plutot_que_de_sarrondir(cas: dict[str, Any]) -> None:
    """Un total nul veut dire qu'aucun appel n'a été facturé ; « 0,0000 € » ferait croire à un arrondi."""
    assert cas["cout_textes"]["zero"] == "cette réponse n'a rien coûté (aucun appel facturé)"


def test_le_questionnaire_du_site_est_couvert_par_le_filtre_du_serveur(cas: dict[str, Any]) -> None:
    """Un septième champ ajouté à `CHAMPS` sans toucher `PROFIL_KEYS` serait perdu exactement comme
    `horizon` l'était — et l'ancien test, qui relisait une liste écrite à la main, ne l'aurait pas vu."""
    from server.app.domain.profil import PROFIL_KEYS

    champs = cas["champs_du_questionnaire"]
    assert champs, "le questionnaire du site n'est pas vide"
    manquants = sorted(set(champs) - set(PROFIL_KEYS))
    assert not manquants, f"champs du site perdus par PROFIL_KEYS : {manquants}"


# --- le matérialiseur : ce que `ui.js` en fait vraiment --------------------
#
# Jusqu'ici `ui.js` était vérifié en lisant son source. C'est ainsi qu'un badge de mode posé dans la
# seule section « Assistant » — donc `hidden` dès qu'on regarde un autre onglet, c'est-à-dire chaque
# fois que le widget flottant sert — a traversé la revue interne : aucun test ne pouvait le voir.
# `tests/js/ui_cases.mjs` exécute donc `materialiser()`, `peindre()` et `badgeMode()` contre un DOM
# minimal. Ce qui reste hors de portée (les tailles, les contrastes, un vrai lecteur d'écran) est
# la reprise différée vers la story 1.11, qui porte le test navigateur de la CI.

def test_larbre_decrit_devient_du_dom_dans_les_deux_journaux(dom: dict[str, Any]) -> None:
    """Le même fil des deux côtés : une bulle par journal, avec le même texte."""
    r = dom["reponse"]
    assert r["bulles"] == 2 and r["dans_le_document"] == 2
    textes = {j["texte"] for j in r["journaux"]}
    assert len(textes) == 1, "les deux journaux ne montrent pas la même chose"
    texte = textes.pop()
    for morceau in ("Vous avez huit jours.", "Passage cité", "Les huit premiers jours",
                    "retrouvée · pertinente · édition git:a8e8593 — actualité non vérifiée",
                    "sûr", "cette réponse a coûté 0,0278 €"):
        assert morceau in texte, morceau


def test_le_texte_du_serveur_arrive_litteralement(dom: dict[str, Any]) -> None:
    """AD-15 : `textContent`, jamais `innerHTML`. Le DOM minimal **lève** sur toute pose d'`innerHTML`
    non vide : arriver jusqu'ici avec une citation qui porte du balisage le démontre, au lieu de le
    déduire d'une lecture du source."""
    assert dom["reponse"]["citation"] == "« <script>alert(1)</script> huit jours »"


def test_le_lien_officiel_dune_citation_est_pose_sans_fuite(dom: dict[str, Any]) -> None:
    lien = dom["reponse"]["lien"]
    assert lien["href"] == "https://guichet.public.lu/arrivee"
    assert lien["target"] == "_blank" and "noopener" in lien["rel"]


def test_le_badge_de_mode_existe_dans_les_deux_surfaces(dom: dict[str, Any]) -> None:
    """FR11. Quand la réponse s'affiche dans le widget flottant depuis un autre onglet, le badge du
    panneau « Assistant » est dans une section `hidden` : il n'indique rien. Les deux le disent."""
    b = dom["badges"]
    for mode, attendu in (("api", ("mode api", "badge on")),
                          ("indisponible", ("mode indisponible", "badge off")),
                          ("local", ("mode local", "badge"))):
        for surface in ("onglet", "widget"):
            assert (b[mode][surface]["texte"], b[mode][surface]["cls"]) == attendu, (mode, surface)


def test_letat_initial_du_badge_nannonce_pas_un_mode_quon_na_pas_essaye(
        dom: dict[str, Any]) -> None:
    """La page servait « mode local » avant même la sonde — le seul mode qui ne se déclenche jamais
    tout seul (AD-11), annoncé avant d'avoir essayé le serveur."""
    assert dom["badges"]["avant_sonde"] == {"texte": "mode : vérification…", "cls": "badge"}
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    for badge in ("mode-badge", "widget-badge"):
        assert re.search(rf'id="{badge}"[^>]*>mode : vérification…<', html), badge
    assert ">mode local<" not in html


def test_une_indisponibilite_materialise_un_bouton_et_le_clic_seul_ouvre_le_local(
        dom: dict[str, Any]) -> None:
    """AD-11 / AD-16 : la **seule** porte vers le moteur lexical, et elle demande un clic. Le test
    clique pour de bon sur le bouton matérialisé, et vérifie ce qui arrive ensuite."""
    avant, apres = dom["indisponible"]["avant"], dom["indisponible"]["apres"]
    assert avant["boutons"] == 1 and avant["type"] == "button"
    assert avant["texte"] == "Consulter le guide en recherche simple"
    assert avant["ecouteurs"] == ["click"]
    assert avant["historique"] == 0, "rien n'a été dit tant que rien n'a été cliqué"
    # Le clic, et lui seul : la réponse locale apparaît des deux côtés, le badge le dit des deux
    # côtés, et le tour est marqué `local` pour ne pas repartir au serveur comme sa propre parole.
    assert apres["locales"] == 2
    assert apres["badges"]["onglet"]["texte"] == "mode local"
    assert apres["badges"]["widget"]["texte"] == "mode local"
    assert apres["historique"] == [{"role": "assistant", "local": True}]
    assert apres["boutons_de_repli_restants"] == 0, "la puce cliquée disparaît des deux journaux"


@pytest.mark.parametrize("nom", ["invalid_request", "input_too_long", "rate_limited",
                                 "reponse_illisible"])
def test_une_requete_refusee_ne_materialise_aucun_bouton(dom: dict[str, Any], nom: str) -> None:
    """AD-16 prévient « repli local sur une erreur 4xx ». La revue interne avait démontré que la
    règle vivait dans le DOM ; elle vit dans `chat.js`, et ce test la mesure **dans** le DOM."""
    vu = dom["refusees"][nom]
    assert vu["boutons"] == 0, vu["texte"]
    assert vu["texte"].startswith("Question non traitée")
    assert dom["refusees"]["boutons_dans_les_journaux"] == 0


def test_une_reponse_servie_ne_propose_jamais_la_recherche_simple(dom: dict[str, Any]) -> None:
    assert dom["reponse"]["boutons_de_repli"] == 0


def test_lattente_verrouille_les_deux_saisies_et_annonce_les_deux_journaux(
        dom: dict[str, Any]) -> None:
    """UX-DR10 : l'attente se **dit** et la saisie se ferme — des deux côtés, puis se rouvre."""
    assert dom["attente"]["pendant"]["desactives"] == [True, True, True, True]
    assert dom["attente"]["pendant"]["busy"] == ["true", "true"]
    assert dom["attente"]["apres"]["desactives"] == [False, False, False, False]
    assert dom["attente"]["apres"]["busy"] == [None, None]
    assert "Je cherche dans le guide" in dom["attente"]["texte"]


def test_peindre_nlaisse_rien_de_la_conversation_dans_le_navigateur(dom: dict[str, Any]) -> None:
    """AD-15 : le `localStorage` ne porte que le profil et les préférences. Peindre n'écrit rien."""
    assert dom["stockage"] == {}


def test_les_identifiants_du_harnais_dom_sont_ceux_de_la_page() -> None:
    """Le DOM minimal peint dans des éléments qu'il fabrique : si la page les renommait, il
    peindrait dans le vide et les tests resteraient verts."""
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    harnais = HARNAIS_UI.read_text("utf-8")
    tableau = harnais[harnais.index("const ELEMENTS = ["):harnais.index("];")]
    ids = re.findall(r'id: "([\w-]+)"', tableau)
    assert len(ids) >= 8, ids
    for identifiant in ids:
        assert f'id="{identifiant}"' in html, identifiant


def test_le_demarrage_du_site_est_le_seul_a_etre_saute_par_le_harnais() -> None:
    """Le point de couture est **un** drapeau, posé après l'export et avant le démarrage : le
    matérialiseur testé est celui que le navigateur exécute, pas une copie."""
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    assert ui.count("__UI_SANS_DEMARRAGE") == 1
    export = ui.index("window.UI = {")
    garde = ui.index("if (window.__UI_SANS_DEMARRAGE) return;")
    demarrage = ui.index("  chargerKB();")
    assert export < garde < demarrage, "la garde n'encadre pas le seul démarrage"


# --- AD-15 : ce que le navigateur garde, et ce qu'il ne garde plus --------

def _corps(source: str, entete: str) -> str:
    """Le corps d'une fonction JS, accolades équilibrées — pour asserter une **propriété** du code
    plutôt que l'orthographe exacte d'une ligne qu'un refactor déplacerait."""
    debut = source.index(entete) + len(entete)
    profondeur, i = 1, debut
    while profondeur:
        profondeur += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
    return source[debut:i]


def test_aucune_conversation_nest_ecrite_dans_le_navigateur() -> None:
    """UX-DR5. La propriété, et pas la ligne : ce qui part dans `localStorage` ne contient aucun
    historique. `historique: historique.slice(-20)` passait l'ancienne assertion."""
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    ecriture = _corps(ui, "function sauverProfil() {")
    assert "historique" not in ecriture, f"sauverProfil() écrit un historique :\n{ecriture}"
    lecture = _corps(ui, "function chargerProfil() {")
    assert not re.search(r"\bhistorique\s*=", lecture), "chargerProfil() restaure une conversation"
    assert "Conversation reprise" not in ui
    # Et toute écriture de la clé de chat passe par cette fonction, pas par une autre.
    ecritures = [m for m in re.findall(r"localStorage\.setItem\(\s*(\w+)", ui)
                 if m == "STORAGE_CHAT"]
    assert len(ecritures) == 1, "une seule écriture de la clé de chat, dans sauverProfil()"


def test_aucun_innerhtml_ne_sert_a_poser_du_texte() -> None:
    """AD-15 : tout texte reçu du serveur est rendu par `textContent`. La propriété est sur
    **chaque** affectation, pas sur la ligne : `a.innerHTML=""; b.innerHTML=t;` passait avant."""
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    sans_commentaires = re.sub(r"//[^\n]*", "", ui)
    affectations = re.findall(r"innerHTML\s*(\+?=)\s*([^;]+)", sans_commentaires)
    assert affectations, "le garde-fou ne mesure plus rien"
    for operateur, valeur in affectations:
        assert operateur == "=" and valeur.strip() == '""', (
            f"innerHTML {operateur} {valeur.strip()} — seul le vidage d'un conteneur est admis")
    # Le chemin du serveur, lui, ne touche pas du tout au HTML : il matérialise une description.
    materialiser = _corps(ui, "function materialiser(vue, grp) {")
    assert "innerHTML" not in materialiser and "textContent" in materialiser


# AD-15 amendé (revue Codex 1.7, tour 1) : la règle réservait « contenu non retenu par défaut » à
# confirmation avant livraison, et la confirmation l'a démentie — la politique d'Anthropic supprime
# les entrées et sorties de l'API **sous 30 jours**, avec des exceptions au-delà. Une promesse plus
# généreuse que celle du tiers qui exécute la requête est la « promesse du site contredite en
# silence » que cet AD prévient.
MENTION = ("Votre question et votre profil sont envoyés au serveur de ce site, puis au fournisseur "
           "du modèle (Anthropic) : sa politique publique, lue le 24/08/2026, prévoit la suppression "
           "des entrées et des sorties de l'API sous 30 jours, avec des exceptions — obligation "
           "légale, ou contenu que ses systèmes de sécurité signalent, conservé jusqu'à deux ans. "
           "Aucune conversation n'est enregistrée : ni par le serveur de ce site, ni dans ce "
           "navigateur, qui ne garde que votre profil et vos préférences d'affichage.")

LIEN_POLITIQUE = ("https://privacy.claude.com/en/articles/"
                  "7996866-how-long-do-you-store-my-organization-s-data")
INTITULE_LIEN = "Politique de conservation d'Anthropic"


def _plat(texte: str) -> str:
    """Blancs normalisés : l'indentation du HTML n'est pas une propriété du produit."""
    return " ".join(texte.split())


def test_la_mention_de_confidentialite_est_unique_et_datee() -> None:
    """AD-15. Trois formulations d'une même promesse font trois promesses ; et une politique
    fournisseur se cite avec la date à laquelle elle a été lue."""
    html = _plat((REPO_ROOT / "web" / "index.html").read_text("utf-8"))
    assert html.count(MENTION) == 2, "la même phrase sous les deux saisies, pas deux variantes"
    # Deux mentions, plus l'intitulé du lien qui les accompagne : rien d'autre ne parle du
    # fournisseur dans la page, donc aucune seconde formulation de la promesse.
    attendu = 2 * (MENTION.count("Anthropic") + INTITULE_LIEN.count("Anthropic"))
    assert html.count("Anthropic") == attendu, "aucune autre formulation ne traîne dans la page"
    assert "24/08/2026" in MENTION
    readme = _plat((REPO_ROOT / "web" / "README.md").read_text("utf-8"))
    assert MENTION in readme, "le README de la copie dit la même chose, mot pour mot"


def test_la_mention_dit_la_duree_reelle_et_donne_le_lien() -> None:
    """AD-15 amendé. « Non conservé par défaut » promettait à l'utilisateur davantage que ce que le
    fournisseur s'engage à faire ; et une affirmation sur un tiers se vérifie, elle ne se croit pas.
    La durée, ses exceptions et le lien sont donc dus — dans les deux saisies et dans le README."""
    html = _plat((REPO_ROOT / "web" / "index.html").read_text("utf-8"))
    readme = _plat((REPO_ROOT / "web" / "README.md").read_text("utf-8"))
    assert "n'est pas conservé par défaut" not in html + readme, (
        "la phrase démentie par la politique du fournisseur")
    for morceau in ("sous 30 jours", "avec des exceptions"):
        assert html.count(morceau) == 2, morceau
        assert morceau in readme
    assert html.count(LIEN_POLITIQUE) == 2, "le lien sous les deux saisies"
    assert LIEN_POLITIQUE in readme


def test_la_mention_est_rattachee_aux_deux_saisies() -> None:
    """Une mention que rien ne relie à la saisie n'est pas lue par qui utilise un lecteur d'écran —
    or c'est le point même de la mention."""
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    for saisie, mention in (("chat-input", "confid-chat"), ("widget-input", "confid-widget")):
        assert re.search(rf'id="{saisie}"[^>]*aria-describedby="{mention}"', html, re.S), saisie
        assert f'id="{mention}"' in html


def test_la_question_est_bornee_par_le_navigateur() -> None:
    """`ChatRequest.question` : 1 000 caractères. C'est le 400 le plus probable, et le seul que le
    navigateur puisse éviter tout seul."""
    from server.app.api.schemas import ChatRequest
    from annotated_types import MaxLen

    maxlen = next(m.max_length for m in ChatRequest.model_fields["question"].metadata
                  if isinstance(m, MaxLen))
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    for saisie in ("chat-input", "widget-input"):
        assert re.search(rf'id="{saisie}"[^>]*maxlength="{maxlen}"', html, re.S), saisie


def test_les_deux_journaux_sont_annonces_aux_lecteurs_decran() -> None:
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    assert html.count('aria-live="polite"') == 2
    # L'attente pose `aria-busy` : la région dit qu'elle est en train de changer.
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    verrou = _corps(ui, "function verrouillerSaisie(v, aRendreLeFocus) {")
    assert "aria-busy" in verrou and "focus()" in verrou


# --- UX-DR10 : ni frappe simulée, ni délai artificiel ---------------------

def test_le_delai_artificiel_et_la_frappe_mot_a_mot_ont_disparu() -> None:
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    assert "function taper(" not in ui
    assert "500 + Math.random() * 600" not in ui
    # L'attente est décrite par `chat.js` (`vueAttente`) et seulement matérialisée ici.
    assert "window.CHAT.vueAttente()" in ui and "verrouillerSaisie(true)" in ui


# --- le cache du navigateur ----------------------------------------------

def test_toutes_les_ressources_du_site_portent_la_meme_version() -> None:
    """L'invariant, pas le numéro : un navigateur qui a le guide en cache doit recharger **tout**
    d'un coup. Un seul fichier resté à la version précédente sert un `chat.js` d'avant le raccord
    contre le serveur d'après — profil en chaîne, donc 400."""
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    ressources = re.findall(r'(?:src|href)="((?:app|comparateur)/[^"]+)"', html)
    assert len(ressources) >= 9, ressources
    versions = {r.partition("?v=")[2] for r in ressources}
    assert versions and "" not in versions, f"une ressource sans version : {ressources}"
    assert len(versions) == 1, f"versions mélangées : {sorted(versions)}"


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


def test_lui_ne_decide_pas_de_ce_qui_souvre_apres_une_erreur() -> None:
    """La règle « pas de repli sur un 4xx » vit dans `chat.js` (`vueErreur`), où elle est testée.
    Si `ui.js` la rejugeait, la mutation qu'a exhibée la revue passerait de nouveau inaperçue."""
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    assert "indisponible" not in _corps(ui, "function afficherErreur(q, erreur) {")
    assert "rechercheSimple" not in _corps(ui, "function afficherErreur(q, erreur) {")
    materialiser = _corps(ui, "function materialiser(vue, grp) {")
    for mot in ("indispo", "recherche_simple", "chip\"", "503"):
        assert mot not in materialiser, f"materialiser() décide de quelque chose : {mot}"


def test_une_requete_en_vol_ne_survit_pas_a_la_reinitialisation() -> None:
    """Sinon elle peint la réponse d'une question effacée dans le fil neuf, et y pousse son tour."""
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    envoi = _corps(ui, "function envoyer(texteForce) {")
    assert "var mien = ++generation;" in envoi
    assert envoi.count("if (mien !== generation) return;") == 2  # succès **et** erreur
    assert "generation++;" in ui


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
