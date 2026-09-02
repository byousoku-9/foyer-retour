"""PDF → arbre de blocs d'un contrat (AD-2), artefacts `data/{doc_id}/*` et `data/manifest.json` (AD-7).

    uv run python -m server.ingest.pdf_to_blocks axa-lu-optihome-2017 [--data data] [--edition "juin 2017"]

Extraction : `page.get_text("dict", sort=True)` ; une `Line` par ligne visuelle (un numéro d'article et le texte
qui le suit sur la même ligne de base sont fusionnés) ; en-têtes/pieds répétés retirés (bande haute/basse, texte
récurrent ou numéro de page). Segmentation : un numéro d'article ouvre un nœud `{doc_id}:a{numero}` (parent = préfixe
le plus long existant, sinon la racine) ; une ligne à puce ouvre un item de `list` ; un écart vertical supérieur à
`para_gap_ratio` hauteurs de ligne ouvre un `para` ; un paragraphe qui continue sur la page suivante est scindé,
le second bloc porte `continues`. Un intitulé autonome de table des matières, dans une ligne ou une table, ouvre
le nœud `{doc_id}:tdm` et se propage aux pages de continuation ; son texte reste `autre`, ses tables restent atomiques,
et ses sections numérotées sont comparées à l'arbre reconstruit.
`text` est brut : seules altérations, déclarées dans `FLAGS` (donc dans l'empreinte), les puces Wingdings deviennent
`•`, le glyphe de tabulation `\\x07` est retiré, les espaces/tabulations de fin de ligne sont retirés, les espaces
de tête d'une ligne sans puce sont retirés, et un numéro d'article seul sur sa ligne est joint par une espace au texte
qui le suit sur la même ligne de base (`merge_number_sep`). `get_toc()` (signets du PDF), s'il est non vide, donne
leur titre aux nœuds qui n'en ont pas et est confronté à la numérotation (`tdm_pdf_ecart`, alerte).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pymupdf
from pydantic import ValidationError

from server.app.config import get_settings
from server.app.corpus.text import normalize, normalize_version
from server.app.domain import Block, BlockRef, Check, Document, Line, ManifestEntry, Node, NodeRef, Report, is_citable
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE
from server.ingest.artifacts import (OVERLAY_FILE, SCHEMA_VERSION, STRUCTURE_FILE,
                                     TYPING_REUSED_IDS_STAT, LectureDuLot, document_json,
                                     exiger_espace_installe, merge_manifest, read_manifest,
                                     verifier_couverture_du_lot)
from server.ingest.fetch_source import GS_URL_RE
from server.ingest.line_identity import line_uid
from server.ingest.report import (attester_arbre, attester_structure, build_pdf_report,
                                  canoniser_transition_apres_typage, enrich_typing_report,
                                  numero_de_noeud, report_from_validation_error, structure_check)
from server.ingest.structure import (STRUCTURE_RULES_VERSION, NoeudVerifie, StructureProposee,
                                     StructureRefusee, arbre, charger_octets, empreinte_proposition,
                                     presente, registre_lignes, verifier)

DOC_ID = "axa-lu-optihome-2017"
TITLE = "Conditions d’assurances OptiHome (multirisques habitation)"
DEFAULT_EDITION = "juin 2017"
TYPING_REUSED_COUNT_STAT = "blocs_typage_reutilises"
TYPING_PENDING_COUNT_STAT = "blocs_typage_a_rejouer"

# Entrent dans `ingest_fingerprint` : toute modification change les IDs attendus (AD-2, stabilité).
# story 4.2c : registre de lignes source, colonnes géométriques (lignes **et** tables), structure
# vérifiée. La génération reste `10` : elle nomme la story, dont les artefacts committés sont déjà
# déclarés périmés dans `docs/choix-et-limites.md`, et c'est `SEGMENTATION_RULES` — lui aussi dans
# l'empreinte — qui porte l'énoncé exact de la règle et change dès qu'elle change.
PARSER_VERSION = "14-visual-baseline-review"
SEGMENTATION_RULES = ("numero:^\\d+(\\.\\d+)*$@x0<article_number_max_x=>noeud a{numero}(parent=prefixe);"
                      "titre:meme_ligne_de_base(size>=title_min_size_pt|sans_ponct_finale&suite_majuscule)=>heading;"
                      "puce:Wingdings|^•=>list(item;continuation=indent>list_indent_pt|minuscule&prec!~[.;:]$);"
                      "gap>para_gap_ratio*h=>para;"
                      "entete:bande&(recurrent|numero_page|MAJUSCULES<=header_caps_max_size_pt);"
                      "table:find_tables=>frontieres_cellules_meme_vides&spans_source=>texte_fidele"
                      "&bloc_atomique(cellules=' | ')&lignes_source_portees_une_fois"
                      "&ligne_hors_cellule=>retour_flux_texte&sans_source_ni_texte=>sans_bloc"
                      "&geometrie_dans_la_detection(rangees a l'aplomb de la boite,sans_bloc=>ignoree);"
                      "ocr:page_visuelle_sans_texte_retenu=>get_textpage_ocr(fra)&source_field=ocr;"
                      "tdm:entrees_structurees(numero+page|points_de_conduite+page)|intitule_autonome_visible"
                      "=>continuation_si_entrees_structurees;fragments_meme_bas_geometrique_et_colonne"
                      "=>gauche_droite;"
                      "sans_rearmement_apres_corps;"
                      "table:reste_atomique&source_field=tdm|preliminaire_si_non_citable;"
                      "preliminaire:avant_tdm_ou_premier_article=>autre;apres_tdm=>contenu_citable;"
                      "terminal:page_physiquement_blanche&queue_sans_article=>preliminaire_racine;"
                      "symbole:famille_EuroMono&span_un_glyphe=>euro;"
                      "mixte:union_aire_images/page>=mixed_page_image_density;numero_para+ligne_minuscule=>meme_para;"
                      "dedent:dernier_frere_reel+item_num_compact+alignement_corps_parent=>parent;"
                      "continues:page_suivante&meme_kind(para|list)&sans_numero&prec!~[.;:]$;"
                      "toc:get_toc()=>titres manquants+tdm_pdf_ecart;"
                      "colonnes:gouttiere>=column_gutter_min_pt"
                      "&cotes>=column_min_lines(lignes+rangees_de_table)"
                      "&hauteur>=column_min_span_ratio=>lecture(bande,colonne,y0,x0);"
                      "colonnes_serrees:blocs_source_disjoints>=column_min_lines_par_cote"
                      "&departs_separes>=column_gutter_min_pt&remplissage>=column_min_fill_ratio"
                      "=>gouttiere;bloc_source_partage=>gouttiere_ecartee;"
                      "rangee(lignes_seules):appariement>column_row_pairing_max_ratio"
                      "&remplissage<column_min_fill_ratio=>gouttiere_ecartee;"
                      "sans_gouttiere=>ordre_lecture=ordre_source;traversante=>colonne0+bande;"
                      "continuation_et_tri_rompus_au_changement_de_colonne_ou_de_bande;"
                      "structure:proposition_verifiee(line_uid)&couverture_totale"
                      "=>noeuds positionnels+titres du registre;"
                      "refus=>quarantaine")
FLAGS = {"sort": True, "wingdings_bullet": "•", "drop_tab_glyph": True, "rstrip_lines": True,
         "lstrip_lines_sans_puce": True, "merge_number_sep": " ",
         "ligatures": "decomposees (TEXT_PRESERVE_LIGATURES absent)", "dehyphenate": False}

_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)\s*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PAGE_NUMBER_RE = re.compile(r"^\d{1,3}$")
_TOC_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$")
_PRINTED_TOC_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(.+?)(?:\s+\.{2,}\s*|\s+)(\d{1,3})\s*$")
_TOC_LEADER_RE = re.compile(r"^\s*(\S.*?)\s*\.{2,}\s*\d{1,3}\s*$")
_BULLET_FONTS = ("Wingdings",)
_TERMINAL = (".", ";", ":")
logger = logging.getLogger(__name__)


def ingest_fingerprint(structure: StructureProposee | None = None) -> str:
    """Empreinte des règles **et** de la proposition de structure effectivement appliquée (AD-2).

    Le loader ne dispose que de `source_hash` et de cette empreinte pour décider que deux artefacts
    portent les mêmes identifiants. Un `structure.json` déposé à côté du même PDF change tous les
    `node_id` : il doit donc changer l'empreinte, exactement comme un seuil géométrique le fait.
    Son absence est un état nommé, pas un trou — sans quoi « aucune proposition » et « une
    proposition qui ne structure rien » se confondraient.

    Les règles de vérification et leurs bornes n'entrent dans l'empreinte que dans la branche où une
    proposition est **effectivement appliquée**. Elles peuvent y faire basculer accepté/refusé, donc
    changer l'arbre servi : c'est le critère. Un document sans `structure.json` n'est en revanche
    concerné par aucune d'elles, et les y faire entrer quand même périmerait ses artefacts pour un
    réglage qui ne décide rien chez lui — une invalidation abusive, que le loader paierait en
    quarantaine. Restent dehors dans les deux cas `structure_max_output_tokens` et
    `structure_max_cost_eur` : ils bornent la fabrication hors ligne de l'artefact, jamais son
    acceptation. `structure_max_input_chars`, lui, y entre : il borne aussi la taille de l'artefact
    relu du disque, donc son admission.
    """
    s = get_settings()
    regles: dict[str, Any] = {"structure": empreinte_proposition(structure)}
    if structure is not None:
        regles["structure_rules"] = STRUCTURE_RULES_VERSION
        regles["structure_thresholds"] = {"structure_max_depth": s.structure_max_depth,
                                          "structure_min_coverage": s.structure_min_coverage,
                                          "structure_max_nodes": s.structure_max_nodes,
                                          "structure_max_children": s.structure_max_children,
                                          "structure_max_input_chars": s.structure_max_input_chars}
    payload = json.dumps({"parser": PARSER_VERSION, "schema": SCHEMA_VERSION, "segmentation": SEGMENTATION_RULES,
                          "flags": FLAGS, "normalize_version": normalize_version, **regles,
                          "thresholds": {"header_band_pt": s.header_band_pt, "footer_band_pt": s.footer_band_pt,
                                         "header_min_pages_ratio": s.header_min_pages_ratio,
                                         "para_gap_ratio": s.para_gap_ratio,
                                         "article_number_max_x": s.article_number_max_x,
                                         "title_min_size_pt": s.title_min_size_pt,
                                         "header_caps_max_size_pt": s.header_caps_max_size_pt,
                                         "baseline_tolerance_pt": s.baseline_tolerance_pt,
                                         "number_gap_tolerance_pt": s.number_gap_tolerance_pt,
                                         "list_indent_pt": s.list_indent_pt,
                                         "dedent_tolerance_pt": s.dedent_tolerance_pt,
                                         "dedent_starter_max_lines": s.dedent_starter_max_lines,
                                         "mixed_page_image_density": s.mixed_page_image_density,
                                         "ocr_dpi": s.ocr_dpi,
                                         "quality_min_words": s.quality_min_words,
                                         "foreign_signal_min": s.foreign_signal_min,
                                         "french_signal_ratio_min": s.french_signal_ratio_min,
                                         "gibberish_ratio_max": s.gibberish_ratio_max,
                                         "residual_header_min_pages_ratio": s.residual_header_min_pages_ratio,
                                         "coverage_threshold": s.coverage_threshold,
                                         "toc_page_number_baseline_pt": s.toc_page_number_baseline_pt,
                                         "toc_column_tolerance_pt": s.toc_column_tolerance_pt,
                                         "toc_indent_tolerance_pt": s.toc_indent_tolerance_pt,
                                         "toc_line_gap_ratio": s.toc_line_gap_ratio,
                                         # story 4.2c : la géométrie des colonnes change l'ordre de
                                         # lecture, donc `seq`, donc les `block_id` (AD-2).
                                         "column_gutter_min_pt": s.column_gutter_min_pt,
                                         "column_min_lines": s.column_min_lines,
                                         "column_min_span_ratio": s.column_min_span_ratio,
                                         "column_row_pairing_max_ratio": s.column_row_pairing_max_ratio,
                                         "column_min_fill_ratio": s.column_min_fill_ratio,
                                         # story 1.3 (revue P1) : le niveau de coupe du sommaire change summary.md
                                         "summary_max_level": s.summary_max_level}},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identite_bloc_pour_typage(block: Block) -> str:
    """Identité source hors ``block_id`` : texte normalisé, provenance et géométrie exactes."""
    payload = {
        "text_norm": normalize(block.text),
        "provenance": {
            "lang": block.lang,
            "loc": block.loc,
            "page": block.page,
            "source_field": block.source_field,
        },
        "bbox": block.bbox,
        # ``line_id`` contient le ``block_id`` et ne peut donc pas participer à une preuve qui
        # doit survivre à un simple changement de rang. Le texte brut et la boîte de chaque
        # ligne restent, eux, une provenance plus forte que le seul texte agrégé.
        "lines": [{"text": line.text, "bbox": line.bbox} for line in block.lines],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _correspondance_bijective_blocs(
        doc: Document, previous: Document,
) -> tuple[dict[str, str], dict[str, str]]:
    """Rend les correspondances ancien→nouveau et nouveau→ancien prouvées sans ambiguïté."""
    anciens: dict[str, list[str]] = {}
    nouveaux: dict[str, list[str]] = {}
    for block in previous.blocks:
        anciens.setdefault(_identite_bloc_pour_typage(block), []).append(block.block_id)
    for block in doc.blocks:
        nouveaux.setdefault(_identite_bloc_pour_typage(block), []).append(block.block_id)
    old_to_new: dict[str, str] = {}
    new_to_old: dict[str, str] = {}
    for identity, old_ids in anciens.items():
        new_ids = nouveaux.get(identity, [])
        # Tout doublon est un conflit de correspondance : le ``block_id`` ne sert jamais à le
        # départager. Les splits, merges et disparitions n'ont, eux, pas de paire 1→1.
        if len(old_ids) != 1 or len(new_ids) != 1:
            continue
        old_to_new[old_ids[0]] = new_ids[0]
        new_to_old[new_ids[0]] = old_ids[0]
    return old_to_new, new_to_old


def _signatures_noeuds(doc: Document) -> dict[str, tuple[str, int, str | None]]:
    """Identité structurelle minimale d'un nœud, hors blocs et portée sémantique."""
    parent = {child: node.node_id for node in doc.nodes for child in node.children}
    return {
        node.node_id: (node.title, node.level, parent.get(node.node_id))
        for node in doc.nodes
    }


def _parents_noeuds(doc: Document) -> dict[str, str]:
    return {child: node.node_id for node in doc.nodes for child in node.children}


