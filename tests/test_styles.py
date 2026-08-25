"""Le contraste des couleurs d'état du chat, calculé sur la feuille elle-même (story 2.5).

**Aucun test du dépôt ne chargeait `web/app/styles.css`.** `tests/js/dom_minimal.mjs` est un DOM en
mémoire : il ne calcule aucun style, n'a pas de `getComputedStyle` et ne charge aucune feuille. La
story 2.4 l'a payé — une couleur d'avertissement visait `var(--warning, …)`, une variable qui
n'existe pas dans le fichier, et la troisième portée de thème (`:root[data-theme="dark"]`, celle du
bouton de thème) tombait à 2,79:1, sous le seuil AA. Le défaut a été trouvé par un pilote Chrome
écrit à la main, hors dépôt ; remettre `var(--warning, …)` aujourd'hui laisserait la CI verte.

Ce module ferme ce trou **sans navigateur** : il lit la feuille, extrait les variables des trois
portées de thème, résout les couleurs des classes d'état du chat — `var(…)` et `color-mix(in srgb,
X n%, transparent)` compris, composité sur la surface de la bulle — et calcule le contraste WCAG
2.x. Un échec est rouge, jamais un avertissement.

**Ce qui est mesuré.** D'abord les *couleurs d'état* — celles qui portent une information : sûr /
partiel / inconnu, dégradé, repli de langue, indisponibilité, erreur, contrôle passé ou échoué, et
le badge de mode. Puis les teintes de texte du thème sur la surface d'une bulle, `--muted` compris :
c'est le pied de réponse, la preuve chiffrée et les titres de rubrique du panneau, et il ne tenait
pas le seuil en thème clair faute que rien ne le mesure.

Le calcul est fait **sur le fichier**, sans navigateur : `var()` est résolu dans la portée demandée,
et `color-mix(in srgb, X n%, transparent)` est composé sur la surface — un aplat translucide de la
couleur du texte rapproche le fond du texte, donc il **retire** du contraste, et le lire comme la
couleur pure ferait passer un badge que le navigateur affiche bien plus pâle.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from server.app.config import REPO_ROOT

FEUILLE = REPO_ROOT / "web" / "app" / "styles.css"

# Les trois portées de thème du fichier, dans l'ordre où elles se recouvrent :
#   1. `:root`                          — le thème clair, base de tout ;
#   2. `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }` — le sombre du
#      système ;
#   3. `:root[data-theme="dark"]`       — le sombre **forcé par le bouton de thème** de la page.
# La troisième est celle que la story 2.4 avait oubliée : elle redéclare tout, et une variable
# ajoutée aux deux premières seulement y garde la valeur claire sur un fond sombre.
PORTEES = ("clair", "sombre_systeme", "sombre_force")

SEUIL_AA = 4.5


# --- lecture de la feuille ------------------------------------------------

def _bloc(source: str, ouverture: str) -> str:
    """Le corps d'un bloc CSS, depuis son sélecteur jusqu'à l'accolade fermante appariée."""
    debut = source.index(ouverture) + len(ouverture)
    profondeur = 1
    i = debut
    while profondeur:
        if source[i] == "{":
            profondeur += 1
        elif source[i] == "}":
            profondeur -= 1
        i += 1
    return source[debut:i - 1]


