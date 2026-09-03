"""Matrice hermétique de la story 3.7 : aucun réseau, modèle ou retrieval."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app.api.conversation_token import signer, verifier
from server.app.api.etat import _conversation_secret
from server.app.api.main import create_app
from server.app.config import Settings
from server.app.domain.answer import AbsenceProof, Answer, LecturePartielle
from server.app.domain.conversation import (
    FACT_KEY_MAX,
    ContinuationState,
    ConversationAction,
    FactEvent,
    appliquer as appliquer_domaine,
    classifier_reponse,
    initialiser as initialiser_domaine,
    montant_de_description,
)
from server.app.domain.errors import ErrorCode, InvalidRequest, PipelineError
from server.app.domain.question import Faits, QuestionScope
from server.app.domain.verdict import (
    ChampsApplicabilite,
    ClaimJugee,
    ClauseCitee,
    ConditionDeSection,
    MissingPackage,
    decider,
)
from tests.test_api_sinistre import Double, XFF, _corps, _mini_corpus, _reponse, _settings, _trace


def initialiser(**kwargs):
    return initialiser_domaine(active_questions_max=3, **kwargs)


def appliquer(state, action, *, request_id: str, ask_client_max: int):
    return appliquer_domaine(
        state, action, request_id=request_id, ask_client_max=ask_client_max,
        max_turns=20, active_questions_max=3)


def _claim() -> ClaimJugee:
    return ClaimJugee(
        claim_id="c-garantie",
        clauses=[ClauseCitee(
            block_id="cg:p1:1", kind="garantie", kind_confirmed=True,
            portee={"cg:socle"}, node_id="cg:socle", socle=True,
            qualificatifs=["subit"])],
        champs=ChampsApplicabilite(
            fait_requis_present=False, fait_manquant="caractère subit",
            qualites_exigees=["caractère subit"],
            qualites_non_etablies=["caractère subit"]),
    )


def _state():
    claim = _claim()
    verdict = decider([claim], ask_client_max=3, missing=MissingPackage())
    answer = Answer(
        found=False, complete=False, texte="Aucune conclusion documentaire.",
        reason=AbsenceProof(kind="claims_rejetes"), verdict=verdict,
        faits_compris=QuestionScope(bien="canapé", cause="bougie"))
    return initialiser(
        doc_id="cg", source_hash="source", ingest_fingerprint="ingest",
        pipeline_digest="pipeline", prompts_digest="prompts", request_id="r-0",
        faits=Faits(description="Une bougie est tombée sur le canapé.", montant_eur=1200),
        answer=answer, decision_claims=[claim])


def _state_lecture_partielle():
    """Story 4.2f : un premier tour dont la **lecture a été bornée** et où rien n'a survécu.

    Aucune clause vérifiée, donc aucune `ClaimJugee` décisionnelle : le fil s'ouvre sur les seules
    questions du paquet manquant, exactement comme un `ne_tranche_pas` ordinaire. Ce qui change est
    le porteur de l'`Answer` — une `LecturePartielle` au lieu d'une `AbsenceProof` — et c'est lui
    que chaque tour de suivi doit savoir reconduire.
    """
    verdict = decider([], ask_client_max=3, missing=MissingPackage())
    answer = Answer(
        found=False, complete=False, texte="Ma lecture du contrat s'est arrêtée avant de conclure.",
        lecture_partielle=LecturePartielle(nodes_read=1, blocks_read=4, documents=["cg"]),
        unknown=["Je n'ai pas pu lire tout ce qui pouvait concerner ce sinistre."],
        verdict=verdict, faits_compris=QuestionScope(bien="canapé", cause="bougie"))
    return initialiser(
        doc_id="cg", source_hash="source", ingest_fingerprint="ingest",
        pipeline_digest="pipeline", prompts_digest="prompts", request_id="r-0",
        faits=Faits(description="Une bougie est tombée sur le canapé.", montant_eur=1200),
        answer=answer, decision_claims=[])


def test_un_suivi_sur_une_lecture_partielle_reste_servable() -> None:
    """P1 : le porteur est reconduit **avec** ce qu'il annonçait manquer, sinon le fil casse.

    Le suivi ne relit rien : la borne du premier tour vaut encore à chaque tour. Vider `unknown[]`
    en gardant le porteur faisait lever le domaine — et `run_followup` convertissait ce
    `ValidationError` en `InvalidRequest`, donc un 400 portant un message de validation interne à
    tous les tours du fil.
    """
    state = _state_lecture_partielle()
    assert state.answer.lecture_partielle is not None
    question = next(q for q in state.questions if q.status == "active")
    updated = appliquer(
        state, ConversationAction(question_id=question.question_id, value="non"),
        request_id="r-1", ask_client_max=3)
    # L'invariant du domaine tient : un porteur, et un seul, avec sa lacune.
    assert updated.answer.found is False and updated.answer.reason is None
    assert updated.answer.lecture_partielle == state.answer.lecture_partielle
    assert updated.answer.unknown == state.answer.unknown
    assert updated.answer.verdict is not None
    # Et le fil reste ouvert : un second tour ne casse pas davantage.
    encore = next(q for q in updated.questions if q.status == "active")
    troisieme = appliquer(
        updated, ConversationAction(question_id=encore.question_id, value="oui"),
        request_id="r-2", ask_client_max=3)
    assert troisieme.answer.lecture_partielle == state.answer.lecture_partielle
    assert troisieme.answer.unknown == state.answer.unknown


def test_un_suivi_ordinaire_ne_reconduit_aucune_lecture_partielle() -> None:
    """Le pendant : un premier tour muni de sa preuve d'absence garde ses propres réserves."""
    state = _state()
    question = next(q for q in state.questions if q.kind == "fait" and q.claim_id)
    updated = appliquer(
        state, ConversationAction(question_id=question.question_id, value="oui"),
        request_id="r-1", ask_client_max=3)
    assert updated.answer.lecture_partielle is None
    assert updated.answer.unknown == []  # `couvert` : plus rien à réserver