def _compatibilite_recursive_noeuds(
        doc: Document, previous: Document, old_to_new: dict[str, str],
) -> dict[str, bool]:
    """Prouve ensemble la chaîne d'ancêtres et le sous-arbre de chaque nœud."""
    precedents = {node.node_id: node for node in previous.nodes}
    courants = {node.node_id: node for node in doc.nodes}
    signatures_precedentes = _signatures_noeuds(previous)
    signatures_courantes = _signatures_noeuds(doc)
    parents_precedents = _parents_noeuds(previous)
    parents_courants = _parents_noeuds(doc)
    memo: dict[str, bool] = {}
    memo_ancetres: dict[str, bool] = {}

    def ancetres_identiques(node_id: str) -> bool:
        if node_id in memo_ancetres:
            return memo_ancetres[node_id]
        parent_precedent = parents_precedents.get(node_id)
        parent_courant = parents_courants.get(node_id)
        if parent_precedent != parent_courant:
            memo_ancetres[node_id] = False
            return False
        if parent_precedent is None:
            memo_ancetres[node_id] = True
            return True
        memo_ancetres[node_id] = False  # garde de cycle pour un document invalide
        compatible = (
            signatures_precedentes.get(parent_precedent)
            == signatures_courantes.get(parent_courant)
            and ancetres_identiques(parent_precedent)
        )
        memo_ancetres[node_id] = compatible
        return compatible

    def identique(node_id: str) -> bool:
        if node_id in memo:
            return memo[node_id]
        precedent = precedents.get(node_id)
        courant = courants.get(node_id)
        if (precedent is None or courant is None
                or signatures_precedentes.get(node_id) != signatures_courantes.get(node_id)
                or not ancetres_identiques(node_id)
                or len(precedent.items) != len(courant.items)):
            memo[node_id] = False
            return False
        memo[node_id] = False  # garde de cycle pour un document invalide
        for ancien, nouveau in zip(precedent.items, courant.items, strict=True):
            if isinstance(ancien, BlockRef) and isinstance(nouveau, BlockRef):
                if old_to_new.get(ancien.block_id) != nouveau.block_id:
                    return False
            elif isinstance(ancien, NodeRef) and isinstance(nouveau, NodeRef):
                if ancien.node_id != nouveau.node_id or not identique(ancien.node_id):
                    return False
            else:
                return False
        memo[node_id] = True
        return True

    for node_id in precedents.keys() | courants.keys():
        identique(node_id)
    return memo


def _cibles_semantiques(block: Block) -> list[str]:
    return [
        *block.refs,
        *([block.overrides] if block.overrides else []),
        *(target for target in block.relation.model_dump().values() if target is not None),
    ]


def _remapper_cible(target: str | None, old_to_new: dict[str, str]) -> str | None:
    return old_to_new[target] if target is not None else None


def _reutiliser_portees_noeuds(
        doc: Document, previous: Document, old_to_new: dict[str, str],
) -> list[Node]:
    """Transporte une portée si le sous-arbre et sa chaîne porteuse sont compatibles."""
    precedents = {node.node_id: node for node in previous.nodes}
    compatibles = _compatibilite_recursive_noeuds(doc, previous, old_to_new)
    memo_bloc_reutilise: dict[str, bool] = {}

    def contient_bloc_reutilise(node_id: str) -> bool:
        if node_id in memo_bloc_reutilise:
            return memo_bloc_reutilise[node_id]
        node = precedents.get(node_id)
        if node is None:
            return False
        memo_bloc_reutilise[node_id] = False  # garde de cycle pour un document invalide
        present = any(
            (isinstance(item, BlockRef) and item.block_id in old_to_new
             and previous.block(item.block_id).kind_source is not None)
            or (isinstance(item, NodeRef) and contient_bloc_reutilise(item.node_id))
            for item in node.items
        )
        memo_bloc_reutilise[node_id] = present
        return present

    return [
        node.model_copy(update={"scope": precedents[node.node_id].scope}, deep=True)
        if compatibles.get(node.node_id, False) and contient_bloc_reutilise(node.node_id) else node
        for node in doc.nodes
    ]


def _ids_typage_precedemment_prouves(previous: Document, report_bytes: bytes | None) -> set[str]:
    """IDs dont le rapport précédent prouve qu'ils ont traversé le typage complet."""
    if report_bytes is None:
        return set()
    try:
        report = Report.model_validate_json(report_bytes)
    except (ValidationError, ValueError, UnicodeDecodeError):
        return set()
    if report.doc_id != previous.doc_id:
        return set()
    if any(check.name == "typage_clauses" and check.level == "info" for check in report.checks):
        return {block.block_id for block in previous.blocks if is_citable(block)}
    raw = report.stats.get(TYPING_REUSED_IDS_STAT)
    if not isinstance(raw, dict) or any(
            not isinstance(key, str) or isinstance(value, bool) or value != 1
            for key, value in raw.items()
    ):
        return set()
    by_id = {block.block_id: block for block in previous.blocks}
    return set(raw) & set(by_id)


def reutiliser_typage_identique(
        doc: Document, previous: Document | None, report_bytes: bytes | None,
) -> tuple[Document, dict[str, int]]:
    """Recopie un typage sur une bijection source exacte, jamais sur le seul ``block_id``."""
    if previous is None:
        return doc, {}
    proven = _ids_typage_precedemment_prouves(previous, report_bytes)
    if (not proven or doc.doc_id != previous.doc_id or doc.source_hash != previous.source_hash
            or doc.kind != previous.kind):
        return doc, {}
    old_to_new, new_to_old = _correspondance_bijective_blocs(doc, previous)
    noeuds_compatibles = _compatibilite_recursive_noeuds(doc, previous, old_to_new)
    reused: dict[str, int] = {}
    blocks: list[Block] = []
    for block in doc.blocks:
        old_id = new_to_old.get(block.block_id)
        old = previous.block(old_id) if old_id is not None else None
        if old is None or old_id not in proven or not is_citable(block):
            blocks.append(block)
            continue
        old_owner = previous.node_of(old.block_id)
        current_owner = doc.node_of(block.block_id)
        semantic_nodes = [*old.scope_node_ids, *([old.scope_node_id] if old.scope_node_id else [])]
        semantic_blocks = _cibles_semantiques(old)
        continuation = old_to_new.get(old.continues) if old.continues is not None else None
        if (old_owner != current_owner
                or not noeuds_compatibles.get(old_owner, False)
                or any(not noeuds_compatibles.get(node_id, False) for node_id in semantic_nodes)
                or any(target not in old_to_new for target in semantic_blocks)
                or (old.continues is not None and old.continues not in old_to_new)
                or continuation != block.continues):
            blocks.append(block)
            continue
        blocks.append(block.model_copy(update={
            "kind": old.kind,
            "structural_kind": block.structural_kind or block.kind,
            "kind_confidence": old.kind_confidence,
            "kind_source": old.kind_source,
            "refs": [old_to_new[target] for target in old.refs],
            "unresolved_refs": list(old.unresolved_refs),
            "defines": old.defines,
            "scope_node_id": old.scope_node_id,
            "scope_node_ids": list(old.scope_node_ids),
            "overrides": _remapper_cible(old.overrides, old_to_new),
            "relation": old.relation.model_copy(update={
                name: _remapper_cible(target, old_to_new)
                for name, target in old.relation.model_dump().items()
            }, deep=True),
        }, deep=True))
        reused[block.block_id] = 1
    # Une correspondance source ne prouve pas que le bloc a traversé le typage. Une portée de
    # nœud ne peut donc s'appuyer que sur les paires effectivement réutilisées ci-dessus.
    old_to_reused_new = {
        old_id: new_id for old_id, new_id in old_to_new.items() if new_id in reused
    }
    nodes = _reutiliser_portees_noeuds(doc, previous, old_to_reused_new)
    typed = doc.model_copy(update={"blocks": blocks, "nodes": nodes}, deep=True)
    return Document.model_validate(typed.model_dump()), reused


@dataclass(frozen=True)
class SourceLine:
    """Ligne source figée (story 4.2c) : l'ancre adressable de l'ingestion, avant toute mutation.

    `uid` nomme la ligne **extraite** — page et rang d'extraction — et ne dérive d'aucun `block_id` :
    une proposition de structure ne peut donc pas s'ancrer sur le découpage qu'elle prétend décider.
    Le registre reste **interne à l'ingestion** : il ne traverse ni `Document`, ni `document.json`,
    ni le loader (aucune migration, aucun amendement AD-2 — `Line` est inchangée).
    """

    uid: str
    text: str
    page: int
    bbox: tuple[float, float, float, float]
    ordre: int  # rang d'extraction sur la page, 1-indexé


@dataclass
class SourceRegistry:
    """Registre d'une page : ses lignes source et le motif de retrait de celles qu'aucun bloc ne porte."""

    lines: list[SourceLine] = field(default_factory=list)
    # uid → motif de retrait, réservé au contenu **réellement non servi** : `bande_recurrente`, et
    # `table_sans_bloc` pour une table détectée qui ne rend finalement aucune rangée.
    removed: dict[str, str] = field(default_factory=dict)

    def add(self, *, page: int, text: str, bbox: list[float]) -> SourceLine:
        line = SourceLine(uid=f"p{page}:l{len(self.lines) + 1}", text=text, page=page,
                          bbox=(bbox[0], bbox[1], bbox[2], bbox[3]), ordre=len(self.lines) + 1)
        self.lines.append(line)
        return line

    def retirer(self, uids: list[str], motif: str) -> None:
        for uid in uids:
            self.removed.setdefault(uid, motif)


@dataclass
class PageLayout:
    """Colonnes retenues sur une page, par la seule géométrie (story 4.2c).

    `boundaries` vide = une seule colonne : l'ordre de lecture reste alors l'ordre d'extraction.
    """

    boundaries: list[float] = field(default_factory=list)
    band_starts: list[tuple[float, int]] = field(default_factory=list)  # (y0 d'ouverture, bande)

    @property
    def multi(self) -> bool:
        return bool(self.boundaries)

    def colonne(self, bbox: list[float] | tuple[float, ...]) -> int:
        """0 = pleine largeur (la boîte traverse une gouttière) ; sinon le rang de la colonne, 1-indexé."""
        if any(bbox[0] < boundary < bbox[2] for boundary in self.boundaries):
            return 0
        return 1 + sum(1 for boundary in self.boundaries if bbox[0] >= boundary)

    def bande(self, y0: float) -> int:
        band = 0
        for start, value in self.band_starts:
            if y0 >= start:
                band = value
        return band


@dataclass
class PageLine:
    text: str
    bbox: list[float]
    size: float  # taille de police dominante
    bullet: bool = False
    number: str | None = None  # numéro d'article si la ligne en commence par un (fusionné)
    # Story 4.2c. `source_uids` porte les lignes du registre dont cette ligne de travail est faite
    # (deux après `_merge_number_lines`) ; `colonne`/`bande`/`ordre_lecture` sont posés par
    # `_assign_reading_order` et valent respectivement 1 / 0 / l'ordre source sur une page mono-colonne.
    source_uids: list[str] = field(default_factory=list)
    # Les blocs texte natifs de PyMuPDF sont une preuve géométrique distincte des seules boîtes de
    # lignes. Ils ne sortent jamais dans `document.json` : ils servent uniquement à départager une
    # vraie colonne serrée d'un retrait ou d'une puce appartenant au même paragraphe source.
    source_blocks: list[str] = field(default_factory=list)
    colonne: int = 1
    bande: int = 0
    ordre_lecture: int = 0


@dataclass
class PageText:
    page: int  # 1-indexée
    width: float
    height: float
    lines: list[PageLine] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # en-têtes/pieds retirés
    images: int = 0
    drawings: int = 0  # tracés vectoriels : une page sans texte mais dessinée n'est pas blanche (AD-8)
    image_area_ratio: float = 0.0
    tables: list[PageTable] = field(default_factory=list)
    is_toc: bool = False
    after_toc: bool = False
    surface_class: str = "substantiel"
    # Fragments natifs conservés pour l'audit de la TdM ; ``lines`` porte leur assemblage visuel.
    toc_fragments: list[PageLine] = field(default_factory=list)
    # `None` garde les PageText synthétiques rétrocompatibles ; l'extracteur pose explicitement
    # si du contenu natif exploitable subsiste après retrait des bandes récurrentes.
    native_text: bool | None = None
    ocr_attempted: bool = False
    ocr_succeeded: bool = False
    ocr_error: str | None = None
    language: str = "fr"
    # Story 4.2c : registre de lignes source figé par l'extracteur, et colonnes retenues sur la page.
    # Un `PageText` synthétique les laisse vides — l'invariant de couverture est alors vide, pas faux.
    source: SourceRegistry = field(default_factory=SourceRegistry)
    layout: PageLayout = field(default_factory=PageLayout)

    @property
    def visual(self) -> bool:
        return bool(self.images or self.drawings)


@dataclass
class PageTable:
    """Table détectée sur une page, conservée comme une unité dans l'ordre visuel.

    `source_uids` (story 4.2c) porte les lignes brutes que la table a absorbées : leur contenu est
    **servi**, par le bloc `table` reconstruit depuis `rows`, et la bijection lignes/blocs exige donc
    qu'elles y soient rattachées — jamais comptées « retirées » pendant que leur texte est publié.
    """

    bbox: list[float]
    rows: list[list[str]]
    row_bboxes: list[list[float]] = field(default_factory=list)
    cell_bboxes: list[list[list[float] | None]] = field(default_factory=list)
    source_uids: list[str] = field(default_factory=list)
    source_lines: list[PageLine] = field(default_factory=list)

    @property
    def sert_un_bloc(self) -> bool:
        """Une table sans rangée ne produit aucun bloc : son contenu n'est alors pas servi."""
        return bool(self.rows)


def _round(b: Any) -> list[float]:
    return [round(float(v), 2) for v in b]


def _center_inside(bbox: list[float], container: list[float]) -> bool:
    """Une ligne appartient à une table lorsque le centre de sa boîte est dans celle de la table.

    Une légende ou une note qui ne fait que frôler la boîte reste donc disponible, tandis que le
    texte atomique des cellules n'est pas dupliqué dans les paragraphes.
    """
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    return container[0] <= center_x <= container[2] and container[1] <= center_y <= container[3]


_UNICODE_FONT_ROLES = {"euromono": "€"}


def _decode_symbol_span(text: str, font: str) -> str:
    """Décode un glyphe seulement si la famille de police annonce un rôle Unicode univoque.

    Une famille monofonction explicite comme ``EuroMono`` et un span d'un seul glyphe fournissent
    ensemble la preuve. Une famille textuelle comme ``Eurostile`` ou un span de plusieurs caractères
    restent inchangés : leur seule apparence ne permet pas d'inventer un symbole.
    """
    family = re.split(r"[-_,+]", font.casefold(), maxsplit=1)[0]
    font_role = re.sub(r"[^a-z0-9]+", "", family)
    symbol = _UNICODE_FONT_ROLES.get(font_role)
    visible = [character for character in text if not character.isspace()]
    if symbol is None or len(visible) != 1:
        return text
    return "".join(character if character.isspace() else symbol for character in text)


