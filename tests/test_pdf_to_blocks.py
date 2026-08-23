"""pdf_to_blocks sur des PDF synthétiques (PyMuPDF) : matrice I/O de la spec 1.2, sans PDF réel."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from server.app.domain import Document
from server.ingest import pdf_to_blocks as p
from server.ingest.artifacts import document_json

DOC = "doc-a"
FONT_BODY, FONT_TITLE = "helv", "hebo"


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
    return d


def _run(d: Path):
    return p.run(d, edition="test 2026", doc_id=DOC)


def test_nominal_tree_ids_bbox_scopes_and_continues(data: Path) -> None:
    report, entry = _run(data)
    assert not report.blocking and entry.status == "servi" and entry.gate is None
    doc = Document.model_validate_json((data / "document.json").read_bytes())
    assert doc.kind == "contrat" and doc.edition == "test 2026" and doc.ingest_fingerprint == p.ingest_fingerprint()
    by = {n.node_id: n for n in doc.nodes}
    parent = {c: n.node_id for n in doc.nodes for c in n.children}
    assert parent[f"{DOC}:a3.1.1.1.6"] == f"{DOC}:a3.1.1"  # préfixe le plus long existant (3.1.1.1 absent)
    assert parent[f"{DOC}:a1.1.1"] == f"{DOC}:a1.1" and parent[f"{DOC}:a4"] == DOC
    assert by[f"{DOC}:a1.1"].scope.kind == "commun" and by[f"{DOC}:a3.1.1"].scope.kind == "commun"
    assert by[f"{DOC}:a3.1.8.3"].scope.kind == "extension" and by[f"{DOC}:a4"].scope.kind == "special"
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
    assert p.main([DOC, "--data", str(d.parent)]) == 1
    report = json.loads((d / "report.json").read_text("utf-8"))
    check = next(c for c in report["checks"] if c["name"] == "page_sans_texte")
    assert check["level"] == "bloquant" and "3" in check["detail"]
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
    doc[0].insert_image(pymupdf.Rect(100, 400, 300, 600), pixmap=pix)
    _save(doc, d / "source.pdf")
    report, entry = _run(d)
    check = next(c for c in report.checks if c.name == "pages_mixtes")
    assert check.level == "alerte" and check.detail.endswith(": 1") and entry.status == "servi"


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
    assert p.main([DOC, "--data", str(d.parent)]) == 1
    report = json.loads((d / "report.json").read_text("utf-8"))
    assert report["checks"][0]["level"] == "bloquant" and fragment in report["checks"][0]["detail"]
    assert not (d / "document.json").exists()
    assert json.loads((d.parent / "manifest.json").read_text("utf-8"))[DOC]["status"] == "quarantaine"


def test_main_with_unknown_doc_dir_is_blocking(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "inconnu").mkdir()
    assert p.main(["inconnu", "--data", str(tmp_path / "data")]) == 1


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


def test_fingerprint_depends_on_rules_and_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    before = p.ingest_fingerprint()
    monkeypatch.setattr(p, "SEGMENTATION_RULES", "autre")
    assert p.ingest_fingerprint() != before
    monkeypatch.setattr(p, "SEGMENTATION_RULES", p.SEGMENTATION_RULES)
    monkeypatch.setenv("HEADER_BAND_PT", "41")
    p.get_settings.cache_clear()
    try:
        assert p.ingest_fingerprint() != before
    finally:
        p.get_settings.cache_clear()


@pytest.mark.parametrize("numero, kind", [("1", "commun"), ("2.11", "commun"), ("3.1", "commun"), ("3.1.1.1.6", "commun"),
                                          ("3.1.8", "extension"), ("3.1.8.3", "extension"), ("3.2", "special"),
                                          ("4.1.2", "special"), ("3", "special")])
def test_scope_kind(numero: str, kind: str) -> None:
    assert p.scope_kind(numero) == kind
