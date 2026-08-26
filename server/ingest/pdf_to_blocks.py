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
from server.ingest.report import build_pdf_report, report_from_validation_error

DOC_ID = "axa-lu-optihome-2017"
TITLE = "Conditions d’assurances OptiHome (multirisques habitation)"
DEFAULT_EDITION = "juin 2017"

# Entrent dans `ingest_fingerprint` : toute modification change les IDs attendus (AD-2, stabilité).
PARSER_VERSION = "9"  # story 3.3 review : la clôture exige la fin réelle de la fratrie
SEGMENTATION_RULES = ("numero:^\\d+(\\.\\d+)*$@x0<article_number_max_x=>noeud a{numero}(parent=prefixe);"
                      "titre:meme_ligne_de_base(size>=title_min_size_pt|sans_ponct_finale&suite_majuscule)=>heading;"
                      "puce:Wingdings|^•=>list(item;continuation=indent>list_indent_pt|minuscule&prec!~[.;:]$);"
                      "gap>para_gap_ratio*h=>para;"
                      "entete:bande&(recurrent|numero_page|MAJUSCULES<=header_caps_max_size_pt);"
                      "table:find_tables=>bloc_atomique(cellules=' | ')&lignes_source_exclues;"
                      "ocr:page_visuelle_sans_texte_retenu=>get_textpage_ocr(fra)&source_field=ocr;"
                      "tdm:entrees_structurees(numero+page|points_de_conduite+page)|intitule_autonome_visible"
                      "=>continuation_si_entrees_structurees;sans_rearmement_apres_corps;"
                      "table:reste_atomique&source_field=tdm|preliminaire_si_non_citable;"
                      "preliminaire:avant_tdm_ou_premier_article=>autre;apres_tdm=>contenu_citable;"
                      "mixte:union_aire_images/page>=mixed_page_image_density;numero_para+ligne_minuscule=>meme_para;"
                      "dedent:dernier_frere_reel+item_num_compact+alignement_corps_parent=>parent;"
                      "continues:page_suivante&meme_kind(para|list)&sans_numero&prec!~[.;:]$;"
                      "toc:get_toc()=>titres manquants+tdm_pdf_ecart")
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


def ingest_fingerprint() -> str:
    s = get_settings()
    payload = json.dumps({"parser": PARSER_VERSION, "schema": SCHEMA_VERSION, "segmentation": SEGMENTATION_RULES,
                          "flags": FLAGS, "normalize_version": normalize_version,
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
                                         # story 1.3 (revue P1) : le niveau de coupe du sommaire change summary.md
                                         "summary_max_level": s.summary_max_level}},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class PageLine:
    text: str
    bbox: list[float]
    size: float  # taille de police dominante
    bullet: bool = False
    number: str | None = None  # numéro d'article si la ligne en commence par un (fusionné)


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

    @property
    def visual(self) -> bool:
        return bool(self.images or self.drawings)


@dataclass
class PageTable:
    """Table détectée sur une page, conservée comme une unité dans l'ordre visuel."""

    bbox: list[float]
    rows: list[list[str]]
    row_bboxes: list[list[float]] = field(default_factory=list)


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


def _raw_lines(page: pymupdf.Page, *, textpage: Any | None = None,
               excluded: list[list[float]] | None = None) -> tuple[list[PageLine], int]:
    """Lignes brutes (texte nettoyé, bbox, taille dominante, puce) et nombre d'images."""
    out: list[PageLine] = []
    images = 0
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
            if excluded and any(_center_inside(bbox, table_bbox) for table_bbox in excluded):
                continue
            out.append(PageLine(text=text, bbox=bbox, size=size, bullet=bullet))
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
    """« 1.12 » seul sur sa ligne + « Contenu » sur la même ligne de base ⇒ une seule `Line` « 1.12 Contenu »."""
    s = get_settings()
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
                    and nxt.bbox[0] >= cur.bbox[2] - s.number_gap_tolerance_pt:
                cur.text = f"{cur.text}{FLAGS['merge_number_sep']}{nxt.text}"
                cur.bbox = [min(cur.bbox[0], nxt.bbox[0]), min(cur.bbox[1], nxt.bbox[1]),
                            max(cur.bbox[2], nxt.bbox[2]), max(cur.bbox[3], nxt.bbox[3])]
                cur.size = nxt.size
                i += 1
        out.append(cur)
        i += 1
    return out


