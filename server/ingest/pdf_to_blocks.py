"""PDF → arbre de blocs d'un contrat (AD-2), artefacts `data/{doc_id}/*` et `data/manifest.json` (AD-7).

    uv run python -m server.ingest.pdf_to_blocks axa-lu-optihome-2017 [--data data] [--edition "juin 2017"]

Extraction : `page.get_text("dict", sort=True)` ; une `Line` par ligne visuelle (un numéro d'article et le texte
qui le suit sur la même ligne de base sont fusionnés) ; en-têtes/pieds répétés retirés (bande haute/basse, texte
récurrent ou numéro de page). Segmentation : un numéro d'article ouvre un nœud `{doc_id}:a{numero}` (parent = préfixe
le plus long existant, sinon la racine) ; une ligne à puce ouvre un item de `list` ; un écart vertical supérieur à
`para_gap_ratio` hauteurs de ligne ouvre un `para` ; un paragraphe qui continue sur la page suivante est scindé,
le second bloc porte `continues`. Les pages dont l'en-tête annonce la table des matières donnent un bloc `autre`
par page sous le nœud `{doc_id}:tdm` (la TdM imprimée n'est pas interprétée — story 3.1).
`text` est brut : seules altérations, déclarées dans `FLAGS`, les puces Wingdings deviennent `•`, le glyphe de
tabulation `\\x07` est retiré, les espaces/tabulations de fin de ligne sont retirés.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
from pydantic import ValidationError

from server.app.config import get_settings
from server.app.corpus.text import normalize_version
from server.app.domain import Block, BlockRef, Check, Document, Line, ManifestEntry, Node, NodeRef, Report
from server.ingest.artifacts import (SCHEMA_VERSION, document_json, load_previous, merge_manifest, read_manifest,
                                     write_atomic)
from server.ingest.report import build_pdf_report, report_from_validation_error

DOC_ID = "axa-lu-optihome-2017"
TITLE = "Conditions d'assurances OptiHome (multirisques habitation)"
DEFAULT_EDITION = "juin 2017"

# Entrent dans `ingest_fingerprint` : toute modification change les IDs attendus (AD-2, stabilité).
PARSER_VERSION = "1"
SEGMENTATION_RULES = ("numero:^\\d+(\\.\\d+)*$@x0<article_number_max_x=>noeud a{numero}(parent=prefixe);"
                      "titre:meme_ligne_de_base(size>=12|sans_ponct_finale&suite_majuscule)=>heading;"
                      "puce:•=>list(item;continuation=indent>4pt);gap>para_gap_ratio*h=>para;"
                      "entete:bande&(recurrent|numero_page|MAJUSCULES<=10pt);tdm:entete~TABLE DES MATIERES=>autre/page;"
                      "numero_para+ligne_minuscule=>meme_para;continues:page_suivante&minuscule&prec!~[.;:]$")
FLAGS = {"sort": True, "wingdings_bullet": "•", "drop_tab_glyph": True, "rstrip_lines": True,
         "ligatures": "decomposees (TEXT_PRESERVE_LIGATURES absent)", "dehyphenate": False}

_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)\s*$")
_TDM_RE = re.compile(r"TABLE\s+DES\s+MATI[EÈ]RES", re.IGNORECASE)
_PAGE_NUMBER_RE = re.compile(r"^\d{1,3}$")
_HEADER_MAX_SIZE = 10.0  # en-tête courant : petites capitales dans la bande haute (« … - OPTIHOME »)
_TITLE_SIZE = 12.0  # FranklinGothic-Demi 12–13 pt = titres d'articles ; en deçà, heuristique de ponctuation
_BULLET_FONTS = ("Wingdings",)


def ingest_fingerprint() -> str:
    s = get_settings()
    payload = json.dumps({"parser": PARSER_VERSION, "schema": SCHEMA_VERSION, "segmentation": SEGMENTATION_RULES,
                          "flags": FLAGS, "normalize_version": normalize_version,
                          "thresholds": {"header_band_pt": s.header_band_pt, "footer_band_pt": s.footer_band_pt,
                                         "header_min_pages_ratio": s.header_min_pages_ratio,
                                         "para_gap_ratio": s.para_gap_ratio,
                                         "article_number_max_x": s.article_number_max_x}},
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
    is_toc: bool = False


def _round(b: Any) -> list[float]:
    return [round(float(v), 2) for v in b]


def _raw_lines(page: pymupdf.Page) -> tuple[list[PageLine], int]:
    """Lignes brutes (texte nettoyé, bbox, taille dominante, puce) et nombre d'images."""
    out: list[PageLine] = []
    images = 0
    for b in page.get_text("dict", sort=FLAGS["sort"])["blocks"]:
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
            text = text.rstrip() if FLAGS["rstrip_lines"] else text
            text = text.lstrip(" \t") if not bullet else text
            if not text.strip():
                continue
            size = max(sizes, key=lambda k: sizes[k]) if sizes else 0.0
            out.append(PageLine(text=text, bbox=_round(line["bbox"]), size=size, bullet=bullet))
    return out, images


