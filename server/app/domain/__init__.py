"""Modèles partagés du domaine (couche `domain` : stdlib + pydantic, rien d'autre)."""

from .document import (Block, BlockKind, BlockRef, ContextRole, Document, Line, Node,
                       NodeRef, NodeRelation, ParcoursCondition, Source, SurfaceClass,
                       is_citable)
from .ingest import Check, CheckLevel, Gate, GateContext, GateDecision, Manifest, ManifestEntry, Report
from .retrieval import (AdmissionDecision, BudgetSnapshot, ContextUnit, FacetteCouverture,
                        FullContextSelection,
                        NodeChild, NodeWindow, QuestionClauseScore, RetrievalBudget,
                        RetrievalResult, ScoredHit, SemanticSufficiencySelection,
                        SufficiencyDecision)
from .retrieval import SummaryEntry, SummaryPage

__all__ = [
    "AdmissionDecision", "Block", "BlockKind", "BlockRef", "BudgetSnapshot", "Check",
    "CheckLevel", "ContextRole", "ContextUnit", "Document", "FacetteCouverture",
    "FullContextSelection", "Gate",
    "GateContext", "GateDecision", "Line", "Manifest", "ManifestEntry", "Node", "NodeChild",
    "NodeRef", "NodeRelation", "NodeWindow", "ParcoursCondition", "QuestionClauseScore", "Report",
    "RetrievalBudget", "RetrievalResult", "ScoredHit", "SemanticSufficiencySelection", "Source",
    "SufficiencyDecision",
    "SurfaceClass", "is_citable",
    "SummaryEntry", "SummaryPage",
]
