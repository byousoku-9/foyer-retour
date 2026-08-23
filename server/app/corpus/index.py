"""AD-1 — Index déterministe du corpus et trois des quatre outils de *retrouver* : `sommaire`, `ouvrir_noeud`, `chercher`.

`definitions` arrive en 1.4. Les seuils (`node_window`, `limit`) sont passés par l'appelant depuis `config.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from server.app.domain import Block, BlockRef, Document, NodeRef, NodeWindow

from .loader import Corpus
from .text import normalize

_WORD = re.compile(r"[a-z0-9]+")


def words(text_norm: str) -> list[str]:
    """Mots d'un `text_norm` : suites alphanumériques (la ponctuation et l'apostrophe séparent)."""
    return _WORD.findall(text_norm)


@dataclass(frozen=True)
class _Entry:
    block: Block
    doc_id: str
    node_id: str
    rank: int  # ordre de lecture global
    tokens: frozenset[str]
    padded: str  # " mot mot … " pour les variantes multi-mots


def reading_order(doc: Document) -> list[tuple[str, str]]:
    """[(block_id, node_id)] par parcours en profondeur de `Node.items` depuis les racines."""
    by_id = {n.node_id: n for n in doc.nodes}
    referenced = {i.node_id for n in doc.nodes for i in n.items if isinstance(i, NodeRef)}
    roots = [n.node_id for n in doc.nodes if n.node_id not in referenced]
    out: list[tuple[str, str]] = []

    def visit(node_id: str) -> None:
        node = by_id[node_id]
        for item in node.items:
            if isinstance(item, BlockRef):
                out.append((item.block_id, node_id))
            else:
                visit(item.node_id)

    for r in roots:
        visit(r)
    return out


class Index:
    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self._entries: list[_Entry] = []
        self._by_block: dict[str, _Entry] = {}
        self._nodes: dict[str, tuple[str, list[str]]] = {}  # node_id → (doc_id, block_ids directs)
        for doc_id, doc in sorted(corpus.documents.items()):
            for n in doc.nodes:
                if n.node_id in self._nodes:
                    raise ValueError(f"node_id {n.node_id!r} présent dans {self._nodes[n.node_id][0]!r} et {doc_id!r}")
                self._nodes[n.node_id] = (doc_id, n.blocks)
            for block_id, node_id in reading_order(doc):
                if block_id in self._by_block:
                    raise ValueError(f"block_id {block_id!r} présent dans {self._by_block[block_id].doc_id!r} et {doc_id!r}")
                b = doc.block(block_id)
                if not b.text_norm:
                    b.text_norm = normalize(b.text)
                ws = words(b.text_norm)
                e = _Entry(block=b, doc_id=doc_id, node_id=node_id, rank=len(self._entries),
                           tokens=frozenset(ws), padded=f" {' '.join(ws)} ")
                self._entries.append(e)
                self._by_block[block_id] = e

    # --- accès ---------------------------------------------------------
    def parent_node(self, block_id: str) -> str:
        return self._by_block[block_id].node_id

    def doc_of(self, block_id: str) -> str:
        return self._by_block[block_id].doc_id

    def __len__(self) -> int:
        return len(self._entries)

    # --- outils AD-1 ---------------------------------------------------
    def sommaire(self, doc_id: str) -> str:
        return self.corpus.summaries[doc_id]

    def ouvrir_noeud(self, node_id: str, focus_block_id: str | None = None, cursor: int | None = None, *,
                     node_window: int) -> NodeWindow:
        """Blocs directs du nœud, par pages de `node_window` ; la page contenant `focus_block_id` si donné."""
        if node_window < 1:
            raise ValueError("node_window doit être ≥ 1")
        if focus_block_id is not None and cursor is not None:
            raise ValueError("focus_block_id et cursor sont exclusifs")
        doc_id, block_ids = self._nodes[node_id]
        start = 0
        if focus_block_id is not None:
            if focus_block_id not in block_ids:
                raise KeyError(f"{focus_block_id} n'est pas un bloc de {node_id}")
            start = (block_ids.index(focus_block_id) // node_window) * node_window
        elif cursor is not None:
            if cursor < 0 or cursor >= len(block_ids):
                raise ValueError(f"cursor {cursor} hors de [0, {len(block_ids)})")
            start = (cursor // node_window) * node_window
        end = start + node_window
        doc = self.corpus.documents[doc_id]
        return NodeWindow(node_id=node_id, blocks=[doc.block(b) for b in block_ids[start:end]],
                          truncated=len(block_ids) > node_window,
                          next_cursor=end if end < len(block_ids) else None)

    def chercher(self, termes: dict[str, list[str]] | Iterable[str], *, limit: int,
                 doc_id: str | None = None) -> list[tuple[str, str]]:
        """Correspondance mot entier sur `text_norm` ; tri par nb de termes canoniques distincts touchés, puis lecture."""
        if limit < 1:
            raise ValueError("limit doit être ≥ 1")
        if isinstance(termes, str):
            raise TypeError("termes : dict[str, list[str]] ou liste de termes attendus, pas une chaîne")
        if doc_id is not None and doc_id not in self.corpus.documents:
            raise KeyError(doc_id)
        mapping = termes if isinstance(termes, dict) else {t: [] for t in termes}
        groups: list[list[str]] = []
        seen_canon: set[str] = set()  # deux clés de même forme normalisée ne comptent qu'une fois
        for canon, variants in mapping.items():
            forms = {" ".join(words(normalize(v))) for v in (canon, *variants)} - {""}
            canon_form = " ".join(words(normalize(canon)))
            if not forms or canon_form in seen_canon:
                continue
            seen_canon.add(canon_form)
            groups.append(sorted(forms))
        if not groups:
            return []
        scored: list[tuple[int, int, str, str]] = []
        for e in self._entries:
            if doc_id is not None and e.doc_id != doc_id:
                continue
            score = sum(1 for forms in groups if any(self._hit(e, f) for f in forms))
            if score:
                scored.append((-score, e.rank, e.block.block_id, e.node_id))
        scored.sort()
        return [(b, n) for _, _, b, n in scored[:limit]]

    @staticmethod
    def _hit(e: _Entry, form: str) -> bool:
        return form in e.tokens if " " not in form else f" {form} " in e.padded
