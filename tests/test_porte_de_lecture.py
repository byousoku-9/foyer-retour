"""La porte de lecture : titre courant tourné, glyphe détaché, et la vraie gouttière.

Corpus **synthétique et neutre** : des pages fabriquées ici, avec `pymupdf`, dont les textes
s'appellent `G…`, `D…`, `Rubrique`, `Repere`. Aucun assureur, aucun titre de contrat réel, aucun
numéro de page emprunté à un document existant : les trois règles éprouvées ici sont géométriques,
et un corpus qui les nommerait par leur texte prouverait le contraire de ce qu'il prétend.

Quatre défauts de la porte de lecture, et pour chacun une **mutation** : la correction neutralisée
doit faire rougir un témoin, sans quoi il serait vert pour d'autres raisons que la sienne.

1. Un titre courant composé **tourné** dans une marge latérale n'est dans aucune bande haute ou
   basse : il traversait la porte, et atterrissait au milieu du flux d'une colonne, entre deux
   moitiés de phrase. Il est retiré dès que son orientation **et** sa récurrence de page en page —
   ses nombres masqués — sont réunies ; une ligne tournée unique dans tout le document reste du
   contenu.
2. Un glyphe seul — puce, point-virgule — sorti dans une ligne à part est rendu à la ligne dont il
   partage la bande, devant s'il est à sa gauche, derrière s'il est à sa droite. Un glyphe qui ne
   partage la bande d'aucune ligne reste une ligne : il n'y a rien à réparer.
3. La frontière retenue est celle qui traverse le moins de contenu, et seulement à égalité la plus
   large. Ne classer que sur la largeur du blanc faisait gagner un retrait intérieur à une colonne
   contre la gouttière de la page.
4. Une gouttière serrée se prouve par des flux natifs **disjoints**, pas par leur **nombre** : une
   colonne que l'extracteur rend d'un seul tenant était rejetée pour n'être pas assez morcelée.
"""

from __future__ import annotations

from math import ceil
from pathlib import Path

import pymupdf
import pytest

from server.app.config import get_settings
from server.ingest import pdf_to_blocks as p

FONT = "helv"
LARGEUR, HAUTEUR = 595.0, 842.0
# Hors de toute bande haute (40 pt) ou basse (842 - 40 pt), à mi-hauteur : la marge droite pour la
# ligne tournée, le milieu de la page pour son jumeau posé droit — aucune des deux n'est en bande.
MARGE_X, MILIEU_X, MARGE_Y = 566.0, 200.0, 470.0


def _ecrire(page: pymupdf.Page, x: float, y: float, texte: str, *, size: float = 10.0,
            rotate: int = 0) -> None:
    if "•" in texte:  # `insert_text` (base-14) dégrade « • » en « · » ; `TextWriter` conserve la puce
        writer = pymupdf.TextWriter(page.rect)
        writer.append((x, y), texte, font=pymupdf.Font(FONT), fontsize=size)
        writer.write_text(page)
        return
    page.insert_text((x, y), texte, fontsize=size, fontname=FONT, rotate=rotate)


# Un texte assez large pour que chaque colonne remplisse la largeur dont elle dispose : une colonne
# qui n'occuperait qu'une fraction de sa place est une **rangée** « libellé … montant », et la garde
# à deux signaux du parseur l'écarterait — ce n'est pas ce qui est éprouvé ici.
CORPS = "ligne de colonne assez large pour remplir sa"


def _colonnes(page: pymupdf.Page, *, depart_gauche: float, depart_droite: float, n: int = 8) -> None:
    """Deux colonnes de texte, dont la droite peut commencer plus haut ou plus bas que la gauche."""
    for i in range(n):
        _ecrire(page, 51.0, depart_gauche + i * 40.0, f"G{i} {CORPS}")
        _ecrire(page, 304.0, depart_droite + i * 40.0, f"D{i} {CORPS}")


def _document(pages: int = 6) -> pymupdf.Document:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=LARGEUR, height=HAUTEUR)
    return doc


def _extraire(doc: pymupdf.Document, tmp_path: Path) -> list[p.PageText]:
    chemin = tmp_path / "source.pdf"
    doc.save(chemin)
    doc.close()
    pages, _toc = p.extract_pages(chemin)
    p.ordonner_pages(pages)
    return pages


def _textes(pages: list[p.PageText]) -> list[str]:
    return [line.text for page in pages for line in page.lines]


