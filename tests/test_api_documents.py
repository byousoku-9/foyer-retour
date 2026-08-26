from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.app.api.errors import gestionnaire_pipeline
from server.app.api.main import create_app
from server.app.api.routes import documents as route_documents
from server.app.config import Settings
from server.app.corpus.loader import Corpus
from server.app.api.page_renderer import PageRenderer, RenderedPage, VerifiedSource
from server.app.domain.document import Block, BlockRef, Document, Line, Node
from server.app.domain.errors import CorpusUnavailable, InvalidRequest, PipelineError, Timeout

DOC_ID = "cg-mini"
BLOCK_1 = f"{DOC_ID}:p1:1"
BLOCK_1_B = f"{DOC_ID}:p1:2"
BLOCK_2 = f"{DOC_ID}:p2:1"
LINE_1 = "p1:1:l1"
LINE_1_B = "p1:2:l1"
LINE_2 = "p2:1:l1"


def _document() -> Document:
    blocks = [
        Block(block_id=BLOCK_1, text="ligne une", loc="p1", seq=1, page=1,
              bbox=[10, 10, 180, 80], lines=[Line(line_id=LINE_1, text="ligne une", bbox=[30, 40, 120, 55])]),
        Block(block_id=BLOCK_1_B, text="ligne une bis", loc="p1", seq=2, page=1,
              bbox=[10, 90, 180, 140], lines=[Line(line_id=LINE_1_B, text="ligne une bis", bbox=[35, 100, 130, 115])]),
        Block(block_id=BLOCK_2, text="ligne deux", loc="p2", seq=1, page=2,
              bbox=[10, 10, 180, 80], lines=[Line(line_id=LINE_2, text="ligne deux", bbox=[35, 60, 130, 75])]),
    ]
    return Document(doc_id=DOC_ID, kind="contrat", title="CG mini", edition="test",
                    nodes=[Node(node_id="root", items=[BlockRef(block_id=b.block_id) for b in blocks])],
                    blocks=blocks, ingest_fingerprint="fp")


def _pdf(path: Path, *, pages: int = 3, width: float = 200, height: float = 200) -> None:
    pdf = pymupdf.open()
    for _ in range(pages):
        pdf.new_page(width=width, height=height)
    pdf.save(path)
    pdf.close()