def _rebuild_table_rows(table: PageTable) -> list[PageLine]:
    """Reconstruit les cellules et rend les lignes que leur géométrie ne prouve pas tabulaires."""
    if not table.cell_bboxes:
        return []
    rebuilt: list[list[str]] = []
    used: set[int] = set()
    for row_index, cells in enumerate(table.cell_bboxes):
        original = table.rows[row_index] if row_index < len(table.rows) else []
        rebuilt_row: list[str] = []
        for cell_index, cell_bbox in enumerate(cells):
            fallback = original[cell_index] if cell_index < len(original) else ""
            if cell_bbox is None:
                rebuilt_row.append(fallback)
                continue
            candidates = [
                (index, line) for index, line in enumerate(table.source_lines)
                if index not in used and _center_inside(line.bbox, cell_bbox)
            ]
            candidates.sort(key=lambda item: (item[1].bbox[1], item[1].bbox[0], item[0]))
            if candidates:
                used.update(index for index, _line in candidates)
                rebuilt_row.append("\n".join(line.text for _index, line in candidates))
            else:
                # Le moteur de table reste un repli structurel lorsqu'aucune ligne source ne tombe
                # dans la cellule. ``None`` a déjà été canonisé en chaîne vide dans ``_tables``.
                rebuilt_row.append(fallback)
        rebuilt.append(rebuilt_row)
    table.rows = rebuilt
    retained = [line for index, line in enumerate(table.source_lines) if index in used]
    leftovers = [line for index, line in enumerate(table.source_lines) if index not in used]
    table.source_lines = retained
    table.source_uids = [uid for line in retained for uid in line.source_uids]
    if not retained and not any(cell.strip() for row in rebuilt for cell in row):
        table.rows = []
    return leftovers


def _raw_lines(page: pymupdf.Page, *, page_no: int = 1, textpage: Any | None = None,
               tables: list[PageTable] | None = None,
               registry: SourceRegistry | None = None) -> tuple[list[PageLine], int]:
    """Lignes brutes (texte nettoyé, bbox, taille dominante, puce) et nombre d'images.

    Seul point d'extraction du parseur, donc **le seul endroit où le registre peut être figé** :
    chaque ligne visuelle retenue y reçoit son `SourceLine` avant toute fusion, tout retrait de bande
    et toute absorption par une table. Une ligne dont le centre tombe dans une table sort du flux des
    paragraphes — son texte serait sinon publié deux fois — mais elle est **confiée à cette table**,
    qui la portera dans son propre bloc : son contenu est servi, elle n'est donc jamais « retirée ».
    Les uid sont réattribués à chaque passe (une page ré-extraite en OCR refait son registre).
    """
    out: list[PageLine] = []
    images = 0
    for table in tables or ():
        table.source_uids = []
        table.source_lines = []
    options: dict[str, Any] = {"sort": FLAGS["sort"]}
    if textpage is not None:
        options["textpage"] = textpage
    for block_index, b in enumerate(page.get_text("dict", **options)["blocks"]):
        if b["type"] != 0:
            images += 1
            continue
        for line in b["lines"]:
            parts: list[str] = []
            sizes: dict[float, float] = {}
            bullet = False
            for s in line["spans"]:
                t = _decode_symbol_span(s["text"], s["font"])
                if any(f in s["font"] for f in _BULLET_FONTS):
                    if not parts and t.strip():
                        bullet = True
                    t = FLAGS["wingdings_bullet"] if t.strip() else t
                if FLAGS["drop_tab_glyph"]:
                    t = t.replace("\x07", "")
                parts.append(t)
                sizes[round(s["size"], 1)] = sizes.get(round(s["size"], 1), 0) + len(t)
            text = "".join(parts)
            bullet = bullet or text.lstrip(" \t").startswith(FLAGS["wingdings_bullet"])  # puce typographique
            text = text.rstrip() if FLAGS["rstrip_lines"] else text
            text = text.lstrip(" \t") if FLAGS["lstrip_lines_sans_puce"] and not bullet else text
            if not text.strip():
                continue
            size = max(sizes, key=lambda k: sizes[k]) if sizes else 0.0
            bbox = _round(line["bbox"])
            source = registry.add(page=page_no, text=text, bbox=bbox) if registry is not None else None
            page_line = PageLine(text=text, bbox=bbox, size=size, bullet=bullet,
                                 source_uids=[source.uid] if source is not None else [],
                                 source_blocks=[f"p{page_no}:b{block_index}"],
                                 ordre_lecture=(source.ordre if source is not None else
                                                len(out) + sum(len(candidate.source_lines)
                                                               for candidate in tables or ()) + 1))
            # La **première** table qui contient le centre l'absorbe : deux boîtes qui se recouvrent
            # ne peuvent donc pas porter la même ligne deux fois.
            absorbante = next((table for table in tables or ()
                               if _center_inside(bbox, table.bbox)), None)
            if absorbante is not None:
                if source is not None:
                    absorbante.source_uids.append(source.uid)
                absorbante.source_lines.append(page_line)
                continue
            out.append(page_line)
    for table in tables or ():
        out.extend(_rebuild_table_rows(table))
    out.sort(key=lambda line: line.ordre_lecture)
    return out, images


def _tables(page: pymupdf.Page) -> list[PageTable]:
    """Tables PyMuPDF, cellules vides comprises. Une détection absente n'altère pas la page."""
    try:
        found = page.find_tables()
    except (AttributeError, RuntimeError, ValueError):
        return []
    tables: list[PageTable] = []
    for table in found.tables:
        native_rows = list(getattr(table, "rows", []))
        cell_bboxes = [[_round(cell) if cell is not None else None for cell in row.cells]
                       for row in native_rows]
        if not cell_bboxes:
            continue
        extracted = list(table.extract() or [])
        rows: list[list[str]] = []
        for index, cells in enumerate(cell_bboxes):
            extracted_row = extracted[index] if index < len(extracted) else []
            rows.append([
                "" if cell >= len(extracted_row) or extracted_row[cell] is None
                else str(extracted_row[cell]).replace("\x07", "").strip()
                for cell in range(len(cells))
            ])
        row_bboxes = [_round(row.bbox) for row in native_rows]
        tables.append(PageTable(bbox=_round(table.bbox), rows=rows, row_bboxes=row_bboxes,
                                cell_bboxes=cell_bboxes))
    return sorted(tables, key=lambda table: (table.bbox[1], table.bbox[0]))


def _rectangles_union_area(rectangles: list[pymupdf.Rect]) -> float:
    """Aire exacte de l'union de rectangles parallèles aux axes, sans compter deux fois les recouvrements."""
    xs = sorted({float(x) for rect in rectangles for x in (rect.x0, rect.x1)})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        if right <= left:
            continue
        intervals = sorted((float(rect.y0), float(rect.y1)) for rect in rectangles
                           if rect.x0 < right and rect.x1 > left)
        if not intervals:
            continue
        bottom, top = intervals[0]
        height = 0.0
        for start, end in intervals[1:]:
            if start <= top:
                top = max(top, end)
            else:
                height += top - bottom
                bottom, top = start, end
        height += top - bottom
        area += (right - left) * height
    return area


def _image_area_ratio(page: pymupdf.Page) -> float:
    """Densité visuelle de l'union des images raster, bornée à la surface de la page."""
    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    rectangles: list[pymupdf.Rect] = []
    try:
        infos = page.get_image_info()
    except (AttributeError, RuntimeError, ValueError):
        infos = []
    for info in infos:
        bbox = info.get("bbox")
        if not bbox:
            continue
        rect = pymupdf.Rect(bbox) & page.rect
        if not rect.is_empty:
            rectangles.append(rect)
    return min(_rectangles_union_area(rectangles) / page_area, 1.0)


def _is_toc_title(text: str) -> bool:
    """Reconnaît un intitulé de TdM autonome, pas une mention dans une phrase de contenu."""
    value = normalize(text).strip()
    # Les préfixes exigent un séparateur et les suffixes une ponctuation : une phrase telle que
    # « consultez la table des matières pour… » ne peut donc pas être prise pour un titre.
    return bool(re.fullmatch(
        r"(?:[^.\n]+?\s[-:]\s*)?table des matieres"
        r"(?:\s*[.:;!?])*(?:\s*[-:]\s*[^.\n]+|\s*\([^()\n]+\))?",
        value,
    ))


def _has_toc_title(page: PageText) -> bool:
    return any(_is_toc_title(line.text) for line in page.lines) or any(
        _is_toc_title(cell) for table in page.tables for row in table.rows for cell in row if cell
    )


def _has_toc_entries(page: PageText) -> bool:
    """Vrai si la page porte une entrée avec numéro en colonne ou points de conduite vers une page."""
    settings = get_settings()
    texts = [line.text for line in page.lines]
    texts.extend(" ".join(cell.strip() for cell in row if cell.strip())
                 for table in page.tables for row in table.rows)
    if any(_PRINTED_TOC_RE.match(text.strip()) or _TOC_LEADER_RE.match(text.strip()) for text in texts):
        return True
    for line in page.lines:
        if _TOC_NUMBER_RE.match(line.text.strip()) and any(
            _PAGE_NUMBER_RE.fullmatch(candidate.text.strip())
            and candidate.bbox[0] > line.bbox[0]
            and abs(candidate.bbox[1] - line.bbox[1]) <= settings.toc_page_number_baseline_pt
            for candidate in page.lines
        ):
            return True
    return False


def _mark_toc_pages(pages: list[PageText]) -> None:
    """Entre par la structure visible, propage tant qu'elle subsiste et ne se réarme jamais après le corps."""
    in_toc = False
    toc_finished = False
    for page in pages:
        structural = _has_toc_entries(page)
        visible_title = _has_toc_title(page)
        has_article = any(line.number is not None for line in page.lines)
        if toc_finished:
            page.is_toc = False
            page.after_toc = True
            continue
        if not in_toc and (structural or visible_title):
            in_toc = True
        if in_toc and (structural or visible_title):
            page.is_toc = True
            page.after_toc = False
            if has_article:
                in_toc = False
                toc_finished = True
        else:
            page.is_toc = False
            if in_toc:
                in_toc = False
                toc_finished = True
            page.after_toc = toc_finished


def _assemble_toc_lines(page: PageText) -> None:
    """Assemble gauche-droite les fragments d'une TdM qui partagent une ligne de base."""
    if not page.lines:
        return
    if page.toc_fragments:
        return
    page.toc_fragments = list(page.lines)
    # La géométrie des colonnes doit être connue avant toute fusion : deux entrées voisines peuvent
    # partager exactement la même ligne de base sans appartenir à la même entrée.
    _assign_reading_order(page)
    tolerance = get_settings().baseline_tolerance_pt
    remaining = sorted(enumerate(page.lines), key=lambda item: (
        item[1].bande, item[1].colonne, item[1].bbox[1], item[1].bbox[0], item[0],
    ))
    assembled: list[PageLine] = []
    while remaining:
        _anchor_index, anchor = remaining.pop(0)
        same_baseline = [anchor]
        keep: list[tuple[int, PageLine]] = []
        for index, candidate in remaining:
            # Des tailles différentes décalent le haut des boîtes alors que leur bas reste aligné
            # sur la même ligne visuelle. La colonne et la bande demeurent des barrières strictes.
            if ((candidate.bande, candidate.colonne) == (anchor.bande, anchor.colonne)
                    and abs(candidate.bbox[3] - anchor.bbox[3]) <= tolerance):
                same_baseline.append(candidate)
            else:
                keep.append((index, candidate))
        remaining = keep
        same_baseline.sort(key=lambda line: (line.bbox[0], line.bbox[1]))
        if len(same_baseline) == 1:
            assembled.append(anchor)
            continue
        texts = [" ".join(line.text.strip().split()) for line in same_baseline if line.text.strip()]
        assembled.append(PageLine(
            text=" ".join(texts),
            bbox=[min(line.bbox[0] for line in same_baseline),
                  min(line.bbox[1] for line in same_baseline),
                  max(line.bbox[2] for line in same_baseline),
                  max(line.bbox[3] for line in same_baseline)],
            size=max(line.size for line in same_baseline),
            bullet=any(line.bullet for line in same_baseline),
            number=next((line.number for line in same_baseline if line.number is not None), None),
            source_uids=[uid for line in same_baseline for uid in line.source_uids],
            source_blocks=list(dict.fromkeys(
                block for line in same_baseline for block in line.source_blocks
            )),
            colonne=anchor.colonne,
            bande=anchor.bande,
            ordre_lecture=min(line.ordre_lecture for line in same_baseline),
        ))
    page.lines = assembled


def _classify_surfaces(pages: list[PageText]) -> None:
    """Projette les classes de surface depuis la mise en page et les ancres structurelles."""
    for page in pages:
        page.surface_class = "table_des_matieres" if page.is_toc else "substantiel"

    first_anchor = next((index for index, page in enumerate(pages)
                         if page.is_toc or any(line.number is not None for line in page.lines)), None)
    if first_anchor is not None:
        for page in pages[:first_anchor]:
            page.surface_class = "preliminaire"

    # Une simple rupture de page ne suffit jamais. La queue ne sort du corps que si un blanc
    # physique la sépare d'un article déjà observé et qu'aucun nouvel article ne s'y ouvre.
    for index in range(len(pages) - 2, -1, -1):
        separator = pages[index]
        physically_blank = (separator.native_text is False and not separator.visual
                            and not separator.tables and not separator.lines)
        if not physically_blank:
            continue
        before_has_article = any(line.number is not None for page in pages[:index]
                                 for line in page.lines)
        tail = pages[index + 1:]
        tail_has_content = any(page.lines or page.tables or page.visual for page in tail)
        tail_has_article = any(line.number is not None for page in tail for line in page.lines)
        if before_has_article and tail_has_content and not tail_has_article:
            separator.surface_class = "preliminaire"
            for page in tail:
                page.surface_class = "preliminaire"
            break


def _ocr_error_category(exc: Exception) -> str:
    """Catégorie publique stable ; le message complet reste exclusivement dans les journaux serveur."""
    message = str(exc).lower()
    if any(fragment in message for fragment in ("no ocr support", "tesseract is not installed", "not installed")):
        return "moteur_absent"
    if any(fragment in message for fragment in ("traineddata", "error opening data file", "failed loading language")):
        return "langue_absente"
    return "echec_moteur"


