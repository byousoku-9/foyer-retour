"""PDF → arbre de blocs d'un contrat (AD-2), artefacts `data/{doc_id}/*` et `data/manifest.json` (AD-7).

    uv run python -m server.ingest.pdf_to_blocks axa-lu-optihome-2017 [--data data] [--edition "juin 2017"]

Extraction : `page.get_text("dict", sort=True)` ; une `Line` par ligne visuelle (un numéro d'article et le texte
qui le suit sur la même ligne de base sont fusionnés) ; en-têtes/pieds répétés retirés (bande haute/basse, texte
récurrent ou numéro de page). Segmentation : un numéro d'article ouvre un nœud `{doc_id}:a{numero}` (parent = préfixe
le plus long existant, sinon la racine) ; une ligne à puce ouvre un item de `list` ; un écart vertical supérieur à
`para_gap_ratio` hauteurs de ligne ouvre un `para` ; un paragraphe qui continue sur la page suivante est scindé,
le second bloc porte `continues`. Un intitulé autonome de table des matières, dans une ligne ou une table, ouvre
le nœud `{doc_id}:tdm` et se propage aux pages de continuation ; son texte reste `autre`, ses tables restent atomiques,
et ses sections numérotées sont comparées à l'arbre reconstruit.
`text` est brut : seules altérations, déclarées dans `FLAGS` (donc dans l'empreinte), les puces Wingdings deviennent
`•`, le glyphe de tabulation `\\x07` est retiré, les espaces/tabulations de fin de ligne sont retirés, les espaces
de tête d'une ligne sans puce sont retirés, et un numéro d'article seul sur sa ligne est joint par une espace au texte
qui le suit sur la même ligne de base (`merge_number_sep`). `get_toc()` (signets du PDF), s'il est non vide, donne
leur titre aux nœuds qui n'en ont pas et est confronté à la numérotation (`tdm_pdf_ecart`, alerte).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pymupdf
from pydantic import ValidationError

from server.app.config import get_settings
from server.app.corpus.text import normalize, normalize_version
from server.app.domain import Block, BlockRef, Check, Document, Line, ManifestEntry, Node, NodeRef, Report, is_citable
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE
from server.ingest.artifacts import (SCHEMA_VERSION, document_json, load_previous, merge_manifest, overlay_hash,
                                     read_manifest, write_atomic)
from server.ingest.fetch_source import GS_URL_RE
from server.ingest.report import (build_pdf_report, numero_de_noeud, report_from_validation_error,
                                  structure_check)
from server.ingest.structure import (STRUCTURE_RULES_VERSION, NoeudVerifie, StructureProposee,
                                     StructureRefusee, arbre, charger, empreinte_proposition,
                                     presente, registre_lignes, verifier)

DOC_ID = "axa-lu-optihome-2017"
TITLE = "Conditions d’assurances OptiHome (multirisques habitation)"
DEFAULT_EDITION = "juin 2017"

# Entrent dans `ingest_fingerprint` : toute modification change les IDs attendus (AD-2, stabilité).
PARSER_VERSION = "10"  # story 4.2c : registre de lignes source, colonnes géométriques, structure vérifiée
SEGMENTATION_RULES = ("numero:^\\d+(\\.\\d+)*$@x0<article_number_max_x=>noeud a{numero}(parent=prefixe);"
                      "titre:meme_ligne_de_base(size>=title_min_size_pt|sans_ponct_finale&suite_majuscule)=>heading;"
                      "puce:Wingdings|^•=>list(item;continuation=indent>list_indent_pt|minuscule&prec!~[.;:]$);"
                      "gap>para_gap_ratio*h=>para;"
                      "entete:bande&(recurrent|numero_page|MAJUSCULES<=header_caps_max_size_pt);"
                      "table:find_tables=>bloc_atomique(cellules=' | ')&lignes_source_portees_une_fois;"
                      "ocr:page_visuelle_sans_texte_retenu=>get_textpage_ocr(fra)&source_field=ocr;"
                      "tdm:entrees_structurees(numero+page|points_de_conduite+page)|intitule_autonome_visible"
                      "=>continuation_si_entrees_structurees;sans_rearmement_apres_corps;"
                      "table:reste_atomique&source_field=tdm|preliminaire_si_non_citable;"
                      "preliminaire:avant_tdm_ou_premier_article=>autre;apres_tdm=>contenu_citable;"
                      "mixte:union_aire_images/page>=mixed_page_image_density;numero_para+ligne_minuscule=>meme_para;"
                      "dedent:dernier_frere_reel+item_num_compact+alignement_corps_parent=>parent;"
                      "continues:page_suivante&meme_kind(para|list)&sans_numero&prec!~[.;:]$;"
                      "toc:get_toc()=>titres manquants+tdm_pdf_ecart;"
                      "colonnes:gouttiere>=column_gutter_min_pt&cotes>=column_min_lines"
                      "&hauteur>=column_min_span_ratio=>lecture(bande,colonne,y0,x0);"
                      "rangee:appariement>column_row_pairing_max_ratio"
                      "&remplissage<column_min_fill_ratio=>gouttiere_ecartee;"
                      "sans_gouttiere=>ordre_lecture=ordre_source;traversante=>colonne0+bande;"
                      "continuation_et_tri_rompus_au_changement_de_colonne_ou_de_bande;"
                      "structure:proposition_verifiee(line_uid)&couverture_totale"
                      "=>noeuds positionnels+titres du registre;"
                      "refus=>quarantaine")
FLAGS = {"sort": True, "wingdings_bullet": "•", "drop_tab_glyph": True, "rstrip_lines": True,
         "lstrip_lines_sans_puce": True, "merge_number_sep": " ",
         "ligatures": "decomposees (TEXT_PRESERVE_LIGATURES absent)", "dehyphenate": False}

_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)\s*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PAGE_NUMBER_RE = re.compile(r"^\d{1,3}$")
_TOC_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$")
_PRINTED_TOC_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(.+?)(?:\s+\.{2,}\s*|\s+)(\d{1,3})\s*$")
_TOC_LEADER_RE = re.compile(r"^\s*(\S.*?)\s*\.{2,}\s*\d{1,3}\s*$")
_BULLET_FONTS = ("Wingdings",)
_TERMINAL = (".", ";", ":")
logger = logging.getLogger(__name__)


def ingest_fingerprint(structure: StructureProposee | None = None) -> str:
    """Empreinte des règles **et** de la proposition de structure effectivement appliquée (AD-2).

    Le loader ne dispose que de `source_hash` et de cette empreinte pour décider que deux artefacts
    portent les mêmes identifiants. Un `structure.json` déposé à côté du même PDF change tous les
    `node_id` : il doit donc changer l'empreinte, exactement comme un seuil géométrique le fait.
    Son absence est un état nommé, pas un trou — sans quoi « aucune proposition » et « une
    proposition qui ne structure rien » se confondraient.
    """
    s = get_settings()
    payload = json.dumps({"parser": PARSER_VERSION, "schema": SCHEMA_VERSION, "segmentation": SEGMENTATION_RULES,
                          "flags": FLAGS, "normalize_version": normalize_version,
                          "structure_rules": STRUCTURE_RULES_VERSION,
                          "structure": empreinte_proposition(structure),
                          "thresholds": {"header_band_pt": s.header_band_pt, "footer_band_pt": s.footer_band_pt,
                                         "header_min_pages_ratio": s.header_min_pages_ratio,
                                         "para_gap_ratio": s.para_gap_ratio,
                                         "article_number_max_x": s.article_number_max_x,
                                         "title_min_size_pt": s.title_min_size_pt,
                                         "header_caps_max_size_pt": s.header_caps_max_size_pt,
                                         "baseline_tolerance_pt": s.baseline_tolerance_pt,
                                         "number_gap_tolerance_pt": s.number_gap_tolerance_pt,
                                         "list_indent_pt": s.list_indent_pt,
                                         "dedent_tolerance_pt": s.dedent_tolerance_pt,
                                         "dedent_starter_max_lines": s.dedent_starter_max_lines,
                                         "mixed_page_image_density": s.mixed_page_image_density,
                                         "ocr_dpi": s.ocr_dpi,
                                         "quality_min_words": s.quality_min_words,
                                         "foreign_signal_min": s.foreign_signal_min,
                                         "french_signal_ratio_min": s.french_signal_ratio_min,
                                         "gibberish_ratio_max": s.gibberish_ratio_max,
                                         "residual_header_min_pages_ratio": s.residual_header_min_pages_ratio,
                                         "coverage_threshold": s.coverage_threshold,
                                         "toc_page_number_baseline_pt": s.toc_page_number_baseline_pt,
                                         "toc_column_tolerance_pt": s.toc_column_tolerance_pt,
                                         "toc_indent_tolerance_pt": s.toc_indent_tolerance_pt,
                                         "toc_line_gap_ratio": s.toc_line_gap_ratio,
                                         # story 4.2c : la géométrie des colonnes change l'ordre de
                                         # lecture, donc `seq`, donc les `block_id` (AD-2).
                                         "column_gutter_min_pt": s.column_gutter_min_pt,
                                         "column_min_lines": s.column_min_lines,
                                         "column_min_span_ratio": s.column_min_span_ratio,
                                         "column_row_pairing_max_ratio": s.column_row_pairing_max_ratio,
                                         "column_min_fill_ratio": s.column_min_fill_ratio,
                                         # story 1.3 (revue P1) : le niveau de coupe du sommaire change summary.md
                                         "summary_max_level": s.summary_max_level}},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceLine:
    """Ligne source figée (story 4.2c) : l'ancre adressable de l'ingestion, avant toute mutation.

    `uid` nomme la ligne **extraite** — page et rang d'extraction — et ne dérive d'aucun `block_id` :
    une proposition de structure ne peut donc pas s'ancrer sur le découpage qu'elle prétend décider.
    Le registre reste **interne à l'ingestion** : il ne traverse ni `Document`, ni `document.json`,
    ni le loader (aucune migration, aucun amendement AD-2 — `Line` est inchangée).
    """

    uid: str
    text: str
    page: int
    bbox: tuple[float, float, float, float]
    ordre: int  # rang d'extraction sur la page, 1-indexé


@dataclass
class SourceRegistry:
    """Registre d'une page : ses lignes source et le motif de retrait de celles qu'aucun bloc ne porte."""

    lines: list[SourceLine] = field(default_factory=list)
    # uid → motif de retrait, réservé au contenu **réellement non servi** : `bande_recurrente`, et
    # `table_sans_bloc` pour une table détectée qui ne rend finalement aucune rangée.
    removed: dict[str, str] = field(default_factory=dict)

    def add(self, *, page: int, text: str, bbox: list[float]) -> SourceLine:
        line = SourceLine(uid=f"p{page}:l{len(self.lines) + 1}", text=text, page=page,
                          bbox=(bbox[0], bbox[1], bbox[2], bbox[3]), ordre=len(self.lines) + 1)
        self.lines.append(line)
        return line

    def retirer(self, uids: list[str], motif: str) -> None:
        for uid in uids:
            self.removed.setdefault(uid, motif)


@dataclass
class PageLayout:
    """Colonnes retenues sur une page, par la seule géométrie (story 4.2c).

    `boundaries` vide = une seule colonne : l'ordre de lecture reste alors l'ordre d'extraction.
    """

    boundaries: list[float] = field(default_factory=list)
    band_starts: list[tuple[float, int]] = field(default_factory=list)  # (y0 d'ouverture, bande)

    @property
    def multi(self) -> bool:
        return bool(self.boundaries)

    def colonne(self, bbox: list[float] | tuple[float, ...]) -> int:
        """0 = pleine largeur (la boîte traverse une gouttière) ; sinon le rang de la colonne, 1-indexé."""
        if any(bbox[0] < boundary < bbox[2] for boundary in self.boundaries):
            return 0
        return 1 + sum(1 for boundary in self.boundaries if bbox[0] >= boundary)

    def bande(self, y0: float) -> int:
        band = 0
        for start, value in self.band_starts:
            if y0 >= start:
                band = value
        return band


@dataclass
class PageLine:
    text: str
    bbox: list[float]
    size: float  # taille de police dominante
    bullet: bool = False
    number: str | None = None  # numéro d'article si la ligne en commence par un (fusionné)
    # Story 4.2c. `source_uids` porte les lignes du registre dont cette ligne de travail est faite
    # (deux après `_merge_number_lines`) ; `colonne`/`bande`/`ordre_lecture` sont posés par
    # `_assign_reading_order` et valent respectivement 1 / 0 / l'ordre source sur une page mono-colonne.
    source_uids: list[str] = field(default_factory=list)
    colonne: int = 1
    bande: int = 0
    ordre_lecture: int = 0


@dataclass
class PageText:
    page: int  # 1-indexée
    width: float
    height: float
    lines: list[PageLine] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # en-têtes/pieds retirés
    images: int = 0
    drawings: int = 0  # tracés vectoriels : une page sans texte mais dessinée n'est pas blanche (AD-8)
    image_area_ratio: float = 0.0
    tables: list[PageTable] = field(default_factory=list)
    is_toc: bool = False
    after_toc: bool = False
    # `None` garde les PageText synthétiques rétrocompatibles ; l'extracteur pose explicitement
    # si du contenu natif exploitable subsiste après retrait des bandes récurrentes.
    native_text: bool | None = None
    ocr_attempted: bool = False
    ocr_succeeded: bool = False
    ocr_error: str | None = None
    language: str = "fr"
    # Story 4.2c : registre de lignes source figé par l'extracteur, et colonnes retenues sur la page.
    # Un `PageText` synthétique les laisse vides — l'invariant de couverture est alors vide, pas faux.
    source: SourceRegistry = field(default_factory=SourceRegistry)
    layout: PageLayout = field(default_factory=PageLayout)

    @property
    def visual(self) -> bool:
        return bool(self.images or self.drawings)


@dataclass
class PageTable:
    """Table détectée sur une page, conservée comme une unité dans l'ordre visuel.

    `source_uids` (story 4.2c) porte les lignes brutes que la table a absorbées : leur contenu est
    **servi**, par le bloc `table` reconstruit depuis `rows`, et la bijection lignes/blocs exige donc
    qu'elles y soient rattachées — jamais comptées « retirées » pendant que leur texte est publié.
    """

    bbox: list[float]
    rows: list[list[str]]
    row_bboxes: list[list[float]] = field(default_factory=list)
    source_uids: list[str] = field(default_factory=list)

    @property
    def sert_un_bloc(self) -> bool:
        """Une table sans rangée ne produit aucun bloc : son contenu n'est alors pas servi."""
        return bool(self.rows)


