"""pdf_to_blocks sur des PDF synthétiques (PyMuPDF) : matrice I/O de la spec 1.2, sans PDF réel."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pymupdf
import pytest

from tests.helpers_espace import poser_espace
from pydantic import ValidationError

from server.app.domain import (Block, BlockRef, Check, Document, Line, Node, NodeRef, Report,
                               is_citable)
from server.app.domain.document import Relation, Scope
from server.ingest import pdf_to_blocks as p
from server.ingest.artifacts import document_json
from server.ingest.report import _printed_toc_check, _quality, _tree_category, report_from_validation_error

DOC = "doc-a"
FONT_BODY, FONT_TITLE = "helv", "hebo"
ROOT = Path(__file__).resolve().parents[1]
# Marqueur déclaratif lu dans `docs/choix-et-limites.md` : « l'empreinte committée de ce document
# est périmée, et on le sait ». C'est la seule tolérance admise par la garde ci-dessous.
DECLARATION_EMPREINTE = re.compile(r"empreinte-committee-perimee:\s*([a-z0-9-]+)")


def empreintes_perimees_declarees() -> set[str]:
    """Les déclarations actives, hors registre historique repliable."""
    document = (ROOT / "docs" / "choix-et-limites.md").read_text("utf-8")
    synthese = document.partition("\n<details>\n")[0]
    return set(DECLARATION_EMPREINTE.findall(synthese))


def assert_empreinte_committee_declaree(doc_id: str, committee: str) -> None:
    """Garde **toujours exécutée** : une empreinte committée périmée est tolérée, mais **déclarée**.

    « Le parseur courant reproduirait cet artefact » ne se prouve qu'à la réingestion, qui exige les
    PDF réels — non committés, donc absents de ce dépôt : le test qui en portait l'égalité est
    `skipif`. Sans garde toujours jouée, la divergence n'avait plus aucun témoin : elle pouvait
    s'installer, s'aggraver ou s'étendre à un troisième document sans que rien ne rougisse.

    La tolérance est donc déclarative, et symétrique. Tant que le document est nommé dans
    `docs/choix-et-limites.md`, la divergence est un état publié. Une divergence **non** déclarée
    rougit. Et le jour où la réingestion rétablit l'égalité, c'est la déclaration devenue fausse qui
    rougit à son tour — pour qu'elle soit retirée, et non oubliée là pour toujours. Aucune empreinte
    n'est écrite en dur : les deux valeurs comparées sont lues, l'une dans l'artefact, l'autre au
    parseur courant.
    """
    courante = p.ingest_fingerprint()
    declaree = doc_id in empreintes_perimees_declarees()
    if committee == courante:
        assert not declaree, (
            f"{doc_id} : l'empreinte committée vaut désormais celle du parseur courant — retirer sa "
            f"déclaration « empreinte-committee-perimee » de docs/choix-et-limites.md")
    else:
        assert declaree, (
            f"{doc_id} : l'empreinte committée diverge du parseur courant sans être déclarée. "
            f"Réingérer avec le PDF réel, ou publier la limite dans docs/choix-et-limites.md")


def _page(doc: pymupdf.Document, items: list[tuple[float, float, str, float, str]], page_no: int) -> None:
    """items = (x, y, texte, taille, police) ; en-tête courant et numéro de page ajoutés."""
    page = doc.new_page(width=595, height=842)
    page.insert_text((56, 30), "DOC - CONDITIONS", fontsize=10, fontname=FONT_BODY)
    page.insert_text((530, 822), str(page_no), fontsize=9, fontname=FONT_BODY)
    for x, y, text, size, font in items:
        if text.startswith("•"):  # insert_text (base-14) dégrade « • » en « · » ; TextWriter conserve la puce
            tw = pymupdf.TextWriter(page.rect)
            tw.append((x, y), text, font=pymupdf.Font(font), fontsize=size)
            tw.write_text(page)
        else:
            page.insert_text((x, y), text, fontsize=size, fontname=font)


def _article(items: list, y: float, numero: str, title: str, *, size: float = 13.0) -> float:
    items.append((56, y, numero, size, FONT_TITLE))
    items.append((122, y, title, size, FONT_TITLE))
    return y + 26


def _para(items: list, y: float, lines: list[str]) -> float:
    for t in lines:
        items.append((122, y, t, 10.0, FONT_BODY))
        y += 13
    return y + 11


def _fake_lines(registry, page_no: int, items: list[tuple[str, list[float], float]]) -> list:
    """Double d'extraction : il alimente le registre comme le vrai `_raw_lines` (story 4.2c)."""
    out = []
    for text, bbox, size in items:
        uids = [registry.add(page=page_no, text=text, bbox=bbox).uid] if registry is not None else []
        out.append(p.PageLine(text, bbox, size, source_uids=uids))
    return out


def _write_sha(path: Path) -> None:
    """`source.sha256` obligatoire (AD-7, revue Codex 1.2) : l'ingestion refuse un PDF sans empreinte de référence."""
    path.with_name("source.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n", "utf-8")


