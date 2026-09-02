"""Les tables écrites deux fois, rejouées côte à côte (story 2.5).

Trois règles vivent en double par **décision antérieure**, pas par négligence : D8 rend les pages
`tools/` autonomes — elles n'importent rien de `web/app/` et n'empruntent pas sa feuille de style —
et la règle de profil est appliquée par le site sur son propre parcours, sans appeler le serveur.
Aucune ne peut être factorisée sans casser cette autonomie ; toutes peuvent être **amarrées**.

  1. les phrases d'alerte du serveur — `web/app/chat.js` / `tools/accueil/accueil.js` ;
  2. les phrases d'état, la preuve d'absence et les libellés de contrôle — `web/app/chat.js` /
     `tools/sinistre/sinistre.js` ;
  3. la règle de profil — `web/app/ui.js::etapeConcerne` / `server/app/domain/profil.py`.

Pour les deux premières, l'exigence est l'**identité mot pour mot** sur les clés communes. Pour la
troisième elle ne peut pas l'être : la divergence d'économie est voulue (le site cache dans le
doute, le serveur ne promeut pas dans le doute). `tests/data/profil_cas.json` la déclare donc
**colonne par colonne** — `site_garde` et `serveur_promeut` — si bien qu'une dérive non voulue
rougit et qu'une divergence voulue se lit.

Le harnais `tests/js/tables_cases.mjs` monte les quatre modules JavaScript dans des contextes neufs
et écrit du JSON ; il ne juge rien. Tout ce qui est affirmé l'est ici.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from server.app.config import REPO_ROOT
from server.app.domain.document import ParcoursCondition
from server.app.domain.profil import noeuds_du_profil

HARNAIS = REPO_ROOT / "tests" / "js" / "tables_cases.mjs"
TABLE = REPO_ROOT / "tests" / "data" / "profil_cas.json"

# Même règle que `tests/test_web_chat.py` : sans `node`, ces cas passent en `skip`, et la CI pose
# `FRONT_TESTS_REQUIS=1` pour que le skip devienne un échec.
REQUIS = os.environ.get("FRONT_TESTS_REQUIS", "") not in ("", "0")


def _lancer() -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        motif = "node absent de la machine : les tables partagées ne peuvent pas être rejouées"
        if REQUIS:
            pytest.fail("FRONT_TESTS_REQUIS=1 mais " + motif)
        pytest.skip(motif)
    fini = subprocess.run([node, str(HARNAIS)], capture_output=True, text=True, timeout=120,
                          cwd=str(REPO_ROOT))
    assert fini.returncode == 0, f"le harnais a échoué ({fini.returncode}) :\n{fini.stderr}"
    charge = json.loads(fini.stdout) if fini.stdout.strip() else {"ok": False, "erreur": fini.stderr}
    assert charge.get("ok") is True, charge.get("erreur")
    return charge["cas"]


@pytest.fixture(scope="module")
def cas() -> dict[str, Any]:
    """Le harnais est exécuté **une fois** pour tout le module."""
    return _lancer()


@pytest.fixture(scope="module")
def table() -> dict[str, Any]:
    return json.loads(Path(TABLE).read_text("utf-8"))


# --- 1. les phrases d'alerte : chat et accueil ----------------------------

def test_les_phrases_dalerte_du_chat_et_de_laccueil_sont_identiques(cas: dict[str, Any]) -> None:
    """Un badge et une page d'accueil qui décrivent la même alerte en deux phrases différentes
    laisseraient croire à deux faits. La table de l'accueil fait autorité (story 1.10) ; celle du
    chat en est la copie, et cette assertion est la seule chose qui l'empêche de dériver."""
    chat = cas["alertes"]["chat"]
    accueil = cas["alertes"]["accueil"]
    communes = set(chat) & set(accueil)
    assert communes, "les deux tables n'ont aucune clé en commun : l'une des deux a été vidée"
    divergentes = {k: (chat[k], accueil[k]) for k in sorted(communes) if chat[k] != accueil[k]}
    assert not divergentes, f"phrases d'alerte divergentes : {divergentes}"
    # Et le chat ne peut pas en connaître **moins** : le badge dirait « source_absente » en clair.
    assert set(accueil) <= set(chat), sorted(set(accueil) - set(chat))