# --- 1. Le titre courant tourné -----------------------------------------------------------------

def _pdf_titre_courant(tmp_path: Path, *, unique: bool = False, tourne: bool = True) -> list[p.PageText]:
    """Six pages de corps ; un titre courant en marge droite, répété ou posé une seule fois."""
    doc = _document()
    for numero, page in enumerate(doc, start=1):
        for i in range(6):
            _ecrire(page, 51.0, 120.0 + i * 40.0, f"Corps de la page {numero}, ligne {i}.")
        if unique and numero != 3:
            continue
        _ecrire(page, MARGE_X if tourne else MILIEU_X, MARGE_Y,
                f"Rubrique courante  {numero} | 6", rotate=90 if tourne else 0)
    return _extraire(doc, tmp_path)


def test_un_titre_courant_tourne_et_repete_est_retire_ou_quil_soit_pose(tmp_path: Path) -> None:
    """AC 1 : hors de toute bande, l'orientation et la récurrence suffisent — et sont exigées."""
    pages = _pdf_titre_courant(tmp_path)
    assert not any("Rubrique courante" in texte for texte in _textes(pages))
    # Retiré, donc compté : `en_tetes_retires` somme `page.removed`.
    assert [page.removed for page in pages] == [[f"Rubrique courante  {n} | 6"] for n in range(1, 7)]
    motifs = {motif for page in pages for motif in page.source.removed.values()}
    assert motifs == {"titre_courant_tourne"}


def test_une_ligne_tournee_unique_dans_le_document_reste_du_contenu(tmp_path: Path) -> None:
    """AC 1, l'autre côté : sans récurrence, une ligne tournée est une mention, pas un titre courant."""
    pages = _pdf_titre_courant(tmp_path, unique=True)
    assert [texte for texte in _textes(pages) if "Rubrique courante" in texte] == \
        ["Rubrique courante  3 | 6"]
    assert [page.removed for page in pages] == [[] for _ in range(6)]


def test_un_texte_recurrent_pose_droit_hors_bande_reste_du_corps(tmp_path: Path) -> None:
    """AC 1, le second côté : la récurrence seule ne suffit pas — sinon un refrain du corps partirait."""
    pages = _pdf_titre_courant(tmp_path, tourne=False)
    assert len([texte for texte in _textes(pages) if "Rubrique courante" in texte]) == 6


def test_mutation_sans_le_masque_des_nombres_le_titre_courant_survit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation de la correction 1 : comparer les textes bruts fait de chaque page un titre unique."""
    monkeypatch.setattr(p, "_sans_numeros", lambda texte: texte)
    pages = _pdf_titre_courant(tmp_path)
    assert len([texte for texte in _textes(pages) if "Rubrique courante" in texte]) == 6


# --- 2. Le glyphe détaché de sa ligne -----------------------------------------------------------

def _pdf_glyphes(tmp_path: Path) -> list[p.PageText]:
    """Une puce posée un point plus bas que son texte, un `;` posé après le sien, un `;` seul.

    La puce **suit** son texte à l'extraction, `sort=True` ordonnant sur le haut de la boîte : c'est
    le défaut même, et le monter dans l'ordre attendu ferait de la sortie l'entrée.
    """
    doc = _document(pages=1)
    page = doc[0]
    _ecrire(page, 64.0, 200.0, "Rubrique annoncee par une puce")
    _ecrire(page, 51.0, 201.0, "•")
    _ecrire(page, 51.0, 260.0, "Enonce qui se termine par un signe")
    _ecrire(page, 240.0, 261.0, ";")
    _ecrire(page, 51.0, 400.0, ";")  # seul dans sa bande : rien à réparer
    return _extraire(doc, tmp_path)


def test_un_glyphe_detache_revient_a_sa_ligne_a_la_place_que_sa_geometrie_lui_donne(
        tmp_path: Path) -> None:
    """AC 2 : la puce devant, le signe derrière, et le glyphe isolé conservé tel quel."""
    pages = _pdf_glyphes(tmp_path)
    assert _textes(pages) == [
        "• Rubrique annoncee par une puce",
        "Enonce qui se termine par un signe ;",
        ";",
    ]
    ligne = pages[0].lines[0]
    assert ligne.bullet and ligne.bbox[0] == pytest.approx(51.0, abs=1.0)
    # Le registre ne perd aucune ligne source : la ligne d'accueil porte les deux uid, dans l'ordre.
    assert [len(line.source_uids) for line in pages[0].lines] == [2, 2, 1]


def test_un_glyphe_seul_dans_sa_bande_reste_une_ligne(tmp_path: Path) -> None:
    """AC 2, l'autre côté : sans bande partagée, l'inventer serait une supposition."""
    pages = _pdf_glyphes(tmp_path)
    assert pages[0].lines[-1].text == ";" and len(pages[0].lines[-1].source_uids) == 1


def test_un_glyphe_ne_franchit_pas_une_gouttiere_pour_rejoindre_lautre_colonne(
        tmp_path: Path) -> None:
    """AC 2 : la colonne voisine partage la bande, mais la gouttière interdit la fusion."""
    doc = _document(pages=1)
    page = doc[0]
    # Colonnes décalées d'une demi-interligne : le `;` ne partage la bande **que** d'une ligne de
    # droite, et il est posé à gauche de la gouttière. Sans elle, il rejoindrait `D0`.
    _colonnes(page, depart_gauche=120.0, depart_droite=140.0)
    _ecrire(page, 280.0, 140.0, ";")
    pages = _extraire(doc, tmp_path)
    frontiere = pages[0].layout.boundaries
    assert frontiere == [304.0] and ";" in _textes(pages)
    assert not any(texte.endswith(" ;") for texte in _textes(pages))


def test_mutation_sans_la_fusion_des_glyphes_la_puce_reste_une_ligne_muette(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation de la correction 2 : la puce ressort en ligne à part, **après** le texte qu'elle annonce."""
    monkeypatch.setattr(p, "_fusionner_glyphes", lambda lines, tables=(): lines)
    pages = _pdf_glyphes(tmp_path)
    assert _textes(pages) == [
        "Rubrique annoncee par une puce",
        "•",
        "Enonce qui se termine par un signe",
        ";",
        ";",
    ]


