"""AD-8 — rapports statiques d'ingestion (guide et contrat PDF) : invariants d'arbre, statistiques, `ids_disparus`."""

from __future__ import annotations

import re
from collections import Counter
from math import ceil
from typing import Any

from pydantic import ValidationError

from server.app.config import get_settings
from server.app.corpus.text import normalize
from server.app.domain import Check, Document, Report, is_citable

_WORD = re.compile(r"[a-zà-öø-ÿ]+", re.IGNORECASE)
_FRENCH_SIGNALS = frozenset({
    "à", "au", "aux", "ce", "ces", "dans", "de", "des", "du", "elle", "en", "est", "et", "la", "le",
    "les", "ne", "ou", "par", "pas", "pour", "que", "qui", "sont", "sur", "un", "une",
})
_FOREIGN_SIGNALS = frozenset({
    "and", "are", "das", "der", "die", "ein", "eine", "for", "from", "is", "of", "oder", "the", "this",
    "und", "von", "with", "you", "com", "não", "para", "uma",
})
# Lexique juridique général, séparé des mots fonctionnels : il empêche qu'une page de clauses très télégraphique
# soit qualifiée de charabia, sans introduire aucun terme propre à un assureur ou à un document.
_LEGAL_WORDS = frozenset({
    "assuré", "assuree", "assureur", "avenant", "clause", "conditions", "contrat", "dommage", "exclusion",
    "franchise", "garantie", "indemnité", "indemnite", "prime", "responsabilité", "responsabilite", "sinistre",
})
_LEGAL_KINDS = frozenset({"definition", "garantie", "exclusion", "condition", "franchise"})
_NEGATIVE_MARKERS = frozenset({"ne", "pas", "exclu", "exclus", "exclue", "exclues", "sauf", "hors", "jamais"})
_TYPING_CHECKS = frozenset({
    "corruption_decisionnelle", "unresolved_refs", "definition_introuvable",
    "exclusion_sans_marqueur", "confiance_typage_faible", "kinds_non_confirmes", "typage_clauses",
    "typage_transport",
})


def _tree_category(message: str) -> str:
    """Catégorie stable pour les formulations françaises ou ASCII des validateurs structurels."""
    lowered = message.lower()
    if "block_id dupliqué" in lowered:
        return "block_id_duplique"
    if ("node_id" in lowered or "nœud" in lowered) and "dupliqu" in lowered:
        return "node_id_duplique"
    if "orphelin" in lowered:
        return "bloc_orphelin"
    if "rattaché à deux nœuds" in lowered:
        return "bloc_parent_multiple"
    if ("nœud" in lowered or "node_id" in lowered) \
            and ("deux parents" in lowered or "multiple parent" in lowered):
        return "noeud_parent_multiple"
    if "cycle" in lowered:
        return "cycle_noeuds"
    if "racine" in lowered:
        return "racine_invalide"
    return "reference_ou_forme_invalide"


def report_from_validation_error(doc_id: str, exc: ValidationError) -> Report:
    """Le modèle a refusé l'arbre : la catégorie précise accompagne le bloquant P5."""
    messages = [str(e.get("msg", "")) for e in exc.errors()]
    categories = [_tree_category(message) for message in messages]
    detail = f"catégories={','.join(dict.fromkeys(categories))} ; " + "; ".join(messages)
    detail = detail[:2000]
    return Report(doc_id=doc_id, checks=[Check(name="invariants_arbre", level="bloquant", detail=detail)])


def structure_check(motif: str | None, detail: str) -> Check:
    """`structure_proposee` (story 4.2c) : bloquant sur refus nommé, info sinon.

    Fail-closed : un refus du vérificateur met le document en quarantaine et le motif est publié tel
    quel. Il n'existe **pas** de niveau intermédiaire — retomber sur l'heuristique numérique après un
    refus servirait un arbre que personne n'a prouvé en laissant croire le contraire (AD-16).
    """
    if motif is None:
        return Check(name="structure_proposee", level="info", detail=detail[:2000])
    return Check(name="structure_proposee", level="bloquant", detail=f"{motif} : {detail}"[:2000])


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
        checks.append(Check(name="taille_sommaire", level="info",
                            detail=f"{len(summary)} caractères (aucune estimation de tokens publiée)"))
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


def _page_words(page: Any) -> list[str]:
    text = " ".join(line.text for line in page.lines)
    for table in getattr(page, "tables", []):
        text += " " + " ".join(cell for row in table.rows for cell in row)
    return [word.lower() for word in _WORD.findall(text)]


