"""Matrice d'E/S du front du guide (spec 1.7) : AD-11 (a–f), AD-15, AD-16, AD-4, FR11, NFR4.

Le harnais `tests/js/chat_cases.mjs` charge `web/app/kb.js` puis `web/app/chat.js` dans un `node:vm`
avec `window`, `location` et `fetch` **doublés**, exécute les cas et écrit du JSON. Il ne juge rien :
tout ce qui est affirmé l'est ici. Aucun navigateur, aucun réseau, aucune dépendance ajoutée à
`pyproject.toml` — le harnais n'utilise que la bibliothèque standard de Node.

`ui.js` **est** couvert, par un second harnais (`tests/js/ui_cases.mjs`) qui l'exécute contre le DOM
minimal de `tests/js/dom_minimal.mjs`. La story 1.7 l'avait réduit à peindre, et cette docstring en
concluait qu'il n'y avait rien à y vérifier ; la story 2.2 a montré le contraire (revue 2.2, P9) —
c'est `afficherReponse` qui décidait de **ce que la page conserve** de chaque réponse, et elle n'en
gardait pas la question posée. Sont donc vérifiés ici : ce que `chat.js` décide (corps de la requête,
mapping de l'historique, lecture du contrat, classification des erreurs, appariement des citations,
textes composés) **et** ce que `ui.js` en fait (matérialisation, badges, tours conservés, et la
boucle entière par `envoyer()`). Ce qui reste au Chrome headless de `docs/tests-live.md` est le
rendu visuel, que ni l'un ni l'autre harnais ne juge.
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
from server.app.domain.answer import AbsenceProof, Answer
from server.app.domain.langue import LANGUES_SERVIES
from server.app.domain.question import CLARIFICATION_MAX_CHARS, Turn

HARNAIS = REPO_ROOT / "tests" / "js" / "chat_cases.mjs"
HARNAIS_UI = REPO_ROOT / "tests" / "js" / "ui_cases.mjs"
HARNAIS_CORPS = REPO_ROOT / "tests" / "js" / "corps_servi.mjs"


# Sans `node`, ces cas passent en `skip` — et un `skip` est indiscernable d'un succès dans un
# `pytest -q`. La CI (story 1.11) pose `FRONT_TESTS_REQUIS=1` : le skip devient alors un échec, et
# une image sans `node` ne peut plus faire passer la suite au vert en taisant la moitié du front.
REQUIS = os.environ.get("FRONT_TESTS_REQUIS", "") not in ("", "0")


def _lancer(harnais: Path, *args: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        motif = ("node absent de la machine : les cas du front ne peuvent pas tourner "
                 "(l'image de CI de la story 1.11 en a un)")
        if REQUIS:
            pytest.fail("FRONT_TESTS_REQUIS=1 mais " + motif)
        pytest.skip(motif)
    fini = subprocess.run([node, str(harnais), *args], capture_output=True, text=True, timeout=120,
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


# --- story 2.2 : le tour conservé est ce que l'assistant a dit -------------
#
# Le signal de clarification existe depuis la 1.4 (AD-5 : *comprendre* a deux sorties typées
# exclusives), le court-circuit depuis la 1.5, et la page peint la clarification comme une question
# depuis la 1.7 — mais la boucle ne se refermait jamais : `ui.js` ne poussait dans l'historique de
# page que `Answer.texte`, c'est-à-dire la phrase générique de refus. La question réellement posée
# (`Answer.clarification`) n'entrait nulle part ; au tour suivant *comprendre* recevait un historique
# où sa propre question ne figurait pas, la réponse d'un mot de l'arrivant restait irrésoluble, et il
# reposait la même question. Ce qui suit asserte les deux moitiés du correctif : la composition
# (`chat.js::tourAssistant`, pure) et le tour effectivement conservé par `ui.js`.

def test_le_tour_dune_reponse_ordinaire_est_exactement_le_texte(cas: dict[str, Any]) -> None:
    """Sans clarification, rien n'est ajouté : le tour vaut `texte`, octet pour octet. Une réponse
    servie n'a aucune raison de changer de forme dans l'historique."""
    releve = cas["tour_assistant_ordinaire"]
    assert releve["clarification"] is None
    assert releve["tour"] == releve["texte"]
    # Les bords ne fabriquent rien et ne lèvent pas : le tour vide est déjà filtré par
    # `historiquePourApi`, et une réponse sans `answer` (la recherche simple) rend son seul texte.
    bords = cas["tour_assistant_bords"]
    assert bords["vide"] == ""
    assert bords["absent"] == ""
    assert bords["sans_answer"] == "Réponse lexicale du guide, non vérifiée."
    assert bords["clarification_seule"] == "Lequel ?", "aucune espace ajoutée à une phrase absente"


def test_le_tour_dune_clarification_porte_la_question_posee(cas: dict[str, Any]) -> None:
    """La clarification **précède** la phrase, dans l'ordre exact où `vueReponse` les peint : le tour
    conservé reproduit l'écran, seule définition non arbitraire de « ce que l'assistant a dit ».
    La phrase générique reste — elle dit au modèle que l'assistant n'a **pas** répondu, là où la
    question seule laisserait croire à un échange normal."""
    from server.app.steps.restituer import PHRASES_DE_REFUS

    releve = cas["tour_assistant_clarification"]
    assert releve["texte"] == PHRASES_DE_REFUS["fr"]["clarification_requise"]
    assert releve["clarification"].endswith("?")
    assert releve["tour"] == releve["clarification"] + " " + releve["texte"]
    assert releve["tour"].index(releve["clarification"]) < releve["tour"].index(releve["texte"])


def test_un_tour_compose_ne_depasse_jamais_la_borne_dun_tour(cas: dict[str, Any]) -> None:
    """Revue 2.2, P1 — la borne se décide à la **composition**, pas à l'envoi.

    Rien ne borne `ClarificationRequise.clarification` côté serveur et `comprendre_max_tokens` vaut
    1 024 : une clarification de 1 968 caractères composait un tour de 2 063, au-delà de
    `Turn.texte`. `historiquePourApi` écartait alors ce tour **et tout ce qui le précédait** — sa
    règle « un tour qu'on ne peut pas envoyer casse la chaîne » —, si bien que *comprendre* recevait
    un historique vide avec « du permis de conduire » et reposait la même question : la boucle que
    cette story referme se rouvrait en silence.
    """
    from annotated_types import MaxLen

    maxlen = next(m.max_length for m in Turn.model_fields["texte"].metadata
                  if isinstance(m, MaxLen))
    releve = cas["tour_assistant_clarification_longue"]
    assert releve["compose_sans_borne"] > maxlen, "le cas ne mesure plus rien"
    assert releve["tour"] <= maxlen
    # Le morceau gardé l'est **entier** : hors borne ⇒ écarté, jamais tronqué (specs 1.8 D8, 1.9 D4).
    assert releve["tour_ne_coupe_rien"] is True
    # Priorité à la clarification : c'est elle, et elle seule, qui rend le tour suivant résoluble.
    assert releve["tour_est_la_clarification_entiere"] is True
    # Et l'essentiel : l'historique envoyé au tour suivant porte **encore** la question de
    # l'utilisateur — la chaîne n'est pas coupée.
    envoye = releve["envoye"]
    assert [t["role"] for t in envoye] == ["user", "assistant"]
    assert envoye[0]["texte"] == "Et celui-là, il faut le faire quand ?"
    for tour in envoye:
        Turn(**tour)


def test_une_clarification_qui_ne_tient_pas_seule_laisse_le_tour_au_texte(
        cas: dict[str, Any]) -> None:
    """Le dernier repli du front, désormais **hors du contrat** (revue Codex 2.2, B1).

    Ce repli perd la question posée : le tour redevient la phrase générique, l'assistant ne revoit
    plus sa propre question et la repose — la boucle que la story referme se rouvrait, en silence,
    ce qu'AD-16 interdit. Le front seul ne pouvait pas mieux faire (couper la question changerait ce
    qui a été dit) ; c'est donc le **serveur** qui a été corrigé : `Answer.clarification` est bornée
    par la même valeur que `Turn.texte`, si bien qu'aucune réponse conforme ne peut plus emprunter
    cette branche. Elle reste écrite, et testée, comme défense contre un serveur non conforme : sans
    elle, un tour hors borne couperait tout l'historique au lieu d'en perdre une ligne.
    """
    releve = cas["tour_assistant_clarification_enorme"]
    assert releve["tour_est_le_texte"] is True
    envoye = releve["envoye"]
    assert [t["role"] for t in envoye] == ["user", "assistant"]
    assert envoye[0]["texte"] == "Et celui-là, il faut le faire quand ?"
    # Ce que le contrat, lui, ne laisse plus passer : la clarification de ce cas est inatteignable.
    assert releve["clarification"] > CLARIFICATION_MAX_CHARS
    with pytest.raises(Exception, match="string_too_long|at most"):
        Answer(found=False, complete=False, reason=AbsenceProof(kind="clarification_requise"),
               clarification="y" * releve["clarification"])


def test_la_borne_de_la_clarification_est_celle_dun_tour_de_page(cas: dict[str, Any]) -> None:
    """Trois nombres, un seul invariant (revue Codex 2.2, B1).

    La question posée par l'assistant ne referme la boucle que si elle **tient** dans le tour
    d'historique que la page reconduit. Le serveur borne la clarification, le domaine borne le tour,
    le front porte `TOUR_MAX_CARACTERES` : si les trois divergent, la clarification recommence à
    disparaître sans qu'aucun test ne rougisse. Ce test les amarre les uns aux autres.
    """
    from annotated_types import MaxLen

    maxlen = next(m.max_length for m in Turn.model_fields["texte"].metadata
                  if isinstance(m, MaxLen))
    assert cas["bornes_avant_sonde"]["tour_max_caracteres"] == maxlen == CLARIFICATION_MAX_CHARS
    # Et la conséquence, sur le cas mesuré en revue : une clarification à la borne exacte, suivie de
    # la phrase générique, laisse le tour composé **égal à la clarification entière** — donc
    # envoyable, donc résoluble au tour suivant.
    releve = cas["tour_assistant_clarification_a_la_borne"]
    assert releve["clarification"] == CLARIFICATION_MAX_CHARS
    assert releve["compose_sans_borne"] > maxlen, "le cas ne mesure plus rien"
    assert releve["tour_est_la_clarification_entiere"] is True
    assert [t["texte"] for t in releve["envoye"]][-1] == releve["clarification_texte"]
    for tour in releve["envoye"]:
        Turn(**tour)


def test_la_question_posee_repart_au_serveur_au_tour_suivant(cas: dict[str, Any]) -> None:
    """La boucle refermée : le tour composé est poussé dans l'historique de page, l'arrivant répond
    « du permis de conduire », et c'est bien la question de l'assistant qui part — sous `{role,
    texte}`, la seule forme qu'AD-11 accepte."""
    envoye = cas["boucle_refermee"]
    assert [t["role"] for t in envoye] == ["user", "assistant"]
    assert envoye[-1]["texte"].startswith("De quel document ou démarche parlez-vous ?")
    for tour in envoye:
        assert set(tour) == {"role", "texte"}
        Turn(**tour)  # le contrat du serveur l'accepte tel quel


