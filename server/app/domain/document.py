"""AD-2 — Tout document est un arbre de blocs identifiés : Document → Node → Block → Line.

Amendements (story 1.1) : `Document.blocks` est le registre des blocs, validé par les invariants d'arbre ;
`Document.ingest_fingerprint` (symétrique de `source_hash`) est écrit par l'ingestion et vérifié par le loader.
"""

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
StructuralBlockKind = Literal["para", "heading", "table", "list", "autre"]
RelationKind = Literal["exception_de", "specialise", "contredit"]
# Typage des clauses : seul un kind `manual` ou `model_verified` est confirmé (AD-6, AD-8).
KindSource = Literal["manual", "model", "model_verified"]

DOC_ID_PATTERN = r"^[a-z0-9-]+$"
DOC_ID_MAX = 64
DOC_ID_RE = re.compile(DOC_ID_PATTERN)
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
    # Preuve locale du kind produit par l'ingestion source avant tout enrichissement sémantique.
    # Elle permet à une nouvelle lecture `autre` de retirer un ancien typage sans deviner si le
    # bloc était initialement un paragraphe, une liste, un tableau ou un titre.
    structural_kind: StructuralBlockKind | None = None
    kind_confidence: float | None = Field(default=None, ge=0, le=1)
    kind_source: KindSource | None = None
    source_field: str | None = None
    continues: str | None = None
    refs: list[str] = Field(default_factory=list)
    unresolved_refs: list[str] = Field(default_factory=list)
    defines: str | None = None
    scope_node_id: str | None = None
    # Portée explicite (amendement AD-2, revue Codex 1.2) : quand la clause ne couvre qu'une partie des descendants
    # de `scope_node_id` (« pour les extensions 3.1.8.3 à 3.1.8.6 »), la liste exhaustive des nœuds couverts ;
    # vide = tout le sous-arbre de `scope_node_id`.
    scope_node_ids: list[str] = Field(default_factory=list)
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


NON_CITABLE_SOURCE_FIELDS = frozenset({"preliminaire", "tdm"})


def is_citable(block: Block) -> bool:
    """Prédicat unique du rappel : les préliminaires restent auditables, jamais proposés comme preuve."""
    return block.kind != "autre" and block.source_field not in NON_CITABLE_SOURCE_FIELDS


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


class ParcoursCondition(DomainModel):
    """Condition de parcours d'une source : « ce nœud concerne un profil qui vérifie `si` » (story 2.3).

    C'est une **donnée de la source**, pas un jugement : la `timeline` de `kb.js` attache à chaque
    étape la fiche qu'elle vise et la condition de profil qui la rend pertinente (`si: {enfants:
    true}` → fiche `ecole`). L'ingestion n'en reprend que le couple `(node_id, si)` — jamais le texte
    des étapes, qui deviendrait citable alors qu'il n'appartient à aucune fiche (spec 1.1).

    Les valeurs de `si` sont celles que la source écrit : un booléen (`enfants`, `vehicule`) ou l'un
    des libellés sans accent du questionnaire (`"Louer"`, `"Acheter"`, `"Independant"`). C'est
    `domain/profil.py::noeuds_du_profil` — et lui seul — qui décide si un profil les satisfait.
    """

    node_id: str
    si: dict[str, str | bool] = Field(default_factory=dict)


