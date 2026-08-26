"""Pages clés du contrat AXA (9, 11, 34, 46) : extraits relus et typage automatique committé.

Sans réseau : le PDF réel n'est utilisé que s'il est présent (`data/axa-lu-optihome-2017/source.pdf`, non committé) —
l'ingestion PDF doit alors regénérer leur structure et leur contenu à l'identique ; le typage
Batch, testé sans réseau ailleurs, est la seconde étape qui enrichit cet artefact.
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
TYPING_CHECKS = {
    "corruption_decisionnelle", "unresolved_refs", "definition_introuvable",
    "exclusion_sans_marqueur", "confiance_typage_faible", "kinds_non_confirmes", "typage_clauses",
}


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
    # Les portées 3.2 viennent uniquement des relations résolues, jamais des quatre règles AXA
    # historiques de `pdf_to_blocks.scope_kind()`.
    assert by[f"{DOC}:a3.1.8.3"].scope.kind == "commun" and by[f"{DOC}:a4.1"].scope.kind == "commun"
    assert by[f"{DOC}:a2.17.3"].scope.kind == "special"
    assert by[DOC].children == [f"{DOC}:tdm", *(f"{DOC}:a{i}" for i in (1, 2, 3, 4))]
    assert all(b.loc == f"p{b.page}" and b.lines and b.bbox for b in doc.blocks)
    assert all("\x07" not in b.text and "Wingdings" not in b.text for b in doc.blocks)
    assert {b.kind for b in doc.blocks} <= {
        "para", "heading", "table", "definition", "garantie", "exclusion", "condition",
        "franchise", "renvoi", "autre",
    }
    assert all(b.structural_kind in {"para", "heading", "table", "list", "autre"} for b in doc.blocks)
    assert sum(b.kind_source == "model_verified" for b in doc.blocks) == 924
    # scissions de page (AD-2) : p53:8 → p54:1 commence par une majuscule (revue Codex 1.2, B6)
    linked = {b.block_id: b.continues for b in doc.blocks if b.continues}
    assert linked[f"{DOC}:p54:1"] == f"{DOC}:p53:8"
    # La grille de résiliation demeure scindée en deux blocs atomiques par page et sans lien ;
    # son `kind` est désormais sémantique (`condition`), pas le type de mise en page `table`.
    assert doc.block(f"{DOC}:p30:4").continues is None and doc.block(f"{DOC}:p31:1").continues is None
    assert linked[f"{DOC}:p82:1"] == f"{DOC}:p81:16"
    assert doc.block(f"{DOC}:p46:1").continues is None  # l'alinéa suit « … particulières. »
    # continuations de liste alignées (revue Codex 1.2, I5) : p54 « … et » puis « immeubles … » = même item
    p54 = doc.block(f"{DOC}:p54:5")
    assert p54.lines[2].text.startswith("immeubles qu") and p54.lines[4].text.startswith("de vol")
    report = Report.model_validate_json((REAL / "report.json").read_bytes())
    assert not report.blocking and report.stats["pages"] == 109 and report.stats["tdm_pdf_entrees"] == 0
    assert [c.name for c in report.alerts] == [
        "blocs_non_citables", "pages_mixtes", "unresolved_refs", "definition_introuvable",
        "exclusion_sans_marqueur", "confiance_typage_faible", "kinds_non_confirmes",
    ]
    printed_toc = next(c for c in report.checks if c.name == "tdm_imprimee")
    assert printed_toc.level == "info" and "4 titre(s)" in printed_toc.detail
    by_check = {check.name: check for check in report.checks}
    assert "4 bloc(s) sur 4 page(s)" in by_check["blocs_non_citables"].detail
    assert by_check["pages_mixtes"].detail.endswith(": 1")
    assert report.stats["tables"] == 7 and report.stats["couverture"] == 1.0
    assert report.stats["blocs_types_modele"] == 981
    assert report.stats["blocs_juridiques_confirmes"] == 924
    assert report.stats["references_non_resolues"] == 40
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text("utf-8"))[DOC]
    # Le document et l'overlay ont réellement changé : l'ingestion invalide le gate, que seul le
    # runner d'évals pourra réécrire après certification de l'artefact final.
    assert manifest["status"] == "servi" and manifest["gate"] is None
    assert manifest["overlay_hash"] is None
    assert manifest["ingest_fingerprint"] == doc.ingest_fingerprint == p.ingest_fingerprint()
    assert manifest["document_hash"] == hashlib.sha256((REAL / "document.json").read_bytes()).hexdigest()
    assert manifest["source_hash"] == (REAL / "source.sha256").read_text("utf-8").strip()


def test_typage_automatique_confirme_les_quatre_goldens_sans_overlay() -> None:
    c = load_corpus(ROOT / "data", allow_ungated=True)
    assert c.quarantine == {} and set(c.documents) == {DOC, "lux-guide"}
    d = c.documents[DOC]
    confirmed = {b.block_id: b for b in d.blocks if b.kind_confirmed}
    golden_ids = {f"{DOC}:p9:2", f"{DOC}:p11:12", f"{DOC}:p34:12", f"{DOC}:p46:1"}
    assert len(confirmed) == 924 and golden_ids <= set(confirmed)
    assert confirmed[f"{DOC}:p9:2"].kind == "definition"
    assert normalize(confirmed[f"{DOC}:p9:2"].defines or "") == "contenu"
    assert confirmed[f"{DOC}:p11:12"].kind == "definition"
    assert normalize(confirmed[f"{DOC}:p11:12"].defines or "") == "mobilier de jardin"
    assert (confirmed[f"{DOC}:p34:12"].kind, confirmed[f"{DOC}:p34:12"].scope_node_id) == \
        ("garantie", f"{DOC}:a3.1.1.1.6")
    assert all(confirmed[block_id].kind_source == "model_verified" for block_id in golden_ids)
    # portée exacte de l'exclusion p. 46 : les extensions 3.1.8.3 à 3.1.8.6, et elles seules (revue Codex 1.2, B2)
    ex = confirmed[f"{DOC}:p46:1"]
    assert (ex.kind, ex.scope_node_id) == ("exclusion", f"{DOC}:a3.1.8")
    assert ex.scope_node_ids == [f"{DOC}:a3.1.8.{i}" for i in (3, 4, 5, 6)]
    covered = d.scope_nodes(ex.block_id)
    assert covered == {f"{DOC}:a3.1.8.{i}" for i in (3, 4, 5, 6)}
    assert not covered & {f"{DOC}:a3.1.8", *(f"{DOC}:a3.1.8.{i}" for i in (1, 2, 7, 8))}
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text("utf-8"))[DOC]
    assert manifest["overlay_hash"] is None and not (REAL / "typing.manual.json").exists()
    raw = json.loads((REAL / "document.json").read_text("utf-8"))
    raw_by_id = {block["block_id"]: block for block in raw["blocks"]}
    assert all(raw_by_id[block_id]["kind_source"] == "model_verified" for block_id in golden_ids)


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
    # `pdf_to_blocks` reconstruit exactement l'identité immuable. Les champs juridiques et les
    # scopes sont ajoutés ensuite par les deux lots Opus et ne doivent donc pas être comparés ici.
    identity = lambda block: (  # noqa: E731 - projection locale lisible dans les deux assertions
        block.block_id, block.text, block.loc, block.seq, block.page, block.bbox, block.continues,
        [line.model_dump() for line in block.lines],
    )
    assert [identity(block) for block in built.blocks] == [identity(block) for block in doc.blocks]
    node_identity = lambda node: (  # noqa: E731
        node.node_id, node.level, node.title, node.items, node.sources,
    )
    assert [node_identity(node) for node in built.nodes] == [node_identity(node) for node in doc.nodes]
    assert p.build_summary(built) == (REAL / "summary.md").read_text("utf-8")
    report = build_pdf_report(built, doc, pages=pages, numbers=meta["numbers"], duplicates=meta["duplicates"],
                              continues=meta["continues"], toc=toc, toc_gaps=meta["toc_gaps"],
                              printed_toc=meta["printed_toc"], summary=p.build_summary(built))
    committed = Report.model_validate_json((REAL / "report.json").read_bytes())
    assert report.checks == [check for check in committed.checks if check.name not in TYPING_CHECKS]
    assert all(committed.stats[key] == value for key, value in report.stats.items())