def test_premier_tour_source_les_faits_et_propose_deux_a_trois_questions() -> None:
    state = _state()
    assert {(f.key, f.source) for f in state.facts} >= {
        ("description", "declaration_initiale"), ("montant_eur", "declaration_initiale"),
        ("bien", "extraction"), ("cause", "extraction"),
    }
    assert 2 <= len([q for q in state.questions if q.status == "active"]) <= 3
    assert all(q.question_id for q in state.questions)


def test_reponse_ciblee_recalcule_ad6_sans_modifier_l_historique() -> None:
    state = _state()
    question = next(q for q in state.questions if q.kind == "fait" and q.claim_id)
    updated = appliquer(
        state, ConversationAction(question_id=question.question_id, value="oui"),
        request_id="r-1", ask_client_max=3)
    assert state.answer.verdict.value == "ne_tranche_pas"
    assert updated.answer.verdict.value == "couvert"
    assert updated.history[-1].changed is True
    assert updated.facts[-1].question_id == question.question_id
    assert updated.answer.texte.startswith("Verdict recalculé : couvert.")
    assert updated.answer.segments == [
        updated.answer.segments[0].model_copy()]  # une seule restitution déterministe
    assert updated.answer.segments[0].kind == "limite"


def test_ne_sait_pas_reste_inconnu_et_le_rejeu_est_deterministe() -> None:
    state = _state()
    question = next(q for q in state.questions if q.kind == "fait" and q.claim_id)
    action = ConversationAction(question_id=question.question_id, value="ne sait pas")
    a = appliquer(state, action, request_id="stable", ask_client_max=3)
    b = appliquer(state, action, request_id="stable", ask_client_max=3)
    assert a == b
    assert a.facts[-1].value == "inconnu"
    assert a.answer.verdict == state.answer.verdict


def test_question_inconnue_perimee_et_double_clic_sont_refuses() -> None:
    state = _state()
    with pytest.raises(ValueError, match="inconnue ou périmée"):
        appliquer(state, ConversationAction(question_id="absente", value="oui"),
                  request_id="r-1", ask_client_max=3)
    question = state.questions[0]
    updated = appliquer(state, ConversationAction(question_id=question.question_id, value="oui"),
                        request_id="r-1", ask_client_max=3)
    with pytest.raises(ValueError, match="déjà reçu"):
        appliquer(updated, ConversationAction(question_id=question.question_id, value="oui"),
                  request_id="r-2", ask_client_max=3)


def test_contradiction_conserve_les_deux_versions_jusqu_au_choix() -> None:
    state = _state()
    # La question porte volontairement sur un fait déjà actif avec une valeur différente.
    question = next(q for q in state.questions if q.kind == "fait")
    question.fact_key = "cause"
    before = state.answer.verdict
    conflicted = appliquer(
        state, ConversationAction(question_id=question.question_id, value="court-circuit"),
        request_id="r-1", ask_client_max=3)
    conflict = conflicted.conflicts[-1]
    assert conflict.status == "ouvert"
    assert conflicted.answer.verdict == before
    assert {f.value for f in conflicted.facts if f.event_id in conflict.event_ids} == {
        "bougie", "court-circuit"}
    resolved = appliquer(
        conflicted, ConversationAction(
            action="resolution", conflict_id=conflict.conflict_id,
            chosen_event_id=conflict.event_ids[0]),
        request_id="r-2", ask_client_max=3)
    assert resolved.conflicts[-1].status == "resolu"


def test_correction_est_append_only_et_le_jeton_altere_est_refuse() -> None:
    state = _state()
    original = next(f for f in state.facts if f.key == "cause")
    corrected = appliquer(
        state, ConversationAction(
            action="correction", fact_key="cause", value="plaque de cuisson",
            replaces_event_id=original.event_id),
        request_id="r-1", ask_client_max=3)
    assert original in corrected.facts
    assert corrected.facts[-1].replaces_event_id == original.event_id
    secret = b"x" * 32
    token = signer(corrected, secret)
    assert verifier(token, secret) == corrected
    altered = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(InvalidRequest, match="altéré|illisible"):
        verifier(altered, secret)


@pytest.mark.parametrize("texte", ["non, pas du tout", "ce n'était pas subit", "jamais"])
def test_les_negations_libres_restent_negatives(texte: str) -> None:
    state = _state()
    question = next(q for q in state.questions if q.kind == "fait" and q.claim_id)
    updated = appliquer(state, ConversationAction(question_id=question.question_id, value=texte),
                        request_id="r-neg", ask_client_max=3)
    assert classifier_reponse(texte).value == "negative"
    assert updated.decision_claims[0].champs is not None
    assert updated.decision_claims[0].champs.fait_requis_present is False
    assert updated.answer.verdict.value != "couvert"


def test_un_texte_ambigu_est_un_fait_declare_sans_resserrer_ad6() -> None:
    state = _state()
    question = next(q for q in state.questions if q.kind == "fait" and q.claim_id)
    updated = appliquer(
        state, ConversationAction(question_id=question.question_id,
                                  value="La chaleur a marqué le tissu."),
        request_id="r-ambigu", ask_client_max=3)
    assert classifier_reponse(updated.facts[-1].value).value == "ambiguous"
    assert updated.answer.verdict == state.answer.verdict