def _merge_number_lines(lines: list[PageLine], max_x: float) -> list[PageLine]:
    """« 1.12 » seul sur sa ligne + « Contenu » sur la même ligne de base ⇒ une seule `Line` « 1.12 Contenu »."""
    out: list[PageLine] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        m = _NUMBER_RE.match(cur.text)
        if m and cur.bbox[0] < max_x:
            cur.number = m.group(1)
            cur.text = m.group(1)
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            if nxt and not nxt.bullet and abs(nxt.bbox[1] - cur.bbox[1]) <= 3.0 and nxt.bbox[0] >= cur.bbox[2] - 1:
                cur.text = f"{cur.text} {nxt.text}"
                cur.bbox = [min(cur.bbox[0], nxt.bbox[0]), min(cur.bbox[1], nxt.bbox[1]),
                            max(cur.bbox[2], nxt.bbox[2]), max(cur.bbox[3], nxt.bbox[3])]
                cur.size = nxt.size
                i += 1
        out.append(cur)
        i += 1
    return out


def extract_pages(pdf: Path | str) -> tuple[list[PageText], list[Any]]:
    """Pages (lignes + bbox + police, en-têtes/pieds retirés) et `get_toc()` du PDF."""
    s = get_settings()
    doc = pymupdf.open(str(pdf))
    toc = doc.get_toc()
    pages: list[PageText] = []
    band_texts: dict[str, int] = {}
    for pno, page in enumerate(doc, start=1):
        lines, images = _raw_lines(page)
        pt = PageText(page=pno, width=page.rect.width, height=page.rect.height, lines=lines, images=images)
        for line in lines:
            if line.bbox[1] < s.header_band_pt or line.bbox[3] > pt.height - s.footer_band_pt:
                band_texts[line.text] = band_texts.get(line.text, 0) + 1
        pages.append(pt)
    min_pages = max(2, int(len(pages) * s.header_min_pages_ratio))
    for pt in pages:
        kept: list[PageLine] = []
        for line in pt.lines:
            in_band = line.bbox[1] < s.header_band_pt or line.bbox[3] > pt.height - s.footer_band_pt
            running = band_texts.get(line.text, 0) >= min_pages or _PAGE_NUMBER_RE.match(line.text) is not None \
                or (line.size <= _HEADER_MAX_SIZE and line.text == line.text.upper())
            if in_band and running:
                pt.removed.append(line.text)
                if _TDM_RE.search(line.text):
                    pt.is_toc = True
                continue
            kept.append(line)
        pt.lines = _merge_number_lines(kept, s.article_number_max_x)
    doc.close()
    return pages, toc


