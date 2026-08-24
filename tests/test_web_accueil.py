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

from server.app.api.schemas import SanteResponse
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


def test_la_page_ne_sonde_quune_route_et_ne_coute_rien(cas: dict[str, Any]) -> None:
    """`/api/v1/sante` n'est pas limitée et ne coûte rien : la page ne demande rien d'autre."""
    assert cas["appels_au_demarrage"] == 1
    assert cas["demarrage"]["appels"] == ["https://foyer-retour.example/api/v1/sante"]


def test_une_page_hors_ligne_ne_sonde_rien(cas: dict[str, Any]) -> None:
    """`file://` : aucun serveur à joindre, donc aucune requête — et la page le dit."""
    assert cas["hors_ligne"]["api_base"] == ""
    assert cas["hors_ligne"]["appels_reseau"] == 0
    assert cas["hors_ligne"]["motif"] == "hors_ligne"
    assert any("fichier local" in t for t in cas["hors_ligne"]["textes"])


# --- D8, état 1 : la sonde répond avec un profil --------------------------

def test_le_niveau_de_validation_vient_du_serveur(cas: dict[str, Any]) -> None:
    """AC : « niveau de validation : vertical — 2 cas relus à la main », lu sur `/api/v1/sante`."""
    assert cas["nominal"]["validation"] == {
        "etat": "gate",
        "texte": "niveau de validation : vertical — 2 cas relus à la main"}
    textes = cas["nominal"]["textes"]
    assert "niveau de validation : vertical — 2 cas relus à la main" in textes
    assert any("documents servis : axa-lu-optihome-2017, lux-guide" == t for t in textes)
    assert any("version servie : b79ca1b" == t for t in textes)


def test_le_compte_de_cas_nest_pas_ecrit_dans_la_page(cas: dict[str, Any], code: str,
                                                      page: str) -> None:
    """D8 : « `gate_cases` vient du serveur ; “2 cas” n'est écrit nulle part dans la page ».

    Un serveur qui publie `gate_cases: 1` doit faire écrire « 1 cas relu », singulier compris —
    c'est la preuve que le nombre n'est pas un littéral déguisé.
    """
    assert cas["un_seul_cas"]["texte"] == "niveau de validation : vertical — 1 cas relu à la main"
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
    ungated = [t for t in textes if "ungated_en_production" in t]
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


# --- D8, état 3 : la sonde échoue -----------------------------------------

@pytest.mark.parametrize("situation", ["reseau", "http_500", "http_503", "corps_non_json"])
def test_sonde_en_panne_le_niveau_est_inconnu_et_rien_nest_invente(cas: dict[str, Any],
                                                                   situation: str) -> None:
    """AC : « la page dit que le niveau est inconnu et n'affiche **aucun** profil »."""
    releve = cas["sonde_echouee"][situation]
    textes = releve["textes"]
    assert "niveau de validation : inconnu (le serveur n'a pas répondu)" in textes
    for interdit in ("vertical", "full", "aucun gate", "documents servis"):
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
    assert "niveau de validation : vertical — 2 cas relus à la main" in textes
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
    """`EtatApp.gate_cases` rend `null` dès que `gate_profile` l'est : un corps qui les dissocie ment."""
    assert cas["corps_refuses"]["profil_sans_compte"]["motif"] == "reponse_illisible"
    assert cas["corps_refuses"]["compte_sans_profil"]["motif"] == "reponse_illisible"


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
            "rapport_etranger", "ungated_en_production"} <= emises, emises

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


def test_les_identifiants_du_harnais_sont_ceux_de_la_page(page: str) -> None:
    """Un renommage dans la page ne doit pas laisser le harnais piloter un DOM qui n'existe plus."""
    harnais = HARNAIS.read_text(encoding="utf-8")
    for identifiant in re.findall(r'\{ tag: "\w+", id: "([\w-]+)" \}', harnais):
        assert f'id="{identifiant}"' in page, f"#{identifiant} attendu par le harnais, absent de la page"


def test_le_script_est_charge_par_la_page(page: str) -> None:
    assert '<script src="accueil.js"></script>' in page


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
