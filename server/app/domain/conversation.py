"""État conversationnel pur : événements, conflits et recalcul AD-6 sans modèle."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from .answer import Answer, AnswerSegment
from .document import DomainModel
from .question import Faits, QuestionScope
from .verdict import (
    QUESTION_CONDITIONS_PARTICULIERES,
    QUESTION_OPTION,
    ClaimJugee,
    MissingPackage,
    Verdict,
    applicabilites_des_claims,
    conditions_de_section_ouvertes,
    decider,
    fusionner_faits,
    meme_fait,
    question_de_fait_exige,
    question_de_section,
    questions_du_paquet_typees,
)

FACT_KEY_MAX = 256
# La clé d'un fait que la personne précise sans qu'aucune question l'ait demandé (story 5.7, L1k).
# Neutre et numérotée : elle ne se confond avec aucune clé de question, donc ne déclenche ni conflit
# ni recalcul, et la page la lit pour ce qu'elle est — « Vous avez précisé : … ».
CLE_PRECISION = "precision_client:"
FactSource = Literal["declaration_initiale", "extraction", "reponse_client", "correction"]
QuestionKind = Literal["fait", "option", "conditions_particulieres", "avenant_date"]
QuestionStatus = Literal["pending", "active", "repondue"]
ConflictStatus = Literal["ouvert", "resolu"]
ActionKind = Literal["reponse", "correction", "resolution"]


class ResponseMeaning(str, Enum):
    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"


def _normalise(value: str) -> str:
    value = "".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                    if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value.strip())


INCONNU = frozenset({"", "inconnu", "je ne sais pas", "ne sait pas", "unknown", "?"})
OUI = frozenset({"oui", "yes", "vrai", "true", "confirme", "c'est confirme", "tout a fait"})
NON = frozenset({"non", "no", "faux", "false"})


def classifier_reponse(value: str) -> ResponseMeaning:
    """Une phrase informative n'est jamais une confirmation par défaut."""
    normalized = _normalise(value)
    if normalized in INCONNU:
        return ResponseMeaning.UNKNOWN
    if normalized in OUI:
        return ResponseMeaning.AFFIRMATIVE
    if normalized in NON or re.match(r"^(non|no)(?:\b|\s*[,;:.!?])", normalized):
        return ResponseMeaning.NEGATIVE
    patterns = (r"\bpas du tout\b", r"\bjamais\b", r"\bn['’]?est pas\b",
                r"\bn['’]?etait pas\b", r"\bne\b.{0,80}\bpas\b")
    if any(re.search(pattern, normalized) for pattern in patterns):
        return ResponseMeaning.NEGATIVE
    return ResponseMeaning.AMBIGUOUS


def _id(prefixe: str, *parties: object) -> str:
    brut = json.dumps(parties, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefixe}-{hashlib.sha256(brut.encode()).hexdigest()[:16]}"


def _texte(valeur: object) -> str:
    return f"{valeur:g}" if isinstance(valeur, float) else str(valeur).strip()


def _fact_key(value: str) -> str:
    value = value.strip()
    if len(value) <= FACT_KEY_MAX:
        return value
    suffix = hashlib.sha256(value.encode()).hexdigest()[:16]
    return f"{value[:FACT_KEY_MAX - 17]}-{suffix}"


def montant_de_description(description: str) -> str | None:
    """Extrait seulement un montant littéral accompagné de sa devise, sans modèle ni calcul."""
    match = re.search(r"(?<!\w)(\d+(?:[ .]\d{3})*(?:[,.]\d{1,2})?)\s*(€|euros?)(?!\w)",
                      description, flags=re.IGNORECASE)
    return None if match is None else f"{match.group(1).strip()} {match.group(2)}"


class FactEvent(DomainModel):
    event_id: str
    key: str = Field(max_length=FACT_KEY_MAX)
    value: str
    source: FactSource
    turn: int = Field(ge=0)
    question_id: str | None = None
    replaces_event_id: str | None = None


class FactConflict(DomainModel):
    conflict_id: str
    key: str = Field(max_length=FACT_KEY_MAX)
    event_ids: list[str] = Field(min_length=2)
    status: ConflictStatus = "ouvert"
    chosen_event_id: str | None = None
    resolution_event_id: str | None = None
    resolved_turn: int | None = Field(None, ge=1)


class TargetQuestion(DomainModel):
    question_id: str
    text: str
    kind: QuestionKind
    fact_key: str = Field(max_length=FACT_KEY_MAX)
    claim_id: str | None = None
    expected_value: str | None = None
    status: QuestionStatus = "pending"
    answered_event_id: str | None = None


class VerdictTurn(DomainModel):
    turn: int = Field(ge=0)
    value: str
    reason: str
    changed: bool = False
    causal_event_ids: list[str] = Field(default_factory=list)
    causal_events: list[str] = Field(default_factory=list)
    decisive_terms: list[str] = Field(default_factory=list)
    request_id: str