def scope_kind(numero: str) -> str:
    """`1`, `2`, `3.1` et descendants ⇒ commun ; `3.1.8` et descendants ⇒ extension ; le reste ⇒ special."""
    parts = numero.split(".")
    if parts[:3] == ["3", "1", "8"]:
        return "extension"
    if parts[0] in ("1", "2") or parts[:2] == ["3", "1"]:
        return "commun"
    return "special"


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
        node = Node(node_id=node_id, level=len(parts), title="")
        node.scope.kind = scope_kind(numero)  # type: ignore[assignment]
        self.nodes[node_id] = node
        self.order.append(node_id)
        parent.items.append(NodeRef(node_id=node_id))
        self.numbers.append(numero)
        return node

    def add_block(self, page: int, lines: list[PageLine], kind: str, *, continues: str | None = None) -> Block:
        loc = f"p{page}"
        seq = sum(1 for b in self.blocks if b.loc == loc) + 1
        block_id = f"{self.doc_id}:{loc}:{seq}"
        bbox = [min(l.bbox[0] for l in lines), min(l.bbox[1] for l in lines),
                max(l.bbox[2] for l in lines), max(l.bbox[3] for l in lines)]
        block = Block(block_id=block_id, text="\n".join(l.text for l in lines), loc=loc, seq=seq, page=page,
                      bbox=bbox, kind=kind, continues=continues,  # type: ignore[arg-type]
                      lines=[Line(line_id=f"{block_id}:l{i}", text=l.text, bbox=l.bbox) for i, l in enumerate(lines, 1)])
        self.blocks.append(block)
        self.current.items.append(BlockRef(block_id=block_id))
        if continues:
            self.continues += 1
        return block


def _is_heading(line: PageLine, nxt: PageLine | None) -> bool:
    """Texte sur la ligne de base d'un numéro : titre si gros, ou court sans ponctuation finale et suivi d'une majuscule."""
    if line.size >= _TITLE_SIZE:
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
            if kind == "list" and close and line.bbox[0] > prev_lines[0].bbox[0] + 4:
                prev_lines.append(line)  # continuation indentée d'un item
                continue
            if kind == "para" and close and line.size < _TITLE_SIZE and prev.size < _TITLE_SIZE:
                prev_lines.append(line)
                continue
            if kind == "para" and prev.number is not None and len(prev_lines) == 1 and line.text[:1].islower():
                prev_lines.append(line)  # « 3.1.1.1.1 L'incendie, » puis « c'est-à-dire … » : même alinéa
                continue
        kind = "heading" if line.size >= _TITLE_SIZE and line.number is None and not line.text.endswith(".") else "para"
        groups.append((kind, [line]))
    return groups


def build_document(pages: list[PageText], *, edition: str, source_hash: str, toc: list[Any],
                   doc_id: str = DOC_ID, title: str = TITLE) -> tuple[Document, dict[str, Any]]:
    """Arbre de blocs + méta de construction (numéros dans l'ordre, doublons, `continues`)."""
    b = _Builder(doc_id, title)
    toc_node: Node | None = None
    last_text_block: Block | None = None  # dernier bloc para|list de la page précédente
    for pt in pages:
        if pt.is_toc:
            if toc_node is None:
                toc_node = Node(node_id=f"{doc_id}:tdm", level=1, title="Table des matières")
                b.nodes[toc_node.node_id] = toc_node
                b.order.append(toc_node.node_id)
                b.root.items.append(NodeRef(node_id=toc_node.node_id))
            if pt.lines:
                saved, b.current = b.current, toc_node
                b.add_block(pt.page, pt.lines, "autre")
                b.current = saved
            last_text_block = None
            continue
        first = True
        page_last: Block | None = None
        for kind, lines in _segment_page(pt):
            continues = None
            if first and kind in ("para", "list") and lines[0].number is None and last_text_block is not None \
                    and lines[0].text[:1].islower() and not last_text_block.text.rstrip().endswith((".", ";", ":")):
                continues = last_text_block.block_id
            first = False
            if lines[0].number is not None:
                b.current = b.node_for(lines[0].number)
                if kind == "heading":
                    b.current.title = lines[0].text
            blk = b.add_block(pt.page, lines, kind, continues=continues)
            if kind in ("para", "list"):
                page_last = blk
            if lines[0].number is not None and kind == "para":
                b.current.title = lines[0].text[:80]
        last_text_block = page_last
    nodes = [b.nodes[nid] for nid in b.order]
    doc = Document(doc_id=doc_id, kind="contrat", title=title, edition=edition, lang="fr", nodes=nodes,
                   blocks=b.blocks, source_url=None, source_hash=source_hash, ingest_fingerprint=ingest_fingerprint())
    meta = {"numbers": b.numbers, "duplicates": b.duplicates, "continues": b.continues, "toc": toc}
    return doc, meta


