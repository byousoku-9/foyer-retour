"""Matrice d'E/S du front de l'accueil (spec 1.10) : AD-11, AD-12, AD-15, AD-16, D8, D9, UX-DR9.

Le harnais `tests/js/accueil_cases.mjs` charge `tools/accueil/accueil.js` dans un `node:vm` avec
`window`, `location`, `document` (le DOM minimal) et `fetch` **doublés**, exécute les cas et écrit du
JSON. Il ne juge rien : tout ce qui est affirmé l'est ici. Aucun navigateur, aucun réseau, aucune
dépendance ajoutée à `pyproject.toml`.

Ce que ces tests protègent, en une phrase : **la page n'invente aucun niveau de validation**. Ni
quand la sonde échoue, ni quand elle répond quelque chose que cette page ne sait pas lire.
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

from server.app.api.schemas import EtatDictionnaire, SanteResponse
from server.app.config import REPO_ROOT, Settings

HARNAIS = REPO_ROOT / "tests" / "js" / "accueil_cases.mjs"
PAGE = REPO_ROOT / "tools" / "accueil" / "index.html"
SCRIPT = REPO_ROOT / "tools" / "accueil" / "accueil.js"

# Comme pour les deux autres fronts : sans `node`, ces cas passent en `skip`, et un `skip` est
# indiscernable d'un succès dans un `pytest -q`. `FRONT_TESTS_REQUIS=1` (CI, story 1.11) en fait un
# échec.
REQUIS = os.environ.get("FRONT_TESTS_REQUIS", "") not in ("", "0")


def _lancer(harnais: Path) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        motif = ("node absent de la machine : les cas du front d'accueil ne peuvent pas tourner "
                 "(l'image de CI de la story 1.11 en a un)")
        if REQUIS:
            pytest.fail("FRONT_TESTS_REQUIS=1 mais " + motif)
        pytest.skip(motif)
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
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code() -> str:
    """Le script **sans ses commentaires** : ce qui s'exécute, et non ce qui l'explique.

    Sans cela, un test qui interdit un littéral (« vertical », « localStorage ») interdirait aussi
    d'expliquer pourquoi il est interdit — et la seule façon de le faire passer serait de retirer le
    commentaire, ce qui n'apprend rien à personne.
    """
    lignes = []
    for ligne in SCRIPT.read_text(encoding="utf-8").splitlines():
        nu = ligne.strip()
        if nu.startswith("//") or nu.startswith("*") or nu.startswith("/**") or nu.startswith("/*"):
            continue
        lignes.append(ligne)
    return "\n".join(lignes)


def _textes(dom: dict[str, Any]) -> list[str]:
    out = []
    if dom.get("texte"):
        out.append(dom["texte"])
    for e in dom.get("enfants", []):
        out += _textes(e)
    return out


# --- AD-12 : une seule origine, une seule route sondée --------------------

def test_lapi_est_lorigine_de_la_page(cas: dict[str, Any]) -> None:
    assert cas["api_base"] == "https://foyer-retour.example"
    assert cas["sonde_url"] == "https://foyer-retour.example/api/v1/sante"
    assert cas["sonde_methode"] == "GET"


def test_la_page_ne_sonde_que_deux_routes_gratuites(cas: dict[str, Any]) -> None:
    """Deux routes, et **seulement** deux : `/sante` et `/evals/latest` (story 4.5, FR41/FR42).

    Toutes deux sont calculées au démarrage du serveur, ne coûtent rien et ne sont pas limitées —
    AD-13 protège les routes qui appellent un modèle, pas celles qui disent où en est le service.
    L'ajout de la seconde est ce qui permet à `/` de reprendre le **même artefact** que
    `docs/evals/latest.md` au lieu d'écrire des chiffres qui lui seraient propres.

    Une sonde par bloc : `sonder()` seule ne demande toujours qu'une chose.
    """
    assert cas["appels_au_demarrage"] == 1
    assert cas["routes"] == {"sante": "/api/v1/sante", "evals": "/api/v1/evals/latest"}
    assert cas["demarrage"]["appels"] == [
        "https://foyer-retour.example/api/v1/evals/latest",
        "https://foyer-retour.example/api/v1/sante",
    ]


# --- FR41 / FR42 : les résultats publiés, repris tels quels ----------------


def test_la_page_reprend_les_chiffres_publies_sans_en_recalculer_un(cas: dict[str, Any]) -> None:
    """AC 4 : `/` compose le **même artefact** que les trois autres surfaces, sans arithmétique.

    Chaque chiffre affiché est celui du corps servi, à l'octet près : un recall recalculé, une
    moyenne redérivée ou une limite rédigée ici seraient une affirmation que le serveur n'a pas
    faite — la classe d'invention que D8 et AD-16 interdisent.
    """
    textes = cas["evals_publie"]["textes"]
    assert cas["evals_publie"]["url"] == "https://foyer-retour.example/api/v1/evals/latest"
    assert cas["evals_publie"]["methode"] == "GET"
    attendus = [
        "résultats des questions-témoins : gate rouge — profil full",
        # Les chiffres passent par le **formatage partagé** avec `publication.nombre` : la
        # fixture porte volontairement des valeurs qui ne tombent pas sur quatre décimales
        # (`1.0`, `0.055`, `0.02`, `0`), et la page doit les rendre comme le Markdown (revue P5).
        "rappel : 1.0000",
        "stabilité : 1/2 cas stables sur N=3 répétitions",
        "coût : 0.1649 € froid, 0.0550 € en moyenne par exécution, 0.0200 € au p95",
        "latence : 14243 ms p50, 34370 ms p95",
        "taux de ne_tranche_pas : 0.0000",
        "labels : bonne_reponse ×3, parsing ×1",
        "variantes : local ×2, outils ×3",
        "cases_hash : " + "d" * 64,
        "run_digest : " + "a" * 64,
        "révision candidate : " + "1" * 40,
    ]
    for attendu in attendus:
        assert attendu in textes, attendu
    # Les trois réserves de l'AC 3, lisibles sur `/` — et toutes fausses, ce qui est l'état du dépôt.
    assert ("réserves — contresignature humaine : non ; validation par un expert assurance : non ; "
            "dictionnaire des variantes validé : non") in textes
    # Les limites viennent du runner, mot pour mot : la page ne les compose pas.
    assert any(t.startswith("décision rouge stabilite_sinistre") for t in textes)


def test_un_gate_rouge_est_publie_comme_les_autres(cas: dict[str, Any], code: str) -> None:
    """FR41 : publier n'est pas promouvoir. Le cas nominal du harnais **est** un gate rouge."""
    assert cas["evals_publie"]["lu"]["publication"]["evals_ok"] is False
    assert any("Publier ne promeut rien" in t for t in cas["evals_publie"]["textes"])
    # La page ne décide de rien : aucune mention de promotion, aucun `evals_ok` recalculé.
    assert "evals_ok" in code