def _verified(path: Path) -> VerifiedSource:
    return VerifiedSource(path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _renderer(*, max_lines: int = 24, max_blocks: int = 10, concurrency: int = 2,
              cache_pages: int = 2, dpi: int = 72, max_pixels: int = 1_000_000,
              queue_timeout_s: float = 1.0) -> PageRenderer:
    return PageRenderer(max_lines=max_lines, max_blocks=max_blocks, concurrency=concurrency,
                        cache_pages=cache_pages, dpi=dpi, max_pixels=max_pixels,
                        queue_timeout_s=queue_timeout_s)


@contextmanager
def _client(tmp_path: Path, *, document: Document | None = None,
            renderer: PageRenderer | None = None, corrupt: bool = False) -> Iterator[TestClient]:
    source_path = tmp_path / "source.pdf"
    if corrupt:
        source_path.write_bytes(b"ceci n'est pas un PDF")
    else:
        _pdf(source_path)
    source = _verified(source_path)
    corpus = Corpus(documents={DOC_ID: document or _document()})
    app = FastAPI()
    app.add_exception_handler(PipelineError, gestionnaire_pipeline)
    app.include_router(route_documents.router, prefix="/api/v1")
    app.state.foyer = SimpleNamespace(
        corpus=corpus, page_renderer=renderer or _renderer(), pdf_sources={DOC_ID: source},
        reports={}, source_urls={})
    with TestClient(app) as client:
        yield client


def _pixel(png: bytes, x: int, y: int) -> tuple[int, ...]:
    return tuple(pymupdf.Pixmap(png).pixel(x, y))


def test_page_png_est_paresseuse_exacte_et_sans_repli_bbox(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path) as client:
        renderer = client.app.state.foyer.page_renderer
        original = renderer._render_sync
        calls = 0

        def counted(source: bytes, request: object) -> RenderedPage:
            nonlocal calls
            calls += 1
            return original(source, request)

        monkeypatch.setattr(renderer, "_render_sync", counted)
        assert calls == 0

        plain = client.get(f"/api/v1/documents/{DOC_ID}/pages/1.png")
        highlighted = client.get(
            f"/api/v1/documents/{DOC_ID}/pages/1.png",
            params=[("blocks", BLOCK_1), ("line_ids", LINE_1)])

        assert plain.status_code == highlighted.status_code == 200
        assert highlighted.headers["x-document-pages"] == "3"
        assert highlighted.headers["x-highlighted-lines"] == "1"
        assert _pixel(plain.content, 60, 47) != _pixel(highlighted.content, 60, 47)
        assert _pixel(plain.content, 15, 20) == _pixel(highlighted.content, 15, 20)
        assert plain.headers["x-highlighted-lines"] == "0"
        assert _pixel(plain.content, 20, 20) == (255, 255, 255)


def test_blocks_seuls_surlignent_deux_blocs_compatibles(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            f"/api/v1/documents/{DOC_ID}/pages/1.png",
            params={"blocks": f"{BLOCK_1},{BLOCK_1_B}"})
        assert response.status_code == 200
        assert response.headers["x-highlighted-lines"] == "2"
        assert _pixel(response.content, 60, 47) != (255, 255, 255)
        assert _pixel(response.content, 60, 107) != (255, 255, 255)


def test_citation_explicitement_sans_lignes_ne_surligne_pas_le_bloc(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            f"/api/v1/documents/{DOC_ID}/pages/1.png?blocks={BLOCK_1}&line_ids=")
        assert response.status_code == 200
        assert response.headers["x-highlighted-lines"] == "0"
        assert _pixel(response.content, 60, 47) == (255, 255, 255)


def test_page_finale_sans_bloc_est_valide_selon_le_vrai_pdf(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get(f"/api/v1/documents/{DOC_ID}/pages/3.png")
        assert response.status_code == 200
        assert response.headers["x-document-page"] == "3"
        assert response.headers["x-document-pages"] == "3"


def test_deux_requetes_identiques_reutilisent_le_cache_borne(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, renderer=_renderer(cache_pages=1)) as client:
        renderer = client.app.state.foyer.page_renderer
        original = renderer._render_sync
        calls = 0

        def counted(source: bytes, request: object) -> RenderedPage:
            nonlocal calls
            calls += 1
            return original(source, request)

        monkeypatch.setattr(renderer, "_render_sync", counted)
        url = f"/api/v1/documents/{DOC_ID}/pages/1.png?blocks={BLOCK_1}"
        assert client.get(url).status_code == 200
        assert client.get(url).status_code == 200
        assert calls == 1
        assert len(renderer._cache) == 1


def test_source_changee_apres_cache_est_refusee_sans_png_obsolete(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path) as client:
        renderer = client.app.state.foyer.page_renderer
        original = renderer._render_sync
        calls = 0

        def counted(source: bytes, request: object) -> RenderedPage:
            nonlocal calls
            calls += 1
            return original(source, request)

        monkeypatch.setattr(renderer, "_render_sync", counted)
        url = f"/api/v1/documents/{DOC_ID}/pages/1.png?blocks={BLOCK_1}"
        assert client.get(url).status_code == 200
        _pdf(client.app.state.foyer.pdf_sources[DOC_ID].path, pages=1)
        response = client.get(url)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "corpus_unavailable"
        assert calls == 1
        assert len(renderer._cache) == 0


async def test_rendus_concurrents_sont_bornes_et_ne_bloquent_pas_event_loop(tmp_path: Path) -> None:
    source_path = tmp_path / "source.pdf"
    _pdf(source_path)
    source = _verified(source_path)
    document = _document()
    renderer = _renderer(concurrency=1, cache_pages=1)
    active = 0
    maximum = 0
    lock = threading.Lock()

    def slow(_source: bytes, request: object) -> RenderedPage:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return RenderedPage(png=b"png", page_count=3,
                            highlighted_lines=len(request.rectangles))  # type: ignore[attr-defined]

    renderer._render_sync = slow  # type: ignore[method-assign]
    marker_ran_while_rendering = False

    async def marker() -> None:
        nonlocal marker_ran_while_rendering
        await asyncio.sleep(0.01)
        with lock:
            marker_ran_while_rendering = active > 0

    await asyncio.gather(
        renderer.render(renderer.resolve(document, 1, [BLOCK_1]), source),
        renderer.render(renderer.resolve(document, 2, [BLOCK_2]), source),
        marker())
    assert maximum == 1
    assert marker_ran_while_rendering is True
    assert len(renderer._cache) == 1


async def test_deux_misses_concurrentes_identiques_partagent_un_seul_rendu(tmp_path: Path) -> None:
    source_path = tmp_path / "source.pdf"
    _pdf(source_path)
    source = _verified(source_path)
    # Le follower identique ne doit pas consommer une deuxième place ni expirer dans la file.
    renderer = _renderer(concurrency=1, queue_timeout_s=0.01)
    request = renderer.resolve(_document(), 1, [BLOCK_1])
    calls = 0

    def slow(_source: bytes, _request: object) -> RenderedPage:
        nonlocal calls
        calls += 1
        time.sleep(0.04)
        return RenderedPage(png=b"partage", page_count=3, highlighted_lines=1)

    renderer._render_sync = slow  # type: ignore[method-assign]
    first, second = await asyncio.gather(renderer.render(request, source), renderer.render(request, source))
    assert calls == 1
    assert first is second


async def test_attente_du_semaphore_est_bornee(tmp_path: Path) -> None:
    source_path = tmp_path / "source.pdf"
    _pdf(source_path)
    source = _verified(source_path)
    renderer = _renderer(concurrency=1, queue_timeout_s=0.01)
    started = threading.Event()

    def slow(_source: bytes, _request: object) -> RenderedPage:
        started.set()
        time.sleep(0.06)
        return RenderedPage(png=b"png", page_count=3, highlighted_lines=1)

    renderer._render_sync = slow  # type: ignore[method-assign]
    first = asyncio.create_task(renderer.render(renderer.resolve(_document(), 1, [BLOCK_1]), source))
    await asyncio.to_thread(started.wait, 1)
    with pytest.raises(Timeout):
        await renderer.render(renderer.resolve(_document(), 2, [BLOCK_2]), source)
    await first


@pytest.mark.parametrize("suffix", [
    "/documents/inconnu/pages/1.png",
    f"/documents/{DOC_ID}/pages/4.png",
    f"/documents/{DOC_ID}/pages/1.png?blocks=inconnu",
    f"/documents/{DOC_ID}/pages/1.png?blocks={BLOCK_1}&line_ids=inconnue",
    f"/documents/{DOC_ID}/pages/1.png?blocks={BLOCK_1}&line_ids={LINE_2}",
    f"/documents/{DOC_ID}/pages/1.png?blocks={BLOCK_1}&line_ids={LINE_1}&line_ids={LINE_1}",
    f"/documents/{DOC_ID}/pages/1.png?line_ids={LINE_1}",
    f"/documents/{DOC_ID}/pages/1.png?inconnu=1",
])
def test_entrees_hostiles_sont_refusees_sans_exposer_de_chemin(tmp_path: Path, suffix: str) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/v1" + suffix)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert str(tmp_path) not in response.text


def test_onze_blocs_sont_refuses_avant_rasterisation_avec_le_defaut_reel(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    renderer = _renderer()  # défaut 3.4 : 10 blocs
    with _client(tmp_path, renderer=renderer) as client:
        def forbidden(*_args: object) -> RenderedPage:
            raise AssertionError("PyMuPDF ne doit pas être appelé")

        monkeypatch.setattr(renderer, "_render_sync", forbidden)
        blocks = ",".join(f"{DOC_ID}:p1:{index}" for index in range(1, 12))
        response = client.get(f"/api/v1/documents/{DOC_ID}/pages/1.png?blocks={blocks}")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


def test_line_id_duplique_dans_le_corpus_est_refuse_meme_si_bbox_identique(tmp_path: Path) -> None:
    document = _document()
    document.blocks[1].lines[0].line_id = LINE_1
    document.blocks[1].lines[0].bbox = list(document.blocks[0].lines[0].bbox or [])
    with _client(tmp_path, document=document) as client:
        response = client.get(f"/api/v1/documents/{DOC_ID}/pages/1.png?blocks={BLOCK_1}")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "corpus_unavailable"


@pytest.mark.parametrize("bbox", [
    [float("nan"), 10, 20, 20], [30, 10, 20, 20], [10, 10, 10, 20], [10, 10, 220, 20],
])
async def test_bbox_invalide_est_refusee_avant_dessin(tmp_path: Path, bbox: list[float]) -> None:
    source_path = tmp_path / "source.pdf"
    _pdf(source_path)
    document = _document()
    document.blocks[0].lines[0].bbox = bbox
    renderer = _renderer()
    request = renderer.resolve(document, 1, [BLOCK_1], [LINE_1])
    with pytest.raises(CorpusUnavailable, match="bbox invalide"):
        await renderer.render(request, _verified(source_path))


async def test_page_extreme_depasse_les_pixels_avant_get_pixmap(tmp_path: Path) -> None:
    source_path = tmp_path / "source.pdf"
    _pdf(source_path, pages=1, width=1000, height=1000)
    renderer = _renderer(max_pixels=999_999)
    with pytest.raises(InvalidRequest, match="pixels"):
        await renderer.render(renderer.resolve(_document(), 1, [], None), _verified(source_path))


def test_pdf_absent_rend_une_erreur_typee_sans_chemin_local(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        source = client.app.state.foyer.pdf_sources.pop(DOC_ID)
        response = client.get(f"/api/v1/documents/{DOC_ID}/pages/1.png")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "corpus_unavailable"
        assert str(source.path) not in response.text


def test_pdf_present_mais_corrompu_rend_503_sans_chemin(tmp_path: Path) -> None:
    with _client(tmp_path, corrupt=True) as client:
        response = client.get(f"/api/v1/documents/{DOC_ID}/pages/1.png")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "corpus_unavailable"
        assert str(tmp_path) not in response.text


def _data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    doc_dir = data_dir / DOC_ID
    doc_dir.mkdir(parents=True)
    source = doc_dir / "source.pdf"
    _pdf(source)
    document = _document()
    document.source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    document_json = document.model_dump_json().encode()
    (doc_dir / "document.json").write_bytes(document_json)
    (doc_dir / "summary.md").write_text("# CG mini\n", encoding="utf-8")
    manifest = {
        DOC_ID: {
            "status": "servi",
            "source_hash": document.source_hash,
            "ingest_fingerprint": document.ingest_fingerprint,
            "document_hash": hashlib.sha256(document_json).hexdigest(),
            "edition": document.edition,
            "overlay_hash": None,
            "gate": None,
        }
    }
    (data_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return data_dir


def test_lifespan_reel_branche_la_source_verifiee_du_loader(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, env="dev", allow_ungated=True,
                        guide_doc_id=DOC_ID, sinistre_doc_id=DOC_ID)
    with TestClient(create_app(settings, data_dir=_data_dir(tmp_path))) as client:
        response = client.get(
            f"/api/v1/documents/{DOC_ID}/pages/1.png",
            params=[("blocks", BLOCK_1), ("line_ids", LINE_1)])
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        source = client.app.state.foyer.pdf_sources[DOC_ID]
        assert source.sha256 == hashlib.sha256(source.path.read_bytes()).hexdigest()
