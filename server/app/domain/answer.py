"""AD-3 / AD-4 — Claim, statuts, ébauche et réponse unique."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, get_args

from pydantic import ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .document import DomainModel
from .langue import LANGUES_SERVIES

# `Applicable` vit dans `verdict.py` (story 1.8) : c'est AD-6 qui en fixe les trois valeurs et le
# code qui les dérive. Deux littéraux identiques dans deux modules auraient pu diverger en silence.
from .question import CLARIFICATION_MAX_CHARS, QuestionScope
from .verdict import Applicable, ApplicableReason, ClaimJugee, Verdict

SegmentKind = Literal["factuel", "transition", "limite"]
# `non_citee` amende AD-4 (revue Codex 1.5) : une claim retrouvée et pertinente qu'**aucun** segment
# factuel affiché ne cite n'a pas d'endroit où paraître. La retenir dans `claims[]` autoriserait
# `found=True` sur un `Answer.texte` vide — « réponse vide présentée comme réponse », ce qu'AD-16
# nomme précisément comme ce qu'il empêche. Aucun des trois kinds d'origine ne la décrit : elle n'est
# ni introuvable, ni ambiguë, ni jugée non pertinente.
RejectionKind = Literal["non_retrouvee", "non_pertinente", "ambigue", "non_citee"]
# Le vocabulaire **fermé** des raisons de non-pertinence. Il vivait uniquement dans le schéma de
# sortie de *vérifier* et n'atteignait le reste de la chaîne que fondu dans une phrase de motif.
# Or ces trois raisons n'ont pas la même nature (correctif du tour 2, rapport rédiger F) : une
# citation `non_soutenue` et une `conclusion_ajoutee` sont des défauts de **rédaction**, qu'une
# reformulation corrige ; un `hors_objet` est un jugement de **périmètre**, stable, que relancer ne
# déplace pas. Décider entre les deux en relisant une phrase française aurait été une heuristique
# de texte ; la raison devient donc un fait typé, porté par la claim rejetée.
RaisonNonPertinence = Literal["non_soutenue", "hors_objet", "conclusion_ajoutee"]
RAISONS_CORRIGEABLES: frozenset[str] = frozenset({"non_soutenue", "conclusion_ajoutee"})
# `clarification_requise` amende AD-4 (story 1.5) : `Answer` exige un `reason` dès que `found=False`, et
# aucun des trois kinds d'origine ne décrit « la question n'a pas pu être rendue autonome » (AD-5, deux
# sorties exclusives de *comprendre*). Répondre `hors_perimetre` à une anaphore irrésoluble serait un
# mensonge : la question relève peut-être du périmètre, on ne le sait pas encore.
AbsenceKind = Literal["hors_perimetre", "zero_hit", "claims_rejetes", "clarification_requise"]
LacuneKind = Literal[
    "lecture_bornee",
    "sans_decoupage",
    # Correctif du 2026-09-02 : *retrouver* n'a rapporté **aucune** clause décisionnelle confirmée
    # pour une facette de la question, et il le déclare (`RetrievalResult.facettes`). La cause est
    # distincte de `facettes_sans_reponse`, qui dit qu'une sous-question dont les clauses **ont**
    # été lues n'a pas reçu d'affirmation affichée : ici, il n'y avait rien à rendre. Les deux
    # comptes sont exclusifs — une facette n'entre que dans l'un des deux — pour qu'un même manque
    # ne soit pas annoncé deux fois à qui lit la réponse.
    "facettes_sans_clause",
    "facettes_sans_reponse",
    "renvoi_non_resolu",
    "contradiction_non_resolue",
    "phrases_ecartees",
    "segments_retires",
    "relance_abandonnee",
    # Story 4.2e : le contrôle a **demandé** le contexte qui lui manquait et ne l'a pas relu — demande
    # mal formée, non satisfaite, sans place sous le budget, ou renouvelée une seconde fois. La cause
    # est distincte de `renvoi_non_resolu` (un renvoi que le corpus n'a pas résolu, constaté sur le
    # bloc) et de `lecture_bornee` (une lecture coupée par le budget, avant tout jugement) : ici, la
    # lecture a eu lieu, le jugement a nommé ce qui lui manquait, et ce manque est resté ouvert.
    "contexte_non_relu",
]
# Ces deux causes sont les seules dont la phrase porte un cardinal. Cette donnée appartient au
# domaine : tous les producteurs et toutes les projections doivent partager la même définition.
# `contexte_non_relu` n'en est pas : une demande de contexte est unique par vérification (bornage
# strict de la story 4.2e), donc son cardinal est toujours `n == 0`.
LACUNES_PLURALISEES: frozenset[LacuneKind] = frozenset({
    "facettes_sans_clause",
    "facettes_sans_reponse",
    "phrases_ecartees",
})

# Story 4.2e — le vocabulaire **fermé** d'une demande de contexte. Il vit ici, avec le type qui le
# porte, et non dans l'étape : `steps/verifier.py` construit le schéma envoyé au modèle à partir de
# ces mêmes littéraux (comme `Applicable` ci-dessus, deux copies auraient divergé au premier
# amendement, et le schéma du fournisseur aurait alors accepté ce que le domaine refuse).
#
# `definition` : le contrôle ne sait pas ce qu'un terme du texte transmis désigne dans ce contrat.
# `renvoi`     : un bloc fourni renvoie ailleurs, et la cible du renvoi n'a pas été lue.
# `qualite`    : une qualité que le modèle a lui-même énumérée ne peut pas être confrontée aux faits.
DemandeKind = Literal["definition", "renvoi", "qualite"]
DemandeRaison = Literal["definition_manquante", "renvoi_non_lu", "qualite_non_verifiable"]
DEMANDE_KINDS: tuple[str, ...] = get_args(DemandeKind)
DEMANDE_RAISONS: tuple[str, ...] = get_args(DemandeRaison)


class Quote(DomainModel):
    block_id: str
    quote: str


def passage_canonique(texte: str) -> str:
    """Forme canonique d'un passage cité : ce qui reste quand on retire ce qui n'est pas du contenu.

    Blancs réduits à une espace simple (même règle que `AnswerDraft.digest`), apostrophe
    typographique ramenée à l'apostrophe simple, casse repliée. Ce n'est **pas** la normalisation du
    corpus (`corpus.text.normalize`, hors de portée du domaine) et elle n'a pas à l'être : elle ne
    sert jamais à retrouver une occurrence dans un bloc — seulement à décider si deux citations
    désignent le même passage.
    """
    return " ".join(texte.replace("\u2019", "'").split()).casefold()


class Claim(DomainModel):
    claim_id: str
    text: str
    quotes: list[Quote] = Field(min_length=1)  # une quote par bloc

    @property
    def preuve(self) -> frozenset[tuple[str, str]]:
        """Ce que cette affirmation **prouve** : ses passages, jamais sa formulation.

        Correctif du tour 2 (rapport citations, A1). La fusion de relance ne dédupliquait que sur
        l'égalité byte-exacte du couple `(text, quotes)`. Or le prompt de relance demande de
        **reconduire** les acquis, et une reconduction est naturellement une paraphrase : « le
        contrat couvre » devient « le contrat garantit ». La comparaison échouait, la branche
        « correction » se déclenchait, et l'acquis **et** sa paraphrase survivaient tous les deux —
        sur le même bloc, le même passage. La réponse servie disait alors deux fois la même chose,
        `sources[]` publiait deux cartes identiques, et la duplication **gagnait** la dominance,
        puisque seul le compte de claims y est gonflable.

        Deux affirmations qui s'appuient exactement sur les mêmes passages avancent la même preuve.
        Ce sont donc les `(block_id, passage canonique)` qui les identifient.
        """
        return frozenset((quote.block_id, passage_canonique(quote.quote))
                         for quote in self.quotes)

    @field_validator("quotes")
    @classmethod
    def _pas_deux_fois_la_meme_citation(cls, quotes: list[Quote]) -> list[Quote]:
        """Deux citations **identiques** dans une claim n'ajoutent rien ; deux extraits d'un même
        bloc, si — et ce n'est plus un échec de schéma (correctif du tour 2, rapport rédiger §1).

        La règle « au plus une quote par bloc » était portée **ici**, donc terminale : le parse
        échouait, le retry unique d'AD-16 était consommé, le modèle rejouait la même faute, et
        l'utilisateur recevait un 503 sur une question nominale. Les deux issues que le prompt lui
        proposait étaient d'ailleurs fermées toutes les deux : le passage englobant dépassait la
        borne annoncée, et la place de claims était déjà prise.

        Ce que la règle protégeait — deux extraits disjoints d'un même bloc se faisant passer pour
        deux preuves — est désormais obtenu **par le code**, en fusionnant les deux extraits en un
        seul passage contigu qui les couvre (`steps.rediger`). La preuve reste une, le contenu n'est
        pas perdu, et rien n'est refusé au modèle qu'il puisse corriger.

        AD-10/AD-15 (revue Codex 1.4, B7) : le message d'un validateur du domaine est recopié tel
        quel dans `StepTrace.checks` et dans la relance ; il ne cite donc **jamais** une valeur reçue.
        """
        vues = {(q.block_id, q.quote) for q in quotes}
        if len(vues) != len(quotes):
            raise ValueError("deux citations identiques dans une même affirmation : cite un passage "
                             "une seule fois")
        return quotes


class ClaimStatus(DomainModel):
    retrouvee: bool
    pertinente: bool | None = None
    applicable: Applicable | None = None
    applicable_reason: ApplicableReason | None = None
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
    # La raison fermée du rejet de pertinence, quand le contrôle en a rendu une. `None` sur les
    # autres kinds de rejet, et sur une pertinence rejetée sans raison valide — absence de mesure,
    # jamais une raison par défaut (AD-16).
    rejection_reason: RaisonNonPertinence | None = None
    motif: str = ""

    # Une claim rejetée sur ses citations n'a pas d'occurrence à conserver : ses quotes restent celles
    # du draft, sans offsets. Une claim rejetée sur la **pertinence**, elle, a bien été retrouvée : ses
    # offsets et `line_ids` sont conservés (AD-3 : les claims rejetées sont « conservées […] affichables
    # par le front » — sans offsets, le front n'a rien à surligner). D'où l'union, `VerifiedQuote`
    # d'abord : pydantic essaie le membre le plus riche avant de retomber sur la quote nue.
    quotes: list[VerifiedQuote | Quote] = Field(min_length=1)


class Lacune(DomainModel):
    """Cause typée d'une réponse incomplète ; sa phrase est rendue par *restituer*."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: LacuneKind
    n: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _cardinal_coherent(self) -> Lacune:
        if self.kind in LACUNES_PLURALISEES:
            if self.n < 1:
                raise ValueError("une lacune pluralisée exige n >= 1")
        elif self.n != 0:
            raise ValueError("une lacune non pluralisée exige n == 0")
        return self