def test_le_tour_de_la_recherche_simple_reste_local_et_ne_repart_pas(
        cas: dict[str, Any], dom: dict[str, Any]) -> None:
    """Le même compositeur sert les deux `push`, et la règle de 1.7 ne bouge pas : la recherche
    simple ne pose aucune clarification (son tour vaut son texte), elle reste marquée `local`, elle
    ne repart pas au serveur et elle casse la chaîne — seule la queue contiguë qui la suit part."""
    releve = cas["tour_assistant_local"]
    assert releve["tour_egale_texte"] is True
    assert releve["texte_non_vide"] is True, "un tour vide passerait le test sans rien prouver"
    assert [t["role"] for t in releve["envoye"]] == ["user", "assistant"]
    assert releve["envoye"][0]["texte"] == "Et celui-là, il faut le faire quand ?"
    # Et le clic **réel** sur le bouton de repli, côté `ui.js`, marque toujours le tour.
    assert dom["indisponible"]["apres"]["historique"] == [{"role": "assistant", "local": True}]


def test_lhistorique_pousse_par_ui_porte_la_question_posee(dom: dict[str, Any]) -> None:
    """Le défaut corrigé vit dans le matérialiseur, pas dans la composition : c'est `afficherReponse`
    qui conserve. Le harnais l'exécute pour de bon, contre le DOM minimal — un test de `chat.js`
    seul ne l'aurait pas vu (leçon de la revue 1.7)."""
    releve = dom["tour_clarification"]
    assert [t["role"] for t in releve["historique"]] == ["assistant"]
    tour = releve["historique"][0]
    assert tour["local"] is False
    assert releve["clarification_peinte"] in tour["content"]
    assert releve["texte_peint"] in tour["content"]
    # `ui.js` pose ce que `chat.js` compose, il n'ajoute aucun mot de son cru.
    assert tour["content"] == releve["compose"]
    # Et ce tour repart tel quel au serveur quand l'arrivant répond en trois mots.
    assert releve["envoye"] == [{"role": "assistant", "texte": tour["content"]}]
    for envoye in releve["envoye"]:
        Turn(**envoye)


def test_le_tour_pousse_par_ui_pour_une_reponse_ordinaire_est_inchange(dom: dict[str, Any]) -> None:
    """L'autre moitié de la propriété : une réponse sans clarification garde exactement son texte."""
    releve = dom["tour_ordinaire"]
    assert releve["historique"] == [releve["texte"]]


def test_la_boucle_entiere_passe_par_envoyer_et_porte_la_question_posee(
        dom: dict[str, Any]) -> None:
    """Revue 2.2, P4 — le seul test du dépôt qui traverse la couture réparée de bout en bout.

    `afficherReponse` appelée seule fabrique un historique commençant par un tour `assistant`, ce
    qui n'arrive jamais dans la page : c'est `envoyer()` qui pousse d'abord le tour `user`. Ici, deux
    tours d'affilée dans le DOM minimal, avec un double de `fetch` — la clarification, puis une
    réponse ordinaire —, et l'on relève les **corps postés**.
    """
    releve = dom["boucle_complete"]
    postes = releve["corps_postes"]
    assert [c["question"] for c in postes] == ["Et celui-là, il faut le faire quand ?",
                                               "du permis de conduire"]
    # Premier tour : rien à envoyer, la conversation commence (l'écho de la question est exclu).
    assert postes[0]["historique"] == []
    # Second tour : la question posée par l'assistant est là, après le tour de l'utilisateur.
    second = postes[1]["historique"]
    assert [t["role"] for t in second] == ["user", "assistant"]
    assert second[1]["texte"].startswith("De quel document ou démarche parlez-vous ?")
    for tour in second:
        assert set(tour) == {"role", "texte"}
        Turn(**tour)
    # Et l'historique de page alterne, sans trou ni tour local.
    final = releve["historique_final"]
    assert [t["role"] for t in final] == ["user", "assistant", "user", "assistant"]
    assert all(t["local"] is False for t in final)
    assert final[3]["content"] == "Vous avez huit jours."  # une réponse ordinaire, texte inchangé
    assert releve["badges"]["onglet"]["texte"].startswith("mode api")


def test_la_composition_du_tour_est_la_meme_de_part_et_dautre(cas: dict[str, Any]) -> None:
    """Revue 2.2, P5 — `tests/test_suivi_live.py` écrit en Python le tour que la page compose en
    JavaScript, pour construire l'historique de son troisième scénario. Deux implémentations d'une
    même règle qu'aucune assertion ne relie divergent un jour ; c'est `chat.js::tourAssistant` qui
    fait autorité, et ce test compare les deux chaînes."""
    from tests.test_suivi_live import TOUR_ASSISTANT

    assert cas["tour_assistant_du_live"] == TOUR_ASSISTANT


def test_ui_ne_pousse_jamais_le_seul_texte_dans_lhistorique() -> None:
    """La propriété, pas la ligne : les **deux** `push` d'un tour assistant passent par le
    compositeur de `chat.js`. Un `content: r.texte` réintroduit ici perdrait de nouveau la question
    posée, sans qu'aucune vue ne change — c'est exactement ainsi que le défaut a duré cinq stories."""
    ui = (REPO_ROOT / "web" / "app" / "ui.js").read_text("utf-8")
    pousses = re.findall(r'historique\.push\(\{\s*role:\s*"assistant"[^}]*\}\)', ui)
    assert len(pousses) == 2, pousses
    for pousse in pousses:
        assert "window.CHAT.tourAssistant(r)" in pousse, pousse


def test_le_compositeur_du_tour_assistant_est_exporte(cas: dict[str, Any]) -> None:
    """Il est pur et sans DOM : `ui.js` ne peut le poser que s'il est exporté, et les tests ne
    peuvent l'exercer que par là."""
    assert "tourAssistant" in cas["exporte"]


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


def test_un_compteur_de_preuve_non_entier_nest_pas_une_reponse(cas: dict[str, Any]) -> None:
    """AD-16 : un 200 dont un champ affiché n'a pas le type du contrat est `reponse_illisible`,
    jamais peint comme une réponse (ici les compteurs de `AbsenceProof`, rendus à l'écran)."""
    for nom in ("preuve_compteur_objet", "preuve_compteur_chaine"):
        vu = cas["contrat_incomplet"][nom]
        assert vu["a_repondu"] is False, nom
        assert vu["code"] == "reponse_illisible", nom
        assert vu["lectures_du_moteur_lexical"] == 0, nom


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

CONTRATS_INCOMPLETS = [
    ("vide", "texte"), ("sans_answer", "answer"), ("sans_trace", "trace"),
    ("sans_texte", "texte"), ("answer_nul", "answer"),
    ("answer_sans_found", "answer.found"), ("answer_sans_complete", "answer.complete"),
    ("trace_sans_request_id", "trace.request_id"), ("trace_sans_pipeline", "trace.pipeline"),
    ("answer_trouve_sans_claim", "answer.claims"),
    ("answer_absent_sans_reason", "answer.reason"),
    ("answer_absent_avec_claims", "answer.claims"),
    ("answer_claim_non_pertinente", "answer.claims"),
    ("answer_complete_sans_found", "answer.complete"),
    ("answer_complete_avec_unknown", "answer.complete"),
    ("answer_claims_non_liste", "answer.claims"),
    ("sources_non_liste", "sources"), ("ancien_contrat", "texte"),
    # `null` n'est pas l'absence : aucun champ à valeur par défaut du contrat n'est `| None`.
    ("segments_nuls", "segments"), ("sources_nulles", "sources"), ("fiches_nulles", "fiches"),
    ("unknown_nul", "unknown"), ("comparateur_nul", "comparateur"), ("via_nul", "via"),
    ("cout_nul", "trace.total_cost_eur"), ("answer_claims_nulles", "answer.claims"),
    ("answer_unknown_nul", "answer.unknown"),
    ("answer_lang_nombre", "answer.lang"), ("answer_lang_nul", "answer.lang"),
    ("answer_lang_inconnu", "answer.lang"),
    ("answer_repli_nul", "answer.lang_fallback"),
    ("vue_traduite_et_repliee", "answer.lang_fallback"),
    # La preuve d'absence est un `AbsenceProof` entier, pas un objet quelconque.
    ("answer_reason_vide", "answer.reason.kind"),
    ("answer_reason_kind_inconnu", "answer.reason.kind"),
    ("answer_reason_termes_nuls", "answer.reason.terms_searched"),
    # Les champs obligatoires des objets imbriqués que l'écran consomme.
    ("segment_sans_kind", "segments[0].kind"), ("source_sans_quote", "sources[0].quote"),
    ("claim_sans_quote", "answer.claims[0].quotes"),
    ("claim_sans_status", "answer.claims[0].status"),
    # Story 2.5 : ce que le panneau « Pourquoi cette réponse » consomme est **strict sur le type**.
    # Tolérant à l'absence (les deux lots sont écrits en parallèle), mais un champ présent et mal
    # typé peindrait un compteur que rien n'a calculé sous le panneau qui répond de l'honnêteté du
    # reste. Chacun est aussi refusé par `ChatResponse.model_validate()` (test suivant).
    ("trace_blocs_non_liste", "trace.blocs"),
    ("trace_bloc_titre_nombre", "trace.blocs[0].titre"),
    ("trace_gate_nombre", "trace.gate"),
    ("trace_gate_alerts_non_liste", "trace.gate.alerts"),
    ("trace_dictionnaire_non_booleen", "trace.dictionnaire.charge"),
    ("trace_retries_objet", "trace.retries"),
    ("trace_steps_non_liste", "trace.steps"),
    ("trace_step_check_ok_chaine", "trace.steps[0].checks[0].ok"),
    ("trace_thresholds_non_objet", "trace.thresholds"),
    ("trace_cout_negatif", "trace.total_cost_eur"),
    ("trace_cout_infini", "trace.total_cost_eur"),
    ("trace_seuil_chaine", "trace.thresholds.max_opens"),
    ("trace_seuil_booleen", "trace.thresholds.max_opens"),
    # Story 4.2f : le second porteur d'un `found=false` est lu aussi strictement que le premier.
    # Ses deux compteurs sont **affichés** ; absents ou négatifs, la page peindrait une lecture que
    # rien n'a mesurée. Et « exactement un porteur » vaut ici comme dans le domaine.
    ("answer_lecture_sans_compteur", "answer.lecture_partielle.nodes_read"),
    ("answer_lecture_compteur_negatif", "answer.lecture_partielle.nodes_read"),
    ("answer_lecture_compteur_fractionnaire", "answer.lecture_partielle.nodes_read"),
    ("answer_deux_porteurs", "answer.lecture_partielle"),
    ("answer_lecture_sans_manque", "answer.unknown"),
    ("answer_lecture_sur_reponse_trouvee", "answer.lecture_partielle"),
    # « `found=True` n'en porte **aucun** » : les deux porteurs, pas seulement le second.
    ("answer_reason_sur_reponse_trouvee", "answer.reason"),
    # Les deux compteurs ont un plancher à 1 : zéro section pour au moins un passage est un état
    # impossible (AD-2), zéro passage est l'erreur terminale d'AD-1/NFR2.
    ("answer_lecture_sans_section", "answer.lecture_partielle.nodes_read"),
    ("answer_lecture_sans_passage", "answer.lecture_partielle.blocks_read"),
]


