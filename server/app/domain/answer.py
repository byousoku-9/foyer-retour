"""AD-3 / AD-4 — Claim, statuts, ébauche et réponse unique."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .document import DomainModel

from .verdict import Verdict

SegmentKind = Literal["factuel", "transition", "limite"]
Applicable = Literal["oui", "non", "humain"]
RejectionKind = Literal["non_retrouvee", "non_pertinente", "ambigue"]
AbsenceKind = Literal["hors_perimetre", "zero_hit", "claims_rejetes"]


class Quote(DomainModel):
    block_id: str
    quote: str


class Claim(DomainModel):
    claim_id: str
    text: str
    quotes: list[Quote] = Field(min_length=1)  # une quote par bloc

    @field_validator("quotes")
    @classmethod
    def _one_quote_per_block(cls, quotes: list[Quote]) -> list[Quote]:
        ids = [q.block_id for q in quotes]
        dupes = sorted({b for b in ids if ids.count(b) > 1})
        if dupes:
            raise ValueError(f"une seule quote par bloc : {dupes}")
        return quotes


class ClaimStatus(DomainModel):
    retrouvee: bool
    pertinente: bool | None = None
    applicable: Applicable | None = None
    edition: str


class AnswerSegment(DomainModel):
    text: str
    kind: SegmentKind
    claim_ids: list[str] = Field(default_factory=list)


class AnswerDraft(DomainModel):
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


class AbsenceProof(DomainModel):
    kind: AbsenceKind
    terms_searched: list[str] = Field(default_factory=list)  # termes canoniques seulement
    variants_count: int = 0
    blocks_scanned: int = 0
    documents: list[str] = Field(default_factory=list)


class Answer(DomainModel):
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

    @model_validator(mode="after")
    def _found_coherence(self) -> Answer:
        if not self.found and self.reason is None:
            raise ValueError("found=False exige une preuve d'absence (reason)")
        if self.found and not self.claims:
            raise ValueError("found=True exige au moins une claim retrouvée et pertinente")
        if not self.found and self.claims:
            raise ValueError("found=False exige claims=[]")
        bad = [c.claim_id for c in self.claims if not (c.status.retrouvee is True and c.status.pertinente is True)]
        if bad:
            raise ValueError(f"claims[] ne contient que des claims retrouvee ∧ pertinente ; fautives : {bad}")
        if self.complete and (not self.found or self.unknown):
            raise ValueError("complete=True exige found=True et unknown=[]")
        return self