def test_aucun_run_publie_est_rendu_comme_une_absence(cas: dict[str, Any]) -> None:
    """AC : « `GET /api/v1/evals/latest` rend un état typé `publie: false` ; `/` le dit ».

    Les trois raisons du serveur sont traduites, une raison inconnue est affichée telle quelle
    (la taire serait pire que la montrer brute), et **aucun chiffre** n'est peint à la place.
    """
    attendus = {
        "absent": "aucun run n'a encore été publié dans cette image",
        # P14 : `illisible` nomme les deux causes qu'il couvre réellement — octets illisibles
        # **ou** JSON invalide —, `hors_schema` la troisième. Le serveur les distingue vraiment
        # (`json.loads` avant la validation), donc la page peut les dire séparément.
        "illisible": "ne se lit pas (octets ou JSON invalides)",
        "hors_schema": "le fichier de résultats ne correspond pas au format que ce serveur sait lire",
        "motif_inconnu": "motif_inconnu",
        # `raison: null` reste un état publié : « aucun run », sans motif à donner.
        "null": "aucun run publié",
    }
    for raison, phrase in attendus.items():
        releve = cas["evals_absent"][raison]
        assert releve["lu"]["publie"] is False
        textes = releve["textes"]
        assert "résultats des questions-témoins : aucun run publié" in textes
        assert any(phrase in t for t in textes), (raison, textes)
        assert "Aucun chiffre n'est affiché à la place." in textes
        assert not any("rappel :" in t or "coût :" in t for t in textes)


def test_une_sonde_de_resultats_morte_nest_pas_une_absence_de_run(cas: dict[str, Any]) -> None:
    """AD-16 : « le serveur n'a pas répondu » et « aucun run publié » ne sont pas la même phrase.

    Les confondre ferait annoncer une absence de mesure à partir d'une panne réseau — et
    réciproquement, une panne ferait disparaître un run qui existe.
    """
    releve = cas["evals_sonde_morte"]
    assert releve["motif"] == "http_503"
    textes = releve["textes"]
    assert "résultats des questions-témoins : inconnus (le serveur n'a pas répondu)" in textes
    assert not any("aucun run publié" in t for t in textes)
    assert not any("rappel :" in t for t in textes)
    # Le **motif réel** est propagé, comme il l'est déjà pour `/sante` : trois causes, trois
    # phrases, trois correctifs. Les rabattre sur un message unique faisait disparaître
    # l'information au moment précis où elle sert.
    assert any("503" in t for t in textes)
    assert any("fichier local" in t for t in releve["textes_hors_ligne"])
    assert any("pas celle que cette page sait lire" in t for t in releve["textes_illisible"])
    assert releve["textes"] != releve["textes_sans_motif"]


def test_une_sonde_morte_neffe_pas_ce_que_lautre_a_etabli(cas: dict[str, Any]) -> None:
    """Deux sondes indépendantes : l'échec de l'une ne doit pas effacer le résultat de l'autre."""
    releve = cas["demarrage_evals_mort"]
    assert any("niveau de validation : vertical" in t for t in releve["textes_etat"])
    assert any("inconnus (le serveur n'a pas répondu)" in t for t in releve["textes_evals"])


def test_le_lecteur_des_resultats_refuse_tout_corps_quaucune_route_na_ecrit(
        cas: dict[str, Any]) -> None:
    """Même règle que `lireSante` : une clé absente n'est pas « le champ vaut zéro »."""
    releve = cas["evals_illisibles"]
    assert releve.pop("nominal_lisible") is True
    faux = sorted(nom for nom, refuse in releve.items() if not refuse)
    assert not faux, f"ces corps devraient être refusés : {faux}"


def test_une_page_hors_ligne_ne_sonde_rien(cas: dict[str, Any]) -> None:
    """`file://` : aucun serveur à joindre, donc aucune requête — et la page le dit."""
    assert cas["hors_ligne"]["api_base"] == ""
    assert cas["hors_ligne"]["appels_reseau"] == 0
    assert cas["hors_ligne"]["motif"] == "hors_ligne"
    assert any("fichier local" in t for t in cas["hors_ligne"]["textes"])


# --- D8, état 1 : la sonde répond avec un profil --------------------------

def test_le_niveau_de_validation_vient_du_serveur(cas: dict[str, Any]) -> None:
    """AC : « niveau de validation : vertical — 2 cas relus à la main », lu sur `/api/v1/sante`.

    La phrase de l'AC est celle de l'état que l'AC décrit : deux cas dont la relecture a été
    **contresignée**. Le serveur le dit par `gate_countersigned` (amendement AD-7 / AD-14, revue
    Codex 1.10 tour 2, B2) ; le cas `nominal` du harnais est l'état du dépôt aujourd'hui, où la
    contresignature est due — voir `test_un_gate_non_contresigne_ne_dit_pas_relus_a_la_main`.
    """
    assert cas["contresigne"]["validation"] == {
        "etat": "gate",
        "contresigne": True,
        "texte": "niveau de validation : vertical — 2 cas relus à la main"}
    assert "niveau de validation : vertical — 2 cas relus à la main" in cas["contresigne"]["textes"]
    textes = cas["nominal"]["textes"]
    assert any("documents servis : axa-lu-optihome-2017, lux-guide" == t for t in textes)
    assert any("version servie : b79ca1b" == t for t in textes)