def test_le_pied_dit_la_traduction_et_le_repli_sans_les_confondre(cas: dict[str, Any]) -> None:
    """Story 2.4 : mention et repli sont deux faits indépendants, absents quand ils ne valent pas."""
    aucune = cas["vue_nominale"]
    defauts_absents = cas["vue_langue_absente"]
    traduction = cas["vue_traduite"]
    repli = cas["vue_repliee"]

    assert aucune["mentions_langue"] == [] and aucune["repli_langue"] is None
    assert defauts_absents["mentions_langue"] == [] and defauts_absents["repli_langue"] is None
    assert traduction["mentions_langue"] == [
        "traduit depuis le guide (français) — les passages cités restent tels qu'ils sont écrits"
    ]
    assert traduction["repli_langue"] is None
    assert repli["mentions_langue"] == [
        "langue non prise en charge ou non détectée : réponse en français"
    ]
    assert repli["repli_langue"] == repli["mentions_langue"][0]
    assert traduction["pied"] == ["etat etat-sur", "etat-phrase", "langue-mention", "cout"]
    assert repli["pied"] == [
        "etat etat-sur", "etat-phrase", "langue-mention langue-repli", "cout"]


def test_les_langues_du_front_sont_amarrees_a_celles_du_serveur(cas: dict[str, Any]) -> None:
    assert cas["bornes_avant_sonde"]["langues_servies"] == list(LANGUES_SERVIES)


@pytest.mark.parametrize(("nom", "champ"), CONTRATS_INCOMPLETS)
def test_un_200_qui_ne_tient_pas_le_contrat_nest_pas_peint(cas: dict[str, Any], nom: str,
                                                           champ: str) -> None:
    """AD-16 prévient « réponse vide présentée comme réponse ». Une valeur par défaut à la place d'un
    champ obligatoire peignait un corps `{}` en réponse « inconnu », l'ajoutait à l'historique et le
    faisait repartir au serveur au tour suivant. Un 200 incomplet est un serveur cassé, pas une
    réponse dégradée : `reponse_illisible`, comme un corps non-JSON.

    « Incomplet » se lit sur le contrat **entier** : les objets imbriqués que l'écran consomme ont
    eux aussi des champs obligatoires (`AbsenceProof.kind` décide de la preuve chiffrée affichée), et
    `null` n'est **pas** l'absence — aucun champ à valeur par défaut de `ChatResponse` n'est `| None`,
    donc pydantic refuse `null` là où il accepte l'omission."""
    vu = cas["contrat_incomplet"][nom]
    assert vu["a_repondu"] is False, f"{nom} a été peint comme une réponse"
    assert (vu["kind"], vu["code"]) == ("requete", "reponse_illisible")
    assert vu["champ"] == champ
    # « requete » et non « indisponible » : aucun bouton de repli (AD-11/AD-16).
    assert vu["lectures_du_moteur_lexical"] == 0


@pytest.mark.parametrize("nom", [n for n, _ in CONTRATS_INCOMPLETS])
def test_aucun_corps_refuse_par_le_front_nest_un_corps_que_le_serveur_sert(cas: dict[str, Any],
                                                                          nom: str) -> None:
    """Le pendant du test précédent : ce que le front refuse, le serveur ne peut pas le servir.

    `ChatResponse.model_validate()` est l'autorité — `Trace` exige `request_id` et `pipeline`,
    `Answer._found_coherence` exige une preuve d'absence sous `found=False`, au moins une claim
    retrouvée ∧ pertinente sous `found=True`, et `unknown=[]` sous `complete=True`. Un corps refusé
    par le front mais accepté par pydantic serait une réponse servie perdue ; c'est cette moitié-là
    que ce test garde, l'autre moitié étant gardée par
    `test_les_champs_a_valeur_par_defaut_restent_facultatifs`.
    """
    from pydantic import ValidationError

    from server.app.api.schemas import ChatResponse

    corps = cas["contrat_incomplet"][nom]["corps"]
    with pytest.raises(ValidationError):
        ChatResponse.model_validate(corps)


def test_le_champ_fautif_ne_va_pas_a_lecran(cas: dict[str, Any]) -> None:
    """Le nom du champ manquant sert au développeur ; l'utilisateur lit la phrase du `code`."""
    for vu in cas["contrat_incomplet"].values():
        assert vu["champ"] not in vu["message"]
        assert vu["message"] == cas["corps_illisible"]["message"]


def test_les_champs_a_valeur_par_defaut_restent_facultatifs(cas: dict[str, Any]) -> None:
    """La lecture stricte porte sur ce que le contrat exige, et **rien de plus** : refuser un corps
    parce que `sources` est absent inventerait une exigence que `ChatResponse` n'a pas.

    Le corps témoin est validé par `ChatResponse.model_validate()` avant d'être opposé au front : la
    borne du bas est donc un corps réellement servable, et non un corps que le harnais aurait cru
    valide. Le précédent ne l'était pas — `found=True` sans claim et une trace sans `request_id` ni
    `pipeline` — et la lecture du front le peignait pourtant en réponse (revue Codex 1.7, B2, tour 2).
    """
    from server.app.api.schemas import ChatResponse

    obligatoires = {n for n, f in ChatResponse.model_fields.items() if f.is_required()}
    assert obligatoires == {"texte", "answer", "trace"}, obligatoires
    minimal = cas["contrat_minimal"]
    ChatResponse.model_validate(minimal["corps"])  # le serveur peut le servir…
    absents = set(ChatResponse.model_fields) - set(minimal["corps"])
    assert absents == {"segments", "sources", "fiches", "unknown", "comparateur", "via"}, absents
    assert minimal["texte"] == "Cette question sort de ce que couvre le guide."  # … le front le lit
    assert minimal["sources"] == [] and minimal["segments"] == []
    assert minimal["via"] == "api/v1" and minimal["comparateur"] is False


def _corps_servis() -> list[dict[str, Any]]:
    """Trois réponses **sérialisées par le serveur lui-même** : `ChatResponse.model_dump(mode="json")`
    est exactement ce que FastAPI écrit sur le fil pour `POST /chat`.

    Les corps ne sont donc pas écrits à la main : chaque champ qu'ils portent (y compris les `null`
    des champs optionnels) vient des modèles du domaine, montés avec leurs validateurs.
    """
    from server.app.api.schemas import ChatResponse, SourceItem
    from server.app.domain.answer import (
        AbsenceProof, Answer, AnswerSegment, ClaimStatus, VerifiedClaim, VerifiedQuote,
    )
    from server.app.domain.trace import Trace

    texte = "Vous avez huit jours pour déclarer votre arrivée."
    quote = VerifiedQuote(block_id="lux-guide:farrivee:2", quote="huit jours",
                          start=0, end=10, text_start=12, text_end=22)
    claim = VerifiedClaim(claim_id="c1", text="Le délai est de huit jours.", quotes=[quote],
                          status=ClaimStatus(retrouvee=True, pertinente=True, edition="git:a8e8593"))
    segment = AnswerSegment(text=texte, kind="factuel", claim_ids=["c1"])
    source = SourceItem(block_id=quote.block_id, fiche_id="arrivee", titre="Déclarer son arrivée",
                        url="https://guichet.public.lu/arrivee", quote=quote.quote, status="verifiee")
    trace = Trace(request_id="r-servi", pipeline="guide", intent="question", total_cost_eur=0.0278)

    sure = Answer(found=True, complete=True, texte=texte, segments=[segment], claims=[claim])
    partielle = Answer(found=True, complete=False, texte=texte, segments=[segment], claims=[claim],
                       unknown=["le coût exact"])
    phrase = "Cette question sort de ce que couvre le guide."
    refus = Answer(found=False, complete=False, texte=phrase,
                   segments=[AnswerSegment(text=phrase, kind="limite")],
                   reason=AbsenceProof(kind="hors_perimetre", terms_searched=["météo"],
                                       variants_count=4, blocks_scanned=312))
    # Story 4.2f : la quatrième issue servable. Son corps n'est pas écrit à la main non plus — il
    # passe par `Answer` (donc par « exactement un porteur » et « une lecture partielle dit ce qui
    # lui manque ») puis par `ChatResponse`, c'est-à-dire par le code même de la route.
    from server.app.domain.answer import LecturePartielle

    borne = "Je n'ai pas pu lire tout ce qui pouvait concerner votre question."
    lecture = Answer(
        found=False, complete=False, texte="Ma lecture s'est arrêtée avant de conclure.",
        segments=[AnswerSegment(text="Ma lecture s'est arrêtée avant de conclure.", kind="limite")],
        lecture_partielle=LecturePartielle(nodes_read=2, blocks_read=5,
                                           documents=["lux-guide"]),
        unknown=[borne])
    reponses = [
        ChatResponse(texte=sure.texte, segments=sure.segments, sources=[source], fiches=["arrivee"],
                     unknown=[], answer=sure, trace=trace),
        ChatResponse(texte=partielle.texte, segments=partielle.segments, sources=[source],
                     fiches=["arrivee"], unknown=partielle.unknown, answer=partielle, trace=trace),
        ChatResponse(texte=refus.texte, segments=refus.segments, answer=refus, trace=trace),
        ChatResponse(texte=lecture.texte, segments=lecture.segments, unknown=lecture.unknown,
                     answer=lecture, trace=trace),
    ]
    return [r.model_dump(mode="json") for r in reponses]


def test_le_front_lit_les_reponses_que_le_serveur_sert_vraiment(tmp_path: Path) -> None:
    """L'autre moitié de la lecture stricte : ce que le serveur sert, le front doit le **peindre**.

    Une exigence de trop et une réponse valide devient un « assistant indisponible » à l'écran —
    panne silencieuse et symétrique de celle que la revue Codex a relevée (B2). Les corps opposés au
    front sont sérialisés par `ChatResponse`, donc par le code même de la route.
    """
    corps = _corps_servis()
    fichier = tmp_path / "corps-servis.json"
    fichier.write_text(json.dumps(corps, ensure_ascii=False), "utf-8")

    releves = _lancer(HARNAIS_CORPS, str(fichier))

    assert [r["lu"] for r in releves] == [True, True, True, True], releves
    sure, partielle, refus, lecture = releves
    assert sure["etat"]["cle"] == "sur" and sure["citations_par_segment"] == [1]
    assert sure["sources"] == ["lux-guide:farrivee:2"] and sure["via"] == "api/v1"
    assert partielle["etat"]["cle"] == "partiel"
    assert refus["etat"]["cle"] == "inconnu" and refus["sources"] == []
    # Story 4.2f : le corps que la story fait naître est lu, badgé et **chiffré** — pas refusé.
    assert lecture["etat"]["cle"] == "lecture-partielle"
    assert lecture["etat"]["texte"] == "lecture partielle"
    assert lecture["sources"] == []
    assert lecture["lecture"] == ("Lecture partielle : 2 sections lues, 5 passages transmis au "
                                  "modèle — le reste n'a pas été lu, et rien n'en est affirmé")
    assert lecture["inconnus"] == [
        "Je n'ai pas pu lire tout ce qui pouvait concerner votre question."]


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
    assert refus["texte"] == PHRASES_DE_REFUS["fr"]["hors_perimetre"]
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
    assert vue["ordre_des_blocs"] == ["seg seg-factuel", "seg seg-factuel", "pied", "pourquoi",
                                     "chips"]
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
    # Le pied vient après le corps, et le panneau « Pourquoi cette réponse » après le pied : il
    # n'explique rien qui ne soit déjà écrit.
    assert vue["ordre_des_blocs"][-3] == "pied"
    assert vue["ordre_des_blocs"][-2] == "pourquoi"