def _round(b: Any) -> list[float]:
    return [round(float(v), 2) for v in b]


def _center_inside(bbox: list[float], container: list[float]) -> bool:
    """Une ligne appartient à une table lorsque le centre de sa boîte est dans celle de la table.

    Une légende ou une note qui ne fait que frôler la boîte reste donc disponible, tandis que le
    texte atomique des cellules n'est pas dupliqué dans les paragraphes.
    """
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    return container[0] <= center_x <= container[2] and container[1] <= center_y <= container[3]


def _raw_lines(page: pymupdf.Page, *, page_no: int = 1, textpage: Any | None = None,
               tables: list[PageTable] | None = None,
               registry: SourceRegistry | None = None) -> tuple[list[PageLine], int]:
    """Lignes brutes (texte nettoyé, bbox, taille dominante, puce) et nombre d'images.

    Seul point d'extraction du parseur, donc **le seul endroit où le registre peut être figé** :
    chaque ligne visuelle retenue y reçoit son `SourceLine` avant toute fusion, tout retrait de bande
    et toute absorption par une table. Une ligne dont le centre tombe dans une table sort du flux des
    paragraphes — son texte serait sinon publié deux fois — mais elle est **confiée à cette table**,
    qui la portera dans son propre bloc : son contenu est servi, elle n'est donc jamais « retirée ».
    Les uid sont réattribués à chaque passe (une page ré-extraite en OCR refait son registre).
    """
    out: list[PageLine] = []
    images = 0
    for table in tables or ():
        table.source_uids = []
    options: dict[str, Any] = {"sort": FLAGS["sort"]}
    if textpage is not None:
        options["textpage"] = textpage
    for b in page.get_text("dict", **options)["blocks"]:
        if b["type"] != 0:
            images += 1
            continue
        for line in b["lines"]:
            parts: list[str] = []
            sizes: dict[float, float] = {}
            bullet = False
            for s in line["spans"]:
                t = s["text"]
                if any(f in s["font"] for f in _BULLET_FONTS):
                    if not parts and t.strip():
                        bullet = True
                    t = FLAGS["wingdings_bullet"] if t.strip() else t
                if FLAGS["drop_tab_glyph"]:
                    t = t.replace("\x07", "")
                parts.append(t)
                sizes[round(s["size"], 1)] = sizes.get(round(s["size"], 1), 0) + len(t)
            text = "".join(parts)
            bullet = bullet or text.lstrip(" \t").startswith(FLAGS["wingdings_bullet"])  # puce typographique
            text = text.rstrip() if FLAGS["rstrip_lines"] else text
            text = text.lstrip(" \t") if FLAGS["lstrip_lines_sans_puce"] and not bullet else text
            if not text.strip():
                continue
            size = max(sizes, key=lambda k: sizes[k]) if sizes else 0.0
            bbox = _round(line["bbox"])
            source = registry.add(page=page_no, text=text, bbox=bbox) if registry is not None else None
            # La **première** table qui contient le centre l'absorbe : deux boîtes qui se recouvrent
            # ne peuvent donc pas porter la même ligne deux fois.
            absorbante = next((table for table in tables or ()
                               if _center_inside(bbox, table.bbox)), None)
            if absorbante is not None:
                if source is not None:
                    absorbante.source_uids.append(source.uid)
                continue
            out.append(PageLine(text=text, bbox=bbox, size=size, bullet=bullet,
                                source_uids=[source.uid] if source is not None else []))
    return out, images


def _tables(page: pymupdf.Page) -> list[PageTable]:
    """Tables PyMuPDF, cellules vides comprises. Une détection absente n'altère pas la page."""
    try:
        found = page.find_tables()
    except (AttributeError, RuntimeError, ValueError):
        return []
    tables: list[PageTable] = []
    for table in found.tables:
        extracted = table.extract() or []
        rows = [["" if cell is None else str(cell).replace("\x07", "").strip() for cell in row]
                for row in extracted]
        if not rows or not any(cell for row in rows for cell in row):
            continue
        row_bboxes = [_round(row.bbox) for row in getattr(table, "rows", [])]
        tables.append(PageTable(bbox=_round(table.bbox), rows=rows, row_bboxes=row_bboxes))
    return sorted(tables, key=lambda table: (table.bbox[1], table.bbox[0]))