def test_un_gate_non_contresigne_ne_dit_pas_relus_a_la_main(cas: dict[str, Any],
                                                            code: str) -> None:
    """AD-14 / AC : `vertical` = « relus à la main », et la page l'affirme **publiquement**.

    Revue Codex 1.10 tour 2, B2 : la relecture qui fonde les deux cas du dépôt a été faite par la
    boucle autonome, et la contresignature de la personne à qui `epics.md` l'attribue (entrée humaine
    **bloquante**) reste due. La page écrivait pourtant « 2 cas relus à la main » sur la seule foi du
    nom du profil — une affirmation de relecture humaine que rien dans le dépôt n'établissait, c'est
    la classe d'invention qu'AD-16 interdit et que cette story combat.

    La qualification vient donc de `gate_countersigned`, pas du profil. Et elle ne se code pas dans
    la page : le mot « contresignature » n'y est écrit qu'une fois, dans la phrase de réserve.
    """
    validation = cas["nominal"]["validation"]
    assert validation["contresigne"] is False
    assert validation["texte"] == (
        "niveau de validation : vertical — 2 cas relus par la boucle, "
        "contresignature humaine en attente")
    assert "à la main" not in validation["texte"]
    # Le pluriel suit le compte du serveur des deux côtés de la bascule.
    assert cas["un_seul_cas"]["texte"] == (
        "niveau de validation : vertical — 1 cas relu par la boucle, "
        "contresignature humaine en attente")
    assert cas["un_seul_cas_contresigne"]["texte"] == \
        "niveau de validation : vertical — 1 cas relu à la main"
    # La bascule est lue sur le corps, jamais déduite d'autre chose.
    assert "gate_countersigned" in code


def test_le_readme_ne_decrit_pas_une_relecture_que_les_gates_livres_nattestent_pas() -> None:
    """AD-16 : rien n'affirme ce que rien n'établit — et le README est une surface publique.

    Revue Codex 1.10 tour 3, B2 : le tour 2 avait retiré « relus à la main » des deux fronts, mais la
    prose du dépôt décrivait toujours l'état contresigné comme le comportement courant (« la sonde
    répond avec un profil (“niveau de validation : vertical — N cas relus à la main”) »), alors
    qu'aucun cas du dépôt n'est contresigné. Le symptôme cité par la revue avait disparu de la page ;
    la même affirmation subsistait dans le fichier que lit qui n'ouvre pas la page.

    Le garde-fou n'est pas un `grep` figé : il **relit `data/manifest.json`**. Tant qu'un gate livré
    porte `countersigned: false`, le README ne décrit pas la phrase de l'état contresigné comme celle
    que la sonde compose. La contresignature portée dans les deux cas et les gates rejoués, le test
    se tait de lui-même et le README pourra reprendre la phrase de l'AC.

    Portée : `README.md` seul. `docs/tests-live.md` est un journal daté de runs — il consigne ce qui
    a été relevé à une date, et le réécrire serait la falsification inverse.
    """
    gates = [entree["gate"] for entree in
             json.loads((REPO_ROOT / "data" / "manifest.json").read_text("utf-8")).values()
             if isinstance(entree, dict) and entree.get("gate")]
    assert gates, "le dépôt livre au moins un gate depuis la story 1.10"
    if all(g.get("countersigned") for g in gates):
        pytest.skip("tous les gates livrés sont contresignés : le README peut le dire")
    motif = re.compile(r"niveau de validation\s*:[^»]*\brelus?\s+à\s+la\s+main")
    fautes = [(n, ligne) for n, ligne in
              enumerate((REPO_ROOT / "README.md").read_text("utf-8").splitlines(), 1)
              if motif.search(ligne)]
    assert not fautes, (
        "le README décrit « relus à la main » comme la phrase affichée, alors que "
        f"{sum(1 for g in gates if not g.get('countersigned'))} gate(s) livré(s) portent "
        f"countersigned: false : {fautes}")


def test_le_compte_de_cas_nest_pas_ecrit_dans_la_page(cas: dict[str, Any], code: str,
                                                      page: str) -> None:
    """D8 : « `gate_cases` vient du serveur ; “2 cas” n'est écrit nulle part dans la page ».

    Un serveur qui publie `gate_cases: 1` doit faire écrire « 1 cas relu », singulier compris —
    c'est la preuve que le nombre n'est pas un littéral déguisé.
    """
    assert cas["un_seul_cas_contresigne"]["texte"] == \
        "niveau de validation : vertical — 1 cas relu à la main"
    for littéral in ("2 cas", "vertical —", "niveau de validation : vertical"):
        assert littéral not in page, f"{littéral!r} est écrit en dur dans la page"
    # `vertical` apparaît une fois dans le code, et une seule : la qualification « relus à la main »
    # n'est vraie que de ce profil (AD-14), et la page ne doit pas la promettre sous un `full`. Le
    # profil affiché, lui, vient toujours du serveur — jamais du script.
    assert code.count('"vertical"') == 1, "le profil de gate ne doit servir qu'à un test, pas à un affichage"
    assert 'gate_profile + " — "' in code or "gate_profile +" in code, \
        "le profil affiché doit venir du corps de la sonde"


def test_un_profil_ne_promet_une_relecture_humaine_que_sil_en_est_une(cas: dict[str, Any]) -> None:
    """AD-14 : `vertical` = « relus à la main ». `full` (story 4.1) ne promet aucune relecture.

    Écrire « relus à la main » sous un gate `full` ferait affirmer à la page une relecture humaine
    qui n'a pas eu lieu — la classe d'invention que D8 interdit. Le profil vient du serveur ; la
    qualification n'est écrite que là où elle est vraie.
    """
    assert cas["profil_full"]["texte"] == "niveau de validation : full — 47 cas"
    assert "à la main" not in cas["profil_full"]["texte"]