def test_une_reponse_incomplete_montre_ce_quelle_ne_sait_pas(cas: dict[str, Any]) -> None:
    vue = cas["vue_partielle"]
    assert vue["inconnus"] == ["montant exact", "délai de recours"]
    assert vue["etat_texte"] == "partiel"
    assert vue["ordre_des_blocs"].index("inconnu") < vue["ordre_des_blocs"].index("pied")


# --- Story 2.3 : les trois états sont dits en clair, pas seulement peints -
#
# L'AC de la story 2.3 demande que la réponse « distingue sûr / partiel / inconnu ». Le badge d'un
# mot ne distingue rien à lui seul : « PARTIEL » ne dit ni ce qui manque ni où le lire. La phrase du
# pied nomme ce que l'état engage — et elle ne décrit que ce que la vue a **réellement** posé.

@pytest.mark.parametrize(("nom", "cle", "attendue"), [
    ("vue_nominale", "sur", "tout ce qui est affirmé ci-dessus est appuyé par un passage cité, "
                            "et la question est couverte"),
    ("vue_partielle", "partiel", "il manque des éléments : ils sont listés sous "
                                 "« Ce que je ne sais pas »"),
    ("vue_refus", "inconnu", "rien n'a été retenu : la preuve de cette absence est ci-dessus"),
])
def test_chaque_etat_est_suivi_de_la_phrase_qui_le_rend_explicite(
        cas: dict[str, Any], nom: str, cle: str, attendue: str) -> None:
    vue = cas[nom]
    assert vue["etat"] == "etat etat-" + cle
    # Le badge d'abord, sa phrase juste après, le coût ensuite : l'ordre du pied est celui de la
    # lecture, et la phrase ne s'intercale pas entre le coût et rien.
    assert vue["pied"][:2] == ["etat etat-" + cle, "etat-phrase"]
    assert vue["etat_phrase"] == attendue


def test_la_phrase_du_partiel_renvoie_a_la_liste_seulement_quand_elle_est_la(
        cas: dict[str, Any]) -> None:
    """Renvoyer à « Ce que je ne sais pas » quand la section n'est pas peinte désignerait un bloc
    absent. Le domaine interdit désormais ce corps (`found ∧ ¬complete ⇒ unknown ≠ []`), mais la
    page ne refuse pas une réponse que le serveur a jugée servable : elle dit ce qu'elle a."""
    avec = cas["vue_partielle"]
    assert avec["inconnus"] and "Ce que je ne sais pas" in avec["etat_phrase"]

    sans = cas["vue_partielle_sans_liste"]
    assert sans["etat_texte"] == "partiel" and sans["inconnus"] == []
    assert "inconnu" not in sans["ordre_des_blocs"]
    # Sans la liste, la page ne sait pas **quelle** cause a empêché `complete` — facette omise,
    # lecture bornée, renvoi non résolu, relance abandonnée… Elle dit donc ce qu'elle sait, et rien
    # de plus : aucune de ces causes n'est un défaut de couverture, et l'affirmer serait faux.
    assert sans["etat_phrase"] == "il manque des éléments, et rien n'indique lesquels"
    assert "Ce que je ne sais pas" not in sans["etat_phrase"]
    assert "couvre" not in sans["etat_phrase"]


def test_la_phrase_de_linconnu_ne_promet_une_preuve_que_si_elle_est_peinte(
        cas: dict[str, Any]) -> None:
    """Une clarification est un `found=False` **sans** preuve chiffrée (AD-4 : « rien n'a été
    cherché ») : lui accrocher « la preuve est ci-dessus » désignerait le vide."""
    refus = cas["vue_refus"]
    assert refus["preuve"] and "la preuve de cette absence est ci-dessus" in refus["etat_phrase"]

    clar = cas["vue_clarification"]
    assert clar["etat_texte"] == "inconnu" and clar["preuve"] is None
    assert clar["etat_phrase"] == "rien n'a été cherché : la question doit d'abord être précisée"


def test_la_phrase_detat_est_composee_par_le_code_et_ne_leve_sur_aucun_bord(
        cas: dict[str, Any]) -> None:
    """AD-16 / NFR2 : ces phrases sont du code, pas du modèle. Un état ou un contexte absent doit
    donc rendre une phrase — la plus prudente — plutôt que `undefined` ou une exception."""
    phrases = cas["phrases_etat"]
    assert phrases["partiel_avec_liste"] != phrases["partiel_sans_liste"]
    assert phrases["inconnu_avec_preuve"] != phrases["inconnu_sans_preuve"]
    # Sans contexte, on ne renvoie à rien ; sans état, on ne présume pas d'une réponse trouvée.
    assert phrases["sans_contexte"] == phrases["partiel_sans_liste"]
    assert phrases["sans_etat"] == phrases["inconnu_sans_preuve"]
    assert phrases["etat_inconnu_du_front"] == phrases["inconnu_avec_preuve"]
    for valeur in phrases.values():
        assert isinstance(valeur, str) and valeur and "undefined" not in valeur


def test_aucune_phrase_detat_ne_nomme_le_document(cas: dict[str, Any]) -> None:
    """Revue 2.3, B1. `server/app/steps/verifier.py` compose ses phrases de lacune sans nommer aucun
    document, et le justifie par le fait que la page sinistre rend la même section « Ce que je ne
    sais pas ». Le pied qui les surmonte est soumis à la même contrainte, sans quoi les deux
    raisonnements se contredisent : « rien n'a été retenu du guide » sous une réponse portant sur des
    conditions générales serait faux, et obligerait à un second jeu de phrases pour l'autre front.
    """
    interdits = ("guide", "contrat", "conditions générales", "fiche", "police", "assureur")
    phrases = list(cas["phrases_etat"].values())
    phrases += [v["etat_phrase"] for nom, v in cas.items()
                if nom.startswith("vue_") and v.get("etat_phrase")]
    assert phrases
    for phrase in phrases:
        minuscule = phrase.lower()
        assert not any(mot in minuscule for mot in interdits), phrase


def test_la_recherche_simple_nemprunte_pas_la_phrase_dune_reponse_verifiee(
        cas: dict[str, Any]) -> None:
    """Le pied local dit déjà « aucune vérification » : lui ajouter la phrase d'un état vérifié lui
    donnerait la forme qu'AD-16 lui refuse."""
    vue = cas["vue_locale"]
    assert vue["etat_phrase"] is None
    assert vue["pied"] == ["etat etat-local", "sans-verif"]


def test_un_refus_montre_la_phrase_du_serveur_puis_la_preuve(cas: dict[str, Any]) -> None:
    from server.app.steps.restituer import PHRASES_DE_REFUS

    vue = cas["vue_refus"]
    assert vue["segments"][0]["texte"] == PHRASES_DE_REFUS["fr"]["hors_perimetre"]
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
                                                      "p", "span", "strong", "summary", "ul"}



# --- Story 2.5 : « Pourquoi cette réponse » --------------------------------
#
# AD-10 pose que la trace est « consultable » ; jusqu'ici le front n'en lisait que `total_cost_eur`.
# L'AC est une **liste de rubriques** : elle se vérifie sur l'arbre composé, rubrique par rubrique.
# La règle qui gouverne tout ce bloc est celle d'AD-16 : **ce que la trace ne dit pas, l'écran ne le
# dit pas** — un champ absent fait disparaître sa rubrique, aucune valeur n'est devinée, aucun
# défaut n'est présenté comme une mesure.