@pytest.mark.parametrize(("description", "attendu"), [
    ("Le devis atteint 1 200,50 euros.", "1 200,50 euros"),
    ("Dommage estimé à 850 €", "850 €"),
    ("Il y a 2 meubles endommagés.", None),
])
def test_le_montant_libre_est_extrait_seulement_avec_sa_devise(
        description: str, attendu: str | None) -> None:
    assert montant_de_description(description) == attendu


def test_une_claim_multi_exigences_conserve_un_non_anterieur() -> None:
    claim = _claim().model_copy(deep=True)
    assert claim.champs is not None
    claim.champs.fait_manquant = None
    claim.champs.qualites_exigees = ["subit", "accidentel"]
    claim.champs.qualites_non_etablies = ["subit", "accidentel"]
    state = _state()
    verdict = decider([claim], ask_client_max=3, missing=MissingPackage(
        conditions_particulieres=False, options_souscrites=False, avenants=False, date_effet=False))
    state = initialiser(
        doc_id="cg", source_hash="source", ingest_fingerprint="ingest", pipeline_digest="pipeline",
        prompts_digest="prompts", request_id="r-0", faits=Faits(description="dommage"),
        answer=state.answer.model_copy(update={"verdict": verdict}), decision_claims=[claim])
    first, second = [q for q in state.questions if q.claim_id == claim.claim_id and q.kind == "fait"]
    state = appliquer(state, ConversationAction(question_id=first.question_id, value="non"),
                      request_id="r-1", ask_client_max=3)
    state = appliquer(state, ConversationAction(question_id=second.question_id, value="oui"),
                      request_id="r-2", ask_client_max=3)
    assert state.decision_claims[0].champs is not None
    assert state.decision_claims[0].champs.fait_requis_present is False
    assert state.answer.verdict.value != "couvert"


def test_contradiction_initiale_independante_ne_bloque_pas_un_fait_decisif() -> None:
    state = _state()
    answer = state.answer.model_copy(update={
        "faits_compris": QuestionScope(lieu="cuisine", bien="canapé")})
    state = initialiser(
        doc_id="cg", source_hash="source", ingest_fingerprint="ingest", pipeline_digest="pipeline",
        prompts_digest="prompts", request_id="r-0",
        faits=Faits(description="dommage", lieu="salon"), answer=answer,
        decision_claims=[_claim()])
    assert len(state.conflicts) == 1 and state.conflicts[0].key == "lieu"
    question = next(q for q in state.questions if q.status == "active")
    updated = appliquer(state, ConversationAction(question_id=question.question_id, value="oui"),
                        request_id="r-1", ask_client_max=3)
    assert updated.answer.verdict.value == "couvert"


def test_conflit_sur_un_fait_decisif_exclut_ses_versions_du_recalcul() -> None:
    state = _state()
    question = next(q for q in state.questions if q.kind == "fait" and q.claim_id)
    state.facts.append(FactEvent(
        event_id="version-oui", key=question.fact_key, value="oui", source="reponse_client",
        turn=0, question_id=question.question_id))
    conflicted = appliquer(
        state, ConversationAction(question_id=question.question_id, value="non"),
        request_id="r-conflit", ask_client_max=3)
    assert conflicted.conflicts[-1].status == "ouvert"
    assert conflicted.answer.verdict.value == "ne_tranche_pas"


def test_les_questions_tournent_et_une_question_perimee_est_refusee() -> None:
    claim = _claim().model_copy(deep=True)
    assert claim.champs is not None
    claim.champs.fait_manquant = None
    claim.champs.qualites_non_etablies = [f"qualité {n}" for n in range(5)]
    claim.champs.qualites_exigees = list(claim.champs.qualites_non_etablies)
    base = _state()
    verdict = decider([claim], ask_client_max=3, missing=MissingPackage(
        conditions_particulieres=False, options_souscrites=False, avenants=False, date_effet=False))
    state = initialiser(
        doc_id="cg", source_hash="s", ingest_fingerprint="i", pipeline_digest="p",
        prompts_digest="q", request_id="r0", faits=Faits(description="d"),
        answer=base.answer.model_copy(update={"verdict": verdict}), decision_claims=[claim])
    assert len([q for q in state.questions if q.status == "active"]) == 3
    first = next(q for q in state.questions if q.status == "active")
    updated = appliquer(state, ConversationAction(question_id=first.question_id, value="inconnu"),
                        request_id="r1", ask_client_max=3)
    assert len([q for q in updated.questions if q.status == "active"]) == 3
    assert any(q.status == "active" and q.question_id not in {x.question_id for x in state.questions[:3]}
               for q in updated.questions)
    with pytest.raises(ValueError, match="déjà reçu"):
        appliquer(updated, ConversationAction(question_id=first.question_id, value="oui"),
                  request_id="r2", ask_client_max=3)


