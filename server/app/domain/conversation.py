"""État conversationnel pur : événements, conflits et recalcul AD-6 sans modèle."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from .answer import Answer
from .document import DomainModel
from .question import Faits, QuestionScope
from .verdict import ClaimJugee, MissingPackage, Verdict, decider

FACT_KEY_MAX = 256
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
    turn: int = Field(default=0, ge=0, le=20)
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
        if self.action == "reponse" and (not self.question_id or self.value is None):
            raise ValueError("une réponse exige question_id et value")
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


def _question_candidates(claims: list[ClaimJugee], verdict: Verdict) -> list[TargetQuestion]:
    candidates: list[tuple[QuestionKind, str, str, str | None, str | None]] = []
    for claim in claims:
        if claim.champs is None:
            continue
        champs = claim.champs
        if (champs.fait_manquant or "").strip():
            fact = champs.fait_manquant.strip()
            candidates.append(("fait", f"Pouvez-vous confirmer ce fait : {fact} ?", fact,
                               claim.claim_id, fact))
        for quality in champs.qualites_non_etablies:
            candidates.append(("fait", f"L'événement présente-t-il cette qualité : {quality} ?",
                               quality, claim.claim_id, quality))
        if champs.option_requise:
            candidates.append(("option", "L'option requise était-elle souscrite à la date du sinistre ?",
                               "option_requise", claim.claim_id, None))
        if champs.cp_requise:
            candidates.append(("conditions_particulieres",
                               "Les conditions particulières établissent-elles le point exigé ?",
                               "conditions_particulieres", claim.claim_id, None))
    for text in verdict.ask_client:
        lower = text.casefold()
        if "option" in lower:
            item = ("option", text, "option_requise", None, None)
        elif "conditions particulières" in lower:
            item = ("conditions_particulieres", text, "conditions_particulieres", None, None)
        elif "avenant" in lower or "pris effet" in lower:
            item = ("avenant_date", text, "avenant_date", None, None)
        else:
            item = ("fait", text, text, None, text)
        candidates.append(item)
    result: list[TargetQuestion] = []
    seen: set[tuple[str, str, str | None]] = set()
    for kind, text, key, claim_id, expected in candidates:
        bounded = _fact_key(key)
        signature = (kind, bounded.casefold(), claim_id)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(TargetQuestion(
            question_id=_id("question", kind, bounded, claim_id), text=text, kind=kind,
            fact_key=bounded, claim_id=claim_id, expected_value=expected))
    return result


def _promote(questions: list[TargetQuestion]) -> None:
    active = sum(q.status == "active" for q in questions)
    for question in questions:
        if active >= 3:
            break
        if question.status == "pending":
            question.status = "active"
            active += 1


def _merge_questions(questions: list[TargetQuestion], claims: list[ClaimJugee], verdict: Verdict) -> None:
    existing = {q.question_id for q in questions}
    questions.extend(q for q in _question_candidates(claims, verdict) if q.question_id not in existing)
    _promote(questions)


def initialiser(*, doc_id: str, source_hash: str, ingest_fingerprint: str,
                pipeline_digest: str, prompts_digest: str, request_id: str, faits: Faits,
                answer: Answer, decision_claims: list[ClaimJugee]) -> ContinuationState:
    verdict = answer.verdict
    if verdict is None:
        raise ValueError("un premier tour sinistre exige un verdict")
    facts = _faits_initiaux(faits, answer.faits_compris)
    questions = _question_candidates(decision_claims, verdict)
    _promote(questions)
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
    return {event.question_id: classifier_reponse(event.value) for event in active_events(state)
            if event.question_id is not None}


def _recompute(state: ContinuationState) -> tuple[list[ClaimJugee], MissingPackage]:
    answers = _response_by_question(state)
    questions = {q.question_id: q for q in state.questions}
    claims = [claim.model_copy(deep=True) for claim in state.base_claims]
    missing = state.base_missing.model_copy(deep=True)
    unlinked_corrections = [event for event in active_events(state)
                            if event.source == "correction" and event.question_id is None]
    if unlinked_corrections:
        corrected = ", ".join(dict.fromkeys(event.key for event in unlinked_corrections))
        for claim in claims:
            if claim.champs is not None:
                claim.champs.fait_requis_present = False
                claim.champs.fait_manquant = f"valeur corrigée à confirmer : {corrected}"
    for claim in claims:
        if claim.champs is None:
            continue
        relevant = [(questions[qid], meaning) for qid, meaning in answers.items()
                    if qid in questions and questions[qid].claim_id == claim.claim_id
                    and questions[qid].kind == "fait"]
        negatives = [q for q, meaning in relevant if meaning == ResponseMeaning.NEGATIVE]
        positives = [q for q, meaning in relevant if meaning == ResponseMeaning.AFFIRMATIVE]
        if negatives:
            claim.champs.fait_requis_present = False
            claim.champs.fait_manquant = None
        elif positives:
            confirmed = {q.expected_value for q in positives if q.expected_value}
            claim.champs.qualites_non_etablies = [q for q in claim.champs.qualites_non_etablies
                                                  if q not in confirmed]
            if claim.champs.fait_manquant in confirmed:
                claim.champs.fait_manquant = None
            required = [q for q in state.questions
                        if q.claim_id == claim.claim_id and q.kind == "fait"]
            if required and all(answers.get(q.question_id) == ResponseMeaning.AFFIRMATIVE
                                for q in required):
                claim.champs.fait_requis_present = True
    for question in state.questions:
        meaning = answers.get(question.question_id)
        if meaning not in {ResponseMeaning.AFFIRMATIVE, ResponseMeaning.NEGATIVE}:
            continue
        targets = [c for c in claims if question.claim_id is None or c.claim_id == question.claim_id]
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
                if claim.champs is not None:
                    claim.champs.cp_requise = False
                    if meaning == ResponseMeaning.NEGATIVE:
                        claim.champs.fait_requis_present = False
                        claim.champs.fait_manquant = None
        elif question.kind == "avenant_date":
            if meaning == ResponseMeaning.AFFIRMATIVE:
                missing.avenants = False
                missing.date_effet = False
    return claims, missing


def _with(state: ContinuationState, **updates: object) -> ContinuationState:
    data = state.model_dump(mode="python")
    data.update(updates)
    return ContinuationState.model_validate(data)


def _describe(event: FactEvent) -> str:
    source = "tour client" if event.source in {"reponse_client", "correction"} else event.source
    return f"{event.key} = {event.value} ({source})"


def appliquer(state: ContinuationState, action: ConversationAction, *, request_id: str,
              ask_client_max: int) -> ContinuationState:
    if state.turn >= 20:
        raise ValueError("le dossier a atteint la limite de 20 tours : recommencer l'analyse")
    turn = state.turn + 1
    facts = [e.model_copy(deep=True) for e in state.facts]
    conflicts = [c.model_copy(deep=True) for c in state.conflicts]
    questions = [q.model_copy(deep=True) for q in state.questions]
    causal_ids: list[str] = []
    causal_events: list[str] = []
    decisive_terms: list[str] = []

    if action.action == "reponse":
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
    verdict = (previous.model_copy(deep=True)
               if any(c.status == "ouvert" for c in conflicts)
               else decider(claims, ask_client_max=ask_client_max, missing=missing))
    answer = state.answer.model_copy(update={"verdict": verdict}, deep=True)
    _merge_questions(questions, claims, verdict)
    changed = (verdict.value, verdict.reason) != (previous.value, previous.reason)
    history = [h.model_copy(deep=True) for h in state.history]
    history.append(VerdictTurn(
        turn=turn, value=verdict.value, reason=verdict.reason, changed=changed,
        causal_event_ids=causal_ids, causal_events=causal_events,
        decisive_terms=decisive_terms if changed else [], request_id=request_id))
    return _with(intermediate, answer=answer, decision_claims=claims, missing=missing,
                 questions=questions, history=history)
