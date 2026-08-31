"""Porte structurelle PDF : invariants automatiques et revue visuelle liée au rendu."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf

from server.app.domain import Check, Report
from server.ingest import pdf_structure_gate as gate
from server.ingest.artifacts import document_json
from server.ingest.pdf_to_blocks import build_document, extract_pages


def _corpus(tmp_path: Path, *, columns: bool = True) -> Path:
    doc_dir = tmp_path / "data" / "contrat-synthetique"
    doc_dir.mkdir(parents=True)
    pdf_path = doc_dir / "source.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_text((56, 30), "CONDITIONS", fontsize=10)
    for index in range(6):
        y = 100 + index * 90
        page.insert_text(
            (56, y), f"G{index} texte utile assez long dans la colonne gauche.", fontsize=9
        )
        if columns:
            page.insert_text(
                (330, y), f"D{index} texte utile assez long dans la colonne droite.", fontsize=9
            )
    pdf.save(pdf_path)
    pdf.close()
    source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    (doc_dir / "source.sha256").write_text(source_hash + "\n", "utf-8")
    (doc_dir / "source.url").write_text("https://example.test/contrat.pdf\n", "utf-8")
    pages, toc = extract_pages(pdf_path)
    document, _ = build_document(
        pages,
        edition="2026",
        source_hash=source_hash,
        toc=toc,
        doc_id=doc_dir.name,
        title="Contrat synthétique",
        source_url="https://example.test/contrat.pdf",
    )
    (doc_dir / "document.json").write_text(document_json(document), "utf-8")
    report = Report(
        doc_id=doc_dir.name,
        checks=[Check(name="typage_clauses", level="info", detail="preuve synthétique")],
    )
    (doc_dir / "report.json").write_text(
        json.dumps(report.model_dump(), ensure_ascii=False) + "\n", "utf-8"
    )
    return doc_dir


def _review(
        doc_dir: Path,
        path: Path,
        *,
        verdict: str = "ok",
        rendered: str | None = None,
        mode: str = "audit_visuel",
) -> None:
    actual = gate._render_hashes(doc_dir / "source.pdf", [1], dpi=144)[1]
    page: dict[str, str | int] = {
        "page": 1,
        "mode": mode,
        "verdict": verdict,
        "note": "deux colonnes visuellement séparées",
    }
    if mode == "audit_visuel":
        page["render_sha256"] = rendered or actual
    path.write_text(json.dumps({
        "schema_version": "2",
        "reader": "lecteur indépendant synthétique",
        "dpi": 144,
        "pages": [page],
    }), "utf-8")


def _historique_different(doc_dir: Path) -> None:
    """Simule un ancien parseur sans toucher aux octets source ni à l'arbre du corpus."""
    raw = json.loads((doc_dir / "document.json").read_text("utf-8"))
    raw["blocks"][0]["lines"][0]["text"] = "texte historique différent"
    (doc_dir / "document.json").write_text(json.dumps(raw, ensure_ascii=False), "utf-8")


def test_la_porte_lie_chaque_page_changee_aux_invariants_et_au_rendu(tmp_path: Path) -> None:
    doc_dir = _corpus(tmp_path)
    _historique_different(doc_dir)
    review = tmp_path / "review.json"
    _review(doc_dir, review)

    report = gate.audit(doc_dir, review)

    assert report["status"] == "vert"
    assert report["changed_pages"] == [1]
    assert report["summary"]["pages_audited"] == 1
    assert report["summary"]["pages_exempted_mechanically"] == 0
    assert report["summary"]["pages_accounted"] == 1
    assert report["global_checks"]["registre_sans_orphelin"]["ok"]
    assert report["global_checks"]["arbre_portees_dependances"]["ok"]
    assert report["pages"][0]["boundaries"]
    assert report["pages"][0]["status"] == "vert"
    assert all(check["ok"] for check in report["pages"][0]["checks"].values())


def test_un_rendu_different_ou_un_verdict_ambigu_garde_la_porte_rouge(tmp_path: Path) -> None:
    doc_dir = _corpus(tmp_path)
    _historique_different(doc_dir)
    wrong_hash = "0" * 64
    for verdict, rendered in (("ok", wrong_hash), ("ambigu", None)):
        review = tmp_path / f"review-{verdict}-{rendered is None}.json"
        _review(doc_dir, review, verdict=verdict, rendered=rendered)

        report = gate.audit(doc_dir, review)

        assert report["status"] == "rouge"
        assert not report["global_checks"]["revue_visuelle_complete"]["ok"]
        assert not report["pages"][0]["checks"]["revue_visuelle"]["ok"]


def test_une_page_identique_est_exoneree_seulement_par_preuve_mecanique(tmp_path: Path) -> None:
    doc_dir = _corpus(tmp_path, columns=False)
    review = tmp_path / "review.json"
    _review(doc_dir, review, mode="exoneree")

    report = gate.audit(doc_dir, review)

    assert report["status"] == "vert"
    assert report["summary"]["pages_audited"] == 0
    assert report["summary"]["pages_exempted_mechanically"] == 1
    assert report["summary"]["pages_accounted"] == 1
    assert report["pages"][0]["mechanical_identity"]["ok"]
    assert report["pages"][0]["checks"]["exoneration_mecanique"]["ok"]
    assert report["global_checks"]["denominateur_ferme"]["ok"]


def test_un_delta_ne_peut_pas_sauto_exonerer_mecaniquement(tmp_path: Path) -> None:
    doc_dir = _corpus(tmp_path)
    _historique_different(doc_dir)
    review = tmp_path / "review.json"
    _review(doc_dir, review, mode="exoneree")

    report = gate.audit(doc_dir, review)

    assert report["status"] == "rouge"
    assert not report["pages"][0]["checks"]["exoneration_mecanique"]["ok"]
    assert not report["pages"][0]["checks"]["mode_de_revue"]["ok"]


def test_un_verdict_exonere_rouge_ou_ambigu_garde_la_porte_rouge(tmp_path: Path) -> None:
    doc_dir = _corpus(tmp_path, columns=False)
    for verdict in ("rouge", "ambigu"):
        review = tmp_path / f"review-exoneree-{verdict}.json"
        _review(doc_dir, review, mode="exoneree", verdict=verdict)

        report = gate.audit(doc_dir, review)

        assert report["status"] == "rouge"
        assert not report["global_checks"]["revue_visuelle_complete"]["ok"]