# --- 3. La frontière qui traverse le moins de contenu -------------------------------------------

def _pdf_fausse_gouttiere(tmp_path: Path, *, depart_droite: float) -> list[p.PageText]:
    """Une gouttière de page étroite, et dans la colonne de droite un retrait plus large qu'elle.

    Le retrait n'existe que sur quelques lignes : la frontière qu'il offre traverse toutes les
    autres lignes de la colonne. Classée sur la seule largeur du blanc, elle gagne ; classée sur le
    contenu qu'elle coupe, elle perd — et c'est la gouttière de la page qui est retenue.
    """
    doc = _document(pages=1)
    page = doc[0]
    _colonnes(page, depart_gauche=120.0, depart_droite=depart_droite)
    for i in range(6):
        _ecrire(page, 360.0, 470.0 + i * 50.0, f"R{i} ligne en retrait")
    return _extraire(doc, tmp_path)


def _fausse_gouttiere_plus_large(page: p.PageText) -> None:
    """Le retrait offre bien plus de blanc que la gouttière de page : le témoin n'est pas creux."""
    minimum = get_settings().column_min_lines
    boites = p._boites(page.lines, page.tables)
    mesures: dict[float, tuple[float, int]] = {}
    for candidate in sorted({b.bbox[0] for b in boites}):
        gauche = [b for b in boites if b.bbox[2] <= candidate]
        droite = [b for b in boites if b.bbox[0] >= candidate]
        if len(gauche) < minimum or len(droite) < minimum:
            continue
        mesures[candidate] = (min(b.bbox[0] for b in droite) - max(b.bbox[2] for b in gauche),
                              sum(1 for b in boites if b.bbox[0] < candidate < b.bbox[2]))
    retenue = page.layout.boundaries[0]
    assert mesures[retenue][1] == 0, "la frontière retenue ne coupe aucune boîte"
    assert max(largeur for largeur, _ in mesures.values()) > mesures[retenue][0], \
        "une candidate plus large existe : le témoin discrimine bien le classement"


@pytest.mark.parametrize("depart_droite", [90.0, 120.0, 180.0])
def test_la_frontiere_retenue_est_celle_qui_traverse_le_moins_de_contenu(
        tmp_path: Path, depart_droite: float) -> None:
    """AC 3 : la droite commence plus haut, à la même hauteur ou plus bas — même gouttière retenue."""
    pages = _pdf_fausse_gouttiere(tmp_path, depart_droite=depart_droite)
    page = pages[0]
    assert len(page.layout.boundaries) == 1
    _fausse_gouttiere_plus_large(page)
    assert {line.colonne for line in page.lines} == {1, 2}


