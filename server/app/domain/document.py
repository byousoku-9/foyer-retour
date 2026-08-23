"""AD-2 — Tout document est un arbre de blocs identifiés : Document → Node → Block → Line."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

DocumentKind = Literal["guide", "contrat"]
ScopeKind = Literal["commun", "special", "extension"]
BlockKind = Literal[
    "para", "heading", "table", "list", "definition", "garantie",
    "exclusion", "condition", "franchise", "renvoi", "autre",
]
RelationKind = Literal["exception_de", "specialise", "contredit"]
# Typage des clauses : seul un kind `manual` ou `model_verified` est confirmé (AD-6, AD-8).
KindSource = Literal["manual", "model", "model_verified"]

DOC_ID_RE = re.compile(r"^[a-z0-9-]+$")
BLOCK_ID_RE = re.compile(r"^[a-z0-9-]+:(p\d+|f[^:]+|q\d+):\d+$")

# bbox = [x0, y0, x1, y1] en points PDF, origine haut-gauche.
Bbox = Annotated[list[float], Field(min_length=4, max_length=4)]


class DomainModel(BaseModel):
    """Contrat interne : aucun champ inconnu n'est toléré."""

    model_config = ConfigDict(extra="forbid")


class Line(DomainModel):
    """Ligne d'un bloc PDF, pour le surlignage précis."""

    line_id: str
    text: str
    bbox: Bbox | None = None


class Scope(DomainModel):
    kind: ScopeKind = "commun"


class Source(DomainModel):
    """Lien officiel d'une fiche du guide."""

    titre: str
    url: str


class BlockRef(DomainModel):
    block_id: str


class NodeRef(DomainModel):
    node_id: str


class Relation(DomainModel):
    exception_de: str | None = None
    specialise: str | None = None
    contredit: str | None = None


class Block(DomainModel):
    block_id: str
    text: str
    text_norm: str = ""  # calculé au chargement par corpus.normalize()
    lang: str = "fr"
    loc: str
    seq: int = Field(ge=1)
    page: int | None = None
    bbox: Bbox | None = None
    kind: BlockKind = "para"
    kind_confidence: float | None = None
    kind_source: KindSource | None = None
    source_field: str | None = None
    continues: str | None = None
    refs: list[str] = Field(default_factory=list)
    unresolved_refs: list[str] = Field(default_factory=list)
    defines: str | None = None
    scope_node_id: str | None = None
    overrides: str | None = None
    relation: Relation = Field(default_factory=Relation)
    lines: list[Line] = Field(default_factory=list)

    @field_validator("block_id")
    @classmethod
    def _block_id_format(cls, v: str) -> str:
        if not BLOCK_ID_RE.match(v):
            raise ValueError(f"block_id invalide (attendu '{{doc_id}}:{{loc}}:{{seq}}') : {v!r}")
        return v

    @property
    def kind_confirmed(self) -> bool:
        return self.kind_source in ("manual", "model_verified")

    @model_validator(mode="after")
    def _block_id_matches_loc_seq(self) -> Block:
        suffix = f":{self.loc}:{self.seq}"
        if not self.block_id.endswith(suffix):
            raise ValueError(f"block_id {self.block_id!r} doit se terminer par {suffix!r}")
        return self


class Node(DomainModel):
    node_id: str
    level: int = 0
    title: str = ""
    items: list[BlockRef | NodeRef] = Field(default_factory=list)  # source unique de l'ordre de lecture
    scope: Scope = Field(default_factory=Scope)
    sources: list[Source] = Field(default_factory=list)

    @property
    def children(self) -> list[str]:
        """Projection : node_ids dans l'ordre de lecture."""
        return [i.node_id for i in self.items if isinstance(i, NodeRef)]

    @property
    def blocks(self) -> list[str]:
        """Projection : block_ids dans l'ordre de lecture."""
        return [i.block_id for i in self.items if isinstance(i, BlockRef)]


class Document(DomainModel):
    doc_id: str
    kind: DocumentKind
    title: str
    edition: str
    lang: str = "fr"
    nodes: list[Node] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)  # registre ; Node.items reste la seule source de l'ordre
    source_url: str | None = None
    source_hash: str = ""
    _by_id: dict[str, Block] = PrivateAttr(default_factory=dict)

    def block(self, block_id: str) -> Block:
        return self._by_id[block_id]

    @model_validator(mode="after")
    def _tree_invariants(self) -> Document:
        """AD-8 : block_id uniques, références résolues, chaque bloc rattaché à exactement un nœud."""
        by_id: dict[str, Block] = {}
        for b in self.blocks:
            if b.block_id in by_id:
                raise ValueError(f"block_id dupliqué : {b.block_id}")
            by_id[b.block_id] = b
        node_ids = {n.node_id for n in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("node_id dupliqué")
        parents: dict[str, str] = {}
        for n in self.nodes:
            for item in n.items:
                if isinstance(item, NodeRef):
                    if item.node_id not in node_ids:
                        raise ValueError(f"{n.node_id} référence un nœud inconnu : {item.node_id}")
                elif item.block_id not in by_id:
                    raise ValueError(f"{n.node_id} référence un bloc inconnu : {item.block_id}")
                elif item.block_id in parents:
                    raise ValueError(f"bloc {item.block_id} rattaché à deux nœuds : {parents[item.block_id]}, {n.node_id}")
                else:
                    parents[item.block_id] = n.node_id
        orphans = sorted(set(by_id) - set(parents))
        if orphans:
            raise ValueError(f"blocs orphelins (sans nœud) : {orphans}")
        node_parent: dict[str, str] = {}
        children: dict[str, list[str]] = {n.node_id: n.children for n in self.nodes}
        for n in self.nodes:
            for c in n.children:
                if c in node_parent:
                    raise ValueError(f"nœud {c} rattaché à deux parents : {node_parent[c]}, {n.node_id}")
                node_parent[c] = n.node_id
        # aucun cycle : en remontant les parents depuis chaque nœud, on doit atteindre une racine
        for start in children:
            seen = {start}
            cur = start
            while cur in node_parent:
                cur = node_parent[cur]
                if cur in seen:
                    raise ValueError(f"cycle de nœuds via {cur}")
                seen.add(cur)
        self._by_id = by_id
        return self

    @field_validator("doc_id")
    @classmethod
    def _doc_id_slug(cls, v: str) -> str:
        if not DOC_ID_RE.match(v):
            raise ValueError(f"doc_id doit être un slug [a-z0-9-]+ : {v!r}")
        return v
