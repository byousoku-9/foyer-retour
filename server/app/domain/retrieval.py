"""AD-1 — Budget et résultat de l'étape *retrouver* (commun à toutes les variantes)."""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .document import Block, DomainModel


def stable_uid(prefix: str, payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


class QuestionClauseScore(DomainModel):
    """Score exact calculé une fois par l'index et transporté sans recomputation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_uid: str
    clause_uid: str
    scorer_uid: str
    scorer_version: str
    # Tuple lexical explicite : nombre de groupes pleins, rappel partiel et précision. Les fractions
    # sont sérialisées par numérateur/dénominateur pour ne jamais dépendre d'un arrondi flottant.
    full_matches: int = Field(ge=0)
    partial_numerator: int = Field(ge=0)
    partial_denominator: int = Field(ge=1)
    precision_numerator: int = Field(ge=0)
    precision_denominator: int = Field(ge=1)
    # Couverture de la question résolue entière, distincte du score de rappel. Elle permet à
    # l'admission et à la suffisance de ne jamais prendre un terme de recherche pour la question.
    question_numerator: int = Field(default=0, ge=0)
    question_denominator: int = Field(default=1, ge=1)
    kind_priority: int = Field(ge=0)

    @model_validator(mode="after")
    def _fractions_dans_le_domaine(self) -> QuestionClauseScore:
        if self.partial_numerator > self.partial_denominator:
            raise ValueError("score partiel hors domaine [0,1]")
        if self.precision_numerator > self.precision_denominator:
            raise ValueError("précision hors domaine [0,1]")
        if self.question_numerator > self.question_denominator:
            raise ValueError("couverture question hors domaine [0,1]")
        return self

    @property
    def sort_key(self) -> tuple[int, Fraction, Fraction, Fraction, int]:
        partial = Fraction(self.partial_numerator, self.partial_denominator)
        precision = Fraction(self.precision_numerator, self.precision_denominator)
        question = Fraction(self.question_numerator, self.question_denominator)
        return (-self.full_matches, -partial, -precision, -question, self.kind_priority)


class ScoredHit(DomainModel):
    """Record canonique de ``chercher`` ; titre, extrait et score survivent ensemble."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_uid: str
    document_uid: str
    clause_uid: str
    node_uid: str
    title: str
    excerpt: str
    score: QuestionClauseScore

    @model_validator(mode="after")
    def _identites_coherentes(self) -> ScoredHit:
        if self.score.clause_uid != self.clause_uid:
            raise ValueError("score et hit ne désignent pas la même clause")
        expected = stable_uid("result-v1", {
            "question_uid": self.score.question_uid,
            "clause_uid": self.clause_uid,
            "scorer_uid": self.score.scorer_uid,
            "scorer_version": self.score.scorer_version,
            "score": self.score.model_dump(mode="json"),
            "document_uid": self.document_uid,
            "node_uid": self.node_uid,
            "title": self.title,
            "excerpt": self.excerpt,
        })
        if self.result_uid != expected:
            raise ValueError("result_uid ne correspond pas au contenu canonique du hit")
        return self

    def __iter__(self):
        """Dépaquetage transitoire ``block_id, node_id`` sans égalité avec un tuple."""
        yield self.clause_uid
        yield self.node_uid

    def __getitem__(self, index: int) -> str:
        if index == 0:
            return self.clause_uid
        if index == 1:
            return self.node_uid
        raise IndexError(index)


class ContextUnit(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block_uid: str
    role: Literal[
        "target", "parent_preamble", "same_clause_continuation", "explicit_dependency",
        "definition_override",
    ]
    document_uid: str
    section_uid: str
    order: int = Field(ge=0)


class BudgetSnapshot(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    opens_used: int = Field(ge=0)
    blocks_used: int = Field(ge=0)
    tokens_used: int = Field(ge=0)
    opens_remaining: int = Field(ge=0)
    blocks_remaining: int | None = Field(default=None, ge=0)
    tokens_remaining: int | None = Field(default=None, ge=0)


class AdmissionDecision(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_uid: str
    state: Literal["admitted", "rejected"]
    reason: str
    snapshot: BudgetSnapshot


class SufficiencyDecision(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: bool
    reason: str
    sufficiency_result_uid: str | None = None
    considered_result_uids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _appartenance(self) -> SufficiencyDecision:
        if len(self.considered_result_uids) != len(set(self.considered_result_uids)):
            raise ValueError("considered_result_uids contient un doublon")
        if self.complete and self.sufficiency_result_uid is None:
            raise ValueError("une suffisance vraie exige sufficiency_result_uid")
        if not self.complete and self.sufficiency_result_uid is not None:
            raise ValueError("une suffisance fausse interdit sufficiency_result_uid")
        if (self.sufficiency_result_uid is not None
                and self.sufficiency_result_uid not in self.considered_result_uids):
            raise ValueError("sufficiency_result_uid n'appartient pas aux résultats considérés")
        return self


class SemanticSufficiencySelection(DomainModel):
    """Décision terminale du navigateur : une identité canonique ou une insuffisance explicite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sufficient: bool
    result_uid: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _identite_iff_suffisant(self) -> SemanticSufficiencySelection:
        if self.sufficient != (self.result_uid is not None):
            raise ValueError("result_uid est requis si et seulement si sufficient=true")
        return self


class RetrievalBudget(DomainModel):
    """Borne toute l'étape : appels modèle, nœuds, blocs, tokens, définitions et renvois inclus."""

    max_opens: int
    node_window: int
    search_limit: int
    # Ce plafond de sûreté appartient au budget lui-même : les tests, évals et appels directs ne
    # passent pas nécessairement par `Settings`, et la livraison 2.6 interdit un troisième tour.
    # La valeur opérationnelle reste une hypothèse de configuration portée aussi par `Settings` ;
    # la story 4.1 pourra rediscuter ce plafond et le contrat de domaine ensemble si sa mesure le demande.
    max_llm_turns: int = Field(ge=1, le=2)
    max_blocks: int | None = None
    max_tokens: int | None = None
    # Story 2.3 : les places réservées, **parmi** `max_opens`, aux nœuds que le profil désigne. Elle
    # vit ici et non dans `Settings` (revue coordonnée 2.3, A4) : c'est `max_opens` qu'elle borne, et
    # un appelant qui construit un budget réduit — évals, tests, mode économique — abaissait le quota
    # sans abaisser la réserve, si bien que la réserve devenait le quota entier. Les deux nombres se
    # lisent maintenant au même endroit, et le validateur ci-dessous interdit qu'ils se contredisent.
    profil_max_opens: int = Field(0, ge=0)

    @model_validator(mode="after")
    def _la_reserve_ne_mange_pas_le_quota(self) -> RetrievalBudget:
        if self.profil_max_opens >= self.max_opens:
            # Une réserve égale au quota évincerait **tout** ce que la question a classé : le profil
            # ne serait plus un ordre mais un filtre, ce que la story interdit explicitement.
            raise ValueError(
                f"profil_max_opens ({self.profil_max_opens}) doit rester strictement inférieur à "
                f"max_opens ({self.max_opens}) : le profil ordonne, il ne remplace pas la question")
        return self


class RetrievalResult(DomainModel):
    blocs: list[Block] = Field(default_factory=list)
    opened_block_ids: list[str] = Field(default_factory=list)
    # Story 4.2f : les nœuds distincts d'où viennent les **blocs transmis**, dans leur ordre
    # d'apparition. Aucune variante ne publiait ce nombre, si bien qu'une réponse ne pouvait pas
    # dire combien elle avait lu.
    #
    # Deux bornes, et elles se lisent ensemble. « Transmis » et non « ouvert » : une ouverture dont
    # le budget de blocs a écarté toute la fenêtre n'a rien fait lire, et la compter gonflerait le
    # chiffre rendu à l'utilisateur. Mais **tous** les blocs transmis comptent, pas seulement ceux
    # d'une fenêtre : un bloc entré par `definitions` ou comme dépendance directe a bien été lu, et
    # AD-2 lui donne exactement un nœud propriétaire. Ne compter que les fenêtres laissait la
    # variante `outils` — celle qui est servie — annoncer « 0 section lue, N passages transmis »,
    # deux chiffres qui se contredisent sous les yeux de l'utilisateur.
    opened_node_ids: list[str] = Field(default_factory=list)
    # Dépendances directes effectivement admises avec une garantie/exclusion primaire. *rédiger*
    # peut ainsi rendre visibles les résolutions utiles sans exiger une claim pour tout le contexte.
    decision_dependency_block_ids: list[str] = Field(default_factory=list)
    discarded_block_ids: list[str] = Field(default_factory=list)
    scored_hits: list[ScoredHit] = Field(default_factory=list)
    admission_decisions: list[AdmissionDecision] = Field(default_factory=list)
    sufficiency: SufficiencyDecision | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def _decisions_uniques(self) -> RetrievalResult:
        hit_ids = [hit.result_uid for hit in self.scored_hits]
        if len(hit_ids) != len(set(hit_ids)):
            raise ValueError("scored_hits contient un result_uid dupliqué")
        decision_ids = [decision.result_uid for decision in self.admission_decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("un résultat possède plusieurs décisions terminales")
        if set(decision_ids) != set(hit_ids):
            raise ValueError("chaque hit exige exactement une décision terminale")
        if self.sufficiency is not None:
            if set(self.sufficiency.considered_result_uids) - set(hit_ids):
                raise ValueError("la suffisance considère un hit absent")
            sufficient = self.sufficiency.sufficiency_result_uid
            if sufficient is not None:
                decisions = {decision.result_uid: decision for decision in self.admission_decisions}
                if sufficient not in self.sufficiency.considered_result_uids:
                    raise ValueError("le résultat suffisant doit être considéré")
                if decisions[sufficient].state != "admitted":
                    raise ValueError("le résultat suffisant doit être admis")
        return self


class FullContextSelection(DomainModel):
    """Sortie minimale du modèle : des identifiants, jamais des blocs inventés."""

    block_ids: list[str] = Field(default_factory=list)

    @field_validator("block_ids")
    @classmethod
    def _uniques(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("block_ids exige des identifiants non blancs et trimés")
        if len(values) != len(set(values)):
            raise ValueError("block_ids ne peut pas contenir de doublon")
        return values


class NodeChild(DomainModel):
    """Un enfant directement navigable d'un nœud, sans contenu documentaire."""

    node_id: str
    title: str = ""


class SummaryEntry(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    title: str
    level: int = Field(ge=0)


class SummaryPage(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_uid: str
    entries: tuple[SummaryEntry, ...]
    cursor: int = Field(ge=0)
    next_cursor: int | None = Field(default=None, ge=0)
    truncated: bool = False


class NodeWindow(DomainModel):
    """Résultat d'`ouvrir_noeud` (AD-1) : fenêtre de blocs du nœud, `truncated` si le nœud dépasse `node_window`."""

    node_id: str
    title: str = ""
    children: list[NodeChild] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    context_units: list[ContextUnit] = Field(default_factory=list)
    truncated: bool = False
    next_cursor: int | None = None