def test_option_et_conditions_particulieres_mettront_a_jour_le_paquet() -> None:
    claim = _claim().model_copy(deep=True)
    assert claim.champs is not None
    claim.champs.option_requise = True
    claim.champs.cp_requise = True
    state = _state()
    verdict = decider([claim], ask_client_max=5, missing=MissingPackage())
    state = initialiser(
        doc_id="cg", source_hash="s", ingest_fingerprint="i", pipeline_digest="p",
        prompts_digest="q", request_id="r0", faits=Faits(description="d"),
        answer=state.answer.model_copy(update={"verdict": verdict}), decision_claims=[claim])
    option = next(q for q in state.questions if q.kind == "option")
    if option.status != "active":
        pytest.fail("la question option doit faire partie des trois premières cibles")
    state = appliquer(state, ConversationAction(question_id=option.question_id, value="oui"),
                      request_id="r1", ask_client_max=5)
    assert state.missing.options_souscrites is False
    cp = next(q for q in state.questions if q.kind == "conditions_particulieres")
    assert cp.status == "active"
    state = appliquer(state, ConversationAction(question_id=cp.question_id, value="oui"),
                      request_id="r2", ask_client_max=5)
    assert state.missing.conditions_particulieres is False


def test_aucune_question_visible_n_est_decisionnellement_morte() -> None:
    state = _state()
    assert all(q.claim_id is not None or q.kind in {
        "option", "conditions_particulieres", "avenant_date"}
               for q in state.questions if q.status == "active")
    question = next(q for q in state.questions if q.status == "active")
    updated = appliquer(state, ConversationAction(
        question_id=question.question_id, value="inconnu"), request_id="r-doc", ask_client_max=3)
    assert updated.history[-1].causal_events
    assert updated.history[-1].changed is False


def test_reponse_negative_avenant_date_rouvre_explicitement_les_claims() -> None:
    claim = _claim()
    base = _state()
    verdict = decider([claim], ask_client_max=8, missing=MissingPackage())
    state = initialiser(
        doc_id="cg", source_hash="s", ingest_fingerprint="i", pipeline_digest="p",
        prompts_digest="q", request_id="r0", faits=Faits(description="d"),
        answer=base.answer.model_copy(update={"verdict": verdict}), decision_claims=[claim])
    avenant = next(q for q in state.questions if q.kind == "avenant_date")
    while avenant.status != "active":
        first = next(q for q in state.questions if q.status == "active")
        state = appliquer(state, ConversationAction(
            question_id=first.question_id, value="inconnu"), request_id="r0", ask_client_max=8)
        avenant = next(q for q in state.questions if q.kind == "avenant_date")
    assert avenant.status == "active"
    updated = appliquer(state, ConversationAction(
        question_id=avenant.question_id, value="non"), request_id="r-non", ask_client_max=8)
    assert updated.missing.avenants is True and updated.missing.date_effet is True
    assert updated.decision_claims[0].champs is not None
    assert updated.decision_claims[0].champs.fait_manquant == "date d'effet et avenants applicables"


def test_correction_liee_ne_touche_que_la_claim_concernee() -> None:
    first = _claim().model_copy(deep=True)
    second = _claim().model_copy(deep=True)
    first.claim_id = "claim-subit"
    second.claim_id = "claim-accidentel"
    assert first.champs is not None and second.champs is not None
    first.champs.fait_manquant = "subit"
    first.champs.qualites_exigees = ["subit"]
    first.champs.qualites_non_etablies = ["subit"]
    second.champs.fait_manquant = "accidentel"
    second.champs.qualites_exigees = ["accidentel"]
    second.champs.qualites_non_etablies = ["accidentel"]
    base = _state()
    verdict = decider([first, second], ask_client_max=6, missing=MissingPackage(
        conditions_particulieres=False, options_souscrites=False, avenants=False, date_effet=False))
    state = initialiser(
        doc_id="cg", source_hash="s", ingest_fingerprint="i", pipeline_digest="p",
        prompts_digest="q", request_id="r0", faits=Faits(description="d"),
        answer=base.answer.model_copy(update={"verdict": verdict}), decision_claims=[first, second])
    question = next(q for q in state.questions if q.claim_id == first.claim_id)
    state = appliquer(state, ConversationAction(question_id=question.question_id, value="oui"),
                      request_id="r1", ask_client_max=6)
    event = next(e for e in state.facts if e.question_id == question.question_id)
    state = appliquer(state, ConversationAction(
        action="correction", fact_key=event.key, value="non", replaces_event_id=event.event_id),
        request_id="r2", ask_client_max=6)
    by_id = {claim.claim_id: claim for claim in state.decision_claims}
    assert by_id[first.claim_id].champs is not None and by_id[second.claim_id].champs is not None
    assert by_id[first.claim_id].champs.fait_requis_present is False
    assert by_id[second.claim_id].champs.fait_manquant == "accidentel"


def test_correction_identique_remplacee_et_en_conflit() -> None:
    state = _state()
    original = next(f for f in state.facts if f.key == "cause")
    identical = appliquer(state, ConversationAction(
        action="correction", fact_key="cause", value=original.value,
        replaces_event_id=original.event_id), request_id="same", ask_client_max=3)
    assert identical == state
    corrected = appliquer(state, ConversationAction(
        action="correction", fact_key="cause", value="plaque", replaces_event_id=original.event_id),
        request_id="r1", ask_client_max=3)
    assert appliquer(corrected, ConversationAction(
        action="correction", fact_key="cause", value="plaque", replaces_event_id=original.event_id),
        request_id="idem", ask_client_max=3) == corrected
    with pytest.raises(ValueError, match="déjà été remplacé"):
        appliquer(corrected, ConversationAction(
            action="correction", fact_key="cause", value="four", replaces_event_id=original.event_id),
            request_id="r2", ask_client_max=3)
    conflict_state = _state()
    question = next(q for q in conflict_state.questions if q.kind == "fait")
    question.fact_key = "cause"
    conflict_state = appliquer(conflict_state, ConversationAction(
        question_id=question.question_id, value="court-circuit"), request_id="r1", ask_client_max=3)
    with pytest.raises(ValueError, match="conflit ouvert"):
        appliquer(conflict_state, ConversationAction(
            action="correction", fact_key="cause", value="four",
            replaces_event_id=original.event_id), request_id="r2", ask_client_max=3)


