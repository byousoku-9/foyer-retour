"""AD-1 — Budget et résultat de l'étape *retrouver* (commun à toutes les variantes)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .document import Block


class RetrievalBudget(BaseModel):
    """Borne toute l'étape : appels modèle, nœuds, blocs, tokens, définitions et renvois inclus."""

    max_opens: int
    node_window: int
    search_limit: int
    max_llm_turns: int
    max_blocks: int | None = None
    max_tokens: int | None = None


class RetrievalResult(BaseModel):
    blocs: list[Block] = Field(default_factory=list)
    opened_block_ids: list[str] = Field(default_factory=list)
    discarded_block_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