class ContinuationState(DomainModel):
    version: Literal[1] = 1
    doc_id: str
    source_hash: str
    ingest_fingerprint: str
    pipeline_digest: str
    prompts_digest: str
    initial_request_id: str
    turn: int = Field(default=0, ge=0)
    answer: Answer
    base_claims: list[ClaimJugee] = Field(default_factory=list)
    decision_claims: list[ClaimJugee] = Field(default_factory=list)
    base_missing: MissingPackage = Field(default_factory=MissingPackage)
    missing: MissingPackage = Field(default_factory=MissingPackage)
    facts: list[FactEvent] = Field(default_factory=list)
    conflicts: list[FactConflict] = Field(default_factory=list)
    questions: list[TargetQuestion] = Field(default_factory=list)
    history: list[VerdictTurn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references_exist(self) -> ContinuationState:
        events = {event.event_id for event in self.facts}
        if len(events) != len(self.facts):
            raise ValueError("deux événements portent le même identifiant")
        question_ids = {question.question_id for question in self.questions}
        if len(question_ids) != len(self.questions):
            raise ValueError("deux questions portent le même identifiant")
        replaced: set[str] = set()
        for event in self.facts:
            if event.question_id is not None and event.question_id not in question_ids:
                raise ValueError("un événement référence une question absente")
            if event.replaces_event_id is not None:
                if event.replaces_event_id not in events:
                    raise ValueError("une correction référence un événement absent")
                if event.replaces_event_id in replaced:
                    raise ValueError("un événement ne peut être remplacé deux fois")
                replaced.add(event.replaces_event_id)
        for conflict in self.conflicts:
            if len(set(conflict.event_ids)) != len(conflict.event_ids):
                raise ValueError("un conflit duplique une version")
            if not set(conflict.event_ids) <= events:
                raise ValueError("un conflit référence un événement absent")
            if conflict.status == "ouvert":
                if conflict.chosen_event_id is not None or conflict.resolution_event_id is not None:
                    raise ValueError("un conflit ouvert ne porte aucune résolution")
            elif (conflict.chosen_event_id not in conflict.event_ids or not conflict.resolution_event_id
                  or conflict.resolved_turn is None):
                raise ValueError("une résolution doit choisir une version et tracer son événement")
        for question in self.questions:
            if question.status == "repondue" and question.answered_event_id not in events:
                raise ValueError("une question répondue référence un événement absent")
            if question.status != "repondue" and question.answered_event_id is not None:
                raise ValueError("une question non répondue ne référence aucun événement")
        return self


class ConversationAction(DomainModel):
    action: ActionKind = "reponse"
    value: str | None = Field(None, max_length=500)
    question_id: str | None = None
    fact_key: str | None = Field(None, max_length=FACT_KEY_MAX)
    replaces_event_id: str | None = None
    conflict_id: str | None = None
    chosen_event_id: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> ConversationAction:
        # Story 5.7 (L1k) — `question_id` est **facultatif** sur une `reponse` : sans lui, la valeur
        # est un fait que la personne précise d'elle-même, pas la réponse à une question posée. Les
        # deux formes sont acceptées, et c'est la présence de `question_id` qui les sépare.
        if self.action == "reponse" and self.value is None:
            raise ValueError("une réponse ou une précision exige value")
        if self.action == "correction" and (not self.fact_key or self.value is None
                                              or not self.replaces_event_id):
            raise ValueError("une correction exige fact_key, value et replaces_event_id")
        if self.action == "resolution" and (not self.conflict_id or not self.chosen_event_id):
            raise ValueError("une résolution exige conflict_id et chosen_event_id")
        return self


def _faits_initiaux(faits: Faits, scope: QuestionScope | None) -> list[FactEvent]:
    values: list[tuple[str, object, FactSource]] = []
    for key, value in (("date", faits.date), ("lieu", faits.lieu),
                       ("montant_eur", faits.montant_eur), ("description", faits.description)):
        if value is not None and _texte(value):
            values.append((key, value, "declaration_initiale"))
    if faits.montant_eur is None:
        montant = montant_de_description(faits.description)
        if montant is not None:
            values.append(("montant_eur", montant, "extraction"))
    if scope is not None:
        mappings = (("bien", "bien"), ("evenement", "evenement"), ("lieu", "lieu"),
                    ("cause", "cause"), ("date", "moment"))
        for key, attribute in mappings:
            value = getattr(scope, attribute, None)
            if value is not None and _texte(value):
                values.append((key, value, "extraction"))
    return [FactEvent(event_id=_id("fait", 0, i, key, value, source), key=key,
                      value=_texte(value), source=source, turn=0)
            for i, (key, value, source) in enumerate(values)]


def _initial_conflicts(facts: list[FactEvent]) -> list[FactConflict]:
    grouped: dict[str, list[FactEvent]] = {}
    for event in facts:
        grouped.setdefault(event.key, []).append(event)
    conflicts: list[FactConflict] = []
    for key, events in grouped.items():
        known = [e for e in events if classifier_reponse(e.value) != ResponseMeaning.UNKNOWN]
        if len({_normalise(e.value) for e in known}) > 1:
            ids = [e.event_id for e in known]
            conflicts.append(FactConflict(conflict_id=_id("conflit", key, *ids),
                                          key=key, event_ids=ids))
    return conflicts


def _cle_de_section(block_id: str) -> str:
    """La condition écrite en tête d'une section est un fait **distinct** du paquet des CP.

    Les deux questions portent sur les conditions particulières, et elles ne demandent pas la même
    chose : l'une demande ce qu'elles prévoient (montants, franchises), l'autre si elles mentionnent
    **cette** garantie — c'est celle-là qui plafonne le verdict (L1e). Leur donner la même clé les
    faisait s'écraser l'une l'autre, et c'est la question décisive qui disparaissait.
    """
    return f"conditions_particulieres:{block_id}"


def _texte_question_cp(claim: ClaimJugee, ouvertes: dict[str, object], paquet: dict[str, str]) -> str:
    """La question des conditions particulières **de cette claim**, dans les mots du contrat.

    Quand une condition de section est ouverte sur l'une de ses clauses, c'est elle que la question
    nomme (« Vos conditions particulières mentionnent-elles la garantie « 3.1.4 Dégâts des eaux » ? ») :
    la personne reconnaît la garantie dont il s'agit. Sinon la clause renvoie aux CP sans les
    qualifier, et c'est la question du paquet manquant qui vaut.
    """
    for clause in claim.clauses:
        condition = clause.condition_section
        if condition is not None and condition.block_id in ouvertes:
            return question_de_section(condition)
    return paquet.get("conditions_particulieres", QUESTION_CONDITIONS_PARTICULIERES)


def _cle_de_claim_cp(claim: ClaimJugee, ouvertes: dict[str, object]) -> str:
    for clause in claim.clauses:
        condition = clause.condition_section
        if condition is not None and condition.block_id in ouvertes:
            return _cle_de_section(condition.block_id)
    return "conditions_particulieres"


# Story 5.7 (L1k) — l'ordre des questions **est** l'ordre de décision d'AD-6, et rien d'autre.
#
# Le parcours réel du 03/09 posait d'abord deux questions sur des exclusions que la table venait
# d'écarter, et reléguait en troisième la seule qui plafonnait le verdict. Quelqu'un qui répond dans
# l'ordre où on lui demande travaille alors longtemps avant que quoi que ce soit bouge. L'ordre suit
# donc ce qui décide : la condition qui plafonne, puis ce qu'exige la garantie, puis ce que
# vérifierait une exclusion, puis les pièces du dossier — dues quel que soit le verdict, donc jamais
# urgentes. Le tri est stable : à rang égal, l'ordre d'apparition des clauses est conservé.
RANG_CONDITION_DE_SECTION = 0
RANG_QUALITE_DE_GARANTIE = 1
RANG_FAIT_D_EXCLUSION = 2
RANG_PAQUET_CONTRACTUEL = 3


def _question_candidates(claims: list[ClaimJugee], verdict: Verdict) -> list[TargetQuestion]:
    """Les questions du fil, dites avec **les mots du moteur** et non ceux de la table (L1g).

    Le défaut que cette fonction portait : elle réécrivait chaque demande dans le vocabulaire du
    recalcul. « Les conditions particulières établissent-elles le point exigé ? » ne nomme ni la
    garantie, ni la pièce, ni ce que la personne doit aller regarder — c'est la ligne de la table
    d'AD-6 rendue visible, là où le verdict avait déjà formulé la question lisible
    (`Verdict.ask_client`, story 5.7 L1e). Le fil ne formule donc plus rien : il **reprend**
    `verdict.question_de_*` / `questions_du_paquet_typees`, l'unique vocabulaire, et n'ajoute que le
    rattachement décisionnel — `claim_id` et `expected_value` — dont la table a besoin et que le
    texte ne porte pas.

    Deux claims qui réclament la même pièce ne font pas deux questions : le même texte affiché deux
    fois est la même demande. Elles fusionnent alors en **une** question de dossier (`claim_id=None`),
    que `_recompute` applique aux claims qui l'avaient réellement déclarée.

    **Une clause écartée n'interroge rien (story 5.7, L1k).** C'était déjà la règle des libellés
    manquants (`verdict._libelles_manquants`) ; elle manquait ici, et le parcours réel du 03/09 en a
    montré le prix : deux des trois questions portaient sur des exclusions que la table avait rendues
    `applicable = "non"`, la page affichait à côté « non applicable · sa portée contractuelle ne
    couvre pas le cas déclaré », et répondre « oui » ne changeait rien — ce que la clause exigeait
    est sans objet, donc le recalcul n'avait rien à rouvrir. Poser une question dont la réponse ne
    peut rien faire est un simulacre d'affinage. Le filtre est le même code, appliqué au même endroit
    de la décision : `applicabilites_des_claims` sur les claims retenues.

    **Un fait n'est demandé qu'une fois (L1k).** Les libellés passent par `fusionner_faits` : les
    deux exclusions du parcours exigeaient le même défaut d'entretien sous deux formulations, et le
    fil posait deux questions pour un seul fait. La classe donne le libellé retenu — la question — et
    les clauses qui l'ont déclaré : la question est rattachée à celle qui l'a portée quand elle est
    seule, et devient une question de dossier dès qu'elles sont plusieurs, `_vise` la rendant alors à
    toutes celles dont l'exigence tombe dans la même classe.
    """
    retenues = [claim for claim in claims if claim.retenue]
    etat = {claim_id: value for claim_id, (value, _reason)
            in applicabilites_des_claims(retenues).items()}
    ouvertes = {condition.block_id: condition
                for condition in conditions_de_section_ouvertes(retenues, etat=etat)}
    paquet_typees = questions_du_paquet_typees(retenues, verdict.missing)
    paquet = dict(paquet_typees)
    candidates: list[tuple[int, QuestionKind, str, str, str | None, str | None]] = []
    exiges: list[tuple[ClaimJugee, str]] = []
    for claim in claims:
        if claim.champs is None or etat.get(claim.claim_id) == "non":
            continue
        champs = claim.champs
        if (champs.fait_manquant or "").strip():
            exiges.append((claim, champs.fait_manquant.strip()))
        for quality in champs.qualites_non_etablies:
            if quality.strip():
                exiges.append((claim, quality.strip()))
        if champs.option_requise:
            candidates.append((RANG_PAQUET_CONTRACTUEL, "option",
                               paquet.get("option", QUESTION_OPTION),
                               "option_requise", claim.claim_id, None))
        if champs.cp_requise:
            cle = _cle_de_claim_cp(claim, ouvertes)
            candidates.append((RANG_CONDITION_DE_SECTION if cle != "conditions_particulieres"
                               else RANG_PAQUET_CONTRACTUEL,
                               "conditions_particulieres",
                               _texte_question_cp(claim, ouvertes, paquet),
                               cle, claim.claim_id, None))
    for libelle, variantes in fusionner_faits([texte for _claim, texte in exiges]):
        porteuses = [claim for claim, texte in exiges if texte in variantes]
        cibles = {claim.claim_id for claim in porteuses}
        kind = porteuses[0].kind
        candidates.append((RANG_QUALITE_DE_GARANTIE if kind != "exclusion"
                           else RANG_FAIT_D_EXCLUSION,
                           "fait", question_de_fait_exige(libelle, kind=kind), libelle,
                           porteuses[0].claim_id if len(cibles) == 1 else None, libelle))
    # Les questions du moteur, **typées par identité** et non par mots-clés : ce sont exactement les
    # textes que `verdict.py` vient de composer, retrouvés tels quels dans `ask_client`. La condition
    # de section passe **devant** le paquet manquant : c'est elle qui plafonne le verdict, donc celle
    # que la personne doit lire en premier — le paquet, lui, est dû quel que soit le verdict.
    cles_du_paquet = {"option": "option_requise",
                      "conditions_particulieres": "conditions_particulieres",
                      "avenant_date": "avenant_date"}
    connues: dict[str, tuple[QuestionKind, str]] = {
        question_de_section(condition): ("conditions_particulieres", _cle_de_section(block_id))
        for block_id, condition in ouvertes.items()}
    sections = set(connues)
    connues.update({texte: (kind, cles_du_paquet[kind]) for kind, texte in paquet_typees})
    for text in verdict.ask_client:
        connue = connues.get(text)
        if connue is not None:
            kind, cle = connue
            candidates.append((RANG_CONDITION_DE_SECTION if text in sections
                               else RANG_PAQUET_CONTRACTUEL, kind, text, cle, None, None))
            continue
        lower = text.casefold()
        if "option" in lower:
            item = ("option", text, "option_requise", None, None)
        elif "conditions particulières" in lower or "condition posée en tête" in lower:
            item = ("conditions_particulieres", text, "conditions_particulieres", None, None)
        elif "avenant" in lower or "pris effet" in lower:
            # Le texte du moteur, tel quel : la version précédente le réécrivait en « Les pièces
            # établissent-elles… », une phrase que rien n'avait formulée et qui ne disait plus
            # quelles pièces ni pourquoi.
            item = ("avenant_date", text, "avenant_date", None, None)
        else:
            # Une question factuelle sans claim cible serait visible mais décisionnellement morte :
            # les faits/qualités des claims ont déjà produit leurs questions liées ci-dessus.
            continue
        candidates.append((RANG_PAQUET_CONTRACTUEL, *item))
    candidates.sort(key=lambda candidate: candidate[0])
    result: list[TargetQuestion] = []
    position: dict[tuple[str, str], int] = {}
    emis: set[str] = set()
    for _rang, kind, text, key, claim_id, expected in candidates:
        bounded = _fact_key(key)
        rang = position.get((kind, " ".join(text.split()).casefold()))
        if rang is not None:
            # Le même texte déjà posé. Venant d'une **autre** claim, c'est la demande du dossier et
            # elle se délie de sa claim ; venant du moteur sans cible (`ask_client`), la question
            # liée le couvre déjà et le doublon disparaît.
            existante = result[rang]
            if claim_id is not None and existante.claim_id not in (None, claim_id):
                delie = _id("question", kind, existante.fact_key, None)
                if delie not in emis:
                    emis.discard(existante.question_id)
                    emis.add(delie)
                    result[rang] = existante.model_copy(update={
                        "question_id": delie, "claim_id": None})
            continue
        question_id = _id("question", kind, bounded, claim_id)
        if question_id in emis:
            continue  # même pièce, même cible : une seule question, la première formulée
        emis.add(question_id)
        position[(kind, " ".join(text.split()).casefold())] = len(result)
        result.append(TargetQuestion(
            question_id=question_id, text=text, kind=kind,
            fact_key=bounded, claim_id=claim_id, expected_value=expected))
    return result


def _promote(questions: list[TargetQuestion], *, active_questions_max: int) -> None:
    active = sum(q.status == "active" for q in questions)
    for question in questions:
        if active >= active_questions_max:
            break
        if question.status == "pending":
            question.status = "active"
            active += 1


def _merge_questions(questions: list[TargetQuestion], claims: list[ClaimJugee], verdict: Verdict,
                     *, active_questions_max: int) -> None:
    existing = {q.question_id for q in questions}
    questions.extend(q for q in _question_candidates(claims, verdict) if q.question_id not in existing)
    _promote(questions, active_questions_max=active_questions_max)


def initialiser(*, doc_id: str, source_hash: str, ingest_fingerprint: str,
                pipeline_digest: str, prompts_digest: str, request_id: str, faits: Faits,
                answer: Answer, decision_claims: list[ClaimJugee],
                active_questions_max: int) -> ContinuationState:
    verdict = answer.verdict
    if verdict is None:
        raise ValueError("un premier tour sinistre exige un verdict")
    facts = _faits_initiaux(faits, answer.faits_compris)
    questions = _question_candidates(decision_claims, verdict)
    _promote(questions, active_questions_max=active_questions_max)
    base_missing = verdict.missing.model_copy(deep=True)
    return ContinuationState.model_validate({
        "doc_id": doc_id, "source_hash": source_hash, "ingest_fingerprint": ingest_fingerprint,
        "pipeline_digest": pipeline_digest, "prompts_digest": prompts_digest,
        "initial_request_id": request_id, "answer": answer, "base_claims": decision_claims,
        "decision_claims": decision_claims, "base_missing": base_missing, "missing": base_missing,
        "facts": facts, "conflicts": _initial_conflicts(facts), "questions": questions,
        "history": [VerdictTurn(turn=0, value=verdict.value, reason=verdict.reason,
                                request_id=request_id)],
    })


def _resolved_losers(state: ContinuationState) -> set[str]:
    return {eid for conflict in state.conflicts if conflict.status == "resolu"
            for eid in conflict.event_ids if eid != conflict.chosen_event_id}


def active_events(state: ContinuationState, key: str | None = None,
                  *, include_contested: bool = True) -> list[FactEvent]:
    """Versions actives; le dossier peut exclure toutes celles d'un conflit ouvert."""
    removed = {event.replaces_event_id for event in state.facts if event.replaces_event_id}
    removed |= _resolved_losers(state)
    if not include_contested:
        removed |= {eid for conflict in state.conflicts if conflict.status == "ouvert"
                    for eid in conflict.event_ids}
    return [event for event in state.facts
            if event.event_id not in removed and (key is None or event.key == key)]


def _response_by_question(state: ContinuationState) -> dict[str, ResponseMeaning]:
    return {event.question_id: classifier_reponse(event.value)
            for event in active_events(state, include_contested=False)
            if event.question_id is not None}


def _vise(question: TargetQuestion, claim: ClaimJugee) -> bool:
    """La question porte-t-elle sur cette claim ?

    Story 5.7 (L1g). Une question **de dossier** (`claim_id=None`) n'est pas une question sans cible :
    c'est la même demande, posée par plusieurs claims, fusionnée pour n'être lue qu'une fois
    (`_question_candidates`). Elle vise donc les claims qui l'avaient déclarée — reconnues au libellé
    qu'elles portent, ou à la pièce qu'elles subordonnent —, jamais toutes : répondre « non » à un
    fait qu'une seule clause exige ne doit pas dégrader celles qui ne l'exigeaient pas. Une question
    qu'aucune claim ne réclame (le paquet manquant d'AD-6, dû quel que soit le verdict) ne vise rien
    ici, et `_cibles` la rend alors à toutes les claims, comme avant.

    Le test se fait toujours sur les claims **de base**, jamais sur les copies que `_recompute`
    amende : une qualité que le recalcul vient de retirer reste la qualité que la question demandait.
    """
    if question.claim_id is not None:
        return question.claim_id == claim.claim_id
    champs = claim.champs
    if champs is None:
        return False
    if question.kind == "fait":
        expected = question.expected_value
        # L1k : la comparaison passe par `meme_fait`, pas par l'égalité de chaînes. Une question
        # fusionnée porte **le libellé retenu** ; la clause qui l'avait déclaré dans d'autres termes
        # doit malgré tout se reconnaître dedans, sinon la réponse ne l'atteint jamais.
        return expected is not None and any(
            meme_fait(expected, libelle) for libelle in
            [(champs.fait_manquant or "").strip(), *champs.qualites_non_etablies] if libelle)
    if question.kind == "option":
        return champs.option_requise
    if question.kind == "conditions_particulieres":
        return champs.cp_requise
    return False


def _cibles(question: TargetQuestion, claims: list[ClaimJugee],
            vises: dict[str, set[str]]) -> list[ClaimJugee]:
    vise = vises.get(question.question_id) or set()
    if question.claim_id is not None or vise:
        return [claim for claim in claims if claim.claim_id in vise]
    return list(claims)


def _recompute(state: ContinuationState) -> tuple[list[ClaimJugee], MissingPackage]:
    answers = _response_by_question(state)
    questions = {q.question_id: q for q in state.questions}
    vises = {question.question_id:
             {claim.claim_id for claim in state.base_claims if _vise(question, claim)}
             for question in state.questions}
    contested_keys = {conflict.key.casefold() for conflict in state.conflicts
                      if conflict.status == "ouvert"}
    claims = [claim.model_copy(deep=True) for claim in state.base_claims]
    missing = state.base_missing.model_copy(deep=True)
    for claim in claims:
        if claim.champs is None:
            continue
        relevant = [(questions[qid], meaning) for qid, meaning in answers.items()
                    if qid in questions and claim.claim_id in vises[qid]
                    and questions[qid].kind == "fait"]
        negatives = [q for q, meaning in relevant if meaning == ResponseMeaning.NEGATIVE]
        positives = [q for q, meaning in relevant if meaning == ResponseMeaning.AFFIRMATIVE]
        contested = next((q for q in state.questions if claim.claim_id in vises[q.question_id]
                          and (q.fact_key.casefold() in contested_keys
                               or (q.expected_value or "").casefold() in contested_keys)), None)
        if negatives:
            claim.champs.fait_requis_present = False
            claim.champs.fait_manquant = None
        elif positives:
            # L1k, même raison que dans `_vise` : « oui » lève l'exigence que la question portait,
            # y compris chez la clause qui l'écrivait autrement. Sans cela une question fusionnée
            # laissait sa seconde clause `humain`, et le verdict ne bougeait pas d'un pouce.
            confirmed = [q.expected_value for q in positives if q.expected_value]
            claim.champs.qualites_non_etablies = [
                q for q in claim.champs.qualites_non_etablies
                if not any(meme_fait(dit, q) for dit in confirmed)]
            manquant = (claim.champs.fait_manquant or "").strip()
            if manquant and any(meme_fait(dit, manquant) for dit in confirmed):
                claim.champs.fait_manquant = None
            required = [q for q in state.questions
                        if claim.claim_id in vises[q.question_id] and q.kind == "fait"]
            if required and all(answers.get(q.question_id) == ResponseMeaning.AFFIRMATIVE
                                for q in required):
                claim.champs.fait_requis_present = True
        if contested is not None and not negatives:
            claim.champs.fait_requis_present = False
            claim.champs.fait_manquant = contested.expected_value or contested.fact_key
    for question in state.questions:
        meaning = answers.get(question.question_id)
        if meaning not in {ResponseMeaning.AFFIRMATIVE, ResponseMeaning.NEGATIVE}:
            continue
        targets = _cibles(question, claims, vises)
        levees: set[str] = set()  # L1m : les conditions de section que cette réponse établit
        if question.kind == "option":
            missing.options_souscrites = False
            for claim in targets:
                if claim.champs is not None:
                    claim.champs.option_requise = False
                    if meaning == ResponseMeaning.NEGATIVE:
                        claim.champs.fait_requis_present = False
                        claim.champs.fait_manquant = None
        elif question.kind == "conditions_particulieres":
            missing.conditions_particulieres = False
            for claim in targets:
                # Story 5.7 (L1e) : la condition écrite en tête de la section renvoie aux conditions
                # particulières, et c'est elle que la question rapporte. Une réponse affirmative la
                # lève ; sans cela le verdict resterait plafonné à `sous_conditions` sur une pièce que
                # le gestionnaire vient précisément de produire, et la boucle ne se refermerait jamais.
                # Une réponse négative la laisse ouverte : les CP ne mentionnent pas la garantie.
                if meaning == ResponseMeaning.AFFIRMATIVE:
                    for clause in claim.clauses:
                        if clause.condition_section is not None and clause.condition_section.renvoie_cp:
                            levees.add(clause.condition_section.block_id)
                            clause.condition_section = None
                if claim.champs is not None:
                    claim.champs.cp_requise = False
                    if meaning == ResponseMeaning.NEGATIVE:
                        claim.champs.fait_requis_present = False
                        claim.champs.fait_manquant = None
            # Story 5.7 (L1m) : la condition est celle de la **section**, pas de la claim qui a porté
            # la question. Le parcours de prod du 04/09 le montre : deux clauses du même nœud
            # « 3.1.4 Dégâts des eaux » la portaient, la question n'en visait qu'une, et le verdict
            # restait plafonné par la seconde après que la personne eut produit la pièce. Une pièce
            # produite l'est pour toute la section qu'elle mentionne.
            for claim in claims:
                for clause in claim.clauses:
                    if clause.condition_section is not None and clause.condition_section.block_id in levees:
                        clause.condition_section = None
        elif question.kind == "avenant_date":
            if meaning == ResponseMeaning.AFFIRMATIVE:
                missing.avenants = False
                missing.date_effet = False
            else:
                # L'absence de ces pièces concerne la période de validité de toutes les clauses :
                # elle ne prouve pas une non-couverture, elle remet explicitement leur application
                # à l'état humain jusqu'à production de la date/du ou des avenants.
                for claim in targets:
                    if claim.champs is not None:
                        claim.champs.fait_requis_present = False
                        claim.champs.fait_manquant = "date d'effet et avenants applicables"
    return claims, missing


def _with(state: ContinuationState, **updates: object) -> ContinuationState:
    data = state.model_dump(mode="python")
    data.update(updates)
    return ContinuationState.model_validate(data)


def _describe(event: FactEvent) -> str:
    source = "tour client" if event.source in {"reponse_client", "correction"} else event.source
    return f"{event.key} = {event.value} ({source})"


def _inconnues_du_tour(initial: Answer, *, found: bool, complete: bool) -> list[str]:
    """Ce que le tour de suivi déclare ne pas savoir — story 4.2f.

    **Le suivi ne relit rien.** Il n'ouvre aucun nœud, n'appelle aucun modèle et rejoue la table
    d'AD-6 sur l'état signé du premier tour. Quand ce premier tour était une **lecture partielle**,
    sa borne vaut donc encore, mot pour mot : le porteur est reconduit par le `model_copy` ci-dessous
    (il n'y a rien qui l'aurait levée), et il doit l'être **avec ce qu'il annonçait manquer** — le
    domaine exige qu'une `LecturePartielle` dise ce qui lui manque, et rien ici ne l'apprend.

    Ne pas reconduire le porteur n'était pas une option : `found` reste faux (les affirmations
    vérifiées du suivi sont celles du premier tour, et une lecture partielle n'en a aucune), et
    `found=False` exige exactement un porteur. Le retirer aurait laissé un refus sans rien pour
    l'expliquer ; le garder en vidant `unknown[]` faisait lever pydantic, donc un 400 portant un
    message de validation interne à chaque tour du fil. Les phrases reconduites sont celles que
    *restituer* a projetées dans la langue de la réponse : rien n'est réécrit ici.
    """
    if initial.lecture_partielle is not None:
        return list(initial.unknown)
    if complete or not found:
        return []
    return ["Le verdict conserve des conditions, des pièces ou des faits non établis."]


LABELS_VERDICT = {
    "couvert": "couvert", "non_couvert": "non couvert",
    "sous_conditions": "sous conditions", "ne_tranche_pas": "ne tranche pas",
}


AMORCE_PREFIXE = "Verdict mis à jour avec vos réponses, sans relire le contrat : "


def amorce_de_recalcul(verdict: Verdict) -> str:
    """La seule phrase que le tour de suivi écrit lui-même — et elle dit ce qu'elle a fait."""
    return f"{AMORCE_PREFIXE}{LABELS_VERDICT[verdict.value]}."


def _est_amorce(texte: str) -> bool:
    return texte.strip().startswith(AMORCE_PREFIXE)


def _corps_sans_amorce(texte: str) -> str:
    """Le texte du tour précédent, débarrassé de la mention que ce tour-ci va réécrire (L1k).

    `_followup_answer` compose à partir de l'`Answer` du tour **précédent**, qui portait déjà sa
    propre mention : au deuxième tour la page en affichait deux, au troisième trois, chacune datée
    d'un verdict révolu. La mention n'est pas un contenu de la réponse, c'est l'en-tête du tour
    courant ; on la retire avant de la réécrire, et le corps du modèle est le même à tous les tours.
    """
    corps = texte.strip()
    if not _est_amorce(corps):
        return corps
    _amorce, _separateur, reste = corps.partition("\n\n")
    return reste.strip()


def _followup_answer(initial: Answer, claims: list[ClaimJugee], verdict: Verdict) -> Answer:
    """Le texte du modèle **conservé**, précédé de ce que la réponse du client a changé (L1g).

    Ce que cette fonction faisait, et que la lecture du 04/09 a montré : elle jetait la réponse. Le
    texte du premier tour — les phrases du modèle, la transition, le rattachement aux faits — était
    remplacé par « Verdict recalculé : sous conditions. <raison de la table> », et le `text` de
    chaque clause par « Clause vérifiée conservée pour le recalcul du verdict. ». La personne qui
    répondait à une question perdait donc la réponse qu'elle venait de lire, au profit de la
    projection de l'état interne. C'est une lecture de la table d'AD-6, pas une réponse.

    La règle qui l'avait motivée reste entière : **aucun texte figé ne peut contredire le verdict
    courant**. Elle est tenue autrement, et plus exactement. Ce que le premier tour a écrit est un
    compte rendu du contrat (« ce que disent les conditions générales »), que la réponse du client
    n'invalide pas — elle ajoute une pièce au dossier. Ce qui pourrait contredire le verdict courant,
    c'est la **conclusion** ; elle est donc réénoncée en tête, dans une phrase qui dit d'où elle
    vient et ce qu'elle n'a pas fait (« sans relire le contrat »), et le statut d'applicabilité de
    chaque clause est recalculé sur ses champs — ce que le front affiche sous la clause. La raison
    technique de la table, elle, n'a jamais eu sa place dans une phrase servie : elle vit dans
    `verdict.reason` et dans la trace, sous « Comment cette réponse a été obtenue ».
    """
    prefixe = amorce_de_recalcul(verdict)
    resolutions = applicabilites_des_claims(claims)
    judged = {claim.claim_id: resolution for claim, resolution in
              ((claim, resolutions.get(claim.claim_id)) for claim in claims)}
    verified = []
    for claim in initial.claims:
        resolution = judged.get(claim.claim_id)
        status = claim.status.model_copy(deep=True)
        if resolution is not None:
            status.applicable, status.applicable_reason = resolution
        else:
            status.applicable, status.applicable_reason = None, None
        verified.append(claim.model_copy(update={"status": status}, deep=True))
    rejected = []
    for claim in initial.rejected_claims:
        status = claim.status.model_copy(update={
            "applicable": None, "applicable_reason": None}, deep=True)
        rejected.append(claim.model_copy(update={"status": status}, deep=True))
    found = bool(verified)
    complete = found and verdict.value in {"couvert", "non_couvert"}
    corps = _corps_sans_amorce(initial.texte)
    anciens_segments = [segment for segment in initial.segments
                        if not (segment.kind == "limite" and _est_amorce(segment.text))]
    answer = initial.model_copy(update={
        "found": found,
        "complete": complete,
        "texte": f"{prefixe}\n\n{corps}" if corps else prefixe,
        "segments": [AnswerSegment(text=prefixe, kind="limite"),
                     *[segment.model_copy(deep=True) for segment in anciens_segments]],
        "claims": verified,
        "rejected_claims": rejected,
        "unknown": _inconnues_du_tour(initial, found=found, complete=complete),
        "verdict": verdict,
    }, deep=True)
    answer._decision_claims = [claim.model_copy(deep=True) for claim in claims]
    return Answer.model_validate(answer.model_dump(mode="python"))


def appliquer(state: ContinuationState, action: ConversationAction, *, request_id: str,
              ask_client_max: int, max_turns: int,
              active_questions_max: int) -> ContinuationState:
    if state.turn >= max_turns:
        raise ValueError(f"le dossier a atteint la limite de {max_turns} tours : recommencer l'analyse")
    turn = state.turn + 1
    facts = [e.model_copy(deep=True) for e in state.facts]
    conflicts = [c.model_copy(deep=True) for c in state.conflicts]
    questions = [q.model_copy(deep=True) for q in state.questions]
    causal_ids: list[str] = []
    causal_events: list[str] = []
    decisive_terms: list[str] = []

    if action.action == "reponse" and action.question_id is None:
        # Story 5.7 (L1k) — **le fait libre**. Sur le parcours réel du 03/09, « Le voisin du dessous
        # a fait constater des dégâts sur son plafond pour 2 500 euros » est parti sous la clé de la
        # question sélectionnée, et le dossier l'a relu comme la réponse à « défaut de réparation ou
        # d'entretien » : une précision spontanée devenait l'aveu d'une faute. Un fait libre porte
        # donc une clé neutre à lui — il n'a répondu à rien, il ne referme aucune question, et le
        # recalcul ne le lit pas (`_response_by_question` ne voit que les événements liés à une
        # question). Ce qu'une question attend, la personne le dit **par** la question.
        assert action.value is not None
        precision = action.value.strip()
        if not precision:
            raise ValueError("une précision vide n'ajoute aucun fait au dossier")
        rang = 1 + sum(1 for e in facts if e.key.startswith(CLE_PRECISION))
        key = f"{CLE_PRECISION}{rang}"
        event = FactEvent(event_id=_id("fait", "precision", turn, key, precision), key=key,
                          value=precision, source="reponse_client", turn=turn)
        facts.append(event)
        causal_ids.append(event.event_id)
        causal_events.append(_describe(event))
        decisive_terms.append(key)
    elif action.action == "reponse":
        question = next((q for q in questions if q.question_id == action.question_id), None)
        if question is None:
            raise ValueError("question inconnue ou périmée")
        if question.status == "repondue":
            raise ValueError("cette question a déjà reçu une réponse")
        if question.status != "active":
            raise ValueError("question inconnue ou périmée")
        assert action.value is not None
        meaning = classifier_reponse(action.value)
        value = "inconnu" if meaning == ResponseMeaning.UNKNOWN else action.value.strip()
        event = FactEvent(event_id=_id("fait", question.question_id, action.value),
                          key=question.fact_key, value=value, source="reponse_client", turn=turn,
                          question_id=question.question_id)
        prior = active_events(state, question.fact_key)
        contradictory = meaning != ResponseMeaning.UNKNOWN and any(
            classifier_reponse(old.value) != ResponseMeaning.UNKNOWN
            and _normalise(old.value) != _normalise(event.value) for old in prior)
        facts.append(event)
        causal_ids.append(event.event_id)
        causal_events.append(_describe(event))
        decisive_terms.append(question.expected_value or question.fact_key)
        question.status = "repondue"
        question.answered_event_id = event.event_id
        if contradictory:
            ids = [*[old.event_id for old in prior], event.event_id]
            conflicts.append(FactConflict(conflict_id=_id("conflit", question.fact_key, *ids),
                                          key=question.fact_key, event_ids=ids))
    elif action.action == "correction":
        replaced = next((e for e in facts if e.event_id == action.replaces_event_id), None)
        if replaced is None or replaced.key != action.fact_key:
            raise ValueError("fait corrigé absent ou d'une autre nature")
        assert action.value is not None and action.fact_key is not None
        if any(c.status == "ouvert" and replaced.event_id in c.event_ids for c in conflicts):
            raise ValueError("ce fait participe à un conflit ouvert : résolvez d'abord le conflit")
        if any(c.status == "resolu" and replaced.event_id in c.event_ids
               and replaced.event_id != c.chosen_event_id for c in conflicts):
            raise ValueError("ce fait a déjà été remplacé par la résolution du conflit")
        replacement = next((e for e in facts if e.replaces_event_id == replaced.event_id), None)
        if replacement is not None:
            if _normalise(replacement.value) == _normalise(action.value):
                return ContinuationState.model_validate(state.model_dump(mode="python"))
            raise ValueError("ce fait a déjà été remplacé")
        if _normalise(replaced.value) == _normalise(action.value):
            return ContinuationState.model_validate(state.model_dump(mode="python"))
        event = FactEvent(event_id=_id("fait", "correction", replaced.event_id, action.value),
                          key=action.fact_key, value=action.value.strip(), source="correction",
                          turn=turn, replaces_event_id=replaced.event_id,
                          question_id=replaced.question_id)
        facts.append(event)
        causal_ids.append(event.event_id)
        causal_events.append(_describe(event))
        decisive_terms.append(action.fact_key)
        other_active = [e for e in active_events(state, action.fact_key)
                        if e.event_id != replaced.event_id]
        contradictory = any(classifier_reponse(e.value) != ResponseMeaning.UNKNOWN
                            and _normalise(e.value) != _normalise(event.value) for e in other_active)
        if contradictory:
            ids = [*[e.event_id for e in other_active], event.event_id]
            conflicts.append(FactConflict(conflict_id=_id("conflit", action.fact_key, *ids),
                                          key=action.fact_key, event_ids=ids))
    else:
        conflict = next((c for c in conflicts if c.conflict_id == action.conflict_id), None)
        if conflict is None or conflict.status != "ouvert":
            raise ValueError("conflit inconnu ou déjà résolu")
        if action.chosen_event_id not in conflict.event_ids:
            raise ValueError("la version choisie n'appartient pas au conflit")
        chosen = next(e for e in facts if e.event_id == action.chosen_event_id)
        resolution_id = _id("resolution", conflict.conflict_id, chosen.event_id)
        conflict.status = "resolu"
        conflict.chosen_event_id = chosen.event_id
        conflict.resolution_event_id = resolution_id
        conflict.resolved_turn = turn
        causal_ids.append(resolution_id)
        causal_events.append(f"conflit {conflict.key} résolu : {_describe(chosen)}")
        decisive_terms.append(conflict.key)

    intermediate = _with(state, turn=turn, facts=facts, conflicts=conflicts, questions=questions)
    claims, missing = _recompute(intermediate)
    previous = state.answer.verdict
    assert previous is not None
    verdict = decider(claims, ask_client_max=ask_client_max, missing=missing)
    answer = _followup_answer(state.answer, claims, verdict)
    _merge_questions(questions, claims, verdict,
                     active_questions_max=active_questions_max)
    changed = (verdict.value, verdict.reason) != (previous.value, previous.reason)
    history = [h.model_copy(deep=True) for h in state.history]
    history.append(VerdictTurn(
        turn=turn, value=verdict.value, reason=verdict.reason, changed=changed,
        causal_event_ids=causal_ids, causal_events=causal_events,
        decisive_terms=decisive_terms if changed else [], request_id=request_id))
    return _with(intermediate, answer=answer, decision_claims=claims, missing=missing,
                 questions=questions, history=history)
