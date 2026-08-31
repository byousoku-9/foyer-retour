"""Story 4.2c — les colonnes du corps se détectent par la seule géométrie.

Corpus **synthétique et neutre** : les lignes s'appellent `G1…` et `D1…` (gauche, droite), aucun
assureur, aucun document, aucune page réelle. Ce que ces tests prouvent :

- sans gouttière retenue, **rien ne bouge** — l'ordre de lecture est l'ordre d'extraction, à
  l'identique, jusqu'aux octets de `document.json` et de `summary.md` ;
- avec une gouttière, la lecture épuise une colonne avant d'entamer la suivante, et aucun bloc
  accepté ne mêle deux colonnes ni deux bandes ;
- un titre pleine largeur ouvre une bande et se lit au-dessus des colonnes qu'il coiffe ;
- la table des matières, absente ou discordante, ne structure jamais l'arbre.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.app.domain import Document
from server.ingest import pdf_to_blocks as p

DOC = "doc-colonnes"
# Deux colonnes franches sur une page A4 : gouttière de 60 pt entre x=270 et x=330.
GAUCHE_X, DROITE_X = 56.0, 330.0


def _ligne(text: str, x: float, y: float, *, largeur: float = 200.0, size: float = 10.0,
           **kwargs) -> p.PageLine:
    return p.PageLine(text, [x, y, x + largeur, y + 12.0], size, **kwargs)


def _colonne(prefixe: str, x: float, *, depart: float = 100.0, n: int = 6, pas: float = 90.0,
             largeur: float = 200.0) -> list[p.PageLine]:
    """Une colonne haute : `column_min_lines` lignes au moins, sur toute la hauteur écrite."""
    return [_ligne(f"{prefixe}{i} texte de la colonne.", x, depart + i * pas, largeur=largeur)
            for i in range(n)]


def _extrait(*groupes: list[p.PageLine]) -> list[p.PageLine]:
    """L'ordre que rend réellement l'extraction : le tri `(y0, x0)` de `get_text(sort=True)`.

    **Aucune fixture ne part de l'ordre attendu.** Deux colonnes s'extraient entrelacées
    (`G0 D0 G1 D1 …`) — c'est le défaut même que la story corrige. Monter les lignes déjà triées
    colonne par colonne ferait de la sortie attendue l'entrée : le réordonnancement ne serait alors
    jamais exercé, et neutraliser le tri final laisserait ces tests verts.
    """
    return sorted([line for groupe in groupes for line in groupe],
                  key=lambda line: (line.bbox[1], line.bbox[0]))


def _page_deux_colonnes(*, tete: list[p.PageLine] | None = None, page: int = 1) -> p.PageText:
    lines = _extrait(tete or [], _colonne("G", GAUCHE_X), _colonne("D", DROITE_X))
    return p.PageText(page=page, width=595, height=842, lines=lines)


def _page_libelle_montant(*, n: int = 8) -> p.PageText:
    """Une liste « libellé … montant » : deux blancs verticaux francs, une seule colonne de lecture.

    Chaque montant est posé sur la ligne de base de son libellé et n'occupe qu'une fraction de la
    largeur dont il dispose — la signature d'une **rangée**. `find_tables()` ne voit rien ici : il
    n'y a ni trait ni fond, seulement de l'espace.
    """
    lines: list[p.PageLine] = []
    for i in range(n):
        y = 100.0 + i * 60.0
        lines.append(_ligne(f"Libelle {i} de la rangee courante", GAUCHE_X, y, largeur=244.0))
        lines.append(_ligne(f"{i}00,00", 470.0, y, largeur=50.0))
    return p.PageText(page=1, width=595, height=842, lines=_extrait(lines))


def _construire(pages: list[p.PageText]) -> Document:
    document, _meta = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[],
                                       doc_id=DOC, title="Contrat synthétique")
    return document


# --- Mono-colonne : identité stricte ------------------------------------------------------------

def test_une_page_sans_gouttiere_garde_lordre_dextraction_a_lidentique() -> None:
    """AC : aucune gouttière retenue ⇒ `ordre_lecture == ordre_source`, objets et ordre inchangés."""
    lines = [_ligne(f"Ligne {i} du paragraphe courant.", 56.0, 100.0 + i * 20.0) for i in range(12)]
    page = p.PageText(page=1, width=595, height=842, lines=list(lines))
    p.ordonner_pages([page])
    assert page.layout.boundaries == [] and not page.layout.multi
    assert [id(line) for line in page.lines] == [id(line) for line in lines]  # aucun réordonnancement
    assert [line.ordre_lecture for line in page.lines] == list(range(1, 13))
    assert {line.colonne for line in page.lines} == {1} and {line.bande for line in page.lines} == {0}


def test_deux_colonnes_trop_courtes_ou_trop_peu_peuplees_ne_sont_pas_retenues() -> None:
    """Une gouttière n'est retenue que si les deux côtés sont assez peuplés **et** assez hauts."""
    peu_peuple = p.PageText(page=1, width=595, height=842, lines=_extrait(
        _colonne("G", GAUCHE_X), _colonne("D", DROITE_X, n=2, pas=90.0),
    ))
    p.ordonner_pages([peu_peuple])
    assert peu_peuple.layout.boundaries == []  # column_min_lines
    trop_courte = p.PageText(page=1, width=595, height=842, lines=_extrait(
        _colonne("G", GAUCHE_X), _colonne("D", DROITE_X, depart=100.0, n=6, pas=12.0),
    ))
    p.ordonner_pages([trop_courte])
    assert trop_courte.layout.boundaries == []  # column_min_span_ratio