def build_pdf(path: Path, *, pages: list[list], image_page: bool = False, drawing_page: bool = False,
              toc: list | None = None, sha: bool = True) -> Path:
    doc = pymupdf.open()
    for i, items in enumerate(pages, start=1):
        _page(doc, items, i)
    if image_page:
        page = doc.new_page(width=595, height=842)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.clear_with(120)
        page.insert_image(pymupdf.Rect(100, 100, 300, 300), pixmap=pix)
    if drawing_page:
        page = doc.new_page(width=595, height=842)
        page.draw_rect(pymupdf.Rect(100, 100, 300, 300), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    if toc:
        doc.set_toc(toc)
    doc.save(str(path))
    if sha:
        _write_sha(path)
    return path


def _save(doc: pymupdf.Document, path: Path) -> None:
    doc.save(str(path))
    _write_sha(path)


def nominal_pages() -> list[list]:
    p1: list = []
    y = _article(p1, 80, "1", "Lexique", size=17)
    y = _article(p1, y, "1.1", "Contenu")
    y = _para(p1, y, ["Ensemble des biens qui se trouvent dans le batiment.", "Il comprend les rubriques suivantes :"])
    y = _article(p1, y, "1.1.1", "le mobilier ;", size=10)
    y = _article(p1, y, "3", "Garanties", size=17)
    y = _article(p1, y, "3.1", "Garanties de base")
    y = _article(p1, y, "3.1.1", "Incendie", size=12)
    y = _article(p1, y, "3.1.1.1.6", "Les degats occasionnes par un evenement soudain, resultant", size=10)
    y = _para(p1, y - 24 + 13, ["de la chaleur, meme sans embrasement."])
    y = _article(p1, y, "3.1.8", "Extension", size=12)
    y = _para(p1, y, ["Les presentes conditions sont applicables par extension aux garanties", "souscrites et la couverture est acquise aux endroits"])
    p2: list = []
    y = _para(p2, 80, ["precises ci-apres pour autant que l'evenement ne tombe pas sous une exclusion."])
    y = _article(p2, y, "3.1.8.3", "La residence de villegiature", size=10)
    y = _para(p2, y, ["Les degats materiels accidentels causes par un Assure."])
    y = _article(p2, y, "4", "Responsabilite", size=17)
    return [p1, p2]


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    build_pdf(d / "source.pdf", pages=nominal_pages())
    (d / "source.url").write_text("https://example.test/contrat.pdf\n", "utf-8")
    return d


def _poser_la_disposition(tmp_path: Path, *doc_dirs: Path) -> None:
    """La disposition du `data-dir` d'un test, posée comme un opérateur la pose (story 4.5, N3).

    Un entrypoint de production — ici `pdf_to_blocks.main` — exige une racine **installée** et
    refuse avant toute extraction sinon : un `data-dir` custom non installé n'offre aucune opération
    tout-ou-rien. Les tests qui appellent `run()` directement exercent la primitive interne et n'ont
    donc rien à poser.
    """
    cibles = [dossier.relative_to(tmp_path) / nom
              for dossier in doc_dirs
              for nom in ("document.json", "summary.md", "report.json")]
    poser_espace(tmp_path, cibles=cibles)


def _run(d: Path):
    source_url = d / "source.url"
    if not source_url.exists():
        source_url.write_text("https://example.test/contrat.pdf\n", "utf-8")
    return p.run(d, edition="test 2026", doc_id=DOC, title="Contrat synthétique")


def test_nominal_tree_ids_bbox_scopes_and_continues(data: Path) -> None:
    report, entry = _run(data)
    assert not report.blocking and entry.status == "servi" and entry.gate is None
    doc = Document.model_validate_json((data / "document.json").read_bytes())
    assert doc.kind == "contrat" and doc.title == "Contrat synthétique" and doc.edition == "test 2026"
    assert doc.source_url == "https://example.test/contrat.pdf" and doc.ingest_fingerprint == p.ingest_fingerprint()
    by = {n.node_id: n for n in doc.nodes}
    parent = {c: n.node_id for n in doc.nodes for c in n.children}
    assert parent[f"{DOC}:a3.1.1.1.6"] == f"{DOC}:a3.1.1"  # préfixe le plus long existant (3.1.1.1 absent)
    assert parent[f"{DOC}:a1.1.1"] == f"{DOC}:a1.1" and parent[f"{DOC}:a4"] == DOC
    # Le parseur ne déduit aucune portée sémantique d'un numéro d'article ; `type_clauses` est
    # l'unique écrivain de `Node.scope.kind` après résolution des signaux structurels.
    assert {node.scope.kind for node in doc.nodes} == {"commun"}
    assert by[f"{DOC}:a3.1.1.1.6"].level == 5 and by[f"{DOC}:a1.1"].title == "1.1 Contenu"
    for b in doc.blocks:
        assert b.loc == f"p{b.page}" and b.lines and b.block_id == f"{DOC}:{b.loc}:{b.seq}"
        assert b.bbox == [min(l.bbox[0] for l in b.lines), min(l.bbox[1] for l in b.lines),
                          max(l.bbox[2] for l in b.lines), max(l.bbox[3] for l in b.lines)]
        assert b.text == "\n".join(l.text for l in b.lines)
        assert "DOC - CONDITIONS" not in b.text and b.text not in ("1", "2")
    seqs = [b.seq for b in doc.blocks if b.page == 1]
    assert seqs == list(range(1, len(seqs) + 1))
    # scission de page : la suite (minuscule initiale) du dernier paragraphe de la p. 1 ouvre la p. 2
    first_p2 = next(b for b in doc.blocks if b.page == 2 and b.seq == 1)
    last_p1 = [b for b in doc.blocks if b.page == 1][-1]
    assert first_p2.continues == last_p1.block_id and first_p2.text.startswith("precises")
    assert last_p1.text.startswith("Les presentes") and report.stats["continues"] == 1
    # numéro + texte sur la même ligne de base = une seule Line ; alinéa numéré poursuivi par une minuscule
    garantie = doc.block(by[f"{DOC}:a3.1.1.1.6"].blocks[0])
    assert garantie.kind == "para" and garantie.lines[0].text.startswith("3.1.1.1.6 Les degats") and len(garantie.lines) == 2
    assert doc.block(by[f"{DOC}:a1.1.1"].blocks[0]).kind == "para"
    assert report.stats["pages"] == 2 and report.stats["en_tetes_retires"] == 4
    assert (data / "summary.md").read_text("utf-8").count("`") >= 2
    manifest = json.loads((data.parent / "manifest.json").read_text("utf-8"))[DOC]
    assert manifest["document_hash"] == hashlib.sha256((data / "document.json").read_bytes()).hexdigest()


def test_summary_respects_summary_max_level(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # revue P5 : le PDF synthétique a des nœuds de niveaux 1 à 5 (a4, a1.1, a3.1.1, a3.1.8.3, a3.1.1.1.6).
    _run(data)
    summary = (data / "summary.md").read_text("utf-8")
    assert f"`{DOC}:a4`" in summary and f"`{DOC}:a1.1`" in summary  # niveaux 1 et 2 présents
    assert f"`{DOC}:a3.1.1`" not in summary and f"`{DOC}:a3.1.1.1.6`" not in summary  # niveau > 2 exclus
    doc = Document.model_validate_json((data / "document.json").read_bytes())
    monkeypatch.setenv("SUMMARY_MAX_LEVEL", "3")
    p.get_settings.cache_clear()
    try:
        deeper = p.build_summary(doc)
    finally:
        p.get_settings.cache_clear()
    assert f"`{DOC}:a3.1.1`" in deeper and f"`{DOC}:a3.1.1.1.6`" not in deeper


def test_two_runs_are_byte_identical(data: Path) -> None:
    _run(data)
    first = {n: (data / n).read_bytes() for n in ("document.json", "report.json", "summary.md")}
    first["manifest"] = (data.parent / "manifest.json").read_bytes()
    report, _ = _run(data)
    assert all(c.name != "ids_disparus" for c in report.checks)
    assert first == {**{n: (data / n).read_bytes() for n in ("document.json", "report.json", "summary.md")},
                     "manifest": (data.parent / "manifest.json").read_bytes()}


def test_page_with_image_but_no_text_is_blocking(tmp_path: Path) -> None:
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    build_pdf(d / "source.pdf", pages=nominal_pages(), image_page=True)
    (d / "source.url").write_text("https://example.test/contrat.pdf\n", "utf-8")
    _poser_la_disposition(tmp_path, d)
    assert p.main([DOC, "--data", str(d.parent), "--title", "Contrat synthétique",
                   "--edition", "test 2026"]) == 1
    report = json.loads((d / "report.json").read_text("utf-8"))
    check = next(c for c in report["checks"] if c["name"] == "page_sans_texte")
    assert check["level"] == "bloquant" and "3" in check["detail"]
    assert "Tesseract" in check["detail"] and "fra" in check["detail"]
    assert report["stats"]["pages_ocr_echec"] == 1
    assert json.loads((d.parent / "manifest.json").read_text("utf-8"))[DOC]["status"] == "quarantaine"
    assert not (d / "document.json").exists() and not (d / "summary.md").exists()


def test_blank_page_is_only_info(tmp_path: Path) -> None:
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    doc = pymupdf.open()
    _page(doc, [(56, 80, "1", 17.0, FONT_TITLE), (122, 80, "Lexique", 17.0, FONT_TITLE)], 1)
    doc.new_page()
    _save(doc, d / "source.pdf")
    report, entry = _run(d)
    assert entry.status == "servi" and report.stats["pages_blanches"] == 1 and report.stats["pages_sans_texte"] == "2"
    assert all(c.level != "alerte" for c in report.checks)


def test_page_with_only_vector_drawings_and_no_text_is_blocking(tmp_path: Path) -> None:
    """AD-8 : une page dessinée (tracés vectoriels) sans texte n'est pas blanche (revue Codex 1.2, B3)."""
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    build_pdf(d / "source.pdf", pages=nominal_pages(), drawing_page=True)
    report, entry = _run(d)
    check = next(c for c in report.checks if c.name == "page_sans_texte")
    assert check.level == "bloquant" and "3" in check.detail and entry.status == "quarantaine"
    assert report.stats["pages_blanches"] == 0 and report.stats["pages_sans_texte_dessinees"] == 1


def test_drawn_page_whose_only_lines_are_a_removed_header_is_blocking(tmp_path: Path) -> None:
    """AD-8 : page dont les seules lignes sont l'en-tête/pied retirés + un tracé vectoriel ⇒ bloquante, pas blanche
    (revue Codex 1.2, B3 tour 2)."""
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    doc = pymupdf.open()
    _page(doc, [(56, 80, "1", 17.0, FONT_TITLE), (122, 80, "Lexique", 17.0, FONT_TITLE)], 1)
    _page(doc, [], 2)  # en-tête courant + numéro de page seulement, puis un rectangle
    doc[1].draw_rect(pymupdf.Rect(100, 100, 300, 300), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    _save(doc, d / "source.pdf")
    report, entry = _run(d)
    assert report.stats["en_tetes_retires"] == 4 and report.stats["pages_sans_texte"] == "2"
    check = next(c for c in report.checks if c.name == "page_sans_texte")
    assert check.level == "bloquant" and "2" in check.detail and entry.status == "quarantaine"
    assert report.stats["pages_blanches"] == 0 and report.stats["pages_sans_texte_dessinees"] == 1


def test_mixed_text_and_image_page_is_an_alert(tmp_path: Path) -> None:
    """AD-8 : pages mixtes texte/image en alerte, document servi (revue Codex 1.2, B4)."""
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    doc = pymupdf.open()
    _page(doc, [(56, 80, "1", 17.0, FONT_TITLE), (122, 80, "Lexique", 17.0, FONT_TITLE)], 1)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
    pix.clear_with(120)
    doc[0].insert_image(pymupdf.Rect(50, 250, 545, 700), pixmap=pix)
    _save(doc, d / "source.pdf")
    report, entry = _run(d)
    check = next(c for c in report.checks if c.name == "pages_mixtes")
    assert check.level == "alerte" and check.detail.endswith(": 1") and entry.status == "servi"


def test_small_logo_stays_below_mixed_page_density(tmp_path: Path) -> None:
    """P3 : une image sous le seuil configuré ne fabrique pas une page mixte."""
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    doc = pymupdf.open()
    _page(doc, [(56, 80, "1", 17.0, FONT_TITLE), (122, 80, "Lexique", 17.0, FONT_TITLE)], 1)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
    pix.clear_with(120)
    doc[0].insert_image(pymupdf.Rect(500, 50, 540, 90), pixmap=pix)
    _save(doc, d / "source.pdf")
    report, entry = _run(d)
    assert entry.status == "servi" and all(check.name != "pages_mixtes" for check in report.checks)


def test_table_is_one_atomic_block_and_its_cells_are_not_duplicated(tmp_path: Path) -> None:
    """P1 : une rangée par ligne, cellules séparées par ` | `, aucun doublon en paragraphe."""
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((56, 70), "1", fontsize=17, fontname=FONT_TITLE)
    page.insert_text((122, 70), "Garanties", fontsize=17, fontname=FONT_TITLE)
    for x in (50, 200, 350):
        page.draw_line((x, 100), (x, 180))
    for y in (100, 140, 180):
        page.draw_line((50, y), (350, y))
    for x, y, text in ((60, 125, "Garantie"), (210, 125, "Plafond"),
                       (60, 165, "Incendie"), (210, 165, "")):
        if text:
            page.insert_text((x, y), text, fontsize=10, fontname=FONT_BODY)
    _save(doc, d / "source.pdf")
    report, _ = _run(d)
    built = Document.model_validate_json((d / "document.json").read_bytes())
    tables = [block for block in built.blocks if block.kind == "table"]
    assert len(tables) == 1 and tables[0].text == "Garantie | Plafond\nIncendie | "
    assert sum("Incendie" in block.text for block in built.blocks) == 1
    assert report.stats["tables"] == 1


def test_table_rows_keep_their_source_geometry() -> None:
    """P1 : les lignes d'une table gardent les boîtes réelles, même si leurs hauteurs diffèrent."""
    table = p.PageTable(
        [50, 100, 350, 190],
        [["entête"], ["cellule multiligne"]],
        row_bboxes=[[50, 100, 350, 125], [50, 125, 350, 190]],
    )
    groups = p._segment_page(p.PageText(page=1, width=595, height=842, tables=[table]))
    assert len(groups) == 1 and groups[0][0] == "table"
    assert [line.bbox for line in groups[0][1]] == table.row_bboxes


def test_empty_table_is_ignored_and_only_line_center_inside_excludes_text() -> None:
    class EmptyTable:
        bbox = (10, 10, 50, 50)

        @staticmethod
        def extract():
            return [[None, ""], ["  ", None]]

    class TablePage:
        @staticmethod
        def find_tables():
            return type("Found", (), {"tables": [EmptyTable()]})()

    assert p._tables(TablePage()) == []

    class TextPage:
        @staticmethod
        def get_text(kind, **options):
            assert kind == "dict"
            return {"blocks": [{"type": 0, "lines": [{
                "bbox": [0, 0, 100, 10],
                "spans": [{"text": "ligne chevauchée", "font": "helv", "size": 10}],
            }]}]}

    lines, _ = p._raw_lines(TextPage(), tables=[p.PageTable([95, 0, 110, 10], [["cellule"]])])
    assert [line.text for line in lines] == ["ligne chevauchée"]  # légende qui ne fait que frôler la table
    lines, _ = p._raw_lines(TextPage(), tables=[p.PageTable([40, 0, 110, 10], [["cellule"]])])
    assert lines == []  # texte de cellule atomique : centre contenu, donc pas de doublon


def test_overlapping_images_use_their_union_not_the_sum() -> None:
    class ImagePage:
        rect = pymupdf.Rect(0, 0, 100, 100)

        @staticmethod
        def get_image_info():
            return [{"bbox": [0, 0, 50, 100]}, {"bbox": [25, 0, 75, 100]}]

    assert p._image_area_ratio(ImagePage()) == pytest.approx(0.75)


def test_targeted_ocr_is_marked_and_preserves_geometry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P2 : seul le chemin sans texte natif appelle l'OCR ; les blocs produits sont marqués."""
    d = tmp_path / "source.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
    pix.clear_with(120)
    page.insert_image(page.rect, pixmap=pix)
    doc.save(d)
    native = p._raw_lines

    def fake_raw(page, *, page_no=1, textpage=None, tables=None, registry=None):
        if textpage is None:
            return [], 1
        return _fake_lines(registry, page_no, [("1", [56, 80, 70, 96], 17),
                                               ("Lexique OCR", [122, 80, 210, 96], 17)]), 1

    calls: list[dict] = []

    def fake_ocr(self, **kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setenv("OCR_DPI", "288")
    p.get_settings.cache_clear()
    monkeypatch.setattr(p, "_raw_lines", fake_raw)
    monkeypatch.setattr(pymupdf.Page, "get_textpage_ocr", fake_ocr)
    try:
        pages, toc = p.extract_pages(d)
    finally:
        monkeypatch.setattr(p, "_raw_lines", native)
        p.get_settings.cache_clear()
    built, meta = p.build_document(pages, edition="test", source_hash="0" * 64, toc=toc,
                                   doc_id=DOC, title="Contrat synthétique")
    assert pages[0].ocr_attempted and pages[0].ocr_succeeded
    assert calls == [{"language": "fra", "dpi": 288, "full": True}]
    assert pages[0].native_text is False
    assert built.blocks[0].source_field == "ocr" and built.blocks[0].page == 1 and built.blocks[0].bbox
    report = p.build_pdf_report(built, None, pages=pages, numbers=meta["numbers"],
                                duplicates=meta["duplicates"], continues=meta["continues"], toc=toc,
                                printed_toc=meta["printed_toc"])
    assert next(check for check in report.checks if check.name == "ocr_cible").level == "info"
    assert all(check.name != "pages_mixtes" for check in report.checks)


def test_visual_page_ocr_runs_after_native_band_removal_and_empty_result_keeps_diagnostic(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "source.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(pymupdf.Rect(100, 100, 300, 300), fill=(0.5, 0.5, 0.5))
    doc.save(pdf)

    def fake_raw(page, *, page_no=1, textpage=None, tables=None, registry=None):
        if textpage is None:
            return _fake_lines(registry, page_no, [("EN-TÊTE", [56, 10, 150, 24], 9)]), 0
        return _fake_lines(registry, page_no, [("1", [530, 810, 540, 824], 9)]), 0

    monkeypatch.setattr(p, "_raw_lines", fake_raw)
    monkeypatch.setattr(pymupdf.Page, "get_textpage_ocr", lambda self, **kwargs: object())
    pages, toc = p.extract_pages(pdf)
    assert pages[0].native_text is False
    assert pages[0].ocr_attempted and not pages[0].ocr_succeeded and pages[0].lines == []
    assert pages[0].ocr_error == "sortie_vide"
    built, meta = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=toc,
                                   doc_id=DOC, title="Contrat")
    report = p.build_pdf_report(built, None, pages=pages, numbers=meta["numbers"],
                                duplicates=meta["duplicates"], continues=meta["continues"], toc=toc)
    assert all(check.name != "ocr_cible" for check in report.checks)
    assert next(check for check in report.checks if check.name == "page_sans_texte").level == "bloquant"


def test_ocr_exception_report_never_exposes_local_engine_paths(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue 3.1 M2 : le rapport public ne recopie jamais le message brut du moteur OCR."""
    pdf = tmp_path / "source.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(pymupdf.Rect(100, 100, 300, 300), fill=(0.5, 0.5, 0.5))
    doc.save(pdf)

    monkeypatch.setattr(
        pymupdf.Page,
        "get_textpage_ocr",
        lambda self, **kwargs: (_ for _ in ()).throw(
            RuntimeError("No OCR support: /Users/alice/private/tessdata/fra.traineddata"),
        ),
    )
    pages, toc = p.extract_pages(pdf)
    built, meta = p.build_document(
        pages, edition="2026", source_hash="0" * 64, toc=toc, doc_id=DOC, title="Contrat",
    )
    report = p.build_pdf_report(
        built, None, pages=pages, numbers=meta["numbers"], duplicates=meta["duplicates"],
        continues=meta["continues"], toc=toc,
    )
    detail = next(check.detail for check in report.checks if check.name == "page_sans_texte")
    assert "RuntimeError" in detail and "moteur_absent" in detail
    assert "/Users/alice" not in detail and "tessdata/fra.traineddata" not in detail


def test_contract_without_numeric_articles_keeps_its_content_citable() -> None:
    pages = [p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("Garantie incendie sans numérotation", [56, 100, 350, 114], 10),
        p.PageLine("Les dommages sont couverts.", [56, 115, 350, 129], 10),
    ])]
    document, _ = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[],
                                   doc_id=DOC, title="Contrat sans articles")
    assert document.blocks and all(block.kind != "autre" for block in document.blocks)
    assert "Garantie incendie" in document.blocks[0].text


def test_preliminary_groups_before_first_article_on_same_page_are_separate() -> None:
    pages = [p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("Avertissement préliminaire", [56, 80, 300, 94], 10),
        p.PageLine("1 Garanties", [56, 130, 250, 146], 17, number="1"),
        p.PageLine("La couverture commence ici.", [56, 165, 350, 179], 10),
    ])]
    document, _ = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[],
                                   doc_id=DOC, title="Contrat")
    assert document.blocks[0].kind == "autre" and document.blocks[0].text == "Avertissement préliminaire"
    article = next(node for node in document.nodes if node.node_id == f"{DOC}:a1")
    assert article.blocks == [block.block_id for block in document.blocks[1:]]


def test_an_aligned_closing_paragraph_returns_to_the_numbered_parent() -> None:
    """T4 : dé-indentation par arbre + géométrie, sans numéro ni mots propres au contrat."""
    page = p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("1.4 Catégorie", [56, 60, 210, 74], 17, number="1.4"),
        p.PageLine("Le groupe comprend les items suivants :", [122, 90, 390, 104], 10),
        p.PageLine("1.4.1 premier item ;", [56, 125, 250, 139], 10, number="1.4.1"),
        p.PageLine("1.4.2 second item ;", [56, 160, 250, 174], 10, number="1.4.2"),
        p.PageLine("1.4.3 dernier item.", [56, 195, 250, 209], 10, number="1.4.3"),
        p.PageLine("Le groupe ne comprend pas :", [122, 230, 330, 244], 10),
        p.PageLine("• la valeur écartée.", [122, 247, 310, 261], 10, bullet=True),
        p.PageLine("1.5 Suite", [56, 285, 180, 299], 17, number="1.5"),
    ])
    document, _ = p.build_document(
        [page], edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC, title="Contrat",
    )
    parent = next(node for node in document.nodes if node.node_id == f"{DOC}:a1.4")
    leaf = next(node for node in document.nodes if node.node_id == f"{DOC}:a1.4.3")
    assert [document.block(block_id).text for block_id in leaf.blocks] == ["1.4.3 dernier item."]
    assert [document.block(block_id).text for block_id in parent.blocks][-2:] == [
        "Le groupe ne comprend pas :", "• la valeur écartée.",
    ]


def test_an_aligned_body_stays_under_a_child_that_has_a_later_sibling() -> None:
    page = p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("1 Parent", [56, 50, 180, 64], 17, number="1"),
        p.PageLine("Corps du parent.", [122, 80, 280, 94], 10),
        p.PageLine("1.1 Premier", [56, 115, 180, 129], 10, number="1.1"),
        p.PageLine("1.2 Deuxième", [56, 150, 190, 164], 10, number="1.2"),
        p.PageLine("Corps aligné du deuxième.", [122, 185, 330, 199], 10),
        p.PageLine("1.3 Troisième", [56, 220, 190, 234], 10, number="1.3"),
    ])
    document, _ = p.build_document(
        [page], edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC, title="Contrat",
    )
    parent = next(node for node in document.nodes if node.node_id == f"{DOC}:a1")
    child = next(node for node in document.nodes if node.node_id == f"{DOC}:a1.2")
    assert "Corps aligné du deuxième." in [document.block(block_id).text
                                            for block_id in child.blocks]
    assert "Corps aligné du deuxième." not in [document.block(block_id).text
                                                for block_id in parent.blocks]


def test_a_colon_terminated_starter_never_dedents_its_body() -> None:
    page = p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("2 Parent", [56, 50, 180, 64], 17, number="2"),
        p.PageLine("Corps du parent.", [122, 80, 280, 94], 10),
        p.PageLine("2.1 Premier", [56, 115, 180, 129], 10, number="2.1"),
        p.PageLine("2.2 Éléments :", [56, 150, 210, 164], 10, number="2.2"),
        p.PageLine("Corps annoncé.", [122, 185, 260, 199], 10),
    ])
    document, _ = p.build_document(
        [page], edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC, title="Contrat",
    )
    child = next(node for node in document.nodes if node.node_id == f"{DOC}:a2.2")
    assert [document.block(block_id).text for block_id in child.blocks][-1] == "Corps annoncé."


def test_a_starter_over_the_line_limit_never_dedents_its_body() -> None:
    page = p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("3 Parent", [56, 50, 180, 64], 17, number="3"),
        p.PageLine("Corps du parent.", [122, 80, 280, 94], 10),
        p.PageLine("3.1 Premier", [56, 115, 180, 129], 10, number="3.1"),
        p.PageLine("3.2 élément étendu,", [56, 150, 240, 164], 10, number="3.2"),
        p.PageLine("qui continue sur une ligne", [70, 166, 280, 180], 10),
        p.PageLine("puis sur une troisième ligne.", [70, 182, 300, 196], 10),
        p.PageLine("Corps aligné après le long item.", [122, 235, 360, 249], 10),
    ])
    document, _ = p.build_document(
        [page], edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC, title="Contrat",
    )
    child = next(node for node in document.nodes if node.node_id == f"{DOC}:a3.2")
    assert [document.block(block_id).text for block_id in child.blocks][-1] == \
        "Corps aligné après le long item."


def test_a_node_with_an_existing_body_never_dedents_a_later_paragraph() -> None:
    page = p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("4 Parent", [56, 50, 180, 64], 17, number="4"),
        p.PageLine("Corps du parent.", [122, 80, 280, 94], 10),
        p.PageLine("4.1 Premier", [56, 115, 180, 129], 10, number="4.1"),
        p.PageLine("4.2 Second", [56, 150, 190, 164], 10, number="4.2"),
        p.PageLine("Premier corps propre.", [70, 185, 270, 199], 10),
        p.PageLine("Corps aligné plus tard.", [122, 235, 330, 249], 10),
    ])
    document, _ = p.build_document(
        [page], edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC, title="Contrat",
    )
    child = next(node for node in document.nodes if node.node_id == f"{DOC}:a4.2")
    assert [document.block(block_id).text for block_id in child.blocks][-2:] == [
        "Premier corps propre.", "Corps aligné plus tard.",
    ]


def test_an_ordinary_first_child_body_is_not_dedented_on_alignment_alone() -> None:
    page = p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("2 Parent", [56, 60, 180, 74], 17, number="2"),
        p.PageLine("Corps du parent.", [122, 90, 280, 104], 10),
        p.PageLine("2.1 Enfant", [56, 125, 190, 139], 17, number="2.1"),
        p.PageLine("Corps propre de l'enfant.", [122, 160, 320, 174], 10),
    ])
    document, _ = p.build_document(
        [page], edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC, title="Contrat",
    )
    child = next(node for node in document.nodes if node.node_id == f"{DOC}:a2.1")
    assert [document.block(block_id).text for block_id in child.blocks] == [
        "2.1 Enfant", "Corps propre de l'enfant.",
    ]


@pytest.mark.parametrize("is_toc", [False, True])
def test_table_stays_atomic_in_preliminary_and_toc_pages(is_toc: bool) -> None:
    table = p.PageTable([50, 100, 350, 180], [["Garantie", "Plafond"], ["Incendie", "100"]])
    first = p.PageText(page=1, width=595, height=842, tables=[table], is_toc=is_toc)
    article = p.PageText(page=2, width=595, height=842,
                         lines=[p.PageLine("1 Garanties", [56, 80, 250, 96], 17, number="1")])
    document, _ = p.build_document([first, article], edition="2026", source_hash="0" * 64, toc=[],
                                   doc_id=DOC, title="Contrat")
    page_one = [block for block in document.blocks if block.page == 1]
    assert len(page_one) == 1 and page_one[0].kind == "table"
    assert page_one[0].source_field == ("tdm" if is_toc else "preliminaire")


def test_toc_title_in_table_continues_until_article_and_body_mention_does_not_trigger() -> None:
    body_mention = p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("Consultez la table des matières pour vous orienter.", [56, 100, 450, 114], 10),
    ])
    title_table = p.PageText(page=2, width=595, height=842, tables=[
        p.PageTable([50, 80, 350, 160], [["Table des matières"], ["1", "Lexique", "4"]]),
    ])
    continuation = p.PageText(page=3, width=595, height=842, lines=[
        p.PageLine("2 Garanties 12", [56, 100, 250, 114], 10),
    ])
    article = p.PageText(page=4, width=595, height=842, lines=[
        p.PageLine("1 Lexique", [56, 100, 250, 116], 17, number="1"),
    ])
    pages = [body_mention, title_table, continuation, article]
    p._mark_toc_pages(pages)
    assert [page.is_toc for page in pages] == [False, True, True, False]


@pytest.mark.parametrize("title", ["Table des matières :", "TABLE DES MATIÈRES — suite", "Contrat : table des matières"])
def test_toc_title_accepts_generic_punctuation_and_suffix_variants(title: str) -> None:
    assert p._is_toc_title(title)
    assert not p._is_toc_title("Le corps mentionne la table des matières pour information.")


def test_structural_sommaire_without_literal_title_marks_toc_and_keeps_unnumbered_body_citable() -> None:
    """Revue 3.1 B1 : la géométrie d'une TdM suffit, même sans le littéral « table des matières »."""
    pages = [
        p.PageText(page=1, width=595, height=842, lines=[
            p.PageLine("CONDITIONS GÉNÉRALES", [56, 100, 320, 118], 17),
        ]),
        p.PageText(page=2, width=595, height=842, lines=[
            p.PageLine("Sommaire", [56, 70, 220, 88], 17),
            p.PageLine("Garanties ................................ 5", [56, 110, 500, 124], 10),
            p.PageLine("Exclusions .............................. 9", [56, 132, 500, 146], 10),
        ]),
        p.PageText(page=3, width=595, height=842, lines=[
            p.PageLine("Franchises ............................. 12", [56, 90, 500, 104], 10),
        ]),
        p.PageText(page=4, width=595, height=842, lines=[
            p.PageLine("GARANTIES", [56, 80, 260, 98], 17),
            p.PageLine("Les dommages causés par incendie sont couverts.", [56, 120, 460, 134], 10),
        ]),
    ]

    p._mark_toc_pages(pages)
    assert [page.is_toc for page in pages] == [False, True, True, False]
    assert [page.after_toc for page in pages] == [False, False, False, True]

    document, meta = p.build_document(
        pages, edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC, title="Contrat sans articles",
    )
    cover = next(block for block in document.blocks if block.page == 1)
    body = [block for block in document.blocks if block.page == 4]
    assert cover.kind == "autre"
    assert body and all(p.is_citable(block) for block in body)
    assert meta["printed_toc"] == []  # TdM non numérotée : détectée, mais aucun numéro n'est inventé.
    report = p.build_pdf_report(
        document, None, pages=pages, numbers=meta["numbers"], duplicates=meta["duplicates"],
        continues=meta["continues"], toc=[], printed_toc=meta["printed_toc"],
    )
    check = next(check for check in report.checks if check.name == "tdm_imprimee")
    assert check.level == "alerte" and "aucune entrée numérotée exploitable" in check.detail


def test_removed_toc_header_cannot_rearm_toc_after_body_started() -> None:
    """Revue 3.1 B2 : une bande retirée répétée ne peut plus faire disparaître le corps du rappel."""
    removed = ["CONTRAT - TABLE DES MATIÈRES"]
    pages = [
        p.PageText(page=1, width=595, height=842, removed=removed.copy(), lines=[
            p.PageLine("1 Garanties 4", [56, 100, 500, 114], 10),
            p.PageLine("2 Exclusions 8", [56, 122, 500, 136], 10),
        ]),
        p.PageText(page=2, width=595, height=842, removed=removed.copy(), lines=[
            p.PageLine("1 Garanties", [56, 80, 250, 96], 17, number="1"),
        ]),
        p.PageText(page=3, width=595, height=842, removed=removed.copy(), lines=[
            p.PageLine("Sont exclus les dommages causés par un animal appartenant à l'assuré.",
                       [56, 100, 520, 114], 10),
            p.PageLine("La franchise reste de 250 euros.", [56, 116, 380, 130], 10),
        ]),
    ]

    p._mark_toc_pages(pages)
    assert [page.is_toc for page in pages] == [True, False, False]
    assert pages[2].after_toc
    document, _ = p.build_document(
        pages, edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC, title="Contrat",
    )
    clauses = [block for block in document.blocks if block.page == 3]
    assert clauses and all(p.is_citable(block) for block in clauses)


def test_toc_continuation_stops_on_unnumbered_body_and_body_before_first_article_is_citable() -> None:
    pages = [
        p.PageText(page=1, width=595, height=842, lines=[
            p.PageLine("Table des matières", [56, 80, 250, 94], 14),
            p.PageLine("1 Garanties 4", [56, 110, 250, 124], 10),
        ]),
        p.PageText(page=2, width=595, height=842, lines=[
            p.PageLine("Cette introduction explique les garanties applicables.", [56, 80, 420, 94], 10),
        ]),
        p.PageText(page=3, width=595, height=842, lines=[
            p.PageLine("1 Garanties", [56, 80, 250, 96], 17, number="1"),
        ]),
    ]
    p._mark_toc_pages(pages)
    assert [page.is_toc for page in pages] == [True, False, False]
    assert [page.after_toc for page in pages] == [False, True, True]
    document, _ = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[],
                                   doc_id=DOC, title="Contrat")
    intro = next(block for block in document.blocks if block.page == 2)
    assert intro.kind == "para" and "introduction" in intro.text


def test_toc_and_first_article_on_same_page_are_split_at_article() -> None:
    page = p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("Table des matières", [56, 60, 250, 74], 14),
        p.PageLine("1 Garanties 4", [56, 90, 250, 104], 10),
        p.PageLine("1 Garanties", [56, 150, 250, 166], 17, number="1"),
        p.PageLine("Le corps citable commence ici.", [56, 190, 350, 204], 10),
    ])
    p._mark_toc_pages([page])
    document, _ = p.build_document([page], edition="2026", source_hash="0" * 64, toc=[],
                                   doc_id=DOC, title="Contrat")
    toc_node = next(node for node in document.nodes if node.node_id == f"{DOC}:tdm")
    article = next(node for node in document.nodes if node.node_id == f"{DOC}:a1")
    assert all(document.block(block_id).text != "1 Garanties" for block_id in toc_node.blocks)
    assert [document.block(block_id).text for block_id in article.blocks] == [
        "1 Garanties", "Le corps citable commence ici.",
    ]


def test_non_monotonic_numbering_is_alert_and_served(tmp_path: Path) -> None:
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    items: list = []
    y = _article(items, 80, "1.2", "Deux")
    y = _article(items, y, "1.1", "Un")
    build_pdf(d / "source.pdf", pages=[items])
    report, entry = _run(d)
    check = next(c for c in report.checks if c.name == "numerotation_non_monotone")
    assert check.level == "alerte" and check.detail == "1.2 → 1.1" and entry.status == "servi"


def test_toc_pages_become_one_autre_block(tmp_path: Path) -> None:
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((300, 30), "DOC - TABLE DES MATIÈRES", fontsize=10, fontname=FONT_BODY)
    page.insert_text((56, 100), "1 Lexique 6", fontsize=12, fontname=FONT_BODY)
    page.insert_text((56, 120), "2 Garanties 20", fontsize=12, fontname=FONT_BODY)
    _page(doc, [(56, 80, "1", 17.0, FONT_TITLE), (122, 80, "Lexique", 17.0, FONT_TITLE)], 2)
    _save(doc, d / "source.pdf")
    report, _ = _run(d)
    doc_ = Document.model_validate_json((d / "document.json").read_bytes())
    tdm = [b for b in doc_.blocks if b.page == 1]
    assert len(tdm) == 1 and tdm[0].kind == "autre" and tdm[0].text == "1 Lexique 6\n2 Garanties 20"
    assert next(n for n in doc_.nodes if n.node_id == f"{DOC}:tdm").blocks == [tdm[0].block_id]
    assert report.stats["pages_tdm"] == 1
    check = next(c for c in report.checks if c.name == "tdm_imprimee")
    assert check.level == "alerte" and check.detail.endswith(": 2")


def test_printed_toc_match_is_info_and_bookmarks_remain_distinct(tmp_path: Path) -> None:
    """P4 : la TdM imprimée concordante informe ; les signets PDF gardent leur propre contrôle."""
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((300, 30), "DOC - TABLE DES MATIÈRES", fontsize=10, fontname=FONT_BODY)
    page.insert_text((56, 100), "1 Lexique 2", fontsize=12, fontname=FONT_BODY)
    page.insert_text((56, 120), "2 Garanties 2", fontsize=12, fontname=FONT_BODY)
    items: list = []
    y = _article(items, 80, "1", "Lexique", size=17)
    _article(items, y, "2", "Garanties", size=17)
    _page(doc, items, 2)
    doc.set_toc([[1, "1 Lexique", 2], [1, "3 Signet absent de l'arbre", 2]])
    _save(doc, d / "source.pdf")

    report, entry = _run(d)

    printed = next(c for c in report.checks if c.name == "tdm_imprimee")
    bookmarks = next(c for c in report.checks if c.name == "tdm_pdf_ecart")
    assert printed.level == "info" and "2 titre(s)" in printed.detail
    assert bookmarks.level == "alerte" and "3 (p. 2)" in bookmarks.detail
    assert entry.status == "servi"


def test_table_row_printed_toc_flows_through_document_and_final_report() -> None:
    toc_page = p.PageText(page=1, width=595, height=842, tables=[p.PageTable(
        [50, 70, 400, 150],
        [["Table des matières"], ["1", "Garanties", "2"], ["2", "Exclusions", "3"]],
    )])
    article_pages = [
        p.PageText(page=2, width=595, height=842,
                   lines=[p.PageLine("1 Garanties", [56, 80, 250, 96], 17, number="1")]),
        p.PageText(page=3, width=595, height=842,
                   lines=[p.PageLine("2 Exclusions", [56, 80, 250, 96], 17, number="2")]),
    ]
    pages = [toc_page, *article_pages]
    p._mark_toc_pages(pages)
    document, meta = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[],
                                      doc_id=DOC, title="Contrat")
    toc_table = next(block for block in document.blocks if block.page == 1)
    assert toc_table.kind == "table" and toc_table.source_field == "tdm"
    assert meta["printed_toc"] == [("1", "Garanties"), ("2", "Exclusions")]
    report = p.build_pdf_report(document, None, pages=pages, numbers=meta["numbers"],
                                duplicates=meta["duplicates"], continues=meta["continues"], toc=[],
                                printed_toc=meta["printed_toc"])
    check = next(check for check in report.checks if check.name == "tdm_imprimee")
    assert check.level == "info" and "2 titre(s)" in check.detail


def _document_for_toc() -> Document:
    children = [Node(node_id=f"{DOC}:a1", level=1, title="1 Conditions générales"),
                Node(node_id=f"{DOC}:a1.1", level=2, title="1.1 Objet"),
                Node(node_id=f"{DOC}:a2", level=1, title="2 Garanties complémentaires")]
    root = Node(node_id=DOC, items=[p.NodeRef(node_id=node.node_id) for node in children if node.level == 1])
    children[0].items.append(p.NodeRef(node_id=children[1].node_id))
    return Document(doc_id=DOC, kind="contrat", title="Contrat", edition="2026",
                    nodes=[root, *children], blocks=[])


def test_printed_toc_compares_only_announced_levels_and_combines_differences() -> None:
    document = _document_for_toc()
    equal = _printed_toc_check(document, [("1", "Conditions générales communes"),
                                          ("2", "Garanties complémentaires")], 1)
    assert equal.level == "info"
    missing = _printed_toc_check(document, [("1", "Conditions générales communes")], 1)
    assert missing.level == "alerte" and "absents de la TdM : 2" in missing.detail
    combined = _printed_toc_check(document, [("1", "Titre erroné"), ("3", "Inconnu")], 1)
    assert combined.level == "alerte"
    assert "absents de l'arbre : 3" in combined.detail
    assert "absents de la TdM : 2" in combined.detail
    assert "titres différents : 1" in combined.detail
    assert "1.1" not in combined.detail  # le niveau 2 n'est pas annoncé par cette TdM


def test_toc_title_prefix_threshold_changes_the_report_level(monkeypatch: pytest.MonkeyPatch) -> None:
    document = _document_for_toc()
    entries = [("1", "Conditions générales communes"), ("2", "Garanties complémentaires")]
    assert _printed_toc_check(document, entries, 1).level == "info"
    monkeypatch.setenv("TOC_TITLE_PREFIX_MIN_CHARS", "30")
    p.get_settings.cache_clear()
    try:
        stricter = _printed_toc_check(document, entries, 1)
    finally:
        p.get_settings.cache_clear()
    assert stricter.level == "alerte" and "titres différents : 1" in stricter.detail


def test_printed_toc_duplicate_and_conflicting_numbers_alert() -> None:
    check = _printed_toc_check(_document_for_toc(), [
        ("1", "Conditions générales"),
        ("1", "Conditions générales"),
        ("2", "Garanties complémentaires"),
        ("2", "Titre incompatible"),
    ], 1)
    assert check.level == "alerte"
    assert "annoncés plusieurs fois : 1, 2" in check.detail
    assert "titres conflictuels pour un même numéro : 2" in check.detail


def test_table_parser_preserves_identical_printed_toc_duplicates_for_report() -> None:
    page = p.PageText(page=1, width=595, height=842, is_toc=True, tables=[p.PageTable(
        [50, 70, 400, 150],
        [["Table des matières"], ["1", "Conditions générales", "2"],
         ["1", "Conditions générales", "3"]],
    )])
    entries = p._printed_toc_entries([page])
    assert entries == [("1", "Conditions générales"), ("1", "Conditions générales")]
    check = _printed_toc_check(_document_for_toc(), entries, 1)
    assert check.level == "alerte" and "annoncés plusieurs fois : 1" in check.detail


def test_multiline_toc_titles_in_columns_are_assembled_deterministically() -> None:
    page = p.PageText(page=1, width=595, height=842, is_toc=True, lines=[
        p.PageLine("2 Garanties", [330, 100, 440, 114], 12),
        p.PageLine("complémentaires", [330, 116, 460, 130], 12),
        p.PageLine("28", [540, 100, 560, 114], 12),
        p.PageLine("1 Conditions générales", [56, 100, 220, 114], 12),
        p.PageLine("communes", [56, 116, 130, 130], 12),
        p.PageLine("12", [280, 100, 300, 114], 12),
    ])
    entries = p._printed_toc_entries([page])
    assert entries == [("1", "Conditions générales communes"), ("2", "Garanties complémentaires")]
    assert _printed_toc_check(_document_for_toc(), entries, 1).level == "info"


@pytest.mark.parametrize(
    "mutate, category",
    [
        (lambda raw: raw["blocks"].append(dict(raw["blocks"][0])), "block_id_duplique"),
        (lambda raw: raw["nodes"][0].update(items=[]), "bloc_orphelin"),
        (lambda raw: raw["nodes"].append({"node_id": "second", "items": [{"block_id": f"{DOC}:p1:1"}]}),
         "bloc_parent_multiple"),
    ],
)
def test_tree_validation_categories_are_explicit_blockers(mutate, category: str) -> None:
    """P5 : Pydantic reste l'autorité ; le rapport nomme la catégorie refusée."""
    raw = {
        "doc_id": DOC,
        "kind": "contrat",
        "title": "Contrat",
        "edition": "test",
        "blocks": [{"block_id": f"{DOC}:p1:1", "text": "Clause", "loc": "p1", "seq": 1}],
        "nodes": [{"node_id": "root", "items": [{"block_id": f"{DOC}:p1:1"}]}],
    }
    mutate(raw)
    with pytest.raises(ValidationError) as caught:
        Document.model_validate(raw)
    report = report_from_validation_error(DOC, caught.value)
    assert report.blocking[0].name == "invariants_arbre"
    assert category in report.blocking[0].detail


@pytest.mark.parametrize(
    "nodes,category",
    [
        ([{"node_id": "root"}, {"node_id": "root"}], "node_id_duplique"),
        ([{"node_id": "root", "items": [{"node_id": "left"}, {"node_id": "right"}]},
          {"node_id": "left", "items": [{"node_id": "shared"}]},
          {"node_id": "right", "items": [{"node_id": "shared"}]},
          {"node_id": "shared"}], "noeud_parent_multiple"),
        ([{"node_id": "left", "items": [{"node_id": "right"}]},
          {"node_id": "right", "items": [{"node_id": "left"}]}], "cycle_noeuds"),
        ([{"node_id": "left"}, {"node_id": "right"}], "racine_invalide"),
    ],
)
def test_node_validation_categories_cover_duplicate_parent_cycle_and_root(nodes: list[dict], category: str) -> None:
    raw = {"doc_id": DOC, "kind": "contrat", "title": "Contrat", "edition": "test",
           "blocks": [], "nodes": nodes}
    with pytest.raises(ValidationError) as caught:
        Document.model_validate(raw)
    report = report_from_validation_error(DOC, caught.value)
    assert category in report.blocking[0].detail


@pytest.mark.parametrize("message", [
    "nœud dupliqué",
    "node_id dupliqué",
])
def test_node_duplicate_validator_wording_maps_to_explicit_category(message: str) -> None:
    assert _tree_category(message) == "node_id_duplique"


@pytest.mark.parametrize("message", [
    "nœud enfant rattaché à deux parents",
    "node_id child has multiple parents",
])
def test_node_multiple_parent_validator_wording_maps_to_explicit_category(message: str) -> None:
    assert _tree_category(message) == "noeud_parent_multiple"


def test_quality_checks_are_generic_and_never_raise_coverage_to_blocking() -> None:
    """P6 : résidu et charabia alertent, langue étrangère sort du ratio, couverture reste informative."""
    header = p.PageLine("Entête résiduel", [20, 10, 180, 22], 9)
    french = p.PageLine(
        "Le contrat est applicable dans les conditions et pour les dommages qui sont décrits par une clause.",
        [20, 40, 560, 60], 10,
    )
    english = p.PageLine(
        "The contract is valid and this policy is for you with the insurer and the client.",
        [20, 40, 560, 60], 10,
    )
    gibberish = p.PageLine(
        "bcdfg ghjkl mnpqr stvwxyz bcdfg ghjkl mnpqr stvwxyz bcdfg ghjkl mnpqr stvwxyz",
        [20, 40, 560, 60], 10,
    )
    pages = [
        p.PageText(page=1, width=595, height=842, lines=[header, french]),
        p.PageText(page=2, width=595, height=842,
                   lines=[p.PageLine("English heading", [20, 10, 180, 22], 9), english]),
        p.PageText(page=3, width=595, height=842, lines=[header, gibberish]),
        p.PageText(page=4, width=595, height=842, lines=[], images=1),
    ]
    blocks = [
        Block(block_id=f"{DOC}:p{page}:1", text=text, loc=f"p{page}", seq=1, page=page)
        for page, text in ((1, french.text), (2, english.text), (3, gibberish.text))
    ]
    document = Document(
        doc_id=DOC,
        kind="contrat",
        title="Contrat",
        edition="test",
        blocks=blocks,
        nodes=[Node(node_id="root", items=[BlockRef(block_id=block.block_id) for block in blocks])],
    )

    report = p.build_pdf_report(document, None, pages=pages, numbers=[], duplicates=[], continues=0, toc=[])
    levels = {check.name: check.level for check in report.checks}

    assert levels["page_sans_texte"] == "bloquant"
    assert levels["residus_entete_pied"] == "alerte"
    assert levels["charabia"] == "alerte"
    assert levels["pages_non_francaises"] == "alerte"
    assert levels["couverture"] == "info"
    assert next(check for check in report.checks if check.name == "charabia").detail == "3"
    assert next(check for check in report.checks if check.name == "pages_non_francaises").detail.endswith("2")
    assert report.stats["couverture"] == pytest.approx(2 / 3, abs=0.0001)


def test_report_makes_every_non_citable_page_observable() -> None:
    """Revue 3.1 B2 : l'exclusion du rappel est une alerte chiffrée, jamais une perte silencieuse."""
    blocks = [
        Block(block_id=f"{DOC}:p1:1", text="Couverture", loc="p1", seq=1, page=1, kind="autre"),
        Block(block_id=f"{DOC}:p2:1", text="Garanties | 4", loc="p2", seq=1, page=2,
              kind="table", source_field="tdm"),
        Block(block_id=f"{DOC}:p3:1", text="Clause citable", loc="p3", seq=1, page=3),
    ]
    document = Document(
        doc_id=DOC, kind="contrat", title="Contrat", edition="test", blocks=blocks,
        nodes=[Node(node_id="root", items=[BlockRef(block_id=block.block_id) for block in blocks])],
    )
    pages = [
        p.PageText(page=number, width=595, height=842,
                   lines=[p.PageLine(block.text, [56, 80, 400, 94], 10)])
        for number, block in enumerate(blocks, start=1)
    ]
    report = p.build_pdf_report(document, None, pages=pages, numbers=[], duplicates=[], continues=0, toc=[])
    check = next(check for check in report.checks if check.name == "blocs_non_citables")
    assert check.level == "alerte"
    assert "2 bloc(s)" in check.detail and "2 page(s)" in check.detail and "1, 2" in check.detail


def test_quality_analysis_does_not_mutate_extracted_pages() -> None:
    """Revue 3.1 M4 : le rapport décrit son entrée sans changer la provenance extraite."""
    page = p.PageText(page=1, width=595, height=842, lines=[
        p.PageLine("the contract is valid and this policy is for you", [56, 80, 400, 94], 10),
    ])
    first = _quality([page])
    second = _quality([page])
    assert first == second
    assert first[0] == [1] and page.language == "fr"


def test_short_page_with_configured_foreign_signals_is_not_silently_french(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUALITY_MIN_WORDS", "20")
    monkeypatch.setenv("FOREIGN_SIGNAL_MIN", "2")
    monkeypatch.setenv("FRENCH_SIGNAL_RATIO_MIN", "0.40")
    p.get_settings.cache_clear()
    try:
        page = p.PageText(page=1, width=595, height=842, lines=[
            p.PageLine("le contract the and", [56, 80, 180, 94], 10),
        ])
        foreign, gibberish, _ = _quality([page])
    finally:
        p.get_settings.cache_clear()
    assert foreign == [1] and gibberish == [] and page.language == "fr"


def test_non_default_gibberish_and_residual_thresholds_change_behavior(
        monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        p.PageText(page=number, width=595, height=842, lines=[
            p.PageLine("résidu rare", [56, 10, 160, 24], 9),
            p.PageLine("bcdfg ghjkl mnpqr avec voyelle", [56, 80, 330, 94], 10),
        ])
        for number in range(1, 4)
    ]
    monkeypatch.setenv("QUALITY_MIN_WORDS", "4")
    monkeypatch.setenv("GIBBERISH_RATIO_MAX", "0.20")
    monkeypatch.setenv("RESIDUAL_HEADER_MIN_PAGES_RATIO", "1.0")
    p.get_settings.cache_clear()
    try:
        _, gibberish, residues = _quality(pages)
    finally:
        p.get_settings.cache_clear()
    assert gibberish == [1, 2, 3]
    assert residues == ["residu rare"]


def test_table_plus_dense_image_is_mixed_and_residual_headers_use_ceil_and_bands() -> None:
    table = p.PageTable([50, 100, 350, 180], [["Garantie", "Plafond"]])
    mixed_page = p.PageText(page=1, width=595, height=842, tables=[table], images=1,
                            image_area_ratio=0.5)
    table_block = Block(block_id=f"{DOC}:p1:1", text="Garantie | Plafond", loc="p1", seq=1,
                        page=1, kind="table")
    document = Document(doc_id=DOC, kind="contrat", title="Contrat", edition="test",
                        blocks=[table_block],
                        nodes=[Node(node_id="root", items=[BlockRef(block_id=table_block.block_id)])])
    report = p.build_pdf_report(document, None, pages=[mixed_page], numbers=[], duplicates=[],
                                continues=0, toc=[])
    assert next(check for check in report.checks if check.name == "pages_mixtes").detail.endswith(": 1")

    pages = []
    for number in range(1, 8):
        lines = [p.PageLine("Corps répété", [56, 100, 220, 114], 10)]
        if number <= 2:  # 2/7 : `int(7 × 0,3)` alertait, `ceil(7 × 0,3)` exige bien 3 pages.
            lines.insert(0, p.PageLine("Résidu", [56, 10, 120, 24], 9))
        pages.append(p.PageText(page=number, width=595, height=842, lines=lines))
    _, _, residues = _quality(pages)
    assert residues == []  # ni les deux vraies bandes, ni la première ligne du corps répétée ne suffisent


def test_initial_running_band_ratio_uses_ceil(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "source.pdf"
    doc = pymupdf.open()
    for number in range(3):
        page = doc.new_page(width=595, height=842)
        if number < 2:
            page.insert_text((56, 20), "bande récurrente", fontsize=9)
        page.insert_text((56, 100), f"corps page {number + 1}", fontsize=10)
    doc.save(pdf)
    monkeypatch.setenv("HEADER_MIN_PAGES_RATIO", "0.67")
    p.get_settings.cache_clear()
    try:
        pages, _ = p.extract_pages(pdf)
    finally:
        p.get_settings.cache_clear()
    assert [line.text for line in pages[0].lines] == ["bande récurrente", "corps page 1"]
    assert pages[0].removed == []


def test_mixed_density_setting_and_zero_image_area_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    line = p.PageLine("Texte natif", [56, 80, 180, 94], 10)
    blocks = [Block(block_id=f"{DOC}:p1:1", text=line.text, loc="p1", seq=1, page=1)]
    document = Document(doc_id=DOC, kind="contrat", title="Contrat", edition="test", blocks=blocks,
                        nodes=[Node(node_id="root", items=[BlockRef(block_id=blocks[0].block_id)])])
    monkeypatch.setenv("MIXED_PAGE_IMAGE_DENSITY", "0")
    p.get_settings.cache_clear()
    try:
        no_image = p.PageText(page=1, width=595, height=842, lines=[line], image_area_ratio=0,
                              native_text=True)
        report = p.build_pdf_report(document, None, pages=[no_image], numbers=[], duplicates=[],
                                    continues=0, toc=[])
        assert all(check.name != "pages_mixtes" for check in report.checks)
        image = no_image.__class__(page=1, width=595, height=842, lines=[line], images=1,
                                   image_area_ratio=0.01, native_text=True)
        report = p.build_pdf_report(document, None, pages=[image], numbers=[], duplicates=[],
                                    continues=0, toc=[])
    finally:
        p.get_settings.cache_clear()
    assert next(check for check in report.checks if check.name == "pages_mixtes").level == "alerte"


def test_missing_or_corrupt_source_is_blocking(tmp_path: Path) -> None:
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    (d / "source.sha256").write_text("0" * 64 + "\n", "utf-8")
    report, entry = _run(d)
    assert report.blocking[0].name == "source_illisible" and entry.status == "quarantaine"
    (d / "source.pdf").write_bytes(b"not a pdf")
    report, _ = _run(d)
    assert report.blocking[0].name == "source_illisible" and "≠ source.sha256" in report.blocking[0].detail
    build_pdf(d / "source.pdf", pages=nominal_pages(), sha=False)
    report, _ = _run(d)
    assert "≠ source.sha256" in report.blocking[0].detail


@pytest.mark.parametrize("content, fragment", [(None, "source.sha256 absent"), ("", "mal formé"), ("abc\n", "mal formé")])
def test_reference_hash_is_mandatory_and_checked_before_extraction(tmp_path: Path, content: str | None, fragment: str) -> None:
    """AD-7 (revue Codex 1.2, B8) : sans empreinte de référence, aucun PDF n'est ingéré."""
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    build_pdf(d / "source.pdf", pages=nominal_pages(), sha=False)
    if content is not None:
        (d / "source.sha256").write_text(content, "utf-8")
    _poser_la_disposition(tmp_path, d)
    assert p.main([DOC, "--data", str(d.parent), "--title", "Contrat synthétique",
                   "--edition", "test 2026"]) == 1
    report = json.loads((d / "report.json").read_text("utf-8"))
    assert report["checks"][0]["level"] == "bloquant" and fragment in report["checks"][0]["detail"]
    assert not (d / "document.json").exists()
    assert json.loads((d.parent / "manifest.json").read_text("utf-8"))[DOC]["status"] == "quarantaine"


def test_main_with_unknown_doc_dir_is_blocking(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "inconnu").mkdir()
    _poser_la_disposition(tmp_path, tmp_path / "data" / "inconnu")
    assert p.main(["inconnu", "--data", str(tmp_path / "data")]) == 1


def _generic_source(tmp_path: Path, doc_id: str = DOC) -> Path:
    data_dir = tmp_path / "data" / doc_id
    data_dir.mkdir(parents=True)
    build_pdf(data_dir / "source.pdf", pages=[[ (56, 100, "Clause sans numéro.", 10.0, FONT_BODY) ]])
    return data_dir


@pytest.mark.parametrize("url", ["http://example.test/contrat.pdf", "https://example.test/contrat.pdf",
                                 "gs://bucket-valid/contracts/contrat.pdf"])
def test_generic_contract_accepts_valid_source_url(tmp_path: Path, url: str) -> None:
    data_dir = _generic_source(tmp_path)
    (data_dir / "source.url").write_text(url + "\n", "utf-8")
    _poser_la_disposition(tmp_path, data_dir)
    assert p.main([DOC, "--data", str(data_dir.parent), "--title", "Contrat générique",
                   "--edition", "édition 2026"]) == 0
    entry = json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))[DOC]
    assert entry["status"] == "servi"
    document = Document.model_validate_json((data_dir / "document.json").read_bytes())
    assert document.source_url == url and document.edition == "édition 2026"


@pytest.mark.parametrize("source_url", [None, "", "ftp://example.test/contrat.pdf", "https://",
                                         "gs://", "gs://bucket", "gs://B/object", "gs://bucket/a b"])
def test_generic_contract_rejects_missing_empty_or_malformed_source_url(
        tmp_path: Path, source_url: str | None) -> None:
    data_dir = _generic_source(tmp_path)
    if source_url is not None:
        (data_dir / "source.url").write_text(source_url, "utf-8")
    report, entry = p.run(data_dir, edition="édition 2026", doc_id=DOC, title="Contrat générique")
    assert entry.status == "quarantaine" and report.blocking[0].name == "source_illisible"
    assert "URL HTTP(S) ou gs://bucket/objet non vide" in report.blocking[0].detail


@pytest.mark.parametrize("doc_id", ["..", "../sortie", "dossier/doc", "dossier\\doc", "/absolu", "Doc-A"])
def test_generic_cli_rejects_doc_id_outside_repository_slug_before_path_resolution(
        tmp_path: Path, doc_id: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    assert p.main([doc_id, "--data", str(data), "--title", "Contrat", "--edition", "2026"]) == 2
    assert list(data.iterdir()) == []


def test_generic_cli_requires_edition_and_non_blank_title(tmp_path: Path) -> None:
    data_dir = _generic_source(tmp_path)
    (data_dir / "source.url").write_text("https://example.test/contrat.pdf\n", "utf-8")
    _poser_la_disposition(tmp_path, data_dir)
    assert p.main([DOC, "--data", str(data_dir.parent), "--title", "Contrat générique"]) == 1
    entry = json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))[DOC]
    assert entry["edition"] == "" and entry["edition"] != p.DEFAULT_EDITION
    assert "--edition" in json.loads((data_dir / "report.json").read_text("utf-8"))["checks"][0]["detail"]
    assert p.main([DOC, "--data", str(data_dir.parent), "--title", "   ",
                   "--edition", "édition 2026"]) == 1
    assert "--title ne peut pas être vide" in json.loads(
        (data_dir / "report.json").read_text("utf-8"))["checks"][0]["detail"]