def test_resolution_de_trois_versions_ne_duplique_pas_la_gagnante() -> None:
    state = _state()
    extra = FactEvent(event_id="cause-extra", key="cause", value="plaque", source="extraction", turn=0)
    state.facts.append(extra)
    question = next(q for q in state.questions if q.kind == "fait")
    question.fact_key = "cause"
    conflicted = appliquer(state, ConversationAction(
        question_id=question.question_id, value="court-circuit"), request_id="r1", ask_client_max=3)
    conflict = conflicted.conflicts[-1]
    assert len(conflict.event_ids) == 3
    chosen = conflict.event_ids[1]
    resolved = appliquer(conflicted, ConversationAction(
        action="resolution", conflict_id=conflict.conflict_id, chosen_event_id=chosen),
        request_id="r2", ask_client_max=3)
    assert len(resolved.facts) == len(conflicted.facts)
    assert resolved.conflicts[-1].chosen_event_id == chosen


def test_tour_20_est_refuse_avant_de_produire_un_tour_21() -> None:
    state = ContinuationState.model_validate({**_state().model_dump(mode="python"), "turn": 20})
    question = next(q for q in state.questions if q.status == "active")
    with pytest.raises(ValueError, match="limite de 20 tours"):
        appliquer(state, ConversationAction(question_id=question.question_id, value="oui"),
                  request_id="r21", ask_client_max=3)


def test_jeton_refuse_base64_non_canonique_et_fact_key_reste_adressable() -> None:
    state = _state()
    token = signer(state, b"s" * 32)
    encoded, signature = token.split(".")
    with pytest.raises(InvalidRequest, match="invalide"):
        verifier(f"{encoded}=.{signature}", b"s" * 32)
    claim = _claim().model_copy(deep=True)
    assert claim.champs is not None
    claim.champs.fait_manquant = "x" * (FACT_KEY_MAX + 100)
    verdict = decider([claim], ask_client_max=3)
    bounded = initialiser(
        doc_id="cg", source_hash="s", ingest_fingerprint="i", pipeline_digest="p",
        prompts_digest="q", request_id="r", faits=Faits(description="d"),
        answer=state.answer.model_copy(update={"verdict": verdict}), decision_claims=[claim])
    assert max(len(q.fact_key) for q in bounded.questions) <= FACT_KEY_MAX


def test_api_suivi_valide_avant_cout_et_trace_les_cinq_etapes() -> None:
    app = create_app(_settings(env="dev", allow_ungated=True))
    with TestClient(app) as client:
        corpus, index = _mini_corpus()
        double = Double((_reponse(corpus), _trace()))
        app.state.foyer.corpus = corpus
        app.state.foyer.index = index
        app.state.foyer.pipeline_sinistre = double
        first = client.post(
            "/api/v1/sinistre", json=_corps(), headers={**XFF, "X-Sinistre-Conversation": "1"})
        assert first.status_code == 200
        conversation = first.json()["conversation"]
        question = next(q for q in conversation["questions"] if q["status"] == "active")
        followup = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
            "doc_id": "cg-mini", "token": conversation["token"],
            "question_id": question["question_id"], "value": "inconnu",
        })
        assert followup.status_code == 200
        body = followup.json()
        assert body["trace"]["total_cost_eur"] == 0
        assert [step["name"] for step in body["trace"]["steps"]] == [
            "comprendre", "retrouver", "rediger", "verifier", "restituer"]
        assert len(double.appels) == 1, "le suivi ne rappelle jamais le pipeline LLM/retrieval"

        altered = conversation["token"][:-1] + ("A" if conversation["token"][-1] != "A" else "B")
        rejected = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
            "doc_id": "cg-mini", "token": altered,
            "question_id": question["question_id"], "value": "oui",
        })
        assert rejected.status_code == 400
        assert len(double.appels) == 1


