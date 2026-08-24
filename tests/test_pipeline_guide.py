"""Matrice I/O de `pipelines/guide.py` (spec 1.5), LLM mocké : les cinq étapes, les court-circuits,
la relance unique d'AD-3 et la trace d'AD-10.

`FakeAnthropic` lève sur tout appel non scripté : la **longueur du script est une assertion**. C'est
ainsi que « aucun appel `reason` pour un refus » (AD-5) se vérifie sans compter les appels à la main.
"""

from __future__ import annotations

import json

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.document import Document, Node
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.errors import CorpusUnavailable, InvalidRequest, Timeout
from server.app.domain.ingest import ManifestEntry
from server.app.domain.profil import Profil
from server.app.domain.question import Turn
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines.guide import repondre_guide
from tests.llm_fake import FakeAnthropic, fake_message

DOC_ID = "mini"
ARRIVEE = ("Vous disposez de huit jours après votre arrivée pour vous déclarer au Biergercenter de "
           "votre commune de résidence.")
ECOLE = ("L'inscription à l'école se fait auprès de la commune, avant le 1er septembre, sur "
         "présentation du certificat de résidence.")
Q_ARRIVEE = "huit jours après votre arrivée pour vous déclarer"
Q_ECOLE = "L'inscription à l'école se fait auprès de la commune"


@pytest.fixture(scope="module")
def index() -> Index:
    blocs = [
        {"block_id": f"{DOC_ID}:f1:1", "loc": "f1", "seq": 1, "kind": "heading", "text": "Déclarer son arrivée"},
        {"block_id": f"{DOC_ID}:f1:2", "loc": "f1", "seq": 2, "kind": "para", "text": ARRIVEE},
        {"block_id": f"{DOC_ID}:f2:1", "loc": "f2", "seq": 1, "kind": "para", "text": ECOLE},
        {"block_id": f"{DOC_ID}:f3:1", "loc": "f3", "seq": 1, "kind": "para",
         "text": "Le mobilier de jardin relève du contenu de l'habitation."},
    ]
    doc = Document(doc_id=DOC_ID, kind="guide", title="Mini guide", edition="git:test",
                   nodes=[Node(node_id=f"{DOC_ID}:f1", title="Arrivée",
                               items=[{"block_id": f"{DOC_ID}:f1:1"}, {"block_id": f"{DOC_ID}:f1:2"}]),
                          Node(node_id=f"{DOC_ID}:f2", title="École", items=[{"block_id": f"{DOC_ID}:f2:1"}]),
                          Node(node_id=f"{DOC_ID}:f3", title="Divers", items=[{"block_id": f"{DOC_ID}:f3:1"}]),
                          Node(node_id=f"{DOC_ID}:root", title="Mini",
                               items=[{"node_id": f"{DOC_ID}:f1"}, {"node_id": f"{DOC_ID}:f2"},
                                      {"node_id": f"{DOC_ID}:f3"}])],
                   blocks=blocs)
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    manifest = {DOC_ID: ManifestEntry(status="servi", source_hash="sha-source", ingest_fingerprint="fp-1",
                                      document_hash="sha-doc", edition="git:test")}
    return Index(Corpus(documents={DOC_ID: doc}, manifest=manifest,
                        summaries={DOC_ID: "# Mini guide\n- f1 Déclarer son arrivée\n- f2 École"}))


def _settings(**kw) -> Settings:
    kw.setdefault("guide_doc_id", DOC_ID)
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget(deadline_s: float = 30.0) -> RequestBudget:
    return RequestBudget(deadline_s=deadline_s, max_attempts=6, max_cost_eur=0.10)


def _comprendre(intent: str = "question", *, terms: list[str] | None = None,
                clarification: str | None = None, language: str = "fr") -> dict:
    resolue = None if clarification else "Quel délai pour déclarer mon arrivée ?"
    return fake_message(model=TIERS["micro"], text=json.dumps({
        "intent": intent, "question_resolue": resolue, "clarification": clarification,
        "language": language, "terms": terms if terms is not None else ["arrivée", "école"],
        "themes": [], "bien": None, "evenement": None, "lieu": None, "cause": None, "moment": None}))


def _rediger(*claims: tuple[str, str, list[tuple[str, str]]], transition: bool = False) -> dict:
    segments = [{"text": f"Segment {cid}.", "kind": "factuel", "claim_ids": [cid]} for cid, _, _ in claims]
    if transition:
        segments.append({"text": "En résumé.", "kind": "transition", "claim_ids": []})
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": segments,
        "claims": [{"claim_id": cid, "text": texte,
                    "quotes": [{"block_id": b, "quote": q} for b, q in quotes]}
                   for cid, texte, quotes in claims]}))