def test_axa_compatibility_cli_keeps_defaults_and_allows_missing_source_url(tmp_path: Path) -> None:
    data_dir = _generic_source(tmp_path, p.DOC_ID)
    assert not (data_dir / "source.url").exists()
    _poser_la_disposition(tmp_path, data_dir)
    assert p.main([p.DOC_ID, "--data", str(data_dir.parent)]) == 0
    document = Document.model_validate_json((data_dir / "document.json").read_bytes())
    assert document.edition == p.DEFAULT_EDITION and document.title == p.TITLE and document.source_url is None


def test_ids_disparus_is_reported_when_text_moves(data: Path) -> None:
    """Cas positif d'`ids_disparus` (revue Codex 1.2, I6) : un paragraphe inséré décale les IDs de la page."""
    _run(data)
    pages = nominal_pages()
    pages[0].insert(2, (122, 70, "Avertissement ajoute en tete de page.", 10.0, FONT_BODY))
    build_pdf(data / "source.pdf", pages=pages)
    report, entry = _run(data)
    check = next(c for c in report.checks if c.name == "ids_disparus")
    assert check.level == "alerte" and f"{DOC}:p1:1" in check.detail and entry.status == "servi"
    assert report.stats["ids_disparus"] > 0 and report.stats["ids_nouveaux"] > 0


