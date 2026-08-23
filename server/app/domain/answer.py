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
        # AD-10/AD-15 (revue Codex 1.4, B7) : le message d'un validateur du domaine est recopié tel
        # quel dans `StepTrace.checks` et dans la relance ; il ne cite donc **jamais** une valeur reçue
        # (`block_id`, `claim_id`) — elles viennent du modèle et sont du contenu non fiable. Le chemin
        # pydantic (`claims.2.quotes`) suffit à situer la faute, et il est produit par le code.
        ids = [q.block_id for q in quotes]
        if len(set(ids)) != len(ids):
            raise ValueError("deux quotes portent le même block_id (une seule quote par bloc dans une claim) : "
                             "regrouper le passage, ou faire deux claims distinctes")
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

    @model_validator(mode="after")
    def _draft_coherence(self) -> AnswerDraft:
        """AD-3 (story 1.4) : validé au parse ⇒ un draft incohérent déclenche le retry motivé du client."""
        ids = [c.claim_id for c in self.claims]
        if len(set(ids)) != len(ids):
            # Aucune valeur reçue dans le message : elle viendrait du modèle (revue Codex 1.4, B7).
            raise ValueError("deux claims portent le même claim_id (chaque claim_id est unique)")
        known = set(ids)
        for i, s in enumerate(self.segments):
            if set(s.claim_ids) - known:
                raise ValueError(f"segments[{i}] référence un claim_id absent de claims[] "
                                 "(n'utiliser que les claim_id déclarés dans claims[])")
            if s.kind == "factuel" and not s.claim_ids:
                raise ValueError(f"segments[{i}] est factuel sans claim_id (chaque segment factuel cite ≥ 1 claim)")
        return self


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
        if any(not (c.status.retrouvee is True and c.status.pertinente is True) for c in self.claims):
            raise ValueError("claims[] ne contient que des claims retrouvee ∧ pertinente")
        if self.complete and (not self.found or self.unknown):
            raise ValueError("complete=True exige found=True et unknown=[]")
        return self