def _rubriques(vue: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {r["titre"]: r["lignes"] for r in vue["pourquoi"]["rubriques"]}


def test_le_panneau_est_replie_et_porte_le_titre_de_lac(cas: dict[str, Any]) -> None:
    panneau = cas["vue_pourquoi_complet"]["pourquoi"]
    # Un `<details>` natif : dépliable sans une ligne de JavaScript, donc sans état à tenir, et
    # replié par défaut (aucun `open` n'est décrit).
    assert panneau["tag"] == "details"
    assert panneau["summary"] == "Pourquoi cette réponse"


def test_le_panneau_affiche_les_etapes_avec_leur_tier_et_leur_duree(cas: dict[str, Any]) -> None:
    lignes = [ligne["texte"] for ligne in _rubriques(cas["vue_pourquoi_complet"])["Étapes"]]
    assert lignes == ["comprendre · micro · 912 ms", "retrouver · reason · 3480 ms",
                      "rediger · reason · 12800 ms", "verifier · micro · 1450 ms",
                      "restituer · aucun appel · 2 ms"]
    # `restituer` n'appelle aucun modèle : `tier` est `null`, et cela se **dit** plutôt que de
    # laisser une place vide qu'on lirait comme une donnée manquante.
    assert lignes[-1].endswith("aucun appel · 2 ms")


def test_le_panneau_distingue_les_blocs_ouverts_de_ceux_qui_ont_ete_ecartes(
        cas: dict[str, Any]) -> None:
    """AD-10 : « blocs effectivement passés au modèle » d'un côté, « candidats de `chercher` non
    ouverts » de l'autre. Les confondre ferait croire que le modèle a lu ce qu'il n'a pas lu."""
    rubriques = _rubriques(cas["vue_pourquoi_complet"])
    assert [ligne["texte"] for ligne in rubriques["Passages ouverts"]] == [
        "lux-guide:farrivee:2 — Les huit premiers jours",
        "lux-guide:farrivee:3 — Les huit premiers jours"]
    assert [ligne["texte"] for ligne in rubriques["Passages écartés, non lus par le modèle"]] == [
        "lux-guide:fbanque:1 — Ouvrir un compte"]


def test_un_bloc_que_la_trace_ne_resout_pas_porte_son_id_seul(cas: dict[str, Any]) -> None:
    """M3 : jamais de titre deviné. Le front n'a aucun moyen de retrouver le titre d'une fiche du
    corpus servi — `kb.js` peut en diverger —, et en inventer un sous une réponse sourcée serait
    exactement l'invention que la story combat."""
    rubriques = _rubriques(cas["vue_pourquoi_bloc_non_resolu"])
    assert [ligne["texte"] for ligne in rubriques["Passages ouverts"]] == [
        "lux-guide:farrivee:2 — Les huit premiers jours", "lux-guide:farrivee:3"]
    assert [ligne["texte"] for ligne in rubriques["Passages écartés, non lus par le modèle"]] == [
        "lux-guide:fbanque:1"]


def test_le_panneau_dit_les_controles_passes_et_echoues(cas: dict[str, Any]) -> None:
    lignes = _rubriques(cas["vue_pourquoi_complet"])["Contrôles"]
    assert [ligne["etat"] for ligne in lignes] == ["ok", "ok", "ok", "ok", "ko"]
    # Le libellé français, puis le détail composé par le serveur — jamais le seul `name` brut.
    assert lignes[0]["texte"].startswith("intention rendue par le modèle")
    assert "3 déclencheur(s) du dictionnaire sur 30" in lignes[0]["texte"]
    assert lignes[-1]["texte"].startswith("des sous-questions posées ne sont pas couvertes")


def test_un_controle_inconnu_du_front_saffiche_tel_quel(cas: dict[str, Any]) -> None:
    """Un `CheckResult.name` ajouté demain ne doit pas **disparaître** de l'écran : le panneau
    répond de l'honnêteté du reste, il serait le pire endroit où taire un contrôle."""
    lignes = _rubriques(cas["vue_pourquoi_controle_inconnu"])["Contrôles"]
    assert [ligne["texte"] for ligne in lignes] == ["controle_de_demain — détail"]
    assert cas["tables"]["controle_inconnu"] == "controle_de_demain"
    assert cas["tables"]["controle_connu"] == "refus composé, avec sa preuve d'absence"


def test_le_pictogramme_dun_controle_ne_porte_pas_seul_son_sens(cas: dict[str, Any]) -> None:
    """Une information portée par la seule couleur (ou le seul glyphe) est invisible pour qui ne la
    distingue pas. Le pictogramme est donc `aria-hidden`, et il ne fait que répéter ce que le
    libellé écrit déjà — c'est aussi le seul usage d'`attrs` dans tout ce que `chat.js` compose."""
    lignes = _rubriques(cas["vue_pourquoi_complet"])["Contrôles"]
    for ligne in lignes:
        assert ligne["picto"] == {"aria-hidden": "true"}
    assert all(a == {"aria-hidden": "true"} for a in cas["vue_pourquoi_complet"]["pourquoi"]["attrs"])


def test_le_panneau_ne_pose_aucun_id_car_il_est_peint_deux_fois(cas: dict[str, Any]) -> None:
    """L'arbre est matérialisé **une fois par journal** (l'onglet Assistant et le widget flottant) :
    un `id` unique s'y trouverait deux fois dans le même document."""
    for nom, vue in cas.items():
        if nom.startswith("vue_") and vue.get("pourquoi"):
            assert vue["pourquoi"]["ids"] == 0, nom


def test_le_panneau_montre_les_affirmations_ecartees_sans_leur_citation(cas: dict[str, Any]) -> None:
    """Reprise différée de 1.7 : `answer.rejected_claims` n'était peint nulle part côté guide.

    AD-3 les conserve « affichables par le front » et AD-11 s'appuie sur cette publication pour
    justifier qu'elles n'entrent pas dans `sources[]`. Ce qui n'est **jamais** affiché, c'est leur
    citation : une claim `non_retrouvee` ou `ambigue` porte la chaîne du modèle, qu'aucun corpus n'a
    confirmée (la page sinistre a tranché ainsi en 1.9, le guide s'aligne).
    """
    rejetees = cas["vue_pourquoi_complet"]["pourquoi"]["rejetees"]
    assert [r["texte"] for r in rejetees] == ["Le certificat coûte dix euros."]
    assert rejetees[0]["motif"] == "citation introuvable dans les passages relus (non_retrouvee)"
    # La quote de la claim rejetée n'apparaît nulle part dans l'arbre entier.
    assert "CETTE QUOTE NE DOIT JAMAIS S'AFFICHER" not in cas["pourquoi_texte_entier"]
    assert cas["tables"]["motif_inconnu"] == "écartée par la vérification"


def test_le_panneau_dit_les_relances_les_troncatures_et_le_cout(cas: dict[str, Any]) -> None:
    lignes = [ligne["texte"] for ligne in
              _rubriques(cas["vue_pourquoi_complet"])["Ce que la requête a coûté"]]
    # Zéro se **dit** : « 0 troncature » est une mesure, l'omission serait une absence de mesure.
    assert lignes == ["1 relance", "0 troncature", "délai restant : 36,3 s",
                      "cette réponse a coûté 0,0278 €"]


def test_les_seuils_actifs_sont_dans_un_sous_panneau_replie(cas: dict[str, Any]) -> None:
    """Convention Seuils du spine : « exposés dans `Trace.thresholds` ». Ils sont nombreux et
    rarement lus : un second `<details>`, replié, les met à portée sans encombrer le panneau."""
    seuils = cas["vue_pourquoi_complet"]["pourquoi"]["seuils"]
    assert seuils["tag"] == "details"
    assert seuils["summary"] == "Seuils actifs (3)"
    # Triés : deux requêtes portant les mêmes seuils écrivent la même liste.
    assert seuils["lignes"] == ["max_opens : 8", "quote_min_chars : 24", "search_limit : 40"]


def test_le_panneau_dit_le_profil_de_gate_et_les_alertes_du_document(cas: dict[str, Any]) -> None:
    """AD-7 / AD-14 : le niveau affiché s'accompagne du nombre de cas **réellement exécutés** et de
    la contresignature humaine de leur relecture. Sans elle, l'écran n'écrit pas « relus à la
    main » — et l'alerte du serveur voyage dans le même objet, donc elle se lit avec."""
    lignes = _rubriques(cas["vue_pourquoi_complet"])["Validation du document interrogé"]
    assert lignes[0]["texte"] == "profil de validation : vertical (2 cas)"
    assert lignes[1] == {"etat": "ko", "picto": {"aria-hidden": "true"},
                         "texte": "relecture des cas non contresignée : elle est celle de la "
                                  "boucle autonome"}
    assert lignes[2]["etat"] == "ko"
    assert lignes[2]["texte"].endswith("(gate_perime)")


def test_labsence_de_gate_nest_dite_quune_fois(cas: dict[str, Any]) -> None:
    lignes = _rubriques(cas["vue_pourquoi_sans_gate"])["Validation du document interrogé"]
    assert [ligne["texte"] for ligne in lignes] == [
        "aucune question-témoin ne valide ce document (sans_gate)"]


def test_une_alerte_de_gate_inconnue_se_dit_telle_quelle(cas: dict[str, Any]) -> None:
    lignes = _rubriques(cas["vue_pourquoi_alerte_inconnue"])["Validation du document interrogé"]
    assert [ligne["texte"] for ligne in lignes] == [
        "aucune question-témoin ne valide ce document", "alerte_de_demain"]


def test_lecran_ecrit_que_le_refus_zero_hit_est_desarme(cas: dict[str, Any]) -> None:
    """M12 / AC : « given un dictionnaire non signé, then l'écran écrit que le refus « zéro hit »
    est désarmé ». C'est un état **publié par le serveur** (`court_circuit_actif`), jamais recalculé
    par la page à partir des trois autres booléens."""
    desarme = _rubriques(cas["vue_pourquoi_complet"])["Dictionnaire des variantes"]
    assert [ligne["etat"] for ligne in desarme] == ["ok", "ko", "ok", "ko"]
    assert desarme[1]["texte"] == "signé par personne"
    assert desarme[-1]["texte"].startswith("le refus « zéro hit » est désarmé")

    arme = _rubriques(cas["vue_pourquoi_dictionnaire_arme"])["Dictionnaire des variantes"]
    assert [ligne["etat"] for ligne in arme] == ["ok", "ok", "ok", "ok"]
    assert arme[-1]["texte"].startswith("le refus « zéro hit » est armé")


def test_le_motif_du_desarment_du_dictionnaire_suit_les_drapeaux_reels(
        cas: dict[str, Any]) -> None:
    attendus = {
        "absent": "aucun dictionnaire n'est chargé",
        "perime": "le dictionnaire ne décrit pas le corpus servi",
        "non_signe": "le dictionnaire n'a pas de validation humaine",
    }
    for nom, motif in attendus.items():
        lignes = _rubriques(cas["pourquoi_dictionnaires_desarmes"][nom])[
            "Dictionnaire des variantes"]
        assert motif in lignes[-1]["texte"], nom


def test_une_deadline_negative_est_affichee_comme_depassee(cas: dict[str, Any]) -> None:
    lignes = _rubriques(cas["vue_pourquoi_deadline_depassee"])["Ce que la requête a coûté"]
    delai = [ligne["texte"] for ligne in lignes if "délai" in ligne["texte"]]
    assert delai == ["délai dépassé de 2,3 s"]
    assert "restant : -" not in delai[0]


def test_le_panneau_relie_lecran_a_la_ligne_de_log(cas: dict[str, Any]) -> None:
    lignes = [ligne["texte"] for ligne in _rubriques(cas["vue_pourquoi_complet"])["Cette requête"]]
    assert lignes == ["pipeline : guide · variante deterministe", "intention : question",
                      "référence de requête : r-trace"]


def test_une_trace_pauvre_ne_fait_apparaitre_aucune_rubrique_inventee(cas: dict[str, Any]) -> None:
    """M2 / AC : « given une trace privée d'un champ, then la rubrique correspondante est absente et
    aucune valeur n'est inventée ». Absence ≠ valeur : pas d'« inconnu », pas de ligne vide."""
    panneau = cas["vue_pourquoi_pauvre"]["pourquoi"]
    titres = [r["titre"] for r in panneau["rubriques"]]
    assert titres == ["Ce que la requête a coûté", "Cette requête"]
    for absent in ("Étapes", "Passages ouverts", "Passages écartés, non lus par le modèle",
                   "Contrôles", "Validation du document interrogé", "Dictionnaire des variantes"):
        assert absent not in titres
    assert panneau["seuils"] is None and panneau["rejetees"] == []
    # Aucune ligne vide, nulle part.
    for rubrique in panneau["rubriques"]:
        for ligne in rubrique["lignes"]:
            assert ligne["texte"].strip(), rubrique["titre"]


def test_un_gate_et_un_dictionnaire_nuls_ne_sont_pas_une_mesure(cas: dict[str, Any]) -> None:
    """`Trace.gate` et `Trace.dictionnaire` sont `… | None` : `null` veut dire « pas de mesure »
    (document hors manifest, pipeline sans dictionnaire), pas « mesure à zéro »."""
    titres = [r["titre"] for r in cas["vue_pourquoi_champs_nuls"]["pourquoi"]["rubriques"]]
    assert "Validation du document interrogé" not in titres
    assert "Dictionnaire des variantes" not in titres
    # `blocs: []` en revanche **ne fait pas** disparaître les passages : ce sont les étapes qui les
    # nomment, et `trace.blocs` ne fait que les résoudre. Ils s'affichent donc par leur seul
    # identifiant — dire moins reviendrait à cacher ce que le modèle a lu.
    rubriques = _rubriques(cas["vue_pourquoi_champs_nuls"])
    assert [ligne["texte"] for ligne in rubriques["Passages ouverts"]] == [
        "lux-guide:farrivee:2", "lux-guide:farrivee:3"]


def test_une_trace_qui_na_rien_a_dire_nouvre_pas_un_panneau_vide(cas: dict[str, Any]) -> None:
    """Un `<details>` qui n'a que son `<summary>` promet une explication qu'il ne donne pas."""
    assert cas["vue_pourquoi_muette"]["pourquoi"] is None


# --- M10 : une erreur tracée porte aussi le panneau ------------------------

def test_une_enveloppe_derreur_qui_porte_une_trace_porte_le_panneau(cas: dict[str, Any]) -> None:
    """AD-10 : « une erreur porte `trace` si le pipeline a commencé ». C'est l'écran qui a le plus
    besoin d'être expliqué — l'utilisateur n'a pas de réponse, il a le droit de savoir jusqu'où on
    est allé."""
    vu = cas["erreur_avec_trace"]
    assert (vu["kind"], vu["code"]) == ("indisponible", "llm_unavailable")
    assert vu["a_une_trace"] is True
    panneau = vu["vue"]["pourquoi"]
    assert panneau["summary"] == "Pourquoi cette réponse"
    assert [r["titre"] for r in panneau["rubriques"]][0] == "Étapes"
    # Le bandeau garde par ailleurs son bouton unique : le panneau n'ajoute aucune action.
    assert [a["nom"] for a in vu["vue"]["actions"]] == ["recherche_simple"]


def test_une_trace_derreur_qui_ne_tient_pas_le_contrat_ne_devient_pas_un_panneau(
        cas: dict[str, Any]) -> None:
    """Elle n'a pas franchi `lireReponse` : elle est relue par la même lecture stricte, et écartée
    si elle ne tient pas. L'échec reste celui du serveur (503) — il perd seulement son panneau."""
    vu = cas["erreur_trace_cassee"]
    assert vu["kind"] == "indisponible" and vu["code"] == "llm_unavailable"
    assert vu["a_une_trace"] is False
    assert vu["vue"]["pourquoi"] is None


def test_une_erreur_sans_trace_naffiche_aucun_panneau(cas: dict[str, Any]) -> None:
    assert cas["erreur_sans_trace"]["a_une_trace"] is False
    assert cas["erreur_sans_trace"]["vue"]["pourquoi"] is None


# --- M6 / M7 : le message, zéro action, le badge conservé ------------------

def test_un_429_dit_le_delai_et_ne_propose_rien(cas: dict[str, Any]) -> None:
    """M6 : « 429 + `Retry-After: 30` ⇒ message « réessayez dans 30 secondes », **zéro** action,
    badge `mode api` conservé »."""
    vu = cas["limite_de_debit"]
    assert vu["retry_after"] == 30
    assert vu["message"] == "Trop de questions en peu de temps : réessayez dans 30 secondes."
    assert vu["vue"]["actions"] == []
    # `modeApresErreur` rend `null` : le badge existant n'est pas touché — le serveur a répondu.
    assert vu["mode_apres"] is None


def test_retry_after_naccepte_que_les_secondes_entieres_ou_une_http_date_valide(
        cas: dict[str, Any]) -> None:
    assert cas["retry_after_strict"] == {
        "secondes": 30,
        "date": 30,
        "date_passee": 0,
        "suffixe": None,
        "decimal": None,
        "signe": None,
        "date_impossible": None,
    }


def test_un_4xx_affiche_un_message_lisible_et_zero_action(cas: dict[str, Any]) -> None:
    """M7 : le `message` du serveur est produit par pydantic, en anglais, avec le chemin du champ.
    Il n'est jamais affiché : la phrase se compose sur le `code` d'AD-16."""
    vu = cas["requete_refusee"]
    assert vu["message_du_serveur_affiche"] is False
    assert vu["message"].startswith("Le serveur a refusé la question")
    assert vu["vue"]["actions"] == []
    assert vu["mode_apres"] is None


# --- M9 : la phrase du retrait -------------------------------------------

def test_le_retrait_dun_tour_sans_reponse_est_dit_a_lecran(cas: dict[str, Any]) -> None:
    """« retrait jamais silencieux ». La phrase est composée par `chat.js` — comme tout ce qui
    s'affiche — et `ui.js` ne fait que dire ce qu'il a fait."""
    avec = cas["vue_erreur_avec_retrait"]
    assert avec["retrait"] == ("Cette question n'a pas reçu de réponse : elle est retirée de la "
                               "conversation, et ne repartira pas au serveur avec la suivante.")
    # Sans retrait, rien n'est écrit : la page n'annonce pas une modification qu'elle n'a pas faite.
    assert cas["vue_erreur_sans_retrait"]["retrait"] is None

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


# --- story 1.10 (D10) : le badge dit le niveau de validation du corpus servi ---
#
# Reprise différée de 1.7 : `testerApi()` lisait `gate_profile` et `alerts` sur `/sante` puis les
# jetait. Le badge disait « mode api » de la même façon que le corpus soit validé par des
# questions-témoins ou pas du tout — c'est-à-dire dans l'état où les réponses reposent sur des
# documents que rien n'a mesurés, la « bascule silencieuse » qu'AD-11 nomme, dans le seul écran où
# l'information était déjà là.

def test_le_badge_porte_le_profil_de_gate_et_son_nombre_de_cas(cas: dict[str, Any]) -> None:
    v = cas["validation"]
    assert v["contresigne"]["badge"]["texte"] == "mode api · vertical (2 cas)"
    # Le nombre vient du serveur : un gate à un cas s'écrit « 1 cas », pas « 2 ».
    assert v["un_cas"]["badge"]["texte"] == "mode api · vertical (1 cas)"
    assert v["gate"]["lue"]["gate_profile"] == "vertical" and v["gate"]["lue"]["gate_cases"] == 2


def test_le_badge_dit_que_la_contresignature_humaine_est_due(cas: dict[str, Any]) -> None:
    """AD-14 définit `vertical` par une relecture **à la main** (revue Codex 1.10 tour 2, B2).

    Le seul nom du profil affirme donc au lecteur du badge une relecture humaine. Tant que
    `gate_countersigned` est faux, cette relecture est celle de la boucle autonome : le badge pose la
    réserve, comme il pose déjà celle de la péremption, et l'accueil la dit en toutes lettres.
    """
    v = cas["validation"]
    assert v["gate"]["lue"]["gate_countersigned"] is False
    assert v["gate"]["badge"]["texte"] == "mode api · vertical (2 cas, non contresigné)"
    assert v["contresigne"]["lue"]["gate_countersigned"] is True
    assert v["contresigne"]["badge"]["texte"] == "mode api · vertical (2 cas)"
    # Les deux réserves cohabitent : aucune n'en efface une autre.
    assert v["perime_et_non_contresigne"]["badge"]["texte"] == \
        "mode api · vertical (2 cas, périmé, non contresigné)"
    assert v["perime_et_contresigne"]["badge"]["texte"] == "mode api · vertical (2 cas, périmé)"


def test_le_badge_dit_la_peremption_que_le_serveur_signale(cas: dict[str, Any]) -> None:
    """Le badge affirmait un niveau à jour que `/sante` démentait (revue 1.10).

    `gate_perime` veut dire que les empreintes du gate ne sont plus celles de l'image qui répond.
    L'accueil pose sa réserve depuis la revue ; le badge, lui, écrivait « vertical (2 cas) » — et
    c'est le seul écran où l'on pose réellement une question. `lireValidation()` collectait déjà les
    alertes, `suffixeValidation()` ne les regardait pas.
    """
    v = cas["validation"]
    assert v["gate_perime"]["badge"]["texte"] == \
        "mode api · vertical (2 cas, périmé, non contresigné)"
    # La péremption se trouve où qu'elle soit dans la liste — et depuis la story 2.5 elle n'est
    # plus la seule réserve lue : `source_absente` porte sur le même corpus servi (M14).
    assert v["perime_et_autres_alertes"]["badge"]["texte"] == \
        "mode api · vertical (2 cas, source absente, périmé, non contresigné)"
    # …et seule, sur un gate contresigné, elle s'écrit seule.
    assert v["perime_et_contresigne"]["badge"]["texte"] == "mode api · vertical (2 cas, périmé)"
    assert v["contresigne"]["badge"]["texte"] == "mode api · vertical (2 cas)"


# --- story 2.5 (M14) : le badge porte les réserves que le serveur publie ---

def test_le_badge_porte_la_quarantaine_et_la_source_absente(cas: dict[str, Any]) -> None:
    """Reprise différée de 1.10 : « le badge ne dit pas ce qu'il a lu des alertes ».

    `testerApi()` retenait `alerts` depuis 1.10 et le badge n'en disait qu'une, la péremption du
    gate. Or un document **en quarantaine** (il n'est plus servi) et un **fichier source absent**
    (l'édition annoncée n'est vérifiable nulle part) sont des faits publiés sur ce à quoi les
    réponses s'adossent, dans le seul écran où l'on pose une question. L'accueil les montre toutes ;
    le badge en porte désormais la réserve.
    """
    v = cas["validation"]
    assert v["quarantaine"]["badge"]["texte"] == \
        "mode api · vertical (2 cas, document écarté, non contresigné)"
    assert v["source_absente"]["badge"]["texte"] == \
        "mode api · vertical (2 cas, source absente, non contresigné)"
    # Deux réserves cohabitent, et aucune n'en efface une autre.
    assert v["quarantaine_et_source_absente"]["badge"]["texte"] == \
        "mode api · vertical (2 cas, document écarté, source absente)"


def test_lordre_des_reserves_ne_depend_pas_de_lordre_du_serveur(cas: dict[str, Any]) -> None:
    """Deux corps qui portent les mêmes alertes dans un ordre différent écrivent le même badge :
    sinon la phrase affichée dépendrait de l'ordre d'itération d'un dictionnaire côté serveur."""
    v = cas["validation"]
    assert v["alertes_dans_lautre_ordre"]["badge"]["texte"] == \
        v["quarantaine_et_source_absente"]["badge"]["texte"]


def test_une_alerte_inconnue_du_badge_ninvente_aucune_reserve(cas: dict[str, Any]) -> None:
    """AD-16 : ce que le serveur n'a pas dit, l'écran ne le dit pas. Une alerte hors table est lue
    (l'accueil la montre en clair) mais ne produit **aucune** réserve dans le badge, qui n'a que
    trois mots pour le dire."""
    v = cas["validation"]
    assert v["alerte_inconnue"]["badge"]["texte"] == "mode api · vertical (2 cas)"
    assert [a["alerte"] for a in v["alerte_inconnue"]["lue"]["alerts"]] == ["alerte_de_demain"]


def test_sans_gate_les_reserves_restent_dues(cas: dict[str, Any]) -> None:
    """« non validé » dit qu'aucun document n'est mesuré ; il ne dit pas qu'un document a été
    écarté au chargement. Les deux faits sont indépendants, et le second reste dû."""
    v = cas["validation"]
    assert v["sans_gate_mais_quarantaine"]["badge"]["texte"] == \
        "mode api · non validé (document écarté)"
    assert cas["validation"]["sans_gate"]["badge"]["texte"] == "mode api · non validé"


def test_le_badge_dit_non_valide_quand_aucun_document_nest_gate(cas: dict[str, Any]) -> None:
    v = cas["validation"]["sans_gate"]
    assert v["badge"]["texte"] == "mode api · non validé"
    assert [a["alerte"] for a in v["lue"]["alerts"]] == ["sans_gate"]


@pytest.mark.parametrize("corps", ["profil_absent", "profil_sans_compte",
                                   "gate_cases_fractionnaire", "gate_profile_vide",
                                   "contresignature_absente", "profil_sans_contresignature",
                                   "contresignature_non_booleenne"])
def test_un_corps_de_sante_illisible_ne_suffixe_rien(cas: dict[str, Any], corps: str) -> None:
    """Une clé absente n'est pas « le champ vaut null » (patron 1.9) : aucun niveau n'est inventé."""
    v = cas["validation"][corps]
    assert v["lue"] is None
    assert v["badge"]["texte"] == "mode api"


def test_une_alerte_mal_formee_ne_supprime_pas_le_niveau(cas: dict[str, Any]) -> None:
    """Les alertes ne conditionnent pas la lecture du niveau : le badge ne les affiche même pas.

    Les coupler faisait perdre tout le suffixe — un `gate_profile` parfaitement lisible — pour un
    champ qui n'atteint jamais l'écran. Ce qui n'est pas lisible est écarté, le reste est retenu.
    """
    for nom, releve in cas["alertes_tolerees"].items():
        assert releve["lue"] is not None, nom
        assert releve["badge"]["texte"] == "mode api · vertical (2 cas, non contresigné)", nom


def test_le_front_du_guide_et_laccueil_jugent_les_memes_corps_pareil(cas: dict[str, Any]) -> None:
    """Les deux lecteurs stricts du 200 de `/sante`, confrontés à la **même** table de corps.

    La version précédente de ce test grepait deux littéraux dans les deux sources. Elle serait restée
    verte pendant que l'accueil acceptait `gate_profile: ""` et un `gate_cases` négatif que le badge
    refusait — la divergence même qu'elle prétendait empêcher. Un grep sur du texte source ne vérifie
    pas une sémantique : `tests/js/sante_corpus.mjs` porte la table, les deux harnais la rejouent
    dans leur lecteur, et les verdicts sont comparés corps par corps.
    """
    accueil = _lancer(REPO_ROOT / "tests" / "js" / "accueil_cases.mjs")["corpus_partage"]
    guide = cas["corpus_partage"]
    assert set(accueil) == set(guide) and len(guide) >= 15

    desaccords = {nom: (guide[nom]["lisible"], accueil[nom]["lisible"])
                  for nom in guide if guide[nom]["lisible"] != accueil[nom]["lisible"]}
    assert not desaccords, f"le badge et l'accueil jugent différemment (badge, accueil) : {desaccords}"

    faux = {nom: v["lisible"] for nom, v in guide.items() if v["lisible"] != v["attendu"]}
    assert not faux, f"verdict contraire à la règle partagée : {faux}"

    # Et la table exerce bien les deux côtés de la règle, sans quoi « tout le monde d'accord »
    # pourrait vouloir dire « tout le monde refuse tout ».
    assert sum(1 for v in guide.values() if v["attendu"]) >= 5
    assert sum(1 for v in guide.values() if not v["attendu"]) >= 13


def test_une_sonde_morte_nannonce_aucun_niveau(cas: dict[str, Any]) -> None:
    v = cas["validation"]["sonde_morte"]
    assert v["lue"] is None
    assert v["badge"]["texte"] == "mode indisponible"
    assert v["badge_api"]["texte"] == "mode api"


def test_le_suffixe_ne_vaut_que_pour_le_mode_api(cas: dict[str, Any]) -> None:
    """« mode local · non validé » mêlerait deux choses sans rapport : le suffixe parle du corpus."""
    autres = cas["validation"]["autres_modes"]
    assert autres["local"]["texte"] == "mode local"
    assert autres["indisponible"]["texte"] == "mode indisponible"
    assert autres["avant_sonde"]["texte"] == "mode : vérification…"


def test_ui_pose_le_suffixe_dans_les_deux_surfaces(dom: dict[str, Any]) -> None:
    """`ui.js` ne lit rien : il passe à `libelleMode()` ce que `chat.js` a retenu de la sonde.

    Le défaut de 1.7 (revue Codex, B6) était précisément un badge composé et jamais posé dans le
    widget flottant : le suffixe doit atteindre les **deux** surfaces.
    """
    b = dom["badge_validation"]
    for surface in ("onglet", "widget"):
        assert b["avec_gate"][surface]["texte"] == "mode api · vertical (2 cas)"
        # La réserve de contresignature atteint elle aussi les deux surfaces (revue Codex 1.10
        # tour 2) : c'est le même défaut de 1.7 qui la guettait.
        assert b["non_contresigne"][surface]["texte"] == \
            "mode api · vertical (2 cas, non contresigné)"
        assert b["sans_gate"][surface]["texte"] == "mode api · non validé"
        assert b["sonde_morte"][surface]["texte"] == "mode api"


def test_le_niveau_de_validation_nest_pas_un_litteral_du_front() -> None:
    """Ni le profil ni le compte ne sont écrits dans `chat.js` : ils viennent de `/sante`."""
    code = "\n".join(l for l in (REPO_ROOT / "web" / "app" / "chat.js").read_text("utf-8").splitlines()
                     if not l.strip().startswith("//"))
    assert "vertical" not in code
    assert "gate_profile" in code and "gate_cases" in code


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



# --- Story 2.5 (M9) : la question sans réponse quitte l'historique ---------

def test_une_question_restee_sans_reponse_quitte_lhistorique(dom: dict[str, Any]) -> None:
    """Reprise différée de 2.2. `envoyer()` pousse le tour `user` **avant** l'appel et
    `afficherErreur()` ne poussait rien : après un 503, la question suivante partait avec deux tours
    `user` à la file, dont l'un n'avait jamais reçu de réponse — une conversation que *comprendre*
    devait interpréter alors qu'elle n'a pas eu lieu. `ChatRequest` ne valide que le **nombre** de
    tours, pas leur alternance : rien côté serveur ne pouvait rattraper cela.

    On exerce ici le chemin entier : une question qui échoue, puis une seconde qui aboutit.
    """
    vu = dom["tour_sans_reponse"]
    assert vu["apres_echec"]["historique"] == [], "le tour sans réponse est resté dans le fil"
    # Et la requête suivante ne porte pas deux tours `user` à la file.
    _, seconde = vu["corps_postes"]
    assert seconde["question"] == "Et pour l'école ?"
    roles = [t["role"] for t in seconde["historique"]]
    assert not any(roles[i] == roles[i + 1] == "user" for i in range(len(roles) - 1)), roles
    assert "Quel délai pour déclarer mon arrivée ?" not in [t["texte"] for t in seconde["historique"]]
    # Le fil repart proprement : la question suivante et sa réponse, et rien d'autre.
    assert vu["historique_final"] == [
        {"role": "user", "content": "Et pour l'école ?"},
        {"role": "assistant", "content": "Vous avez huit jours."}]


def test_le_retrait_du_tour_est_dit_dans_le_bandeau(dom: dict[str, Any]) -> None:
    """« retrait jamais silencieux » : la page a modifié la conversation, elle le dit."""
    apres = dom["tour_sans_reponse"]["apres_echec"]
    assert apres["retrait_peint"] == (
        "Cette question n'a pas reçu de réponse : elle est retirée de la conversation, et ne "
        "repartira pas au serveur avec la suivante.")
    assert apres["retrait_peint"] in apres["bandeau"]
    # Le bandeau reste celui de l'indisponibilité, avec son bouton unique — un par journal.
    assert apres["boutons_de_repli"] == 2
    assert apres["badges"]["onglet"]["texte"] == "mode indisponible"
    assert apres["badges"]["widget"]["texte"] == "mode indisponible"


def test_une_panne_reseau_retire_le_tour_comme_un_503(dom: dict[str, Any]) -> None:
    """M5 : « même bandeau, même bouton unique » — et le même retrait."""
    vu = dom["panne_reseau"]
    assert vu["bandeaux"] == 2 and vu["boutons_de_repli"] == 2
    assert vu["retrait_peint"] is not None
    assert vu["historique"] == []


# --- Story 2.5 (M4 / M8) : le repli, par le vrai chemin de la page ---------

def test_le_bandeau_dun_503_porte_un_bouton_par_journal_et_le_clic_seul_ouvre_le_local(
        dom: dict[str, Any]) -> None:
    """M4 puis M8, à travers `envoyer()` : le chemin que la page emprunte vraiment."""
    vu = dom["repli_par_envoyer"]
    assert vu["avant"]["bandeaux"] == 2
    assert vu["avant"]["boutons_de_repli"] == 2  # un par journal, et un seul dans chacun
    assert vu["avant"]["locales"] == 0, "le moteur lexical a produit une bulle sans qu'on clique"
    assert vu["avant"]["historique"] == 0, "le tour sans réponse est resté dans le fil"
    # Le clic, et lui seul.
    assert vu["apres"]["locales"] == 2
    assert vu["apres"]["boutons_de_repli_restants"] == 0, "la puce cliquée disparaît des deux côtés"
    assert vu["apres"]["badges"]["onglet"]["texte"] == "mode local"
    assert vu["apres"]["badges"]["widget"]["texte"] == "mode local"
    # Le tour local est marqué : il ne repartira pas au serveur comme la parole de l'assistant.
    assert vu["apres"]["historique"] == [{"role": "assistant", "local": True}]
    assert "aucune vérification" in vu["apres"]["texte_local"]


@pytest.mark.parametrize("nom", ["erreur_limite", "erreur_refusee"])
def test_un_429_ou_un_4xx_conserve_le_badge_et_nouvre_aucune_porte(dom: dict[str, Any],
                                                                   nom: str) -> None:
    """M6 / M7 : « badge `mode api` conservé », « zéro action ». Le serveur a répondu — il a refusé
    la requête —, et le mode n'est donc pas devenu indisponible."""
    vu = dom[nom]
    assert vu["badge_avant"]["onglet"]["texte"].startswith("mode api")
    assert vu["badge_apres"] == vu["badge_avant"], "le badge a changé alors que le serveur a répondu"
    assert vu["boutons_de_repli"] == 0
    assert vu["boutons_dans_les_journaux"] == 0
    assert vu["texte"], "le message d'erreur n'est pas peint"
    # Là aussi, la question sans réponse quitte le fil : un 4xx n'a pas eu de réponse non plus.
    assert vu["historique"] == []


def test_le_429_affiche_le_delai_de_retry_after(dom: dict[str, Any]) -> None:
    assert "réessayez dans 30 secondes" in dom["erreur_limite"]["texte"]


# --- Story 2.5 (M1) : le panneau est matérialisé dans les deux journaux ----

def test_le_panneau_pourquoi_est_peint_dans_les_deux_journaux(dom: dict[str, Any]) -> None:
    """Le défaut de 1.7 (B6) était un badge composé et jamais posé dans le widget flottant : le
    panneau ne doit pas le rejouer. Un `<details>` par journal, replié, sans `id`."""
    vu = dom["panneau"]
    assert vu["panneaux"] == 2
    assert vu["summary"] == "Pourquoi cette réponse"
    # Replié par défaut : la trace explique la réponse, elle ne la précède pas.
    assert vu["attribut_open"] == [None, None]
    # Deux `<details>` dans une bulle : le panneau et son sous-panneau de seuils.
    assert vu["details_dans_la_bulle"] == 2
    assert vu["ids"] == 0, "un `id` dans un arbre peint deux fois rend le document invalide"


def test_le_pictogramme_dun_controle_est_pose_par_setattribute(dom: dict[str, Any]) -> None:
    """`materialiser()` pose `vue.attrs` par `setAttribute` : c'est la seule voie, et le DOM minimal
    ne modélise rien d'autre. Sans elle, la branche décrite par `chat.js` serait inatteignable."""
    pictos = dom["panneau"]["pictos"]
    assert pictos, "aucun pictogramme de contrôle matérialisé"
    for picto in pictos:
        assert picto["aria"] == "true"
        assert picto["texte"] in ("✓", "✗")


def test_le_panneau_materialise_porte_bien_ce_que_la_trace_disait(dom: dict[str, Any]) -> None:
    texte = dom["panneau"]["texte"]
    for attendu in ("Pourquoi cette réponse", "retrouver · reason · 3480 ms",
                    "b1 — Les huit premiers jours", "citations relues dans le corpus",
                    "profil de validation : vertical (2 cas)",
                    "le refus « zéro hit » est désarmé", "Seuils actifs (1)"):
        assert attendu in texte, attendu

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
           "du modèle (Anthropic) : sa politique publique, lue le 25/08/2026, prévoit la suppression "
           "des entrées et des sorties de l'API sous 30 jours, avec des exceptions — un service à "
           "rétention plus longue que vous contrôlez, un accord de rétention différent conclu avec "
           "lui, l'application de sa politique d'usage, une obligation légale — et le contenu que "
           "ses systèmes de sécurité signalent est conservé jusqu'à deux ans. "
           "Aucune conversation n'est enregistrée : ni par le serveur de ce site, ni dans ce "
           "navigateur, qui ne garde que votre profil et vos préférences d'affichage.")