def test_un_typage_precedent_ne_se_reutilise_que_sur_identite_forte(data: Path) -> None:
    _run(data)
    raw = Document.model_validate_json((data / "document.json").read_bytes())
    target = next(block for block in raw.blocks if block.kind != "autre")
    old_target = target.model_copy(update={
        "kind": "condition", "kind_source": "model_verified", "kind_confidence": 0.93,
        "structural_kind": target.kind,
    }, deep=True)
    previous = Document.model_validate(raw.model_copy(update={
        "blocks": [old_target if block.block_id == target.block_id else block for block in raw.blocks],
    }).model_dump())
    proof = Report(doc_id=raw.doc_id, checks=[
        Check(name="typage_clauses", level="info", detail="couverture complète"),
    ]).model_dump_json().encode("utf-8")

    reused, identities = p.reutiliser_typage_identique(raw, previous, proof)
    assert identities[target.block_id] == 1
    assert reused.block(target.block_id).kind == "condition"
    assert reused.block(target.block_id).kind_source == "model_verified"

    changed_target = target.model_copy(update={"text": target.text + " dérive"}, deep=True)
    changed = Document.model_validate(raw.model_copy(update={
        "blocks": [changed_target if block.block_id == target.block_id else block for block in raw.blocks],
    }).model_dump())
    not_reused, identities = p.reutiliser_typage_identique(changed, previous, proof)
    assert target.block_id not in identities
    assert not_reused.block(target.block_id).kind_source is None