def test_la_page_sinistre_dit_les_alertes_comme_les_deux_autres(cas: dict[str, Any]) -> None:
    """La trace du sinistre affiche les alertes du gate de son contrat : mêmes phrases."""
    chat = cas["alertes"]["chat"]
    sinistre = cas["alertes"]["sinistre"]
    assert sinistre == chat, {k: (sinistre.get(k), chat.get(k))
                              for k in set(sinistre) | set(chat) if sinistre.get(k) != chat.get(k)}


def test_les_alertes_du_front_couvrent_celles_que_le_serveur_publie() -> None:
    """La table ne vaut que si elle traduit ce que `/api/v1/sante` peut réellement écrire.

    Le vocabulaire des alertes vit dans `server/app/api/etat.py` (et `corpus/loader.py` pour la
    quarantaine) ; un nom ajouté là-bas sans phrase ici s'afficherait brut à l'utilisateur — ce que
    le code accepte délibérément (« un nom inconnu se dit tel quel »), mais qui doit être une
    surprise, pas la règle."""
    import re

    table_du_front = set(_table_js("ALERTES"))
    etat = (REPO_ROOT / "server" / "app" / "api" / "etat.py").read_text("utf-8")
    loader = (REPO_ROOT / "server" / "app" / "corpus" / "loader.py").read_text("utf-8")
    publiees = set(re.findall(r'alerte="([a-z_]+)"', etat + loader))
    assert publiees, "aucun nom d'alerte trouvé côté serveur : le motif de recherche a dérivé"
    manquantes = sorted(publiees - table_du_front)
    assert not manquantes, f"alertes publiées par le serveur sans phrase française : {manquantes}"


# --- 2. les phrases d'état : chat et sinistre -----------------------------

def test_les_phrases_detat_du_chat_et_du_sinistre_sont_identiques(cas: dict[str, Any]) -> None:
    """Reprise différée de 2.3 : la page sinistre rendait les phrases de lacune sans le cadre des
    trois états. Les cinq phrases du chat ont été écrites **sans nommer le document** exactement
    pour pouvoir servir sur un contrat ; il ne restait qu'à les y brancher, et à les amarrer."""
    for entree in cas["phrases_etat"]:
        assert entree["chat"] == entree["sinistre"], entree
    # Et elles ne nomment aucun document, ni d'un côté ni de l'autre.
    for entree in cas["phrases_etat"]:
        for mot in ("guide", "contrat", "conditions générales"):
            assert mot not in entree["chat"], (mot, entree)
    # Story 4.2f : la phrase du quatrième état n'annonce ni panne ni absence — c'est tout son objet.
    partielles = [e["chat"] for e in cas["phrases_etat"]
                  if (e["entree"] or {}).get("cle") == "lecture-partielle"]
    assert len(partielles) == 4 and len(set(partielles)) == 4  # les deux drapeaux comptent
    for phrase in partielles:
        assert phrase.startswith("ma lecture s'est arrêtée avant de conclure")
        assert "indisponible" not in phrase


def test_les_trois_etats_sont_derives_pareil_des_deux_booleens(cas: dict[str, Any]) -> None:
    for entree in cas["etats"]:
        assert entree["chat"] == entree["sinistre"], entree
    assert [e["chat"]["cle"] for e in cas["etats"]] == [
        "sur", "partiel", "inconnu", "lecture-partielle", "inconnu"]


def test_la_preuve_dabsence_est_chiffree_pareil_des_deux_cotes(cas: dict[str, Any]) -> None:
    """M15 : « rien trouvé sur 1 457 passages » et « rien n'a été cherché » sont deux refus
    différents. Le guide le disait depuis 1.7, le sinistre ne le disait pas ; ils le disent
    désormais dans les mêmes mots, accords en nombre compris."""
    for entree in cas["preuves"]:
        assert entree["chat"] == entree["sinistre"], entree
    par_entree = {json.dumps(e["entree"], sort_keys=True): e["chat"] for e in cas["preuves"]}
    grand = par_entree[json.dumps({"kind": "zero_hit", "terms_searched": ["mobilier"],
                                   "variants_count": 0, "blocks_scanned": 1457}, sort_keys=True)]
    assert grand == "Termes cherchés : mobilier — 0 variante essayée, 1457 passages parcourus"
    # Les compteurs s'affichent **même à zéro**, et s'accordent en nombre : « 0 variante essayée »
    # (singulier), « 1457 passages parcourus » (pluriel).
    petit = par_entree[json.dumps({"kind": "zero_hit", "terms_searched": ["bail"],
                                   "variants_count": 1, "blocks_scanned": 1}, sort_keys=True)]
    assert petit == "Termes cherchés : bail — 1 variante essayée, 1 passage parcouru"
    par_kind = {(e["entree"] or {}).get("kind"): e["chat"] for e in cas["preuves"]}
    # Une clarification n'a rien cherché : aucune preuve chiffrée (AD-4).
    assert par_kind["clarification_requise"] == ""
    assert par_kind[None] == ""