def _merge_number_lines(lines: list[PageLine], tables: Sequence[PageTable] = ()) -> list[PageLine]:
    """« 1.12 » seul sur sa ligne + « Contenu » sur la même ligne de base ⇒ une seule `Line` « 1.12 Contenu ».

    La fusion ne franchit **jamais** une gouttière : un numéro en bas d'une colonne et une ligne de
    la colonne voisine partagent souvent une ligne de base, et les réunir fabriquait une ligne
    pleine largeur factice — donc une bande parasite, un titre faux et un bloc à cheval sur deux
    colonnes. Les frontières sont recalculées ici sur la géométrie d'**avant** fusion, la seule que
    la fusion puisse consulter (`_boites` et `_column_boundaries` sont définis plus bas ; Python les
    résout à l'appel), et sur la **même** géométrie que la lecture — tables comprises : une gouttière que
    seules les tables révèlent doit arrêter la fusion comme les autres, sans quoi la page se
    corrigerait à la lecture après s'être abîmée à la fusion.
    """
    s = get_settings()
    boites = _boites(lines, tables)
    written_height = (max(b.bbox[3] for b in boites) - min(b.bbox[1] for b in boites)) if boites else 0.0
    boundaries = _column_boundaries(boites, written_height) if boites else []
    out: list[PageLine] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        m = _NUMBER_RE.match(cur.text)
        if m and cur.bbox[0] < s.article_number_max_x:
            cur.number = m.group(1)
            cur.text = m.group(1)
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            if nxt and not nxt.bullet and abs(nxt.bbox[1] - cur.bbox[1]) <= s.baseline_tolerance_pt \
                    and nxt.bbox[0] >= cur.bbox[2] - s.number_gap_tolerance_pt \
                    and not any(cur.bbox[2] <= frontiere <= nxt.bbox[0] for frontiere in boundaries):
                cur.text = f"{cur.text}{FLAGS['merge_number_sep']}{nxt.text}"
                cur.bbox = [min(cur.bbox[0], nxt.bbox[0]), min(cur.bbox[1], nxt.bbox[1]),
                            max(cur.bbox[2], nxt.bbox[2]), max(cur.bbox[3], nxt.bbox[3])]
                cur.size = nxt.size
                # La ligne fusionnée porte **deux** uid : le registre ne perd jamais une ligne source
                # parce que le parseur en a réuni deux (story 4.2c).
                cur.source_uids = [*cur.source_uids, *nxt.source_uids]
                cur.source_blocks = list(dict.fromkeys([*cur.source_blocks, *nxt.source_blocks]))
                i += 1
        out.append(cur)
        i += 1
    return out


def _without_running_bands(page: PageText, lines: list[PageLine], band_texts: dict[str, int],
                           minimum: int, *, registry: SourceRegistry | None = None) -> list[PageLine]:
    """Retire les bandes récurrentes d'une couche native ou OCR et conserve leur diagnostic."""
    s = get_settings()
    reg = page.source if registry is None else registry
    kept: list[PageLine] = []
    for line in lines:
        in_band = line.bbox[1] < s.header_band_pt or line.bbox[3] > page.height - s.footer_band_pt
        running = band_texts.get(line.text, 0) >= minimum or _PAGE_NUMBER_RE.match(line.text) is not None \
            or (line.size <= s.header_caps_max_size_pt and line.text == line.text.upper())
        if in_band and running:
            if line.text not in page.removed:
                page.removed.append(line.text)
            reg.retirer(line.source_uids, "bande_recurrente")
            continue
        kept.append(line)
    return kept


@dataclass(frozen=True)
class _Boite:
    """Contribution géométrique d'un objet de page à la détection des gouttières (story 4.2c).

    `ligne` porte la `PageLine` d'origine quand la boîte en est une, et `None` quand elle représente
    une rangée de table : la garde « libellé … montant » a besoin de faire la différence, la
    géométrie des gouttières non.
    """

    bbox: tuple[float, float, float, float]
    ligne: PageLine | None


def _boites(lines: list[PageLine], tables: Sequence[PageTable] = ()) -> list[_Boite]:
    """Géométrie observée par la détection de colonnes : les lignes **et** les tables.

    Une table est un bloc atomique : une gouttière ne peut pas la couper — elle est soit entièrement
    d'un côté, soit traversante, donc pleine largeur et ouvrant une bande, exactement comme une ligne
    traversante. Cette atomicité est **structurelle** ici, jamais un contrôle ajouté après coup :
    chaque rangée entre à l'aplomb de la boîte de sa table (les `x` de la table, les `y` de la
    rangée), si bien qu'aucune frontière ne peut passer entre deux rangées d'une même table sans les
    traverser toutes. Les `y` sont bornés par la boîte de la table : une rangée mal publiée par
    l'extracteur ne peut donc pas dilater la hauteur écrite de la page.

    Les rangées, et non la seule boîte, parce qu'une table est du **contenu écrit** : la compter pour
    un objet unique la laissait sous `column_min_lines`, et une colonne entièrement tabulaire restait
    invisible. À défaut de `row_bboxes` — un extracteur qui ne les publie pas —, la boîte compte pour
    une : c'est la seule géométrie disponible, jamais une supposition sur les rangées.

    Sans table sur la page, la liste rendue est exactement celle des lignes : une page sans table ne
    peut donc pas changer de comportement, et l'invariant « aucune gouttière retenue ⇒ rien ne bouge »
    y reste vrai à l'octet.
    """
    boites = [_Boite(bbox=(line.bbox[0], line.bbox[1], line.bbox[2], line.bbox[3]), ligne=line)
              for line in lines]
    for table in tables:
        if not table.sert_un_bloc:
            # Une table détectée qui ne rend aucune rangée ne produit aucun bloc (`table_sans_bloc`) :
            # elle ne sert rien, donc elle ne pèse rien. La laisser voter aurait fait naître une
            # colonne d'un contenu que l'ingestion s'apprête justement à déclarer non servi.
            continue
        x0, y0, x1, y1 = table.bbox[0], table.bbox[1], table.bbox[2], table.bbox[3]
        if not table.row_bboxes:
            boites.append(_Boite(bbox=(x0, y0, x1, y1), ligne=None))
            continue
        for row in table.row_bboxes:
            haut = min(max(row[1], y0), y1)
            bas = min(max(row[3], y0), y1)
            boites.append(_Boite(bbox=(x0, haut, x1, bas), ligne=None))
    return boites


def _appariement_des_lignes_de_base(left: list[PageLine], right: list[PageLine]) -> float:
    """Part **mutuelle** des lignes qui trouvent une partenaire sur leur ligne de base de l'autre côté.

    Une rangée « libellé … montant » apparie ses deux côtés un pour un : chaque montant est posé sur
    la ligne de base de son libellé. Deux colonnes de lecture, elles, dérivent l'une de l'autre dès
    qu'un paragraphe s'achève. Le minimum des deux parts évite qu'un seul côté très peuplé dilue le
    signal. La tolérance verticale est `baseline_tolerance_pt`, qui définit déjà « même ligne de
    base » ailleurs dans le parseur : aucune notion nouvelle n'est introduite.
    """
    tolerance = get_settings().baseline_tolerance_pt

    def part(cote: list[PageLine], autre: list[PageLine]) -> float:
        apparies = sum(1 for line in cote
                       if any(abs(other.bbox[1] - line.bbox[1]) <= tolerance for other in autre))
        return apparies / len(cote) if cote else 0.0

    return min(part(left, right), part(right, left))


def _remplissage_minimal(lines: list[PageLine], left: list[PageLine], right: list[PageLine]) -> float:
    """Part de sa largeur **disponible** qu'occupe le côté le moins rempli d'une gouttière.

    La largeur disponible d'un côté va de la marge de texte de la page — mesurée sur *toutes* les
    lignes de ce niveau, traversantes comprises — jusqu'au bord **opposé** de la gouttière. La
    mesurer contre l'étendue des lignes du côté lui-même rendrait 1 par construction et ne
    distinguerait rien. Une colonne de texte remplit sa largeur utile ; une colonne de montants
    alignés à droite n'en occupe qu'une fraction, et c'est cette fraction qui la trahit.
    """
    marge_gauche = min(line.bbox[0] for line in lines)
    marge_droite = max(line.bbox[2] for line in lines)
    parts: list[float] = []
    for cote, disponible in ((left, min(line.bbox[0] for line in right) - marge_gauche),
                             (right, marge_droite - max(line.bbox[2] for line in left))):
        occupe = max(line.bbox[2] for line in cote) - min(line.bbox[0] for line in cote)
        parts.append(occupe / disponible if disponible > 0 else 1.0)
    return min(parts)


def _best_boundary(boites: list[_Boite], written_height: float) -> float | None:
    """Meilleure gouttière d'un ensemble de boîtes, ou `None` — géométrie seule, aucun cas particulier.

    Une frontière candidate `c` partage les boîtes en trois : celles **entièrement** à gauche
    (`x1 <= c`), celles entièrement à droite (`x0 >= c`) et celles qui la traversent — ces dernières
    sont pleine largeur et n'invalident pas la gouttière. La gouttière est le blanc réellement mesuré
    entre les deux colonnes, `[max(x1 des gauches), min(x0 des droites)]` ; elle n'est retenue que si
    elle est assez large, si chaque côté porte assez de boîtes et si chaque côté est assez haut. Les
    boîtes sont les lignes **et** les rangées des tables (`_boites`) : une page dominée par des
    tables se lisait sinon en rangées, faute de la moindre géométrie tabulaire dans ce parcours.

    Elle est écartée en dernier ressort par la conjonction de **deux** signaux, jamais par un seul :
    les deux côtés appariés ligne de base à ligne de base **et** un côté qui ne remplit pas la
    largeur dont il dispose — la signature d'une rangée « libellé … montant ». L'appariement seul ne
    suffirait pas : une mise en page à deux colonnes partage très souvent la même grille de lignes
    de base, et l'écarter pour cela annulerait la correction sur les documents mêmes qu'elle vise.

    Cette garde ne porte que sur les **lignes**, jamais sur les rangées de table, et ce n'est pas une
    commodité. Elle existe contre une rangée « libellé … montant » que `find_tables()` **n'a pas
    vue** : la lire comme deux colonnes séparerait chaque montant de son libellé, parce que rien
    d'autre ne les tient ensemble. Une rangée que `find_tables()` a vue est déjà un bloc atomique —
    son libellé et son montant sont les cellules d'une même ligne du même bloc `table`, qu'aucune
    gouttière ne peut disjoindre. Lui donner voix ici l'aurait fait voter contre des gouttières
    qu'elle ne risque rien à voir retenues, et la grille régulière d'un tableau, appariée par
    construction, aurait écarté les colonnes voisines. La garde reste donc armée à l'identique sur
    les lignes d'une page qui porte aussi des tables ; elle se tait quand un côté n'a aucune ligne,
    n'ayant alors aucune paire à observer.
    Elle est intérieure par construction : elle a du contenu des deux côtés, donc jamais une marge.
    """
    s = get_settings()
    if len(boites) < 2 * s.column_min_lines:
        return None
    lignes = [b.ligne for b in boites if b.ligne is not None]
    best: tuple[float, float] | None = None  # (largeur de gouttière, frontière)
    for candidate in sorted({b.bbox[0] for b in boites}):
        left = [b for b in boites if b.bbox[2] <= candidate]
        right = [b for b in boites if b.bbox[0] >= candidate]
        if len(left) < s.column_min_lines or len(right) < s.column_min_lines:
            continue
        crossing = [b for b in boites if b.bbox[0] < candidate < b.bbox[2]]
        sources_left = {
            source for b in left if b.ligne is not None for source in b.ligne.source_blocks
        }
        sources_right = {
            source for b in right if b.ligne is not None for source in b.ligne.source_blocks
        }
        sources_crossing = {
            source for b in crossing if b.ligne is not None for source in b.ligne.source_blocks
        }
        gutter = min(b.bbox[0] for b in right) - max(b.bbox[2] for b in left)
        lignes_gauche = [b.ligne for b in left if b.ligne is not None]
        lignes_droite = [b.ligne for b in right if b.ligne is not None]
        # Couper un bloc texte natif n'est un signal de faux positif que lorsque le côté ainsi
        # détaché ne remplit pas sa largeur : c'est la signature d'une puce ou d'un retrait. PyMuPDF
        # peut aussi regrouper deux vraies colonnes dans un seul bloc source ; si elles sont toutes
        # deux pleines et que le blanc physique suffit, la preuve géométrique historique prévaut.
        if sources_left & sources_right and lignes_gauche and lignes_droite:
            source_split_pairing = _appariement_des_lignes_de_base(lignes_gauche, lignes_droite)
            source_split_fill = _remplissage_minimal(lignes, lignes_gauche, lignes_droite)
            if (source_split_pairing <= s.column_row_pairing_max_ratio
                    or source_split_fill < s.column_min_fill_ratio):
                continue
        if gutter < s.column_gutter_min_pt:
            # Une gouttière visuellement serrée n'abaisse pas le seuil de blanc. Elle emprunte une
            # seconde preuve : assez de blocs source indépendants de chaque côté, aucun de ces blocs
            # dans une boîte traversante, des départs de colonnes séparés par le seuil publié et deux
            # côtés réellement remplis. Sans provenance source (PageText synthétique historique,
            # table seule), le chemin reste strictement celui du blanc physique.
            if (len(sources_left) < s.column_min_lines
                    or len(sources_right) < s.column_min_lines
                    or bool(sources_left & sources_right)
                    or sources_crossing & (sources_left | sources_right)
                    or not lignes_gauche or not lignes_droite):
                continue
            start_gap = min(line.bbox[0] for line in lignes_droite) - min(
                line.bbox[0] for line in lignes_gauche
            )
            if (start_gap < s.column_gutter_min_pt
                    or _remplissage_minimal(lignes, lignes_gauche, lignes_droite)
                    < s.column_min_fill_ratio):
                continue
        minimum_span = s.column_min_span_ratio * written_height
        if any(max(b.bbox[3] for b in side) - min(b.bbox[1] for b in side) < minimum_span
               for side in (left, right)):
            continue
        if lignes_gauche and lignes_droite \
                and _appariement_des_lignes_de_base(lignes_gauche, lignes_droite) \
                > s.column_row_pairing_max_ratio \
                and _remplissage_minimal(lignes, lignes_gauche, lignes_droite) < s.column_min_fill_ratio:
            continue
        # À partition égale, la gouttière la plus large gagne ; à largeur égale, la frontière la plus
        # à gauche — deux règles totales, donc un résultat indépendant de l'ordre d'itération.
        if best is None or (gutter, -candidate) > (best[0], -best[1]):
            best = (gutter, candidate)
    return None if best is None else best[1]


def _column_boundaries(boites: list[_Boite], written_height: float) -> list[float]:
    """Frontières retenues, triées. La récursion sur chaque côté couvre trois colonnes et plus."""
    boundary = _best_boundary(boites, written_height)
    if boundary is None:
        return []
    left = [b for b in boites if b.bbox[2] <= boundary]
    right = [b for b in boites if b.bbox[0] >= boundary]
    return sorted([*_column_boundaries(left, written_height), boundary,
                   *_column_boundaries(right, written_height)])