def test_api_un_fil_ouvert_sur_une_lecture_partielle_ne_casse_a_aucun_tour() -> None:
    """P1, sur la surface HTTP : le défaut se voyait au second appel, pas au premier.

    Le premier tour rendait bien 200 avec son jeton et ses questions actives — le fil s'ouvrait
    normalement. C'est `/suivi` qui ressortait en 400 avec le texte d'une `ValidationError` pydantic
    dans le corps, à chaque tour : tout gestionnaire dont l'analyse initiale tombait sur une lecture
    bornée héritait d'un fil ouvert et inutilisable.
    """
    app = create_app(_settings(env="dev", allow_ungated=True))
    with TestClient(app) as client:
        corpus, index = _mini_corpus()
        answer = Answer(
            found=False, complete=False,
            texte="Ma lecture du contrat s'est arrêtée avant de conclure.",
            lecture_partielle=LecturePartielle(nodes_read=1, blocks_read=4,
                                               documents=["cg-mini"]),
            unknown=["Je n'ai pas pu lire tout ce qui pouvait concerner ce sinistre."],
            verdict=decider([], ask_client_max=3, missing=MissingPackage()),
            faits_compris=QuestionScope(bien="canapé"))
        app.state.foyer.corpus = corpus
        app.state.foyer.index = index
        app.state.foyer.pipeline_sinistre = Double((answer, _trace()))

        first = client.post(
            "/api/v1/sinistre", json=_corps(), headers={**XFF, "X-Sinistre-Conversation": "1"})
        assert first.status_code == 200
        assert first.json()["answer"]["lecture_partielle"] == {
            "nodes_read": 1, "blocks_read": 4, "documents": ["cg-mini"]}
        conversation = first.json()["conversation"]
        question = next(q for q in conversation["questions"] if q["status"] == "active")

        followup = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
            "doc_id": "cg-mini", "token": conversation["token"],
            "question_id": question["question_id"], "value": "non",
        })

        assert followup.status_code == 200, followup.text
        corps = followup.json()
        # Le porteur est reconduit avec sa réserve : le suivi n'a rien relu, la borne vaut encore.
        assert corps["answer"]["lecture_partielle"] == {
            "nodes_read": 1, "blocks_read": 4, "documents": ["cg-mini"]}
        assert corps["answer"]["reason"] is None and corps["answer"]["unknown"]
        assert corps["answer"]["verdict"]["value"] in {
            "couvert", "non_couvert", "sous_conditions", "ne_tranche_pas"}
        # Et le tour suivant non plus ne casse pas : le fil est réellement utilisable.
        suivante = next(q for q in corps["conversation"]["questions"] if q["status"] == "active")
        second = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
            "doc_id": "cg-mini", "token": corps["conversation"]["token"],
            "question_id": suivante["question_id"], "value": "oui",
        })
        assert second.status_code == 200, second.text
        assert second.json()["answer"]["lecture_partielle"] is not None


def test_api_le_journal_du_suivi_nomme_letat_partiel_a_chaque_tour(
        caplog: pytest.LogCaptureFixture) -> None:
    """I3 : le chemin frère de `/chat` et `/sinistre` — la même règle, sur `/sinistre/suivi`.

    Le tour de suivi **reconduit** le porteur : la cause typée vaut donc encore à chaque tour. Ne la
    journaliser qu'au premier appel faisait disparaître `lecture_tronquee` de l'analytique au
    changement de route, alors que l'état, lui, n'avait pas changé — AD-10 fait pourtant de ces
    champs le support de « l'analytique des échecs ».
    """
    import json as _json
    import logging

    app = create_app(_settings(env="dev", allow_ungated=True))
    with TestClient(app) as client:
        corpus, index = _mini_corpus()
        answer = Answer(
            found=False, complete=False,
            texte="Ma lecture du contrat s'est arrêtée avant de conclure.",
            lecture_partielle=LecturePartielle(nodes_read=1, blocks_read=4,
                                               documents=["cg-mini"]),
            unknown=["Je n'ai pas pu lire tout ce qui pouvait concerner ce sinistre."],
            verdict=decider([], ask_client_max=3, missing=MissingPackage()),
            faits_compris=QuestionScope(bien="canapé"))
        app.state.foyer.corpus = corpus
        app.state.foyer.index = index
        app.state.foyer.pipeline_sinistre = Double((answer, _trace()))

        with caplog.at_level(logging.INFO, logger="foyer.request"):
            first = client.post("/api/v1/sinistre", json=_corps(),
                                headers={**XFF, "X-Sinistre-Conversation": "1"})
            assert first.status_code == 200
            corps = first.json()
            # **Deux** tours de suivi, pas un : c'est au second que l'omission se voyait le moins.
            for _ in range(2):
                question = next(q for q in corps["conversation"]["questions"]
                                if q["status"] == "active")
                reponse = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
                    "doc_id": "cg-mini", "token": corps["conversation"]["token"],
                    "question_id": question["question_id"], "value": "non",
                })
                assert reponse.status_code == 200, reponse.text
                corps = reponse.json()
                assert corps["answer"]["lecture_partielle"] is not None

    lignes = [_json.loads(r.message) for r in caplog.records if r.name == "foyer.request"]
    initial = [l for l in lignes if l.get("path") == "/api/v1/sinistre"]
    suivis = [l for l in lignes if l.get("path") == "/api/v1/sinistre/suivi"]
    assert len(initial) == 1 and len(suivis) == 2
    # Le même vocabulaire à chaque tour, initial compris : l'état n'a pas changé de nom en changeant
    # de route.
    for ligne in [*initial, *suivis]:
        assert ligne["found"] is False, ligne
        assert ligne["reason_kind"] == "lecture_tronquee", ligne
        assert ligne["variants_count"] is None and ligne["blocks_scanned"] is None, ligne
    # Deux tours **distincts** ont bien été journalisés : la ligne n'est pas la même relue deux fois.
    assert len({l["request_id"] for l in suivis}) == 2
    # `conversation_turn` n'y figure pas, et ce n'est pas l'objet de ce test : AD-10 clôt la liste
    # des champs logués (`request_id.CHAMPS_DE_LOG`), et il n'y est pas — comportement antérieur.


def test_api_refuse_un_digest_perime_avant_le_pipeline() -> None:
    app = create_app(_settings(env="dev", allow_ungated=True))
    with TestClient(app) as client:
        corpus, index = _mini_corpus()
        double = Double((_reponse(corpus), _trace()))
        app.state.foyer.corpus = corpus
        app.state.foyer.index = index
        app.state.foyer.pipeline_sinistre = double
        first = client.post(
            "/api/v1/sinistre", json=_corps(), headers={**XFF, "X-Sinistre-Conversation": "1"})
        conversation = first.json()["conversation"]
        app.state.foyer.pipeline_digest_hex = "nouvelle-version"
        question = conversation["questions"][0]
        rejected = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
            "doc_id": "cg-mini", "token": conversation["token"],
            "question_id": question["question_id"], "value": "oui",
        })
        assert rejected.status_code == 400
        assert len(double.appels) == 1