def _rectangles_union_area(rectangles: list[pymupdf.Rect]) -> float:
    """Aire exacte de l'union de rectangles parallèles aux axes, sans compter deux fois les recouvrements."""
    xs = sorted({float(x) for rect in rectangles for x in (rect.x0, rect.x1)})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        if right <= left:
            continue
        intervals = sorted((float(rect.y0), float(rect.y1)) for rect in rectangles
                           if rect.x0 < right and rect.x1 > left)
        if not intervals:
            continue
        bottom, top = intervals[0]
        height = 0.0
        for start, end in intervals[1:]:
            if start <= top:
                top = max(top, end)
            else:
                height += top - bottom
                bottom, top = start, end
        height += top - bottom
        area += (right - left) * height
    return area


def _image_area_ratio(page: pymupdf.Page) -> float:
    """Densité visuelle de l'union des images raster, bornée à la surface de la page."""
    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    rectangles: list[pymupdf.Rect] = []
    try:
        infos = page.get_image_info()
    except (AttributeError, RuntimeError, ValueError):
        infos = []
    for info in infos:
        bbox = info.get("bbox")
        if not bbox:
            continue
        rect = pymupdf.Rect(bbox) & page.rect
        if not rect.is_empty:
            rectangles.append(rect)
    return min(_rectangles_union_area(rectangles) / page_area, 1.0)


def _is_toc_title(text: str) -> bool:
    """Reconnaît un intitulé de TdM autonome, pas une mention dans une phrase de contenu."""
    value = normalize(text).strip()
    # Les préfixes exigent un séparateur et les suffixes une ponctuation : une phrase telle que
    # « consultez la table des matières pour… » ne peut donc pas être prise pour un titre.
    return bool(re.fullmatch(
        r"(?:[^.\n]+?\s[-:]\s*)?table des matieres"
        r"(?:\s*[.:;!?])*(?:\s*[-:]\s*[^.\n]+|\s*\([^()\n]+\))?",
        value,
    ))


def _has_toc_title(page: PageText) -> bool:
    return any(_is_toc_title(line.text) for line in page.lines) or any(
        _is_toc_title(cell) for table in page.tables for row in table.rows for cell in row if cell
    )


def _has_toc_entries(page: PageText) -> bool:
    """Vrai si la page porte une entrée avec numéro en colonne ou points de conduite vers une page."""
    settings = get_settings()
    texts = [line.text for line in page.lines]
    texts.extend(" ".join(cell.strip() for cell in row if cell.strip())
                 for table in page.tables for row in table.rows)
    if any(_PRINTED_TOC_RE.match(text.strip()) or _TOC_LEADER_RE.match(text.strip()) for text in texts):
        return True
    for line in page.lines:
        if _TOC_NUMBER_RE.match(line.text.strip()) and any(
            _PAGE_NUMBER_RE.fullmatch(candidate.text.strip())
            and candidate.bbox[0] > line.bbox[0]
            and abs(candidate.bbox[1] - line.bbox[1]) <= settings.toc_page_number_baseline_pt
            for candidate in page.lines
        ):
            return True
    return False


def _mark_toc_pages(pages: list[PageText]) -> None:
    """Entre par la structure visible, propage tant qu'elle subsiste et ne se réarme jamais après le corps."""
    in_toc = False
    toc_finished = False
    for page in pages:
        structural = _has_toc_entries(page)
        visible_title = _has_toc_title(page)
        has_article = any(line.number is not None for line in page.lines)
        if toc_finished:
            page.is_toc = False
            page.after_toc = True
            continue
        if not in_toc and (structural or visible_title):
            in_toc = True
        if in_toc and (structural or visible_title):
            page.is_toc = True
            page.after_toc = False
            if has_article:
                in_toc = False
                toc_finished = True
        else:
            page.is_toc = False
            if in_toc:
                in_toc = False
                toc_finished = True
            page.after_toc = toc_finished


def _ocr_error_category(exc: Exception) -> str:
    """Catégorie publique stable ; le message complet reste exclusivement dans les journaux serveur."""
    message = str(exc).lower()
    if any(fragment in message for fragment in ("no ocr support", "tesseract is not installed", "not installed")):
        return "moteur_absent"
    if any(fragment in message for fragment in ("traineddata", "error opening data file", "failed loading language")):
        return "langue_absente"
    return "echec_moteur"


def _merge_number_lines(lines: list[PageLine]) -> list[PageLine]:
    """« 1.12 » seul sur sa ligne + « Contenu » sur la même ligne de base ⇒ une seule `Line` « 1.12 Contenu ».

    La fusion ne franchit **jamais** une gouttière : un numéro en bas d'une colonne et une ligne de
    la colonne voisine partagent souvent une ligne de base, et les réunir fabriquait une ligne
    pleine largeur factice — donc une bande parasite, un titre faux et un bloc à cheval sur deux
    colonnes. Les frontières sont recalculées ici sur la géométrie d'**avant** fusion, la seule que
    la fusion puisse consulter (`_column_boundaries` est défini plus bas ; Python le résout à
    l'appel).
    """
    s = get_settings()
    written_height = (max(line.bbox[3] for line in lines) - min(line.bbox[1] for line in lines)) if lines else 0.0
    boundaries = _column_boundaries(lines, written_height) if lines else []
    out: list[PageLine] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        m = _NUMBER_RE.match(cur.text)
        if m and cur.bbox[0] < s.article_number_max_x:
            cur.number = m.group(1)
            cur.text = m.group(1)
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            if nxt and not nxt.bullet and abs(nxt.bbox[1] - cur.bbox[1]) <= s.baseline_tolerance_pt \
                    and nxt.bbox[0] >= cur.bbox[2] - s.number_gap_tolerance_pt \
                    and not any(cur.bbox[2] <= frontiere <= nxt.bbox[0] for frontiere in boundaries):
                cur.text = f"{cur.text}{FLAGS['merge_number_sep']}{nxt.text}"
                cur.bbox = [min(cur.bbox[0], nxt.bbox[0]), min(cur.bbox[1], nxt.bbox[1]),
                            max(cur.bbox[2], nxt.bbox[2]), max(cur.bbox[3], nxt.bbox[3])]
                cur.size = nxt.size
                # La ligne fusionnée porte **deux** uid : le registre ne perd jamais une ligne source
                # parce que le parseur en a réuni deux (story 4.2c).
                cur.source_uids = [*cur.source_uids, *nxt.source_uids]
                i += 1
        out.append(cur)
        i += 1
    return out


def _without_running_bands(page: PageText, lines: list[PageLine], band_texts: dict[str, int],
                           minimum: int, *, registry: SourceRegistry | None = None) -> list[PageLine]:
    """Retire les bandes récurrentes d'une couche native ou OCR et conserve leur diagnostic."""
    s = get_settings()
    reg = page.source if registry is None else registry
    kept: list[PageLine] = []
    for line in lines:
        in_band = line.bbox[1] < s.header_band_pt or line.bbox[3] > page.height - s.footer_band_pt
        running = band_texts.get(line.text, 0) >= minimum or _PAGE_NUMBER_RE.match(line.text) is not None \
            or (line.size <= s.header_caps_max_size_pt and line.text == line.text.upper())
        if in_band and running:
            if line.text not in page.removed:
                page.removed.append(line.text)
            reg.retirer(line.source_uids, "bande_recurrente")
            continue
        kept.append(line)
    return kept


def _appariement_des_lignes_de_base(left: list[PageLine], right: list[PageLine]) -> float:
    """Part **mutuelle** des lignes qui trouvent une partenaire sur leur ligne de base de l'autre côté.

    Une rangée « libellé … montant » apparie ses deux côtés un pour un : chaque montant est posé sur
    la ligne de base de son libellé. Deux colonnes de lecture, elles, dérivent l'une de l'autre dès
    qu'un paragraphe s'achève. Le minimum des deux parts évite qu'un seul côté très peuplé dilue le
    signal. La tolérance verticale est `baseline_tolerance_pt`, qui définit déjà « même ligne de
    base » ailleurs dans le parseur : aucune notion nouvelle n'est introduite.
    """
    tolerance = get_settings().baseline_tolerance_pt

    def part(cote: list[PageLine], autre: list[PageLine]) -> float:
        apparies = sum(1 for line in cote
                       if any(abs(other.bbox[1] - line.bbox[1]) <= tolerance for other in autre))
        return apparies / len(cote) if cote else 0.0

    return min(part(left, right), part(right, left))


def _remplissage_minimal(lines: list[PageLine], left: list[PageLine], right: list[PageLine]) -> float:
    """Part de sa largeur **disponible** qu'occupe le côté le moins rempli d'une gouttière.

    La largeur disponible d'un côté va de la marge de texte de la page — mesurée sur *toutes* les
    lignes de ce niveau, traversantes comprises — jusqu'au bord **opposé** de la gouttière. La
    mesurer contre l'étendue des lignes du côté lui-même rendrait 1 par construction et ne
    distinguerait rien. Une colonne de texte remplit sa largeur utile ; une colonne de montants
    alignés à droite n'en occupe qu'une fraction, et c'est cette fraction qui la trahit.
    """
    marge_gauche = min(line.bbox[0] for line in lines)
    marge_droite = max(line.bbox[2] for line in lines)
    parts: list[float] = []
    for cote, disponible in ((left, min(line.bbox[0] for line in right) - marge_gauche),
                             (right, marge_droite - max(line.bbox[2] for line in left))):
        occupe = max(line.bbox[2] for line in cote) - min(line.bbox[0] for line in cote)
        parts.append(occupe / disponible if disponible > 0 else 1.0)
    return min(parts)


