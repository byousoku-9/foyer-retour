"""Pages clés du contrat AXA (9, 11, 34, 46) : extraits relus et typage automatique committé.

Sans réseau : le PDF réel n'est utilisé que s'il est présent (`data/axa-lu-optihome-2017/source.pdf`, non committé) —
l'ingestion PDF doit alors regénérer leur structure et leur contenu à l'identique ; le typage
Batch, testé sans réseau ailleurs, est la seconde étape qui enrichit cet artefact.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
from server.app.domain import BlockRef, Document, Report, is_citable
from server.ingest import pdf_structure_gate as structure_gate
from server.ingest import pdf_to_blocks as p
from server.ingest.report import (attester_arbre, build_pdf_report,
                                  canoniser_transition_apres_typage)
from tests.helpers_reports import assert_stats_structurelles_exactes
from tests.test_porte_de_lecture import mesure_de_la_porte

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "data" / "axa-lu-optihome-2017"
EXTRACTS = Path(__file__).parent / "data" / "axa"
DOC = "axa-lu-optihome-2017"
TYPING_CHECKS = {
    "corruption_decisionnelle", "unresolved_refs", "definition_introuvable",
    "exclusion_sans_marqueur", "confiance_typage_faible", "kinds_non_confirmes", "typage_clauses",
    "typage_transport",
    # Provenance d'un typage recalculé hors réseau depuis l'audit : une régénération depuis le PDF
    # réutilise ces décisions sans les rejouer, elle ne peut donc pas produire ce contrôle.
    "typage_rejeu_audit",
}
TERMINAL_TYPING_STATS = {
    "blocs_juridiques", "blocs_juridiques_confirmes", "blocs_typage_a_rejouer",
    "blocs_typage_reutilises", "blocs_types_modele", "ids_typage_reutilises",
    "references_non_resolues",
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
    # Le typage est l'unique écrivain sémantique des portées. La cible explicite de p46:1 et son
    # sous-arbre restent hors socle : aucune garantie de villégiature ne peut ouvrir AD-6 (3).
    assert by[f"{DOC}:a3.1.8.3"].scope.kind != "commun"
    assert doc.node_scope_kind(doc.node_of(f"{DOC}:p46:8")) != "commun"
    assert by[DOC].children == [f"{DOC}:tdm", *(f"{DOC}:a{i}" for i in (1, 2, 3, 4))]
    assert all(b.loc == f"p{b.page}" and b.lines and b.bbox for b in doc.blocks)
    assert all("\x07" not in b.text and "Wingdings" not in b.text for b in doc.blocks)
    assert {b.kind for b in doc.blocks} <= {
            "para", "heading", "table", "list", "definition", "garantie", "exclusion", "condition",
        "franchise", "renvoi", "autre",
    }
    assert all(b.structural_kind in {"para", "heading", "table", "list", "autre"} for b in doc.blocks)
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
    # 8 blocs : couverture, sommaire (pages 2-4, un bloc par colonne depuis que la porte de lecture
    # recolle les glyphes à leur ligne) et quatrième de couverture (page 109, coordonnées de
    # l'agent), tous hors rappel.
    assert "8 bloc(s) sur 5 page(s)" in by_check["blocs_non_citables"].detail
    assert by_check["pages_mixtes"].detail.endswith(": 1")
    assert report.stats["tables"] == 7 and report.stats["couverture"] == 1.0
    assert report.stats["tables"] == sum(
        (block.structural_kind or block.kind) == "table" for block in doc.blocks
    )
    owner = {block_id: node.node_id for node in doc.nodes for block_id in node.blocks}

    def definition_anchored(block_id: str) -> bool:
        block = doc.block(block_id)
        current = owner[block_id]
        texts = [block.text]
        while True:
            texts.append(by[current].title)
            if current not in parent:
                break
            current = parent[current]
        term = normalize(block.defines or "")
        return bool(term) and any(term in normalize(text) for text in texts)

    assert all(
        definition_anchored(block.block_id)
        for block in doc.blocks if block.kind == "definition" and block.defines is not None
    )
    assert report.stats["blocs_types_modele"] == sum(
        block.kind_source in {"model", "model_verified"} for block in doc.blocks
    )
    assert report.stats["blocs_juridiques_confirmes"] == sum(
        block.kind in {"definition", "garantie", "exclusion", "condition", "franchise"}
        and block.kind_source == "model_verified" for block in doc.blocks
    )
    assert report.stats["references_non_resolues"] == sum(
        len(block.unresolved_refs) for block in doc.blocks
        if block.kind_source in {"model", "model_verified"}
    )
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text("utf-8"))[DOC]
    # Le document et l'overlay ont réellement changé : l'ingestion a d'abord invalidé le gate, puis
    # le runner d'évals l'a réécrit après certification de l'artefact final. Le test n'épingle ni la
    # date ni le hash des cas : il vérifie que la preuve servie vise bien l'artefact chargé.
    gate = manifest["gate"]
    assert manifest["status"] == "servi" and gate is not None
    assert gate["profile"] == "vertical" and gate["cases"] == 1 and gate["evals_ok"] is True
    assert gate["ingest_fingerprint"] == manifest["ingest_fingerprint"]
    assert gate["source_hash"] == manifest["source_hash"] and gate["overlay_hash"] is None
    assert manifest["overlay_hash"] is None
    # Cohérence artefact ↔ manifest : c'est exactement ce que le loader vérifie pour servir le
    # document. L'égalité avec `p.ingest_fingerprint()` — « le parseur *courant* reproduirait cet
    # artefact » — appartient au test de régénération, qui seul dispose du PDF : depuis la story
    # 4.2c, les règles de segmentation ont changé et l'artefact committé attend une réingestion
    # avec les sources réelles (voir `docs/choix-et-limites.md`).
    assert manifest["ingest_fingerprint"] == doc.ingest_fingerprint
    assert manifest["document_hash"] == hashlib.sha256((REAL / "document.json").read_bytes()).hexdigest()
    assert manifest["source_hash"] == (REAL / "source.sha256").read_text("utf-8").strip()


def test_typage_automatique_confirme_les_quatre_goldens_sans_overlay() -> None:
    c = load_corpus(ROOT / "data", allow_ungated=True)
    assert c.quarantine == {} and set(c.documents) == {
        DOC, "baloise-lu-home-2-2024", "lux-guide",
    }
    d = c.documents[DOC]
    confirmed = {b.block_id: b for b in d.blocks if b.kind_confirmed}
    golden_ids = {f"{DOC}:p9:2", f"{DOC}:p11:12", f"{DOC}:p34:12", f"{DOC}:p46:1"}
    report = Report.model_validate_json((REAL / "report.json").read_bytes())
    legal = {"definition", "garantie", "exclusion", "condition", "franchise"}
    # `blocs_juridiques_confirmes` ne compte que les kinds juridiques. `T2_ELIGIBILITY_MODE`
    # = ISOLATED fait aussi passer les `renvoi` en seconde lecture : un `renvoi` confirmé est un
    # certificat légitime, il n'est simplement pas un kind juridique. Rien d'autre ne peut l'être.
    assert {b.kind for b in confirmed.values()} - legal <= {"renvoi"}
    assert sum(b.kind in legal for b in confirmed.values()) == \
        report.stats["blocs_juridiques_confirmes"]
    assert golden_ids <= set(confirmed)
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
    assert d.node_scope_kind(d.node_of(f"{DOC}:p47:4")) == "extension"
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text("utf-8"))[DOC]
    assert manifest["overlay_hash"] is None and not (REAL / "typing.manual.json").exists()
    raw = json.loads((REAL / "document.json").read_text("utf-8"))
    raw_by_id = {block["block_id"]: block for block in raw["blocks"]}
    assert all(raw_by_id[block_id]["kind_source"] == "model_verified" for block_id in golden_ids)


def test_lempreinte_committee_est_a_jour_ou_declaree_perimee(doc: Document) -> None:
    """Garde toujours exécutée de l'égalité `document.json` ↔ parseur courant (voir le helper).

    L'égalité elle-même appartient au test de régénération, seul à disposer du PDF ; ce qui restait
    sans témoin, c'est la **divergence** : elle est désormais tolérée uniquement tant qu'elle est
    déclarée dans `docs/choix-et-limites.md`.
    """
    from tests.test_pdf_to_blocks import assert_empreinte_committee_declaree

    assert_empreinte_committee_declaree(DOC, doc.ingest_fingerprint)


def test_revue_3_1_records_the_measured_id_reassignment() -> None:
    """Revue 3.1 M1 : une réingestion idempotente ne doit pas effacer la preuve historique d'AD-2."""
    journal = (ROOT / "docs" / "tests-live.md").read_text("utf-8")
    assert "57 `block_id` de l’ancien artefact absents" in journal
    assert "31 identifiants conservés dont le texte a changé" in journal