def _page_serree() -> p.PageText:
    return p.PageText(page=1, width=595, height=842, lines=_extrait(
        _colonne("G", GAUCHE_X, n=6), _colonne("D", 266.0, n=6),  # blanc de 10 pt seulement
    ))


def test_une_gouttiere_plus_etroite_que_le_seuil_nest_pas_une_colonne() -> None:
    serree = _page_serree()
    p.ordonner_pages([serree])
    assert serree.layout.boundaries == []


def test_des_blocs_source_independants_prouvent_deux_colonnes_serrees_sans_abaisser_le_seuil() -> None:
    """Une faible largeur de blanc ne suffit toujours pas ; la provenance native apporte la preuve.

    Les puces et leur corps partagent chaque bloc source gauche : une frontière au retrait couperait
    donc ces blocs et doit être refusée. La frontière entre colonnes garde au contraire six groupes
    indépendants par côté, deux côtés remplis et des départs largement séparés.
    """
    lignes: list[p.PageLine] = []
    for i in range(6):
        y = 100.0 + i * 90.0
        source_gauche = [f"p1:b-gauche-{i}"]
        lignes += [
            _ligne("•", GAUCHE_X, y, largeur=4.0, bullet=True, source_blocks=source_gauche),
            _ligne(f"G{i} corps de la colonne gauche.", 74.0, y, largeur=226.0,
                   source_blocks=source_gauche),
            _ligne(f"D{i} corps de la colonne droite.", 304.0, y, largeur=230.0,
                   source_blocks=[f"p1:b-droite-{i}"]),
        ]
    page = p.PageText(page=1, width=595, height=842, lines=_extrait(lignes))
    p.ordonner_pages([page])
    assert page.layout.boundaries == [304.0]
    assert [line.text[:1] for line in page.lines] == [value for _ in range(6) for value in ("•", "G")] \
        + ["D"] * 6
    assert p.get_settings().column_gutter_min_pt == 18.0  # blanc physique : 4 pt seulement


def test_une_frontiere_qui_coupe_un_bloc_source_est_refusee_meme_avec_un_blanc_franc() -> None:
    """Une puce et son corps restent un même groupe natif, jamais deux colonnes."""
    lignes: list[p.PageLine] = []
    for i in range(6):
        y = 100.0 + i * 90.0
        source = [f"p1:b{i}"]
        lignes += [
            _ligne("•", GAUCHE_X, y, largeur=4.0, bullet=True, source_blocks=source),
            _ligne(f"Texte {i} du même paragraphe.", 90.0, y, largeur=220.0,
                   source_blocks=source),
        ]
    page = p.PageText(page=1, width=595, height=842, lines=_extrait(lignes))
    p.ordonner_pages([page])
    assert page.layout.boundaries == []


