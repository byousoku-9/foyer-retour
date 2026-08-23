"""AD-2 — Tout document est un arbre de blocs identifiés : Document → Node → Block → Line."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DocumentKind = Literal["guide", "contrat"]
ScopeKind = Literal["commun", "special", "extension"]
BlockKind = Literal[
    "para", "heading", "table", "list", "definition", "garantie",
    "exclusion", "condition", "franchise", "renvoi", "autre",
]
RelationKind = Literal["exception_de", "specialise", "contredit"]

DOC_ID_RE = re.compile(r"^[a-z0-9-]+$")
BLOCK_ID_RE = re.compile(r"^[a-z0-9-]+:(p\d+|f[^:]+|q\d+):\d+$")

# bbox = [x0, y0, x1, y1] en points PDF, origine haut-gauche.
Bbox = Annotated[list[float], Field(min_length=4, max_length=4)]


class Line(BaseModel):
    """Ligne d'un bloc PDF, pour le surlignage précis."""

    line_id: str
    text: str
    bbox: Bbox | None = None


class Scope(BaseModel):
    kind: ScopeKind = "commun"


class Source(BaseModel):
    """Lien officiel d'une fiche du guide."""

    titre: str
    url: str


class BlockRef(BaseModel):
    block_id: str


class NodeRef(BaseModel):
    node_id: str


class Relation(BaseModel):
    exception_de: str | None = None
    specialise: str | None = None
    contredit: str | None = None


class Block(BaseModel):
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

    @model_validator(mode="after")
    def _block_id_matches_loc_seq(self) -> Block:
        suffix = f":{self.loc}:{self.seq}"
        if not self.block_id.endswith(suffix):
            raise ValueError(f"block_id {self.block_id!r} doit se terminer par {suffix!r}")
        return self


class Node(BaseModel):
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


class Document(BaseModel):
    doc_id: str
    kind: DocumentKind
    title: str
    edition: str
    lang: str = "fr"
    nodes: list[Node] = Field(default_factory=list)
    source_url: str | None = None
    source_hash: str = ""

    @field_validator("doc_id")
    @classmethod
    def _doc_id_slug(cls, v: str) -> str:
        if not DOC_ID_RE.match(v):
            raise ValueError(f"doc_id doit être un slug [a-z0-9-]+ : {v!r}")
        return v