def _assign_reading_order(pt: PageText) -> None:
    """Pose `colonne`, `bande` et `ordre_lecture`, et réordonne la page colonne par colonne.

    **Aucune gouttière retenue ⇒ rien ne bouge** : `colonne=1`, `bande=0`, et `ordre_lecture` est
    l'ordre d'extraction, à l'identique — un document mono-colonne ne peut donc pas régresser, et
    une page qui ne porte que des tables non plus.
    Sinon la clé de lecture est `(bande, colonne, y0, x0, ordre source)` : une ligne qui traverse une
    gouttière est pleine largeur (`colonne=0`), ouvre une bande et se lit avant les colonnes qu'elle
    coiffe. Idempotent : rejouée sur la même page, la fonction rend le même ordre.

    La géométrie observée est celle des lignes **et** des tables (`_boites`) : sans elles, une page
    dominée par des tables ne portait aucune gouttière et se lisait en rangées — les tables ne
    participaient qu'au bandage, et seulement une fois qu'une gouttière avait déjà été trouvée par les
    lignes. La sortie anticipée porte donc sur les deux : une page sans ligne mais avec des tables a
    bien un `layout`, sans quoi `_segment_page` trierait ses tables sur un `layout` vide.
    """
    lines = pt.lines
    # Après assemblage d'une TdM, les fragments natifs restent l'autorité géométrique : leur
    # remplacement par les lignes élargies ferait disparaître une gouttière pourtant prouvée.
    layout_lines = pt.toc_fragments if pt.is_toc and pt.toc_fragments else lines
    boites = _boites(layout_lines, pt.tables)
    if not boites:
        pt.layout = PageLayout()
        return
    written_height = max(b.bbox[3] for b in boites) - min(b.bbox[1] for b in boites)
    boundaries = _column_boundaries(boites, written_height)
    layout = PageLayout(boundaries=boundaries)
    if not boundaries:
        pt.layout = layout
        for index, line in enumerate(lines, 1):
            line.colonne, line.bande, line.ordre_lecture = 1, 0, index
        return
    for line in lines:
        line.colonne = layout.colonne(line.bbox)
    # Les tables participent au bandage **comme les lignes** : une table qui traverse une gouttière
    # est pleine largeur et ouvre donc une bande. Sans elle dans ce parcours, une table traversante
    # gardait `colonne=0` sans jamais ouvrir de bande, et `0 < 1` dans la clé de tri la faisait
    # remonter en tête de sa bande — devant tout le texte des colonnes qu'elle suit pourtant à l'œil.
    items: list[tuple[float, float, int, PageLine | None]] = [
        (line.bbox[1], line.bbox[0], line.colonne, line) for line in lines
    ]
    items += [(table.bbox[1], table.bbox[0], layout.colonne(table.bbox), None) for table in pt.tables]
    items.sort(key=lambda item: (item[0], item[1]))
    band = 0
    previous_full = True  # un bandeau de tête n'ouvre pas de bande : il est déjà dans la première
    band_starts: list[tuple[float, int]] = []
    for y0, _x0, colonne, line in items:
        full = colonne == 0
        if full and not previous_full:
            band += 1
            band_starts.append((y0, band))
        if line is not None:
            line.bande = band
        previous_full = full
    layout.band_starts = band_starts
    pt.layout = layout
    source_rank = {id(line): index for index, line in enumerate(lines)}
    pt.lines = sorted(lines, key=lambda l: (l.bande, l.colonne, l.bbox[1], l.bbox[0], source_rank[id(l)]))
    for index, line in enumerate(pt.lines, 1):
        line.ordre_lecture = index


def extract_pages(pdf: Path | str) -> tuple[list[PageText], list[Any]]:
    """Pages (lignes + bbox + police, en-têtes/pieds retirés) et `get_toc()` du PDF."""
    s = get_settings()
    doc = pymupdf.open(str(pdf))
    pages: list[PageText] = []
    band_texts: dict[str, int] = {}
    try:
        toc = doc.get_toc()
        for pno, page in enumerate(doc, start=1):
            tables = _tables(page)
            registry = SourceRegistry()
            lines, images = _raw_lines(page, page_no=pno, tables=tables, registry=registry)
            drawings = len(page.get_drawings())
            # Relevé sans condition : une page dont les seules lignes sont un en-tête/pied retiré plus bas
            # doit rester « dessinée » et non blanche (AD-8, revue Codex 1.2 B3).
            pt = PageText(page=pno, width=page.rect.width, height=page.rect.height, lines=lines, images=images,
                          drawings=drawings, image_area_ratio=_image_area_ratio(page), tables=tables,
                          source=registry)
            for line in lines:
                if line.bbox[1] < s.header_band_pt or line.bbox[3] > pt.height - s.footer_band_pt:
                    band_texts[line.text] = band_texts.get(line.text, 0) + 1
            pages.append(pt)
        # `ceil` évite qu'une occurrence sous le ratio configuré soit promue par troncature.
        min_pages = max(2, ceil(len(pages) * s.header_min_pages_ratio))
        for index, pt in enumerate(pages):
            page = doc[index]
            native_kept = _without_running_bands(pt, pt.lines, band_texts, min_pages)
            pt.native_text = bool(native_kept or pt.tables)
            if not pt.native_text and pt.visual:
                pt.ocr_attempted = True
                try:
                    textpage = page.get_textpage_ocr(language="fra", dpi=s.ocr_dpi, full=True)
                    ocr_registry = SourceRegistry()
                    ocr_lines, _ = _raw_lines(page, page_no=pt.page, textpage=textpage,
                                              tables=pt.tables, registry=ocr_registry)
                    retained_ocr = _without_running_bands(pt, ocr_lines, band_texts, min_pages,
                                                          registry=ocr_registry)
                    pt.lines = _merge_number_lines(retained_ocr, pt.tables)
                    # Le registre servi est celui de la couche réellement retenue : la couche native
                    # de cette page n'a rien laissé passer, ses lignes n'ancreraient donc aucun bloc.
                    pt.source = ocr_registry
                    pt.ocr_succeeded = bool(pt.lines)
                    if not pt.ocr_succeeded:
                        pt.ocr_error = "sortie_vide"
                except Exception as exc:  # PyMuPDF expose FzErrorLibrary hors hiérarchie RuntimeError
                    pt.lines = []
                    category = _ocr_error_category(exc)
                    pt.ocr_error = f"ocr_exception:{type(exc).__name__}:{category}"
                    logger.exception("Échec OCR page %s (%s)", pt.page, category)
            else:
                pt.lines = _merge_number_lines(native_kept, pt.tables)
    finally:
        doc.close()
    _mark_toc_pages(pages)
    for page in pages:
        if page.is_toc:
            _assemble_toc_lines(page)
    _classify_surfaces(pages)
    return pages, toc


def anomalies_registre(pages: list[PageText], block_uids: dict[str, list[str]]) -> list[str]:
    """Union complète, intersection vide : chaque `SourceLine` est portée par un bloc **ou** retirée.

    C'est la seule preuve que le registre reste une source et non un décor : une ligne extraite qui
    n'aboutit ni dans un bloc ni dans un motif de retrait aurait disparu en silence, et une ligne
    présente dans deux blocs rendrait la même preuve citable à deux endroits.
    """
    attaches: dict[str, list[str]] = {}
    for block_id, uids in block_uids.items():
        for uid in uids:
            attaches.setdefault(uid, []).append(block_id)
    anomalies: list[str] = []
    connus: set[str] = set()
    for page in pages:
        for source in page.source.lines:
            connus.add(source.uid)
            porteurs = attaches.get(source.uid, [])
            motif = page.source.removed.get(source.uid)
            if len(porteurs) > 1:
                anomalies.append(f"{source.uid} rattachée à {len(porteurs)} blocs : {', '.join(porteurs)}")
            elif porteurs and motif is not None:
                anomalies.append(f"{source.uid} à la fois dans {porteurs[0]} et retirée ({motif})")
            elif not porteurs and motif is None:
                anomalies.append(f"{source.uid} sans bloc ni motif de retrait")
    anomalies.extend(f"{uid} porté par {', '.join(porteurs)} sans exister au registre"
                     for uid, porteurs in sorted(attaches.items()) if uid not in connus)
    return anomalies


def ordonner_pages(pages: list[PageText]) -> None:
    """Arrête l'ordre de lecture de chaque page — colonnes comprises — avant toute exploitation."""
    for pt in pages:
        _assign_reading_order(pt)


class _Builder:
    def __init__(self, doc_id: str, title: str, *, plan: dict[str, NoeudVerifie] | None = None) -> None:
        self.doc_id = doc_id
        self.plan = plan or {}  # node_id → nœud d'une proposition vérifiée (story 4.2c)
        self.root = Node(node_id=doc_id, level=0, title=title)
        self.nodes: dict[str, Node] = {doc_id: self.root}
        self.order: list[str] = [doc_id]
        self.blocks: list[Block] = []
        self.current = self.root
        self.numbers: list[str] = []  # ordre d'apparition, pour `numerotation_non_monotone`
        self.duplicates: list[str] = []
        self.continues = 0
        self.parents: dict[str, str] = {}
        self.starters: dict[str, tuple[str, list[PageLine]]] = {}
        self.body_indents: dict[str, list[float]] = {}
        self.block_uids: dict[str, list[str]] = {}  # story 4.2c : bloc → lignes source, hors `Document`

    def _line_uid(self, page: int, line: PageLine) -> str:
        """Identité v1 de la ligne portée, indépendante du ``block_id`` et de la segmentation."""
        return line_uid(document_uid=self.doc_id, page=page, source_uids=line.source_uids,
                        bbox=line.bbox, order=line.ordre_lecture, text=line.text)

    def node_propose(self, node_id: str) -> Node:
        """Instancie à la demande un nœud **déjà prouvé**, et ses ancêtres, dans l'ordre de lecture.

        La création paresseuse est ce qui garde `Node.items` fidèle à la lecture : un parent voit son
        propre texte arriver avant le `NodeRef` de l'enfant qui le suit dans la page. Le `node_id` et
        le titre viennent du plan, donc du code et du registre — jamais de la réponse du modèle.
        """
        if node_id in self.nodes:
            return self.nodes[node_id]
        spec = self.plan[node_id]
        parent = self.root if spec.parent_id is None else self.node_propose(spec.parent_id)
        node = Node(node_id=node_id, level=spec.level, title=spec.title,
                    article_uid=getattr(spec, "article_uid", None),
                    surface_class=getattr(spec, "surface_class", "substantiel"),
                    relations=list(getattr(spec, "relations", ())))
        self.nodes[node_id] = node
        self.parents[node_id] = parent.node_id
        self.order.append(node_id)
        parent.items.append(NodeRef(node_id=node_id))
        return node

    def node_for(self, numero: str) -> Node:
        node_id = f"{self.doc_id}:a{numero}"
        if node_id in self.nodes:
            self.duplicates.append(numero)
            k = 2
            while f"{node_id}-{k}" in self.nodes:
                k += 1
            node_id = f"{node_id}-{k}"
        parts = numero.split(".")
        parent = self.root
        for n in range(len(parts) - 1, 0, -1):
            cand = f"{self.doc_id}:a{'.'.join(parts[:n])}"
            if cand in self.nodes:
                parent = self.nodes[cand]
                break
        # Le parseur ne donne aucun sens juridique au numéro. `type_clauses` est l'unique écrivain
        # sémantique de `Node.scope.kind`, à partir des relations et portées explicites résolues.
        node = Node(node_id=node_id, level=len(parts), title="", article_uid=f"article:{numero}")
        self.nodes[node_id] = node
        self.parents[node_id] = parent.node_id
        self.order.append(node_id)
        parent.items.append(NodeRef(node_id=node_id))
        self.numbers.append(numero)
        return node

    def _has_later_sibling(self, node_id: str, future_numbers: list[str]) -> bool:
        """Simule les parents que le streaming donnera aux prochains numéros."""
        parent_id = self.parents.get(node_id)
        if parent_id is None:
            return False
        known = set(self.nodes)
        for number in future_numbers:
            parts = number.split(".")
            future_parent = self.root.node_id
            for depth in range(len(parts) - 1, 0, -1):
                candidate = f"{self.doc_id}:a{'.'.join(parts[:depth])}"
                if candidate in known:
                    future_parent = candidate
                    break
            if future_parent == parent_id:
                return True
            known.add(f"{self.doc_id}:a{number}")
        return False

    def dedent_closing_group(self, kind: str, lines: list[PageLine], *, tolerance_pt: float,
                             starter_max_lines: int, future_numbers: list[str]) -> None:
        """Rattache un paragraphe de clôture au parent géométriquement aligné.

        Un groupe non numéroté n'est dé-indenté que lorsqu'il suit le dernier item compact d'une
        fratrie numérotée et que son retrait coïncide avec un retrait de corps déjà observé chez
        un ancêtre. Cette combinaison arbre + géométrie évite toute convention d'article ou de
        vocabulaire juridique. Un premier corps ordinaire, une liste annoncée par deux-points ou un
        item long restent sous le nœud courant.
        """
        if kind != "para" or not lines or lines[0].number is not None or self.current is self.root:
            return
        starter = self.starters.get(self.current.node_id)
        parent_id = self.parents.get(self.current.node_id)
        if starter is None or parent_id is None:
            return
        starter_kind, starter_lines = starter
        if (starter_kind != "para" or len(starter_lines) > starter_max_lines
                or starter_lines[-1].text.rstrip().endswith(":")
                or self.body_indents.get(self.current.node_id)):
            return
        parent = self.nodes[parent_id]
        # Le nœud courant doit clore une vraie fratrie, pas seulement être son dernier enfant *déjà
        # vu*. La simulation des numéros restants emploie la même règle de parent que `node_for` et
        # empêche de déplacer le corps de 1.2 si un frère 1.3 apparaît ensuite.
        if (len(parent.children) < 2 or parent.children[-1] != self.current.node_id
                or self._has_later_sibling(self.current.node_id, future_numbers)):
            return
        x = lines[0].bbox[0]
        if any(abs(x - known) <= tolerance_pt for known in self.body_indents.get(parent_id, [])):
            self.current = parent

    def add_block(self, page: int, lines: list[PageLine], kind: str, *, continues: str | None = None,
                  source_field: str | None = None, surface_class: str | None = None) -> Block:
        loc = f"p{page}"
        seq = sum(1 for b in self.blocks if b.loc == loc) + 1
        block_id = f"{self.doc_id}:{loc}:{seq}"
        bbox = [min(l.bbox[0] for l in lines), min(l.bbox[1] for l in lines),
                max(l.bbox[2] for l in lines), max(l.bbox[3] for l in lines)]
        effective_surface = surface_class or (
            "preliminaire" if source_field == "preliminaire" else
            "table_des_matieres" if source_field == "tdm" else self.current.surface_class
        )
        block = Block(block_id=block_id, text="\n".join(l.text for l in lines), loc=loc, seq=seq, page=page,
                      bbox=bbox, kind=kind, structural_kind=kind, continues=continues,
                      article_uid=self.current.article_uid, surface_class=effective_surface,
                      source_field=source_field,  # type: ignore[arg-type]
                      lines=[Line(line_id=f"{block_id}:l{i}", line_uid=self._line_uid(page, l),
                                  text=l.text, bbox=l.bbox, page=page,
                                  order=l.ordre_lecture)
                             for i, l in enumerate(lines, 1)])
        self.blocks.append(block)
        self.block_uids[block_id] = [uid for line in lines for uid in line.source_uids]
        self.current.items.append(BlockRef(block_id=block_id))
        if lines[0].number is not None:
            self.starters.setdefault(self.current.node_id, (kind, list(lines)))
        else:
            self.body_indents.setdefault(self.current.node_id, []).append(lines[0].bbox[0])
        if continues:
            self.continues += 1
        return block


