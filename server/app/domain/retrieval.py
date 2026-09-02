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


class FacetteSuffisance(DomainModel):
    """Ce que le navigateur déclare pour **une** sous-question : une identité, ou une absence.

    Correctif du tour 2 (cause R2). Le verdict terminal ne connaissait qu'un `result_uid` — un seul,
    pour toute la question —, et le prompt ne mentionnait même pas les facettes alors qu'elles lui
    étaient envoyées. Un navigateur qui avait couvert la première sous-question n'avait donc aucun
    moyen de **dire** qu'il n'avait rien trouvé pour la seconde : son verdict était vrai, et muet
    sur la moitié de la demande.

    `result_uid = None` est une **déclaration d'absence**, pas une abstention : c'est le navigateur
    qui dit « je n'ai rien trouvé de décisionnel pour cette sous-question ». Elle ne décide de rien
    à elle seule — le code mesure la couverture de son côté (`FacetteCouverture`) et c'est lui qui
    fait autorité (AD-1 : le modèle propose, le code vérifie). Elle est publiée, et l'écart entre
    les deux lectures est nommé en trace.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    facette: int = Field(ge=0)
    result_uid: str | None = Field(default=None, max_length=80)


class SemanticSufficiencySelection(DomainModel):
    """Décision terminale du navigateur : une identité canonique ou une insuffisance explicite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sufficient: bool
    result_uid: str | None = Field(default=None, max_length=80)
    # Une entrée par sous-question quand la question en pose plusieurs ; vide sinon. Le défaut vide
    # garde inchangés le schéma de sortie des appelants sans facette et les verdicts déjà écrits.
    facettes: tuple[FacetteSuffisance, ...] = ()

    @model_validator(mode="after")
    def _identite_iff_suffisant(self) -> SemanticSufficiencySelection:
        if self.sufficient != (self.result_uid is not None):
            raise ValueError("result_uid est requis si et seulement si sufficient=true")
        rangs = [facette.facette for facette in self.facettes]
        if len(rangs) != len(set(rangs)):
            raise ValueError("une sous-question ne reçoit qu'un verdict")
        return self