def _best_boundary(lines: list[PageLine], written_height: float) -> float | None:
    """Meilleure gouttière d'un ensemble de lignes, ou `None` — géométrie seule, aucun cas particulier.

    Une frontière candidate `c` partage les lignes en trois : celles **entièrement** à gauche
    (`x1 <= c`), celles entièrement à droite (`x0 >= c`) et celles qui la traversent — ces dernières
    sont pleine largeur et n'invalident pas la gouttière. La gouttière est le blanc réellement mesuré
    entre les deux colonnes, `[max(x1 des gauches), min(x0 des droites)]` ; elle n'est retenue que si
    elle est assez large, si chaque côté porte assez de lignes et si chaque côté est assez haut.
    Elle est écartée en dernier ressort par la conjonction de **deux** signaux, jamais par un seul :
    les deux côtés appariés ligne de base à ligne de base **et** un côté qui ne remplit pas la
    largeur dont il dispose — la signature d'une rangée « libellé … montant ». L'appariement seul ne
    suffirait pas : une mise en page à deux colonnes partage très souvent la même grille de lignes
    de base, et l'écarter pour cela annulerait la correction sur les documents mêmes qu'elle vise.
    Elle est intérieure par construction : elle a du texte des deux côtés, donc jamais une marge.
    """
    s = get_settings()
    if len(lines) < 2 * s.column_min_lines:
        return None
    best: tuple[float, float] | None = None  # (largeur de gouttière, frontière)
    for candidate in sorted({line.bbox[0] for line in lines}):
        left = [line for line in lines if line.bbox[2] <= candidate]
        right = [line for line in lines if line.bbox[0] >= candidate]
        if len(left) < s.column_min_lines or len(right) < s.column_min_lines:
            continue
        gutter = min(line.bbox[0] for line in right) - max(line.bbox[2] for line in left)
        if gutter < s.column_gutter_min_pt:
            continue
        minimum_span = s.column_min_span_ratio * written_height
        if any(max(line.bbox[3] for line in side) - min(line.bbox[1] for line in side) < minimum_span
               for side in (left, right)):
            continue
        if _appariement_des_lignes_de_base(left, right) > s.column_row_pairing_max_ratio \
                and _remplissage_minimal(lines, left, right) < s.column_min_fill_ratio:
            continue
        # À partition égale, la gouttière la plus large gagne ; à largeur égale, la frontière la plus
        # à gauche — deux règles totales, donc un résultat indépendant de l'ordre d'itération.
        if best is None or (gutter, -candidate) > (best[0], -best[1]):
            best = (gutter, candidate)
    return None if best is None else best[1]


def _column_boundaries(lines: list[PageLine], written_height: float) -> list[float]:
    """Frontières retenues, triées. La récursion sur chaque côté couvre trois colonnes et plus."""
    boundary = _best_boundary(lines, written_height)
    if boundary is None:
        return []
    left = [line for line in lines if line.bbox[2] <= boundary]
    right = [line for line in lines if line.bbox[0] >= boundary]
    return sorted([*_column_boundaries(left, written_height), boundary,
                   *_column_boundaries(right, written_height)])


def _assign_reading_order(pt: PageText) -> None:
    """Pose `colonne`, `bande` et `ordre_lecture`, et réordonne la page colonne par colonne.

    **Aucune gouttière retenue ⇒ rien ne bouge** : `colonne=1`, `bande=0`, et `ordre_lecture` est
    l'ordre d'extraction, à l'identique — un document mono-colonne ne peut donc pas régresser.
    Sinon la clé de lecture est `(bande, colonne, y0, x0, ordre source)` : une ligne qui traverse une
    gouttière est pleine largeur (`colonne=0`), ouvre une bande et se lit avant les colonnes qu'elle
    coiffe. Idempotent : rejouée sur la même page, la fonction rend le même ordre.
    """
    lines = pt.lines
    if not lines:
        pt.layout = PageLayout()
        return
    written_height = max(line.bbox[3] for line in lines) - min(line.bbox[1] for line in lines)
    boundaries = _column_boundaries(lines, written_height)
    layout = PageLayout(boundaries=boundaries)
    if not boundaries:
        pt.layout = layout
        for index, line in enumerate(lines, 1):
            line.colonne, line.bande, line.ordre_lecture = 1, 0, index
        return
    for line in lines:
        line.colonne = layout.colonne(line.bbox)
    # Les tables participent au bandage **comme les lignes** : une table qui traverse une gouttière
    # est pleine largeur et ouvre donc une bande. Sans elle dans ce parcours, une table traversante
    # gardait `colonne=0` sans jamais ouvrir de bande, et `0 < 1` dans la clé de tri la faisait
    # remonter en tête de sa bande — devant tout le texte des colonnes qu'elle suit pourtant à l'œil.
    items: list[tuple[float, float, int, PageLine | None]] = [
        (line.bbox[1], line.bbox[0], line.colonne, line) for line in lines
    ]
    items += [(table.bbox[1], table.bbox[0], layout.colonne(table.bbox), None) for table in pt.tables]
    items.sort(key=lambda item: (item[0], item[1]))
    band = 0
    previous_full = True  # un bandeau de tête n'ouvre pas de bande : il est déjà dans la première
    band_starts: list[tuple[float, int]] = []
    for y0, _x0, colonne, line in items:
        full = colonne == 0
        if full and not previous_full:
            band += 1
            band_starts.append((y0, band))
        if line is not None:
            line.bande = band
        previous_full = full
    layout.band_starts = band_starts
    pt.layout = layout
    source_rank = {id(line): index for index, line in enumerate(lines)}
    pt.lines = sorted(lines, key=lambda l: (l.bande, l.colonne, l.bbox[1], l.bbox[0], source_rank[id(l)]))
    for index, line in enumerate(pt.lines, 1):
        line.ordre_lecture = index


def extract_pages(pdf: Path | str) -> tuple[list[PageText], list[Any]]:
    """Pages (lignes + bbox + police, en-têtes/pieds retirés) et `get_toc()` du PDF."""
    s = get_settings()
    doc = pymupdf.open(str(pdf))
    pages: list[PageText] = []
    band_texts: dict[str, int] = {}
    try:
        toc = doc.get_toc()
        for pno, page in enumerate(doc, start=1):
            tables = _tables(page)
            registry = SourceRegistry()
            lines, images = _raw_lines(page, page_no=pno, tables=tables, registry=registry)
            drawings = len(page.get_drawings())
            # Relevé sans condition : une page dont les seules lignes sont un en-tête/pied retiré plus bas
            # doit rester « dessinée » et non blanche (AD-8, revue Codex 1.2 B3).
            pt = PageText(page=pno, width=page.rect.width, height=page.rect.height, lines=lines, images=images,
                          drawings=drawings, image_area_ratio=_image_area_ratio(page), tables=tables,
                          source=registry)
            for line in lines:
                if line.bbox[1] < s.header_band_pt or line.bbox[3] > pt.height - s.footer_band_pt:
                    band_texts[line.text] = band_texts.get(line.text, 0) + 1
            pages.append(pt)
        # `ceil` évite qu'une occurrence sous le ratio configuré soit promue par troncature.
        min_pages = max(2, ceil(len(pages) * s.header_min_pages_ratio))
        for index, pt in enumerate(pages):
            page = doc[index]
            native_kept = _without_running_bands(pt, pt.lines, band_texts, min_pages)
            pt.native_text = bool(native_kept or pt.tables)
            if not pt.native_text and pt.visual:
                pt.ocr_attempted = True
                try:
                    textpage = page.get_textpage_ocr(language="fra", dpi=s.ocr_dpi, full=True)
                    ocr_registry = SourceRegistry()
                    ocr_lines, _ = _raw_lines(page, page_no=pt.page, textpage=textpage,
                                              tables=pt.tables, registry=ocr_registry)
                    retained_ocr = _without_running_bands(pt, ocr_lines, band_texts, min_pages,
                                                          registry=ocr_registry)
                    pt.lines = _merge_number_lines(retained_ocr)
                    # Le registre servi est celui de la couche réellement retenue : la couche native
                    # de cette page n'a rien laissé passer, ses lignes n'ancreraient donc aucun bloc.
                    pt.source = ocr_registry
                    pt.ocr_succeeded = bool(pt.lines)
                    if not pt.ocr_succeeded:
                        pt.ocr_error = "sortie_vide"
                except Exception as exc:  # PyMuPDF expose FzErrorLibrary hors hiérarchie RuntimeError
                    pt.lines = []
                    category = _ocr_error_category(exc)
                    pt.ocr_error = f"ocr_exception:{type(exc).__name__}:{category}"
                    logger.exception("Échec OCR page %s (%s)", pt.page, category)
            else:
                pt.lines = _merge_number_lines(native_kept)
    finally:
        doc.close()
    _mark_toc_pages(pages)
    return pages, toc