@pytest.fixture(scope="module")
def regeneration() -> tuple[Document, dict, list, list, str]:
    """Réingestion du PDF réel, faite **une fois** pour les deux moitiés du certificat.

    La moitié « invariants du chemin PDF réel » est toujours exécutée dès que le PDF est là ; la
    moitié « comparaison aux artefacts committés » passe par la garde partagée. Les deux partagent
    la même extraction : la scinder ne doit pas la payer deux fois.
    """
    pdf = REAL / "source.pdf"
    assert pdf.is_file(), "source.pdf AXA requis par la porte de déploiement"
    source_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert source_hash == (REAL / "source.sha256").read_text("utf-8").strip()
    pages, toc = p.extract_pages(pdf)
    built, meta = p.build_document(pages, edition=p.DEFAULT_EDITION, source_hash=source_hash, toc=toc,
                                   source_url=(REAL / "source.url").read_text("utf-8").strip())
    return built, meta, pages, toc, source_hash


@pytest.mark.skipif(
    not (REAL / "source.pdf").is_file() and os.environ.get("REAL_PDF_TESTS_REQUIRED") != "1",
    reason="source.pdf absent (non committé)",
)
def test_real_pdf_invariants_de_surface_et_de_fidelite(
        regeneration: tuple[Document, dict, list, list, str]) -> None:
    """Moitié toujours exécutée : ce que le PDF réel prouve sans rien comparer à un golden.

    Ces invariants ne dépendent d'aucun artefact committé — ils ne peuvent donc pas être suspendus
    par une empreinte périmée, et c'est tout l'intérêt de les séparer de la comparaison.
    """
    built, meta, pages, _toc, _source_hash = regeneration
    assert p.anomalies_registre(pages, meta["source_uids"]) == []
    assert all(block.surface_class == "preliminaire" and not is_citable(block)
               for block in built.blocks if block.page == 1)
    assert all(block.surface_class == "table_des_matieres" and not is_citable(block)
               for block in built.blocks if block.page in {2, 3, 4})
    toc_node = next(node for node in built.nodes if node.node_id == f"{DOC}:tdm")
    assert toc_node.surface_class == "table_des_matieres" and toc_node.blocks
    terminal = [block for block in built.blocks if block.page == 109]
    assert terminal and all(block.surface_class == "preliminaire" and block.article_uid is None
                            and not is_citable(block) for block in terminal)
    assert all(block.block_id in next(node for node in built.nodes if node.node_id == DOC).blocks
               for block in terminal)
    tables_30_31 = "\n".join(block.text for block in built.blocks
                              if block.page in {30, 31} and block.structural_kind == "table")
    assert "notification" in tables_30_31 and "réflexion" in tables_30_31
    assert "notifciation" not in tables_30_31 and "réfelxion" not in tables_30_31
    table_91 = next(block.text for block in built.blocks
                    if block.page == 91 and block.structural_kind == "table")
    assert "2.500 €" in table_91 and "5.750 €" in table_91
    assert "2.500 r" not in table_91 and "5.750 r" not in table_91
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
def test_real_pdf_ce_que_la_porte_de_lecture_laisse_passer(
        regeneration: tuple[Document, dict, list, list, str]) -> None:
    """Vérité **régénérée** de la porte de lecture, indépendante de tout artefact committé.

    Ce contrat porte exactement une ligne tournée dans ses 109 pages — une mention d'édition en marge,
    jamais répétée. Elle doit donc être **conservée** : c'est le côté « pas de faux positif » de la
    règle du titre courant, et il n'est prouvé que sur un document qui en offre une. Il ne porte
    aucune ligne-glyphe et, sur ses trois pages à deux colonnes, aucune ligne de colonne n'est
    déclarée pleine largeur.
    """
    built, _meta, pages, _toc, _source_hash = regeneration
    assert mesure_de_la_porte(pages) == {
        "lignes_tournees_conservees": 1,
        "titres_courants_survivants": 0,
        "lignes_glyphes": 0,
        "lignes_pleine_largeur_hors_marge": 0,
        "pages_a_gouttiere": 3,
    }
    assert (len(built.blocks), len(built.nodes)) == (1400, 751)