# Les réserves que la politique lue le 25/08/2026 oppose aux 30 jours, et qu'AD-15 exige de dire —
# « accord de rétention différent » y est nommément écrit. En taire une rend la promesse du site plus
# généreuse que l'engagement du tiers qui exécute la requête : la « promesse contredite en silence »
# que cet AD prévient. Les deux fronts doivent les porter toutes (revue Codex 1.11).
RESERVES = ("service à rétention plus longue que vous contrôlez",
            "accord de rétention différent",
            "politique d'usage",
            "obligation légale",
            "deux ans")

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
    # La **forme** de la date, pas sa valeur : elle bouge à chaque relecture de la politique du
    # fournisseur (24/08 puis 25/08/2026), et l'épingler deux fois dans ce fichier en ferait un
    # second texte à mettre à jour — celui qu'on oublie. `MENTION` en est l'unique autorité ici, et
    # les assertions ci-dessus la confrontent au HTML et au README.
    assert re.search(r"lue le \d{2}/\d{2}/\d{4}", MENTION), (
        "la citation d'une politique de tiers porte la date à laquelle elle a été lue")
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
    for morceau in ("sous 30 jours", "avec des exceptions", *RESERVES):
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
    # Le champ `contexte` du corps envoyé, et lui seul : la borne du mot évite qu'une clé qui se
    # **termine** par `contexte` (un libellé de contrôle, story 4.2e) fasse rougir la garde pour un
    # nom, alors que la règle porte sur ce que la page envoie au serveur.
    assert re.search(r"(?<![A-Za-z0-9_])contexte:", chat) is None


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