def _quality(pages: list[Any]) -> tuple[list[int], list[int], list[str]]:
    """Pages non françaises, pages de charabia et résidus d'en-tête ; contrôles génériques non bloquants."""
    settings = get_settings()
    foreign: list[int] = []
    gibberish: list[int] = []
    edge_texts: Counter[str] = Counter()
    eligible = 0
    for page in pages:
        words = _page_words(page)
        french = sum(word in _FRENCH_SIGNALS for word in words) / len(words) if words else 0.0
        foreign_signals = sum(word in _FOREIGN_SIGNALS for word in words)
        # Une courte page portant des signaux étrangers nets reste étrangère ; `quality_min_words`
        # borne seulement le calcul de charabia, trop instable sur quelques mots.
        if french < settings.french_signal_ratio_min and foreign_signals >= settings.foreign_signal_min:
            foreign.append(page.page)
        if len(words) >= settings.quality_min_words:
            plausible = sum(any(char in "aeiouyàâäéèêëîïôöùûü" for char in word) or word in _LEGAL_WORDS
                            for word in words)
            if 1 - plausible / len(words) > settings.gibberish_ratio_max:
                gibberish.append(page.page)
        if page.lines and not page.is_toc:
            eligible += 1
            band_lines = {
                normalize(line.text) for line in page.lines
                if line.bbox[1] < settings.header_band_pt
                or line.bbox[3] > page.height - settings.footer_band_pt
            }
            for key in band_lines - {""}:
                edge_texts[key] += 1
    minimum = max(2, ceil(eligible * settings.residual_header_min_pages_ratio))
    residues = [text for text, count in edge_texts.items() if count >= minimum]
    return foreign, gibberish, residues


_NODE_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?(?:\s|$)")


def numero_de_noeud(doc_id: str, node_id: str, title: str) -> str | None:
    """Numéro d'article d'un nœud, **quel que soit le chemin qui a bâti l'arbre** (story 4.2c).

    L'heuristique numérique encode le numéro dans l'identifiant (`{doc_id}:a3.1`) ; une proposition
    vérifiée le laisse là où l'imprimeur l'a mis, dans le **titre** relu au registre, et calcule un
    chemin positionnel (`{doc_id}:s1.2`). Les deux contrôles de table des matières lisent donc cet
    index plutôt qu'une convention d'identifiant qu'un seul des deux chemins respecte — sans quoi un
    document ingéré avec proposition publiait « signets sans nœud correspondant » et « numéros
    annoncés absents de l'arbre » pour **tous** ses numéros, alertes toutes fausses.
    """
    prefix = f"{doc_id}:a"
    if node_id.startswith(prefix):
        return node_id[len(prefix):].split("-", 1)[0]
    match = _NODE_NUMBER_RE.match(title.strip())
    return match.group(1) if match else None


