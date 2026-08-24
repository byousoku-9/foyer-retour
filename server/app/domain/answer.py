"""AD-3 / AD-4 — Claim, statuts, ébauche et réponse unique."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .document import DomainModel

from .verdict import Verdict

SegmentKind = Literal["factuel", "transition", "limite"]
Applicable = Literal["oui", "non", "humain"]
RejectionKind = Literal["non_retrouvee", "non_pertinente", "ambigue"]
# `clarification_requise` amende AD-4 (story 1.5) : `Answer` exige un `reason` dès que `found=False`, et
# aucun des trois kinds d'origine ne décrit « la question n'a pas pu être rendue autonome » (AD-5, deux
# sorties exclusives de *comprendre*). Répondre `hors_perimetre` à une anaphore irrésoluble serait un
# mensonge : la question relève peut-être du périmètre, on ne le sait pas encore.
AbsenceKind = Literal["hors_perimetre", "zero_hit", "claims_rejetes", "clarification_requise"]


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

    def digest(self) -> str:
        """Hash canonique du draft (AD-3) : deux ébauches identiques ⇒ même empreinte.

        AD-3 : « chaque relance change quelque chose (un draft identique — même hash canonique —
        arrête la relance) ». Canonique = le contenu, débarrassé de ce qui n'en est pas : les blancs
        de chaque texte sont réduits à une espace simple, les clés JSON triées. L'**ordre** des
        segments et des claims, lui, fait partie de l'ébauche : deux ordres différents sont deux
        réponses différentes. Le hash ne sert qu'à comparer deux drafts de la même requête : il n'est
        ni un identifiant stable entre versions, ni une donnée exposée.
        """
        def plat(texte: str) -> str:
            return " ".join(texte.split())

        canon = {
            "segments": [{"text": plat(s.text), "kind": s.kind, "claim_ids": list(s.claim_ids)}
                         for s in self.segments],
            "claims": [{"claim_id": plat(c.claim_id), "text": plat(c.text),
                        "quotes": [{"block_id": plat(q.block_id), "quote": plat(q.quote)} for q in c.quotes]}
                       for c in self.claims],
        }
        payload = json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class VerifiedQuote(Quote):
    """Quote retrouvée dans le bloc relu depuis le corpus : où, exactement (AD-3).

    `start`/`end` sont des offsets dans `Block.text_norm` — la seule forme sur laquelle l'inclusion a
    été prouvée (convention Texte : `normalize()` est l'unique normalisation admise). `line_ids` sont
    les lignes du bloc que l'occurrence traverse, pour le surlignage PDF ; vides quand le document n'a
    pas de lignes (le guide, ingéré depuis `kb.js`).
    """

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    line_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _span_is_not_empty(self) -> VerifiedQuote:
        if self.end <= self.start:
            raise ValueError("end doit être > start (une occurrence retrouvée n'est jamais vide)")
        return self


class VerifiedClaim(Claim):
    """Claim après *vérifier* : statut calculé par le code, offsets pour le surlignage."""

    quotes: list[VerifiedQuote] = Field(min_length=1)
    status: ClaimStatus
    line_ids: list[str] = Field(default_factory=list)  # union des `line_ids` des quotes, dans l'ordre


class RejectedClaim(VerifiedClaim):
    rejection_kind: RejectionKind
    motif: str = ""

    # Une claim rejetée sur ses citations n'a pas d'occurrence à conserver : ses quotes restent celles
    # du draft, sans offsets. Une claim rejetée sur la **pertinence**, elle, a bien été retrouvée : ses
    # offsets et `line_ids` sont conservés (AD-3 : les claims rejetées sont « conservées […] affichables
    # par le front » — sans offsets, le front n'a rien à surligner). D'où l'union, `VerifiedQuote`
    # d'abord : pydantic essaie le membre le plus riche avant de retomber sur la quote nue.
    quotes: list[VerifiedQuote | Quote] = Field(min_length=1)


class Verification(DomainModel):
    """Sortie de *vérifier* (AD-3/AD-4) : ce qui survit, ce qui est rejeté, et pourquoi.

    Elle porte aussi les `segments` de l'ébauche : *restituer* rend `Answer.texte` **déterministement**
    depuis les segments survivants (AD-3), et il ne voit rien d'autre que cette sortie — le pipeline
    ne lui repasse pas le draft, sans quoi deux étapes auraient à décider qui a survécu.
    `motif` est composé par notre code (jamais par le modèle) et n'est renseigné que s'il y a matière
    à relancer *rédiger*.
    """

    segments: list[AnswerSegment] = Field(default_factory=list)
    claims: list[VerifiedClaim] = Field(default_factory=list)
    rejected_claims: list[RejectedClaim] = Field(default_factory=list)
    found: bool = False
    complete: bool = False
    unknown: list[str] = Field(default_factory=list)
    motif: str | None = None


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