class FacetteCouverture(DomainModel):
    """Ce que *retrouver* rapporte pour **une** facette de `ParsedQuestion` : ses blocs, ou rien.

    Les facettes sont arrêtées par *comprendre*, avant tout retrieval (AD-4). Jusqu'ici elles ne
    liaient que *vérifier*, qui **mesure** après coup si une affirmation affichée y répond : rien,
    entre les deux, n'obligeait la lecture à rapporter au moins une règle par sous-question, ni ne
    permettait de dire qu'elle n'en avait rapporté aucune. Une question à deux facettes dont une
    seule était lue rendait donc exactement la même chose qu'une question à une facette — sans que
    la différence soit dite nulle part.

    `rang` est la position de la facette dans `ParsedQuestion.facettes`, la même clé que
    `Verification.facettes_couvertes` : c'est un entier, jamais le libellé, qui vient du modèle et
    n'a rien à faire dans un résultat d'étape ni dans une trace (AD-10/AD-15).

    `block_ids` sont les blocs **transmis** que le classement de cette facette a proposés et dont le
    corpus confirme le kind décisionnel. `candidats` est la taille de ce classement, et c'est lui
    qui empêche la confusion que NFR2 interdit : **une lecture bornée ne prouve aucune absence**.

    Trois états, donc, et pas deux :

    - `retrouvee` — au moins un bloc décisionnel confirmé est parti à la rédaction ;
    - `absente` — le classement de la facette est **vide** : le contrat lu ne porte aucune clause
      décisionnelle confirmée pour cette sous-question. C'est une affirmation, et elle est
      légitime : rien n'est resté fermé ;
    - `bornee` — le classement proposait des candidats et aucun n'a été lu. Ce n'est **pas** une
      absence, c'est une borne de lecture : le quota d'ouvertures, le budget de blocs ou celui de
      tokens l'a arrêtée, et le résultat le dit par `truncated` plutôt que par un « rien trouvé »
      que la lecture ne peut pas soutenir.

    L'absence de **mesure**, enfin, se dit par une liste `RetrievalResult.facettes` vide — un
    résultat qui ne prétend rien plutôt qu'un résultat qui affirme le contraire de ce qu'il sait.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rang: int = Field(ge=0)
    block_ids: tuple[str, ...] = ()
    candidats: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _blocs_uniques(self) -> FacetteCouverture:
        if len(self.block_ids) != len(set(self.block_ids)):
            raise ValueError("une facette ne cite pas deux fois le même bloc")
        if len(self.block_ids) > self.candidats:
            raise ValueError("une facette ne retient pas plus de blocs que son classement n'en a "
                             "proposés")
        return self

    @property
    def retrouvee(self) -> bool:
        return bool(self.block_ids)

    @property
    def absente(self) -> bool:
        """Rien retrouvé **et** rien à lire : la seule forme d'absence qu'une lecture peut affirmer."""
        return not self.block_ids and not self.candidats

    @property
    def bornee(self) -> bool:
        """Rien retrouvé, mais des candidats restés fermés : une borne, jamais une absence (NFR2)."""
        return not self.block_ids and bool(self.candidats)


class RetrievalBudget(DomainModel):
    """Borne toute l'étape : appels modèle, nœuds, blocs, tokens, définitions et renvois inclus."""

    max_opens: int
    node_window: int
    search_limit: int
    # Ce plafond de sûreté appartient au budget lui-même : les tests, évals et appels directs ne
    # passent pas nécessairement par `Settings`. La livraison 2.6 l'avait fixé à deux tours ; la
    # mesure l'a rediscuté, comme son commentaire d'origine le prévoyait. **À deux tours, le
    # verdict terminal est structurellement inatteignable** : le tour 0 cherche, le tour 1 ouvre, et
    # les résultats du tour 1 ne sont jamais réinjectés — le navigateur ne voit donc jamais ce qu'il
    # a ouvert, ne peut constater aucun manque, et ne rend aucun verdict. Les trois runs A16 le
    # confirment : deux appels dans *retrouver*, suffisance toujours refusée, donc bandeau « je n'ai
    # pas pu tout lire » sur une réponse parfaitement sourcée. Le troisième tour est celui de la
    # conclusion ; ce qu'il peut ouvrir reste borné par `max_opens`, les blocs et les tokens.
    max_llm_turns: int = Field(ge=1, le=3)
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
    # Couverture par facette, une entrée par facette de `ParsedQuestion` quand l'appelant en a
    # déclaré une (le sinistre : c'est lui qui passe `kinds_suffisants`). Liste vide = non mesurée.
    facettes: list[FacetteCouverture] = Field(default_factory=list)
    truncated: bool = False

    def facette(self, rang: int) -> FacetteCouverture | None:
        return next((f for f in self.facettes if f.rang == rang), None)

    @property
    def facettes_absentes(self) -> list[int]:
        """Les rangs dont le contrat lu ne porte **aucune** clause décisionnelle confirmée.

        Les facettes seulement **bornées** en sont exclues : leur classement avait des candidats
        que le budget n'a pas laissé lire, et une lecture bornée ne prouve aucune absence (NFR2).
        Elles se disent par `truncated`, comme toute autre borne de l'étape.
        """
        return [f.rang for f in self.facettes if f.absente]

    @property
    def facettes_bornees(self) -> list[int]:
        """Les rangs dont des candidats décisionnels sont restés fermés sous le budget de l'étape."""
        return [f.rang for f in self.facettes if f.bornee]

    @model_validator(mode="after")
    def _facettes_uniques(self) -> RetrievalResult:
        rangs = [f.rang for f in self.facettes]
        if len(rangs) != len(set(rangs)):
            raise ValueError("une facette possède plusieurs couvertures")
        inconnus = [b for f in self.facettes for b in f.block_ids
                    if b not in {bloc.block_id for bloc in self.blocs}]
        if inconnus:
            # Une couverture qui nomme un bloc non transmis promettrait une lecture qui n'a pas eu
            # lieu : la rédaction ne verra jamais ce bloc, et l'aval croirait la facette couverte.
            raise ValueError(f"une facette cite des blocs non transmis : {sorted(set(inconnus))}")
        return self

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
    # Correctif G2 : le signal de navigation que le document porte lui-même — le texte du premier
    # bloc citable du nœud, tronqué. Aucune donnée nouvelle, aucune règle propre à un document :
    # pour le guide c'est le résumé de la fiche, pour un contrat la première clause de la section.
    # Vide quand le nœud n'a pas de bloc direct citable, ou quand le budget de la page ne le porte
    # pas. Un titre seul ne dit pas de quoi une section parle : sans lui, le navigateur d'un
    # document plat choisissait entre 87 titres nus.
    apercu: str = ""


class SummaryPage(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_uid: str
    entries: tuple[SummaryEntry, ...]
    cursor: int = Field(ge=0)
    next_cursor: int | None = Field(default=None, ge=0)
    truncated: bool = False
    # Correctif G2 : la mise en page **dérivée** pour ce document, publiée avec la page. Le
    # navigateur sait ainsi combien d'entrées une page porte et si un aperçu lui est servi ; la
    # trace et les tests lisent la dérivation au lieu de la recalculer.
    page_size: int = Field(default=0, ge=0)
    total_entries: int = Field(default=0, ge=0)


class NodeWindow(DomainModel):
    """Résultat d'`ouvrir_noeud` (AD-1) : fenêtre de blocs du nœud, `truncated` si le nœud dépasse `node_window`."""

    node_id: str
    title: str = ""
    children: list[NodeChild] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    context_units: list[ContextUnit] = Field(default_factory=list)
    truncated: bool = False
    next_cursor: int | None = None