def test_sans_gate_nest_dit_quune_fois_et_pareil_des_deux_cotes(cas: dict[str, Any]) -> None:
    """I1, revue 2.5 : mesure absente et alerte `sans_gate` ne sont pas deux faits."""
    attendu = ["aucune question-témoin ne valide ce document (sans_gate)"]
    assert cas["gate_sans_gate"]["chat"] == attendu
    assert cas["gate_sans_gate"]["sinistre"] == attendu


def test_les_libelles_de_controle_sont_les_memes_des_deux_cotes(cas: dict[str, Any]) -> None:
    chat = cas["controles"]["chat"]
    sinistre = cas["controles"]["sinistre"]
    assert chat == sinistre, {k: (chat.get(k), sinistre.get(k))
                              for k in set(chat) | set(sinistre) if chat.get(k) != sinistre.get(k)}
    # Un nom inconnu n'est **jamais** masqué : il se dit tel quel, des deux côtés.
    assert cas["controle_inconnu"] == {"chat": "controle_de_demain",
                                       "sinistre": "controle_de_demain"}


def test_les_libelles_de_controle_couvrent_ceux_que_le_serveur_emet() -> None:
    """Un `CheckResult.name` sans libellé s'afficherait brut dans « Pourquoi cette réponse ».

    Le code l'accepte (« un nom inconnu s'affiche tel quel, jamais masqué ») : c'est ce qui garantit
    qu'un contrôle ajouté demain ne disparaît pas de l'écran. Mais l'état **normal** est que chaque
    contrôle émis par le serveur ait sa phrase, et c'est ce que ce test tient."""
    import re

    noms = set()
    for chemin in (REPO_ROOT / "server" / "app").rglob("*.py"):
        source = chemin.read_text("utf-8")
        for bloc in re.findall(r"CheckResult\((.{0,120})", source, flags=re.S):
            noms |= set(re.findall(r'name="([a-z_0-9]+)"', bloc))
    assert len(noms) >= 30, f"trop peu de contrôles trouvés ({len(noms)}) : le motif a dérivé"

    manquants = sorted(noms - set(_table_js("CONTROLES")))
    assert not manquants, f"contrôles émis par le serveur sans libellé français : {manquants}"


# --- 3. la règle de profil : le site et le serveur ------------------------

def _table_js(nom: str) -> list[str]:
    """Les clés d'une table de `web/app/chat.js`, lues sur le fichier lui-même.

    Le harnais Node relève la table **évaluée** ; ceci lit le **texte**, et sert aux deux tests qui
    la confrontent au serveur (Python ne peut pas exécuter le JS sans le harnais, et ces deux
    tests-là comparent des noms, pas des phrases)."""
    import re

    chat = (REPO_ROOT / "web" / "app" / "chat.js").read_text("utf-8")
    debut = chat.index(f"var {nom} = {{")
    corps = chat[debut:chat.index("\n  };", debut)]
    cles = re.findall(r"^    ([a-z_0-9]+):", corps, flags=re.M)
    assert cles, f"table {nom} illisible dans chat.js"
    return cles


def _serveur_promeut(si: dict[str, Any], profil: dict[str, Any]) -> bool:
    """Le nœud est-il **désigné** par ce profil ? C'est la seule question que le serveur pose."""
    condition = ParcoursCondition(node_id="n", si=si)
    return noeuds_du_profil([condition], profil) == ["n"]


