"""Modèles partagés du domaine (couche `domain` : stdlib + pydantic, rien d'autre)."""

from .document import Block, BlockKind, BlockRef, Document, Line, Node, NodeRef, ParcoursCondition, Source
from .ingest import Check, CheckLevel, Gate, GateContext, Manifest, ManifestEntry, Report
from .retrieval import NodeChild, NodeWindow, RetrievalBudget, RetrievalResult

__all__ = [
    "Block", "BlockKind", "BlockRef", "Check", "CheckLevel", "Document", "Gate", "GateContext", "Line", "Manifest", "ManifestEntry", "Node",
    "NodeChild", "NodeRef", "NodeWindow", "ParcoursCondition", "Report", "RetrievalBudget", "RetrievalResult", "Source",
]
