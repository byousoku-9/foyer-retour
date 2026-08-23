"""Modèles partagés du domaine (couche `domain` : stdlib + pydantic, rien d'autre)."""

from .document import Block, BlockKind, BlockRef, Document, Line, Node, NodeRef, Source
from .ingest import Check, CheckLevel, Gate, GateContext, Manifest, ManifestEntry, Report
from .retrieval import NodeWindow, RetrievalBudget, RetrievalResult

__all__ = [
    "Block", "BlockKind", "BlockRef", "Check", "CheckLevel", "Document", "Gate", "GateContext", "Line", "Manifest", "ManifestEntry", "Node",
    "NodeRef", "NodeWindow", "Report", "RetrievalBudget", "RetrievalResult", "Source",
]