def anomalies_registre(pages: list[PageText], block_uids: dict[str, list[str]]) -> list[str]:
    """Union complète, intersection vide : chaque `SourceLine` est portée par un bloc **ou** retirée.

    C'est la seule preuve que le registre reste une source et non un décor : une ligne extraite qui
    n'aboutit ni dans un bloc ni dans un motif de retrait aurait disparu en silence, et une ligne
    présente dans deux blocs rendrait la même preuve citable à deux endroits.
    """
    attaches: dict[str, list[str]] = {}
    for block_id, uids in block_uids.items():
        for uid in uids:
            attaches.setdefault(uid, []).append(block_id)
    anomalies: list[str] = []
    connus: set[str] = set()
    for page in pages:
        for source in page.source.lines:
            connus.add(source.uid)
            porteurs = attaches.get(source.uid, [])
            motif = page.source.removed.get(source.uid)
            if len(porteurs) > 1:
                anomalies.append(f"{source.uid} rattachée à {len(porteurs)} blocs : {', '.join(porteurs)}")
            elif porteurs and motif is not None:
                anomalies.append(f"{source.uid} à la fois dans {porteurs[0]} et retirée ({motif})")
            elif not porteurs and motif is None:
                anomalies.append(f"{source.uid} sans bloc ni motif de retrait")
    anomalies.extend(f"{uid} porté par {', '.join(porteurs)} sans exister au registre"
                     for uid, porteurs in sorted(attaches.items()) if uid not in connus)
    return anomalies


def ordonner_pages(pages: list[PageText]) -> None:
    """Arrête l'ordre de lecture de chaque page — colonnes comprises — avant toute exploitation."""
    for pt in pages:
        _assign_reading_order(pt)


class _Builder:
    def __init__(self, doc_id: str, title: str, *, plan: dict[str, NoeudVerifie] | None = None) -> None:
        self.doc_id = doc_id
        self.plan = plan or {}  # node_id → nœud d'une proposition vérifiée (story 4.2c)
        self.root = Node(node_id=doc_id, level=0, title=title)
        self.nodes: dict[str, Node] = {doc_id: self.root}
        self.order: list[str] = [doc_id]
        self.blocks: list[Block] = []
        self.current = self.root
        self.numbers: list[str] = []  # ordre d'apparition, pour `numerotation_non_monotone`
        self.duplicates: list[str] = []
        self.continues = 0
        self.parents: dict[str, str] = {}
        self.starters: dict[str, tuple[str, list[PageLine]]] = {}
        self.body_indents: dict[str, list[float]] = {}
        self.block_uids: dict[str, list[str]] = {}  # story 4.2c : bloc → lignes source, hors `Document`

    def node_propose(self, node_id: str) -> Node:
        """Instancie à la demande un nœud **déjà prouvé**, et ses ancêtres, dans l'ordre de lecture.

        La création paresseuse est ce qui garde `Node.items` fidèle à la lecture : un parent voit son
        propre texte arriver avant le `NodeRef` de l'enfant qui le suit dans la page. Le `node_id` et
        le titre viennent du plan, donc du code et du registre — jamais de la réponse du modèle.
        """
        if node_id in self.nodes:
            return self.nodes[node_id]
        spec = self.plan[node_id]
        parent = self.root if spec.parent_id is None else self.node_propose(spec.parent_id)
        node = Node(node_id=node_id, level=spec.level, title=spec.title)
        self.nodes[node_id] = node
        self.parents[node_id] = parent.node_id
        self.order.append(node_id)
        parent.items.append(NodeRef(node_id=node_id))
        return node

    def node_for(self, numero: str) -> Node:
        node_id = f"{self.doc_id}:a{numero}"
        if node_id in self.nodes:
            self.duplicates.append(numero)
            k = 2
            while f"{node_id}-{k}" in self.nodes:
                k += 1
            node_id = f"{node_id}-{k}"
        parts = numero.split(".")
        parent = self.root
        for n in range(len(parts) - 1, 0, -1):
            cand = f"{self.doc_id}:a{'.'.join(parts[:n])}"
            if cand in self.nodes:
                parent = self.nodes[cand]
                break
        # Le parseur ne donne aucun sens juridique au numéro. `type_clauses` est l'unique écrivain
        # sémantique de `Node.scope.kind`, à partir des relations et portées explicites résolues.
        node = Node(node_id=node_id, level=len(parts), title="")
        self.nodes[node_id] = node
        self.parents[node_id] = parent.node_id
        self.order.append(node_id)
        parent.items.append(NodeRef(node_id=node_id))
        self.numbers.append(numero)
        return node

    def _has_later_sibling(self, node_id: str, future_numbers: list[str]) -> bool:
        """Simule les parents que le streaming donnera aux prochains numéros."""
        parent_id = self.parents.get(node_id)
        if parent_id is None:
            return False
        known = set(self.nodes)
        for number in future_numbers:
            parts = number.split(".")
            future_parent = self.root.node_id
            for depth in range(len(parts) - 1, 0, -1):
                candidate = f"{self.doc_id}:a{'.'.join(parts[:depth])}"
                if candidate in known:
                    future_parent = candidate
                    break
            if future_parent == parent_id:
                return True
            known.add(f"{self.doc_id}:a{number}")
        return False

    def dedent_closing_group(self, kind: str, lines: list[PageLine], *, tolerance_pt: float,
                             starter_max_lines: int, future_numbers: list[str]) -> None:
        """Rattache un paragraphe de clôture au parent géométriquement aligné.

        Un groupe non numéroté n'est dé-indenté que lorsqu'il suit le dernier item compact d'une
        fratrie numérotée et que son retrait coïncide avec un retrait de corps déjà observé chez
        un ancêtre. Cette combinaison arbre + géométrie évite toute convention d'article ou de
        vocabulaire juridique. Un premier corps ordinaire, une liste annoncée par deux-points ou un
        item long restent sous le nœud courant.
        """
        if kind != "para" or not lines or lines[0].number is not None or self.current is self.root:
            return
        starter = self.starters.get(self.current.node_id)
        parent_id = self.parents.get(self.current.node_id)
        if starter is None or parent_id is None:
            return
        starter_kind, starter_lines = starter
        if (starter_kind != "para" or len(starter_lines) > starter_max_lines
                or starter_lines[-1].text.rstrip().endswith(":")
                or self.body_indents.get(self.current.node_id)):
            return
        parent = self.nodes[parent_id]
        # Le nœud courant doit clore une vraie fratrie, pas seulement être son dernier enfant *déjà
        # vu*. La simulation des numéros restants emploie la même règle de parent que `node_for` et
        # empêche de déplacer le corps de 1.2 si un frère 1.3 apparaît ensuite.
        if (len(parent.children) < 2 or parent.children[-1] != self.current.node_id
                or self._has_later_sibling(self.current.node_id, future_numbers)):
            return
        x = lines[0].bbox[0]
        if any(abs(x - known) <= tolerance_pt for known in self.body_indents.get(parent_id, [])):
            self.current = parent

    def add_block(self, page: int, lines: list[PageLine], kind: str, *, continues: str | None = None,
                  source_field: str | None = None) -> Block:
        loc = f"p{page}"
        seq = sum(1 for b in self.blocks if b.loc == loc) + 1
        block_id = f"{self.doc_id}:{loc}:{seq}"
        bbox = [min(l.bbox[0] for l in lines), min(l.bbox[1] for l in lines),
                max(l.bbox[2] for l in lines), max(l.bbox[3] for l in lines)]
        block = Block(block_id=block_id, text="\n".join(l.text for l in lines), loc=loc, seq=seq, page=page,
                      bbox=bbox, kind=kind, continues=continues, source_field=source_field,  # type: ignore[arg-type]
                      lines=[Line(line_id=f"{block_id}:l{i}", text=l.text, bbox=l.bbox) for i, l in enumerate(lines, 1)])
        self.blocks.append(block)
        self.block_uids[block_id] = [uid for line in lines for uid in line.source_uids]
        self.current.items.append(BlockRef(block_id=block_id))
        if lines[0].number is not None:
            self.starters.setdefault(self.current.node_id, (kind, list(lines)))
        else:
            self.body_indents.setdefault(self.current.node_id, []).append(lines[0].bbox[0])
        if continues:
            self.continues += 1
        return block


def _is_heading(line: PageLine, nxt: PageLine | None) -> bool:
    """Texte sur la ligne de base d'un numéro : titre si gros, ou court sans ponctuation finale et suivi d'une majuscule."""
    if line.size >= get_settings().title_min_size_pt:
        return True
    if line.text.rstrip().endswith((".", ";", ":", ",")):
        return False
    return nxt is None or nxt.number is not None or nxt.bullet or nxt.text[:1].isupper()


def _noeud_de_ligne(line: PageLine, node_of_uid: dict[str, str]) -> str | None:
    """Nœud proposé qui couvre cette ligne, s'il y en a un."""
    return next((node_of_uid[uid] for uid in line.source_uids if uid in node_of_uid), None)