def test_un_200_dont_le_corps_ne_vient_jamais_est_abandonne(cas: dict[str, Any]) -> None:
    """Revue Codex 1.10, I1 : la borne doit couvrir le **corps**, pas seulement les en-têtes.

    `finir()` coupait la minuterie dès la réception des en-têtes. Un serveur qui envoie un 200 puis
    bloque sur le corps laissait donc la sonde pendre sans abandon armé — et la première question
    attend la sonde (`reponseApi`) : la saisie restait verrouillée sans fin, contre la borne que la
    page annonce (AD-11/AD-16). La question `POST /chat` portait le même défaut.
    """
    assert cas["sonde_corps_qui_pend_minuteur_arme"] == 1, \
        "l'abandon de la sonde n'est plus armé pendant la lecture du corps"
    assert cas["sonde_corps_qui_pend"] is False  # sonde en échec, jamais un mode api inventé
    assert cas["question_corps_qui_pend_minuteur_arme"] == 1
    # Un corps abandonné n'est pas un corps illisible : c'est le serveur qui n'a pas répondu à temps.
    assert cas["question_corps_qui_pend"] == {"kind": "indisponible", "code": "timeout_client"}


def test_toutes_les_fonctions_pures_de_la_story_sont_exportees(cas: dict[str, Any]) -> None:
    attendues = {"historiquePourApi", "citationsParSegment", "statutTexte", "preuveAbsence",
                 "coutTexte", "etatReponse", "phraseEtat", "messageErreur", "rechercheSimple"}
    assert attendues <= set(cas["exporte"])


