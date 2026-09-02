"""Preuves hermétiques et parsing réel du second contrat Baloise Luxembourg HOME."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from server.app.domain import BlockRef, Document, NodeRef, Report, is_citable
from server.ingest import pdf_structure_gate as structure_gate
from server.ingest import pdf_to_blocks as p
from server.ingest.report import (attester_arbre, attester_structure, build_pdf_report,
                                  canoniser_transition_apres_typage)
from tests.helpers_reports import assert_stats_structurelles_exactes
from tests.test_porte_de_lecture import mesure_de_la_porte

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
    # Provenance d'un typage recalculé hors réseau depuis l'audit : une régénération depuis le PDF
    # réutilise ces décisions sans les rejouer, elle ne peut donc pas produire ce contrôle.
    "typage_rejeu_audit",
}
# Ce que l'artefact publié porte **en plus** de la projection structurelle. Une réingestion qui
# retrouve chaque bloc à l'identique transporte le typage sans rien rejouer : le bilan de la campagne
# du 02/09 décrivait la génération précédente, et `canoniser_transition_apres_typage` le retire.
# Reste le bilan du transport lui-même.
TERMINAL_TYPING_STATS = {
    "blocs_typage_a_rejouer", "blocs_typage_reutilises", "ids_typage_reutilises",
}


@pytest.fixture(scope="module")
def doc() -> Document:
    return Document.model_validate_json((REAL / "document.json").read_bytes())


@pytest.mark.etat_servi  # gates servis exigés : hors preuve offline pendant l'amorçage (plancher.yaml)
def test_baloise_artifacts_publish_the_verified_identity_and_measured_gaps(doc: Document) -> None:
    report = Report.model_validate_json((REAL / "report.json").read_bytes())
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text("utf-8"))[DOC]
    assert (doc.doc_id, doc.title, doc.edition) == (DOC, TITLE, EDITION)
    assert doc.source_hash == SOURCE_HASH
    assert doc.source_url == (REAL / "source.url").read_text("utf-8").strip()
    gate = manifest.pop("gate")
    assert manifest == {
        "status": "servi",
        "source_hash": SOURCE_HASH,
        "ingest_fingerprint": doc.ingest_fingerprint,
        "document_hash": hashlib.sha256((REAL / "document.json").read_bytes()).hexdigest(),
        "edition": EDITION,
        "overlay_hash": None,
        # La proposition de structure Opus appliquée (02/09/2026) : le manifest porte l'empreinte
        # des octets réellement lus par l'ingestion.
        "structure_hash": hashlib.sha256((REAL / "structure.json").read_bytes()).hexdigest(),
    }
    assert gate is not None
    assert gate["profile"] == "vertical" and gate["evals_ok"] is True
    assert gate["source_hash"] == SOURCE_HASH
    assert gate["ingest_fingerprint"] == doc.ingest_fingerprint
    assert gate["cases_hash"] == "e493de7d5d41ac4e64537f54124bdb6cac2ad97f6356aaca908a1d67c860e2e8"
    assert gate["pipeline_digest"] == "23699bbe105930b1afc47c03e422a1983a7104e600e6c79839c89757941f3c9e"
    assert gate["prompts_digest"] == "4b8a3fce5e59e0a0978973b14bc78fa5c3891534493c61d70e632ee8fa3d1d45"
    assert gate["model_ids"] == {
        "ingest": "claude-opus-5",
        "reason": "claude-sonnet-5",
        "micro": "claude-haiku-4-5-20251001",
    }
    assert gate["cases"] == 3 and gate["countersigned"] is False
    assert not report.blocking
    assert report.stats["pages"] == report.stats["pages_avec_blocs"] == 48
    assert report.stats["blocs"] == len(doc.blocks) == 1039
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


def test_baloise_typing_is_transported_and_the_relinked_halves_were_reread() -> None:
    """Le typage publié est **transporté**, et ce qu'il ne transporte pas a été relu.

    La campagne du 02/09 (130 messages standard, 4,7695 EUR) a typé l'arbre Opus ; l'ingestion qui
    relie les phrases coupées retrouve 942 blocs à l'identique et recopie leurs décisions sans un
    appel. Les 22 blocs qu'elle ne transporte pas sont exactement ceux qu'elle vient de relier à
    leur moitié précédente : leur typage avait été décidé sur un fragment de phrase, il a été relu
    avec la phrase entière par une campagne standard de 6 messages (0,1807 EUR). Le rapport publié
    décrit ce transport et cette relecture, jamais la génération précédente.
    """
    report = Report.model_validate_json((REAL / "report.json").read_bytes())
    doc = Document.model_validate_json((REAL / "document.json").read_bytes())
    assert report.stats["blocs_typage_reutilises"] == 942
    assert report.stats["blocs_typage_rejoues"] == 22
    assert len(report.stats["ids_typage_reutilises"]) == 942
    assert report.stats["typage_transport"] == "standard"
    assert report.stats["typage_standard_requests"] == 6
    assert report.stats["typage_standard_cost_eur"] == 0.1807
    assert report.stats["typage_total_cost_eur"] <= report.stats["typage_cost_ceiling_eur"] == 3.0
    assert report.stats["blocs_types_modele"] == 610
    assert report.stats["blocs_juridiques"] == 602
    assert report.stats["blocs_juridiques_confirmes"] == 565
    transport = next(check for check in report.checks if check.name == "typage_transport")
    assert transport.level == "info" and "aucune API Batch" in transport.detail
    assert sum(block.kind_source in {"model", "model_verified"} for block in doc.blocks) == 610
    # Trois moitiés reliées restent sans kind après relecture : la campagne les a lues et n'y a vu
    # aucune clause (une liste d'intitulés, un fragment d'alinéa, des coordonnées). Tout autre bloc
    # relié citable porte son kind.
    sans_kind = sorted(block.block_id for block in doc.blocks
                       if is_citable(block) and block.continues and block.kind_source is None
                       and block.structural_kind in {"para", "list"})
    assert sans_kind == [f"{DOC}:p10:20", f"{DOC}:p15:14", f"{DOC}:p46:14"]


def test_baloise_has_citable_contract_passages_for_the_three_witnesses(doc: Document) -> None:
    corpus = "\n".join(block.text.casefold() for block in doc.blocks)
    assert "brûlure de\ncigarette" in corpus
    assert "arrêt accidentel du congélateur" in corpus
    assert "responsabilité civile vie privée" in corpus
    assert all(block.lines and block.bbox and block.loc == f"p{block.page}" for block in doc.blocks)
    # Arbre Opus (02/09/2026) : la racine porte les 18 nœuds de niveau 1 — couverture, sommaire,
    # les quinze sections numérotées, cartouche — et chaque autre nœud est référencé exactement
    # une fois, par son parent.
    nested = [item for node in doc.nodes for item in node.items if isinstance(item, NodeRef)]
    root = next(node for node in doc.nodes if node.node_id == DOC)
    assert [item.node_id for item in root.items if isinstance(item, NodeRef)] == [
        node.node_id for node in doc.nodes if node.level == 1]
    assert len({node.node_id for node in doc.nodes if node.level == 1}) == 18
    assert sorted(item.node_id for item in nested) == sorted(
        node.node_id for node in doc.nodes if node.node_id != DOC)
    assert all(
        isinstance(item, (BlockRef, NodeRef)) for node in doc.nodes for item in node.items
    )


@pytest.fixture(scope="module")
def regeneration() -> tuple[Document, dict, list, list]:
    """Réingestion du PDF réel, faite **une fois** pour les deux moitiés du certificat."""
    pdf = REAL / "source.pdf"
    assert pdf.is_file(), "source.pdf Baloise requis par la porte de déploiement"
    source_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert source_hash == SOURCE_HASH == (REAL / "source.sha256").read_text("utf-8").strip()
    pages, toc = p.extract_pages(pdf)
    built, meta = p.build_document(
        pages, edition=EDITION, source_hash=source_hash, toc=toc, doc_id=DOC, title=TITLE,
        source_url=(REAL / "source.url").read_text("utf-8").strip(),
    )
    return built, meta, pages, toc


@pytest.mark.skipif(
    not (REAL / "source.pdf").is_file() and os.environ.get("REAL_PDF_TESTS_REQUIRED") != "1",
    reason="source.pdf absent (non committé)",
)
def test_real_baloise_pdf_invariants_de_surface_et_de_registre(
        regeneration: tuple[Document, dict, list, list]) -> None:
    """Moitié toujours exécutée : l'asymétrie entre les deux certificats était le défaut.

    Ce contrat n'avait aucune moitié indépendante des artefacts committés — et l'artefact committé
    ne porte, lui, aucune `surface_class` autre que `substantiel` : une preuve sur ses octets ne
    discriminerait rien. Ce sont donc les invariants **génériques** du chemin PDF réel qui portent
    la couverture, exactement les mêmes que pour l'autre contrat.
    """
    built, meta, pages, _toc = regeneration
    assert p.anomalies_registre(pages, meta["source_uids"]) == []
    toc_node = next(node for node in built.nodes if node.node_id == f"{DOC}:tdm")
    assert toc_node.surface_class == "table_des_matieres" and toc_node.blocks
    assert all(built.block(block_id).surface_class == "table_des_matieres"
               and not is_citable(built.block(block_id)) for block_id in toc_node.blocks)
    assert structure_gate._semantic_issues(built) == []
    page_issues = {}
    for page in pages:
        issues = structure_gate._page_issues(page, built, meta["source_uids"])
        if failing := {name: errors for name, errors in issues.items() if errors}:
            page_issues[page.page] = failing
    assert page_issues == {}


@pytest.mark.skipif(
    not (REAL / "source.pdf").is_file() and os.environ.get("REAL_PDF_TESTS_REQUIRED") != "1",
    reason="source.pdf absent (non committé)",
)
def test_real_baloise_pdf_ce_que_la_porte_de_lecture_laisse_passer(
        regeneration: tuple[Document, dict, list, list]) -> None:
    """Vérité **régénérée** de la porte de lecture, indépendante de tout artefact committé.

    C'est ce contrat qui a fait rougir les quatre règles à la fois : un titre courant composé tourné
    dans la marge droite de 47 de ses 48 pages, tombé au milieu du flux d'une colonne ; des centaines
    de puces et de points-virgules sortis en lignes muettes ; et deux colonnes qu'une fausse frontière
    intérieure relisait entrelacées. Après correction, aucune ligne tournée ne subsiste, une seule
    ligne-glyphe reste — isolée dans sa bande, donc rien à lui rendre —, et aucune ligne de colonne
    n'est déclarée pleine largeur sur les 42 pages à gouttière.
    """
    built, _meta, pages, _toc = regeneration
    assert mesure_de_la_porte(pages) == {
        "lignes_tournees_conservees": 0,
        "titres_courants_survivants": 0,
        "lignes_glyphes": 1,
        "lignes_pleine_largeur_hors_marge": 0,
        "pages_a_gouttiere": 42,
    }
    assert (len(built.blocks), len(built.nodes)) == (962, 2)


@pytest.mark.skipif(
    not (REAL / "source.pdf").is_file() and os.environ.get("REAL_PDF_TESTS_REQUIRED") != "1",
    reason="source.pdf absent (non committé)",
)
def test_real_baloise_pdf_regenerates_the_committed_structural_identity(
        doc: Document, regeneration: tuple[Document, dict, list, list]) -> None:
    """Moitié « comparaison » : même garde partagée que l'autre contrat, aux mêmes trois issues."""
    from tests.test_pdf_to_blocks import exiger_comparaison_aux_artefacts_committes

    exiger_comparaison_aux_artefacts_committes(DOC, doc.ingest_fingerprint)
    _built_sans_structure, _meta, pages, toc = regeneration
    # L'artefact servi applique la proposition de structure Opus publiée à côté du PDF : la
    # régénération la lit comme l'ingestion (mêmes octets, même empreinte) avant de comparer.
    structure, octets_structure = p.charger_octets(REAL / "structure.json")
    built, meta = p.build_document(
        pages, edition=EDITION, source_hash=SOURCE_HASH, toc=toc, doc_id=DOC, title=TITLE,
        source_url=(REAL / "source.url").read_text("utf-8").strip(), structure=structure,
    )
    identity = lambda block: (  # noqa: E731
        block.block_id, block.text, block.lang, block.loc, block.seq, block.page, block.bbox,
        block.structural_kind, block.source_field, block.continues,
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
        structure=f"proposition vérifiée : {len(structure.noeuds)} nœud(s) sur lignes source",
    )
    report = canoniser_transition_apres_typage(report)
    document_hash = hashlib.sha256((REAL / "document.json").read_bytes()).hexdigest()
    report = attester_structure(
        report, document_hash=document_hash,
        structure_hash=hashlib.sha256(octets_structure).hexdigest(),
    )
    report = attester_arbre(
        report, document_hash=document_hash, ingest_fingerprint=doc.ingest_fingerprint,
    )
    committed = Report.model_validate_json((REAL / "report.json").read_bytes())
    assert report.checks == [check for check in committed.checks if check.name not in TYPING_CHECKS]
    assert_stats_structurelles_exactes(report, committed, TERMINAL_TYPING_STATS)
