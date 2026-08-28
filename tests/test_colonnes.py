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


# --- Table des matières -------------------------------------------------------------------------

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
                      _page_serree)).lower()
    for interdit in ("axa", "baloise", "optihome", "home", "lu-", "p30", "p40", "p48"):
        assert interdit not in source, f"vocabulaire non neutre : {interdit!r}"