def test_un_report_de_preuve_etranger_ne_reutilise_aucun_typage(data: Path) -> None:
    _run(data)
    raw = Document.model_validate_json((data / "document.json").read_bytes())
    target = next(block for block in raw.blocks if is_citable(block))
    previous = Document.model_validate(raw.model_copy(update={
        "blocks": [
            block.model_copy(update={
                "kind": "garantie", "kind_source": "model_verified", "kind_confidence": 0.99,
            }, deep=True) if block.block_id == target.block_id else block
            for block in raw.blocks
        ],
    }, deep=True).model_dump())
    foreign_proof = Report(doc_id="autre-document", checks=[
        Check(name="typage_clauses", level="info", detail="couverture complète"),
    ]).model_dump_json().encode("utf-8")

    typed, reused = p.reutiliser_typage_identique(raw, previous, foreign_proof)

    assert reused == {}
    assert typed.block(target.block_id).kind_source is None


def test_run_publie_sans_delta_en_transportant_portee_et_recalculant_le_rapport(
        data: Path) -> None:
    """Couvre directement la branche de publication ``pending == 0``, hors fournisseur."""
    first, _entry = _run(data)
    previous = Document.model_validate_json((data / "document.json").read_bytes())
    scoped = next(
        node for node in previous.nodes
        if node.items and all(
            isinstance(item, BlockRef) and is_citable(previous.block(item.block_id))
            for item in node.items
        )
    )
    blocks = [
        block.model_copy(update={
            "kind": "garantie", "kind_source": "model_verified", "kind_confidence": 0.99,
        }, deep=True) if is_citable(block) else block
        for block in previous.blocks
    ]
    nodes = [
        node.model_copy(update={"scope": Scope(kind="special")}, deep=True)
        if node.node_id == scoped.node_id else node
        for node in previous.nodes
    ]
    typed = Document.model_validate(previous.model_copy(update={
        "blocks": blocks, "nodes": nodes,
    }, deep=True).model_dump())
    (data / "document.json").write_text(document_json(typed), encoding="utf-8")
    stale = first.model_copy(update={
        "checks": [*first.checks, Check(name="ids_disparus", level="alerte", detail="ancien"),
                   Check(name="typage_clauses", level="info", detail="double lecture")],
        "stats": {**first.stats, "ids_disparus": 4, "ids_nouveaux": 7},
    }, deep=True)
    (data / "report.json").write_text(stale.model_dump_json(), encoding="utf-8")

    report, entry = _run(data)
    published = Document.model_validate_json((data / "document.json").read_bytes())

    assert entry.status == "servi" and not report.blocking
    assert report.stats[p.TYPING_PENDING_COUNT_STAT] == 0
    assert report.stats[p.TYPING_REUSED_COUNT_STAT] == sum(is_citable(b) for b in published.blocks)
    assert all(check.name != "ids_disparus" for check in report.checks)
    assert report.stats["ids_disparus"] == report.stats["ids_nouveaux"] == 0
    assert next(check for check in report.checks if check.name == "typage_clauses").level == "info"
    assert all(check.name != "typage_transport" for check in report.checks)
    assert next(node for node in published.nodes if node.node_id == scoped.node_id).scope.kind == "special"