def _verdicts(*paires: tuple[str, bool]) -> dict:
    return fake_message(model=TIERS["micro"], text=json.dumps(
        {"verdicts": [{"claim_id": c, "pertinente": p} for c, p in paires]}))


async def _run(index: Index, script: list, *, historique: list[Turn] | None = None,
               settings: Settings | None = None, budget: RequestBudget | None = None,
               question: str = "Quel délai pour déclarer mon arrivée ?", lang: str | None = None):
    settings = settings or _settings()
    fake = FakeAnthropic(script)
    client = LlmClient(settings, anthropic_client=fake)
    answer, trace = await repondre_guide(question, historique or [], Profil(), corpus=index.corpus,
                                         index=index, client=client, settings=settings,
                                         request_id="req-test", lang=lang, budget=budget or _budget())
    return answer, trace, fake


BONNE = ("c1", "Le délai est de huit jours.", [(f"{DOC_ID}:f1:2", Q_ARRIVEE)])
BONNE_2 = ("c2", "L'inscription passe par la commune.", [(f"{DOC_ID}:f2:1", Q_ECOLE)])
MAUVAISE = ("c3", "Le délai est de quinze jours.", [(f"{DOC_ID}:f1:2", "quinze jours après votre arrivée")])
AUTRE_ECOLE = ("c2", "L'inscription se fait avant le 1er septembre.",
               [(f"{DOC_ID}:f2:1", "avant le 1er septembre, sur présentation du certificat")])


# --- nominal -----------------------------------------------------------------
async def test_a_sourced_answer_runs_the_five_steps_and_carries_its_trace(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, BONNE_2, transition=True),
                                             _verdicts(("c1", True), ("c2", True))])
    assert fake.remaining_script == 0 and len(fake.requests) == 3  # micro, reason, micro — pas un de plus
    assert answer.found is True and answer.reason is None
    assert answer.texte == "Segment c1. Segment c2. En résumé."
    assert [c.claim_id for c in answer.claims] == ["c1", "c2"]
    assert all(c.status.retrouvee and c.status.pertinente for c in answer.claims)
    assert all(c.status.edition == "git:test" for c in answer.claims)

    # AD-1 : la chaîne est fixe et complète, dans cet ordre
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier", "restituer"]
    assert [s.tier for s in trace.steps] == ["micro", "reason", "reason", "micro", None]
    assert trace.steps[1].calls == []  # *retrouver* déterministe n'appelle aucun modèle
    assert trace.pipeline == "guide" and trace.variant == "deterministe" and trace.request_id == "req-test"
    assert trace.source_hash == {DOC_ID: "sha-source"} and trace.ingest_fingerprint == {DOC_ID: "fp-1"}
    assert trace.pipeline_digest and trace.prompts_digest
    assert trace.thresholds["quote_min_chars"] == 25 and trace.retries == 0
    assert trace.total_cost_eur > 0 and trace.deadline_remaining_s is not None


async def test_the_trace_never_carries_the_text_of_a_block(index: Index) -> None:
    """AD-10 : « la trace ne contient jamais le texte des blocs »."""
    _answer, trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE),
                                               _verdicts(("c1", True))])
    serialise = trace.model_dump_json()
    for bloc in index.corpus.documents[DOC_ID].blocks:
        assert bloc.text not in serialise
    assert Q_ARRIVEE not in serialise  # ni le texte du bloc, ni la citation qui en est tirée
    assert f"{DOC_ID}:f1:2" in serialise  # les identifiants, eux, y sont (AD-10 : blocs ouverts)


async def test_the_pipeline_fills_the_retrieval_budget_from_the_settings(index: Index) -> None:
    """Reprise 1.4 : `max_blocks`/`max_tokens` existaient sans que personne ne les renseigne."""
    settings = _settings(retrieval_max_blocks=1)
    # La borne mord au point que le bloc cité par BONNE n'est plus transmis : la claim est donc
    # rejetée (« bloc non fourni »), la relance rejoue la même ébauche et le refus est motivé.
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE), _rediger(BONNE)],
                                     settings=settings)
    assert fake.remaining_script == 0
    retrouver = trace.steps[1]
    assert len(retrouver.opened_block_ids) == 1  # sans la borne, plusieurs blocs partaient
    assert trace.truncations == 1
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "claims_rejetes"


