"""Porte structurelle PDF : invariants automatiques et revue visuelle liée au rendu."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from server.app.domain import BlockRef, Check, Document, Node, NodeRef, Report, is_citable
from server.ingest import pdf_structure_gate as gate
from server.ingest.artifacts import document_json
from server.ingest.pdf_to_blocks import PageLine, PageText, SourceRegistry, build_document, extract_pages


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


@pytest.mark.parametrize(("target", "field", "value"), [
    ("node", "title", "Titre inventé"),
    ("node", "article_uid", "article-inventé"),
    ("node", "surface_class", "preliminaire"),
    ("node", "relations", [{"kind": "explicit_dependency", "target_node_id": "d:s2"}]),
    ("block", "article_uid", "article-inventé"),
    ("block", "surface_class", "preliminaire"),
    ("block", "continues", "d:p9:9"),
])
def test_projection_gate_refuse_toute_semantique_non_issue_de_la_structure_trusted(
        tmp_path: Path, target: str, field: str, value: object) -> None:
    doc_dir = _corpus(tmp_path)
    trusted = Document.model_validate_json((doc_dir / "document.json").read_bytes())
    candidate = trusted.model_copy(deep=True)
    if target == "node":
        source = candidate.nodes[-1]
        candidate.nodes[-1] = source.model_copy(update={field: value})
    else:
        source = candidate.blocks[-1]
        candidate.blocks[-1] = source.model_copy(update={field: value})

    issues = gate._projection_issues(trusted, candidate)

    assert any(field in issue and "trusted" in issue for issue in issues)


@pytest.mark.parametrize(("title", "article_uid", "surface", "expected"), [
    ("Sommaire", None, "substantiel", "table_des_matieres"),
    ("Préambule", None, "substantiel", "preliminaire"),
    ("Article 12 Garanties", "article:13", "substantiel", "article_uid"),
])
def test_gate_rejoue_oracle_semantique_sans_faire_confiance_a_la_structure_trusted(
        tmp_path: Path, title: str, article_uid: str | None, surface: str,
        expected: str) -> None:
    doc_dir = _corpus(tmp_path)
    document = Document.model_validate_json((doc_dir / "document.json").read_bytes())
    document.nodes.append(Node(
        node_id=f"{document.doc_id}:s1", level=1, title=title,
        article_uid=article_uid, surface_class=surface,
    ))

    issues = gate._semantic_issues(document)

    assert any(expected in issue for issue in issues)


@pytest.mark.parametrize(("lang", "signale"), [("es", True), ("fr", False)])
def test_gate_lit_le_vocabulaire_technique_dans_la_langue_du_document(
        tmp_path: Path, lang: str, signale: bool) -> None:
    """La porte applique la même règle que le vérificateur, sur la langue qu'elle a sous la main.

    « Índice » est le sommaire d'un contrat espagnol ; sur un contrat français, un titre qui
    commence par « Indice » nomme couramment la clause de revalorisation, du corps citable. Lire
    les sept vocabulaires faisait signaler la bonne réponse, ici comme au vérificateur. Le nœud ne
    porte aucun bloc : la langue est alors celle du document, le repli que la porte doit tenir.
    """
    doc_dir = _corpus(tmp_path)
    document = Document.model_validate_json((doc_dir / "document.json").read_bytes())
    document.lang = lang
    document.nodes.append(Node(
        node_id=f"{document.doc_id}:s1", level=1, title="Indice de revalorisation",
        surface_class="substantiel",
    ))

    issues = gate._semantic_issues(document)

    assert any("table_des_matieres" in issue for issue in issues) is signale


def test_gate_lit_lintervalle_dun_conteneur_et_non_ses_seuls_blocs_directs(
        tmp_path: Path) -> None:
    """Une section qui délègue tout son texte à ses sous-sections a bien du corps observable.

    Le vérificateur prouve la surface d'un nœud sur **les lignes de son intervalle**, descendants
    compris ; la porte ne lisait que les blocs attachés au nœud lui-même. Un conteneur dont le seul
    corps est celui de ses enfants — « Résiliation » : quatre sous-sections et rien en propre —
    était donc « sans preuve locale » ici et prouvé là. Sept nœuds de l'arbre réel tombaient dessus.
    """
    doc_dir = _corpus(tmp_path)
    document = Document.model_validate_json((doc_dir / "document.json").read_bytes())
    corps = document.blocks[0]
    conteneur, enfant = f"{document.doc_id}:c1", f"{document.doc_id}:c1.1"
    # Un intitulé nu : ni identité d'article, ni ponctuation finale. Sans texte observable, l'oracle
    # ferme le gate — c'est voulu, et c'est ce que le conteneur déclenchait à tort.
    document.nodes.append(Node(node_id=conteneur, level=1, title="Résiliation",
                               surface_class="substantiel", items=[NodeRef(node_id=enfant)]))
    document.nodes.append(Node(node_id=enfant, level=2, title="Résiliation par l'assureur",
                               surface_class="substantiel",
                               items=[BlockRef(block_id=corps.block_id)]))

    issues = gate._semantic_issues(document)

    assert not any(issue.startswith(f"{conteneur}:") for issue in issues), issues
    # L'étendue lue est celle de l'intervalle : le bloc du descendant en fait partie.
    assert gate._blocs_de_lintervalle(document, document.nodes[-2]) == [corps.block_id]


def test_gate_observe_la_classe_et_la_fidelite_dune_surface_preliminaire() -> None:
    registry = SourceRegistry()
    source = registry.add(page=1, text="Couverture fidèle", bbox=[56, 100, 250, 114])
    cover = PageText(page=1, width=595, height=842, lines=[
        PageLine(source.text, list(source.bbox), 12, source_uids=[source.uid]),
    ], source=registry)
    article = PageText(page=2, width=595, height=842, lines=[
        PageLine("1 Corps", [56, 100, 250, 114], 17, number="1"),
    ])
    document, meta = build_document(
        [cover, article], edition="2026", source_hash="0" * 64, toc=[],
        doc_id="contrat-synthetique", title="Contrat synthétique",
    )
    block = next(block for block in document.blocks if block.page == 1)

    assert block.surface_class == "preliminaire" and not is_citable(block)
    assert all(not issues for issues in gate._page_issues(cover, document, meta["source_uids"]).values())
    assert not any(
        block.block_id in issue and "classe divergente" in issue
        for issue in gate._semantic_issues(document)
    )


def test_gate_reconnait_un_titre_numerote_comme_identite_article_locale(tmp_path: Path) -> None:
    doc_dir = _corpus(tmp_path)
    document = Document.model_validate_json((doc_dir / "document.json").read_bytes())
    document.nodes.append(Node(
        node_id=f"{document.doc_id}:a12", level=1, title="12 Garanties",
        article_uid="article:12", surface_class="substantiel",
    ))

    issues = gate._semantic_issues(document)

    assert not any(f"{document.doc_id}:a12:" in issue for issue in issues)


def test_gate_ninvente_pas_darticle_pour_un_millesime_sans_identite_revendique(
        tmp_path: Path) -> None:
    doc_dir = _corpus(tmp_path)
    document = Document.model_validate_json((doc_dir / "document.json").read_bytes())
    node_id = f"{document.doc_id}:tarifs"
    document.nodes.append(Node(
        node_id=node_id, level=1, title="2024 Tarifs",
        article_uid=None, surface_class="substantiel",
    ))

    issues = gate._semantic_issues(document)

    assert not any(node_id in issue and "article_uid" in issue for issue in issues)


def test_gate_refuse_une_tdm_rattachee_a_la_racine_sans_noeud_navigable(tmp_path: Path) -> None:
    doc_dir = _corpus(tmp_path)
    document = Document.model_validate_json((doc_dir / "document.json").read_bytes())
    source = document.blocks[0]
    document.blocks[0] = source.model_copy(update={"surface_class": "table_des_matieres"})

    issues = gate._semantic_issues(document)

    assert any(source.block_id in issue and "classe divergente" in issue for issue in issues)
