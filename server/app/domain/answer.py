"""AD-3 / AD-4 — Claim, statuts, ébauche et réponse unique."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .verdict import Verdict

SegmentKind = Literal["factuel", "transition", "limite"]
Applicable = Literal["oui", "non", "humain"]
RejectionKind = Literal["non_retrouvee", "non_pertinente", "ambigue"]
AbsenceKind = Literal["hors_perimetre", "zero_hit", "claims_rejetes"]


class Quote(BaseModel):
    block_id: str
    quote: str


class Claim(BaseModel):
    claim_id: str
    text: str
    quotes: list[Quote] = Field(min_length=1)  # une quote par bloc


class ClaimStatus(BaseModel):
    retrouvee: bool
    pertinente: bool | None = None
    applicable: Applicable | None = None
    edition: str


class AnswerSegment(BaseModel):
    text: str
    kind: SegmentKind
    claim_ids: list[str] = Field(default_factory=list)


class AnswerDraft(BaseModel):
    """Sortie structurée de *rédiger*."""

    segments: list[AnswerSegment]
    claims: list[Claim]


class VerifiedClaim(Claim):
    """Claim après *vérifier* : statut calculé par le code, offsets pour le surlignage."""

    status: ClaimStatus
    line_ids: list[str] = Field(default_factory=list)


class RejectedClaim(VerifiedClaim):
    rejection_kind: RejectionKind
    motif: str = ""


class AbsenceProof(BaseModel):
    kind: AbsenceKind
    terms_searched: list[str] = Field(default_factory=list)  # termes canoniques seulement
    variants_count: int = 0
    blocks_scanned: int = 0
    documents: list[str] = Field(default_factory=list)


class Answer(BaseModel):
    """Seul objet de réponse des deux pipelines."""

    found: bool
    complete: bool
    lang: str = "fr"
    lang_fallback: bool = False
    texte: str = ""
    segments: list[AnswerSegment] = Field(default_factory=list)
    claims: list[VerifiedClaim] = Field(default_factory=list)
    rejected_claims: list[RejectedClaim] = Field(default_factory=list)
    reason: AbsenceProof | None = None
    verdict: Verdict | None = None
    unknown: list[str] = Field(default_factory=list)
    clarification: str | None = None