# --- court-circuits d'AD-5 ---------------------------------------------------
async def test_an_out_of_scope_intent_never_reaches_the_reason_tier(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre("meteo")])
    assert fake.remaining_script == 0 and len(fake.requests) == 1  # le seul appel est `micro`
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "hors_perimetre" and answer.reason.terms_searched == []
    assert answer.texte and answer.segments[0].kind == "limite"


async def test_a_clarification_short_circuits_and_is_carried_by_the_answer(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre(clarification="De quelles personnes parlez-vous ?")])
    assert fake.remaining_script == 0 and [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.clarification == "De quelles personnes parlez-vous ?"
    assert answer.reason is not None and answer.reason.kind == "clarification_requise"


async def test_an_empty_retrieval_never_pays_for_a_reason_call(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre(terms=["hippopotame"])])
    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "restituer"]
    assert answer.found is False and answer.reason is not None
    # l'`AbsenceProof` dit ce qui a été cherché, jamais que l'information n'existe pas (AD-1/AD-4)
    assert answer.reason.kind == "zero_hit" and answer.reason.terms_searched == ["hippopotame"]
    assert answer.reason.blocks_scanned == 4 and answer.reason.documents == [DOC_ID]
    assert answer.reason.variants_count == 0  # aucun dictionnaire validé (AD-5, story 2.1)


# --- relance unique (AD-3) ---------------------------------------------------
async def test_a_rejected_claim_triggers_exactly_one_retry_that_can_save_the_answer(index: Index) -> None:
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)),
        _rediger(BONNE, BONNE_2), _verdicts(("c1", True), ("c2", True))])
    assert fake.remaining_script == 0
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "rediger", "verifier", "restituer"]
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1", "c2"]
    assert trace.retries == 1
    # la claim rejetée au premier tour n'est plus dans la réponse : c'est le second draft qui compte
    assert answer.rejected_claims == []


async def test_an_identical_retry_stops_the_loop(index: Index) -> None:
    """AD-3 : « chaque relance change quelque chose (un draft identique arrête la relance) »."""
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(MAUVAISE), _rediger(MAUVAISE)])
    assert fake.remaining_script == 0  # aucune seconde vérification : elle rendrait le même résultat
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "rediger", "restituer"]
    assert [c.name for c in trace.steps[4].checks] == ["relance_sans_effet"]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "claims_rejetes"
    assert [c.claim_id for c in answer.rejected_claims] == ["c3"]


async def test_no_surviving_claim_after_the_retry_is_a_motivated_refusal(index: Index) -> None:
    autre_mauvaise = ("c4", "Le délai est de trente jours.",
                      [(f"{DOC_ID}:f1:2", "trente jours après votre arrivée")])
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(MAUVAISE), _rediger(autre_mauvaise)])
    assert fake.remaining_script == 0
    assert answer.found is False and answer.claims == []
    assert answer.reason is not None and answer.reason.kind == "claims_rejetes"
    assert answer.reason.terms_searched == ["arrivée", "école"]
    # exactement **une** relance : deux appels `reason`, pas trois
    assert sum(1 for s in trace.steps if s.name == "rediger") == 2 and trace.retries == 1
    assert [c.claim_id for c in answer.rejected_claims] == ["c4"]


async def test_the_retry_carries_the_motive_composed_by_our_code(index: Index) -> None:
    _answer, _trace, fake = await _run(index, [_comprendre(), _rediger(MAUVAISE), _rediger(MAUVAISE)])
    relance = fake.requests[2]["messages"][0]["content"]
    assert '<untrusted kind="motif">' in relance  # AD-15 : le motif est délimité comme tout le reste
    assert "introuvable dans le bloc mini:f1:2" in relance
    premier = fake.requests[1]["messages"][0]["content"]
    assert '<untrusted kind="motif">' not in premier
    # le préfixe système est byte-identique entre l'appel et sa relance (AD-9, cache)
    assert fake.requests[1]["system"] == fake.requests[2]["system"]


# --- bornes d'entrée ---------------------------------------------------------
async def test_a_history_beyond_the_bound_is_refused_never_truncated(index: Index) -> None:
    historique = [Turn(role="user", texte=f"tour {i}") for i in range(7)]
    with pytest.raises(InvalidRequest, match="historique de 7 tours"):
        await _run(index, [], historique=historique)  # script vide : rien n'a été facturé
    ok = [Turn(role="user", texte=f"tour {i}") for i in range(6)]
    answer, _trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE), _verdicts(("c1", True))],
                                       historique=ok)
    assert answer.found is True


async def test_an_exhausted_deadline_stops_before_the_first_call(index: Index) -> None:
    with pytest.raises(Timeout, match="comprendre"):
        await _run(index, [], budget=_budget(deadline_s=0.0))


