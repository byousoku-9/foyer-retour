"""AD-8 minimal — rapport statique d'ingestion du guide : invariants d'arbre, statistiques, `ids_disparus`."""

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
    # Un ID est « disparu » s'il n'existe plus ou si son texte a changé (ID réaffecté à un autre paragraphe) ;
    # sans `document.json` précédent, rien n'a pu disparaître : le rapport reste identique d'une exécution à l'autre.
    current = {b.block_id: b.text for b in doc.blocks}
    before = {b.block_id: b.text for b in previous.blocks} if previous else {}
    disparus = [bid for bid, text in before.items() if current.get(bid) != text]  # ordre de lecture de l'ancien
    nouveaux = [bid for bid in current if bid not in before] if previous else []
    stats["ids_disparus"] = len(disparus)
    stats["ids_nouveaux"] = len(nouveaux)
    if disparus:
        checks.append(Check(name="ids_disparus", level="alerte", detail=", ".join(disparus)))
    return Report(doc_id=doc.doc_id, checks=checks, stats=stats)