def _segment_page(pt: PageText, node_of_uid: dict[str, str] | None = None,
                  ) -> list[tuple[str, list[PageLine]]]:
    """Découpe une page en (kind, lignes) : heading / para / list, dans l'ordre de lecture.

    Avec `node_of_uid`, un groupe ne peut pas franchir une frontière de nœud prouvé : une section
    dont le titre suit son voisin à interligne serré serait sinon collée dans le même paragraphe, le
    bloc entier partirait sous le premier nœud, et le second nœud — pourtant accepté et annoncé par
    le rapport — n'existerait dans aucun arbre.
    """
    groups: list[tuple[str, list[PageLine]]] = []
    s = get_settings()
    lines = pt.lines
    noeuds = node_of_uid or {}
    for i, line in enumerate(lines):
        prev = lines[i - 1] if i else None
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        # Story 4.2c : aucune continuation ne franchit une colonne, une bande ou un nœud prouvé. Sur
        # une page mono-colonne sans proposition, `contigu` vaut toujours vrai : rien ne change.
        contigu = prev is not None and (prev.colonne, prev.bande) == (line.colonne, line.bande) \
            and (not noeuds or _noeud_de_ligne(prev, noeuds) == _noeud_de_ligne(line, noeuds))
        if line.number is not None:
            if _is_heading(line, nxt):
                groups.append(("heading", [line]))
            else:
                groups.append(("para", [line]))
            continue
        if line.bullet:
            if groups and groups[-1][0] == "list" and contigu:
                groups[-1][1].append(line)
            else:
                groups.append(("list", [line]))
            continue
        if groups and prev is not None and contigu:
            kind, prev_lines = groups[-1]
            gap = line.bbox[1] - prev.bbox[1]
            height = max(prev.bbox[3] - prev.bbox[1], 1.0)
            close = gap <= s.para_gap_ratio * height
            if kind == "list" and close and (line.bbox[0] > prev_lines[0].bbox[0] + s.list_indent_pt
                                             or (line.text[:1].islower() and not prev.text.endswith(_TERMINAL))):
                prev_lines.append(line)  # continuation d'un item : indentée, ou alignée et phrase non terminée
                continue
            if kind == "para" and close and line.size < s.title_min_size_pt and prev.size < s.title_min_size_pt:
                prev_lines.append(line)
                continue
            if kind == "para" and prev.number is not None and len(prev_lines) == 1 and line.text[:1].islower():
                prev_lines.append(line)  # « 3.1.1.1.1 L'incendie, » puis « c'est-à-dire … » : même alinéa
                continue
        kind = "heading" if line.size >= s.title_min_size_pt and line.number is None and not line.text.endswith(".") \
            else "para"
        groups.append((kind, [line]))
    if not pt.tables:
        return groups
    # La clé de tri réunit tables et groupes dans un seul ordre de lecture. Sur une page
    # mono-colonne elle vaut `(0, 1, y, x)` pour tout le monde : le tri est celui d'avant, à
    # l'identique. Sur une page à colonnes, une table appartient à la colonne et à la bande de sa
    # propre boîte, et ne peut donc pas se glisser entre deux groupes de l'autre colonne.
    ordered: list[tuple[int, int, float, float, str, list[PageLine]]] = [
        (lines[0].bande, lines[0].colonne, lines[0].bbox[1], lines[0].bbox[0], kind, lines)
        for kind, lines in groups
    ]
    for table in pt.tables:
        if not table.sert_un_bloc:
            # Cas limite explicite : une table détectée qui ne rend aucune rangée ne produit aucun
            # bloc. Ses lignes brutes seraient alors du contenu réellement non servi — elles sont
            # inscrites au motif de retrait qui le dit, jamais perdues entre l'absorption et un bloc
            # qui n'existe pas.
            pt.source.retirer(table.source_uids, "table_sans_bloc")
            continue
        row_height = max((table.bbox[3] - table.bbox[1]) / max(len(table.rows), 1), 1.0)
        rows = []
        for index, row in enumerate(table.rows):
            bbox = table.row_bboxes[index] if index < len(table.row_bboxes) else [
                table.bbox[0], round(table.bbox[1] + index * row_height, 2), table.bbox[2],
                round(min(table.bbox[1] + (index + 1) * row_height, table.bbox[3]), 2),
            ]
            colonne = pt.layout.colonne(table.bbox)
            rows.append(PageLine(text=" | ".join(row), bbox=bbox, size=0.0,
                                 colonne=colonne, bande=pt.layout.bande(table.bbox[1])))
        # Les lignes brutes absorbées sont portées par la **première** rangée du bloc : le bloc les
        # porte donc exactement une fois, quel que soit le nombre de rangées reconstruites — les
        # rangées ne sont pas les lignes source et n'ont aucune raison de leur correspondre une à une.
        rows[0].source_uids = list(table.source_uids)
        ordered.append((pt.layout.bande(table.bbox[1]), pt.layout.colonne(table.bbox),
                        table.bbox[1], table.bbox[0], "table", rows))
    ordered.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [(kind, lines) for _, _, _, _, kind, lines in ordered]


def _noeud_de_groupe(lines: list[PageLine], node_of_uid: dict[str, str], *, page: int) -> str:
    """Le nœud prouvé d'un groupe, **prouvé ligne à ligne** — jamais hérité d'un voisin.

    La couverture d'une proposition acceptée est totale : toute ligne du registre appartient à un
    intervalle, donc tout groupe qui porte des lignes du registre a son nœud. Rattacher un groupe au
    nœud de son voisin servait au contraire une affectation que la proposition n'avait ni portée ni
    prouvée, sous l'annonce « proposition vérifiée ».

    Deux refus, parce qu'un bloc servi ne peut pas être moins prouvé que la proposition qui l'annonce :
    un groupe sans aucune ligne prouvée serait servi sans liaison ligne/bloc, et un groupe à cheval
    sur deux nœuds — une table atomique que la proposition scinde, ou une ligne fusionnée dont les
    deux uid tombent de part et d'autre d'une frontière — serait servi entier sous l'un des deux.
    """
    noeuds = {node_of_uid[uid] for line in lines for uid in line.source_uids if uid in node_of_uid}
    if len(noeuds) == 1:
        return noeuds.pop()
    apercu = " / ".join(line.text for line in lines[:3])[:200]
    if not noeuds:
        raise StructureRefusee(
            "affectation_non_prouvee",
            f"p{page} : un bloc serait servi sans aucune ligne source prouvée ({apercu!r})")
    raise StructureRefusee(
        "affectation_non_prouvee",
        f"p{page} : un bloc atomique serait servi à cheval sur {len(noeuds)} nœuds prouvés "
        f"({', '.join(sorted(noeuds))}) : {apercu!r}")