async def test_the_forced_language_travels_to_the_answer(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [_comprendre(language="fr"), _rediger(BONNE),
                                               _verdicts(("c1", True))], lang="en")
    assert answer.lang == "en" and answer.lang_fallback is False


# --- quand la relance n'a pas lieu (AD-3 lu, NFR4 tenu) ----------------------
async def test_a_standing_answer_never_pays_a_second_reason_call_for_a_relevance_rejection(
        index: Index) -> None:
    """AD-3 motive la relance par des défauts de **citation** ; une claim écartée par le seul jugement
    de pertinence est déjà « conservée dans rejected_claims[] »."""
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, BONNE_2),
                                             _verdicts(("c1", True), ("c2", False))])
    assert fake.remaining_script == 0  # aucun second appel `reason` : le script vide l'atteste
    assert sum(1 for s in trace.steps if s.name == "rediger") == 1 and trace.retries == 0
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert [c.claim_id for c in answer.rejected_claims] == ["c2"]
    assert answer.texte == "Segment c1."


async def test_a_relevance_rejection_that_leaves_nothing_does_trigger_the_retry(index: Index) -> None:
    """Rien n'a survécu : la relance est le seul chemin vers une réponse (AD-3, « après cette relance »)."""
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE), _verdicts(("c1", False)),
                                             _rediger(BONNE_2), _verdicts(("c2", True))])
    assert fake.remaining_script == 0 and trace.retries == 1
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c2"]


async def test_the_relevance_retry_can_be_switched_on_by_configuration(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, BONNE_2),
                                             _verdicts(("c1", True), ("c2", False)),
                                             _rediger(BONNE, AUTRE_ECOLE),
                                             _verdicts(("c1", True), ("c2", True))],
                                     settings=_settings(relance_sur_non_pertinence=True))
    assert fake.remaining_script == 0 and trace.retries == 1
    assert [c.claim_id for c in answer.claims] == ["c1", "c2"]


async def test_an_unaffordable_retry_serves_the_verified_answer_instead_of_a_503(index: Index) -> None:
    """NFR4 : la relance est une tentative d'amélioration. Refusée faute de budget, elle ne doit pas
    emporter une réponse déjà vérifiée — et la trace le dit."""
    budget = RequestBudget(deadline_s=30.0, max_attempts=3, max_cost_eur=0.10)  # 3 appels, pas 4
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True))], budget=budget)
    assert fake.remaining_script == 0 and trace.retries == 0
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert [c.claim_id for c in answer.rejected_claims] == ["c3"]
    verifier = next(s for s in trace.steps if s.name == "verifier")
    (check,) = [c for c in verifier.checks if c.name == "relance_abandonnee"]
    assert check.ok is False and "budget_exceeded" in check.detail


async def test_a_retry_without_margin_never_starts_and_keeps_the_answer(index: Index) -> None:
    """AD-1 : « aucun retry ne démarre sans marge » — le retry ne démarre pas, la requête garde sa réponse."""
    budget = RequestBudget(deadline_s=3.0, max_attempts=6, max_cost_eur=0.10)  # < llm_retry_margin_s
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True))], budget=budget)
    assert fake.remaining_script == 0 and answer.found is True
    verifier = next(s for s in trace.steps if s.name == "verifier")
    assert [c.name for c in verifier.checks if not c.ok] == ["citations", "relance_abandonnee"]
    assert "timeout" in verifier.checks[-1].detail


async def test_an_unaffordable_retry_with_nothing_verified_is_a_refusal_not_an_error(index: Index) -> None:
    budget = RequestBudget(deadline_s=30.0, max_attempts=2, max_cost_eur=0.10)  # comprendre + rediger
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(MAUVAISE)], budget=budget)
    assert fake.remaining_script == 0
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "claims_rejetes"  # un `Answer` complet (200), jamais une 503
    verifier = next(s for s in trace.steps if s.name == "verifier")
    assert "aucune affirmation n'avait survécu" in verifier.checks[-1].detail


# --- correctifs de revue 1.5 -------------------------------------------------
@pytest.mark.parametrize("intent", ["meteo", "bavardage", "hors_perimetre"])
async def test_every_out_of_scope_intent_short_circuits(index: Index, intent: str) -> None:
    """AD-5 : « l'étage `reason` n'est jamais atteint pour un refus » — pour les trois intents."""
    answer, trace, fake = await _run(index, [_comprendre(intent)])
    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "hors_perimetre"