@pytest.mark.skipif(
    not (REAL / "source.pdf").is_file() and os.environ.get("REAL_PDF_TESTS_REQUIRED") != "1",
    reason="source.pdf absent (non committé)",
)
def test_real_pdf_regenerates_committed_artefacts(
        doc: Document, regeneration: tuple[Document, dict, list, list, str]) -> None:
    """Moitié « comparaison » : elle ne rend jamais vert sans avoir comparé un golden."""
    from tests.test_pdf_to_blocks import exiger_comparaison_aux_artefacts_committes

    exiger_comparaison_aux_artefacts_committes(DOC, doc.ingest_fingerprint)
    built, meta, pages, toc, _source_hash = regeneration
    # `pdf_to_blocks` reconstruit exactement l'identité immuable. Les champs juridiques et les
    # scopes sont ajoutés ensuite par les deux lots Opus et ne doivent donc pas être comparés ici.
    identity = lambda block: (  # noqa: E731 - projection locale lisible dans les deux assertions
        block.block_id, block.text, block.lang, block.loc, block.seq, block.page, block.bbox,
        block.structural_kind, block.source_field, block.continues,
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
    report = canoniser_transition_apres_typage(report)
    report = attester_arbre(
        report,
        document_hash=hashlib.sha256((REAL / "document.json").read_bytes()).hexdigest(),
        ingest_fingerprint=doc.ingest_fingerprint,
    )
    committed = Report.model_validate_json((REAL / "report.json").read_bytes())
    assert report.checks == [check for check in committed.checks if check.name not in TYPING_CHECKS]
    assert_stats_structurelles_exactes(report, committed, TERMINAL_TYPING_STATS)