def build_document(pages: list[PageText], *, edition: str, source_hash: str, toc: list[Any],
                   doc_id: str = DOC_ID, title: str = TITLE, source_url: str | None = None,
                   structure: StructureProposee | None = None) -> tuple[Document, dict[str, Any]]:
    """Arbre de blocs + méta de construction (numéros dans l'ordre, doublons, `continues`).

    Avec `structure`, l'arbre vient d'une proposition **prouvée sur le registre de lignes** : le
    vérificateur passe d'abord, un refus lève `StructureRefusee` et rien n'est construit (AD-16 :
    jamais de repli silencieux vers l'heuristique numérique). Sans `structure`, l'heuristique
    `_NUMBER_RE` reste le chemin nominal, inchangée.
    """
    # L'ordre de lecture est arrêté **avant** toute lecture des pages : la numérotation à venir,
    # les continuations et la segmentation le supposent tous déjà fixé (story 4.2c).
    ordonner_pages(pages)
    plan: dict[str, NoeudVerifie] = {}
    node_of_uid: dict[str, str] = {}
    if structure is not None:
        registre = registre_lignes(pages)
        verdict = verifier(structure, registre, doc_id=doc_id, settings=get_settings())
        if not verdict.accepte:
            raise StructureRefusee(verdict.motif or "refus", verdict.detail)
        plan, node_of_uid = arbre(structure, registre, doc_id)
    b = _Builder(doc_id, title, plan=plan)
    toc_node: Node | None = None
    last_text_block: Block | None = None  # dernier bloc para|list de la page précédente
    precedent_noeud: str | None = None  # nœud proposé qui le portait, pour ne pas continuer à travers
    article_seen = False
    document_has_articles = any(line.number is not None for page in pages for line in page.lines)
    document_has_toc = any(page.is_toc for page in pages)
    future_numbers = [line.number for page in pages for line in page.lines
                      if line.number is not None]

    def add_preliminary_groups(page: PageText, groups: list[tuple[str, list[PageLine]]], *,
                               table_source: str) -> None:
        """Fusionne le texte préliminaire contigu, mais jamais à travers une table atomique."""
        pending: list[PageLine] = []

        def flush() -> None:
            if pending:
                b.add_block(page.page, list(pending), "autre",
                            source_field="ocr" if page.ocr_succeeded else None)
                pending.clear()

        for kind, lines in groups:
            if kind == "table":
                flush()
                # Le kind reste fidèle à la source ; la non-citabilité est une provenance explicite
                # que l'index peut appliquer aux recherches, fréquences et fenêtres.
                b.add_block(page.page, lines, "table", source_field=table_source)
            else:
                pending.extend(lines)
        flush()

    for pt in pages:
        groups = _segment_page(pt, node_of_uid)
        if structure is not None:
            # Une proposition vérifiée gouverne **tout** le rattachement. Les branches TdM et
            # « préliminaire » sont écrites pour l'heuristique numérique : elles rattachent à la
            # racine ou au nœud `:tdm` des lignes que la proposition place ailleurs, et faisaient
            # disparaître des nœuds pourtant acceptés — jusqu'à trouer le chemin positionnel servi.
            page_last = None
            for index, (kind, lines) in enumerate(groups):
                node_id = _noeud_de_groupe(lines, node_of_uid, page=pt.page)
                b.current = b.node_propose(node_id)
                continues = None
                # Scission de page (AD-2), inchangée, mais jamais à travers un nœud : deux sections
                # différentes ne se continuent pas l'une l'autre.
                if index == 0 and kind in ("para", "list") and last_text_block is not None \
                        and last_text_block.kind == kind and node_id == precedent_noeud \
                        and not last_text_block.text.rstrip().endswith(_TERMINAL):
                    continues = last_text_block.block_id
                blk = b.add_block(pt.page, lines, kind, continues=continues,
                                  source_field="ocr" if pt.ocr_succeeded else None)
                if kind in ("para", "list"):
                    page_last = blk
                    precedent_noeud = node_id
            if page_last is not None:
                last_text_block = page_last
            elif groups:
                last_text_block = None
            continue
        if pt.is_toc:
            if toc_node is None:
                toc_node = Node(node_id=f"{doc_id}:tdm", level=1, title="Table des matières")
                b.nodes[toc_node.node_id] = toc_node
                b.order.append(toc_node.node_id)
                b.root.items.append(NodeRef(node_id=toc_node.node_id))
            saved, b.current = b.current, toc_node
            first_article_group = next((i for i, (_, lines) in enumerate(groups)
                                        if lines[0].number is not None), None)
            toc_groups = groups if first_article_group is None else groups[:first_article_group]
            add_preliminary_groups(pt, toc_groups, table_source="tdm")
            b.current = saved
            last_text_block = None
            if first_article_group is None:
                continue
            # Une première clause sur la même page clôt la TdM : elle et tous les groupes suivants
            # appartiennent au corps citable.
            groups = groups[first_article_group:]
            article_seen = True
        first_article_group = next((i for i, (_, lines) in enumerate(groups)
                                    if lines[0].number is not None), None)
        if (document_has_articles or document_has_toc) and not article_seen and not pt.after_toc:
            preliminary_count = len(groups) if first_article_group is None else first_article_group
            add_preliminary_groups(pt, groups[:preliminary_count], table_source="preliminaire")
            if first_article_group is None:
                last_text_block = None
                continue
            groups = groups[first_article_group:]
            article_seen = True
        elif first_article_group is not None:
            article_seen = True
        first = True
        page_last: Block | None = None
        for kind, lines in groups:
            continues = None
            # Scission de page (AD-2) : le premier bloc de la page, de même kind que le dernier bloc texte de la page
            # précédente, sans numéro d'article, alors que ce dernier ne finit pas une phrase (structure, pas casse).
            if first and kind in ("para", "list") and lines[0].number is None and last_text_block is not None \
                    and last_text_block.kind == kind and not last_text_block.text.rstrip().endswith(_TERMINAL):
                continues = last_text_block.block_id
            first = False
            if lines[0].number is not None:
                if future_numbers and future_numbers[0] == lines[0].number:
                    future_numbers.pop(0)
                elif lines[0].number in future_numbers:
                    future_numbers.remove(lines[0].number)
                b.current = b.node_for(lines[0].number)
                if kind == "heading":
                    b.current.title = lines[0].text
            elif continues is None:
                settings = get_settings()
                b.dedent_closing_group(
                    kind, lines, tolerance_pt=settings.dedent_tolerance_pt,
                    starter_max_lines=settings.dedent_starter_max_lines,
                    future_numbers=future_numbers,
                )
            blk = b.add_block(pt.page, lines, kind, continues=continues,
                              source_field="ocr" if pt.ocr_succeeded else None)
            if kind in ("para", "list"):
                page_last = blk
            if lines[0].number is not None and kind == "para":
                b.current.title = lines[0].text[:80]
        last_text_block = page_last
    anomalies = anomalies_registre(pages, b.block_uids)
    if anomalies:
        raise ValueError("registre de lignes source incohérent : " + " ; ".join(anomalies[:20]))
    # Réconciliation : un nœud prouvé que l'arbre bâti ne porte pas est un refus, jamais un `info`.
    # Sans elle, `run()` pouvait publier « proposition vérifiée : N nœud(s) » sur un arbre qui n'en
    # contient qu'une partie — exactement le « dégradé en silence » qu'AD-16 nomme.
    manquants = sorted(node_id for node_id in plan if node_id not in b.nodes)
    if manquants:
        raise StructureRefusee(
            "noeud_non_construit",
            f"{len(manquants)} nœud(s) prouvé(s) absent(s) de l'arbre bâti : {', '.join(manquants[:20])}")
    toc_gaps = _apply_toc(b, toc)
    nodes = [b.nodes[nid] for nid in b.order]
    doc = Document(doc_id=doc_id, kind="contrat", title=title, edition=edition, lang="fr", nodes=nodes,
                   blocks=b.blocks, source_url=source_url, source_hash=source_hash,
                   ingest_fingerprint=ingest_fingerprint(structure))
    printed_toc = _printed_toc_entries(pages)
    meta = {"numbers": b.numbers, "duplicates": b.duplicates, "continues": b.continues, "toc": toc,
            "toc_gaps": toc_gaps, "printed_toc": printed_toc,
            # Le lien bloc → lignes source reste **interne à l'ingestion** : il ne traverse ni
            # `Document`, ni `document.json`, ni le loader (AD-2 inchangé).
            "source_uids": b.block_uids}
    return doc, meta


def _printed_toc_entries(pages: list[PageText]) -> list[tuple[str, str]]:
    """Sections numérotées annoncées par la TdM, titres multilignes réunis par géométrie de colonne."""
    entries: list[tuple[str, str]] = []
    settings = get_settings()
    for page in pages:
        if not page.is_toc:
            continue
        first_article_y = min((line.bbox[1] for line in page.lines if line.number is not None),
                              default=page.height)
        lines = [line for line in page.lines
                 if line.bbox[1] < first_article_y and not _is_toc_title(line.text)]
        starts: list[tuple[PageLine, re.Match[str]]] = []
        for line in lines:
            text = line.text.strip()
            direct = _PRINTED_TOC_RE.match(text)
            if direct:
                entries.append((direct.group(1), direct.group(2).strip(" .")))
                continue
            numbered = _TOC_NUMBER_RE.match(text)
            if numbered:
                starts.append((line, numbered))
        starts.sort(key=lambda item: (item[0].bbox[0], item[0].bbox[1], item[0].text))
        for line, numbered in starts:
            next_y = page.height
            for other, _ in starts:
                if other.bbox[1] > line.bbox[1] \
                        and abs(other.bbox[0] - line.bbox[0]) <= settings.toc_column_tolerance_pt:
                    next_y = min(next_y, other.bbox[1])
            page_numbers = [candidate for candidate in lines
                            if _PAGE_NUMBER_RE.fullmatch(candidate.text.strip())
                            and candidate.bbox[0] > line.bbox[0]
                            and abs(candidate.bbox[1] - line.bbox[1])
                            <= settings.toc_page_number_baseline_pt]
            if not page_numbers:
                continue
            boundary_x = min((candidate.bbox[0] for candidate in page_numbers), default=page.width)
            title_parts = [numbered.group(2).strip(" .")]
            last_bottom = line.bbox[3]
            continuations = sorted(
                (candidate for candidate in lines
                 if line.bbox[1] < candidate.bbox[1] < next_y
                 and candidate.bbox[0] >= line.bbox[0] - settings.toc_indent_tolerance_pt
                 and candidate.bbox[2] < boundary_x
                 and not _PAGE_NUMBER_RE.fullmatch(candidate.text.strip())
                 and _TOC_NUMBER_RE.match(candidate.text.strip()) is None),
                key=lambda candidate: (candidate.bbox[1], candidate.bbox[0]),
            )
            for candidate in continuations:
                line_height = max(candidate.bbox[3] - candidate.bbox[1], 1.0)
                if candidate.bbox[1] - last_bottom > settings.toc_line_gap_ratio * line_height:
                    break
                title_parts.append(candidate.text.strip(" ."))
                last_bottom = candidate.bbox[3]
            entries.append((numbered.group(1), " ".join(part for part in title_parts if part)))
        for table in page.tables:
            if table.bbox[1] >= first_article_y:
                continue
            pending: tuple[str, list[str]] | None = None
            for row in table.rows:
                cells = [cell.strip() for cell in row if cell.strip()]
                if not cells or any(_is_toc_title(cell) for cell in cells):
                    continue
                text = " ".join(cells)
                direct = _PRINTED_TOC_RE.match(text)
                if direct:
                    entries.append((direct.group(1), direct.group(2).strip(" .")))
                    pending = None
                    continue
                numbered = _TOC_NUMBER_RE.match(text)
                if numbered:
                    pending = (numbered.group(1), [numbered.group(2).strip(" .")])
                elif pending:
                    if _PAGE_NUMBER_RE.fullmatch(cells[-1]):
                        pending[1].extend(cells[:-1])
                        entries.append((pending[0], " ".join(pending[1]).strip(" .")))
                        pending = None
                    else:
                        pending[1].extend(cells)
    # Les doublons restent intentionnellement présents : le rapport doit les signaler, y compris
    # quand deux lignes annoncent exactement le même titre et pas seulement quand elles divergent.
    return entries


def _apply_toc(b: _Builder, toc: list[Any]) -> list[str]:
    """Signets du PDF (`get_toc()` : [niveau, titre, page]) : un signet « 1.12 Contenu » donne son titre au nœud
    `a1.12` s'il n'en a pas ; un signet numéroté sans nœud correspondant est un écart (alerte `tdm_pdf_ecart`)."""
    gaps: list[str] = []
    # Index des numéros réellement servis : `{doc_id}:a3.1` sur le chemin heuristique, le numéro lu
    # dans le titre d'un nœud positionnel `{doc_id}:s1.2` sur le chemin proposé (story 4.2c).
    par_numero: dict[str, Node] = {}
    for node_id, node in b.nodes.items():
        numero = numero_de_noeud(b.doc_id, node_id, node.title)
        if numero is not None and node_id != b.doc_id:
            par_numero.setdefault(numero, node)
    for item in toc:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        m = _TOC_NUMBER_RE.match(str(item[1]).strip())
        if not m:
            continue
        node = par_numero.get(m.group(1))
        if node is None:
            gaps.append(f"{m.group(1)} (p. {item[2]})")
        elif not node.title:
            # Un nœud d'une proposition vérifiée a toujours son titre — relu au registre. Le signet
            # ne complète donc que les nœuds numérotés que l'heuristique a laissés sans intitulé.
            node.title = f"{m.group(1)} {m.group(2)}".strip()
    return gaps