def test_un_gate_perime_nest_pas_annonce_comme_a_jour(cas: dict[str, Any]) -> None:
    """AD-7 laisse un gate périmé servir le document ; il n'autorise pas à le dire à jour.

    La page affirmait « il porte les empreintes du corpus, du code et des prompts qui l'ont obtenu »
    — précisément ce que `gate_perime` dément. Le niveau reste affiché (le serveur le publie), avec
    sa réserve.
    """
    assert cas["gate_perime"]["perime"] is True
    textes = cas["gate_perime"]["textes"]
    assert any("Réserve" in t and "autre code" in t for t in textes)
    assert not any("ne signale aucun écart" in t for t in textes)
    # Et le nominal dit bien l'inverse, sinon la réserve ne distinguerait rien.
    assert any("ne signale aucun écart" in t for t in cas["nominal"]["textes"])


def test_un_service_degrade_nest_pas_peint_comme_un_service_sain(cas: dict[str, Any]) -> None:
    """`ok: false` était lu par `lireSante` puis jeté : le guide en quarantaine s'affichait normal."""
    assert any("l'assistant du guide n'est pas disponible" in t for t in cas["pas_ok"]["textes"])
    assert not any("n'est pas disponible" in t for t in cas["nominal"]["textes"])


def test_une_raison_de_quarantaine_est_traduite_la_ou_elle_arrive(cas: dict[str, Any]) -> None:
    """`gate_echoue` et `bloquant_statique` ne sont pas des **noms** d'alerte.

    `corpus/loader.py` les calcule comme raisons de quarantaine et `api/etat._alertes` les publie
    sous `alerte: "quarantaine"`, la raison dans `detail`. Les mettre dans la table des noms
    d'alerte en faisait du code mort : la page n'en aurait traduit aucun.
    """
    textes = cas["alertes"]["textes"]
    assert any("contrôle bloquant du rapport d'ingestion" in t and "page_sans_texte" in t
               for t in textes)
    assert any("questions-témoins de ce document ont échoué" in t for t in textes)


def test_les_alertes_du_serveur_sont_affichees_telles_quelles(cas: dict[str, Any]) -> None:
    """AD-7 : une alerte est visible, jamais muette — y compris une alerte que le front ne connaît pas."""
    textes = cas["alertes"]["textes"]
    assert "Alertes du serveur" in textes
    assert any("gate_perime" in t and "lux-guide" in t for t in textes)
    # `doc_id="*"` : une propriété du service, pas d'un document — pas de préfixe « * : ».
    ungated = [t for t in textes if "ungated_refuse_en_production" in t]
    assert ungated and not ungated[0].startswith("*")
    # Une alerte inconnue du front est **affichée** sous son nom brut : la taire serait pire.
    assert any("alerte_inconnue_du_front" in t for t in textes)


# --- D8, état 2 : la sonde répond sans profil ------------------------------

def test_sans_gate_la_page_dit_aucun_gate_et_jamais_un_profil(cas: dict[str, Any]) -> None:
    assert cas["sans_gate"]["validation"]["etat"] == "sans_gate"
    textes = cas["sans_gate"]["textes"]
    assert any("aucun gate" in t for t in textes)
    assert not any("vertical" in t for t in textes), "un profil est affiché alors qu'il n'y en a pas"
    assert any("sans_gate" in t for t in textes), "la raison n'est pas dite"


def test_aucun_document_servi_est_dit_pour_ce_quil_est(cas: dict[str, Any]) -> None:
    """`gate_profile` est aussi `null` quand rien n'est servi : ce n'est pas la même chose."""
    textes = cas["aucun_document"]["textes"]
    assert any("Aucun document n'est servi" in t for t in textes)
    assert any("documents servis : aucun" == t for t in textes)


# --- AD-5 : le dictionnaire des variantes ---------------------------------

def _ligne_dictionnaire(textes: list[str]) -> str:
    lignes = [t for t in textes if t.startswith("dictionnaire des variantes :")]
    assert len(lignes) == 1, f"une ligne et une seule est attendue, trouvé {lignes}"
    return lignes[0]


ETATS_DICTIONNAIRE = ("arme", "non_signe", "corpus_perime", "absent")


def test_la_page_dit_ou_en_est_le_dictionnaire_des_variantes(cas: dict[str, Any]) -> None:
    """AD-5 / AD-16 : un dictionnaire inutilisable est **dit**, jamais tu.

    **Quatre** formulations (revue coordonnée 2.1), une par état que le serveur établit, et chacune
    annonce ce qui change pour celui qui pose une question. Le taire laisserait lire « niveau de
    validation : vertical » comme si tout l'était.
    """
    arme = _ligne_dictionnaire(cas["dictionnaire"]["arme"]["textes"])
    assert "le refus « zéro hit » est armé" in arme
    assert "validé" in arme and "corpus servi" in arme

    # Chargé, conforme, non signé : ses variantes servent — c'est la différence matérielle avec
    # l'absence, et la phrase doit la porter.
    non_signe = _ligne_dictionnaire(cas["dictionnaire"]["non_signe"]["textes"])
    assert "personne ne l'a signé" in non_signe
    assert "variantes élargissent bien la recherche" in non_signe
    assert "le refus « zéro hit » est désactivé" in non_signe

    perime = _ligne_dictionnaire(cas["dictionnaire"]["corpus_perime"]["textes"])
    assert "autre corpus" in perime
    assert "le refus « zéro hit » est désactivé" in perime

    absent = _ligne_dictionnaire(cas["dictionnaire"]["absent"]["textes"])
    assert "aucun dictionnaire n'est chargé" in absent
    assert "sans aucune variante" in absent
    assert "le refus « zéro hit » est désactivé" in absent

    # Les quatre états ont quatre phrases distinctes : deux états confondus, c'est une différence
    # observable que la page tait.
    lignes = {nom: _ligne_dictionnaire(cas["dictionnaire"][nom]["textes"])
              for nom in ETATS_DICTIONNAIRE}
    assert len(set(lignes.values())) == 4, lignes
    assert {cas["dictionnaire"][nom]["libelle"]["etat"] for nom in ETATS_DICTIONNAIRE} == \
        set(ETATS_DICTIONNAIRE)

    # …et la ligne est bien **peinte**, pas seulement décidée.
    for nom in ETATS_DICTIONNAIRE:
        assert _ligne_dictionnaire(_textes(cas["dictionnaire"][nom]["dom"])) == lignes[nom], nom


