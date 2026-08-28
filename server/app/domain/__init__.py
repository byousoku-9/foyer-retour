"""Modèles partagés du domaine (couche `domain` : stdlib + pydantic, rien d'autre)."""

from .document import Block, BlockKind, BlockRef, Document, Line, Node, NodeRef, ParcoursCondition, Source, is_citable
from .ingest import Check, CheckLevel, Gate, GateContext, GateDecision, Manifest, ManifestEntry, Report
from .retrieval import NodeChild, NodeWindow, RetrievalBudget, RetrievalResult

__all__ = [
    "Block", "BlockKind", "BlockRef", "Check", "CheckLevel", "Document", "Gate", "GateContext", "GateDecision", "Line", "Manifest", "ManifestEntry", "Node",
    "NodeChild", "NodeRef", "NodeWindow", "ParcoursCondition", "Report", "RetrievalBudget", "RetrievalResult", "Source",
    "is_citable",
]