def build_summary(doc: Document) -> str:
    """Sommaire compact : nœuds de niveau ≤ `summary_max_level` avec leur titre et leur première page.

    Abaissé de 3 à 2 en story 1.3 : mesuré à 10 153 tokens (reason) au niveau ≤ 3, au-delà du seuil
    de décision de la spec (5 000) ; les sous-articles restent atteignables par `chercher` (blocs).
    """
    max_level = get_settings().summary_max_level
    lines = [f"<!-- {doc.doc_id} · edition {doc.edition} · source_hash {doc.source_hash} · "
             f"ingest_fingerprint {doc.ingest_fingerprint} -->", f"# {doc.title}", ""]
    by_id = {n.node_id: n for n in doc.nodes}
    first_page: dict[str, int | None] = {}

    def page_of(node: Node) -> int | None:
        if node.node_id in first_page:
            return first_page[node.node_id]
        p: int | None = None
        for item in node.items:
            if isinstance(item, BlockRef):
                block = doc.block(item.block_id)
                q = block.page if is_citable(block) else None
            else:
                q = page_of(by_id[item.node_id])
            if q is not None and (p is None or q < p):
                p = q
        first_page[node.node_id] = p
        return p

    def walk(node: Node) -> None:
        for child_id in node.children:
            child = by_id[child_id]
            if 1 <= child.level <= max_level \
                    and any(is_citable(doc.block(block_id)) for block_id in child.blocks):
                pg = page_of(child)
                lines.append(f"{'  ' * (child.level - 1)}- `{child.node_id}` · {child.title} · p. {pg}")
            walk(child)

    walk(by_id[doc.doc_id])
    return "\n".join(lines).rstrip() + "\n"


def run(data_dir: Path, *, edition: str | None, doc_id: str = DOC_ID,
        title: str | None = None) -> tuple[Report, ManifestEntry]:
    """Ingère `data_dir/source.pdf` ; toute erreur de source devient un check bloquant, jamais une trace Python."""
    manifest_path = data_dir.parent / "manifest.json"
    fingerprint = ingest_fingerprint()
    resolved_edition = edition if edition is not None else (DEFAULT_EDITION if doc_id == DOC_ID else "")
    try:
        raw_manifest = read_manifest(manifest_path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        detail = f"{manifest_path} illisible, rien n'a été écrit : {type(exc).__name__}: {exc}"[:2000]
        report = Report(doc_id=doc_id, checks=[Check(name="manifest_illisible", level="bloquant", detail=detail)])
        return report, ManifestEntry(status="quarantaine", source_hash="", ingest_fingerprint=fingerprint,
                                     document_hash="", edition=resolved_edition, gate=None)
    source_hash = ""
    doc: Document | None = None
    summary = ""
    previous = load_previous(data_dir / "document.json")
    try:
        document_title = TITLE if title is None and doc_id == DOC_ID else title
        if document_title is None:
            raise ValueError("--title est obligatoire pour un contrat autre que le document de compatibilité AXA")
        document_title = document_title.strip()
        if not document_title:
            raise ValueError("--title ne peut pas être vide")
        if not resolved_edition.strip():
            raise ValueError("--edition doit reprendre le libellé imprimé du contrat")
        pdf_path = data_dir / "source.pdf"
        expected_path = data_dir / "source.sha256"
        # AD-7 : l'empreinte de référence est obligatoire et vérifiée avant toute extraction (jamais d'ingestion
        # d'un PDF non vérifié, même si `fetch_source` n'a pas été utilisé).
        if not expected_path.is_file():
            raise ValueError("source.sha256 absent : l'empreinte de référence du PDF est obligatoire (AD-7)")
        expected = expected_path.read_text("utf-8").split()[:1]
        if not expected or not _SHA256_RE.match(expected[0].lower()):
            raise ValueError("source.sha256 mal formé : 64 caractères hexadécimaux attendus")
        source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        if expected[0].lower() != source_hash:
            raise ValueError(f"source.pdf : sha256 {source_hash} ≠ source.sha256 — relancer fetch_source")
        pages, toc = extract_pages(pdf_path)
        source_url_path = data_dir / "source.url"
        source_url = source_url_path.read_text("utf-8").strip() if source_url_path.is_file() else None
        if doc_id != DOC_ID:
            parsed_url = urlsplit(source_url or "")
            http_url = parsed_url.scheme in {"http", "https"} and bool(parsed_url.netloc)
            gs_url = bool(source_url and GS_URL_RE.fullmatch(source_url))
            if not source_url or not (http_url or gs_url) or any(char.isspace() for char in source_url):
                raise ValueError(
                    "source.url doit contenir une URL HTTP(S) ou gs://bucket/objet non vide pour un contrat générique"
                )
        # Story 4.2c : la proposition est un artefact, jamais un appel à chaud (AD-2, stabilité).
        # Absente, l'heuristique numérique reste le chemin nominal ; présente, elle est revérifiée à
        # chaque ingestion et son refus met le document en quarantaine.
        # `presente()` et non `is_file()` : un répertoire ou un lien pendant répondent `False` à
        # `is_file()` et faisaient retomber l'ingestion sur l'heuristique **en silence**, alors que
        # `charger()` promet de les classer `proposition_illisible` (AD-16). Seule l'absence au sens
        # du système de fichiers est une absence.
        structure_path = data_dir / "structure.json"
        structure = charger(structure_path) if presente(structure_path) else None
        # La proposition appliquée entre dans l'empreinte : deux ingestions du même PDF qui rendent
        # des `node_id` différents ne peuvent pas porter la même (AD-2, lu par le loader).
        fingerprint = ingest_fingerprint(structure)
        doc, meta = build_document(pages, edition=resolved_edition, source_hash=source_hash, toc=toc, doc_id=doc_id,
                                   title=document_title, source_url=source_url, structure=structure)
        summary = build_summary(doc)
        detail = (None if structure is None else
                  f"proposition vérifiée : {len(structure.noeuds)} nœud(s) sur lignes source")
        report = build_pdf_report(doc, previous, pages=pages, numbers=meta["numbers"], duplicates=meta["duplicates"],
                                  continues=meta["continues"], toc=toc, toc_gaps=meta["toc_gaps"],
                                  printed_toc=meta["printed_toc"], summary=summary, structure=detail)
    except ValidationError as exc:
        report = report_from_validation_error(doc_id, exc)
    except StructureRefusee as exc:
        report = Report(doc_id=doc_id, checks=[structure_check(exc.motif, exc.detail)])
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        # pymupdf lève FileDataError (RuntimeError) sur un PDF corrompu ; OSError si `source.pdf` manque.
        detail = f"{type(exc).__name__}: {exc}"[:2000]
        report = Report(doc_id=doc_id, checks=[Check(name="source_illisible", level="bloquant", detail=detail)])
    status = "quarantaine" if report.blocking else "servi"
    document_hash = ""
    if doc is not None and not report.blocking:
        doc_text = document_json(doc)
        write_atomic(data_dir / "document.json", doc_text)
        write_atomic(data_dir / "summary.md", summary)
        document_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
    else:
        for stale in ("document.json", "summary.md"):  # jamais un artefact périmé à côté d'un manifest en quarantaine
            (data_dir / stale).unlink(missing_ok=True)
    write_atomic(data_dir / "report.json", json.dumps(report.model_dump(), indent=2, ensure_ascii=False) + "\n")
    entry = merge_manifest(manifest_path, raw_manifest, doc_id,
                           ManifestEntry(status=status, source_hash=source_hash, ingest_fingerprint=fingerprint,
                                         document_hash=document_hash, edition=resolved_edition,
                                         overlay_hash=overlay_hash(data_dir), gate=None))
    return report, entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("doc_id", nargs="?", default=DOC_ID)
    parser.add_argument("--data", default="data", type=Path)
    parser.add_argument("--edition")
    parser.add_argument("--title")
    args = parser.parse_args(argv)
    # Résoudre `data/{doc_id}` avant ce contrôle autoriserait séparateurs et `..` à sortir du dépôt.
    if len(args.doc_id) > DOC_ID_MAX or not DOC_ID_RE.fullmatch(args.doc_id):
        print(f"doc_id invalide (slug [a-z0-9-]+ de {DOC_ID_MAX} caractères maximum attendu) : "
              f"{args.doc_id!r}", file=sys.stderr)
        return 2
    report, entry = run(args.data / args.doc_id, edition=args.edition, doc_id=args.doc_id, title=args.title)
    for c in report.checks:
        print(f"[{c.level}] {c.name}: {c.detail}")
    print(f"stats: {json.dumps(report.stats, ensure_ascii=False)}")
    gate = "null" if entry.gate is None else f"{entry.gate.profile}, evals_ok={entry.gate.evals_ok}"
    line = f"{args.doc_id} : {entry.status} (edition {entry.edition}, gate: {gate})"
    if report.blocking:
        print(line, file=sys.stderr)
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