@pytest.mark.parametrize("depart_droite", [90.0, 120.0, 180.0])
def test_lordre_est_colonne_majeur_dans_la_zone_quel_que_soit_le_depart_des_colonnes(
        tmp_path: Path, depart_droite: float) -> None:
    """AC 3 : la lecture épuise la colonne de gauche avant d'entamer la droite, sans entrelacement."""
    pages = _pdf_fausse_gouttiere(tmp_path, depart_droite=depart_droite)
    prefixes = [line.text[:2] for line in pages[0].lines]
    assert prefixes == [f"G{i}" for i in range(8)] + [f"D{i}" for i in range(8)] + \
        [f"R{i}" for i in range(6)]


def test_une_ligne_pleine_largeur_ouvre_une_zone_et_se_lit_au_dessus_de_ses_colonnes(
        tmp_path: Path) -> None:
    """AC 3 : la zone est la maille de la garantie — colonne-majeur **à l'intérieur** d'une zone."""
    doc = _document(pages=1)
    page = doc[0]
    for i in range(6):
        _ecrire(page, 51.0, 120.0 + i * 40.0, f"G{i} {CORPS}")
        _ecrire(page, 304.0, 120.0 + i * 40.0, f"D{i} {CORPS}")
    _ecrire(page, 51.0, 400.0, "T0 intertitre qui traverse toute la largeur ecrite de la page", size=12)
    for i in range(6):
        _ecrire(page, 51.0, 440.0 + i * 40.0, f"H{i} {CORPS}")
        _ecrire(page, 304.0, 440.0 + i * 40.0, f"K{i} {CORPS}")
    pages = _extraire(doc, tmp_path)
    prefixes = [line.text[:2] for line in pages[0].lines]
    assert prefixes == [f"G{i}" for i in range(6)] + [f"D{i}" for i in range(6)] + ["T0"] + \
        [f"H{i}" for i in range(6)] + [f"K{i}" for i in range(6)]
    intertitre = next(line for line in pages[0].lines if line.text.startswith("T0"))
    assert intertitre.colonne == 0 and intertitre.bande == 1


def test_une_boite_qui_ne_traverse_quune_gouttiere_sur_deux_nest_pas_pleine_largeur() -> None:
    """AC 3 : pleine largeur veut dire d'un bord à l'autre — sinon c'est la colonne de départ.

    Sur deux colonnes — une seule frontière —, « toutes » et « une » se confondent : le
    comportement historique y est inchangé, et c'est ce que la seconde moitié vérifie.
    """
    trois = p.PageLayout(boundaries=[200.0, 400.0])
    assert trois.colonne([50.0, 0.0, 500.0, 10.0]) == 0  # traverse les deux
    assert trois.colonne([250.0, 0.0, 500.0, 10.0]) == 2  # colonnes 2 et 3 : sa colonne de départ
    assert trois.colonne([50.0, 0.0, 300.0, 10.0]) == 1  # colonnes 1 et 2 : sa colonne de départ
    assert trois.colonne([250.0, 0.0, 300.0, 10.0]) == 2
    deux = p.PageLayout(boundaries=[200.0])
    assert deux.colonne([50.0, 0.0, 500.0, 10.0]) == 0 and deux.colonne([250.0, 0.0, 300.0, 10.0]) == 2


# --- 4. Une gouttière serrée se prouve par la disjonction, pas par le morcellement ---------------

def _page_serree_dun_seul_tenant(*, morcelee: bool = False) -> p.PageText:
    """Deux colonnes séparées par 4 pt de blanc, chaque côté rendu par **un seul** bloc natif.

    C'est le cas le plus favorable qui soit : l'extracteur a lui-même reconnu de chaque côté un flux
    unique, qui s'arrête à la gouttière. `morcelee=True` rend les mêmes colonnes en six blocs par
    côté — la même page, la même géométrie, une provenance seulement plus fragmentée.
    """
    lignes: list[p.PageLine] = []
    for i in range(6):
        y = 100.0 + i * 90.0
        lignes += [
            p.PageLine(f"G{i} corps de la colonne gauche.", [51.0, y, 300.0, y + 12.0], 10.0,
                       source_blocks=[f"p1:b-gauche-{i}" if morcelee else "p1:b-gauche"]),
            p.PageLine(f"D{i} corps de la colonne droite.", [304.0, y, 545.0, y + 12.0], 10.0,
                       source_blocks=[f"p1:b-droite-{i}" if morcelee else "p1:b-droite"]),
        ]
    lignes.sort(key=lambda line: (line.bbox[1], line.bbox[0]))  # l'ordre que rend l'extraction
    return p.PageText(page=1, width=595, height=842, lines=lignes)