def _document_metaphorique(*, inverse: bool = False, dependance_deplacee: bool = False,
                            proprietaire_change: bool = False) -> Document:
    ids = (f"{DOC}:p1:2", f"{DOC}:p1:1") if inverse else (f"{DOC}:p1:1", f"{DOC}:p1:2")
    garantie_id, condition_id = ids
    condition_bbox = [60.0, 140.0, 300.0, 154.0]
    if dependance_deplacee:
        condition_bbox = [60.0, 141.0, 300.0, 155.0]
    garantie = Block(
        block_id=garantie_id, text="La garantie couvre l'incendie.", loc="p1",
        seq=int(garantie_id.rsplit(":", 1)[1]), page=1, bbox=[60.0, 100.0, 300.0, 114.0],
        kind="garantie" if not inverse else "para", structural_kind="para",
        kind_source="model_verified" if not inverse else None,
        kind_confidence=0.96 if not inverse else None,
        scope_node_id=f"{DOC}:a1" if not inverse else None,
        refs=[condition_id] if not inverse else [],
        relation=Relation(specialise=condition_id) if not inverse else Relation(),
        lines=[Line(line_id=f"{garantie_id}:l1", text="La garantie couvre l'incendie.",
                    bbox=[60.0, 100.0, 300.0, 114.0])],
    )
    condition = Block(
        block_id=condition_id, text="Sous réserve de la condition.", loc="p1",
        seq=int(condition_id.rsplit(":", 1)[1]), page=1, bbox=condition_bbox,
        kind="condition" if not inverse else "para", structural_kind="para",
        kind_source="model_verified" if not inverse else None,
        kind_confidence=0.91 if not inverse else None,
        scope_node_id=f"{DOC}:a1" if not inverse else None,
        lines=[Line(line_id=f"{condition_id}:l1", text="Sous réserve de la condition.",
                    bbox=condition_bbox)],
    )
    owner = f"{DOC}:a2" if proprietaire_change else f"{DOC}:a1"
    nodes = [
        Node(node_id=DOC, items=[NodeRef(node_id=f"{DOC}:a1"), NodeRef(node_id=f"{DOC}:a2")]),
        Node(node_id=f"{DOC}:a1", level=1, title="Garantie", items=[] if owner != f"{DOC}:a1" else [
            BlockRef(block_id=garantie_id), BlockRef(block_id=condition_id),
        ]),
        Node(node_id=f"{DOC}:a2", level=1, title="Autre branche", items=[] if owner != f"{DOC}:a2" else [
            BlockRef(block_id=garantie_id), BlockRef(block_id=condition_id),
        ]),
    ]
    return Document(
        doc_id=DOC, kind="contrat", title="Contrat métamorphique", edition="2026",
        source_hash="a" * 64, nodes=nodes, blocks=[garantie, condition],
    )


