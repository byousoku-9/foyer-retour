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
    assert doc.source_url == (REAL / "source.url").read_text("utf-8").strip()
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
    assert {b.kind for b in doc.blocks} <= {"para", "heading", "list", "table", "autre"}  # typage 3.1 structurel
    assert sum(b.kind == "table" for b in doc.blocks) == 7
    # scissions de page (AD-2) : p53:8 → p54:1 commence par une majuscule (revue Codex 1.2, B6)
    linked = {b.block_id: b.continues for b in doc.blocks if b.continues}
    assert linked[f"{DOC}:p54:1"] == f"{DOC}:p53:8"
    # Une table est atomique **par page** : la grille de résiliation est scindée en deux blocs,
    # jamais reliée comme un paragraphe continu.
    assert doc.block(f"{DOC}:p30:4").kind == "table" and doc.block(f"{DOC}:p31:1").kind == "table"
    assert linked[f"{DOC}:p82:1"] == f"{DOC}:p81:16" and doc.block(f"{DOC}:p82:1").kind == "list"
    assert doc.block(f"{DOC}:p46:1").continues is None  # l'alinéa suit « … particulières. »
    # continuations de liste alignées (revue Codex 1.2, I5) : p54 « … et » puis « immeubles … » = même item
    p54 = doc.block(f"{DOC}:p54:5")
    assert p54.kind == "list" and p54.lines[2].text.startswith("immeubles qu") and p54.lines[4].text.startswith("de vol")
    report = Report.model_validate_json((REAL / "report.json").read_bytes())
    assert not report.blocking and report.stats["pages"] == 109 and report.stats["tdm_pdf_entrees"] == 0
    assert [c.name for c in report.alerts] == ["blocs_non_citables", "pages_mixtes"]
    printed_toc = next(c for c in report.checks if c.name == "tdm_imprimee")
    assert printed_toc.level == "info" and "4 titre(s)" in printed_toc.detail
    assert "4 bloc(s) sur 4 page(s)" in report.alerts[0].detail
    assert report.alerts[1].detail.endswith(": 1")
    assert report.stats["tables"] == 7 and report.stats["couverture"] == 1.0
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text("utf-8"))[DOC]
    # Story 1.10 : le gate `vertical` est désormais écrit — par `evals run --gate`, jamais par
    # l'ingestion (AD-7). C'est ce que ce test contrôle ici : le gate existe, il porte les empreintes
    # de **cette** entrée, et il n'a pas été fabriqué par le pipeline d'ingestion.
    gate = manifest["gate"]
    assert manifest["status"] == "servi" and gate is not None
    assert gate["profile"] == "vertical" and gate["evals_ok"] is True and gate["cases"] >= 1
    assert (gate["source_hash"], gate["ingest_fingerprint"], gate["overlay_hash"]) == (
        manifest["source_hash"], manifest["ingest_fingerprint"], manifest["overlay_hash"])
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
    assert (confirmed[f"{DOC}:p11:12"].kind, confirmed[f"{DOC}:p11:12"].defines) == ("definition", "mobilier de jardin")
    assert (confirmed[f"{DOC}:p34:12"].kind, confirmed[f"{DOC}:p34:12"].scope_node_id) == ("garantie", f"{DOC}:a3.1.1")
    assert all(b.kind_source == "manual" for b in confirmed.values())
    # portée exacte de l'exclusion p. 46 : les extensions 3.1.8.3 à 3.1.8.6, et elles seules (revue Codex 1.2, B2)
    ex = confirmed[f"{DOC}:p46:1"]
    assert (ex.kind, ex.scope_node_id) == ("exclusion", f"{DOC}:a3.1.8")
    assert ex.scope_node_ids == [f"{DOC}:a3.1.8.{i}" for i in (3, 4, 5, 6)]
    covered = d.scope_nodes(ex.block_id)
    assert covered == {f"{DOC}:a3.1.8.{i}" for i in (3, 4, 5, 6)}
    assert not covered & {f"{DOC}:a3.1.8", *(f"{DOC}:a3.1.8.{i}" for i in (1, 2, 7, 8))}
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text("utf-8"))[DOC]
    assert manifest["overlay_hash"] == hashlib.sha256((REAL / "typing.manual.json").read_bytes()).hexdigest()
    raw = json.loads((REAL / "document.json").read_text("utf-8"))
    assert all("kind" not in b or b["kind"] in ("heading", "list", "table", "autre") for b in raw["blocks"])


def test_revue_3_1_records_the_measured_id_reassignment() -> None:
    """Revue 3.1 M1 : une réingestion idempotente ne doit pas effacer la preuve historique d'AD-2."""
    journal = (ROOT / "docs" / "tests-live.md").read_text("utf-8")
    assert "57 `block_id` de l’ancien artefact absents" in journal
    assert "31 identifiants conservés dont le texte a changé" in journal


@pytest.mark.skipif(not (REAL / "source.pdf").is_file(), reason="source.pdf absent (non committé)")
def test_real_pdf_regenerates_committed_artefacts(doc: Document) -> None:
    pdf = REAL / "source.pdf"
    source_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert source_hash == (REAL / "source.sha256").read_text("utf-8").strip()
    pages, toc = p.extract_pages(pdf)
    built, meta = p.build_document(pages, edition=p.DEFAULT_EDITION, source_hash=source_hash, toc=toc,
                                   source_url=(REAL / "source.url").read_text("utf-8").strip())
    assert p.document_json(built) == (REAL / "document.json").read_text("utf-8")
    assert p.build_summary(built) == (REAL / "summary.md").read_text("utf-8")
    report = build_pdf_report(built, doc, pages=pages, numbers=meta["numbers"], duplicates=meta["duplicates"],
                              continues=meta["continues"], toc=toc, toc_gaps=meta["toc_gaps"],
                              printed_toc=meta["printed_toc"], summary=p.build_summary(built))
    assert report == Report.model_validate_json((REAL / "report.json").read_bytes())