def _is_heading(line: PageLine, nxt: PageLine | None) -> bool:
    """Texte sur la ligne de base d'un numéro : titre si gros, ou court sans ponctuation finale et suivi d'une majuscule."""
    if line.size >= get_settings().title_min_size_pt:
        return True
    if line.text.rstrip().endswith((".", ";", ":", ",")):
        return False
    return nxt is None or nxt.number is not None or nxt.bullet or nxt.text[:1].isupper()


def _noeud_de_ligne(line: PageLine, node_of_uid: dict[str, str]) -> str | None:
    """Nœud proposé qui couvre cette ligne, s'il y en a un."""
    return next((node_of_uid[uid] for uid in line.source_uids if uid in node_of_uid), None)


def _segment_page(pt: PageText, node_of_uid: dict[str, str] | None = None,
                  ) -> list[tuple[str, list[PageLine]]]:
    """Découpe une page en (kind, lignes) : heading / para / list, dans l'ordre de lecture.

    Avec `node_of_uid`, un groupe ne peut pas franchir une frontière de nœud prouvé : une section
    dont le titre suit son voisin à interligne serré serait sinon collée dans le même paragraphe, le
    bloc entier partirait sous le premier nœud, et le second nœud — pourtant accepté et annoncé par
    le rapport — n'existerait dans aucun arbre.
    """
    groups: list[tuple[str, list[PageLine]]] = []
    s = get_settings()
    lines = pt.lines
    noeuds = node_of_uid or {}
    for i, line in enumerate(lines):
        prev = lines[i - 1] if i else None
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        # Story 4.2c : aucune continuation ne franchit une colonne, une bande ou un nœud prouvé. Sur
        # une page mono-colonne sans proposition, `contigu` vaut toujours vrai : rien ne change.
        contigu = prev is not None and (prev.colonne, prev.bande) == (line.colonne, line.bande) \
            and (not noeuds or _noeud_de_ligne(prev, noeuds) == _noeud_de_ligne(line, noeuds))
        if line.number is not None:
            if _is_heading(line, nxt):
                groups.append(("heading", [line]))
            else:
                groups.append(("para", [line]))
            continue
        if line.bullet:
            if groups and groups[-1][0] == "list" and contigu:
                groups[-1][1].append(line)
            else:
                groups.append(("list", [line]))
            continue
        if groups and prev is not None and contigu:
            kind, prev_lines = groups[-1]
            gap = line.bbox[1] - prev.bbox[1]
            height = max(prev.bbox[3] - prev.bbox[1], 1.0)
            close = gap <= s.para_gap_ratio * height
            if kind == "list" and close and (line.bbox[0] > prev_lines[0].bbox[0] + s.list_indent_pt
                                             or (line.text[:1].islower() and not prev.text.endswith(_TERMINAL))):
                prev_lines.append(line)  # continuation d'un item : indentée, ou alignée et phrase non terminée
                continue
            if kind == "para" and close and line.size < s.title_min_size_pt and prev.size < s.title_min_size_pt:
                prev_lines.append(line)
                continue
            if kind == "para" and prev.number is not None and len(prev_lines) == 1 and line.text[:1].islower():
                prev_lines.append(line)  # « 3.1.1.1.1 L'incendie, » puis « c'est-à-dire … » : même alinéa
                continue
        kind = "heading" if line.size >= s.title_min_size_pt and line.number is None and not line.text.endswith(".") \
            else "para"
        groups.append((kind, [line]))
    if not pt.tables:
        return groups
    # La clé de tri réunit tables et groupes dans un seul ordre de lecture. Sur une page
    # mono-colonne elle vaut `(0, 1, y, x)` pour tout le monde : le tri est celui d'avant, à
    # l'identique. Sur une page à colonnes, une table appartient à la colonne et à la bande de sa
    # propre boîte, et ne peut donc pas se glisser entre deux groupes de l'autre colonne.
    ordered: list[tuple[int, int, float, float, str, list[PageLine]]] = [
        (lines[0].bande, lines[0].colonne, lines[0].bbox[1], lines[0].bbox[0], kind, lines)
        for kind, lines in groups
    ]
    for table in pt.tables:
        if not table.sert_un_bloc:
            # Cas limite explicite : une table détectée qui ne rend aucune rangée ne produit aucun
            # bloc. Ses lignes brutes seraient alors du contenu réellement non servi — elles sont
            # inscrites au motif de retrait qui le dit, jamais perdues entre l'absorption et un bloc
            # qui n'existe pas.
            pt.source.retirer(table.source_uids, "table_sans_bloc")
            continue
        row_height = max((table.bbox[3] - table.bbox[1]) / max(len(table.rows), 1), 1.0)
        rows = []
        for index, row in enumerate(table.rows):
            bbox = table.row_bboxes[index] if index < len(table.row_bboxes) else [
                table.bbox[0], round(table.bbox[1] + index * row_height, 2), table.bbox[2],
                round(min(table.bbox[1] + (index + 1) * row_height, table.bbox[3]), 2),
            ]
            colonne = pt.layout.colonne(table.bbox)
            rows.append(PageLine(text=" | ".join(row), bbox=bbox, size=0.0,
                                 colonne=colonne, bande=pt.layout.bande(table.bbox[1]),
                                 # Une rangée synthétique n'est pas une ligne extraite : elle reçoit
                                 # ici son ordre stable propre, au lieu de laisser `add_block`
                                 # transformer silencieusement le sentinel 0 en index local via `or`.
                                 ordre_lecture=index + 1))
        # Les lignes brutes absorbées sont portées par la **première** rangée du bloc : le bloc les
        # porte donc exactement une fois, quel que soit le nombre de rangées reconstruites — les
        # rangées ne sont pas les lignes source et n'ont aucune raison de leur correspondre une à une.
        rows[0].source_uids = list(table.source_uids)
        ordered.append((pt.layout.bande(table.bbox[1]), pt.layout.colonne(table.bbox),
                        table.bbox[1], table.bbox[0], "table", rows))
    ordered.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [(kind, lines) for _, _, _, _, kind, lines in ordered]


def _noeud_de_groupe(lines: list[PageLine], node_of_uid: dict[str, str], *, page: int) -> str:
    """Le nœud prouvé d'un groupe, **prouvé ligne à ligne** — jamais hérité d'un voisin.

    La couverture d'une proposition acceptée est totale : toute ligne du registre appartient à un
    intervalle, donc tout groupe qui porte des lignes du registre a son nœud. Rattacher un groupe au
    nœud de son voisin servait au contraire une affectation que la proposition n'avait ni portée ni
    prouvée, sous l'annonce « proposition vérifiée ».

    Deux refus, parce qu'un bloc servi ne peut pas être moins prouvé que la proposition qui l'annonce :
    un groupe sans aucune ligne prouvée serait servi sans liaison ligne/bloc, et un groupe à cheval
    sur deux nœuds — une table atomique que la proposition scinde, ou une ligne fusionnée dont les
    deux uid tombent de part et d'autre d'une frontière — serait servi entier sous l'un des deux.
    """
    noeuds = {node_of_uid[uid] for line in lines for uid in line.source_uids if uid in node_of_uid}
    if len(noeuds) == 1:
        return noeuds.pop()
    apercu = " / ".join(line.text for line in lines[:3])[:200]
    if not noeuds:
        raise StructureRefusee(
            "affectation_non_prouvee",
            f"p{page} : un bloc serait servi sans aucune ligne source prouvée ({apercu!r})")
    raise StructureRefusee(
        "affectation_non_prouvee",
        f"p{page} : un bloc atomique serait servi à cheval sur {len(noeuds)} nœuds prouvés "
        f"({', '.join(sorted(noeuds))}) : {apercu!r}")


def reconcilier_affectation(nodes: dict[str, Node], block_uids: dict[str, list[str]],
                            node_of_uid: dict[str, str]) -> None:
    """Le **propriétaire effectif** de chaque uid dans l'arbre bâti est-il celui que la proposition prescrit ?

    `_noeud_de_groupe` l'impose groupe par groupe **au moment de l'ajout** ; c'est une garde du
    chemin, pas une preuve du résultat. Rien ne le revérifiait sur le document construit, si bien
    qu'un chemin qui la contournerait — un rattachement futur, un appel programmatique, une branche
    ajoutée à `build_document` — servirait une affectation divergente sous l'annonce « proposition
    vérifiée ». La réconciliation se fait donc ici sur ce qui est réellement bâti : les `BlockRef` des
    nœuds, et non la seule présence des `node_id`.

    Trois refus, tous `affectation_non_prouvee`, parce que c'est un seul défaut vu de trois côtés :
    un bloc servi sans aucune ligne source prouvée n'est comparable à rien, un bloc qu'aucun nœud ne
    réclame n'a pas de propriétaire à confronter, et un bloc dont le propriétaire diverge de
    `node_of_uid` est servi ailleurs que là où la proposition le place.
    """
    proprietaire: dict[str, str] = {}
    for node_id, node in nodes.items():
        for block_id in node.blocks:
            proprietaire[block_id] = node_id
    for block_id in sorted(block_uids):
        uids = block_uids[block_id]
        if not uids:
            raise StructureRefusee(
                "affectation_non_prouvee",
                f"{block_id} serait servi sans aucune ligne source : rien n'y est prouvé")
        node_id = proprietaire.get(block_id)
        if node_id is None:
            raise StructureRefusee(
                "affectation_non_prouvee",
                f"{block_id} n'est réclamé par aucun nœud de l'arbre bâti : son affectation "
                "n'est confrontable à rien")
        for uid in uids:
            prescrit = node_of_uid.get(uid)
            if prescrit != node_id:
                raise StructureRefusee(
                    "affectation_non_prouvee",
                    f"{block_id} est servi sous {node_id} alors que la proposition place {uid} "
                    f"sous {prescrit or 'aucun nœud'}")