class DemandeContexte(DomainModel):
    """Story 4.2e — ce qui manquait au contrôle pour juger une affirmation, dit en vocabulaire fermé.

    Un tour de vérification peut buter sur une définition qu'il n'a pas lue, sur un renvoi dont il n'a
    pas la cible, ou sur une qualité qu'il ne peut pas confronter aux faits. Jusqu'ici son contrat ne
    savait pas exprimer ce manque : le jugement tranchait alors sur un contexte qu'il n'avait pas
    relu. Le manque devient donc un **fait typé du domaine**, et ses invariants se posent une fois :

    - `kind` et `raison` sont deux vocabulaires **fermés** ; aucun texte libre n'entre ici ;
    - `cible` et `claim_id` sont non vides, et l'objet est composé **par le code** de *vérifier*
      après avoir vérifié que la cible désigne quelque chose qui était déjà dans l'entrée du modèle
      (un bloc fourni, un terme du texte transmis, une qualité que le modèle a lui-même énumérée) ;
    - `frozen=True`, comme `Lacune` : une demande ne se corrige pas en route, elle est satisfaite ou
      elle échoue fermée.

    Ce type ne dit **pas** ce qu'il faut faire du manque : le pipeline en décide (AD-1 — *vérifier*
    ne touche aucun outil), au plus une satisfaction et au plus une reprise.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DemandeKind
    # Ce que la demande vise, dans l'univers de l'entrée : un `block_id` fourni (`renvoi`), un terme
    # présent dans le texte transmis (`definition`), une qualité énumérée par le modèle (`qualite`).
    cible: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    raison: DemandeRaison


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
    # Ce que **le code** constate qu'il manque. Une lacune est un fait typé ; sa phrase dépend de la
    # langue et n'est projetée que dans *restituer*, seul endroit où un `Answer` se fabrique.
    lacunes: list[Lacune] = Field(default_factory=list)
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
    # Story 4.2e : ce que le contrôle a **demandé** et n'a pas eu. Composé par le code de *vérifier*
    # à partir d'un champ typé de la sortie sinistre, une fois sa cible retrouvée dans l'entrée
    # réellement envoyée au modèle — jamais recopié tel quel. `None` en guide (le schéma du guide ne
    # porte pas ce champ) et sur toute sortie invalide : une demande mal formée n'est pas une demande.
    #
    # Le champ est **lu par le pipeline**, seul propriétaire de la chaîne, jamais par *vérifier* :
    # AD-1 fait de *retrouver* le seul détenteur des outils, et une étape qui satisferait elle-même
    # sa demande rouvrirait le corpus depuis le contrôle des citations.
    demande_contexte: DemandeContexte | None = None
    # Story 3.7 : état exact qui a alimenté AD-6. Il voyage seulement entre *vérifier*, le pipeline
    # et la fabrique du jeton signé ; il n'est jamais sérialisé dans l'ancien contrat ``Answer``.
    _decision_claims: list[ClaimJugee] = PrivateAttr(default_factory=list)
    motif: str | None = None

    @property
    def nb_manques(self) -> int:
        """Cardinal comparé par la relance, indépendamment de la langue de projection."""
        return len(self.unknown) + len(self.lacunes)


class AbsenceProof(DomainModel):
    kind: AbsenceKind
    terms_searched: list[str] = Field(default_factory=list)  # termes canoniques seulement
    variants_count: int = 0
    blocks_scanned: int = 0
    documents: list[str] = Field(default_factory=list)


class LecturePartielle(DomainModel):
    """Story 4.2f — le troisième porteur d'état d'une réponse : ce qui a été **lu**, pas ce qui manque.

    Une lecture bornée dont aucune affirmation n'a survécu à la vérification n'est ni une panne ni une
    absence. Ce n'est pas une panne : la chaîne est allée jusqu'au bout, des blocs sont partis au
    modèle, la vérification a tourné. Ce n'est pas une absence : `AbsenceProof` publie
    `blocks_scanned = len(document.blocks)` et un compte de variantes, c'est-à-dire l'annonce d'un
    balayage **exhaustif** que la troncature dément — l'opposer à l'utilisateur reviendrait à lui
    affirmer que le corpus ne dit rien de ce que nous n'avons pas lu.

    D'où un type à part, qui ne dit **que** ce qui a été lu :

    - `nodes_read` — les nœuds distincts d'où viennent les blocs transmis, **au moins un** (jamais le
      nombre d'ouvertures tentées : une ouverture refusée par le quota n'a rien fait lire) ;
    - `blocks_read` — les blocs effectivement transmis au modèle, **au moins un** ;
    - `documents` — le ou les documents lus, comme `AbsenceProof.documents`.

    Les deux planchers ne sont pas des précautions de forme, et ils se lisent ensemble.

    `blocks_read ≥ 1` : zéro bloc transmis est exactement le cas qu'AD-1/NFR2 gardent en échec
    terminal (`BudgetExceeded`, « le budget de retrieval n'a laissé passer aucun bloc »), avant même
    *rédiger*. Une lecture partielle qui annoncerait n'avoir rien lu n'aurait rien à chiffrer et se
    confondrait avec cette panne-là.

    `nodes_read ≥ 1` en découle : le compteur est défini comme les nœuds **des blocs transmis**,
    résolus par `Document.node_of`, et AD-2 rend cette résolution **totale** — « chaque bloc rattaché
    à exactement un nœud », vérifié par les invariants d'arbre au chargement. `blocks_read ≥ 1 ⟹
    nodes_read ≥ 1` est donc un théorème des deux producteurs, que le type laissait contourner :
    publier zéro section pour au moins un passage n'est pas un cas rare, c'est un état impossible, et
    l'écran y lirait deux chiffres qui se contredisent.

    Ce n'est délibérément **pas** un cinquième `AbsenceKind` : `AbsenceKind` reste fermé à ses quatre
    valeurs, et réutiliser `AbsenceProof` en y ajoutant un kind rouvrirait le défaut par la porte du
    vocabulaire — l'objet continuerait de porter les champs d'un balayage exhaustif.
    """

    nodes_read: int = Field(ge=1)
    blocks_read: int = Field(ge=1)
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
    # Story 4.2f : l'autre porteur possible d'un `found=False`, à côté de `reason` et jamais avec lui.
    # `reason` dit ce qui a été cherché en vain ; celui-ci dit ce qui a été lu sans conclure. Les deux
    # ensemble seraient deux comptes rendus contradictoires du même refus ; aucun des deux serait le
    # « dégradé silencieux » qu'AD-16 interdit.
    lecture_partielle: LecturePartielle | None = None
    verdict: Verdict | None = None
    # État interne, exclu de toute projection HTTP. La route 3.7 le place dans l'état de continuation
    # avant de signer celui-ci ; le navigateur ne peut donc ni le fabriquer ni le modifier.
    _decision_claims: list[ClaimJugee] = PrivateAttr(default_factory=list)
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

    @field_validator("lang")
    @classmethod
    def _langue_servie(cls, lang: str) -> str:
        if lang not in LANGUES_SERVIES:
            raise ValueError("lang doit désigner une langue servie")
        return lang

    @model_validator(mode="after")
    def _found_coherence(self) -> Answer:
        if self.lang_fallback and self.lang != "fr":
            raise ValueError("lang_fallback=True exige lang='fr'")
        # Story 4.2f : **exactement un** porteur sur `found=False`. Le domaine exigeait une preuve
        # d'absence, et c'est cette ligne qui condamnait une lecture bornée sans claim survivante à
        # un 503 : le seul objet disponible affirmait un balayage exhaustif que la troncature dément.
        # La règle n'est pas assouplie, elle est **typée** : un refus dit ce qu'il a cherché en vain
        # (`reason`), ou ce qu'il a lu sans conclure (`lecture_partielle`) — jamais les deux (deux
        # comptes rendus du même fait, dont l'un ment), jamais aucun (le dégradé muet d'AD-16).
        porteurs = [p for p in (self.reason, self.lecture_partielle) if p is not None]
        if not self.found and len(porteurs) != 1:
            raise ValueError("found=False exige exactement un porteur : une preuve d'absence (reason) "
                             "ou une lecture partielle (lecture_partielle), jamais les deux, jamais aucun")
        if self.found and porteurs:
            # L'autre moitié de la règle, et elle porte sur **les deux** : « `found=True` n'en porte
            # aucun ». Une réponse retenue n'est ni une absence prouvée ni une lecture restée sans
            # conclusion — si la lecture a été bornée et qu'une affirmation a survécu, la borne se
            # dit dans `unknown[]` par la lacune `lecture_bornee`, que `complete=False` publie.
            #
            # Ne fermer que `lecture_partielle` laissait passer un `found=True` muni d'une
            # `AbsenceProof` : *restituer* le refusait côté producteur, mais c'est **ce validateur**
            # que les deux lecteurs stricts rejouent, et une page pouvait donc peindre en même temps
            # une réponse « sûre » et la preuve chiffrée d'une absence.
            raise ValueError("found=True n'admet aucun porteur d'absence : ni preuve d'absence "
                             "(reason), ni lecture partielle (lecture_partielle) — une borne se dit "
                             "dans unknown[] avec complete=False")
        if self.lecture_partielle is not None and not self.unknown:
            # Symétrique de l'invariant « partiel dit ce qui lui manque » : une réponse qui chiffre
            # ce qu'elle a lu doit dire pourquoi cela n'a pas suffi — au minimum la lacune
            # `lecture_bornee` que *vérifier* dépose.
            raise ValueError("une lecture partielle dit ce qui lui manque : unknown[] ne peut pas "
                             "être vide")
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