def _normalized_article_title(title: str) -> str:
    return normalize(re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", title)).strip()


def _titles_match(left: str, right: str) -> bool:
    """Un titre d'article sur une ligne peut être le préfixe honnête de son intitulé multilignes en TdM."""
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= get_settings().toc_title_prefix_min_chars and longer.startswith(shorter + " ")


def _printed_toc_check(doc: Document, entries: list[tuple[str, str]], pages_toc: int) -> Check:
    if not pages_toc:
        return Check(name="tdm_imprimee", level="info", detail="aucune TdM imprimée détectée")
    if not entries:
        return Check(name="tdm_imprimee", level="alerte",
                     detail="TdM imprimée détectée, mais aucune entrée numérotée exploitable")
    expected: dict[str, str] = {}
    for node in doc.nodes:
        numero = numero_de_noeud(doc.doc_id, node.node_id, node.title)
        if numero is None or node.node_id == doc.doc_id:
            continue
        expected.setdefault(numero, _normalized_article_title(node.title))
    titles_by_number: dict[str, list[str]] = {}
    printed: dict[str, str] = {}
    for numero, title in entries:
        if not numero:
            continue
        normalized = _normalized_article_title(title)
        titles_by_number.setdefault(numero, []).append(normalized)
        printed.setdefault(numero, normalized)
    duplicate_numbers = sorted((numero for numero, titles in titles_by_number.items() if len(titles) > 1), key=_key)
    conflicting_numbers = sorted(
        (numero for numero, titles in titles_by_number.items() if len(set(titles)) > 1), key=_key,
    )
    announced_levels = {numero.count(".") + 1 for numero in printed}
    comparable_expected = {numero: title for numero, title in expected.items()
                           if numero.count(".") + 1 in announced_levels}
    unknown_numbers = sorted(set(printed) - set(expected), key=_key)
    missing_numbers = sorted(set(comparable_expected) - set(printed), key=_key)
    different_titles = sorted(
        (numero for numero in set(printed) & set(expected)
         if not _titles_match(printed[numero], expected[numero])),
        key=_key,
    )
    details = []
    if duplicate_numbers:
        details.append("numéros annoncés plusieurs fois : " + ", ".join(duplicate_numbers))
    if conflicting_numbers:
        details.append("titres conflictuels pour un même numéro : " + ", ".join(conflicting_numbers))
    if unknown_numbers:
        details.append("numéros annoncés absents de l'arbre : " + ", ".join(unknown_numbers))
    if missing_numbers:
        details.append("numéros de l'arbre absents de la TdM : " + ", ".join(missing_numbers))
    if different_titles:
        details.append("titres différents : " + ", ".join(different_titles))
    if details:
        return Check(name="tdm_imprimee", level="alerte", detail=" ; ".join(details)[:2000])
    return Check(name="tdm_imprimee", level="info",
                 detail=f"{len(printed)} titre(s) numéroté(s) concordent avec l'arbre aux niveaux annoncés")


def build_pdf_report(doc: Document, previous: Document | None, *, pages: list[Any], numbers: list[str],
                     duplicates: list[str], continues: int, toc: list[Any], toc_gaps: list[str] | None = None,
                     printed_toc: list[tuple[str, str]] | None = None, summary: str = "",
                     structure: str | None = None) -> Report:
    """Checks statiques d'un contrat PDF (AD-8) : `page_sans_texte` bloquant (page sans texte mais portant une image
    ou un tracé vectoriel), numérotation non monotone, pages mixtes et écart avec les signets du PDF en alerte.

    `structure` n'est renseigné que lorsqu'une proposition est **en jeu** : sans elle, aucun check
    `structure_proposee` n'est émis, et le rapport d'un document ingéré par la seule heuristique
    reste exactement celui d'avant la story 4.2c.
    """
    checks: list[Check] = [Check(name="invariants_arbre", level="info", detail="ok")]
    if structure is not None:
        checks.append(structure_check(None, structure))
    kinds = Counter(b.kind for b in doc.blocks)
    pages_with_blocks = {b.page for b in doc.blocks}
    no_text = [p.page for p in pages if not p.lines and not getattr(p, "tables", [])]
    blank = [p for p in no_text if not pages[p - 1].visual]
    non_blank = [p for p in no_text if pages[p - 1].visual]
    foreign, gibberish, residues = _quality(pages)
    eligible_coverage = [p for p in pages if p.page not in foreign and (p.visual or p.lines or getattr(p, "tables", []))]
    covered = [p for p in eligible_coverage if p.lines or getattr(p, "tables", [])]
    coverage = len(covered) / len(eligible_coverage) if eligible_coverage else 1.0
    stats: dict[str, Any] = {
        "pages": len(pages),
        "pages_avec_blocs": len(pages_with_blocks),
        "pages_sans_texte": ", ".join(str(p) for p in no_text),
        "pages_blanches": len(blank),
        "pages_avec_images": sum(1 for p in pages if p.images),
        "pages_ocr": sum(1 for p in pages if getattr(p, "ocr_succeeded", False)),
        "pages_ocr_echec": sum(1 for p in pages if getattr(p, "ocr_attempted", False)
                               and not getattr(p, "ocr_succeeded", False)),
        "pages_non_francaises": len(foreign),
        "couverture": round(coverage, 4),
        "pages_sans_texte_dessinees": sum(1 for p in pages if not p.lines and p.drawings),
        "pages_tdm": sum(1 for p in pages if p.is_toc),
        "noeuds": len(doc.nodes),
        "blocs": len(doc.blocks),
        "blocs_par_kind": dict(sorted(kinds.items())),
        "continues": continues,
        "en_tetes_retires": sum(len(p.removed) for p in pages),
        "tdm_pdf_entrees": len(toc),
        "numeros_articles": len(numbers),
        "tables": kinds.get("table", 0),
        # Donnée structurée consommée par le typage 3.2. Il ne reparcourt ni les pages PDF ni la
        # prose du check `charabia` pour savoir si une clause décisionnelle est corrompue.
        "pages_charabia": {str(page): 1 for page in gibberish},
    }
    non_citable = [block for block in doc.blocks if not is_citable(block)]
    non_citable_pages = sorted({block.page for block in non_citable if block.page is not None})
    stats["blocs_non_citables"] = len(non_citable)
    stats["pages_non_citables"] = len(non_citable_pages)
    if non_citable:
        sources = Counter(block.source_field or f"kind={block.kind}" for block in non_citable)
        checks.append(Check(
            name="blocs_non_citables",
            level="alerte",
            detail=f"{len(non_citable)} bloc(s) sur {len(non_citable_pages)} page(s) exclus du rappel ; "
                   f"pages : {', '.join(str(page) for page in non_citable_pages) or 'hors page'} ; "
                   + ", ".join(f"{source}={count}" for source, count in sorted(sources.items())),
        ))
    if non_blank:
        errors = [f"p.{p.page}: {p.ocr_error}" for p in pages if p.page in non_blank and p.ocr_error]
        dependency = " ; dépendance OCR Tesseract (langue fra) : " + (
            " ; ".join(errors) if errors else "aucun texte exploitable"
        )
        checks.append(Check(name="page_sans_texte", level="bloquant",
                            detail="pages non blanches (image ou tracé vectoriel) sans texte extrait "
                                   "après OCR ciblé : " + ", ".join(str(p) for p in non_blank) + dependency))
    if blank:
        checks.append(Check(name="pages_blanches", level="info", detail=", ".join(str(p) for p in blank)))
    ocr_block_pages = {block.page for block in doc.blocks if block.source_field == "ocr"}
    ocr_pages = [p.page for p in pages if getattr(p, "ocr_succeeded", False) and p.page in ocr_block_pages]
    if ocr_pages:
        checks.append(Check(name="ocr_cible", level="info",
                            detail="pages OCRisées et marquées `source_field=ocr` : " +
                                   ", ".join(str(page) for page in ocr_pages)))
    bad = [f"{prev} → {cur}" for prev, cur in zip(numbers, numbers[1:]) if _key(cur) <= _key(prev)]
    if bad:
        checks.append(Check(name="numerotation_non_monotone", level="alerte", detail=", ".join(bad)[:2000]))
    if duplicates:
        checks.append(Check(name="numerotation_dupliquee", level="alerte", detail=", ".join(duplicates)[:2000]))
    checks.append(_printed_toc_check(doc, printed_toc or [], stats["pages_tdm"]))
    checks.append(Check(name="tdm_pdf", level="info", detail=f"get_toc() : {len(toc)} entrée(s), contrôle distinct"))
    if toc_gaps:
        checks.append(Check(name="tdm_pdf_ecart", level="alerte",
                            detail="signets numérotés sans nœud correspondant : " + ", ".join(toc_gaps)[:2000]))
    mixed = []
    for page in pages:
        native = getattr(page, "native_text", None)
        if native is None:  # PageText synthétique : déduire la provenance comme avant.
            native = bool(page.lines or getattr(page, "tables", []))
        if native and page.image_area_ratio > 0 \
                and page.image_area_ratio >= get_settings().mixed_page_image_density:
            mixed.append(page.page)
    if mixed:  # AD-8 : alerte (revue humaine : l'image peut porter du texte non extrait)
        checks.append(Check(name="pages_mixtes", level="alerte",
                            detail=f"{len(mixed)} page(s) au-dessus de la densité image "
                                   f"{get_settings().mixed_page_image_density:.2f} : " +
                                   ", ".join(str(p) for p in mixed)[:1500]))
    if residues:
        checks.append(Check(name="residus_entete_pied", level="alerte", detail=", ".join(residues)[:2000]))
    if gibberish:
        checks.append(Check(name="charabia", level="alerte", detail=", ".join(str(page) for page in gibberish)))
    if foreign:
        checks.append(Check(name="pages_non_francaises", level="alerte",
                            detail="exclues du dénominateur de couverture : " +
                                   ", ".join(str(page) for page in foreign)))
    checks.append(Check(name="couverture", level="info",
                        detail=f"{len(covered)}/{len(eligible_coverage)} page(s) éligible(s), soit {coverage:.1%} ; "
                               f"seuil indicatif {get_settings().coverage_threshold:.0%}"))
    if summary:
        stats["sommaire_chars"] = len(summary)
    checks += ids_disparus(doc, previous, stats)
    return Report(doc_id=doc.doc_id, checks=checks, stats=stats)


def pages_charabia(report: Report) -> set[int]:
    """Exige la sortie structurée 3.1 ; aucun rapport ancien ne vaut implicitement « zéro page »."""
    if "pages_charabia" not in report.stats:
        raise ValueError("report.stats.pages_charabia absent (relancer pdf_to_blocks avant tout Batch)")
    raw_pages = report.stats["pages_charabia"]
    if not isinstance(raw_pages, dict) or any(
        not isinstance(page, str) or not page.isdecimal() or int(page) < 1
        or isinstance(present, bool) or present != 1
        for page, present in raw_pages.items()
    ):
        raise ValueError(
            "report.stats.pages_charabia doit être un objet structuré {\"page\": 1} "
            "(relancer pdf_to_blocks avant tout Batch)"
        )
    return {int(page) for page in raw_pages}


def enrich_typing_report(
    report: Report,
    doc: Document,
    *,
    rejected_definitions: dict[str, str] | None = None,
) -> Report:
    """Ajoute les checks AD-8 du typage sans modifier les checks structurels de 3.1.

    La fonction est idempotente : une reprise remplace ses propres checks, jamais ceux du parsing.
    La corruption décisionnelle repose exclusivement sur `stats.pages_charabia`, publié par
    `build_pdf_report`; aucune phrase humaine du rapport n'est parsée.
    """
    checks = [check for check in report.checks if check.name not in _TYPING_CHECKS]
    legal = [block for block in doc.blocks if block.kind in _LEGAL_KINDS]
    typed = [block for block in doc.blocks if block.kind_source in {"model", "model_verified"}]
    confirmed = [block for block in legal if block.kind_source == "model_verified"]

    gibberish_pages = pages_charabia(report)
    corrupted = [block.block_id for block in legal if block.page in gibberish_pages]
    if corrupted:
        checks.append(Check(name="corruption_decisionnelle", level="bloquant",
                            detail="bloc(s) juridique(s) sur une page signalée comme charabia : "
                                   + ", ".join(corrupted)[:1900]))

    unresolved = [(block.block_id, ref) for block in typed for ref in block.unresolved_refs]
    if unresolved:
        checks.append(Check(name="unresolved_refs", level="alerte",
                            detail=", ".join(f"{block_id} → {ref}" for block_id, ref in unresolved)[:2000]))

    rejected_definitions = rejected_definitions or {}
    missing_definitions = [block.block_id for block in legal if block.kind == "definition" and not block.defines
                           and block.block_id not in rejected_definitions]
    definition_details = [*missing_definitions, *(
        f"{block_id} → {term}" for block_id, term in rejected_definitions.items()
    )]
    if definition_details:
        checks.append(Check(name="definition_introuvable", level="alerte",
                            detail=", ".join(definition_details)[:2000]))

    exclusions_without_marker = []
    for block in legal:
        if block.kind != "exclusion":
            continue
        tokens = set(normalize(block.text).split())
        if not tokens & _NEGATIVE_MARKERS:
            exclusions_without_marker.append(block.block_id)
    if exclusions_without_marker:
        checks.append(Check(name="exclusion_sans_marqueur", level="alerte",
                            detail=", ".join(exclusions_without_marker)[:2000]))

    threshold = get_settings().kind_confidence_min
    low_confidence = [block.block_id for block in typed
                      if block.kind_confidence is None or block.kind_confidence < threshold]
    if low_confidence:
        checks.append(Check(name="confiance_typage_faible", level="alerte",
                            detail=f"seuil {threshold:.2f} : " + ", ".join(low_confidence)[:1950]))

    unconfirmed = [block.block_id for block in legal if block.kind_source != "model_verified"]
    if unconfirmed:
        checks.append(Check(name="kinds_non_confirmes", level="alerte",
                            detail=", ".join(unconfirmed)[:2000]))

    stats = dict(report.stats)
    # Après typage, `kind` est sémantique ; `structural_kind` reste la source de mise en page.
    stats["tables"] = sum((block.structural_kind or block.kind) == "table" for block in doc.blocks)
    stats.update({
        "blocs_types_modele": len(typed),
        "blocs_juridiques": len(legal),
        "blocs_juridiques_confirmes": len(confirmed),
        "references_non_resolues": len(unresolved),
    })
    checks.append(Check(name="typage_clauses", level="info",
                        detail=f"{len(typed)} bloc(s) étiqueté(s), {len(confirmed)}/{len(legal)} "
                               "kind(s) juridique(s) confirmé(s) par deux lectures"))
    return Report(doc_id=report.doc_id, checks=checks, stats=stats)