@pytest.mark.parametrize("morcelee", [False, True])
def test_une_gouttiere_serree_se_prouve_par_des_flux_disjoints_et_non_par_leur_nombre(
        morcelee: bool) -> None:
    """AC 4 : un côté d'un seul tenant prouve la séparation aussi bien que six blocs.

    La paire est le témoin de mutation : rétablir « assez de blocs source de chaque côté » laisse
    `morcelee=True` vert et fait rougir `morcelee=False`, sur la **même** page et la même géométrie.
    Seule la provenance change — c'est donc bien elle qui décidait, et le nombre ne prouvait rien.
    """
    page = _page_serree_dun_seul_tenant(morcelee=morcelee)
    p.ordonner_pages([page])
    assert page.layout.boundaries == [304.0]
    assert [line.text[:2] for line in page.lines] == [f"G{i}" for i in range(6)] + \
        [f"D{i}" for i in range(6)]


def test_un_flux_natif_qui_franchit_le_blanc_refuse_toujours_la_gouttiere_serree() -> None:
    """AC 4, l'autre côté : la disjonction reste exigée — un bloc à cheval interdit la frontière."""
    page = _page_serree_dun_seul_tenant()
    for ligne in page.lines:
        ligne.source_blocks = ["p1:b-unique"]
    p.ordonner_pages([page])
    assert page.layout.boundaries == []


def test_mutation_la_plus_large_gouttiere_dabord_ramene_lentrelacement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation de la correction 3 : le classement historique, et le défaut revient tel quel."""
    # Le classement d'avant, à l'identique : la largeur du blanc, puis la frontière la plus à gauche.
    monkeypatch.setattr(p, "_merite_de_frontiere",
                        lambda traversees, gutter, candidate: (gutter, -candidate))
    pages = _pdf_fausse_gouttiere(tmp_path, depart_droite=120.0)
    prefixes = [line.text[:2] for line in pages[0].lines]
    assert prefixes != [f"G{i}" for i in range(8)] + [f"D{i}" for i in range(8)] + \
        [f"R{i}" for i in range(6)]


# --- Ce que la porte laisse passer, mesurable sur n'importe quelle ingestion réelle --------------

def mesure_de_la_porte(pages: list[p.PageText]) -> dict[str, int]:
    """Les quatre grandeurs que les corrections de la porte de lecture rendent mesurables.

    Aucune ne nomme un document : chacune se lit sur les pages d'une ingestion quelconque. Les deux
    certificats « PDF réel » s'en servent pour épingler la vérité **régénérée** de leur contrat, sans
    rien comparer à un artefact committé — ils restent donc exécutés quand l'empreinte est périmée.
    """
    minimum = max(2, ceil(len(pages) * get_settings().header_min_pages_ratio))
    vues: dict[str, set[int]] = {}
    for page in pages:
        for line in page.lines:
            if line.tournee:
                vues.setdefault(p._sans_numeros(line.text), set()).add(page.page)
    parasites = 0
    for page in pages:
        if not page.lines:
            continue
        marge = min(line.bbox[0] for line in page.lines)
        # Une ligne pleine largeur part de la marge de texte de sa page. Une ligne de colonne que la
        # détection aurait déclarée pleine largeur part, elle, de sa propre colonne : c'est le défaut.
        parasites += sum(1 for line in page.lines if line.colonne == 0 and line.bbox[0] > marge + 1)
    return {
        "lignes_tournees_conservees": sum(1 for page in pages for line in page.lines if line.tournee),
        "titres_courants_survivants": sum(
            1 for page in pages for line in page.lines
            if line.tournee and len(vues.get(p._sans_numeros(line.text), ())) >= minimum),
        "lignes_glyphes": sum(1 for page in pages for line in page.lines
                              if not p._ALPHANUM_RE.search(line.text)),
        "lignes_pleine_largeur_hors_marge": parasites,
        "pages_a_gouttiere": sum(1 for page in pages if page.layout.multi),
    }
