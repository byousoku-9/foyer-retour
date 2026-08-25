"""AD-3 / AD-4 — Claim, statuts, ébauche et réponse unique."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .document import DomainModel

# `Applicable` vit dans `verdict.py` (story 1.8) : c'est AD-6 qui en fixe les trois valeurs et le
# code qui les dérive. Deux littéraux identiques dans deux modules auraient pu diverger en silence.
from .question import CLARIFICATION_MAX_CHARS, QuestionScope
from .verdict import Applicable, Verdict

SegmentKind = Literal["factuel", "transition", "limite"]
# `non_citee` amende AD-4 (revue Codex 1.5) : une claim retrouvée et pertinente qu'**aucun** segment
# factuel affiché ne cite n'a pas d'endroit où paraître. La retenir dans `claims[]` autoriserait
# `found=True` sur un `Answer.texte` vide — « réponse vide présentée comme réponse », ce qu'AD-16
# nomme précisément comme ce qu'il empêche. Aucun des trois kinds d'origine ne la décrit : elle n'est
# ni introuvable, ni ambiguë, ni jugée non pertinente.
RejectionKind = Literal["non_retrouvee", "non_pertinente", "ambigue", "non_citee"]
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

    Deux systèmes d'offsets, parce qu'il y a deux textes :

    - `start`/`end` indexent `Block.text_norm` — la seule forme sur laquelle l'inclusion a été prouvée
      (convention Texte : `normalize()` est l'unique normalisation admise) ;
    - `text_start`/`text_end` indexent `Block.text`, le texte **brut** du corpus, obtenus en
      retraduisant l'occurrence par `normalize_spans()`. Ce sont eux que le front surligne, et c'est
      d'eux que `quote` est extraite.

    `quote` n'est donc **jamais** la chaîne rendue par le modèle (revue Codex 1.5, B2) : AD-3 veut
    que « le texte affiché comme source soit toujours relu depuis `corpus` », et une citation
    normalisée — même casse, mêmes accents, mêmes séparateurs — peut différer du texte source sans
    que rien ne le signale. `line_ids` sont les lignes du bloc que l'occurrence traverse, pour le
    surlignage PDF ; vides quand le document n'a pas de lignes (le guide, ingéré depuis `kb.js`).
    """

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text_start: int = Field(ge=0)
    text_end: int = Field(ge=0)
    line_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _span_is_not_empty(self) -> VerifiedQuote:
        if self.end <= self.start:
            raise ValueError("end doit être > start (une occurrence retrouvée n'est jamais vide)")
        if self.text_end <= self.text_start:
            raise ValueError("text_end doit être > text_start (une occurrence retrouvée n'est jamais vide)")
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
    # Ce que **le modèle** a déclaré hors de sa portée : le texte des segments `limite` de l'ébauche,
    # rédigé dans la langue de la réponse.
    unknown: list[str] = Field(default_factory=list)
    # Ce que **le code** constate qu'il manque (story 2.3, revue coordonnée A3) : lecture bornée,
    # découpage non établi, sous-questions sans réponse, renvoi non résolu, phrases écartées, relance
    # empêchée. Les deux listes sont affichées ensemble — l'utilisateur lit une seule section « Ce
    # que je ne sais pas » —, mais elles ne sont pas de même nature et ne peuvent pas partager un
    # champ : les phrases du code sont **en français** quelle que soit `ParsedQuestion.language`
    # (leur traduction est l'AC de la story 2.4), exactement comme `restituer.PHRASES_DE_REFUS`, et
    # c'est ce qui doit lever `Answer.lang_fallback`. Fondues dans `unknown`, elles faisaient passer
    # une réponse partiellement française pour une réponse rédigée dans la langue demandée.
    lacunes: list[str] = Field(default_factory=list)
    # **Quelles** facettes de la question une affirmation affichée couvre — leurs rangs dans
    # `ParsedQuestion.facettes`, triés (AD-4, revue Codex 1.5, tours 2 et 3, I2). `complete` n'en est
    # que le cas « toutes » ; la relance, elle, a besoin de l'ensemble et pas de son cardinal : deux
    # vérifications qui couvrent chacune **une** facette différente ont le même compte et ne se
    # valent pas — accepter la seconde échangerait une sous-question contre une autre. Les rangs sont
    # stables entre deux ébauches de la même question, puisque le découpage est arrêté une fois par
    # *comprendre*. C'est une donnée du **code**, jamais un jugement transmis à l'appelant.
    facettes_couvertes: list[int] = Field(default_factory=list)
    # AD-4/AD-6 (story 1.8) : le verdict du sinistre est **calculé ici**, par la table d'AD-6 appliquée
    # aux claims affichées, et *restituer* ne fait que le recopier dans l'unique `Answer`. `None` en
    # guide — AD-4 : « `Verdict` voyage dans l'unique `Answer` », il n'y a pas de second objet de
    # réponse, et une question du guide n'a pas de verdict à porter.
    verdict: Verdict | None = None
    motif: str | None = None

    @property
    def manques(self) -> list[str]:
        """Tout ce qui manque, dans l'ordre d'affichage : ce que le modèle a déclaré, puis ce que le
        code a constaté, sans doublon. C'est cette liste — et elle seule — que `complete` nie, et
        c'est elle que *restituer* recopie dans `Answer.unknown`."""
        return self.unknown + [t for t in self.lacunes if t not in self.unknown]


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
    # Ce que *comprendre* a **compris** des faits déclarés (story 1.9, D4) : `ParsedQuestion.scope`,
    # c'est-à-dire le bien, l'événement, le lieu, la cause et le moment que FR15 fait extraire. L'AC
    # de la story exige de les afficher, et aucun canal ne les publiait — ils étaient écrits par
    # *comprendre* et relus par personne (reprise différée de 1.8). Ils voyagent donc dans l'unique
    # `Answer` d'AD-4, posés par *restituer* comme le verdict, et bornés **avant** par le pipeline :
    # ce sont des libellés du modèle qui atteignent un écran. `None` en guide — le profil du guide
    # n'est pas « ce qui a été compris d'un sinistre », et rien ne l'affiche.
    faits_compris: QuestionScope | None = None
    unknown: list[str] = Field(default_factory=list)
    # Même borne que `ClarificationRequise.clarification`, et pour la même raison (revue Codex
    # 2.2, B1) : c'est ce champ que la page conserve dans son historique, et un tour hors borne
    # y est écarté — la question posée disparaîtrait, et l'assistant la reposerait indéfiniment.
    # La ceinture vaut pour les deux pipelines : tous deux recopient ici la clarification de
    # *comprendre*, mais rien d'autre que ce champ ne le garantirait à un producteur futur.
    clarification: str | None = Field(None, max_length=CLARIFICATION_MAX_CHARS)

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
        if self.found and not self.complete and not self.unknown:
            # Story 2.3, l'autre moitié de l'équivalence. AD-4 faisait déjà de `unknown=[]` une
            # condition de `complete` ; l'AC exige « partiel **avec `unknown[]` listé** », et *vérifier*
            # nomme désormais **chaque** cause d'incomplétude. `complete ⟺ found ∧ unknown = []`
            # devient donc un invariant du domaine, et non plus une propriété qu'un seul producteur
            # respecterait : sans lui, un appelant peut encore fabriquer un « PARTIEL » que
            # l'utilisateur lit sans savoir ce qui manque — le défaut même que la story corrige.
            raise ValueError("found=True et complete=False exigent au moins une lacune dans unknown[] "
                             "(une réponse partielle dit ce qui lui manque)")
        return self
