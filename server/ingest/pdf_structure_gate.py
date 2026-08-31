"""Porte hors réseau sur la structure visuelle d'un PDF avant son typage.

La porte reconstruit le document en mémoire, identifie les pages dont la lecture géométrique
change, puis oppose à chacune les invariants du registre de lignes, de l'ordre des colonnes et de
l'arbre. Une revue visuelle séparée est liée aux octets rendus par leur SHA-256 : une page absente,
ambiguë ou revue sur un autre rendu garde la porte rouge.

    uv run python -m server.ingest.pdf_structure_gate DOC_ID \
      --visual-review revue.json --output rapport.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pymupdf

from server.app.config import REPO_ROOT
from server.app.corpus.text import normalize
from server.app.domain import Document, Report, is_citable
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE
from server.ingest.artifacts import document_json, write_atomic
from server.ingest.pdf_to_blocks import (
    PageText,
    anomalies_registre,
    build_document,
    extract_pages,
    reutiliser_typage_identique,
)
from server.ingest.structure import charger_octets, presente

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_hashes(pdf_path: Path, pages: list[int], *, dpi: int) -> dict[int, str]:
    hashes: dict[int, str] = {}
    with pymupdf.open(pdf_path) as pdf:
        for page in pages:
            pixmap = pdf[page - 1].get_pixmap(dpi=dpi, alpha=False)
            hashes[page] = hashlib.sha256(pixmap.tobytes("png")).hexdigest()
    return hashes


def _visual_review(path: Path, expected_pages: list[int], render_hashes: dict[int, str]) -> tuple[
        dict[int, dict[str, str]], list[str], int]:
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [f"revue visuelle illisible: {type(exc).__name__}"], 144
    if not isinstance(raw, dict) or raw.get("schema_version") != "1":
        return {}, ["revue visuelle: schema_version 1 attendu"], 144
    dpi = raw.get("dpi")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 72 <= dpi <= 600:
        return {}, ["revue visuelle: dpi entier entre 72 et 600 attendu"], 144
    reader = raw.get("reader")
    if not isinstance(reader, str) or not reader.strip():
        return {}, ["revue visuelle: lecteur indépendant non renseigné"], dpi
    entries = raw.get("pages")
    if not isinstance(entries, list):
        return {}, ["revue visuelle: liste pages attendue"], dpi
    by_page: dict[int, dict[str, str]] = {}
    errors: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("revue visuelle: entrée de page non objet")
            continue
        page, rendered, verdict, note = (
            entry.get("page"), entry.get("render_sha256"), entry.get("verdict"), entry.get("note", "")
        )
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            errors.append("revue visuelle: numéro de page invalide")
            continue
        if page in by_page:
            errors.append(f"revue visuelle: page {page} dupliquée")
            continue
        if not isinstance(rendered, str) or _SHA256_RE.fullmatch(rendered) is None:
            errors.append(f"revue visuelle: page {page}, render_sha256 invalide")
            continue
        if verdict not in {"ok", "rouge", "ambigu"}:
            errors.append(f"revue visuelle: page {page}, verdict invalide")
            continue
        if not isinstance(note, str):
            errors.append(f"revue visuelle: page {page}, note non textuelle")
            continue
        by_page[page] = {"render_sha256": rendered, "verdict": verdict, "note": note}
    if set(by_page) != set(expected_pages):
        errors.append(
            "revue visuelle: pages différentes du delta "
            f"(absentes={sorted(set(expected_pages) - set(by_page))}, "
            f"inattendues={sorted(set(by_page) - set(expected_pages))})"
        )
    for page in set(by_page) & set(render_hashes):
        if by_page[page]["render_sha256"] != render_hashes[page]:
            errors.append(f"revue visuelle: page {page}, rendu différent de celui audité")
        if by_page[page]["verdict"] != "ok":
            errors.append(f"revue visuelle: page {page}, verdict {by_page[page]['verdict']}")
    return by_page, errors, dpi


def _semantic_issues(doc: Document) -> list[str]:
    node_ids = {node.node_id for node in doc.nodes}
    block_ids = {block.block_id for block in doc.blocks}
    issues: list[str] = []
    for block in doc.blocks:
        try:
            doc.node_of(block.block_id)
        except (KeyError, ValueError):
            issues.append(f"{block.block_id}: bloc orphelin de l'arbre")
        for node_id in [*block.scope_node_ids, *([block.scope_node_id] if block.scope_node_id else [])]:
            if node_id not in node_ids:
                issues.append(f"{block.block_id}: portée absente {node_id}")
        targets = [
            *block.refs,
            *([block.overrides] if block.overrides else []),
            *(value for value in block.relation.model_dump().values() if value is not None),
        ]
        for target in targets:
            if target not in block_ids:
                issues.append(f"{block.block_id}: dépendance absente {target}")
    return issues


def _page_issues(
        page: PageText,
        doc: Document,
        source_uids: dict[str, list[str]],
) -> dict[str, list[str]]:
    sources = {line.uid: line for line in page.source.lines}
    carriers: dict[str, list[str]] = {}
    for block_id, uids in source_uids.items():
        for uid in uids:
            if uid in sources:
                carriers.setdefault(uid, []).append(block_id)
    coverage: list[str] = []
    assignment: list[str] = []
    for uid, source in sources.items():
        count = len(carriers.get(uid, [])) + int(uid in page.source.removed)
        if count != 1:
            assignment.append(f"{uid}: {count} affectation(s)")
            continue
        if uid in page.source.removed:
            continue
        block = doc.block(carriers[uid][0])
        source_fragment = normalize(source.text)
        # Le parseur conserve le tiret dans chaque Line, puis le retire volontairement au passage
        # à la ligne dans `Block.text`. La couverture porte sur les caractères utiles, pas sur ce
        # tiret typographique de césure.
        if source_fragment.endswith("-"):
            source_fragment = source_fragment[:-1]
        if source_fragment not in normalize(block.text):
            coverage.append(f"{uid}: texte absent de {block.block_id}")

    order: list[str] = []
    keys = [(line.bande, line.colonne, line.bbox[1], line.bbox[0]) for line in page.lines]
    if keys != sorted(keys):
        order.append("les lignes ne sont pas ordonnées par bande, colonne, y puis x")
    for (band, column), lines in _group_lines(page).items():
        yx = [(line.bbox[1], line.bbox[0]) for line in lines]
        if column > 0 and yx != sorted(yx):
            order.append(f"bande {band}, colonne {column}: ordre visuel non monotone")

    crossings: list[str] = []
    for block in (candidate for candidate in doc.blocks if candidate.page == page.page):
        columns = {page.layout.colonne(line.bbox) for line in block.lines}
        side_columns = columns - {0}
        if len(side_columns) > 1 or (0 in columns and side_columns):
            crossings.append(f"{block.block_id}: colonnes {sorted(columns)}")
    return {
        "couverture_texte_utile": coverage,
        "affectation_unique": assignment,
        "ordre_colonne_par_colonne": order,
        "aucun_croisement": crossings,
    }


def _group_lines(page: PageText) -> dict[tuple[int, int], list[Any]]:
    groups: dict[tuple[int, int], list[Any]] = {}
    for line in page.lines:
        groups.setdefault((line.bande, line.colonne), []).append(line)
    return groups


def audit(doc_dir: Path, visual_review_path: Path) -> dict[str, Any]:
    """Reconstruit et audite un contrat sans écrire dans l'espace de publication."""
    pdf_path = doc_dir / "source.pdf"
    expected_path = doc_dir / "source.sha256"
    expected_hash = expected_path.read_text("utf-8").strip()
    if _SHA256_RE.fullmatch(expected_hash) is None:
        raise ValueError("source.sha256 mal formé")
    source_hash = _sha256(pdf_path)
    if source_hash != expected_hash:
        raise ValueError(f"source.pdf: sha256 {source_hash} différent de source.sha256")

    previous = Document.model_validate_json((doc_dir / "document.json").read_bytes())
    previous_report_bytes = (doc_dir / "report.json").read_bytes()
    Report.model_validate_json(previous_report_bytes)
    pages, toc = extract_pages(pdf_path)
    extraction_order = {
        page.page: [tuple(line.source_uids) for line in page.lines]
        for page in pages
    }
    structure = charger_octets(doc_dir / "structure.json")[0] if presente(doc_dir / "structure.json") else None
    built, meta = build_document(
        pages,
        edition=previous.edition,
        source_hash=source_hash,
        toc=toc,
        doc_id=previous.doc_id,
        title=previous.title,
        source_url=previous.source_url,
        structure=structure,
    )
    typed, reused = reutiliser_typage_identique(built, previous, previous_report_bytes)
    changed_pages = [
        page.page for page in pages
        if page.layout.multi
        or extraction_order[page.page] != [tuple(line.source_uids) for line in page.lines]
    ]
    review_raw = json.loads(visual_review_path.read_text("utf-8"))
    requested_dpi = review_raw.get("dpi", 144) if isinstance(review_raw, dict) else 144
    dpi = requested_dpi if isinstance(requested_dpi, int) and not isinstance(requested_dpi, bool) else 144
    render_hashes = _render_hashes(pdf_path, changed_pages, dpi=dpi)
    visual, visual_errors, reviewed_dpi = _visual_review(
        visual_review_path, changed_pages, render_hashes,
    )
    if reviewed_dpi != dpi:
        visual_errors.append("revue visuelle: dpi relu incohérent")

    registry_issues = anomalies_registre(pages, meta["source_uids"])
    semantic_issues = _semantic_issues(typed)
    page_results: list[dict[str, Any]] = []
    for page_number in changed_pages:
        page = pages[page_number - 1]
        checks = _page_issues(page, typed, meta["source_uids"])
        visual_entry = visual.get(page_number)
        checks["revue_visuelle"] = [] if (
            visual_entry is not None
            and visual_entry["verdict"] == "ok"
            and visual_entry["render_sha256"] == render_hashes[page_number]
        ) else ["revue visuelle absente, ambiguë, rouge ou liée à un autre rendu"]
        page_results.append({
            "page": page_number,
            "render_sha256": render_hashes[page_number],
            "boundaries": page.layout.boundaries,
            "lines": len(page.lines),
            "tables": len(page.tables),
            "blocks": sum(block.page == page_number for block in typed.blocks),
            "review_note": visual_entry["note"] if visual_entry is not None else "",
            "checks": {name: {"ok": not errors, "errors": errors} for name, errors in checks.items()},
            "status": "vert" if all(not errors for errors in checks.values()) else "rouge",
        })

    global_errors = [*registry_issues, *semantic_issues, *visual_errors]
    status = "vert" if not global_errors and all(page["status"] == "vert" for page in page_results) else "rouge"
    return {
        "schema_version": "1",
        "status": status,
        "doc_id": previous.doc_id,
        "source_hash": source_hash,
        "previous_ingest_fingerprint": previous.ingest_fingerprint,
        "candidate_ingest_fingerprint": built.ingest_fingerprint,
        "candidate_document_sha256": hashlib.sha256(document_json(typed).encode("utf-8")).hexdigest(),
        "visual_dpi": dpi,
        "changed_pages": changed_pages,
        "summary": {
            "pages_total": len(pages),
            "pages_audited": len(changed_pages),
            "source_lines": sum(len(page.source.lines) for page in pages),
            "candidate_blocks": len(typed.blocks),
            "typing_reused_blocks": len(reused),
            "typing_pending_blocks": sum(
                is_citable(block) and block.block_id not in reused for block in typed.blocks
            ),
        },
        "global_checks": {
            "registre_sans_orphelin": {"ok": not registry_issues, "errors": registry_issues},
            "arbre_portees_dependances": {"ok": not semantic_issues, "errors": semantic_issues},
            "revue_visuelle_complete": {"ok": not visual_errors, "errors": visual_errors},
        },
        "pages": page_results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("doc_id")
    parser.add_argument("--data", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--visual-review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if len(args.doc_id) > DOC_ID_MAX or DOC_ID_RE.fullmatch(args.doc_id) is None:
        print(f"doc_id invalide: {args.doc_id!r}", file=sys.stderr)
        return 2
    try:
        report = audit(args.data / args.doc_id, args.visual_review)
        write_atomic(args.output, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, pymupdf.FileDataError) as exc:
        print(f"porte refusée: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        f"porte structurelle {report['status']}: {len(report['changed_pages'])} page(s) auditée(s), "
        f"rapport {args.output}"
    )
    return 0 if report["status"] == "vert" else 1


if __name__ == "__main__":
    sys.exit(main())