def test_le_refus_nest_annonce_arme_que_quand_le_serveur_le_dit(cas: dict[str, Any],
                                                                code: str) -> None:
    """AC : « la page n'écrit “le refus est armé” que quand le serveur le dit ».

    `refus_zero_hit_actif` est la **règle** (`validated ∧ corpus_ok`), et elle n'a qu'une autorité —
    le serveur (`api/schemas.EtatDictionnaire`). Une page qui referait la conjonction afficherait un
    jour un refus armé qui ne l'est pas, le jour où la règle bougerait d'un côté seulement.
    """
    for nom in ("non_signe", "absent", "corpus_perime"):
        ligne = _ligne_dictionnaire(cas["dictionnaire"][nom]["textes"])
        assert "armé" not in ligne, f"{nom} : le refus est annoncé armé alors qu'il ne l'est pas"
        assert cas["dictionnaire"][nom]["lu"]["refus_zero_hit_actif"] is False, nom
    assert cas["dictionnaire"]["arme"]["lu"]["refus_zero_hit_actif"] is True
    # La bascule est lue sur le champ du serveur, et la conjonction n'est refaite nulle part.
    assert "refus_zero_hit_actif" in code
    assert not re.search(r"validated\s*&&\s*\w*corpus_ok", code), \
        "la page recalcule la règle au lieu de la lire"


def test_un_dictionnaire_absent_nest_ni_perime_ni_confondu_avec_un_dictionnaire_charge(
        cas: dict[str, Any]) -> None:
    """Les deux confusions que `corpus_ok: false` et `validated: false` invitent à faire.

    **Absent ≠ périmé** : un fichier absent et un fichier d'un autre corpus publient les mêmes trois
    booléens ; ce qui les distingue est l'alerte `dictionnaire_corpus_perime`, qu'`api/etat.
    _alertes_dictionnaire` n'émet que lorsque le fichier se lit sans décrire le corpus servi. Écrire
    « périmé » sur la seule foi de `corpus_ok` annoncerait un fichier périmé là où il n'y en a aucun,
    et enverrait chercher le mauvais correctif (réingérer, au lieu d'ingérer).

    **Absent ≠ chargé mais non signé** (revue coordonnée 2.1) : les deux ont `validated: false`, et
    la page rendait la même phrase pour les deux. La différence est pourtant matérielle et le
    serveur la publie — `corpus_ok` : dans un cas les variantes élargissent réellement la recherche
    (une question en anglais ouvre la bonne fiche), dans l'autre rien n'est chargé et la recherche
    est exactement celle d'avant la story.
    """
    assert cas["dictionnaire"]["absent"]["perime"] is False
    assert cas["dictionnaire"]["corpus_perime"]["perime"] is True
    absent = _ligne_dictionnaire(cas["dictionnaire"]["absent"]["textes"])
    assert "autre corpus" not in absent
    assert cas["dictionnaire"]["absent"]["libelle"]["etat"] == "absent"
    assert cas["dictionnaire"]["corpus_perime"]["libelle"]["etat"] == "corpus_perime"

    # Les deux états `validated: false` que `corpus_ok` sépare.
    charge = cas["dictionnaire"]["non_signe"]
    vide = cas["dictionnaire"]["absent"]
    assert charge["lu"]["validated"] is False and vide["lu"]["validated"] is False
    assert charge["lu"]["corpus_ok"] is True and vide["lu"]["corpus_ok"] is False
    assert charge["libelle"]["etat"] != vide["libelle"]["etat"]
    assert "variantes élargissent bien la recherche" in _ligne_dictionnaire(charge["textes"])
    assert "sans aucune variante" in absent


def test_les_deux_alertes_du_dictionnaire_sont_traduites(cas: dict[str, Any]) -> None:
    """Les deux causes tombent ensemble sur un dictionnaire d'un autre corpus : les deux se lisent."""
    textes = cas["dictionnaire"]["corpus_perime"]["textes"]
    assert any("n'a été validé par personne" in t and "dictionnaire_non_valide" in t
               for t in textes)
    assert any("décrit un autre corpus" in t and "dictionnaire_corpus_perime" in t for t in textes)
    # `doc_id="*"` : une propriété du service, pas d'un document — pas de préfixe « * : ».
    for t in textes:
        assert not t.startswith("* :"), t


def test_un_corps_sans_dictionnaire_lisible_est_une_sonde_illisible(cas: dict[str, Any]) -> None:
    """Matrice d'E/S : « la page peint l'état 3, jamais un état partiel ».

    `routes/sante.py` sérialise toujours les trois champs d'`EtatDictionnaire` (tous ont un défaut) :
    un corps qui en ampute un, ou qui rend `dictionary` autre chose qu'un objet, n'a été écrit par
    aucune route. Le peindre « refus désactivé » dirait sur le système quelque chose que le serveur
    n'a pas dit — le même interdit qu'une clé `gate_profile` manquante.
    """
    refuses = [nom for nom in cas["corps_refuses"] if nom.startswith("dictionary_")]
    assert len(refuses) >= 9, refuses
    for nom in refuses:
        assert cas["corps_refuses"][nom]["motif"] == "reponse_illisible", nom
        assert cas["corps_refuses"][nom]["lu"] is None, nom

    # La table de `tests/js/sante_corpus.mjs`, rejouée : les deux côtés de la règle sont exercés.
    table = cas["corpus_dictionnaire"]
    faux = {nom: v["lisible"] for nom, v in table.items() if v["lisible"] != v["attendu"]}
    assert not faux, f"verdict contraire à la table du dictionnaire : {faux}"
    assert sum(1 for v in table.values() if v["attendu"]) >= 4
    assert sum(1 for v in table.values() if not v["attendu"]) >= 10
    # Les quatre états que le serveur écrit sont tous **lisibles** : un lecteur trop strict
    # n'afficherait plus jamais qu'un état 3.
    for nom in ("dictionnaire_arme", "dictionnaire_non_signe", "dictionnaire_absent",
                "dictionnaire_signe_hors_corpus"):
        assert table[nom]["lisible"] is True, nom
    # Un dictionnaire signé mais d'un autre corpus reste un corps **lisible** : c'est un état que le
    # serveur publie (`validated` et `corpus_ok` sont deux faits distincts), pas une sonde en panne.
    assert table["dictionnaire_signe_hors_corpus"]["dictionary"] == {
        "validated": True, "corpus_ok": False, "refus_zero_hit_actif": False}