def test_la_table_de_cas_couvre_les_conditions_du_parcours_livre(cas: dict[str, Any],
                                                                 table: dict[str, Any]) -> None:
    """La table déclare les neuf conditions de la source : si `data/lux-guide/document.json` change,
    elle doit changer avec lui — sans quoi elle vérifierait une règle sur des cas qui n'existent
    plus."""
    document = json.loads((REPO_ROOT / "data" / "lux-guide" / "document.json").read_text("utf-8"))
    reelles = [{"node_id": c["node_id"], "si": c["si"]} for c in document["parcours"]]
    assert cas["conditions"] == reelles, "la table ne décrit plus le parcours servi"
    # Et chaque forme de `si` du parcours livré est exercée par au moins un cas.
    formes_du_parcours = {json.dumps(c["si"], sort_keys=True) for c in reelles}
    formes_de_la_table = {json.dumps(c["si"], sort_keys=True) for c in table["cas"]}
    manquantes = sorted(formes_du_parcours - formes_de_la_table)
    assert not manquantes, f"conditions du parcours livré sans cas : {manquantes}"


@pytest.mark.parametrize("nom", [c["nom"] for c in
                                 json.loads(Path(TABLE).read_text("utf-8"))["cas"]])
def test_chaque_cas_de_profil_donne_le_verdict_declare(cas: dict[str, Any], table: dict[str, Any],
                                                       nom: str) -> None:
    """Les deux colonnes, cas par cas. Le site est relevé par `node`, le serveur calculé ici.

    C'est le garde-fou que la reprise différée de 2.3 réclamait : « rien ne rougira si l'un des deux
    dérive ». Une divergence **voulue** est déclarée dans la table et se lit ; toute autre est un
    échec.
    """
    attendu = {c["nom"]: c for c in table["cas"]}[nom]
    releve = {c["nom"]: c for c in cas["profil"]}[nom]
    assert releve["site_garde"] is attendu["site_garde"], (
        f"`ui.js::etapeConcerne` a dérivé sur {nom} : {attendu['pourquoi'] if 'pourquoi' in attendu else ''}")
    assert _serveur_promeut(attendu["si"], attendu["profil"]) is attendu["serveur_promeut"], (
        f"`domain/profil.py` a dérivé sur {nom}")


def test_chaque_divergence_est_declaree_et_motivee(cas: dict[str, Any],
                                                   table: dict[str, Any]) -> None:
    """Une divergence non déclarée est une dérive ; une divergence déclarée sans motif est un
    constat qu'on a cessé de comprendre. Les deux sont refusées."""
    releve = {c["nom"]: c["site_garde"] for c in cas["profil"]}
    for c in table["cas"]:
        diverge = releve[c["nom"]] is not c["serveur_promeut"]
        assert diverge == bool(c.get("divergence_voulue")), (
            f"{c['nom']} : divergence {'non déclarée' if diverge else 'déclarée mais absente'}")
        if diverge:
            assert c.get("pourquoi", "").strip(), f"{c['nom']} : divergence sans motif écrit"


def test_les_deux_regles_saccordent_partout_ailleurs(cas: dict[str, Any],
                                                     table: dict[str, Any]) -> None:
    """La mesure de ce qui est réellement partagé : hors des divergences déclarées, les deux règles
    disent la même chose, et il y a bien plus d'accords que de divergences."""
    accords = [c["nom"] for c in table["cas"] if not c.get("divergence_voulue")]
    assert len(accords) >= len(table["cas"]) - len(accords), \
        "plus de divergences que d'accords : ce ne sont plus deux écritures d'une même règle"
    releve = {c["nom"]: c["site_garde"] for c in cas["profil"]}
    for nom in accords:
        entree = {c["nom"]: c for c in table["cas"]}[nom]
        assert releve[nom] is entree["serveur_promeut"] is entree["site_garde"]


def test_linversion_deconomie_est_bien_celle_qui_est_documentee(cas: dict[str, Any],
                                                                table: dict[str, Any]) -> None:
    """Le sens de la divergence n'est pas indifférent : le site **cache** des étapes (dans le doute
    il montre), le serveur **promeut** des fiches (dans le doute il ne promeut pas). Sur une clé
    absente, le site garde et le serveur ne désigne pas — jamais l'inverse."""
    absents = [c for c in table["cas"] if c["nom"].endswith("_absent")
               or c["nom"] == "profil_entierement_vide"]
    assert absents, "aucun cas de clé absente : l'inversion n'est pas exercée"
    releve = {c["nom"]: c["site_garde"] for c in cas["profil"]}
    for c in absents:
        assert releve[c["nom"]] is True and c["serveur_promeut"] is False, c["nom"]