def test_les_seuils_de_colonne_sont_des_reglages_publies_et_non_des_valeurs_en_dur(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Convention Seuils : les cinq bornes de colonne sont publiées, et chacune est un vrai levier."""
    publies = p.get_settings().thresholds()
    for nom in ("column_gutter_min_pt", "column_min_lines", "column_min_span_ratio",
                "column_row_pairing_max_ratio", "column_min_fill_ratio"):
        assert publies[nom] == getattr(p.get_settings(), nom)
    monkeypatch.setenv("COLUMN_GUTTER_MIN_PT", "8")
    p.get_settings.cache_clear()
    try:
        serree = _page_serree()
        p.ordonner_pages([serree])
        assert serree.layout.multi
        assert p.get_settings().thresholds()["column_gutter_min_pt"] == 8.0
    finally:
        p.get_settings.cache_clear()


def test_abaisser_le_remplissage_minimal_laisse_repasser_la_liste_libelle_montant(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Le second critère est bien `column_min_fill_ratio`, et non une géométrie écrite en dur."""
    monkeypatch.setenv("COLUMN_MIN_FILL_RATIO", "0.1")
    p.get_settings.cache_clear()
    try:
        rangees = _page_libelle_montant()
        p.ordonner_pages([rangees])
        assert rangees.layout.multi  # seule la borne retenait la gouttière
        assert p.get_settings().thresholds()["column_min_fill_ratio"] == 0.1
    finally:
        p.get_settings.cache_clear()


def test_un_corpus_mono_colonne_rend_les_memes_octets_avec_et_sans_detection(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La détection se désarme d'elle-même : la relever hors de portée ne change aucun artefact."""
    from tests.test_pdf_to_blocks import build_pdf, nominal_pages

    def _artefacts(dossier: Path) -> dict[str, bytes]:
        dossier.mkdir(parents=True)
        build_pdf(dossier / "source.pdf", pages=nominal_pages())
        (dossier / "source.url").write_text("https://example.test/contrat.pdf\n", "utf-8")
        p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat synthétique")
        return {name: (dossier / name).read_bytes() for name in ("document.json", "summary.md")}

    reference = _artefacts(tmp_path / "avec" / DOC)
    monkeypatch.setenv("COLUMN_MIN_LINES", "999")  # détection rendue impossible
    p.get_settings.cache_clear()
    try:
        desarmee = _artefacts(tmp_path / "sans" / DOC)
    finally:
        p.get_settings.cache_clear()
    document, autre = (json.loads(reference["document.json"]), json.loads(desarmee["document.json"]))
    # Blocs, ids, ordre, bbox et sommaire sont identiques ; seule l'empreinte bouge, et elle **doit**
    # bouger — les seuils géométriques changent l'ordre de lecture, donc les `block_id` (AD-2).
    assert autre["ingest_fingerprint"] != document["ingest_fingerprint"]
    for cle in ("blocks", "nodes"):
        assert autre[cle] == document[cle]
    corps = [summary.decode("utf-8").split("\n", 1)[1]  # la première ligne porte justement l'empreinte
             for summary in (desarmee["summary.md"], reference["summary.md"])]
    assert corps[0] == corps[1]


# --- Deux colonnes ------------------------------------------------------------------------------

def test_la_lecture_epuise_la_colonne_gauche_avant_la_colonne_droite() -> None:
    page = _page_deux_colonnes()
    # L'entrée est bien l'entrelacement de l'extraction, jamais la sortie attendue.
    assert [line.text[:2] for line in page.lines][:4] == ["G0", "D0", "G1", "D1"]
    p.ordonner_pages([page])
    assert page.layout.multi and len(page.layout.boundaries) == 1
    textes = [line.text[:2] for line in page.lines]
    assert textes == [f"G{i}" for i in range(6)] + [f"D{i}" for i in range(6)]
    assert [line.ordre_lecture for line in page.lines] == list(range(1, 13))
    assert {line.colonne for line in page.lines[:6]} == {1}
    assert {line.colonne for line in page.lines[6:]} == {2}


def test_aucun_bloc_accepte_ne_mele_deux_colonnes() -> None:
    """AC : toutes les lignes d'un bloc partagent la même colonne et la même bande."""
    document = _construire([_page_deux_colonnes()])
    for block in document.blocks:
        cotes = {"G" if line.text.startswith("G") else "D" for line in block.lines}
        assert len(cotes) == 1, block.text


def test_les_colonnes_ne_sont_pas_recollees_par_une_continuation_de_paragraphe() -> None:
    """Le bas d'une colonne et le haut de l'autre sont proches en `y` : la rupture est structurelle."""
    lines = _extrait(
        [_ligne(f"G{i} suite de phrase sans point final", GAUCHE_X, 100.0 + i * 20.0) for i in range(6)],
        [_ligne(f"D{i} suite de phrase sans point final", DROITE_X, 100.0 + i * 20.0) for i in range(6)],
    )
    page = p.PageText(page=1, width=595, height=842, lines=lines)
    document = _construire([page])
    fusionnes = [block for block in document.blocks
                 if any(line.text.startswith("G") for line in block.lines)
                 and any(line.text.startswith("D") for line in block.lines)]
    assert fusionnes == []


def test_une_puce_de_la_seconde_colonne_nallonge_pas_la_liste_de_la_premiere() -> None:
    lines = _extrait(
        [_ligne(f"• G{i} item de gauche", GAUCHE_X, 100.0 + i * 90.0, bullet=True) for i in range(6)],
        [_ligne(f"• D{i} item de droite", DROITE_X, 100.0 + i * 90.0, bullet=True) for i in range(6)],
    )
    page = p.PageText(page=1, width=595, height=842, lines=lines)
    document = _construire([page])
    listes = [block for block in document.blocks if block.kind == "list"]
    assert listes and all(
        len({"G" if line.text[2:3] == "G" else "D" for line in block.lines}) == 1 for block in listes
    )


def test_trois_colonnes_se_lisent_de_gauche_a_droite() -> None:
    """La récursion sur chaque côté n'est pas un cas particulier : elle tombe de la même règle."""
    page = p.PageText(page=1, width=595, height=842, lines=_extrait(
        _colonne("A", 40.0, largeur=150.0), _colonne("B", 230.0, largeur=150.0),
        _colonne("C", 420.0, largeur=150.0),
    ))
    assert [line.text[:1] for line in page.lines][:3] == ["A", "B", "C"]  # extraction entrelacée
    p.ordonner_pages([page])
    assert len(page.layout.boundaries) == 2
    assert [line.text[:1] for line in page.lines] == ["A"] * 6 + ["B"] * 6 + ["C"] * 6


def test_un_pdf_a_deux_colonnes_se_lit_colonne_par_colonne_de_bout_en_bout(tmp_path: Path) -> None:
    """Le seul test qui traverse `extract_pages` : les `PageText` montés à la main ne prouvent pas
    que le vrai extracteur entrelace, ni que le chemin complet le corrige."""
    from tests.test_pdf_to_blocks import build_pdf

    items: list = []
    y = 120.0
    for i in range(8):
        items.append((56.0, y, f"G{i} texte continu de la colonne de gauche, suite", 10.0, "helv"))
        items.append((310.0, y, f"D{i} texte continu de la colonne de droite, suite", 10.0, "helv"))
        y += 14.0
    build_pdf(tmp_path / "source.pdf", pages=[items])
    pages, _toc = p.extract_pages(tmp_path / "source.pdf")
    # Le défaut mesuré, tel que l'extracteur le rend : les deux colonnes arrivent entrelacées.
    assert [line.text[:2] for line in pages[0].lines] == \
        [f"{cote}{i}" for i in range(8) for cote in ("G", "D")]
    document, _meta = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[],
                                       doc_id=DOC, title="Contrat synthétique")
    assert pages[0].layout.multi
    assert [line.text[:2] for line in pages[0].lines] == \
        [f"G{i}" for i in range(8)] + [f"D{i}" for i in range(8)]
    assert document.blocks and all(
        len({line.text[:1] for line in block.lines}) == 1 for block in document.blocks
    )


# --- Rangée « libellé … montant » : appariée, mais jamais deux colonnes --------------------------

def test_une_liste_libelle_montant_sur_les_memes_lignes_de_base_nest_pas_une_gouttiere() -> None:
    """Deux blancs francs, huit rangées appariées, un côté qui ne remplit pas sa largeur : refusé."""
    rangees = _page_libelle_montant()
    p.ordonner_pages([rangees])
    assert rangees.layout.boundaries == [] and not rangees.layout.multi
    assert [line.ordre_lecture for line in rangees.lines] == list(range(1, 17))
    # Chaque montant reste sur la ligne de base de son libellé, dans l'ordre d'extraction.
    assert [line.text[:2] for line in rangees.lines][:4] == ["Li", "00", "Li", "10"]


def test_deux_vraies_colonnes_partageant_leur_grille_de_lignes_de_base_restent_retenues() -> None:
    """Le second critère existe pour cela : une mise en page professionnelle à deux colonnes partage
    très souvent la même grille de lignes de base, et l'appariement seul l'aurait écartée."""
    page = _page_deux_colonnes()
    gauche = [line for line in page.lines if line.text.startswith("G")]
    droite = [line for line in page.lines if line.text.startswith("D")]
    assert p._appariement_des_lignes_de_base(gauche, droite) == 1.0  # grille strictement partagée
    assert p._appariement_des_lignes_de_base(gauche, droite) > \
        p.get_settings().column_row_pairing_max_ratio
    p.ordonner_pages([page])
    assert page.layout.multi  # le remplissage des deux côtés sauve la gouttière
    assert p._remplissage_minimal(page.lines, gauche, droite) >= p.get_settings().column_min_fill_ratio


def test_le_remplissage_se_mesure_contre_la_largeur_disponible_et_non_contre_soi_meme() -> None:
    """Mesuré contre l'étendue de ses propres lignes, un côté vaudrait 1 et ne distinguerait rien."""
    rangees = _page_libelle_montant()
    libelles = [line for line in rangees.lines if line.text.startswith("Libelle")]
    montants = [line for line in rangees.lines if not line.text.startswith("Libelle")]
    assert p._appariement_des_lignes_de_base(libelles, montants) == 1.0
    remplissage = p._remplissage_minimal(rangees.lines, libelles, montants)
    assert remplissage < p.get_settings().column_min_fill_ratio
    # Le côté des montants s'étend d'un bord à l'autre de ses propres lignes : le rapport « à soi »
    # vaut 1, et c'est précisément la mesure que le critère n'emploie pas.
    etendue = max(l.bbox[2] for l in montants) - min(l.bbox[0] for l in montants)
    assert etendue / etendue == 1.0 > remplissage


# --- Titre pleine largeur -----------------------------------------------------------------------

def test_un_titre_traversant_ouvre_une_bande_lue_avant_ses_colonnes() -> None:
    """AC : `colonne=0`, une bande s'ouvre, et les colonnes suivantes sont lues sous elle."""
    haut = _colonne("G", GAUCHE_X, depart=100.0, n=4, pas=60.0) + \
        _colonne("D", DROITE_X, depart=100.0, n=4, pas=60.0)
    titre = _ligne("Titre pleine largeur", GAUCHE_X, 400.0, largeur=480.0, size=17.0)
    bas = _colonne("H", GAUCHE_X, depart=440.0, n=4, pas=60.0) + \
        _colonne("B", DROITE_X, depart=440.0, n=4, pas=60.0)
    page = p.PageText(page=1, width=595, height=842, lines=_extrait(haut, [titre], bas))
    assert [line.text[:1] for line in page.lines][:3] == ["G", "D", "G"]  # extraction entrelacée
    p.ordonner_pages([page])
    assert page.layout.multi and titre.colonne == 0
    assert [line.text[:1] for line in page.lines] == \
        ["G"] * 4 + ["D"] * 4 + ["T"] + ["H"] * 4 + ["B"] * 4
    assert {line.bande for line in haut} == {0} and titre.bande == 1
    assert {line.bande for line in bas} == {1}
    document = _construire([page])
    for block in document.blocks:
        assert len({line.text[:1] for line in block.lines}) == 1


def test_un_bandeau_de_tete_pleine_largeur_nouvre_pas_une_bande_vide() -> None:
    titre = _ligne("Titre en tête de page", GAUCHE_X, 60.0, largeur=480.0, size=17.0)
    page = _page_deux_colonnes(tete=[titre])
    p.ordonner_pages([page])
    assert titre.colonne == 0 and {line.bande for line in page.lines} == {0}
    assert page.lines[0] is titre  # `colonne=0` se lit avant les colonnes de sa bande


# --- Tables : des boîtes atomiques dans la détection ---------------------------------------------

def _table(prefixe: str, x: float, y: float, *, n: int = 3, largeur: float = 200.0,
           hauteur: float = 20.0) -> p.PageTable:
    """Une table détectée, avec ses `row_bboxes` — le poids géométrique de son contenu écrit."""
    return p.PageTable(
        bbox=[x, y, x + largeur, y + n * hauteur],
        rows=[[f"{prefixe} rangee {i}", "valeur"] for i in range(n)],
        row_bboxes=[[x, y + i * hauteur, x + largeur, y + (i + 1) * hauteur] for i in range(n)],
    )


def _tables_extraites(tables: list[p.PageTable]) -> list[p.PageTable]:
    """L'ordre que rend `_tables()` : le tri `(y0, x0)`, donc deux colonnes entrelacées."""
    return sorted(tables, key=lambda table: (table.bbox[1], table.bbox[0]))


def _ordre_de_lecture(page: p.PageText) -> list[str]:
    """Les deux premiers caractères du premier texte de chaque groupe, dans l'ordre de lecture."""
    p.ordonner_pages([page])
    return [lignes[0].text[:2] for _kind, lignes in p._segment_page(page)]


def _page_tables_cote_a_cote() -> p.PageText:
    """Quatre tables à gauche, quatre à droite : aucune ligne de texte sur la page."""
    tables = [table for i in range(4)
              for table in (_table(f"G{i}", GAUCHE_X, 100.0 + i * 160.0),
                            _table(f"D{i}", DROITE_X, 100.0 + i * 160.0))]
    return p.PageText(page=1, width=595, height=842, lines=[], tables=_tables_extraites(tables))


def test_des_tables_cote_a_cote_se_lisent_colonne_par_colonne() -> None:
    """AC : une table est un bloc — une page qui n'en porte que ne peut pas se lire en rangées."""
    page = _page_tables_cote_a_cote()
    # L'entrée est bien l'entrelacement de l'extraction, jamais la sortie attendue.
    assert [table.rows[0][0][:2] for table in page.tables][:4] == ["G0", "D0", "G1", "D1"]
    assert _ordre_de_lecture(page) == [f"G{i}" for i in range(4)] + [f"D{i}" for i in range(4)]
    assert page.layout.multi and len(page.layout.boundaries) == 1
    assert [page.layout.colonne(table.bbox) for table in page.tables] == [1, 2] * 4


def test_aucun_bloc_de_table_ne_mele_deux_colonnes() -> None:
    """AC : aucun bloc accepté ne mêle deux colonnes — une table est un bloc."""
    document = _construire([_page_tables_cote_a_cote()])
    tables = [block for block in document.blocks if block.kind == "table"]
    assert len(tables) == 8
    for block in tables:
        assert len({line.text[:1] for line in block.lines}) == 1, block.text
    assert [block.text[:2] for block in tables] == \
        [f"G{i}" for i in range(4)] + [f"D{i}" for i in range(4)]


def test_une_table_traversante_ouvre_une_bande_lue_avant_ses_colonnes() -> None:
    """AC : traversante ⇒ pleine largeur, exactement comme une ligne traversante — jamais coupée."""
    tables = [table for i in range(2)
              for table in (_table(f"G{i}", GAUCHE_X, 100.0 + i * 100.0),
                            _table(f"D{i}", DROITE_X, 100.0 + i * 100.0))]
    traversante = _table("TT", GAUCHE_X, 320.0, n=2, largeur=474.0)  # 56 → 530 : elle enjambe
    tables.append(traversante)
    tables += [table for i in range(2, 4)
               for table in (_table(f"G{i}", GAUCHE_X, 220.0 + i * 100.0),
                             _table(f"D{i}", DROITE_X, 220.0 + i * 100.0))]
    page = p.PageText(page=1, width=595, height=842, lines=[], tables=_tables_extraites(tables))
    assert _ordre_de_lecture(page) == ["G0", "G1", "D0", "D1", "TT", "G2", "G3", "D2", "D3"]
    assert page.layout.colonne(traversante.bbox) == 0
    assert page.layout.bande(traversante.bbox[1]) == 1
    assert page.layout.bande(100.0) == 0


def test_une_page_mele_lignes_et_tables_dans_les_memes_colonnes() -> None:
    """Lignes et tables comptent ensemble : trois lignes par côté ne suffisent pas, la table pèse.

    Le mélange est le cas réel — un corps à deux colonnes alterne paragraphes et tableaux — et il
    n'est pas la somme de deux cas déjà couverts : sans les tables dans la détection, chaque côté
    reste sous `column_min_lines` et la page se relit en rangées.
    """
    lignes = _extrait(
        [_ligne(f"G{i} texte de la colonne.", GAUCHE_X, 100.0 + i * 60.0) for i in range(3)],
        [_ligne(f"D{i} texte de la colonne.", DROITE_X, 100.0 + i * 60.0) for i in range(3)],
    )
    tables = _tables_extraites([_table("GT", GAUCHE_X, 300.0), _table("DT", DROITE_X, 300.0)])
    page = p.PageText(page=1, width=595, height=842, lines=lignes, tables=tables)
    assert [line.text[:1] for line in page.lines][:2] == ["G", "D"]  # extraction entrelacée
    assert _ordre_de_lecture(page) == ["G0", "G1", "G2", "GT", "D0", "D1", "D2", "DT"]
    assert [line.text[:1] for line in page.lines] == ["G"] * 3 + ["D"] * 3
    document = _construire([page])
    for block in document.blocks:
        assert len({line.text[:1] for line in block.lines}) == 1, block.text


def test_une_page_qui_ne_porte_que_des_tables_sans_gouttiere_ne_bouge_pas() -> None:
    """L'invariant « aucune gouttière retenue ⇒ rien ne bouge » vaut aussi sans une seule ligne."""
    tables = [_table(f"T{i}", GAUCHE_X, 100.0 + i * 120.0, largeur=474.0) for i in range(4)]
    page = p.PageText(page=1, width=595, height=842, lines=[], tables=list(tables))
    assert _ordre_de_lecture(page) == [f"T{i}" for i in range(4)]
    assert page.layout.boundaries == [] and not page.layout.multi
    assert [table.bbox for table in page.tables] == [table.bbox for table in tables]
    assert all(page.layout.colonne(table.bbox) == 1 for table in page.tables)


def test_une_gouttiere_ne_coupe_jamais_une_table() -> None:
    """Atomicité : une table qu'une frontière traverserait est pleine largeur, jamais scindée."""
    lignes = _extrait(_colonne("G", GAUCHE_X, depart=100.0, n=4, pas=60.0),
                      _colonne("D", DROITE_X, depart=100.0, n=4, pas=60.0),
                      _colonne("H", GAUCHE_X, depart=460.0, n=4, pas=60.0),
                      _colonne("B", DROITE_X, depart=460.0, n=4, pas=60.0))
    chevauchante = _table("XX", 200.0, 380.0, n=2, largeur=200.0)  # 200 → 400 : à cheval
    page = p.PageText(page=1, width=595, height=842, lines=lignes, tables=[chevauchante])
    ordre = _ordre_de_lecture(page)
    assert page.layout.multi
    assert page.layout.colonne(chevauchante.bbox) == 0  # jamais d'un seul côté
    assert ordre == ["G0", "G1", "G2", "G3", "D0", "D1", "D2", "D3",
                     "XX", "H0", "H1", "H2", "H3", "B0", "B1", "B2", "B3"]
    document = _construire([page])
    tables = [block for block in document.blocks if block.kind == "table"]
    assert len(tables) == 1 and len(tables[0].lines) == 2  # les deux rangées, dans un seul bloc


def test_une_table_seule_dun_cote_pese_ses_rangees_et_fait_colonne() -> None:
    """Une table est autant de boîtes que de rangées : son contenu écrit compte comme tel."""
    lignes = _colonne("D", DROITE_X, depart=100.0, n=6, pas=90.0)
    table = _table("GT", GAUCHE_X, 100.0, n=8, hauteur=60.0)
    page = p.PageText(page=1, width=595, height=842, lines=list(lignes), tables=[table])
    assert _ordre_de_lecture(page) == ["GT"] + [f"D{i}" for i in range(6)]
    assert page.layout.multi and page.layout.colonne(table.bbox) == 1


def test_trois_colonnes_de_tables_se_lisent_de_gauche_a_droite() -> None:
    """La récursion ne connaît pas les tables non plus : elle tombe de la même règle."""
    tables = [table for i in range(4)
              for table in (_table(f"A{i}", 40.0, 100.0 + i * 160.0, largeur=150.0),
                            _table(f"B{i}", 230.0, 100.0 + i * 160.0, largeur=150.0),
                            _table(f"C{i}", 420.0, 100.0 + i * 160.0, largeur=150.0))]
    page = p.PageText(page=1, width=595, height=842, lines=[], tables=_tables_extraites(tables))
    assert _ordre_de_lecture(page) == [f"{cote}{i}" for cote in "ABC" for i in range(4)]
    assert len(page.layout.boundaries) == 2


def test_deux_tables_qui_se_recouvrent_ne_fabriquent_aucune_gouttiere() -> None:
    """Deux boîtes qui se chevauchent ne laissent aucun blanc vertical : rien ne bouge."""
    tables = _tables_extraites([_table("T0", GAUCHE_X, 100.0, n=6, largeur=400.0),
                                _table("T1", 200.0, 160.0, n=6, largeur=330.0)])
    page = p.PageText(page=1, width=595, height=842, lines=[], tables=tables)
    assert _ordre_de_lecture(page) == ["T0", "T1"]
    assert page.layout.boundaries == []


def test_une_table_plus_haute_que_les_lignes_voisines_ne_scinde_pas_leur_colonne() -> None:
    """La hauteur écrite compte les tables : un côté trop court se refuse, il ne se devine pas."""
    lignes = _colonne("D", DROITE_X, depart=100.0, n=6, pas=12.0)  # 72 pt de haut seulement
    table = _table("GT", GAUCHE_X, 100.0, n=10, hauteur=60.0)  # 600 pt : elle domine la page
    page = p.PageText(page=1, width=595, height=842, lines=list(lignes), tables=[table])
    p.ordonner_pages([page])
    assert page.layout.boundaries == []  # column_min_span_ratio, mesuré sur la page entière
    assert [line.ordre_lecture for line in page.lines] == list(range(1, 7))


def test_une_table_sans_rangee_ne_pese_dans_aucune_gouttiere() -> None:
    """Une table qui ne rend aucun bloc (`table_sans_bloc`) ne sert rien : elle ne vote pas."""
    vides = _tables_extraites([p.PageTable(bbox=[GAUCHE_X, 100.0 + i * 160.0, 256.0,
                                                 160.0 + i * 160.0], rows=[])
                               for i in range(4)]
                              + [p.PageTable(bbox=[DROITE_X, 100.0 + i * 160.0, 530.0,
                                                   160.0 + i * 160.0], rows=[])
                                 for i in range(4)])
    page = p.PageText(page=1, width=595, height=842, lines=[], tables=vides)
    p.ordonner_pages([page])
    assert page.layout.boundaries == []
    # Les mêmes boîtes, mais servant chacune un bloc, font bien deux colonnes : seule la vacuité
    # les écartait.
    pleine = _page_tables_cote_a_cote()
    p.ordonner_pages([pleine])
    assert pleine.layout.multi


def test_la_fusion_des_numeros_ne_franchit_pas_une_gouttiere_revelee_par_les_tables() -> None:
    """La fusion et la lecture observent la **même** géométrie : sinon la page s'abîme avant d'être lue."""
    def _lignes() -> list[p.PageLine]:
        return _extrait(
            [_ligne(f"G{i} texte de la colonne.", GAUCHE_X, 100.0 + i * 60.0) for i in range(2)],
            [p.PageLine("4", [GAUCHE_X, 400.0, 66.0, 412.0], 10.0)],
            [_ligne(f"D{i} texte de la colonne.", DROITE_X, 100.0 + i * 60.0) for i in range(2)],
            [_ligne("Intitule de la colonne de droite.", DROITE_X, 400.0)],
        )

    tables = _tables_extraites([_table("GT", GAUCHE_X, 460.0, n=3, hauteur=20.0),
                                _table("DT", DROITE_X, 460.0, n=3, hauteur=20.0)])
    sans_tables = p._merge_number_lines(_lignes())
    # Sans la géométrie des tables, deux lignes par côté ne suffisent pas : la gouttière est
    # invisible et le numéro se marie à l'intitulé de la colonne d'en face.
    assert any(line.text.startswith("4 Intitule") for line in sans_tables)
    avec_tables = p._merge_number_lines(_lignes(), tables)
    assert not any(line.text.startswith("4 Intitule") for line in avec_tables)
    assert [line.text for line in avec_tables if line.number is not None] == ["4"]


def test_une_table_ne_pese_pas_dans_la_garde_de_rangee_libelle_montant() -> None:
    """Une rangée détectée par `find_tables()` est déjà un bloc : elle n'a pas le statut d'une paire
    libellé/montant que l'extracteur n'a pas vue, et n'entre donc pas dans la garde à deux signaux."""
    rangees = _page_libelle_montant()
    rangees.tables = [_table("GT", GAUCHE_X, 700.0, n=2, largeur=200.0)]
    p.ordonner_pages([rangees])
    assert rangees.layout.boundaries == []  # la garde reste armée malgré la table
    assert [line.text[:2] for line in rangees.lines][:4] == ["Li", "00", "Li", "10"]


def test_deux_colonnes_de_tables_etroites_restent_deux_colonnes() -> None:
    """La garde ne voit pas les rangées de table — sinon la grille d'un tableau, appariée par
    construction et parfois étroite, écarterait la gouttière que ce tableau lui-même dessine."""
    tables = [table for i in range(4)
              for table in (_table(f"G{i}", GAUCHE_X, 100.0 + i * 160.0, largeur=240.0),
                            _table(f"D{i}", 470.0, 100.0 + i * 160.0, largeur=50.0))]
    page = p.PageText(page=1, width=595, height=842, lines=[], tables=_tables_extraites(tables))
    assert _ordre_de_lecture(page) == [f"G{i}" for i in range(4)] + [f"D{i}" for i in range(4)]
    assert page.layout.multi
    # La géométrie de ces rangées trébucherait sur les deux signaux si on les y faisait voter :
    # appariées une à une, et un côté qui n'occupe pas sa largeur disponible.
    gauche = [line for table in page.tables if table.rows[0][0][0] == "G"
              for line in [_ligne(table.rows[0][0], table.bbox[0], table.bbox[1],
                                  largeur=table.bbox[2] - table.bbox[0])]]
    droite = [line for table in page.tables if table.rows[0][0][0] == "D"
              for line in [_ligne(table.rows[0][0], table.bbox[0], table.bbox[1],
                                  largeur=table.bbox[2] - table.bbox[0])]]
    assert p._appariement_des_lignes_de_base(gauche, droite) == 1.0
    assert p._remplissage_minimal(gauche + droite, gauche, droite) < \
        p.get_settings().column_min_fill_ratio


def test_une_rangee_mal_publiee_ne_dilate_pas_la_hauteur_ecrite() -> None:
    """La représentation géométrique d'une table est **bornée** par sa propre boîte."""
    page = _page_deux_colonnes()
    fautive = _table("XX", GAUCHE_X, 700.0, n=2, hauteur=30.0)
    fautive.row_bboxes = [[GAUCHE_X, 700.0, 256.0, 5000.0], [GAUCHE_X, 730.0, 256.0, 5000.0]]
    page.tables = [fautive]
    p.ordonner_pages([page])
    assert page.layout.multi  # non bornée, la rangée noierait `column_min_span_ratio`
    assert [line.text[:2] for line in page.lines] == \
        [f"G{i}" for i in range(6)] + [f"D{i}" for i in range(6)]


def test_un_pdf_de_tables_a_deux_colonnes_se_lit_colonne_par_colonne_de_bout_en_bout(
        tmp_path: Path) -> None:
    """Le cas mesuré, joué sur un vrai PDF : `find_tables()` rend huit tables et aucune ligne."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)

    def _tracer(x: float, y: float, prefixe: str, *, rangees: int = 3,
                largeur: float = 200.0, hauteur: float = 24.0) -> None:
        for r in range(rangees + 1):
            page.draw_line(pymupdf.Point(x, y + r * hauteur),
                           pymupdf.Point(x + largeur, y + r * hauteur), width=0.6)
        for c in (0.0, largeur / 2, largeur):
            page.draw_line(pymupdf.Point(x + c, y),
                           pymupdf.Point(x + c, y + rangees * hauteur), width=0.6)
        for r in range(rangees):
            page.insert_text((x + 4, y + r * hauteur + 16), f"{prefixe}{r} libelle", fontsize=9)
            page.insert_text((x + largeur / 2 + 4, y + r * hauteur + 16), "valeur", fontsize=9)

    for i in range(4):
        _tracer(GAUCHE_X, 100.0 + i * 160.0, f"G{i}")
        _tracer(DROITE_X, 100.0 + i * 160.0, f"D{i}")
    doc.save(tmp_path / "source.pdf")
    doc.close()

    pages, _toc = p.extract_pages(tmp_path / "source.pdf")
    page_texte = pages[0]
    assert len(page_texte.tables) == 8 and page_texte.lines == []
    # Le défaut mesuré, tel que l'extracteur le rend : les deux colonnes arrivent entrelacées.
    assert [table.rows[0][0][:2] for table in page_texte.tables] == \
        [f"{cote}{i}" for i in range(4) for cote in ("G", "D")]
    document = _construire(pages)
    assert page_texte.layout.multi
    assert [block.text[:2] for block in document.blocks] == \
        [f"G{i}" for i in range(4)] + [f"D{i}" for i in range(4)]


# --- Table des matières -------------------------------------------------------------------------

def test_une_tdm_multicolonne_ne_recolle_pas_les_deux_cotes_dans_un_bloc() -> None:
    """La non-citabilité d'une TdM ne l'autorise pas à perdre sa structure visuelle."""
    page = _page_deux_colonnes()
    page.is_toc = True

    document = _construire([page])

    assert page.layout.multi
    assert document.blocks
    assert all(
        len({"G" if line.text.startswith("G") else "D" for line in block.lines}) == 1
        for block in document.blocks
    )

def test_une_tdm_absente_ne_structure_jamais_larbre() -> None:
    pages = [_page_deux_colonnes()]
    p._mark_toc_pages(pages)
    assert [page.is_toc for page in pages] == [False]
    document = _construire(pages)
    assert all(not node.node_id.endswith(":tdm") for node in document.nodes)
    assert [node.node_id for node in document.nodes] == [DOC]  # racine seule : aucune hiérarchie inventée


def test_une_tdm_discordante_reste_une_alerte_et_ne_construit_aucun_noeud() -> None:
    """AC : TdM ≠ corps ⇒ alerte existante, jamais un bloquant ni une hiérarchie fabriquée."""
    from server.ingest.report import _printed_toc_check

    tdm = p.PageText(page=1, width=595, height=842, is_toc=True, lines=[
        _ligne("1 Section annoncée ........ 4", GAUCHE_X, 100.0),
        _ligne("2 Autre section annoncée ........ 9", GAUCHE_X, 130.0),
    ])
    corps = _page_deux_colonnes(page=2)
    pages = [tdm, corps]
    document = _construire(pages)
    entrees = p._printed_toc_entries(pages)
    check = _printed_toc_check(document, entrees, 1)
    assert check.level == "alerte" and "absents de l'arbre" in check.detail
    assert all(not node.node_id.startswith(f"{DOC}:a") for node in document.nodes)


def test_le_vocabulaire_du_corpus_synthetique_est_neutre() -> None:
    """Never 4.2c : aucun assureur, document, page ou numérotation réelle dans ce corpus."""
    import inspect

    source = "".join(inspect.getsource(fabrique) for fabrique in
                     (_ligne, _colonne, _extrait, _page_deux_colonnes, _page_libelle_montant,
                      _page_serree, _table, _tables_extraites, _page_tables_cote_a_cote)).lower()
    for interdit in ("axa", "baloise", "optihome", "home", "lu-", "p30", "p40", "p48"):
        assert interdit not in source, f"vocabulaire non neutre : {interdit!r}"