def _preuve_typage_complet(doc: Document) -> bytes:
    return Report(doc_id=doc.doc_id, checks=[
        Check(name="typage_clauses", level="info", detail="couverture complète"),
    ]).model_dump_json().encode("utf-8")


def test_reutilisation_metaphorique_remappe_ids_et_dependances_sans_leur_donner_autorite() -> None:
    previous = _document_metaphorique()
    previous.nodes[1].scope = Scope(kind="special")
    current = _document_metaphorique(inverse=True)
    typed, reused = p.reutiliser_typage_identique(current, previous, _preuve_typage_complet(previous))

    assert reused == {f"{DOC}:p1:1": 1, f"{DOC}:p1:2": 1}
    garantie = typed.block(f"{DOC}:p1:2")
    assert garantie.kind == "garantie" and garantie.kind_source == "model_verified"
    assert garantie.refs == [f"{DOC}:p1:1"]
    assert garantie.relation.specialise == f"{DOC}:p1:1"
    assert garantie.scope_node_id == f"{DOC}:a1"
    assert next(node for node in typed.nodes if node.node_id == f"{DOC}:a1").scope.kind == "special"


def test_portee_ne_sappuie_que_sur_les_blocs_effectivement_reutilises() -> None:
    previous = _document_metaphorique()
    previous.nodes[1].scope = Scope(kind="special")
    current = _document_metaphorique(inverse=True)
    garantie = previous.block(f"{DOC}:p1:1")
    proof = Report(doc_id=DOC, stats={
        p.TYPING_REUSED_IDS_STAT: {garantie.block_id: 1},
    }).model_dump_json().encode("utf-8")

    typed, reused = p.reutiliser_typage_identique(current, previous, proof)

    assert reused == {f"{DOC}:p1:2": 1}
    assert next(node for node in typed.nodes if node.node_id == f"{DOC}:a1").scope.kind == "commun"


def test_portee_dun_sous_arbre_vide_ne_se_transporte_pas() -> None:
    previous = _document_metaphorique()
    previous.nodes[2].scope = Scope(kind="special")
    current = _document_metaphorique(inverse=True)
    mapping = {f"{DOC}:p1:1": f"{DOC}:p1:2", f"{DOC}:p1:2": f"{DOC}:p1:1"}

    nodes = p._reutiliser_portees_noeuds(current, previous, mapping)

    assert next(node for node in nodes if node.node_id == f"{DOC}:a2").scope.kind == "commun"


def test_portee_refuse_un_ancetre_modifie_ou_disparu() -> None:
    previous = _document_metaphorique()
    previous.nodes[1].scope = Scope(kind="special")
    current = _document_metaphorique(inverse=True)
    mapping = {f"{DOC}:p1:1": f"{DOC}:p1:2", f"{DOC}:p1:2": f"{DOC}:p1:1"}

    previous.nodes[0].title = "Racine historique"
    modified = p._reutiliser_portees_noeuds(current, previous, mapping)
    assert next(node for node in modified if node.node_id == f"{DOC}:a1").scope.kind == "commun"

    previous = _document_metaphorique()
    previous.nodes[1].scope = Scope(kind="special")
    ancestor = Node(
        node_id=f"{DOC}:a0", level=1, title="Branche porteuse",
        items=[NodeRef(node_id=f"{DOC}:a1")],
    )
    previous.nodes[0].items = [NodeRef(node_id=ancestor.node_id), NodeRef(node_id=f"{DOC}:a2")]
    previous.nodes.insert(1, ancestor)
    previous = Document.model_validate(previous.model_dump())
    disappeared = p._reutiliser_portees_noeuds(current, previous, mapping)
    assert next(node for node in disappeared if node.node_id == f"{DOC}:a1").scope.kind == "commun"


def _ajouter_bloc_incompatible(doc: Document, node_id: str, text: str) -> Document:
    block_id = f"{DOC}:p2:1"
    block = Block(
        block_id=block_id, text=text, loc="p2", seq=1, page=2,
        bbox=[60.0, 100.0, 300.0, 114.0],
        lines=[Line(line_id=f"{block_id}:l1", text=text,
                    bbox=[60.0, 100.0, 300.0, 114.0])],
    )
    nodes = [
        node.model_copy(update={"items": [*node.items, BlockRef(block_id=block_id)]}, deep=True)
        if node.node_id == node_id else node
        for node in doc.nodes
    ]
    return Document.model_validate(doc.model_copy(update={
        "nodes": nodes, "blocks": [*doc.blocks, block],
    }, deep=True).model_dump())


def _imbriquer_noeud_scope(doc: Document) -> Document:
    nodes = []
    for node in doc.nodes:
        if node.node_id == DOC:
            nodes.append(node.model_copy(update={
                "items": [NodeRef(node_id=f"{DOC}:a1")],
            }, deep=True))
        elif node.node_id == f"{DOC}:a1":
            nodes.append(node.model_copy(update={
                "items": [*node.items, NodeRef(node_id=f"{DOC}:a2")],
            }, deep=True))
        else:
            nodes.append(node)
    return Document.model_validate(doc.model_copy(update={"nodes": nodes}, deep=True).model_dump())


@pytest.mark.parametrize("mutation", ["ancetre_proprietaire", "sous_arbre_proprietaire", "scope_nodes"])
def test_typage_exige_ancetres_et_sous_arbres_compatibles_pour_tous_les_noeuds(
        mutation: str) -> None:
    previous = _document_metaphorique()
    current = _document_metaphorique(inverse=True)
    if mutation == "ancetre_proprietaire":
        previous.nodes[0].title = "Racine historique"
    elif mutation == "sous_arbre_proprietaire":
        previous = _ajouter_bloc_incompatible(previous, f"{DOC}:a1", "Ancien contenu")
        current = _ajouter_bloc_incompatible(current, f"{DOC}:a1", "Nouveau contenu")
    else:
        previous = _imbriquer_noeud_scope(previous)
        current = _imbriquer_noeud_scope(current)
        garantie = previous.block(f"{DOC}:p1:1").model_copy(update={
            "scope_node_ids": [f"{DOC}:a2"],
        }, deep=True)
        previous = Document.model_validate(previous.model_copy(update={
            "blocks": [garantie, previous.block(f"{DOC}:p1:2")],
        }, deep=True).model_dump())
        previous = _ajouter_bloc_incompatible(previous, f"{DOC}:a2", "Ancienne portée")
        current = _ajouter_bloc_incompatible(current, f"{DOC}:a2", "Nouvelle portée")

    typed, reused = p.reutiliser_typage_identique(
        current, previous, _preuve_typage_complet(previous),
    )

    assert f"{DOC}:p1:2" not in reused
    assert typed.block(f"{DOC}:p1:2").kind_source is None


def test_reutilisation_metaphorique_refuse_provenance_noeud_et_dependance_modifies() -> None:
    previous = _document_metaphorique()
    proof = _preuve_typage_complet(previous)

    moved_dependency = _document_metaphorique(inverse=True, dependance_deplacee=True)
    _typed, reused = p.reutiliser_typage_identique(moved_dependency, previous, proof)
    assert reused == {}

    moved_owner = _document_metaphorique(inverse=True, proprietaire_change=True)
    _typed, reused = p.reutiliser_typage_identique(moved_owner, previous, proof)
    assert reused == {}


def test_reutilisation_metaphorique_refuse_toute_correspondance_ambigue() -> None:
    previous = _document_metaphorique()
    current = _document_metaphorique(inverse=True)
    duplicated = current.block(f"{DOC}:p1:2").model_copy(update={
        "block_id": f"{DOC}:p1:3", "seq": 3,
        "lines": [Line(line_id=f"{DOC}:p1:3:l1", text="La garantie couvre l'incendie.",
                       bbox=[60.0, 100.0, 300.0, 114.0])],
    }, deep=True)
    node = current.nodes[1].model_copy(update={
        "items": [*current.nodes[1].items, BlockRef(block_id=duplicated.block_id)],
    }, deep=True)
    ambiguous = Document.model_validate(current.model_copy(update={
        "nodes": [current.nodes[0], node, current.nodes[2]],
        "blocks": [*current.blocks, duplicated],
    }, deep=True).model_dump())

    typed, reused = p.reutiliser_typage_identique(ambiguous, previous, _preuve_typage_complet(previous))
    assert f"{DOC}:p1:2" not in reused and f"{DOC}:p1:3" not in reused
    assert typed.block(f"{DOC}:p1:2").kind_source is None
    assert typed.block(f"{DOC}:p1:3").kind_source is None