def _variables(corps: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", corps))


@pytest.fixture(scope="module")
def feuille() -> str:
    return Path(FEUILLE).read_text("utf-8")


@pytest.fixture(scope="module")
def themes(feuille: str) -> dict[str, dict[str, str]]:
    """Les variables des trois portées : la 2 et la 3 recouvrent la 1, elles ne la remplacent pas."""
    clair = _variables(_bloc(feuille, ":root {"))
    media = _bloc(feuille, "@media (prefers-color-scheme: dark) {")
    systeme_explicite = _variables(_bloc(media, ':root:not([data-theme="light"]) {'))
    force_explicite = _variables(_bloc(feuille, ':root[data-theme="dark"] {'))
    systeme = dict(clair, **systeme_explicite)
    force = dict(clair, **force_explicite)
    # Conserver les déclarations **explicites** est indispensable au contrôle de symétrie : dans
    # une carte fusionnée, une variable sombre forcée oubliée semble encore « présente », héritée
    # du clair. Les trois cartes résolues restent celles utilisées par les calculs de couleur.
    return {"clair": clair, "sombre_systeme": systeme, "sombre_force": force,
            "_sombre_systeme_explicite": systeme_explicite,
            "_sombre_force_explicite": force_explicite}


# --- résolution des couleurs ---------------------------------------------

def _hex(valeur: str) -> tuple[float, float, float]:
    v = valeur.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def resoudre(valeur: str, variables: dict[str, str],
             sous: tuple[float, float, float] | None = None) -> tuple[float, float, float]:
    """Une déclaration de couleur, rendue en RVB — `var()` et `color-mix(… , transparent)` compris.

    `color-mix(in srgb, X n%, transparent)` est un aplat de X à `n` % d'opacité : sur une surface
    opaque, il **compose**, et c'est cette composition qui décide du contraste réel. Le lire comme
    la couleur pure ferait passer un badge que le navigateur affiche bien plus pâle.
    """
    v = valeur.strip()
    if v.startswith("var("):
        nom = v[4:v.index(")")].split(",")[0].strip()
        if nom not in variables:
            raise AssertionError(f"variable inconnue de la feuille : {nom} — "
                                 "une variable inventée ne suit aucune portée de thème")
        return resoudre(variables[nom], variables, sous)
    m = re.match(r"color-mix\(in srgb,\s*(.+?)\s+([\d.]+)%,\s*transparent\s*\)$", v)
    if m:
        assert sous is not None, "un aplat translucide doit être composé sur une surface"
        teinte = resoudre(m.group(1), variables, sous)
        part = float(m.group(2)) / 100
        return tuple(teinte[i] * part + sous[i] * (1 - part) for i in range(3))  # type: ignore[return-value]
    if v.startswith("#"):
        return _hex(v)
    raise AssertionError(f"couleur non modélisée : {v}")


def _luminance(rvb: tuple[float, float, float]) -> float:
    def canal(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (canal(x) for x in rvb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contraste(avant: tuple[float, float, float], arriere: tuple[float, float, float]) -> float:
    """Le rapport WCAG 2.x brut ; l'arrondi est réservé au diagnostic, jamais à la décision."""
    a, b = sorted((_luminance(avant), _luminance(arriere)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def _exiger_aa(mesure: float, diagnostic: str) -> None:
    assert mesure >= SEUIL_AA, (
        f"{diagnostic} : {mesure:.2f}:1, sous les {SEUIL_AA}:1 d'AA")


def _exiger_memes_sombres(systeme: dict[str, str], force: dict[str, str]) -> None:
    for nom, valeur in sorted(systeme.items()):
        assert nom in force, f"{nom} : variante absente du thème sombre forcé"
        assert force[nom] == valeur, (
            f"{nom} : le sombre forcé ({force[nom]}) diverge du sombre système ({valeur})")


def declaration(feuille: str, selecteur: str, propriete: str) -> str | None:
    """La valeur d'une propriété dans **la** règle qui porte ce sélecteur exact."""
    for corps in re.findall(re.escape(selecteur) + r"\s*\{([^}]*)\}", feuille):
        trouve = re.search(rf"(?<![\w-]){propriete}\s*:\s*([^;]+)", corps)
        if trouve:
            return trouve.group(1).strip()
    return None


# --- les couleurs d'état du chat -----------------------------------------
#
# Chacune est un triplet (sélecteur, surface sur laquelle elle est peinte). La surface est celle de
# la bulle du serveur — `.msg.bot { background: var(--surface-2) }` —, sauf pour le badge de mode,
# qui vit dans l'en-tête de l'onglet et du widget, donc sur `--surface`.
ETATS_DU_CHAT = [
    (".msg .etat-sur", "--surface-2"),
    (".msg .etat-partiel", "--surface-2"),
    (".msg .etat-inconnu", "--surface-2"),
    (".msg .etat-local", "--surface-2"),
    (".msg .langue-repli", "--surface-2"),
    (".msg .degrade", "--surface-2"),
    (".msg.bot.indispo .alerte-titre", "--surface-2"),
    (".msg.bot.err .alerte-titre", "--surface-2"),
    (".msg .pq-ok", "--surface-2"),
    (".msg .pq-ko", "--surface-2"),
    (".badge.on", "--surface"),
    (".badge.off", "--surface"),
    # Les pastilles du volet sinistre du guide : un **fond plein** pris dans une variable de thème,
    # et une lettre par-dessus. C'est le cas où la substitution du littéral `#be123c` par `var(--ko)`
    # aurait remplacé une dette par une autre — la teinte sombre de `--ko` est claire, et une lettre
    # blanche y tombait à 2,73:1 (celle de `.pastille.ok` y était déjà à 2,02:1).
    (".pastille.ok", "--surface"),
    (".pastille.cond", "--surface"),
    (".pastille.ko", "--surface"),
    (".pastille.nf", "--surface"),
]


def _couleurs(feuille: str, selecteur: str, surface_var: str,
              variables: dict[str, str]) -> tuple[tuple[float, float, float],
                                                  tuple[float, float, float]]:
    """Le couple (texte, fond réellement peint) d'une règle, résolu dans une portée de thème."""
    surface = resoudre(f"var({surface_var})", variables)
    fond_declare = declaration(feuille, selecteur, "background")
    fond = resoudre(fond_declare, variables, surface) if fond_declare else surface
    couleur_declaree = declaration(feuille, selecteur, "color")
    # Les quatre `.pastille.<état>` n'écrivent que leur fond : la lettre hérite légitimement de la
    # règle commune `.pastille`. Le calcul doit suivre cette cascade minimale, faute de quoi le test
    # échoue sur une déclaration parfaitement appliquée par le navigateur au lieu de mesurer son
    # contraste.
    if not couleur_declaree and selecteur.startswith(".pastille."):
        couleur_declaree = declaration(feuille, ".pastille", "color")
    assert couleur_declaree, f"{selecteur} ne déclare aucune couleur de texte"
    return resoudre(couleur_declaree, variables, fond), fond


@pytest.mark.parametrize(("selecteur", "surface"), ETATS_DU_CHAT)
@pytest.mark.parametrize("portee", PORTEES)
def test_chaque_couleur_detat_du_chat_tient_le_seuil_aa(
        feuille: str, themes: dict[str, dict[str, str]], portee: str, selecteur: str,
        surface: str) -> None:
    """AC de la story : « chaque couleur d'état du chat atteint 4,5:1 sur sa surface », dans les
    **trois** portées de thème. Un échec est rouge, jamais un avertissement."""
    texte, fond = _couleurs(feuille, selecteur, surface, themes[portee])
    mesure = contraste(texte, fond)
    _exiger_aa(mesure, f"{selecteur} en thème {portee}")


def test_un_ratio_4496_ne_passe_pas_par_arrondi() -> None:
    """4,496 s'affiche « 4,50 » au centième, mais reste sous le seuil normatif de 4,5."""
    with pytest.raises(AssertionError, match="sous les 4.5"):
        _exiger_aa(4.496, "cas limite")


def test_les_trois_portees_de_theme_existent_et_redefinissent_les_memes_variables(
        themes: dict[str, dict[str, str]]) -> None:
    """Le défaut de 2.4 n'était pas une couleur mal choisie : c'était une variable qui n'existait que
    dans deux portées sur trois. Les deux portées sombres doivent redéfinir **le même jeu**, sans
    quoi l'une des deux garde silencieusement une valeur claire sur un fond sombre."""
    clair = themes["clair"]
    systeme = themes["_sombre_systeme_explicite"]
    force = themes["_sombre_force_explicite"]
    etats = {"--ok", "--warn", "--ko", "--surface", "--surface-2", "--text-2", "--muted", "--accent"}
    assert etats <= set(clair), sorted(etats - set(clair))
    assert etats <= set(systeme) and etats <= set(force)
    # Toute variable de couleur que le sombre du système redéfinit, le sombre forcé la redéfinit
    # aussi — et à la même valeur : c'est le **même** thème, atteint par deux chemins.
    _exiger_memes_sombres(systeme, force)


def test_une_variable_sombre_forcee_manquante_est_detectee() -> None:
    with pytest.raises(AssertionError, match="variante absente"):
        _exiger_memes_sombres({"--ko": "#f4738f"}, {})


def test_aucune_declaration_ne_porte_plus_le_litteral_du_rouge_derreur(feuille: str) -> None:
    """`#be123c` n'avait **aucune** variante sombre : sur `--surface` en thème sombre, il tombait à
    2,88:1. Huit déclarations le gardaient (le comparateur et la page de recommandation), hors du
    périmètre de la story 1.7 qui avait introduit `--ko`. Une couleur écrite en dur échappe par
    construction au contrôle ci-dessus, qui ne sait résoudre que les variables des trois portées.

    Il ne survit nulle part, pas même dans la définition de `--ko` : la valeur claire a été
    reprise avec, pour tenir 4,5:1 là où elle sert de couleur d'état."""
    # Les commentaires sont retirés d'abord : ils **parlent** de ce littéral, ils ne le peignent pas.
    sans_commentaires = re.sub(r"/\*.*?\*/", "", feuille, flags=re.S)
    occurrences = [ligne.strip() for ligne in sans_commentaires.splitlines() if "#be123c" in ligne]
    assert occurrences == [], occurrences


def test_le_rouge_derreur_a_bien_une_variante_dans_les_trois_portees(
        themes: dict[str, dict[str, str]]) -> None:
    valeurs = {portee: themes[portee]["--ko"] for portee in PORTEES}
    assert valeurs["clair"] != valeurs["sombre_systeme"], valeurs
    assert valeurs["sombre_systeme"] == valeurs["sombre_force"], valeurs


def test_la_teinte_de_fond_dun_badge_detat_ne_mange_pas_son_contraste(
        feuille: str, themes: dict[str, dict[str, str]]) -> None:
    """Le fond teinté d'un badge est un aplat de **sa propre couleur** : il rapproche le fond du
    texte, donc il retire du contraste. Mesuré : `.etat-partiel` à 14 % tombait à 4,42:1 en thème
    clair. Ce test garde la marge, pour qu'un « 14 % » remis par habitude rougisse."""
    for selecteur in (".msg .etat-sur", ".msg .etat-partiel"):
        fond = declaration(feuille, selecteur, "background")
        assert fond and "color-mix" in fond, selecteur
        part = float(re.search(r"([\d.]+)%", fond).group(1))
        assert part <= 10, f"{selecteur} : aplat à {part} %, au-delà de ce que le contraste tient"
        sans_teinte = contraste(*_couleurs(feuille, selecteur, "--surface-2", themes["clair"]))
        _exiger_aa(sans_teinte, selecteur)


def test_les_pictogrammes_de_controle_ne_portent_pas_seuls_leur_information(feuille: str) -> None:
    """Le contraste ne suffit pas : une information portée par la **seule** couleur est invisible
    pour qui ne la distingue pas. `.pq-ok` / `.pq-ko` ne sont donc pas des pastilles nues — ils
    portent un pictogramme, marqué `aria-hidden`, et l'état est écrit en toutes lettres à côté
    (composé par `chat.js`, asserté par `tests/test_web_chat.py`)."""
    chat = (REPO_ROOT / "web" / "app" / "chat.js").read_text("utf-8")
    assert '"pq-ok" : "pq-ko"' in chat or "pq-ok" in chat
    assert '{ "aria-hidden": "true" }' in chat
    # Et la feuille ne leur donne ni fond ni forme : ce sont des caractères, pas des pastilles.
    for selecteur in (".msg .pq-ok", ".msg .pq-ko"):
        assert declaration(feuille, selecteur, "background") is None, selecteur


@pytest.mark.parametrize("nom", ["--muted", "--text-2", "--text", "--accent", "--ok", "--warn",
                                 "--ko"])
@pytest.mark.parametrize("portee", PORTEES)
def test_les_teintes_de_texte_du_theme_tiennent_le_seuil_sur_la_bulle(
        themes: dict[str, dict[str, str]], portee: str, nom: str) -> None:
    """Les couleurs d'état ne suffisent pas : le texte d'appoint du chat (`--muted` : le pied, la
    preuve chiffrée, les titres de rubrique du panneau) doit se lire lui aussi.

    Il ne se lisait pas en thème clair — 4,36:1 sur `--surface-2` pour une étiquette de 11 px en
    capitales — parce que rien ne le mesurait. Ce test l'exige désormais dans les trois portées."""
    mesure = contraste(resoudre(f"var({nom})", themes[portee]),
                       resoudre("var(--surface-2)", themes[portee]))
    _exiger_aa(mesure, f"{nom} en thème {portee}")


def test_les_badges_neutres_du_chat_ne_dependent_plus_du_gris_dappoint(feuille: str) -> None:
    """Là où `--muted` portait un **état** — les badges « inconnu » et « recherche simple », qui
    n'ont pas de couleur propre —, il est remplacé par `--text-2`, la teinte du corps de la bulle :
    un badge est une étiquette, pas une mention d'appoint."""
    for selecteur in (".msg .etat-inconnu", ".msg .etat-local"):
        assert declaration(feuille, selecteur, "color") == "var(--text-2)", selecteur


def test_le_panneau_pourquoi_est_style_et_ne_pose_aucune_couleur_en_dur(feuille: str) -> None:
    """Le panneau de la story ajoute sept classes. Aucune ne doit porter de littéral : c'est
    exactement ce qui avait laissé passer le rouge sans variante sombre."""
    classes = [".msg .pourquoi", ".msg .pq-titre", ".msg .pq-ligne", ".msg .pq-ko", ".msg .pq-ok",
               ".msg .pq-seuils", ".msg .retrait"]
    for selecteur in classes:
        assert re.search(re.escape(selecteur) + r"\s*[,{]", feuille), f"{selecteur} n'est pas stylé"
    debut = feuille.index("/* ---------- « Pourquoi cette réponse » (story 2.5) ---------- */")
    fin = feuille.index("/* La mention de confidentialite", debut)
    bloc = feuille[debut:fin]
    litteraux = re.findall(r"#[0-9a-fA-F]{3,8}\b", bloc)
    assert not litteraux, f"couleurs en dur dans le panneau : {litteraux}"
    # Et le `<summary>` reste une commande prenable au doigt : la règle tactile du fichier le pose
    # pour **tout** `summary`, celui du panneau compris.
    assert "summary { min-height: 44px" in feuille


def test_la_feuille_est_bien_celle_que_la_page_charge() -> None:
    """Un test qui lirait une feuille que personne ne sert ne prouverait rien."""
    html = (REPO_ROOT / "web" / "index.html").read_text("utf-8")
    assert re.search(r'href="app/styles\.css\?v=\d+"', html), html[:400]
    assert FEUILLE.exists()
