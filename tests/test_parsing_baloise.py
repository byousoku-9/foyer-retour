"""Preuves hermétiques et parsing réel du second contrat Baloise Luxembourg HOME."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from server.app.domain import BlockRef, Document, NodeRef, Report
from server.ingest import pdf_to_blocks as p
from server.ingest.report import build_pdf_report

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "data" / "baloise-lu-home-2-2024"
DOC = "baloise-lu-home-2-2024"
TITLE = "Baloise Luxembourg HOME"
EDITION = "CG-HOME(2)-LUFR-09-24"
SOURCE_HASH = "2c365b0ea59a47ddf86295b0e1ad65a0c23847bcc30db22ec47861b18ba4a5a6"
TYPING_CHECKS = {
    "corruption_decisionnelle", "unresolved_refs", "definition_introuvable",
    "exclusion_sans_marqueur", "confiance_typage_faible", "kinds_non_confirmes",
    "typage_clauses", "typage_transport",
}


@pytest.fixture(scope="module")
def doc() -> Document:
    return Document.model_validate_json((REAL / "document.json").read_bytes())


def test_baloise_artifacts_publish_the_verified_identity_and_measured_gaps(doc: Document) -> None:
    report = Report.model_validate_json((REAL / "report.json").read_bytes())
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text("utf-8"))[DOC]
    assert (doc.doc_id, doc.title, doc.edition) == (DOC, TITLE, EDITION)
    assert doc.source_hash == SOURCE_HASH
    assert doc.source_url == (REAL / "source.url").read_text("utf-8").strip()
    assert manifest == {
        "status": "servi",
        "source_hash": SOURCE_HASH,
        "ingest_fingerprint": doc.ingest_fingerprint,
        "document_hash": hashlib.sha256((REAL / "document.json").read_bytes()).hexdigest(),
        "edition": EDITION,
        "overlay_hash": None,
        "gate": {
            "profile": "vertical",
            "source_hash": SOURCE_HASH,
            "ingest_fingerprint": doc.ingest_fingerprint,
            "cases_hash": "e493de7d5d41ac4e64537f54124bdb6cac2ad97f6356aaca908a1d67c860e2e8",
            "pipeline_digest": "23699bbe105930b1afc47c03e422a1983a7104e600e6c79839c89757941f3c9e",
            "prompts_digest": "4b8a3fce5e59e0a0978973b14bc78fa5c3891534493c61d70e632ee8fa3d1d45",
            "model_ids": {
                "ingest": "claude-opus-5",
                "reason": "claude-sonnet-5",
                "micro": "claude-haiku-4-5-20251001",
            },
            "evals_ok": True,
            "date": "2026-08-27T05:55:02Z",
            "overlay_hash": None,
            "cases": 3,
            "countersigned": False,
        },
    }
    assert not report.blocking
    assert report.stats["pages"] == report.stats["pages_avec_blocs"] == 48
    assert report.stats["blocs"] == len(doc.blocks) == 692
    assert report.stats["noeuds"] == len(doc.nodes) == 2
    assert report.stats["numeros_articles"] == 0
    assert report.stats["tables"] == 11
    assert report.stats["pages_ocr"] == report.stats["pages_non_francaises"] == 0
    assert report.stats["pages_charabia"] == {}
    assert [check.name for check in report.alerts] == [
        "blocs_non_citables", "tdm_imprimee", "definition_introuvable",
        "exclusion_sans_marqueur", "confiance_typage_faible", "kinds_non_confirmes",
    ]
    assert "numéros annoncés absents de l'arbre" in next(
        check.detail for check in report.checks if check.name == "tdm_imprimee"
    )


def test_lempreinte_committee_est_a_jour_ou_declaree_perimee(doc: Document) -> None:
    """Garde toujours exécutée, que ce document n'avait jamais eue (voir le helper partagé).

    Le test de régénération qui porte l'égalité est `skipif` sans le PDF réel : sans cette garde,
    rien ici ne comparait jamais l'empreinte committée à celle du parseur courant.
    """
    from tests.test_pdf_to_blocks import assert_empreinte_committee_declaree

    assert_empreinte_committee_declaree(DOC, doc.ingest_fingerprint)


def test_baloise_typing_used_only_standard_messages_under_the_ceiling(doc: Document) -> None:
    report = Report.model_validate_json((REAL / "report.json").read_bytes())
    assert report.stats["typage_transport"] == "standard"
    assert report.stats["typage_standard_requests"] == 120
    assert report.stats["typage_batch_cost_eur"] == 0.0
    assert report.stats["typage_standard_cost_eur"] == 4.2518
    assert report.stats["typage_total_cost_eur"] <= report.stats["typage_cost_ceiling_eur"] == 10.0
    assert report.stats["blocs_types_modele"] == 513
    assert report.stats["blocs_juridiques"] == 510
    assert report.stats["blocs_juridiques_confirmes"] == 453
    transport = next(check for check in report.checks if check.name == "typage_transport")
    assert transport.level == "info" and "aucune API Batch" in transport.detail
    assert sum(block.kind_source in {"model", "model_verified"} for block in doc.blocks) == 513


def test_baloise_has_citable_contract_passages_for_the_three_witnesses(doc: Document) -> None:
    corpus = "\n".join(block.text.casefold() for block in doc.blocks)
    assert "brûlure de\ncigarette" in corpus
    assert "arrêt accidentel du congélateur" in corpus
    assert "responsabilité civile vie privée" in corpus
    assert all(block.lines and block.bbox and block.loc == f"p{block.page}" for block in doc.blocks)
    nested = [item for node in doc.nodes for item in node.items if isinstance(item, NodeRef)]
    assert nested == [NodeRef(node_id=f"{DOC}:tdm")]
    assert all(
        isinstance(item, (BlockRef, NodeRef)) for node in doc.nodes for item in node.items
    )


@pytest.mark.skipif(
    not (REAL / "source.pdf").is_file() and os.environ.get("REAL_PDF_TESTS_REQUIRED") != "1",
    reason="source.pdf absent (non committé)",
)
def test_real_baloise_pdf_regenerates_the_committed_structural_identity(doc: Document) -> None:
    pdf = REAL / "source.pdf"
    assert pdf.is_file(), "source.pdf Baloise requis par la porte de déploiement"
    source_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert source_hash == SOURCE_HASH == (REAL / "source.sha256").read_text("utf-8").strip()
    pages, toc = p.extract_pages(pdf)
    built, meta = p.build_document(
        pages, edition=EDITION, source_hash=source_hash, toc=toc, doc_id=DOC, title=TITLE,
        source_url=(REAL / "source.url").read_text("utf-8").strip(),
    )
    identity = lambda block: (  # noqa: E731
        block.block_id, block.text, block.loc, block.seq, block.page, block.bbox, block.continues,
        [line.model_dump() for line in block.lines],
    )
    assert [identity(block) for block in built.blocks] == [identity(block) for block in doc.blocks]
    node_identity = lambda node: (node.node_id, node.level, node.title, node.items, node.sources)  # noqa: E731
    assert [node_identity(node) for node in built.nodes] == [node_identity(node) for node in doc.nodes]
    assert p.build_summary(built) == (REAL / "summary.md").read_text("utf-8")
    report = build_pdf_report(
        built, doc, pages=pages, numbers=meta["numbers"], duplicates=meta["duplicates"],
        continues=meta["continues"], toc=toc, toc_gaps=meta["toc_gaps"],
        printed_toc=meta["printed_toc"], summary=p.build_summary(built),
    )
    committed = Report.model_validate_json((REAL / "report.json").read_bytes())
    assert report.checks == [check for check in committed.checks if check.name not in TYPING_CHECKS]
    assert all(committed.stats[key] == value for key, value in report.stats.items())
