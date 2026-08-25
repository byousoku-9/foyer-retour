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


def build_report(doc: Document, previous: Document | None, kb: dict[str, Any], *,
                 parcours_ignorees: int, parcours_alertes: list[str], summary: str = "") -> Report:
    """Checks statiques calculés sans pipeline ; `doc` a déjà passé les invariants du modèle.

    `parcours_ignorees`/`parcours_alertes` viennent de `kb_to_blocks.parcours_conditions` : ce module
    ne peut pas l'appeler lui-même (`kb_to_blocks` l'importe déjà) et n'a pas à redécider ce qui a
    été retenu de la `timeline` — il en rend compte.

    Les deux sont **obligatoires** (revue coordonnée 2.3, A5). Avec un défaut, un appelant qui les
    omettait publiait « N phases, M étapes : 0 fiche(s) conditionnée(s), 0 étape(s) ignorée(s) » sur
    un document qui en porte neuf — un check arithmétiquement faux, alors que le commentaire du même
    bloc revendique qu'un lecteur puisse vérifier que 9 + 29 = 38 sans ouvrir la source. Deux tests
    du dépôt étaient déjà dans ce cas.
    """
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
    stats["timeline_etapes"] = timeline_items
    stats["parcours_fiches"] = len(doc.parcours)
    stats["parcours_etapes_ignorees"] = parcours_ignorees
    # Story 2.3 : la `timeline` n'est plus « hors périmètre », elle est projetée en conditions de
    # parcours. L'info dit les deux moitiés du compte — ce qui a été retenu, ce qui ne l'a pas été —
    # parce que la seconde est le cas nominal (une étape sans `si` ne dit rien d'un profil) et
    # qu'un lecteur du rapport doit pouvoir vérifier que 9 + 29 = 38 sans ouvrir la source.
    checks.append(Check(name="parcours_ingere", level="info",
                        detail=f"{stats['timeline_phases']} phases, {timeline_items} étapes : "
                               f"{len(doc.parcours)} fiche(s) conditionnée(s) par le profil, "
                               f"{parcours_ignorees} étape(s) ignorée(s) (texte des étapes jamais ingéré)"))
    if parcours_alertes:
        # AD-8 : alerte et non bloquant — une condition perdue dégrade un classement, elle ne rend
        # pas le document illisible (voir `kb_to_blocks.parcours_conditions`).
        checks.append(Check(name="parcours_condition_ignoree", level="alerte",
                            detail="; ".join(parcours_alertes)[:2000]))
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
                     duplicates: list[str], continues: int, toc: list[Any], toc_gaps: list[str] | None = None,
                     summary: str = "") -> Report:
    """Checks statiques d'un contrat PDF (AD-8) : `page_sans_texte` bloquant (page sans texte mais portant une image
    ou un tracé vectoriel), numérotation non monotone, pages mixtes et écart avec les signets du PDF en alerte."""
    checks: list[Check] = [Check(name="invariants_arbre", level="info", detail="ok")]
    kinds = Counter(b.kind for b in doc.blocks)
    pages_with_blocks = {b.page for b in doc.blocks}
    no_text = [p.page for p in pages if not p.lines]
    blank = [p for p in no_text if not pages[p - 1].visual]
    non_blank = [p for p in no_text if pages[p - 1].visual]
    stats: dict[str, Any] = {
        "pages": len(pages),
        "pages_avec_blocs": len(pages_with_blocks),
        "pages_sans_texte": ", ".join(str(p) for p in no_text),
        "pages_blanches": len(blank),
        "pages_avec_images": sum(1 for p in pages if p.images),
        "pages_sans_texte_dessinees": sum(1 for p in pages if not p.lines and p.drawings),
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
                            detail="pages non blanches (image ou tracé vectoriel) sans texte extrait "
                                   "(OCR ou revue humaine requis) : " + ", ".join(str(p) for p in non_blank)))
    if blank:
        checks.append(Check(name="pages_blanches", level="info", detail=", ".join(str(p) for p in blank)))
    bad = [f"{prev} → {cur}" for prev, cur in zip(numbers, numbers[1:]) if _key(cur) <= _key(prev)]
    if bad:
        checks.append(Check(name="numerotation_non_monotone", level="alerte", detail=", ".join(bad)[:2000]))
    if duplicates:
        checks.append(Check(name="numerotation_dupliquee", level="alerte", detail=", ".join(duplicates)[:2000]))
    checks.append(Check(name="tdm_pdf", level="info",
                        detail=f"get_toc() : {len(toc)} entrée(s) ; la TdM imprimée n'est pas comparée (story 3.1)"))
    if toc_gaps:
        checks.append(Check(name="tdm_pdf_ecart", level="alerte",
                            detail="signets numérotés sans nœud correspondant : " + ", ".join(toc_gaps)[:2000]))
    mixed = [p.page for p in pages if p.images and p.lines]
    if mixed:  # AD-8 : alerte (revue humaine : l'image peut porter du texte non extrait)
        checks.append(Check(name="pages_mixtes", level="alerte",
                            detail=f"{len(mixed)} page(s) texte + image : " + ", ".join(str(p) for p in mixed)[:1500]))
    if summary:
        stats["sommaire_chars"] = len(summary)
        stats["sommaire_tokens_estimes"] = len(summary) // CHARS_PER_TOKEN
    checks += ids_disparus(doc, previous, stats)
    return Report(doc_id=doc.doc_id, checks=checks, stats=stats)