def test_une_etape_sans_condition_reste_toujours_a_lecran(cas: dict[str, Any]) -> None:
    """Les bords que `etapeConcerne` traite avant même de regarder `si`. Le serveur, lui, **ignore**
    une condition sans `si` (`noeuds_du_profil` la saute) : une étape que rien ne conditionne n'est
    pas une fiche que le profil désigne. Les deux sont cohérents avec leur économie."""
    assert cas["profil_bords"] == {"sans_si": True, "chaine": True, "nul": True, "si_vide": True}
    assert _serveur_promeut({}, {"enfants": "2"}) is False

def test_le_harnais_vit_dans_le_depot_et_ne_depend_de_rien() -> None:
    """Aucune dépendance ajoutée : le harnais n'utilise que la bibliothèque standard de Node et le
    DOM minimal du dépôt. `ui.js` en a besoin — il touche `document` au chargement."""
    source = Path(HARNAIS).read_text("utf-8")
    imports = {ligne.split('"')[1] for ligne in source.splitlines()
               if ligne.startswith("import ") and '"' in ligne}
    assert imports <= {"node:fs", "node:path", "node:url", "node:vm", "./dom_minimal.mjs"}, imports


def test_chaque_module_est_monte_dans_un_contexte_neuf() -> None:
    """Les quatre tables sont relevées **séparément** : un module qui en écraserait un autre sur un
    `window` partagé ferait coïncider deux tables sans qu'aucune ne soit lue deux fois."""
    source = Path(HARNAIS).read_text("utf-8")
    # Un `monter()` par module comparé : guide, accueil, sinistre, site.
    assert source.count("= monter(") == 4, source.count("= monter(")
    assert "vm.createContext" in source


def test_le_chiffre_de_la_lecture_bornee_est_le_meme_des_deux_cotes(cas: dict[str, Any]) -> None:
    """Story 4.2f : le pendant de la preuve d'absence pour le second porteur d'un `found=false`.

    La même donnée, rendue en deux phrases différentes selon la page, aurait fait croire à deux
    mesures. Comme la preuve, les compteurs s'affichent **même à zéro** — « la navigation n'a rien
    fait entrer » et « douze passages sont partis au modèle » sont deux situations distinctes — et
    la phrase ne nomme aucun document.
    """
    for entree in cas["lectures"]:
        assert entree["chat"] == entree["sinistre"], entree
    par_entree = {json.dumps(e["entree"], sort_keys=True): e["chat"] for e in cas["lectures"]}
    grand = par_entree[json.dumps({"nodes_read": 3, "blocks_read": 12, "documents": []},
                                  sort_keys=True)]
    assert grand == ("Lecture partielle : 3 sections lues, 12 passages transmis au modèle "
                     "— le reste n'a pas été lu, et rien n'en est affirmé")
    petit = par_entree[json.dumps({"nodes_read": 1, "blocks_read": 1, "documents": []},
                                  sort_keys=True)]
    assert petit.startswith("Lecture partielle : 1 section lue, 1 passage")
    # Les deux compteurs ont un plancher à 1 dans le domaine : la table ne joue que des valeurs
    # légales, faute de quoi elle consacrerait un rendu que l'invariant interdit.
    moyen = par_entree[json.dumps({"nodes_read": 2, "blocks_read": 9, "documents": []},
                                  sort_keys=True)]
    assert moyen.startswith("Lecture partielle : 2 sections lues, 9 passages")
    assert par_entree["null"] == ""
    for entree in cas["lectures"]:
        for mot in ("guide", "contrat", "conditions générales"):
            assert mot not in entree["chat"], (mot, entree)


def test_les_etapes_sans_appel_sont_celles_dont_le_tier_est_absent() -> None:
    """Le même fait, énoncé dans deux couches qui ne peuvent pas se lire : il ne doit pas diverger.

    `pipelines` n'a pas le droit de voir `llm` (table des couches), et la deadline a besoin de
    savoir quelle étape dépense. `ETAPES_SANS_APPEL` vit donc dans `domain`, et cette égalité est
    le seul lien qui empêche les deux tables de se contredire — le jour où une étape sans appel
    s'ajoute, elle rougit ici plutôt que de rendre un 503 sur une remise.
    """
    from server.app.domain.trace import ETAPES_SANS_APPEL
    from server.app.llm.models import STEP_TIERS

    assert {nom for nom, tier in STEP_TIERS.items() if tier is None} == ETAPES_SANS_APPEL