def build_document(pages: list[PageText], *, edition: str, source_hash: str, toc: list[Any],
                   doc_id: str = DOC_ID, title: str = TITLE, source_url: str | None = None,
                   structure: StructureProposee | None = None) -> tuple[Document, dict[str, Any]]:
    """Arbre de blocs + méta de construction (numéros dans l'ordre, doublons, `continues`).

    Avec `structure`, l'arbre vient d'une proposition **prouvée sur le registre de lignes** : le
    vérificateur passe d'abord, un refus lève `StructureRefusee` et rien n'est construit (AD-16 :
    jamais de repli silencieux vers l'heuristique numérique). Sans `structure`, l'heuristique
    `_NUMBER_RE` reste le chemin nominal, inchangée.
    """
    # L'ordre de lecture est arrêté **avant** toute lecture des pages : la numérotation à venir,
    # les continuations et la segmentation le supposent tous déjà fixé (story 4.2c).
    _classify_surfaces(pages)
    ordonner_pages(pages)
    plan: dict[str, NoeudVerifie] = {}
    node_of_uid: dict[str, str] = {}
    if structure is not None:
        # v1 reste relisible comme artefact historique. Toute proposition nouvellement produite est
        # v2 et adresse exactement les `line_uid` content-addressés persistés dans `Document.Line`.
        registre = registre_lignes(
            pages, document_uid=doc_id if structure.schema_version == "2" else None,
        )
        verdict = verifier(structure, registre, doc_id=doc_id, settings=get_settings())
        if not verdict.accepte:
            raise StructureRefusee(verdict.motif or "refus", verdict.detail)
        plan, canonical_node_of_uid = arbre(structure, registre, doc_id)
        # Le fournisseur et l'audit parlent l'identité publique content-addressée. Le parseur garde
        # ses ancres d'extraction `pN:lN` uniquement pour rattacher les fragments bruts aux porteurs.
        node_of_uid = ({
            source_uid: canonical_node_of_uid[entree.uid]
            for entree in registre.values()
            if entree.uid in canonical_node_of_uid
            for source_uid in entree.source_uids
        } if structure.schema_version == "2" else canonical_node_of_uid)
    b = _Builder(doc_id, title, plan=plan)
    toc_node: Node | None = None
    last_text_block: Block | None = None  # dernier bloc para|list de la page précédente
    precedent_noeud: str | None = None  # nœud proposé qui le portait, pour ne pas continuer à travers
    article_seen = False
    document_has_articles = any(line.number is not None for page in pages for line in page.lines)
    document_has_toc = any(page.is_toc for page in pages)
    future_numbers = [line.number for page in pages for line in page.lines
                      if line.number is not None]

    def add_preliminary_groups(page: PageText, groups: list[tuple[str, list[PageLine]]], *,
                               table_source: str) -> None:
        """Fusionne le texte préliminaire contigu, mais jamais à travers une table atomique."""
        pending: list[PageLine] = []
        pending_position: tuple[int, int] | None = None

        def flush() -> None:
            nonlocal pending_position
            if pending:
                provenance = "ocr" if page.ocr_succeeded else table_source
                b.add_block(page.page, list(pending), "autre",
                            source_field=provenance, surface_class=page.surface_class)
                pending.clear()
                pending_position = None

        for kind, lines in groups:
            if kind == "table":
                flush()
                # Le kind reste fidèle à la source ; la non-citabilité est une provenance explicite
                # que l'index peut appliquer aux recherches, fréquences et fenêtres.
                provenance = "ocr" if page.ocr_succeeded else table_source
                b.add_block(page.page, lines, "table", source_field=provenance,
                            surface_class=page.surface_class)
            else:
                # Une page préliminaire ou une TdM peut être multicolonne elle aussi. La fusion
                # historique de tous ses groupes en un seul bloc recollait les deux côtés après
                # que l'ordre géométrique les avait correctement séparés. La position est une
                # propriété générique déjà décidée par la porte de lecture : elle rompt le bloc
                # exactement comme elle rompt les paragraphes du corps, sans vocabulaire ni page.
                position = (lines[0].bande, lines[0].colonne)
                if pending and position != pending_position:
                    flush()
                pending_position = position
                pending.extend(lines)
        flush()

    for pt in pages:
        groups = _segment_page(pt, node_of_uid)
        if structure is not None:
            # Une proposition vérifiée gouverne **tout** le rattachement. Les branches TdM et
            # « préliminaire » sont écrites pour l'heuristique numérique : elles rattachent à la
            # racine ou au nœud `:tdm` des lignes que la proposition place ailleurs, et faisaient
            # disparaître des nœuds pourtant acceptés — jusqu'à trouer le chemin positionnel servi.
            page_last = None
            for index, (kind, lines) in enumerate(groups):
                node_id = _noeud_de_groupe(lines, node_of_uid, page=pt.page)
                b.current = b.node_propose(node_id)
                continues = None
                # Scission de page (AD-2), inchangée, mais jamais à travers un nœud : deux sections
                # différentes ne se continuent pas l'une l'autre.
                if index == 0 and kind in ("para", "list") and last_text_block is not None \
                        and last_text_block.kind == kind and node_id == precedent_noeud \
                        and not last_text_block.text.rstrip().endswith(_TERMINAL):
                    continues = last_text_block.block_id
                blk = b.add_block(pt.page, lines, kind, continues=continues,
                                  source_field="ocr" if pt.ocr_succeeded else None)
                if kind in ("para", "list"):
                    page_last = blk
                    precedent_noeud = node_id
            if page_last is not None:
                last_text_block = page_last
            elif groups:
                last_text_block = None
            continue
        if pt.is_toc:
            if toc_node is None:
                toc_node = Node(node_id=f"{doc_id}:tdm", level=1, title="Table des matières",
                                surface_class="table_des_matieres")
                b.nodes[toc_node.node_id] = toc_node
                b.order.append(toc_node.node_id)
                b.root.items.append(NodeRef(node_id=toc_node.node_id))
            saved, b.current = b.current, toc_node
            first_article_group = next((i for i, (_, lines) in enumerate(groups)
                                        if lines[0].number is not None), None)
            toc_groups = groups if first_article_group is None else groups[:first_article_group]
            add_preliminary_groups(pt, toc_groups, table_source="tdm")
            b.current = saved
            last_text_block = None
            if first_article_group is None:
                continue
            # Une première clause sur la même page clôt la TdM : elle et tous les groupes suivants
            # appartiennent au corps citable.
            groups = groups[first_article_group:]
            article_seen = True
        if pt.surface_class == "preliminaire":
            saved, b.current = b.current, b.root
            add_preliminary_groups(pt, groups, table_source="preliminaire")
            b.current = saved
            last_text_block = None
            continue
        first_article_group = next((i for i, (_, lines) in enumerate(groups)
                                    if lines[0].number is not None), None)
        if (document_has_articles or document_has_toc) and not article_seen and not pt.after_toc:
            preliminary_count = len(groups) if first_article_group is None else first_article_group
            add_preliminary_groups(pt, groups[:preliminary_count], table_source="preliminaire")
            if first_article_group is None:
                last_text_block = None
                continue
            groups = groups[first_article_group:]
            article_seen = True
        elif first_article_group is not None:
            article_seen = True
        first = True
        page_last: Block | None = None
        for kind, lines in groups:
            continues = None
            # Scission de page (AD-2) : le premier bloc de la page, de même kind que le dernier bloc texte de la page
            # précédente, sans numéro d'article, alors que ce dernier ne finit pas une phrase (structure, pas casse).
            if first and kind in ("para", "list") and lines[0].number is None and last_text_block is not None \
                    and last_text_block.kind == kind and not last_text_block.text.rstrip().endswith(_TERMINAL):
                continues = last_text_block.block_id
            first = False
            if lines[0].number is not None:
                if future_numbers and future_numbers[0] == lines[0].number:
                    future_numbers.pop(0)
                elif lines[0].number in future_numbers:
                    future_numbers.remove(lines[0].number)
                b.current = b.node_for(lines[0].number)
                if kind == "heading":
                    b.current.title = lines[0].text
            elif continues is None:
                settings = get_settings()
                b.dedent_closing_group(
                    kind, lines, tolerance_pt=settings.dedent_tolerance_pt,
                    starter_max_lines=settings.dedent_starter_max_lines,
                    future_numbers=future_numbers,
                )
            blk = b.add_block(pt.page, lines, kind, continues=continues,
                              source_field="ocr" if pt.ocr_succeeded else None)
            if kind in ("para", "list"):
                page_last = blk
            if lines[0].number is not None and kind == "para":
                b.current.title = lines[0].text
        last_text_block = page_last
    anomalies = anomalies_registre(pages, b.block_uids)
    if anomalies:
        raise ValueError("registre de lignes source incohérent : " + " ; ".join(anomalies[:20]))
    # Réconciliation : un nœud prouvé que l'arbre bâti ne porte pas est un refus, jamais un `info`.
    # Sans elle, `run()` pouvait publier « proposition vérifiée : N nœud(s) » sur un arbre qui n'en
    # contient qu'une partie — exactement le « dégradé en silence » qu'AD-16 nomme.
    manquants = sorted(node_id for node_id in plan if node_id not in b.nodes)
    if manquants:
        raise StructureRefusee(
            "noeud_non_construit",
            f"{len(manquants)} nœud(s) prouvé(s) absent(s) de l'arbre bâti : {', '.join(manquants[:20])}")
    if structure is not None:
        # Et l'inverse : que chaque nœud prouvé existe ne dit rien de **qui porte quoi**. La
        # réconciliation compare le propriétaire effectif de chaque uid à celui que `node_of_uid`
        # prescrit, sur le document réellement bâti.
        reconcilier_affectation(b.nodes, b.block_uids, node_of_uid)
        block_by_uid = {
            line.line_uid: block.block_id
            for block in b.blocks
            for line in block.lines
            if line.line_uid is not None
        }
        missing_line_uids = (sorted(set(registre) - set(block_by_uid))
                             if structure.schema_version == "2" else [])
        if missing_line_uids:
            raise StructureRefusee(
                "affectation_non_prouvee",
                "line_uid proposé absent du Document construit : "
                + ", ".join(missing_line_uids[:20]),
            )
        blocks_by_id = {block.block_id: block for block in b.blocks}
        for node_id, spec in plan.items():
            direct_blocks = b.nodes[node_id].blocks
            positions = {block_id: position for position, block_id in enumerate(direct_blocks)}
            continuation_blocks = list(dict.fromkeys(
                block_by_uid[uid] for uid in spec.continuation_line_uids
            ))
            for block_id in continuation_blocks:
                position = positions.get(block_id)
                if position is None or position == 0:
                    raise StructureRefusee(
                        "affectation_non_prouvee",
                        f"continuation {block_id} sans cible antérieure sous {node_id}")
                target_id = direct_blocks[position - 1]
                block = blocks_by_id[block_id]
                block.continues = target_id
                block.context_role = "same_clause_continuation"
        # Le document validé ci-dessous est la frontière publique : toute continuation proposée
        # doit y survivre exactement, sinon l'artefact entier est refusé avant publication.
        projected = {
            uid: blocks_by_id[block_by_uid[uid]].continues
            for spec in plan.values() for uid in spec.continuation_line_uids
        }
        if any(target is None for target in projected.values()):
            raise StructureRefusee(
                "affectation_non_prouvee", "projection incomplète des continuations acceptées")
    toc_gaps = _apply_toc(b, toc)
    nodes = [b.nodes[nid] for nid in b.order]
    doc = Document(doc_id=doc_id, kind="contrat", title=title, edition=edition, lang="fr", nodes=nodes,
                   blocks=b.blocks, source_url=source_url, source_hash=source_hash,
                   ingest_fingerprint=ingest_fingerprint(structure))
    printed_toc = _printed_toc_entries(pages)
    meta = {"numbers": b.numbers, "duplicates": b.duplicates, "continues": b.continues, "toc": toc,
            "toc_gaps": toc_gaps, "printed_toc": printed_toc,
            # Le lien bloc → lignes source reste **interne à l'ingestion** : il ne traverse ni
            # `Document`, ni `document.json`, ni le loader (AD-2 inchangé).
            "source_uids": b.block_uids}
    return doc, meta


def _printed_toc_entries(pages: list[PageText]) -> list[tuple[str, str]]:
    """Sections numérotées annoncées par la TdM, titres multilignes réunis par géométrie de colonne."""
    entries: list[tuple[str, str]] = []
    settings = get_settings()
    for page in pages:
        if not page.is_toc:
            continue
        first_article_y = min((line.bbox[1] for line in page.lines if line.number is not None),
                              default=page.height)
        lines = [line for line in (page.toc_fragments or page.lines)
                 if line.bbox[1] < first_article_y and not _is_toc_title(line.text)]
        starts: list[tuple[PageLine, re.Match[str]]] = []
        for line in lines:
            text = line.text.strip()
            direct = _PRINTED_TOC_RE.match(text)
            if direct:
                entries.append((direct.group(1), direct.group(2).strip(" .")))
                continue
            numbered = _TOC_NUMBER_RE.match(text)
            if numbered:
                starts.append((line, numbered))
        starts.sort(key=lambda item: (item[0].bbox[0], item[0].bbox[1], item[0].text))
        for line, numbered in starts:
            next_y = page.height
            for other, _ in starts:
                if other.bbox[1] > line.bbox[1] \
                        and abs(other.bbox[0] - line.bbox[0]) <= settings.toc_column_tolerance_pt:
                    next_y = min(next_y, other.bbox[1])
            page_numbers = [candidate for candidate in lines
                            if _PAGE_NUMBER_RE.fullmatch(candidate.text.strip())
                            and candidate.bbox[0] > line.bbox[0]
                            and abs(candidate.bbox[1] - line.bbox[1])
                            <= settings.toc_page_number_baseline_pt]
            if not page_numbers:
                continue
            boundary_x = min((candidate.bbox[0] for candidate in page_numbers), default=page.width)
            title_parts = [numbered.group(2).strip(" .")]
            last_bottom = line.bbox[3]
            continuations = sorted(
                (candidate for candidate in lines
                 if line.bbox[1] < candidate.bbox[1] < next_y
                 and candidate.bbox[0] >= line.bbox[0] - settings.toc_indent_tolerance_pt
                 and candidate.bbox[2] < boundary_x
                 and not _PAGE_NUMBER_RE.fullmatch(candidate.text.strip())
                 and _TOC_NUMBER_RE.match(candidate.text.strip()) is None),
                key=lambda candidate: (candidate.bbox[1], candidate.bbox[0]),
            )
            for candidate in continuations:
                line_height = max(candidate.bbox[3] - candidate.bbox[1], 1.0)
                if candidate.bbox[1] - last_bottom > settings.toc_line_gap_ratio * line_height:
                    break
                title_parts.append(candidate.text.strip(" ."))
                last_bottom = candidate.bbox[3]
            entries.append((numbered.group(1), " ".join(part for part in title_parts if part)))
        for table in page.tables:
            if table.bbox[1] >= first_article_y:
                continue
            pending: tuple[str, list[str]] | None = None
            for row in table.rows:
                cells = [cell.strip() for cell in row if cell.strip()]
                if not cells or any(_is_toc_title(cell) for cell in cells):
                    continue
                text = " ".join(cells)
                direct = _PRINTED_TOC_RE.match(text)
                if direct:
                    entries.append((direct.group(1), direct.group(2).strip(" .")))
                    pending = None
                    continue
                numbered = _TOC_NUMBER_RE.match(text)
                if numbered:
                    pending = (numbered.group(1), [numbered.group(2).strip(" .")])
                elif pending:
                    if _PAGE_NUMBER_RE.fullmatch(cells[-1]):
                        pending[1].extend(cells[:-1])
                        entries.append((pending[0], " ".join(pending[1]).strip(" .")))
                        pending = None
                    else:
                        pending[1].extend(cells)
    # Les doublons restent intentionnellement présents : le rapport doit les signaler, y compris
    # quand deux lignes annoncent exactement le même titre et pas seulement quand elles divergent.
    return entries


def _apply_toc(b: _Builder, toc: list[Any]) -> list[str]:
    """Signets du PDF (`get_toc()` : [niveau, titre, page]) : un signet « 1.12 Contenu » donne son titre au nœud
    `a1.12` s'il n'en a pas ; un signet numéroté sans nœud correspondant est un écart (alerte `tdm_pdf_ecart`)."""
    gaps: list[str] = []
    # Index des numéros réellement servis : `{doc_id}:a3.1` sur le chemin heuristique, le numéro lu
    # dans le titre d'un nœud positionnel `{doc_id}:s1.2` sur le chemin proposé (story 4.2c).
    par_numero: dict[str, Node] = {}
    for node_id, node in b.nodes.items():
        numero = numero_de_noeud(b.doc_id, node_id, node.title)
        if numero is not None and node_id != b.doc_id:
            par_numero.setdefault(numero, node)
    for item in toc:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        m = _TOC_NUMBER_RE.match(str(item[1]).strip())
        if not m:
            continue
        node = par_numero.get(m.group(1))
        if node is None:
            gaps.append(f"{m.group(1)} (p. {item[2]})")
        elif not node.title:
            # Un nœud d'une proposition vérifiée a toujours son titre — relu au registre. Le signet
            # ne complète donc que les nœuds numérotés que l'heuristique a laissés sans intitulé.
            node.title = f"{m.group(1)} {m.group(2)}".strip()
    return gaps


def build_summary(doc: Document) -> str:
    """Projection exhaustive ; l'exposition au modèle est paginée par ``Index.sommaire_page``."""
    lines = [f"<!-- {doc.doc_id} · edition {doc.edition} · source_hash {doc.source_hash} · "
             f"ingest_fingerprint {doc.ingest_fingerprint} -->", f"# {doc.title}", ""]
    by_id = {n.node_id: n for n in doc.nodes}
    first_page: dict[str, int | None] = {}

    def page_of(node: Node) -> int | None:
        if node.node_id in first_page:
            return first_page[node.node_id]
        p: int | None = None
        for item in node.items:
            if isinstance(item, BlockRef):
                block = doc.block(item.block_id)
                q = block.page if is_citable(block) else None
            else:
                q = page_of(by_id[item.node_id])
            if q is not None and (p is None or q < p):
                p = q
        first_page[node.node_id] = p
        return p

    def walk(node: Node) -> None:
        for child_id in node.children:
            child = by_id[child_id]
            if child.level >= 1 and (
                    any(is_citable(doc.block(block_id)) for block_id in child.blocks)
                    or child.children):
                pg = page_of(child)
                lines.append(f"{'  ' * (child.level - 1)}- `{child.node_id}` · {child.title} · p. {pg}")
            walk(child)

    walk(by_id[doc.doc_id])
    return "\n".join(lines).rstrip() + "\n"


class _StructureAChange(Exception):
    """Sort de la fabrique **avec son rapport déjà bâti** : le refus est un check, pas une trace."""


