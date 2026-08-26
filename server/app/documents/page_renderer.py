"""Rasterisation bornée de pages PDF et surlignage exact de lignes du corpus."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from starlette.concurrency import run_in_threadpool

from server.app.domain.document import Document
from server.app.domain.errors import CorpusUnavailable, InvalidRequest, Timeout

BboxTuple = tuple[float, float, float, float]
CacheKey = tuple[str, int, tuple[str, ...]]


@dataclass(frozen=True)
class VerifiedSource:
    """Chemin sélectionné au chargement, lié au hash déjà vérifié par le corpus."""

    path: Path
    sha256: str


@dataclass(frozen=True)
class PageRequest:
    """Demande déjà résolue contre le corpus, donc sans coordonnée venue du navigateur."""

    doc_id: str
    page: int
    block_ids: tuple[str, ...]
    line_ids: tuple[str, ...]
    rectangles: tuple[BboxTuple, ...]


@dataclass(frozen=True)
class RenderedPage:
    png: bytes
    page_count: int
    highlighted_lines: int


class PageRenderer:
    """Renderer PyMuPDF hors event loop, sous sémaphore et LRU borné."""

    def __init__(self, *, max_lines: int, max_blocks: int, concurrency: int, cache_pages: int,
                 dpi: int, max_pixels: int, queue_timeout_s: float) -> None:
        self.max_lines = max_lines
        self.max_blocks = max_blocks
        self.cache_pages = cache_pages
        self.dpi = dpi
        self.max_pixels = max_pixels
        self.queue_timeout_s = queue_timeout_s
        self._semaphore = asyncio.Semaphore(concurrency)
        self._cache: OrderedDict[CacheKey, RenderedPage] = OrderedDict()
        self._cache_lock = asyncio.Lock()
        self._inflight: dict[CacheKey, asyncio.Task[RenderedPage]] = {}

    def resolve(self, document: Document, page: int, block_ids: list[str],
                line_ids: list[str] | None = None) -> PageRequest:
        """Résout l'identité canonique des blocs avant toute ouverture de PDF."""
        if page < 1:
            raise InvalidRequest("page de document invalide")
        if len(block_ids) > self.max_blocks:
            raise InvalidRequest("trop de blocs demandés pour une page")
        if len(set(block_ids)) != len(block_ids):
            raise InvalidRequest("un bloc ne peut être demandé plusieurs fois")
        if line_ids is not None and len(line_ids) > self.max_lines:
            raise InvalidRequest("trop de lignes demandées pour une page")
        if line_ids is not None and len(set(line_ids)) != len(line_ids):
            raise InvalidRequest("une ligne ne peut être demandée plusieurs fois")
        if line_ids is not None and not block_ids:
            raise InvalidRequest("les lignes précises exigent l'identité canonique de leur bloc")

        block_by_id = {block.block_id: block for block in document.blocks}
        requested_blocks = []
        for block_id in block_ids:
            block = block_by_id.get(block_id)
            if block is None or block.page != page:
                raise InvalidRequest("bloc absent de la page demandée")
            requested_blocks.append(block)

        # Un `line_id` doit être une identité globale du corpus. Même deux occurrences aux mêmes
        # coordonnées seraient ambiguës : on refuse le document au lieu de choisir silencieusement.
        by_line_id: dict[str, tuple[str, int, BboxTuple | None]] = {}
        for block in document.blocks:
            if block.page is None:
                continue
            for line in block.lines:
                if line.line_id in by_line_id:
                    raise CorpusUnavailable("un identifiant de ligne est dupliqué dans le document servi")
                bbox = tuple(line.bbox) if line.bbox is not None else None
                by_line_id[line.line_id] = (block.block_id, block.page, bbox)

        selected_ids: list[str]
        if line_ids is None:
            selected_ids = []
            for block in requested_blocks:
                if not block.lines:
                    raise CorpusUnavailable("un bloc PDF publié ne porte aucune ligne localisable")
                selected_ids.extend(line.line_id for line in block.lines)
            if len(selected_ids) > self.max_lines:
                raise InvalidRequest("les blocs demandés contiennent trop de lignes pour une page")
        else:
            selected_ids = line_ids

        requested_set = set(block_ids)
        rectangles: list[BboxTuple] = []
        for line_id in selected_ids:
            resolved = by_line_id.get(line_id)
            if resolved is None or resolved[1] != page or resolved[0] not in requested_set:
                raise InvalidRequest("ligne absente des blocs demandés sur cette page")
            if resolved[2] is None:
                raise CorpusUnavailable("une ligne publiée ne peut pas être localisée dans le PDF")
            rectangles.append(resolved[2])

        # Une permutation des mêmes blocs/lignes produit la même image et partage la même clé LRU.
        ordered = sorted(zip(selected_ids, rectangles, strict=True), key=lambda item: item[0])
        return PageRequest(
            doc_id=document.doc_id,
            page=page,
            block_ids=tuple(sorted(block_ids)),
            line_ids=tuple(item[0] for item in ordered),
            rectangles=tuple(item[1] for item in ordered))

    async def _evict_document(self, doc_id: str) -> None:
        async with self._cache_lock:
            for key in [key for key in self._cache if key[0] == doc_id]:
                del self._cache[key]

    @staticmethod
    def _read_verified_source(source: VerifiedSource) -> bytes:
        payload = source.path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != source.sha256:
            raise CorpusUnavailable("le PDF source ne correspond plus à son empreinte vérifiée")
        return payload

    async def _await_render(self, task: asyncio.Task[RenderedPage]) -> RenderedPage:
        try:
            return await asyncio.shield(task)
        except InvalidRequest:
            raise
        except CorpusUnavailable:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise CorpusUnavailable("le PDF source vérifié est indisponible ou illisible") from exc

    async def _render_leader(self, key: CacheKey, pdf_bytes: bytes,
                             request: PageRequest) -> RenderedPage:
        """Le task partagé attend puis possède sa place, même si son appelant disparaît."""
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=self.queue_timeout_s)
                acquired = True
            except TimeoutError as exc:
                raise Timeout("la file de rendu PDF est momentanément saturée") from exc
            rendered = await run_in_threadpool(self._render_sync, pdf_bytes, request)
            async with self._cache_lock:
                self._cache[key] = rendered
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_pages:
                    self._cache.popitem(last=False)
            return rendered
        finally:
            async with self._cache_lock:
                if self._inflight.get(key) is asyncio.current_task():
                    del self._inflight[key]
            if acquired:
                self._semaphore.release()

    async def render(self, request: PageRequest, source: VerifiedSource | None) -> RenderedPage:
        if source is None or source.path.name != "source.pdf":
            raise CorpusUnavailable("le PDF source vérifié n'est pas disponible")
        key: CacheKey = (request.doc_id, request.page, request.line_ids)
        # Les octets sont lus et hachés dans le threadpool avant **tout** cache hit. Le rendu
        # consomme ces mêmes octets : aucune mutation du chemin entre hash et `open()` ne peut
        # faire rendre une source différente de celle qui vient d'être vérifiée.
        try:
            pdf_bytes = await run_in_threadpool(self._read_verified_source, source)
        except CorpusUnavailable:
            await self._evict_document(request.doc_id)
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            await self._evict_document(request.doc_id)
            raise CorpusUnavailable("le PDF source vérifié est indisponible ou illisible") from exc

        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            task = self._inflight.get(key)
            if task is None:
                # Le task est publié avant d'attendre le sémaphore : tous les appels identiques
                # partagent donc la même attente et le même rendu, même avec `concurrency=1`.
                task = asyncio.create_task(self._render_leader(key, pdf_bytes, request))
                self._inflight[key] = task

        return await self._await_render(task)

    def _render_sync(self, pdf_bytes: bytes, request: PageRequest) -> RenderedPage:
        """Seule fonction bloquante ; elle est toujours appelée dans le threadpool."""
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
            page_count = pdf.page_count
            if request.page > page_count:
                raise InvalidRequest("page absente du PDF source vérifié")
            page = pdf.load_page(request.page - 1)
            page_rect = page.rect
            for bbox in request.rectangles:
                x0, y0, x1, y1 = bbox
                if (not all(math.isfinite(value) for value in bbox)
                        or x0 >= x1 or y0 >= y1
                        or x0 < page_rect.x0 or y0 < page_rect.y0
                        or x1 > page_rect.x1 or y1 > page_rect.y1):
                    raise CorpusUnavailable("une ligne publiée porte une bbox invalide pour sa page PDF")

            scale = self.dpi / 72
            width_px = math.ceil(page_rect.width * scale)
            height_px = math.ceil(page_rect.height * scale)
            if width_px * height_px > self.max_pixels:
                raise InvalidRequest("la page dépasse la borne de pixels du renderer")

            for bbox in request.rectangles:
                # Rectangle exact de `Line.bbox`, sans repli sur `Block.bbox` ni élargissement par
                # recherche textuelle. L'opacité laisse la citation lisible.
                page.draw_rect(pymupdf.Rect(bbox), color=None, fill=(1.0, 0.86, 0.18),
                               fill_opacity=0.34, overlay=True, width=0)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            return RenderedPage(png=pixmap.tobytes("png"), page_count=page_count,
                                highlighted_lines=len(request.rectangles))