def test_le_serveur_publie_les_trois_booleens_que_la_page_lit() -> None:
    """Test croisé : les champs que `lireDictionnaire()` exige existent dans `EtatDictionnaire`.

    Le harnais fabrique ses corps, il ne les demande pas au serveur : sans ce croisement, les deux
    moitiés du contrat dériveraient chacune de son côté — et un champ renommé côté serveur ferait
    peindre l'état 3 à toutes les sondes, en production, suite verte.
    """
    champs = set(EtatDictionnaire.model_fields)
    assert champs == {"validated", "corpus_ok", "refus_zero_hit_actif"}, champs
    assert "dictionary" in set(SanteResponse.model_fields)


def test_le_corps_de_reference_des_harnais_est_celui_que_la_route_ecrit(
        cas: dict[str, Any]) -> None:
    """Un corps de référence qui décrit un dépôt révolu ne mesure plus rien (revue coordonnée 2.1).

    Les harnais posent `dictionary: {…}` comme corps **nominal** : tous les cas non spécifiques en
    héritent, et c'est lui qui décide de la formulation que la page rend par défaut. Il portait
    `corpus_ok: false` avec le commentaire « aucun `data/dictionary.json` n'est livré » — vrai à
    l'écriture, faux depuis que le dictionnaire est committé. La suite restait verte en exerçant un
    état que la production ne voit plus, et la formulation réellement servie n'était couverte par
    rien.

    Le garde-fou n'est donc pas un littéral figé : il **démarre le service** et compare. Quand la
    validation humaine sera posée (`validated: true`), ce test rougira — et ce sera juste : il dira
    que les corps de référence ont pris du retard sur ce que le serveur publie.

    Les **deux** copies du corps nominal sont vérifiées (`tests/js/sante_corpus.mjs` et
    `tests/js/accueil_cases.mjs`) : elles peuvent aussi dériver l'une de l'autre.
    """
    from fastapi.testclient import TestClient

    from server.app.api.main import app

    with TestClient(app) as client:
        publie = client.get("/api/v1/sante").json()["dictionary"]

    for source, corps in cas["corps_nominal"].items():
        assert corps["dictionary"] == publie, (
            f"le corps nominal de {source} décrit un état que la route n'écrit plus : "
            f"{corps['dictionary']} vs {publie}")


# --- D8, état 3 : la sonde échoue -----------------------------------------

@pytest.mark.parametrize("situation", ["reseau", "http_500", "http_503", "corps_non_json"])
def test_sonde_en_panne_le_niveau_est_inconnu_et_rien_nest_invente(cas: dict[str, Any],
                                                                   situation: str) -> None:
    """AC : « la page dit que le niveau est inconnu et n'affiche **aucun** profil »."""
    releve = cas["sonde_echouee"][situation]
    textes = releve["textes"]
    assert "niveau de validation : inconnu (le serveur n'a pas répondu)" in textes
    # `dictionnaire des variantes` s'ajoute à la liste depuis la story 2.1 : l'état 3 ne peint aucun
    # état **partiel**, et annoncer « le refus est désactivé » sur une sonde morte serait affirmer
    # sur le système ce que personne n'a lu.
    for interdit in ("vertical", "full", "aucun gate", "documents servis",
                     "dictionnaire des variantes"):
        assert not any(interdit in t for t in textes), f"{interdit!r} affiché sur une sonde morte"
    assert any("ne s'invente pas" in t for t in textes)
    # et le DOM peint ne dit rien de plus que l'arbre décidé
    assert _textes(releve["dom"]) == textes


def test_un_non_200_nest_pas_une_absence_de_gate(cas: dict[str, Any]) -> None:
    """AD-16 : un 503 sur `/sante` est une sonde en échec, pas « le système n'a pas de gate »."""
    assert cas["sonde_echouee"]["http_503"]["motif"] == "http_503"
    assert any("503" in t for t in cas["sonde_echouee"]["http_503"]["textes"])


def test_au_demarrage_nominal_le_niveau_est_peint_dans_la_page(cas: dict[str, Any]) -> None:
    """L'AC principale de la story, au seul endroit où elle se joue en entier (revue 1.10).

    Le harnais relevait déjà `cas.demarrage.dom` et rien ne le lisait : seule la branche d'échec
    était assertée. Retirer `peindre(vueEtat(sante))` de la branche de succès laissait `/` avec un
    bloc « Ce qui a été vérifié » **vide** en production, sans qu'un seul test rougisse.
    """
    dom = cas["demarrage"]["dom"]
    assert dom["enfants"], "le bloc d'état est resté vide après une sonde qui a répondu"
    textes = _textes(dom)
    assert ("niveau de validation : vertical — 2 cas relus par la boucle, "
            "contresignature humaine en attente") in textes
    assert any("documents servis : axa-lu-optihome-2017, lux-guide" == t for t in textes)
    assert any("version servie : b79ca1b" == t for t in textes)
    # …et surtout : ce n'est pas la vue d'échec qui a été peinte.
    assert not any("inconnu" in t for t in textes)
    assert cas["demarrage"]["localStorage"] == {}


def test_au_demarrage_une_sonde_morte_ne_laisse_pas_la_page_muette(cas: dict[str, Any]) -> None:
    """Le bloc « état du système » est vide dans le HTML : s'il le reste, le lecteur ne sait rien."""
    textes = cas["demarrage_sonde_morte"]["textes"]
    assert any("inconnu" in t for t in textes)
    assert cas["demarrage_sonde_morte"]["dom"]["enfants"], "le bloc est resté vide"


# --- lecture stricte du 200 (patron 1.9, revue Codex tours 2 et 3) ---------

def test_un_200_que_la_route_naurait_pas_pu_ecrire_est_refuse(cas: dict[str, Any]) -> None:
    """Une clé **absente** n'est pas « le champ vaut null » : c'est un corps qu'aucune route n'écrit."""
    for nom, releve in cas["corps_refuses"].items():
        assert releve["motif"] == "reponse_illisible", f"{nom} n'a pas été refusé"
        assert releve["lu"] is None