def build_summary(doc: Document) -> str:
    """Sommaire compact : nœuds de niveau ≤ 3 avec leur titre et leur première page."""
    lines = [f"<!-- {doc.doc_id} · edition {doc.edition} · source_hash {doc.source_hash} · "
             f"ingest_fingerprint {doc.ingest_fingerprint} -->", f"# {doc.title}", ""]
    by_id = {n.node_id: n for n in doc.nodes}
    first_page: dict[str, int | None] = {}

    def page_of(node: Node) -> int | None:
        if node.node_id in first_page:
            return first_page[node.node_id]
        p: int | None = None
        for item in node.items:
            q = doc.block(item.block_id).page if isinstance(item, BlockRef) else page_of(by_id[item.node_id])
            if q is not None and (p is None or q < p):
                p = q
        first_page[node.node_id] = p
        return p

    def walk(node: Node) -> None:
        for child_id in node.children:
            child = by_id[child_id]
            if 1 <= child.level <= 3:
                pg = page_of(child)
                lines.append(f"{'  ' * (child.level - 1)}- `{child.node_id}` · {child.title} · p. {pg}")
            walk(child)

    walk(by_id[doc.doc_id])
    return "\n".join(lines).rstrip() + "\n"


def run(data_dir: Path, *, edition: str, doc_id: str = DOC_ID) -> tuple[Report, ManifestEntry]:
    """Ingère `data_dir/source.pdf` ; toute erreur de source devient un check bloquant, jamais une trace Python."""
    manifest_path = data_dir.parent / "manifest.json"
    fingerprint = ingest_fingerprint()
    try:
        raw_manifest = read_manifest(manifest_path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        detail = f"{manifest_path} illisible, rien n'a été écrit : {type(exc).__name__}: {exc}"[:2000]
        report = Report(doc_id=doc_id, checks=[Check(name="manifest_illisible", level="bloquant", detail=detail)])
        return report, ManifestEntry(status="quarantaine", source_hash="", ingest_fingerprint=fingerprint,
                                     document_hash="", edition=edition, gate=None)
    source_hash = ""
    doc: Document | None = None
    summary = ""
    previous = load_previous(data_dir / "document.json")
    try:
        pdf_path = data_dir / "source.pdf"
        source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        expected_path = data_dir / "source.sha256"
        if expected_path.is_file() and expected_path.read_text("utf-8").split()[:1] != [source_hash]:
            raise ValueError(f"source.pdf : sha256 {source_hash} ≠ source.sha256 — relancer fetch_source")
        pages, toc = extract_pages(pdf_path)
        doc, meta = build_document(pages, edition=edition, source_hash=source_hash, toc=toc, doc_id=doc_id)
        summary = build_summary(doc)
        report = build_pdf_report(doc, previous, pages=pages, numbers=meta["numbers"], duplicates=meta["duplicates"],
                                  continues=meta["continues"], toc=toc, summary=summary)
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
                                         document_hash=document_hash, edition=edition, gate=None))
    return report, entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("doc_id", nargs="?", default=DOC_ID)
    parser.add_argument("--data", default="data", type=Path)
    parser.add_argument("--edition", default=DEFAULT_EDITION)
    args = parser.parse_args(argv)
    report, entry = run(args.data / args.doc_id, edition=args.edition, doc_id=args.doc_id)
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
