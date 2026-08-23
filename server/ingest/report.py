"""AD-8 — rapports statiques d'ingestion (guide et contrat PDF) : invariants d'arbre, statistiques, `ids_disparus`."""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import ValidationError

from server.app.domain import Check, Document, Report

# Estimation grossière : ~4 caractères par token pour du français (le vrai compte vient de l'API en 1.3).
CHARS_PER_TOKEN = 4


def report_from_validation_error(doc_id: str, exc: ValidationError) -> Report:
    """Le modèle a refusé l'arbre (bloc orphelin, id dupliqué…) : un seul check, bloquant."""
    detail = "; ".join(str(e.get("msg", "")) for e in exc.errors())[:2000]
    return Report(doc_id=doc_id, checks=[Check(name="invariants_arbre", level="bloquant", detail=detail)])


def build_report(doc: Document, previous: Document | None, kb: dict[str, Any], *, summary: str = "") -> Report:
    """Checks statiques calculés sans pipeline ; `doc` a déjà passé les invariants du modèle."""
    checks: list[Check] = [Check(name="invariants_arbre", level="info", detail="ok")]
    kinds = Counter(b.kind for b in doc.blocks)
    fiches = kb.get("fiches", [])
    stats: dict[str, Any] = {
        "fiches": len(fiches),
        "faq": len(kb.get("faq", [])),
        "noeuds": len(doc.nodes),
        "blocs": len(doc.blocks),
        "blocs_par_kind": dict(sorted(kinds.items())),
        "tableaux": sum(len(f.get("tableaux", [])) for f in fiches),
        "timeline_phases": len(kb.get("timeline", [])),
    }
    timeline_items = sum(len(p.get("items", [])) for p in kb.get("timeline", []))
    checks.append(Check(name="timeline_non_ingeree", level="info",
                        detail=f"{stats['timeline_phases']} phases, {timeline_items} étapes : parcours hors périmètre (story 1.4)"))
    if summary:
        stats["sommaire_chars"] = len(summary)
        stats["sommaire_tokens_estimes"] = len(summary) // CHARS_PER_TOKEN
        checks.append(Check(name="taille_sommaire", level="info",
                            detail=f"{len(summary)} caractères ≈ {len(summary) // CHARS_PER_TOKEN} tokens"))
    unresolved = [(b.block_id, r) for b in doc.blocks for r in b.unresolved_refs]
    if unresolved:
        checks.append(Check(name="unresolved_refs", level="alerte",
                            detail=", ".join(f"{bid} → {ref}" for bid, ref in unresolved)))
    checks += ids_disparus(doc, previous, stats)
    return Report(doc_id=doc.doc_id, checks=checks, stats=stats)


def ids_disparus(doc: Document, previous: Document | None, stats: dict[str, Any]) -> list[Check]:
    """Un ID est « disparu » s'il n'existe plus ou si son texte a changé (ID réaffecté à un autre paragraphe) ;
    sans `document.json` précédent, rien n'a pu disparaître : le rapport reste identique d'une exécution à l'autre."""
    current = {b.block_id: b.text for b in doc.blocks}
    before = {b.block_id: b.text for b in previous.blocks} if previous else {}
    disparus = [bid for bid, text in before.items() if current.get(bid) != text]  # ordre de lecture de l'ancien
    nouveaux = [bid for bid in current if bid not in before] if previous else []
    stats["ids_disparus"] = len(disparus)
    stats["ids_nouveaux"] = len(nouveaux)
    if disparus:
        return [Check(name="ids_disparus", level="alerte", detail=", ".join(disparus))]
    return []


def _key(numero: str) -> tuple[int, ...]:
    return tuple(int(x) for x in numero.split("."))


def build_pdf_report(doc: Document, previous: Document | None, *, pages: list[Any], numbers: list[str],
                     duplicates: list[str], continues: int, toc: list[Any], summary: str = "") -> Report:
    """Checks statiques d'un contrat PDF (AD-8) : `page_sans_texte` bloquant, numérotation non monotone en alerte."""
    checks: list[Check] = [Check(name="invariants_arbre", level="info", detail="ok")]
    kinds = Counter(b.kind for b in doc.blocks)
    pages_with_blocks = {b.page for b in doc.blocks}
    no_text = [p.page for p in pages if not p.lines]
    blank = [p for p in no_text if not pages[p - 1].images]
    non_blank = [p for p in no_text if pages[p - 1].images]
    stats: dict[str, Any] = {
        "pages": len(pages),
        "pages_avec_blocs": len(pages_with_blocks),
        "pages_sans_texte": ", ".join(str(p) for p in no_text),
        "pages_blanches": len(blank),
        "pages_avec_images": sum(1 for p in pages if p.images),
        "pages_tdm": sum(1 for p in pages if p.is_toc),
        "noeuds": len(doc.nodes),
        "blocs": len(doc.blocks),
        "blocs_par_kind": dict(sorted(kinds.items())),
        "continues": continues,
        "en_tetes_retires": sum(len(p.removed) for p in pages),
        "tdm_pdf_entrees": len(toc),
        "numeros_articles": len(numbers),
    }
    if non_blank:
        checks.append(Check(name="page_sans_texte", level="bloquant",
                            detail="pages non blanches sans texte extrait (OCR ou revue humaine requis) : "
                                   + ", ".join(str(p) for p in non_blank)))
    if blank:
        checks.append(Check(name="pages_blanches", level="info", detail=", ".join(str(p) for p in blank)))
    bad = [f"{prev} → {cur}" for prev, cur in zip(numbers, numbers[1:]) if _key(cur) <= _key(prev)]
    if bad:
        checks.append(Check(name="numerotation_non_monotone", level="alerte", detail=", ".join(bad)[:2000]))
    if duplicates:
        checks.append(Check(name="numerotation_dupliquee", level="alerte", detail=", ".join(duplicates)[:2000]))
    checks.append(Check(name="tdm_pdf", level="info",
                        detail=f"get_toc() : {len(toc)} entrée(s) ; la TdM imprimée n'est pas comparée (story 3.1)"))
    mixed = [p.page for p in pages if p.images and p.lines]
    if mixed:
        checks.append(Check(name="pages_mixtes", level="info",
                            detail=f"{len(mixed)} page(s) texte + image : " + ", ".join(str(p) for p in mixed)[:1500]))
    if summary:
        stats["sommaire_chars"] = len(summary)
        stats["sommaire_tokens_estimes"] = len(summary) // CHARS_PER_TOKEN
    checks += ids_disparus(doc, previous, stats)
    return Report(doc_id=doc.doc_id, checks=checks, stats=stats)