def run(data_dir: Path, *, edition: str | None, doc_id: str = DOC_ID,
        title: str | None = None) -> tuple[Report, ManifestEntry]:
    """Ingère `data_dir/source.pdf` ; toute erreur de source devient un check bloquant, jamais une trace Python."""
    manifest_path = data_dir.parent / "manifest.json"
    fingerprint = ingest_fingerprint()
    resolved_edition = edition if edition is not None else (DEFAULT_EDITION if doc_id == DOC_ID else "")
    try:
        # **Préflight seulement** : refuser tôt un manifest illisible, avec un rapport bloquant
        # plutôt qu'une trace Python. L'état dont la fusion part est relu sous le verrou de la racine
        # par `fusionner_et_publier` (tour de racine unique, fait 2) : une lecture faite ici, avant
        # des minutes de traitement, serait périmée et écraserait une publication concurrente.
        read_manifest(manifest_path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        detail = f"{manifest_path} illisible, rien n'a été écrit : {type(exc).__name__}: {exc}"[:2000]
        report = Report(doc_id=doc_id, checks=[Check(name="manifest_illisible", level="bloquant", detail=detail)])
        return report, ManifestEntry(status="quarantaine", source_hash="", ingest_fingerprint=fingerprint,
                                     document_hash="", edition=resolved_edition, gate=None)
    try:
        # **Préflight de couverture** (revue du tour de racine unique, constat 6) : le lot complet
        # est connu dès maintenant, donc une disposition absente se dit **avant** l'extraction, et
        # par le même chemin que les autres refus — un check bloquant, jamais une trace Python.
        verifier_couverture_du_lot([data_dir / "document.json", data_dir / "summary.md",
                                    data_dir / "report.json", manifest_path])
    except Exception as exc:  # noqa: BLE001 — une disposition absente n'est pas une trace Python
        detail = f"rien n'a été écrit : {type(exc).__name__}: {exc}"[:2000]
        report = Report(doc_id=doc_id, checks=[Check(name="espace_de_publication_incomplet",
                                                     level="bloquant", detail=detail)])
        return report, ManifestEntry(status="quarantaine", source_hash="", ingest_fingerprint=fingerprint,
                                     document_hash="", edition=resolved_edition, gate=None)
    source_hash = ""
    doc: Document | None = None
    summary = ""
    structure_appliquee: str | None = None
    rendu_structure: str | None = None
    echec: Report | None = None
    pages: Any = None
    meta: Any = None
    toc: Any = None
    try:
        document_title = TITLE if title is None and doc_id == DOC_ID else title
        if document_title is None:
            raise ValueError("--title est obligatoire pour un contrat autre que le document de compatibilité AXA")
        document_title = document_title.strip()
        if not document_title:
            raise ValueError("--title ne peut pas être vide")
        if not resolved_edition.strip():
            raise ValueError("--edition doit reprendre le libellé imprimé du contrat")
        pdf_path = data_dir / "source.pdf"
        expected_path = data_dir / "source.sha256"
        # AD-7 : l'empreinte de référence est obligatoire et vérifiée avant toute extraction (jamais d'ingestion
        # d'un PDF non vérifié, même si `fetch_source` n'a pas été utilisé).
        if not expected_path.is_file():
            raise ValueError("source.sha256 absent : l'empreinte de référence du PDF est obligatoire (AD-7)")
        expected = expected_path.read_text("utf-8").split()[:1]
        if not expected or not _SHA256_RE.match(expected[0].lower()):
            raise ValueError("source.sha256 mal formé : 64 caractères hexadécimaux attendus")
        source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        if expected[0].lower() != source_hash:
            raise ValueError(f"source.pdf : sha256 {source_hash} ≠ source.sha256 — relancer fetch_source")
        pages, toc = extract_pages(pdf_path)
        source_url_path = data_dir / "source.url"
        source_url = source_url_path.read_text("utf-8").strip() if source_url_path.is_file() else None
        if doc_id != DOC_ID:
            parsed_url = urlsplit(source_url or "")
            http_url = parsed_url.scheme in {"http", "https"} and bool(parsed_url.netloc)
            gs_url = bool(source_url and GS_URL_RE.fullmatch(source_url))
            if not source_url or not (http_url or gs_url) or any(char.isspace() for char in source_url):
                raise ValueError(
                    "source.url doit contenir une URL HTTP(S) ou gs://bucket/objet non vide pour un contrat générique"
                )
        # Story 4.2c : la proposition est un artefact, jamais un appel à chaud (AD-2, stabilité).
        # Absente, l'heuristique numérique reste le chemin nominal ; présente, elle est revérifiée à
        # chaque ingestion et son refus met le document en quarantaine.
        # `presente()` et non `is_file()` : un répertoire ou un lien pendant répondent `False` à
        # `is_file()` et faisaient retomber l'ingestion sur l'heuristique **en silence**, alors que
        # `charger()` promet de les classer `proposition_illisible` (AD-16). Seule l'absence au sens
        # du système de fichiers est une absence.
        structure_path = data_dir / "structure.json"
        structure = None
        if presente(structure_path):
            # **Une seule lecture, un seul tampon** (revue N1–N3, constat 7) : l'empreinte porte sur
            # les octets réellement appliqués, jamais sur une seconde ouverture du même chemin.
            structure, octets_structure = charger_octets(structure_path)
            structure_appliquee = hashlib.sha256(octets_structure).hexdigest()
        # La proposition appliquée entre dans l'empreinte : deux ingestions du même PDF qui rendent
        # des `node_id` différents ne peuvent pas porter la même (AD-2, lu par le loader).
        fingerprint = ingest_fingerprint(structure)
        doc, meta = build_document(pages, edition=resolved_edition, source_hash=source_hash, toc=toc, doc_id=doc_id,
                                   title=document_title, source_url=source_url, structure=structure)
        summary = build_summary(doc)
        detail = (None if structure is None else
                  f"proposition vérifiée : {len(structure.noeuds)} nœud(s) sur lignes source")
        rendu_structure = detail
    except ValidationError as exc:
        echec = report_from_validation_error(doc_id, exc)
    except StructureRefusee as exc:
        echec = Report(doc_id=doc_id, checks=[structure_check(exc.motif, exc.detail)])
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        # pymupdf lève FileDataError (RuntimeError) sur un PDF corrompu ; OSError si `source.pdf` manque.
        detail = f"{type(exc).__name__}: {exc}"[:2000]
        echec = Report(doc_id=doc_id, checks=[Check(name="source_illisible", level="bloquant", detail=detail)])
    publie: Report | None = None

    def fabriquer(lecture: LectureDuLot) -> tuple[ManifestEntry, list[tuple[Path, str | None]]]:
        """Ce qui décide du contenu publié, lu **sous le verrou** (story 4.5, N3).

        Le document précédent (`ids_disparus`), l'empreinte de l'overlay et celle de la structure
        étaient évalués comme arguments d'appel, donc avant l'ouverture de la transaction : l'entrée
        publiée pouvait nommer un overlay ou une structure qu'une opération concurrente avait
        remplacés entre-temps. Les trois viennent maintenant du repère que la transaction a pincé.

        La structure **appliquée** par l'extraction, elle, a nécessairement été lue avant — c'est
        elle qui décide des `node_id`. Elle est donc **opposée** à celle de la génération publiée :
        si elles diffèrent, l'entrée nommerait une structure que le document n'applique pas, et
        l'ingestion refuse plutôt que de publier cette contradiction.
        """
        nonlocal publie
        # **Le filet suit la construction du rapport là où elle a été déplacée** (revue N1–N3,
        # constat 3) : `run()` promet un check bloquant, jamais une trace Python, et `main()`
        # l'appelle sans `try`.
        rapport = echec
        document_publie = doc
        structure_publiee: str | None = None
        if rapport is None:
            try:
                structure_publiee = lecture.empreinte(data_dir / STRUCTURE_FILE)
                if structure_publiee != structure_appliquee:
                    # **Un refus voulu, rendu comme les autres** (revue N1–N3, constat 3) : l'entrée
                    # nommerait une structure que ce document n'applique pas. Un check bloquant
                    # nommé et un code 1, comme `espace_de_publication_incomplet` — jamais une trace
                    # Python, que `main()` n'attrape pas. Le nom est **hors** du vocabulaire fermé
                    # du vérificateur (`structure.MOTIFS`) : ce n'est pas la proposition qui est
                    # refusée, c'est la publication qui ne peut plus l'attester.
                    rapport = Report(doc_id=doc_id, checks=[Check(
                        name="structure_a_change", level="bloquant",
                        detail=(f"{STRUCTURE_FILE} a changé entre l'extraction et la publication "
                                f"({structure_appliquee} → {structure_publiee}) : l'entrée "
                                "nommerait une structure que ce document n'applique pas — "
                                "relancer l'ingestion")[:2000])])
                    raise _StructureAChange
                precedent = lecture.document_precedent(data_dir / "document.json")
                rapport = build_pdf_report(
                    doc, precedent, pages=pages,
                    numbers=meta["numbers"], duplicates=meta["duplicates"],
                    continues=meta["continues"], toc=toc, toc_gaps=meta["toc_gaps"],
                    printed_toc=meta["printed_toc"], summary=summary, structure=rendu_structure)
                document_publie, reused = reutiliser_typage_identique(
                    doc, precedent, lecture.octets(data_dir / "report.json"),
                )
                pending = sum(
                    is_citable(block) and block.block_id not in reused
                    for block in document_publie.blocks
                )
                rapport.stats.update({
                    TYPING_REUSED_IDS_STAT: reused,
                    TYPING_REUSED_COUNT_STAT: len(reused),
                    TYPING_PENDING_COUNT_STAT: pending,
                })
                if pending == 0:
                    # Une identité intégralement transportée a déjà traversé les deux lectures :
                    # ses contrôles sémantiques se recalculent localement, sans appel fournisseur.
                    # Le rapport final décrit alors le corpus publié, non la génération précédente
                    # contre laquelle le delta d'ingestion a été mesuré.
                    rapport = enrich_typing_report(
                        canoniser_transition_apres_typage(rapport), document_publie,
                    )
            except _StructureAChange:
                pass  # le rapport est déjà bâti, et il est bloquant
            except ValidationError as exc:
                rapport = report_from_validation_error(doc_id, exc)
            except StructureRefusee as exc:
                rapport = Report(doc_id=doc_id, checks=[structure_check(exc.motif, exc.detail)])
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError,
                    RuntimeError) as exc:
                detail = f"{type(exc).__name__}: {exc}"[:2000]
                rapport = Report(doc_id=doc_id, checks=[
                    Check(name="source_illisible", level="bloquant", detail=detail)])
        status = "quarantaine" if rapport.blocking else "servi"
        document_hash = ""
        # Story 4.5, tour de racine unique : le lot **complet** de l'ingestion est constitué ici,
        # puis publié en un seul geste avec le manifest — un point de commit pour l'opération, et
        # non un par artefact. Une absence (`None`) est membre du lot comme un contenu : jamais un
        # artefact périmé à côté d'un manifest en quarantaine.
        artefacts: list[tuple[Path, str | None]] = []
        if document_publie is not None and not rapport.blocking:
            doc_text = document_json(document_publie)
            artefacts += [(data_dir / "document.json", doc_text), (data_dir / "summary.md", summary)]
            document_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
        else:
            artefacts += [(data_dir / "document.json", None), (data_dir / "summary.md", None)]
        # Story 4.5 : quand une proposition de structure a été acceptée et que le document a bien
        # été construit, le rapport **atteste** de quoi il parle — `document_hash` et
        # `structure_hash` observés. Sans cette liaison, « accepté » resterait déclaratif, et le
        # gate `full` prendrait un `structure.json` arbitraire pour une structure prouvée.
        rapport = attester_structure(rapport, document_hash=document_hash,
                                     structure_hash=structure_publiee or "")
        # Et l'attestation d'arbre, sur le même patron (revue B4, volet guide) : les deux ingestions
        # émettent `invariants_arbre`, et aucune ne décide de son périmètre par un `doc_id`. C'est
        # le témoin du plancher qui choisit, par la règle `SOURCE_FILES`, laquelle des deux preuves
        # il oppose à quel document ; l'ingestion, elle, atteste ce qu'elle a vraiment vérifié.
        rapport = attester_arbre(rapport, document_hash=document_hash,
                                 ingest_fingerprint=fingerprint)
        artefacts.append((data_dir / "report.json",
                          json.dumps(rapport.model_dump(), indent=2, ensure_ascii=False) + "\n"))
        publie = rapport
        return ManifestEntry(status=status, source_hash=source_hash,
                             ingest_fingerprint=fingerprint, document_hash=document_hash,
                             edition=resolved_edition,
                             overlay_hash=lecture.empreinte(data_dir / OVERLAY_FILE),
                             # Story 4.5 : la proposition de structure est couverte par le manifest,
                             # exactement comme l'overlay l'est.
                             structure_hash=structure_publiee, gate=None), artefacts

    entry = merge_manifest(manifest_path, doc_id, fabriquer,
                           cibles=[data_dir / "document.json", data_dir / "summary.md",
                                   data_dir / "report.json"])
    if publie is None:  # pragma: no cover — la fabrique le pose toujours ; `assert` disparaît sous -O
        raise RuntimeError("la fabrique n'a pas produit de rapport : rien n'a été publié")
    return publie, entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("doc_id", nargs="?", default=DOC_ID)
    parser.add_argument("--data", default="data", type=Path)
    parser.add_argument("--edition")
    parser.add_argument("--title")
    args = parser.parse_args(argv)
    # Résoudre `data/{doc_id}` avant ce contrôle autoriserait séparateurs et `..` à sortir du dépôt.
    if len(args.doc_id) > DOC_ID_MAX or not DOC_ID_RE.fullmatch(args.doc_id):
        print(f"doc_id invalide (slug [a-z0-9-]+ de {DOC_ID_MAX} caractères maximum attendu) : "
              f"{args.doc_id!r}", file=sys.stderr)
        return 2
    # **Un entrypoint de production exige une racine installée** (story 4.5, N3) : le refus tombe
    # avant l'extraction du PDF, jamais après. Un `--data` custom **installé** passe sans traitement.
    doc_dir = args.data / args.doc_id
    try:
        exiger_espace_installe([doc_dir / "document.json", doc_dir / "summary.md",
                                doc_dir / "report.json", args.data / "manifest.json"])
    except Exception as exc:  # noqa: BLE001 — une disposition absente n'est pas une trace Python
        print(f"refus : {exc}", file=sys.stderr)
        return 2
    report, entry = run(args.data / args.doc_id, edition=args.edition, doc_id=args.doc_id, title=args.title)
    for c in report.checks:
        print(f"[{c.level}] {c.name}: {c.detail}")
    print(f"stats: {json.dumps(report.stats, ensure_ascii=False)}")
    gate = "null" if entry.gate is None else f"{entry.gate.profile}, evals_ok={entry.gate.evals_ok}"
    line = f"{args.doc_id} : {entry.status} (edition {entry.edition}, gate: {gate})"
    if report.blocking:
        print(line, file=sys.stderr)
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