def test_deux_instances_derivent_le_meme_secret_de_production() -> None:
    a = Settings(_env_file=None, anthropic_api_key="cle-partagee", env="dev", allow_ungated=True)
    b = Settings(_env_file=None, anthropic_api_key="cle-partagee", env="dev", allow_ungated=True)
    assert _conversation_secret(a) == _conversation_secret(b)
    assert _conversation_secret(a) != b"cle-partagee"


@pytest.mark.parametrize("perime", ["autre_doc", "source", "ingest", "pipeline", "prompts", "non_servi"])
def test_api_refuse_tout_etat_signe_mais_perime_avant_tout_suivi(perime: str) -> None:
    app = create_app(_settings(env="dev", allow_ungated=True))
    with TestClient(app) as client:
        corpus, index = _mini_corpus()
        double = Double((_reponse(corpus), _trace()))
        app.state.foyer.corpus = corpus
        app.state.foyer.index = index
        app.state.foyer.pipeline_sinistre = double
        first = client.post(
            "/api/v1/sinistre", json=_corps(), headers={**XFF, "X-Sinistre-Conversation": "1"})
        conversation = first.json()["conversation"]
        doc_id = "autre-document" if perime == "autre_doc" else "cg-mini"
        if perime == "source":
            corpus.manifest["cg-mini"].source_hash = "source-modifiee"
        elif perime == "ingest":
            corpus.manifest["cg-mini"].ingest_fingerprint = "ingest-modifie"
        elif perime == "pipeline":
            app.state.foyer.pipeline_digest_hex = "pipeline-modifie"
        elif perime == "prompts":
            app.state.foyer.prompts_digest_hex = "prompts-modifies"
        elif perime == "non_servi":
            corpus.documents.pop("cg-mini")
        question = next(q for q in conversation["questions"] if q["status"] == "active")
        rejected = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
            "doc_id": doc_id, "token": conversation["token"],
            "question_id": question["question_id"], "value": "oui",
        })
        assert rejected.status_code == 400
        assert len(double.appels) == 1


def test_le_quota_suivi_est_borne_et_independant_du_quota_payant() -> None:
    settings = _settings(
        env="dev", allow_ungated=True, rate_limit_per_minute=2, rate_limit_per_day=10,
        conversation_rate_limit_per_minute=2, conversation_rate_limit_per_day=10)
    app = create_app(settings)
    with TestClient(app) as client:
        corpus, index = _mini_corpus()
        double = Double((_reponse(corpus), _trace()))
        app.state.foyer.corpus = corpus
        app.state.foyer.index = index
        app.state.foyer.pipeline_sinistre = double
        for _ in range(2):
            assert client.post("/api/v1/sinistre/suivi", headers=XFF, json={}).status_code == 400
        limited = client.post("/api/v1/sinistre/suivi", headers=XFF, json={})
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"
        assert int(limited.headers["Retry-After"]) >= 1
        paid = client.post("/api/v1/sinistre", json=_corps(), headers=XFF)
        assert paid.status_code == 200 and len(double.appels) == 1


def test_le_quota_payant_epuise_ne_bloque_pas_le_compteur_suivi() -> None:
    settings = _settings(
        env="dev", allow_ungated=True, rate_limit_per_minute=2, rate_limit_per_day=10,
        conversation_rate_limit_per_minute=2, conversation_rate_limit_per_day=10)
    app = create_app(settings)
    with TestClient(app) as client:
        for _ in range(2):
            assert client.post("/api/v1/sinistre", headers=XFF, json={}).status_code == 400
        assert client.post("/api/v1/sinistre", headers=XFF, json={}).status_code == 429
        followup = client.post("/api/v1/sinistre/suivi", headers=XFF, json={})
        assert followup.status_code == 400
        assert followup.json()["error"]["code"] == "invalid_request"


def test_suivi_rattache_sa_trace_si_la_relecture_des_clauses_echoue(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import server.app.api.routes.sinistre as route_sinistre

    app = create_app(_settings(env="dev", allow_ungated=True))
    with TestClient(app) as client:
        corpus, index = _mini_corpus()
        double = Double((_reponse(corpus), _trace()))
        app.state.foyer.corpus = corpus
        app.state.foyer.index = index
        app.state.foyer.pipeline_sinistre = double
        first = client.post(
            "/api/v1/sinistre", json=_corps(), headers={**XFF, "X-Sinistre-Conversation": "1"})
        conversation = first.json()["conversation"]
        question = next(q for q in conversation["questions"] if q["status"] == "active")

        def incoherente(*_args, **_kwargs):
            raise PipelineError(ErrorCode.internal, "preuve de test")

        monkeypatch.setattr(route_sinistre, "clauses_de", incoherente)
        failed = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
            "doc_id": "cg-mini", "token": conversation["token"],
            "question_id": question["question_id"], "value": "inconnu",
        })
        assert failed.status_code == 500
        body = failed.json()
        assert body["trace"]["total_cost_eur"] == 0
        assert body["trace"]["request_id"] == body["error"]["request_id"]