def _without_running_bands(page: PageText, lines: list[PageLine], band_texts: dict[str, int],
                           minimum: int) -> list[PageLine]:
    """Retire les bandes récurrentes d'une couche native ou OCR et conserve leur diagnostic."""
    s = get_settings()
    kept: list[PageLine] = []
    for line in lines:
        in_band = line.bbox[1] < s.header_band_pt or line.bbox[3] > page.height - s.footer_band_pt
        running = band_texts.get(line.text, 0) >= minimum or _PAGE_NUMBER_RE.match(line.text) is not None \
            or (line.size <= s.header_caps_max_size_pt and line.text == line.text.upper())
        if in_band and running:
            if line.text not in page.removed:
                page.removed.append(line.text)
            continue
        kept.append(line)
    return kept


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
            lines, images = _raw_lines(page, excluded=[table.bbox for table in tables])
            drawings = len(page.get_drawings())
            # Relevé sans condition : une page dont les seules lignes sont un en-tête/pied retiré plus bas
            # doit rester « dessinée » et non blanche (AD-8, revue Codex 1.2 B3).
            pt = PageText(page=pno, width=page.rect.width, height=page.rect.height, lines=lines, images=images,
                          drawings=drawings, image_area_ratio=_image_area_ratio(page), tables=tables)
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
                    ocr_lines, _ = _raw_lines(page, textpage=textpage,
                                              excluded=[table.bbox for table in pt.tables])
                    retained_ocr = _without_running_bands(pt, ocr_lines, band_texts, min_pages)
                    pt.lines = _merge_number_lines(retained_ocr)
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


class _Builder:
    def __init__(self, doc_id: str, title: str) -> None:
        self.doc_id = doc_id
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


def _segment_page(pt: PageText) -> list[tuple[str, list[PageLine]]]:
    """Découpe une page en (kind, lignes) : heading / para / list, dans l'ordre de lecture."""
    groups: list[tuple[str, list[PageLine]]] = []
    s = get_settings()
    lines = pt.lines
    for i, line in enumerate(lines):
        prev = lines[i - 1] if i else None
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if line.number is not None:
            if _is_heading(line, nxt):
                groups.append(("heading", [line]))
            else:
                groups.append(("para", [line]))
            continue
        if line.bullet:
            if groups and groups[-1][0] == "list":
                groups[-1][1].append(line)
            else:
                groups.append(("list", [line]))
            continue
        if groups and prev is not None:
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
    ordered: list[tuple[float, float, str, list[PageLine]]] = [
        (lines[0].bbox[1], lines[0].bbox[0], kind, lines) for kind, lines in groups
    ]
    for table in pt.tables:
        row_height = max((table.bbox[3] - table.bbox[1]) / max(len(table.rows), 1), 1.0)
        rows = []
        for index, row in enumerate(table.rows):
            bbox = table.row_bboxes[index] if index < len(table.row_bboxes) else [
                table.bbox[0], round(table.bbox[1] + index * row_height, 2), table.bbox[2],
                round(min(table.bbox[1] + (index + 1) * row_height, table.bbox[3]), 2),
            ]
            rows.append(PageLine(text=" | ".join(row), bbox=bbox, size=0.0))
        ordered.append((table.bbox[1], table.bbox[0], "table", rows))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return [(kind, lines) for _, _, kind, lines in ordered]


def build_document(pages: list[PageText], *, edition: str, source_hash: str, toc: list[Any],
                   doc_id: str = DOC_ID, title: str = TITLE,
                   source_url: str | None = None) -> tuple[Document, dict[str, Any]]:
    """Arbre de blocs + méta de construction (numéros dans l'ordre, doublons, `continues`)."""
    b = _Builder(doc_id, title)
    toc_node: Node | None = None
    last_text_block: Block | None = None  # dernier bloc para|list de la page précédente
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
        groups = _segment_page(pt)
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
    toc_gaps = _apply_toc(b, toc)
    nodes = [b.nodes[nid] for nid in b.order]
    doc = Document(doc_id=doc_id, kind="contrat", title=title, edition=edition, lang="fr", nodes=nodes,
                   blocks=b.blocks, source_url=source_url, source_hash=source_hash,
                   ingest_fingerprint=ingest_fingerprint())
    printed_toc = _printed_toc_entries(pages)
    meta = {"numbers": b.numbers, "duplicates": b.duplicates, "continues": b.continues, "toc": toc,
            "toc_gaps": toc_gaps, "printed_toc": printed_toc}
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
    for item in toc:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        m = _TOC_NUMBER_RE.match(str(item[1]).strip())
        if not m:
            continue
        node = b.nodes.get(f"{b.doc_id}:a{m.group(1)}")
        if node is None:
            gaps.append(f"{m.group(1)} (p. {item[2]})")
        elif not node.title:
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
        doc, meta = build_document(pages, edition=resolved_edition, source_hash=source_hash, toc=toc, doc_id=doc_id,
                                   title=document_title, source_url=source_url)
        summary = build_summary(doc)
        report = build_pdf_report(doc, previous, pages=pages, numbers=meta["numbers"], duplicates=meta["duplicates"],
                                  continues=meta["continues"], toc=toc, toc_gaps=meta["toc_gaps"],
                                  printed_toc=meta["printed_toc"], summary=summary)
    except ValidationError as exc:
        report = report_from_validation_error(doc_id, exc)
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