def test_profil_et_compte_sont_indissociables(cas: dict[str, Any]) -> None:
    """`EtatApp.gate_cases` rend `null` dès que `gate_profile` l'est : un corps qui les dissocie ment.

    Depuis la revue Codex 1.10 tour 2, `gate_countersigned` fait partie du même triplet : c'est lui
    qui décide de « relus à la main », et un corps qui le tait ferait choisir la page entre deux
    phrases dont l'une affirme une relecture humaine.
    """
    for nom in ("profil_sans_compte", "compte_sans_profil", "gate_countersigned_absent",
                "gate_countersigned_non_booleen", "gate_countersigned_numerique",
                "profil_sans_contresignature", "contresignature_sans_profil"):
        assert cas["corps_refuses"][nom]["motif"] == "reponse_illisible", nom


@pytest.mark.parametrize("corps", ["gate_profile_vide", "gate_cases_zero", "gate_cases_negatif"])
def test_un_niveau_que_le_serveur_ne_peut_pas_ecrire_est_refuse(cas: dict[str, Any],
                                                                corps: str) -> None:
    """Le plancher est **1**, pas 0, et un profil est une chaîne **non vide**.

    `evals/run.py` refuse de tourner sur zéro cas (« aucun cas au profil … ») : aucun gate ne peut
    porter `cases: 0`, et « niveau de validation : vertical — 0 cas relu à la main » est une phrase
    que rien ne peut produire. Un compte négatif et un profil vide sont du même ordre d'impossibilité
    que les deux champs dissociés — trois corps qu'aucune route n'écrit, donc trois sondes illisibles.
    """
    assert cas["corps_refuses"][corps]["motif"] == "reponse_illisible"


def test_un_200_conforme_nest_jamais_refuse(cas: dict[str, Any]) -> None:
    """Le garde-fou du durcissement : un lecteur trop strict n'afficherait plus jamais de niveau."""
    for nom, releve in cas["corps_conformes"].items():
        assert releve["motif"] is None, f"{nom} refusé alors qu'il est conforme"
    assert cas["corps_conformes"]["sans_gate"]["gate_profile"] is None


def test_le_serveur_emet_tout_ce_que_la_page_exige_dun_200() -> None:
    """Test croisé : les champs que `lireSante()` exige existent bien dans `SanteResponse`.

    Sans lui, les deux moitiés du contrat pourraient dériver chacune de son côté — le harnais
    fabrique ses corps, il ne les demande pas au serveur.
    """
    champs = set(SanteResponse.model_fields)
    for exige in ("ok", "version", "documents_servis", "gate_profile", "gate_cases", "alerts"):
        assert exige in champs, f"la page lit `{exige}`, que `SanteResponse` ne publie pas"


def test_la_page_sait_traduire_toutes_les_alertes_que_le_serveur_emet(cas: dict[str, Any]) -> None:
    """Croisement de la table du front avec les noms d'alerte réellement émis (revue 1.10).

    `cas.alertes_connues` était relevé par le harnais et lu par personne : rien ne reliait la table
    `ALERTES` de la page aux `Alerte(alerte=…)` que `api/etat` construit et à ceux que
    `corpus/loader` fait remonter. Une alerte ajoutée côté serveur serait apparue brute à l'écran,
    suite verte.

    Les **raisons de quarantaine** (`gate_echoue`, `bloquant_statique`, `sans_gate`) ne sont pas des
    noms d'alerte : elles voyagent dans `detail` sous `alerte: "quarantaine"`, et la page les traduit
    par `RAISONS` — c'est l'objet d'un test voisin.
    """
    etat = (REPO_ROOT / "server" / "app" / "api" / "etat.py").read_text("utf-8")
    loader = (REPO_ROOT / "server" / "app" / "corpus" / "loader.py").read_text("utf-8")
    emises = set(re.findall(r'alerte="([a-z_]+)"', etat))
    # Celles que le loader pose dans `Corpus.alerts`, et qu'`_alertes()` recopie telles quelles.
    emises |= set(re.findall(r'alerts\.append\("([a-z_]+)"\)', loader))
    emises |= set(re.findall(r'\["([a-z_]+)"\]\) if allow_ungated', loader))
    emises |= {"gate_perime"}  # posé par `_gate_alerts`, seule branche à ne pas suivre ces formes
    assert {"sans_gate", "gate_perime", "source_absente", "quarantaine", "rapport_illisible",
            "rapport_etranger", "ungated_refuse_en_production"} <= emises, emises

    connues = set(cas["alertes_connues"])
    manquantes = emises - connues
    assert not manquantes, (
        f"le serveur émet des alertes que la page ne sait pas traduire : {sorted(manquantes)} — "
        "elles apparaîtraient brutes à l'écran")


def test_la_page_sait_traduire_toutes_les_raisons_de_quarantaine(cas: dict[str, Any]) -> None:
    """Même croisement pour les raisons que `corpus/loader` met dans `Corpus.quarantine`.

    Seules celles qu'un lecteur peut comprendre et corriger sont traduites : les raisons purement
    techniques (`document.json absent`, `overlay : …`) sont des phrases françaises complètes, que la
    page affiche telles quelles.
    """
    raisons = {r[0] for r in cas["raisons_connues"]}
    assert {"gate_echoue", "bloquant_statique", "sans_gate"} <= raisons
    loader = (REPO_ROOT / "server" / "app" / "corpus" / "loader.py").read_text("utf-8")
    for raison in raisons:
        assert raison in loader, f"{raison!r} n'est plus une raison de quarantaine du loader"


# --- AD-15 : `textContent` seul, aucun stockage ---------------------------

def test_aucun_innerhtml_ne_sert_a_poser_du_texte(code: str) -> None:
    """AD-15 : `innerHTML` n'est employé que pour **vider** un conteneur (chaîne vide)."""
    poses = re.findall(r"innerHTML\s*=\s*(.+)", code)
    assert poses, "aucun `innerHTML` : le test ne vérifie plus rien"
    for p in poses:
        assert p.strip().startswith('""'), f"innerHTML non vide : {p.strip()}"
    # Le DOM minimal lève sur toute pose non vide : le harnais l'a exécuté sans lever.
    assert "textContent" in code