def test_duplicate_numbering_is_an_alert_with_suffixed_node(tmp_path: Path) -> None:
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    items: list = []
    y = _article(items, 80, "1.1", "Un")
    y = _article(items, y, "1.1", "Un bis")
    build_pdf(d / "source.pdf", pages=[items])
    report, entry = _run(d)
    check = next(c for c in report.checks if c.name == "numerotation_dupliquee")
    assert check.level == "alerte" and check.detail == "1.1" and entry.status == "servi"
    doc = Document.model_validate_json((d / "document.json").read_bytes())
    assert {n.node_id for n in doc.nodes} >= {f"{DOC}:a1.1", f"{DOC}:a1.1-2"}


def test_page_split_links_uppercase_continuation(tmp_path: Path) -> None:
    """AD-2 (revue Codex 1.2, B6) : la scission dépend de la structure, pas de la casse (p53:8 → p54:1 du contrat)."""
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    p1: list = []
    y = _article(p1, 80, "1", "Lexique", size=17)
    y = _para(p1, y, ["Les donnees sont traitees par la Compagnie et par AXA"])
    p2: list = []
    y = _para(p2, 80, ["Assistance, et sont susceptibles d'etre transferees."])
    y = _para(p2, y, ["Nouveau paragraphe apres un point."])
    p3: list = []
    _para(p3, 80, ["Paragraphe ouvrant une page apres une phrase terminee."])
    build_pdf(d / "source.pdf", pages=[p1, p2, p3])
    report, _ = _run(d)
    doc = Document.model_validate_json((d / "document.json").read_bytes())
    by = {b.block_id: b for b in doc.blocks}
    assert by[f"{DOC}:p2:1"].continues == f"{DOC}:p1:2" and by[f"{DOC}:p2:1"].text.startswith("Assistance")
    assert by[f"{DOC}:p3:1"].continues is None and report.stats["continues"] == 1


def test_page_split_requires_same_kind_and_no_article_number(tmp_path: Path) -> None:
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    p1: list = []
    y = _article(p1, 80, "1", "Lexique", size=17)
    y = _para(p1, y, ["Une phrase qui ne se termine pas"])
    p2: list = []
    _article(p2, 80, "1.1", "Contenu")
    p3: list = []
    y = _para(p3, 80, ["Encore une phrase qui ne se termine pas"])
    p4: list = []
    p4.append((122, 80, "• un item de liste", 10.0, FONT_BODY))
    build_pdf(d / "source.pdf", pages=[p1, p2, p3, p4])
    _run(d)
    doc = Document.model_validate_json((d / "document.json").read_bytes())
    assert all(b.continues is None for b in doc.blocks)


def test_list_items_split_across_pages_are_linked(tmp_path: Path) -> None:
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    p1: list = [(122, 80, "• premier item,", 10.0, FONT_BODY), (122, 93, "• deuxieme item,", 10.0, FONT_BODY)]
    p2: list = [(122, 80, "• troisieme item.", 10.0, FONT_BODY)]
    build_pdf(d / "source.pdf", pages=[p1, p2])
    _run(d)
    doc = Document.model_validate_json((d / "document.json").read_bytes())
    assert doc.blocks[0].kind == "list" and doc.blocks[1].kind == "list" and doc.blocks[1].continues == doc.blocks[0].block_id


def test_aligned_list_continuation_stays_in_the_item(tmp_path: Path) -> None:
    """AD-2 (revue Codex 1.2, I5) : « • rend l'habitation … et » puis « immeubles … » aligné = même item (p54 du contrat)."""
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    items: list = [
        (122, 80, "• rend l'habitation dangereuse,", 10.0, FONT_BODY),
        (122, 93, "• entraine un risque de dommage a l'habitation et", 10.0, FONT_BODY),
        (122, 106, "immeubles qu'elle contient, et/ou", 10.0, FONT_BODY),
        (122, 119, "• rend l'habitation inhabitable. En cas", 10.0, FONT_BODY),
        (122, 132, "de vol de la cle.", 10.0, FONT_BODY),
        (122, 145, "Un paragraphe qui suit la liste.", 10.0, FONT_BODY),
        (122, 158, "• item ;", 10.0, FONT_BODY),
        (130, 171, "suite indentee.", 10.0, FONT_BODY),
    ]
    build_pdf(d / "source.pdf", pages=[items])
    _run(d)
    doc = Document.model_validate_json((d / "document.json").read_bytes())
    kinds = [(b.kind, len(b.lines)) for b in doc.blocks]
    assert kinds == [("list", 5), ("para", 1), ("list", 2)]
    assert doc.blocks[0].lines[2].text == "immeubles qu'elle contient, et/ou"


def test_pdf_bookmarks_fill_missing_titles_and_flag_gaps(tmp_path: Path) -> None:
    """AC 1.2 « get_toc() est utilisé s'il existe » (revue Codex 1.2, I4)."""
    d = tmp_path / "data" / DOC
    d.mkdir(parents=True)
    items: list = []
    y = _article(items, 80, "1", "Lexique", size=17)
    items.append((56, y, "1.1", 10.0, FONT_BODY))
    items.append((122, y, "Texte d'un alinea numero sans titre, qui continue.", 10.0, FONT_BODY))
    build_pdf(d / "source.pdf", pages=[items], toc=[[1, "1 Lexique", 1], [2, "1.1 Contenu", 1], [2, "1.2 Batiment", 1],
                                                   [1, "Annexe", 1]])
    report, entry = _run(d)
    doc = Document.model_validate_json((d / "document.json").read_bytes())
    by = {n.node_id: n for n in doc.nodes}
    assert by[f"{DOC}:a1"].title == "1 Lexique" and by[f"{DOC}:a1.1"].title.startswith("1.1 Texte")
    check = next(c for c in report.checks if c.name == "tdm_pdf_ecart")
    assert check.level == "alerte" and "1.2 (p. 1)" in check.detail and entry.status == "servi"
    assert report.stats["tdm_pdf_entrees"] == 4


def test_line_text_matches_raw_extraction_up_to_declared_flags(data: Path) -> None:
    """AC 1.2 « text brut » (revue Codex 1.2, I7) : chaque `Line.text` dérive d'une ligne de `get_text("dict")`
    par les seules transformations déclarées dans FLAGS (rstrip, lstrip hors puce, fusion numéro + espace)."""
    _run(data)
    doc = Document.model_validate_json((data / "document.json").read_bytes())
    raw: set[str] = set()
    with pymupdf.open(str(data / "source.pdf")) as pdf:
        for page in pdf:
            for b in page.get_text("dict", sort=True)["blocks"]:
                for line in b.get("lines", []):
                    raw.add("".join(s["text"] for s in line["spans"]).rstrip().lstrip(" \t"))
    for b in doc.blocks:
        for line in b.lines:
            head, _, tail = line.text.partition(p.FLAGS["merge_number_sep"])
            assert line.text in raw or (p._NUMBER_RE.match(head) and head in raw and tail in raw), line.text


def test_document_json_omits_defaults_and_text_norm(data: Path) -> None:
    _run(data)
    raw = json.loads((data / "document.json").read_text("utf-8"))
    assert "text_norm" not in raw["blocks"][0] and "lang" not in raw and "kind_confidence" not in raw["blocks"][0]
    doc = Document.model_validate(raw)
    assert document_json(doc) == (data / "document.json").read_text("utf-8")


@pytest.mark.parametrize(
    "variable,value",
    [
        ("MIXED_PAGE_IMAGE_DENSITY", "0.31"),
        ("OCR_DPI", "288"),
        ("QUALITY_MIN_WORDS", "17"),
        ("FOREIGN_SIGNAL_MIN", "4"),
        ("FRENCH_SIGNAL_RATIO_MIN", "0.11"),
        ("GIBBERISH_RATIO_MAX", "0.42"),
        ("RESIDUAL_HEADER_MIN_PAGES_RATIO", "0.41"),
        ("COVERAGE_THRESHOLD", "0.73"),
        ("TOC_PAGE_NUMBER_BASELINE_PT", "7.5"),
        ("TOC_COLUMN_TOLERANCE_PT", "72"),
        ("TOC_INDENT_TOLERANCE_PT", "4"),
        ("TOC_LINE_GAP_RATIO", "1.4"),
    ],
)
def test_fingerprint_depends_on_rules_and_thresholds(
        monkeypatch: pytest.MonkeyPatch, variable: str, value: str) -> None:
    before = p.ingest_fingerprint()
    original_rules = p.SEGMENTATION_RULES
    monkeypatch.setattr(p, "SEGMENTATION_RULES", "autre")
    assert p.ingest_fingerprint() != before
    monkeypatch.setattr(p, "SEGMENTATION_RULES", original_rules)
    assert p.ingest_fingerprint() == before
    monkeypatch.setenv(variable, value)
    p.get_settings.cache_clear()
    try:
        assert p.ingest_fingerprint() != before
    finally:
        p.get_settings.cache_clear()


# --- N3 / N1 : la racine, le refus avant tout travail, et le lien pendant qui est une absence -----

def test_la_cli_refuse_un_data_dir_non_installe_avant_toute_extraction(
        data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """N3 : aucun entrypoint de production n'atteint le repli rootless.

    Le refus tombe **avant** l'extraction du PDF, et aucune cible n'est écrite. Le lot d'une
    ingestion PDF n'est jamais mixte au sens du contrôle d'avant : quand *aucune* cible n'est
    couverte, `_espace_du_lot` rendait `None` sans lever et l'écriture partait sans atome.
    """
    assert p.main([DOC, "--data", str(data.parent), "--title", "Contrat synthétique",
                   "--edition", "test 2026"]) == 2
    erreur = capsys.readouterr().err
    assert "aucune racine de publication ne couvre" in erreur and "--depot" in erreur
    assert not (data / "document.json").exists() and not (data / "report.json").exists()
    assert not (data.parent / "manifest.json").exists()


def test_un_structure_json_non_publie_est_une_absence_pas_un_refus(
        data: Path, tmp_path: Path) -> None:
    """Le défaut d'interaction que la disposition crée, et que ce tour ferme.

    Sous une racine, tout artefact jamais publié est un **lien pendant** — c'est la forme même de
    l'absence, et c'est l'état réel des `structure.json` du dépôt. `presente()` employait
    `os.path.lexists`, qui voit l'entrée de répertoire : le lien pendant passait pour « présent »,
    `charger()` rendait `proposition_illisible`, le check devenait bloquant, et la prochaine
    réingestion PDF mettait les documents servis **en quarantaine** — du seul fait de la
    disposition. Hors racine, la règle d'AD-16 est inchangée : un lien pendant ordinaire reste
    « présent mais illisible » (`test_charger_refuse_tout_artefact_non_regulier…` le prouve).
    """
    _poser_la_disposition(tmp_path, data)
    from server.evals.espace import EspacePublie

    EspacePublie(tmp_path, tmp_path / "data").installer(
        [Path("data") / DOC / "structure.json"])
    structure = data / "structure.json"
    assert structure.is_symlink() and not structure.exists(), "le slot n'est pas publié"

    report, entry = p.run(data, edition="test 2026", doc_id=DOC, title="Contrat synthétique")

    assert not report.blocking, [c.name for c in report.blocking]
    assert entry.status == "servi" and entry.structure_hash is None