def test_api_suivi_restitue_un_answer_coherent_et_preserve_ses_clauses() -> None:
    app = create_app(_settings(env="dev", allow_ungated=True))
    with TestClient(app) as client:
        corpus, index = _mini_corpus()
        double = Double((_reponse(corpus), _trace()))
        app.state.foyer.corpus = corpus
        app.state.foyer.index = index
        app.state.foyer.pipeline_sinistre = double
        first = client.post(
            "/api/v1/sinistre", json=_corps(), headers={**XFF, "X-Sinistre-Conversation": "1"})
        conversation = first.json()["conversation"]
        question = next(q for q in conversation["questions"] if q["status"] == "active")
        followup = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
            "doc_id": "cg-mini", "token": conversation["token"],
            "question_id": question["question_id"], "value": "oui",
        })
        assert followup.status_code == 200
        body = followup.json()
        verdict = body["answer"]["verdict"]["value"].replace("_", " ")
        assert f"Verdict recalculé : {verdict}." in body["answer"]["texte"]
        assert all(claim["text"] == "Clause vérifiée conservée pour le recalcul du verdict."
                   for claim in body["answer"]["claims"])
        assert body["sources"], "les clauses vérifiées restent disponibles à la copie et à l'UX"


def test_seuils_conversation_alignent_settings_api_domaine_et_trace() -> None:
    settings = _settings(
        env="dev", allow_ungated=True, conversation_max_turns=1,
        conversation_active_questions_max=2)
    assert settings.thresholds()["conversation_max_turns"] == 1
    assert settings.thresholds()["conversation_active_questions_max"] == 2
    claim = _claim().model_copy(deep=True)
    assert claim.champs is not None
    claim.champs.fait_manquant = None
    claim.champs.qualites_non_etablies = [f"qualité {n}" for n in range(4)]
    claim.champs.qualites_exigees = list(claim.champs.qualites_non_etablies)
    base = _state()
    domain_state = initialiser_domaine(
        doc_id="cg", source_hash="s", ingest_fingerprint="i", pipeline_digest="p",
        prompts_digest="q", request_id="r", faits=Faits(description="d"),
        answer=base.answer.model_copy(update={
            "verdict": decider([claim], ask_client_max=8, missing=MissingPackage())}),
        decision_claims=[claim], active_questions_max=settings.conversation_active_questions_max)
    assert len([q for q in domain_state.questions if q.status == "active"]) == 2
    app = create_app(settings)
    with TestClient(app) as client:
        corpus, index = _mini_corpus()
        double = Double((_reponse(corpus), _trace()))
        app.state.foyer.corpus = corpus
        app.state.foyer.index = index
        app.state.foyer.pipeline_sinistre = double
        first = client.post(
            "/api/v1/sinistre", json=_corps(), headers={**XFF, "X-Sinistre-Conversation": "1"})
        conversation = first.json()["conversation"]
        assert 0 < len([q for q in conversation["questions"] if q["status"] == "active"]) <= 2
        question = next(q for q in conversation["questions"] if q["status"] == "active")
        turn_one = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
            "doc_id": "cg-mini", "token": conversation["token"],
            "question_id": question["question_id"], "value": "inconnu",
        })
        assert turn_one.status_code == 200
        assert turn_one.json()["trace"]["thresholds"]["conversation_max_turns"] == 1
        refused = client.post("/api/v1/sinistre/suivi", headers=XFF, json={
            "doc_id": "cg-mini", "token": turn_one.json()["conversation"]["token"],
            "question_id": question["question_id"], "value": "inconnu",
        })
        assert refused.status_code == 400
        assert "limite de 1 tours" in refused.json()["error"]["message"]


def test_les_cp_produites_levent_la_condition_ecrite_en_tete_de_section() -> None:
    """Story 5.7 (L1e) : le plafond de section doit pouvoir se refermer, sinon le fil tourne en rond.

    Le premier tour plafonne le verdict à `sous_conditions` parce que le contrat n'ouvre la section de
    la garantie que si les conditions particulières la mentionnent, et qu'à J+1 personne ne les lit.
    C'est précisément la pièce que le fil réclame : quand le gestionnaire répond qu'elle la mentionne,
    la condition est levée et la table retrouve son chemin ordinaire.
    """
    condition = ConditionDeSection(
        block_id="cg:p1:9", titre="3.1.4 Dégâts des eaux",
        texte="Les présentes conditions spéciales sont applicables si les conditions particulières "
              "mentionnent que la garantie “ dégâts des eaux ” est souscrite.",
        renvoie_cp=True)
    claim = _claim().model_copy(deep=True)
    assert claim.champs is not None
    claim.champs.fait_requis_present = True
    claim.champs.fait_manquant = None
    claim.champs.qualites_exigees = []
    claim.champs.qualites_non_etablies = []
    claim.clauses[0].qualificatifs = []
    claim.clauses[0].condition_section = condition
    verdict = decider([claim], ask_client_max=5, missing=MissingPackage(
        conditions_particulieres=True, options_souscrites=False, avenants=False, date_effet=False))
    assert verdict.value == "sous_conditions" and "cg:p1:9" in verdict.reason
    state = initialiser(
        doc_id="cg", source_hash="s", ingest_fingerprint="i", pipeline_digest="p",
        prompts_digest="q", request_id="r0", faits=Faits(description="d"),
        answer=_state().answer.model_copy(update={"verdict": verdict}), decision_claims=[claim])
    cp = next(q for q in state.questions if q.kind == "conditions_particulieres")
    state = appliquer(state, ConversationAction(question_id=cp.question_id, value="oui"),
                      request_id="r1", ask_client_max=5)
    assert state.answer.verdict is not None
    assert state.answer.verdict.value == "couvert", state.answer.verdict.reason