def test_la_page_necrit_rien_dans_le_navigateur(cas: dict[str, Any], code: str) -> None:
    """AD-15 : aucune donnée personnelle, aucun `localStorage` sur l'accueil."""
    assert cas["nominal"]["localStorage"] == {}
    assert cas["demarrage"]["localStorage"] == {}
    assert "localStorage" not in code
    assert "sessionStorage" not in code
    assert "document.cookie" not in code


def test_aucune_requete_tierce(code: str, page: str) -> None:
    """AD-12 : aucune requête tierce, aucun framework, aucun build.

    Les liens du pied de page vers GitHub sont des liens que l'utilisateur clique, pas des requêtes
    faites par la page : ce test regarde ce qui **part** au chargement.
    """
    assert re.search(r"fetch\(\s*API_BASE", code), "la seule requête doit partir sur API_BASE"
    assert len(re.findall(r"\bfetch\(", code)) == 1
    for tiers in ("<script src=\"http", "<link rel=\"stylesheet\" href=\"http", "cdn.", "googleapis"):
        assert tiers not in page


# --- AD-12 / FR42 : la page elle-même -------------------------------------

def test_la_page_porte_les_trois_entrees(page: str) -> None:
    """AC : trois entrées — `/guide/`, `/sinistre/`, sujet 3."""
    assert 'href="/guide/"' in page
    assert 'href="/sinistre/"' in page
    assert 'href="#sujet-3"' in page and 'id="sujet-3"' in page


def test_la_page_porte_la_reponse_ecrite_au_sujet_3(page: str) -> None:
    """FR24 / D9 : la réponse J+1 est écrite, avec ses sept points."""
    assert "je ne fais pas un RAG" in page
    for point in ("base SQL", "profil par colonne", "SELECT", "LIMIT", "lecture seule",
                  "après le filtre", "lignes sources", "ne disent pas", "Chez Foyer"):
        assert point in page, f"la réponse au sujet 3 ne dit rien de {point!r}"


def test_la_mini_demo_est_annoncee_comme_bonus_et_aucune_page_excel_nest_promise(page: str) -> None:
    """D9 : « annoncer une page `/excel/` qui n'existe pas serait la même faute »."""
    assert 'href="/excel/"' not in page
    assert "viendrait" in page and "bonus" in page


def test_la_page_porte_les_liens_vers_le_depot(page: str) -> None:
    assert "https://github.com/byousoku-9/foyer-retour" in page
    # Les documents qui n'existent pas ne sont pas liés (`Never` de la spec : 5.2/5.3).
    assert "docs/architecture.md" not in page
    assert "docs/choix-et-limites.md" not in page


def test_le_bloc_detat_est_vide_dans_le_html(page: str) -> None:
    """AD-11 : la page n'écrit aucun niveau ; le bloc est rempli par le script, ou pas du tout."""
    assert re.search(r'<div id="etat"[^>]*>\s*</div>', page)
    # Rempli après la sonde : sans région live, un lecteur d'écran n'entend jamais apparaître le
    # niveau de validation, qui est l'information que cette section existe pour donner.
    assert re.search(r'<div id="etat"[^>]*role="status"', page)
    assert 'aria-live="polite"' in page
    assert "<noscript>" in page, "sans JavaScript, le lecteur doit savoir pourquoi le bloc est vide"


def test_le_bloc_des_resultats_est_vide_dans_le_html(page: str) -> None:
    """Story 4.5 : `/` n'écrit aucun chiffre de mesure ; ils viennent tous de la route.

    Le bloc est rempli par le script, ou pas du tout. Un chiffre codé dans la page survivrait à
    n'importe quelle campagne et affirmerait une mesure qui n'a pas eu lieu.
    """
    assert re.search(r'<div id="evals"[^>]*>\s*</div>', page)
    assert re.search(r'<div id="evals"[^>]*role="status"', page)
    assert "/api/v1/evals/latest" in page, "la page nomme la route qu'elle sonde"
    # Aucun chiffre de mesure n'est écrit dans le HTML : ni recall, ni coût, ni latence.
    for interdit in ("recall", "run_digest", "cases_hash", "ne_tranche_pas"):
        assert interdit not in page.lower(), f"{interdit} ne s'écrit pas dans la page"


def test_les_identifiants_du_harnais_sont_ceux_de_la_page(page: str) -> None:
    """Un renommage dans la page ne doit pas laisser le harnais piloter un DOM qui n'existe plus."""
    harnais = HARNAIS.read_text(encoding="utf-8")
    for identifiant in re.findall(r'\{ tag: "\w+", id: "([\w-]+)" \}', harnais):
        assert f'id="{identifiant}"' in page, f"#{identifiant} attendu par le harnais, absent de la page"


def test_le_script_est_charge_par_la_page(page: str) -> None:
    assert '<script src="accueil.js?v=1"></script>' in page


# --- Convention Seuils ----------------------------------------------------

def test_le_budget_de_la_sonde_est_un_seuil_du_serveur(cas: dict[str, Any]) -> None:
    """Convention Seuils : le littéral du front vaut ce que `config.py` publie, et rien d'autre.

    Ce n'est pas une marge ajoutée à une deadline — `/sante` n'en a pas —, c'est le temps qu'on
    accepte d'attendre pour un état de santé ; la valeur choisie est celle que `config.py` réserve
    déjà au client.
    """
    seuils = Settings(_env_file=None).thresholds()
    assert cas["bornes"]["sonde_budget_s"] == seuils["client_abort_margin_s"]


# --- UX-DR9 : le lien « ← retour » de la copie du site --------------------

def test_la_copie_du_site_porte_un_lien_retour_discret() -> None:
    """AC de la story (UX-DR9) : acquise en 1.6 (commit `web/` séparé), à vérifier — pas à réécrire."""
    index = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="retour-projet"' in index
    assert 'href="/"' in index
    assert "retour" in index