async def test_a_retry_that_parses_badly_never_takes_the_verified_answer_with_it(index: Index) -> None:
    """Même surface d'échec que le budget, même traitement : la première vérification fait foi."""
    casse = fake_message(model=TIERS["reason"], text="{ pas du json")
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True)), casse, casse])
    assert fake.remaining_script == 0  # les deux tentatives du client (appel + relance motivée)
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    verifier_step = next(s for s in trace.steps if s.name == "verifier")
    (check,) = [c for c in verifier_step.checks if c.name == "relance_abandonnee"]
    assert "llm_parse" in check.detail


async def test_a_retry_that_verifies_worse_never_replaces_the_answer(index: Index) -> None:
    """AD-3 relance pour améliorer : une seconde ébauche moins bonne ne jette pas l'acquis."""
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True)), _rediger(MAUVAISE)])
    assert fake.remaining_script == 0  # la seconde vérification n'a rien à juger : aucun appel micro
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    verifier_2 = [s for s in trace.steps if s.name == "verifier"][-1]
    (check,) = [c for c in verifier_2.checks if c.name == "relance_moins_bonne"]
    assert "0 affirmation(s) contre 1" in check.detail


async def test_an_abandoned_retry_forbids_declaring_the_answer_complete(index: Index) -> None:
    """AD-4 : `complete=True` exige « aucune troncature de budget » — une relance refusée en est une."""
    settings = _settings(retrieval_max_blocks=2)  # pas de troncature « naturelle » ici
    budget = RequestBudget(deadline_s=30.0, max_attempts=3, max_cost_eur=0.10)
    answer, _trace, _fake = await _run(index, [_comprendre(terms=["arrivée"]),
                                               _rediger(BONNE, MAUVAISE), _verdicts(("c1", True))],
                                       settings=settings, budget=budget)
    assert answer.found is True and answer.complete is False


async def test_an_unserved_document_is_refused_before_any_paid_call(index: Index) -> None:
    with pytest.raises(CorpusUnavailable, match="non servi"):
        await _run(index, [], settings=_settings(guide_doc_id="mini-inexistant"))


async def test_without_a_budget_the_pipeline_asks_the_client_for_one(index: Index) -> None:
    """Chemin de la story 1.6 : l'API appelle `repondre_guide` sans budget."""
    settings = _settings()
    fake = FakeAnthropic([_comprendre(), _rediger(BONNE), _verdicts(("c1", True))])
    client = LlmClient(settings, anthropic_client=fake)
    answer, trace = await repondre_guide("Quel délai ?", [], Profil(), corpus=index.corpus, index=index,
                                         client=client, settings=settings, request_id="sans-budget")
    assert answer.found is True and trace.deadline_remaining_s is not None
    # le budget vient des seuils actifs : la deadline restante ne dépasse jamais `deadline_s`
    assert 0 < trace.deadline_remaining_s <= settings.deadline_s
    budget = client.new_budget()
    assert (budget.deadline_s, budget.max_attempts, budget.max_cost_eur) == (
        settings.deadline_s, settings.max_llm_attempts, settings.max_cost_eur_per_request)


async def test_the_trace_digests_are_the_real_ones_and_can_be_supplied(index: Index) -> None:
    _answer, trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE), _verdicts(("c1", True))])
    assert trace.pipeline_digest == pipeline_digest() != prompts_digest()
    assert trace.prompts_digest == prompts_digest()

    settings = _settings()
    fake = FakeAnthropic([_comprendre(), _rediger(BONNE), _verdicts(("c1", True))])
    _a, fournis = await repondre_guide(
        "Quel délai ?", [], Profil(), corpus=index.corpus, index=index,
        client=LlmClient(settings, anthropic_client=fake), settings=settings, request_id="digests",
        budget=_budget(), pipeline_digest_hex="pipe-de-l-image", prompts_digest_hex="prompts-de-l-image")
    # story 1.6 : l'API les calcule au démarrage et les passe ; ils sont repris tels quels
    assert (fournis.pipeline_digest, fournis.prompts_digest) == ("pipe-de-l-image", "prompts-de-l-image")


async def test_trace_retries_counts_the_client_parse_retries_too(index: Index) -> None:
    """AD-10 ne définit pas `retries` : le pipeline le fait, et le test l'épingle."""
    casse = fake_message(model=TIERS["reason"], text="{ pas du json")
    _answer, trace, fake = await _run(index, [_comprendre(), casse, _rediger(BONNE),
                                              _verdicts(("c1", True))])
    assert fake.remaining_script == 0
    rediger_step = next(s for s in trace.steps if s.name == "rediger")
    assert [c.name for c in rediger_step.checks] == ["parse_retry"]
    assert trace.retries == 1  # la relance motivée du client, sans relance d'AD-3