def test_le_harnais_vit_dans_le_depot_et_ne_depend_de_rien() -> None:
    """Aucune dépendance ajoutée : le harnais n'utilise que `node:fs`, `node:path`, `node:vm`."""
    source = Path(HARNAIS).read_text("utf-8")
    imports = {ligne.split('"')[1] for ligne in source.splitlines()
               if ligne.startswith("import ") and '"' in ligne}
    # `./sante_corpus.mjs` est la table de corps partagée avec le harnais de l'accueil : un module du
    # dépôt, pas une dépendance — c'est précisément ce qui permet aux deux lecteurs d'être confrontés
    # aux mêmes corps sans que rien ne s'installe.
    assert imports <= {"node:fs", "node:path", "node:url", "node:vm", "./sante_corpus.mjs"}, imports


# --- story 4.2f : la lecture partielle, lue puis peinte -------------------

def test_une_lecture_partielle_est_lue_comme_une_reponse(cas: dict[str, Any]) -> None:
    """AC : le corps servi sur ce chemin est **peint**, jamais traité comme une indisponibilité.

    Avant la story, la page recevait un 503 et affichait « L'assistant est indisponible pour le
    moment. » avec son bouton de repli — alors que rien n'était en panne. Ici : la phrase du serveur,
    le chiffre de ce qui a été lu, la lacune, et zéro lecture du moteur lexical (donc aucun repli
    ouvert, même en coulisses).
    """
    vu = cas["lecture_partielle"]
    assert vu["segments_kind"] == ["limite"] and vu["sources"] == 0
    assert vu["etat"] == {"cle": "lecture-partielle", "texte": "lecture partielle"}
    # Un porteur, et un seul : la preuve chiffrée d'un refus n'a rien à dire ici.
    assert vu["preuve"] == ""
    assert vu["lecture"] == ("Lecture partielle : 2 sections lues, 5 passages transmis au modèle "
                             "— le reste n'a pas été lu, et rien n'en est affirmé")
    assert vu["unknown"] and vu["rejetees"] == ["non_retrouvee"]
    assert vu["lectures_du_moteur_lexical"] == 0


def test_letat_de_lecture_partielle_ne_se_confond_pas_avec_inconnu(cas: dict[str, Any]) -> None:
    """« inconnu » est un refus fondé sur une recherche menée à son terme ; « lecture partielle » dit
    que la lecture s'est arrêtée avant de conclure. Un seul badge pour les deux redonnerait à
    l'utilisateur l'exhaustivité que la troncature dément."""
    etats = cas["etats_lecture_partielle"]
    assert etats["porteur"]["texte"] == "lecture partielle"
    assert etats["sans_porteur"]["texte"] == "inconnu"
    phrases = cas["phrases_lecture_partielle"]
    assert phrases["avec_liste"].startswith("ma lecture s'est arrêtée avant de conclure")
    assert "Ce que je ne sais pas" in phrases["avec_liste"]
    assert "Ce que je ne sais pas" not in phrases["sans_liste"]
    # La phrase ne décrit que ce que la vue a **peint** : elle ne promet le chiffre « ci-dessus »
    # que s'il y est, comme les trois autres états le font pour la liste et pour la preuve.
    assert "chiffré ci-dessus" in phrases["sans_liste"]
    assert "chiffré ci-dessus" not in phrases["sans_chiffre"]
    assert "Ce que je ne sais pas" in phrases["sans_chiffre"]
    assert "chiffré ci-dessus" not in phrases["sans_rien"]
    assert "Ce que je ne sais pas" not in phrases["sans_rien"]
    # Aucune des quatre phrases n'affirme une absence ni une panne.
    assert len(set(phrases.values())) == 4
    for phrase in phrases.values():
        assert "indisponible" not in phrase and "rien n'a été retenu" not in phrase


def test_les_compteurs_lus_saffichent_meme_a_zero(cas: dict[str, Any]) -> None:
    """Comme la preuve chiffrée d'un refus : « la navigation n'a rien fait entrer » et « douze
    passages sont partis au modèle » sont deux situations différentes, que l'omission confondrait."""
    textes = cas["lecture_textes"]
    assert textes["pluriel"].startswith("Lecture partielle : 3 sections lues, 12 passages")
    assert textes["singulier"].startswith("Lecture partielle : 1 section lue, 1 passage")
    # `blocks_read` a un plancher à 1 dans le domaine — zéro bloc transmis est un `BudgetExceeded`
    # terminal, pas une lecture partielle. `nodes_read`, lui, reste `ge=0` : la page l'affiche tel
    # quel plutôt que d'inventer un nombre.
    # Le plancher légal des deux compteurs, et non une valeur que le domaine refuse : un cas de
    # rendu sur « 0 section lue » bénirait exactement ce que l'invariant interdit.
    assert textes["plancher"] == textes["singulier"]
    assert textes["absente"] == ""


def test_la_vue_dune_lecture_partielle_montre_le_chiffre_et_les_affirmations_ecartees(
        cas: dict[str, Any]) -> None:
    """AC : « une réponse partielle chiffrée avec ses affirmations écartées, sans message
    d'indisponibilité, sans bouton de repli et sans état sûr »."""
    vue = cas["vue_lecture_partielle"]
    assert vue["ordre_des_blocs"] == ["seg", "lecture-partielle", "inconnu", "pied", "pourquoi"]
    assert vue["etat"] == "etat etat-lecture-partielle"
    assert vue["etat_texte"] == "lecture partielle"
    assert vue["preuve"] is None  # aucune absence n'est affirmée
    assert vue["inconnus"] and vue["actions"] == []  # aucun bouton de repli
    assert vue["pied"] == ["etat etat-lecture-partielle", "etat-phrase", "cout"]
    # Les affirmations écartées deviennent enfin visibles : le bloc était écrit, jamais atteint.
    assert [r["texte"] for r in vue["pourquoi"]["rejetees"]] == [
        "Une affirmation que la vérification a écartée."]
    # Et la citation d'une claim écartée n'apparaît nulle part (AD-3/AD-11).
    assert "CETTE QUOTE" not in json.dumps(vue, ensure_ascii=False)


def test_un_corps_de_lecture_partielle_mal_forme_nest_pas_peint(cas: dict[str, Any]) -> None:
    """Un compteur en chaîne est accepté par pydantic (lecture permissive) mais **affiché** tel quel
    par la page : c'est elle qui doit le refuser, comme pour les compteurs de la preuve d'absence."""
    vu = cas["contrat_incomplet"]["answer_lecture_compteur_chaine"]
    assert vu["a_repondu"] is False
    assert (vu["kind"], vu["code"]) == ("requete", "reponse_illisible")
    assert vu["lectures_du_moteur_lexical"] == 0