class Document(DomainModel):
    # Une seule borne pour l'identité du document : ingestion, loader et contrats HTTP relisent
    # tous ce modèle ou ses constantes. Sans elle, un document valide pour le domaine pouvait être
    # impossible à adresser par l'API, donc disparaître de l'audit public.
    doc_id: str = Field(max_length=DOC_ID_MAX)
    kind: DocumentKind
    title: str
    edition: str
    lang: str = "fr"
    nodes: list[Node] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)  # registre ; Node.items reste la seule source de l'ordre
    # Conditions de parcours de la source (story 2.3) : vide par défaut — un contrat PDF n'en a pas,
    # et `exclude_defaults=True` laisse son `document.json` inchangé.
    parcours: list[ParcoursCondition] = Field(default_factory=list)
    source_url: str | None = None
    source_hash: str = ""
    ingest_fingerprint: str = ""  # empreinte du parseur/schéma/règles (AD-2 stabilité), couverte par document_hash
    _by_id: dict[str, Block] = PrivateAttr(default_factory=dict)
    # Rattachement bloc → nœud et portée déclarée de chaque nœud, tous deux **déjà** parcourus par
    # `_tree_invariants` (story 1.8) : la table AD-6 a besoin du nœud d'un bloc et du `scope.kind` de
    # ce nœud, et `domain/verdict.py` ne peut importer ni `corpus` ni `llm` (table des couches).
    # Les recalculer à chaque claim referait le parcours de `Node.items` pour rien.
    _node_of: dict[str, str] = PrivateAttr(default_factory=dict)
    _scope_of_node: dict[str, ScopeKind] = PrivateAttr(default_factory=dict)

    def block(self, block_id: str) -> Block:
        return self._by_id[block_id]

    def node_of(self, block_id: str) -> str:
        """Nœud auquel le bloc est rattaché (AD-2 : exactement un, garanti par les invariants d'arbre)."""
        return self._node_of[block_id]

    def node_scope_kind(self, node_id: str) -> ScopeKind:
        """`Node.scope.kind` du nœud **lui-même** — `commun` est le socle au sens d'AD-6 (règle 3).

        Aucune remontée vers les ancêtres, et ce n'est pas un oubli (revue 1.8) : l'ingestion résout
        la portée nœud par nœud, et un descendant **prime** son ancêtre. Dans le contrat AXA, `a3`
        porte `special` tandis que `a3.1` et tout son sous-arbre portent `commun` — hériter de
        l'ancêtre ferait sortir la garantie `p34:12` (nœud `a3.1.1.1.6`) du socle, et la règle (3)
        d'AD-6 ne rendrait plus jamais `couvert` sur ce contrat. Un nœud sans `scope` déclaré vaut
        déjà `commun` par le défaut d'AD-2 : il n'y a rien à aller chercher plus haut.
        """
        return self._scope_of_node[node_id]

    def scope_nodes(self, block_id: str) -> set[str]:
        """Nœuds couverts par la portée d'un bloc : `scope_node_ids` (et leurs descendants) s'ils sont donnés,
        sinon le sous-arbre de `scope_node_id` ; vide si le bloc n'a pas de portée."""
        b = self.block(block_id)
        roots = b.scope_node_ids or ([b.scope_node_id] if b.scope_node_id else [])
        by_id = {n.node_id: n for n in self.nodes}
        covered: set[str] = set()
        stack = list(roots)
        while stack:
            nid = stack.pop()
            if nid in covered:
                continue
            covered.add(nid)
            stack.extend(by_id[nid].children)
        return covered

    @model_validator(mode="after")
    def _tree_invariants(self) -> Document:
        """AD-8 : block_id uniques, références résolues, chaque bloc rattaché à exactement un nœud, une seule racine."""
        by_id: dict[str, Block] = {}
        prefix = f"{self.doc_id}:"
        for b in self.blocks:
            if b.block_id in by_id:
                raise ValueError(f"block_id dupliqué : {b.block_id}")
            if not b.block_id.startswith(prefix):
                raise ValueError(f"block_id {b.block_id!r} ne commence pas par {prefix!r}")
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
        for b in self.blocks:
            targets = {"refs": b.refs, "continues": [b.continues], "overrides": [b.overrides],
                       "relation.exception_de": [b.relation.exception_de], "relation.specialise": [b.relation.specialise],
                       "relation.contredit": [b.relation.contredit]}
            for field_name, ids in targets.items():
                for t in ids:
                    if t is not None and t not in by_id:
                        raise ValueError(f"{b.block_id}.{field_name} vise un bloc inconnu : {t}")
            if b.scope_node_id is not None and b.scope_node_id not in node_ids:
                raise ValueError(f"{b.block_id}.scope_node_id vise un nœud inconnu : {b.scope_node_id}")
            if len(set(b.scope_node_ids)) != len(b.scope_node_ids):
                raise ValueError(f"{b.block_id}.scope_node_ids contient un doublon")
            for t in b.scope_node_ids:
                if t not in node_ids:
                    raise ValueError(f"{b.block_id}.scope_node_ids vise un nœud inconnu : {t}")
        for condition in self.parcours:  # story 2.3 : un parcours ne désigne que des nœuds du document
            if condition.node_id not in node_ids:
                raise ValueError(f"parcours vise un nœud inconnu : {condition.node_id}")
        node_parent: dict[str, str] = {}
        children: dict[str, list[str]] = {n.node_id: n.children for n in self.nodes}
        for n in self.nodes:
            for c in n.children:
                if c in node_parent:
                    raise ValueError(f"nœud {c} rattaché à deux parents : {node_parent[c]}, {n.node_id}")
                node_parent[c] = n.node_id
        for b in self.blocks:  # une portée explicite reste dans le sous-arbre de `scope_node_id`
            if b.scope_node_id is not None:
                for t in b.scope_node_ids:
                    cur = t
                    while cur != b.scope_node_id and cur in node_parent:
                        cur = node_parent[cur]
                    if cur != b.scope_node_id:
                        raise ValueError(f"{b.block_id}.scope_node_ids : {t} n'est pas sous {b.scope_node_id}")
        # aucun cycle : en remontant les parents depuis chaque nœud, on doit atteindre une racine
        for start in children:
            seen = {start}
            cur = start
            while cur in node_parent:
                cur = node_parent[cur]
                if cur in seen:
                    raise ValueError(f"cycle de nœuds via {cur}")
                seen.add(cur)
        roots = [n for n in children if n not in node_parent]
        if self.nodes and len(roots) != 1:
            raise ValueError(f"exactement un nœud racine attendu, trouvé : {roots}")
        self._by_id = by_id
        self._node_of = parents
        self._scope_of_node = {n.node_id: n.scope.kind for n in self.nodes}
        return self

    @field_validator("doc_id")
    @classmethod
    def _doc_id_slug(cls, v: str) -> str:
        if not DOC_ID_RE.match(v):
            raise ValueError(f"doc_id doit être un slug [a-z0-9-]+ : {v!r}")
        return v
