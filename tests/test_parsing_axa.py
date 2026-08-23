"""Pages clés du contrat AXA (9, 11, 34, 46) : extraits relus contre `document.json` committé ; overlay typé.

Sans réseau : le PDF réel n'est utilisé que s'il est présent (`data/axa-lu-optihome-2017/source.pdf`, non committé) —
l'ingestion doit alors regénérer les artefacts à l'identique.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
from server.app.domain import BlockRef, Document, Report
from server.ingest import pdf_to_blocks as p
from server.ingest.report import build_pdf_report

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "data" / "axa-lu-optihome-2017"
EXTRACTS = Path(__file__).parent / "data" / "axa"
DOC = "axa-lu-optihome-2017"


@pytest.fixture(scope="module")
def doc() -> Document:
    return Document.model_validate_json((REAL / "document.json").read_bytes())


def subtree_text(doc: Document, node_id: str, page: int) -> str:
    """Texte des blocs du nœud et de ses descendants sur une page, dans l'ordre de lecture."""
    by = {n.node_id: n for n in doc.nodes}
    out: list[str] = []

    def walk(nid: str) -> None:
        for item in by[nid].items:
            if isinstance(item, BlockRef):
                b = doc.block(item.block_id)
                if b.page == page:
                    out.append(b.text)
            else:
                walk(item.node_id)

    walk(node_id)
    return "\n".join(out)


@pytest.mark.parametrize("extract, node_id, page", [
    ("p09-1.12.txt", f"{DOC}:a1.12", 9),
    ("p11-1.28.txt", f"{DOC}:a1.28", 11),
    ("p34-3.1.1.1.6.txt", f"{DOC}:a3.1.1.1.6", 34),
])
def test_extracts_match_node_text(doc: Document, extract: str, node_id: str, page: int) -> None:
    assert normalize((EXTRACTS / extract).read_text("utf-8")) == normalize(subtree_text(doc, node_id, page))


def test_p46_exclusion_is_first_block_of_page_under_a318(doc: Document) -> None:
    b = doc.block(f"{DOC}:p46:1")
    assert normalize((EXTRACTS / "p46-exclusion.txt").read_text("utf-8")) == normalize(b.text)
    parent = {bid: n.node_id for n in doc.nodes for bid in n.blocks}
    assert parent[b.block_id] == f"{DOC}:a3.1.8" and b.continues is None


def test_document_shape(doc: Document) -> None:
    assert doc.kind == "contrat" and doc.edition == "juin 2017" and doc.doc_id == DOC
    pages = {b.page for b in doc.blocks}
    assert len(pages) == 108 and min(pages) == 1 and max(pages) == 109 and 108 not in pages  # p. 108 blanche
    by = {n.node_id: n for n in doc.nodes}
    parent = {c: n.node_id for n in doc.nodes for c in n.children}
    assert parent[f"{DOC}:a3.1.1.1.6"] == f"{DOC}:a3.1.1.1"
    assert by[f"{DOC}:a1.12"].scope.kind == "commun" and by[f"{DOC}:a3.1.1"].scope.kind == "commun"
    assert by[f"{DOC}:a3.1.8.3"].scope.kind == "extension" and by[f"{DOC}:a4.1"].scope.kind == "special"
    assert by[DOC].children == [f"{DOC}:tdm", *(f"{DOC}:a{i}" for i in (1, 2, 3, 4))]
    assert all(b.loc == f"p{b.page}" and b.lines and b.bbox for b in doc.blocks)
    assert all("\x07" not in b.text and "Wingdings" not in b.text for b in doc.blocks)
    assert {b.kind for b in doc.blocks} <= {"para", "heading", "list", "autre"}  # le typage vient de l'overlay
    assert sum(1 for b in doc.blocks if b.continues) >= 1
    report = Report.model_validate_json((REAL / "report.json").read_bytes())
    assert not report.blocking and report.stats["pages"] == 109 and report.stats["tdm_pdf_entrees"] == 0
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text("utf-8"))[DOC]
    assert manifest["status"] == "servi" and manifest["gate"] is None
    assert manifest["ingest_fingerprint"] == doc.ingest_fingerprint == p.ingest_fingerprint()
    assert manifest["document_hash"] == hashlib.sha256((REAL / "document.json").read_bytes()).hexdigest()
    assert manifest["source_hash"] == (REAL / "source.sha256").read_text("utf-8").strip()


def test_overlay_confirms_four_blocks() -> None:
    c = load_corpus(ROOT / "data", allow_ungated=True)
    assert c.quarantine == {} and set(c.documents) == {DOC, "lux-guide"}
    d = c.documents[DOC]
    confirmed = {b.block_id: b for b in d.blocks if b.kind_confirmed}
    assert set(confirmed) == {f"{DOC}:p9:2", f"{DOC}:p11:12", f"{DOC}:p34:12", f"{DOC}:p46:1"}
    assert (confirmed[f"{DOC}:p9:2"].kind, confirmed[f"{DOC}:p9:2"].defines) == ("definition", "contenu")
    assert confirmed[f"{DOC}:p34:12"].kind == "garantie"
    assert (confirmed[f"{DOC}:p46:1"].kind, confirmed[f"{DOC}:p46:1"].scope_node_id) == ("exclusion", f"{DOC}:a3.1.8")
    raw = json.loads((REAL / "document.json").read_text("utf-8"))
    assert all("kind" not in b or b["kind"] in ("heading", "list", "autre") for b in raw["blocks"])  # document.json intact


@pytest.mark.skipif(not (REAL / "source.pdf").is_file(), reason="source.pdf absent (non committé)")
def test_real_pdf_regenerates_committed_artefacts(doc: Document) -> None:
    pdf = REAL / "source.pdf"
    source_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert source_hash == (REAL / "source.sha256").read_text("utf-8").strip()
    pages, toc = p.extract_pages(pdf)
    built, meta = p.build_document(pages, edition=p.DEFAULT_EDITION, source_hash=source_hash, toc=toc)
    assert p.document_json(built) == (REAL / "document.json").read_text("utf-8")
    assert p.build_summary(built) == (REAL / "summary.md").read_text("utf-8")
    report = build_pdf_report(built, doc, pages=pages, numbers=meta["numbers"], duplicates=meta["duplicates"],
                              continues=meta["continues"], toc=toc, summary=p.build_summary(built))
    assert report == Report.model_validate_json((REAL / "report.json").read_bytes())
