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
import anthropic
import httpx

from server.app.domain.errors import (
    BudgetExceeded,
    CorpusUnavailable,
    InvalidRequest,
    LlmParse,
    LlmUnavailable,
    Timeout,
)
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
                clarification: str | None = None, language: str = "fr",
                facettes: list[str] | None = None) -> dict:
    """*comprendre* arrête aussi le découpage de la question en facettes (AD-4, revue Codex 1.5,
    tour 3) : c'est lui le barème de `complete`, et il est fixé avant toute rédaction."""
    resolue = None if clarification else "Quel délai pour déclarer mon arrivée ?"
    return fake_message(model=TIERS["micro"], text=json.dumps({
        "intent": intent, "question_resolue": resolue, "clarification": clarification,
        "language": language, "terms": terms if terms is not None else ["arrivée", "école"],
        "themes": [], "facettes": facettes if facettes is not None else ["délai de déclaration"],
        "bien": None, "evenement": None, "lieu": None, "cause": None, "moment": None}))


def _rediger(*claims: tuple[str, str, list[tuple[str, str]]], transition: bool = False,
             limite: str | None = None) -> dict:
    segments = [{"text": f"Segment {cid}.", "kind": "factuel", "claim_ids": [cid]} for cid, _, _ in claims]
    if transition:
        segments.append({"text": "En résumé.", "kind": "transition", "claim_ids": []})
    if limite is not None:
        segments.append({"text": limite, "kind": "limite", "claim_ids": []})
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": segments,
        "claims": [{"claim_id": cid, "text": texte,
                    "quotes": [{"block_id": b, "quote": q} for b, q in quotes]}
                   for cid, texte, quotes in claims]}))


def _verdicts(*paires: tuple[str, bool], facettes: list[list[str]] | None = None,
              segments: dict[int, bool] | None = None, nb_segments: int = 8) -> dict:
    """Verdicts groupés + phrases soutenues + couverture des facettes (AD-4). `facettes[i]` = les
    `claim_id` attribués à la facette de rang `i` de la question (le découpage vient de
    *comprendre*). Par défaut une facette, couverte par les claims jugées pertinentes (la
    question-témoin des tests n'en pose qu'une), et toutes les phrases soumises jugées soutenues."""
    if facettes is None:
        facettes = [[c for c, ok in paires if ok]]
    soutiens = {i: True for i in range(nb_segments)} | (segments or {})
    return fake_message(model=TIERS["micro"], text=json.dumps(
        {"verdicts": [{"claim_id": c, "pertinente": p} for c, p in paires],
         "facettes": [{"facette": rang, "claim_ids": ids} for rang, ids in enumerate(facettes)],
         "segments": [{"segment": i, "soutenu": ok} for i, ok in sorted(soutiens.items())]}))


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
    # Story 1.9 : `faits_compris` et `verdict` sont les deux champs de l'unique `Answer` que le guide
    # ne remplit **pas**. Le profil du guide n'est pas « ce qu'on a compris d'un sinistre », et une
    # question du guide n'a pas de verdict à porter (AD-4).
    assert answer.faits_compris is None and answer.verdict is None
    # AD-10 : `intent` est le seul champ de la ligne de log que l'API ne peut pas lire sur l'`Answer` —
    # il n'existe que dans la trace, et seul le pipeline le produit.
    assert trace.intent == "question"
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


async def test_a_retry_that_could_not_be_verified_never_starts_at_all(index: Index) -> None:
    """AD-1, « aucun retry ne démarre sans marge », appliqué au **compteur d'appels** : une relance,
    c'est deux appels — rédiger, puis vérifier ce qu'elle a rendu — et AD-3 interdit de montrer un
    draft relancé mais non vérifié. Avec la place pour un seul, démarrer *rédiger* serait payer un
    appel `reason` dont rien ne pourrait sortir (NFR4). Mesuré en live (revue Codex 1.5, tour 3) :
    le plafond par défaut coupait pile entre les deux, et la question ressortait en 503."""
    budget = RequestBudget(deadline_s=30.0, max_attempts=4, max_cost_eur=0.10)  # 3 + 1 : pas les deux
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True))], budget=budget)
    assert fake.remaining_script == 0 and trace.retries == 0  # aucun appel de relance n'a été payé
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert answer.complete is False  # AD-4 : une relance empêchée est une troncature
    verifier = next(s for s in trace.steps if s.name == "verifier")
    (check,) = [c for c in verifier.checks if c.name == "relance_abandonnee"]
    assert "budget_exceeded" in check.detail and "2 requis" in check.detail


async def test_a_second_verification_that_never_starts_keeps_the_verified_answer(index: Index) -> None:
    """La ligne de partage d'AD-16 se lit sur l'appel qui **rate**, pas sur la relance entière : le
    plafond de coût atteint *après* une relance rédigée n'est pas un appel raté, c'est un appel qui
    n'a jamais démarré. Le compteur de référence est donc ré-armé après chaque appel réussi (revue
    Codex 1.5, tour 3) — sans quoi la réponse déjà vérifiée partait en 503."""
    budget = _ferme_le_budget_apres(RequestBudget(deadline_s=30.0, max_attempts=6, max_cost_eur=10.0), 4)
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True)), _rediger(BONNE, BONNE_2)],
                                     budget=budget)
    assert fake.remaining_script == 0  # la relance a bien été rédigée, la seconde vérification non
    assert trace.retries == 1 and budget.attempts == 4
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert answer.complete is False
    verifier = next(s for s in trace.steps if s.name == "verifier")
    (check,) = [c for c in verifier.checks if c.name == "relance_abandonnee"]
    assert "budget_exceeded" in check.detail


def _ferme_le_budget_apres(budget: RequestBudget, appels: int) -> RequestBudget:
    """Referme le plafond de coût dès que `appels` appels ont été envoyés (le client vérifie le coût
    **avant** d'envoyer : c'est exactement le « jamais démarré » d'AD-16)."""
    original = type(budget).note_call

    def note_call(self, *a, **kw):
        out = original(self, *a, **kw)
        if self.attempts >= appels:
            self.max_cost_eur = 0.0
        return out

    budget.note_call = note_call.__get__(budget, type(budget))
    return budget


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
    assert trace.intent == intent  # AD-10 : le log dit de quoi il s'agissait, même pour un refus
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "hors_perimetre"


async def test_a_retry_call_that_fails_is_terminal_and_carries_its_partial_trace(index: Index) -> None:
    """AD-16, littéralement : « un appel LLM en timeout, un parse invalide après 1 retry, un 429/529
    fournisseur ⇒ **503 avec trace partielle** ». Un appel **commencé** — donc facturé — qui échoue
    n'est pas une relance qui n'a pas démarré : l'avaler rendrait 200 sur une panne du fournisseur
    (revue Codex 1.5, B5)."""
    casse = fake_message(model=TIERS["reason"], text="{ pas du json")
    with pytest.raises(LlmParse) as exc:
        await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)),
                           casse, casse])  # les deux tentatives du client (appel + relance motivée)
    trace = exc.value.trace
    assert trace is not None and trace.request_id == "req-test"
    # Trace **partielle** d'un échec survenu après *comprendre* : l'intention comprise y est restée.
    assert trace.intent == "question"
    # la `StepTrace` de l'appel raté est dans la trace : son coût y compte, il ne disparaît pas
    rediger_steps = [s for s in trace.steps if s.name == "rediger"]
    assert len(rediger_steps) == 2 and len(rediger_steps[1].calls) == 2
    assert trace.total_cost_eur > 0


async def test_a_provider_failure_on_the_retry_is_terminal_too(index: Index) -> None:
    answer_script = [_comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)),
                     anthropic.APIStatusError("529", response=httpx.Response(
                         529, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)]
    with pytest.raises(LlmUnavailable) as exc:
        await _run(index, answer_script)
    assert exc.value.trace is not None and [s.name for s in exc.value.trace.steps][-1] == "rediger"


async def test_a_retry_whose_call_never_started_keeps_the_verified_answer(index: Index) -> None:
    """La contrepartie : plafond d'appels atteint, rien n'a été facturé, la réponse acquise reste due
    (AD-1 « aucun retry ne démarre sans marge », étendu aux euros par AD-4)."""
    budget = RequestBudget(deadline_s=30.0, max_attempts=3, max_cost_eur=0.10)
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True))], budget=budget)
    assert fake.remaining_script == 0 and answer.found is True
    rediger_steps = [s for s in trace.steps if s.name == "rediger"]
    assert len(rediger_steps) == 1  # l'étape avortée n'a rien appelé : elle n'entre pas dans la trace
    verifier = next(s for s in trace.steps if s.name == "verifier")
    assert [c.name for c in verifier.checks if c.name == "relance_abandonnee"]


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


async def test_a_retrieval_emptied_by_the_budget_never_becomes_a_proof_of_absence(index: Index) -> None:
    """AD-1 / NFR2 : « budget épuisé ou troncature non résolue ⇒ `complete=False` et **aucune absence
    du corpus n'est affirmée** ». Un `zero_hit` produit par notre propre borne serait une preuve
    d'absence fabriquée (revue Codex 1.5, B4)."""
    settings = _settings(retrieval_max_tokens=1)  # aucune unité n'entre dans le budget
    with pytest.raises(BudgetExceeded, match="aucun bloc"):
        await _run(index, [_comprendre()], settings=settings)
    # la trace partielle voyage avec l'erreur (AD-16), et *retrouver* y figure avec sa troncature
    try:
        await _run(index, [_comprendre()], settings=settings)
    except BudgetExceeded as exc:
        assert exc.trace is not None and [s.name for s in exc.trace.steps] == ["comprendre", "retrouver"]
        assert exc.trace.truncations == 1
    # sans troncature, un retrieval vide reste un refus `zero_hit` motivé : rien n'a empêché la recherche
    answer, _trace, _fake = await _run(index, [_comprendre(terms=["hippopotame"])])
    assert answer.reason is not None and answer.reason.kind == "zero_hit"


async def test_a_retry_that_ties_on_claims_but_loses_completeness_never_replaces_the_answer(
        index: Index) -> None:
    """La dominance porte sur tous les axes, pas sur le seul décompte : à nombre égal, une relance qui
    perd `complete` ou allonge `unknown` dégrade encore la réponse (revue Codex 1.5, I2)."""
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)),
        _rediger(BONNE, limite="Je ne sais rien des frontaliers."), _verdicts(("c1", True))])
    assert fake.remaining_script == 0
    # même nombre d'affirmations, mais la seconde ébauche déclare un inconnu de plus et n'est plus complète
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert answer.unknown == [] and answer.complete is True  # l'acquis, pas la relance
    verifier_2 = [s for s in trace.steps if s.name == "verifier"][-1]
    (check,) = [c for c in verifier_2.checks if c.name == "relance_moins_bonne"]
    assert "ne domine pas" in check.detail


class _BudgetQuiExpire(RequestBudget):
    """Budget dont la deadline s'épuise juste après le n-ième appel facturé (horloge factice).

    Compter les appels plutôt que les secondes rend le test déterministe : l'instant d'expiration est
    exactement l'entre-deux-étapes que l'on veut éprouver, sans dépendre de l'horloge de la machine.
    """

    def __init__(self, apres_appels: int) -> None:
        super().__init__(deadline_s=30.0, max_attempts=6, max_cost_eur=0.10)
        self._restants = apres_appels

    def note_call(self, usage) -> None:
        super().note_call(usage)
        self._restants -= 1

    def remaining(self) -> float:
        return 30.0 if self._restants > 0 else -1.0


@pytest.mark.parametrize("appels, etape", [(1, "retrouver"), (2, "verifier"), (3, "restituer")])
async def test_the_deadline_is_checked_before_every_step_including_restituer(index: Index, appels: int,
                                                                             etape: str) -> None:
    """Contrat du pipeline : « deadline vérifiée **avant chaque étape** ». *restituer* en est une —
    elle ne l'était pas, et une requête dont le temps venait d'expirer rendait une réponse normale
    (revue Codex 1.5, I3). AD-16 : « la deadline globale (AD-9) produit `timeout` »."""
    with pytest.raises(Timeout, match=etape):
        await _run(index, [_comprendre(), _rediger(BONNE), _verdicts(("c1", True))],
                   budget=_BudgetQuiExpire(appels))


async def test_the_deadline_is_checked_before_a_refusal_too(index: Index) -> None:
    """Les chemins courts appellent *restituer* comme les autres : `refuser()` contrôle aussi."""
    with pytest.raises(Timeout, match="restituer"):
        await _run(index, [_comprendre("meteo")], budget=_BudgetQuiExpire(1))


async def test_a_provider_failure_during_the_second_verification_is_terminal_too(index: Index) -> None:
    """La ligne de partage est « un appel a-t-il démarré ? », et elle se tranche sur le **budget**.

    *vérifier* ne renseignait pas `PipelineError.step` : le pipeline concluait « relance non démarrée »
    et servait la première réponse en 200 sur une panne du fournisseur survenue pendant la **seconde
    vérification** (revue Codex 1.5, tour 2, B5). AD-16 : « un 429/529 fournisseur ⇒ 503 avec trace
    partielle »."""
    panne = anthropic.APIStatusError("529", response=httpx.Response(
        529, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
    with pytest.raises(LlmUnavailable) as exc:
        await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)),
                           _rediger(BONNE, BONNE_2), panne])
    trace = exc.value.trace
    assert trace is not None and [s.name for s in trace.steps][-1] == "verifier"
    # le `StepTrace` de l'appel raté voyage avec l'erreur : son coût ne disparaît pas de la trace
    assert len([s for s in trace.steps if s.name == "verifier"]) == 2
    assert trace.total_cost_eur > 0


DEUX_FACETTES = ["délai de déclaration", "inscription à l'école"]


async def test_a_limit_written_by_the_model_never_reaches_the_answer_text(index: Index) -> None:
    """De bout en bout : « le guide ne dit rien de X » est une assertion d'absence, qu'aucun passage
    ne peut soutenir (revue Codex 1.5, tour 3, B1). Elle ne s'affiche pas dans la réponse ; elle est
    dite par `unknown[]`, qui interdit déjà `complete=True`. La seule phrase d'absence que
    l'utilisateur lise reste celle du refus, composée par le code."""
    limite = "Le guide ne dit rien des frontaliers."
    answer, _trace, fake = await _run(index, [
        _comprendre(), _rediger(BONNE, transition=True, limite=limite), _verdicts(("c1", True))])
    assert fake.remaining_script == 0
    assert answer.found is True
    assert "frontaliers" not in answer.texte
    assert [s.kind for s in answer.segments] == ["factuel", "transition"]
    assert answer.unknown == [limite] and answer.complete is False


async def test_a_facet_of_the_question_the_answer_never_covers_forbids_complete(index: Index) -> None:
    """AD-4, littéralement : « toutes les facettes de `ParsedQuestion` couvertes ». Le découpage est
    celui qu'a arrêté *comprendre*, avant tout retrieval — celui qui répond ne peut donc plus effacer
    la sous-question qu'il a manquée en ne la rendant pas (revue Codex 1.5, tour 3, B3)."""
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=DEUX_FACETTES), _rediger(BONNE),
        _verdicts(("c1", True), facettes=[["c1"], []])])
    assert fake.remaining_script == 0
    assert answer.found is True and answer.unknown == [] and answer.complete is False
    verifier = [s for s in trace.steps if s.name == "verifier"][-1]
    (check,) = [c for c in verifier.checks if c.name == "facettes_non_couvertes"]
    assert "1 facette(s) couverte(s) par une affirmation affichée sur 2 posée(s)" in check.detail
    # et le libellé de la facette, qui vient de la question, ne fuit pas dans la trace (AD-10)
    assert "école" not in json.dumps([s.model_dump(mode="json") for s in trace.steps], ensure_ascii=False)


async def test_a_question_without_any_facet_is_never_declared_complete(index: Index) -> None:
    """*comprendre* muet sur le découpage : rien ne prouve la couverture, donc `complete=False`.
    L'absence de mesure ne vaut jamais complétude (AD-4)."""
    answer, _trace, fake = await _run(index, [
        _comprendre(facettes=[]), _rediger(BONNE), _verdicts(("c1", True), facettes=[])])
    assert fake.remaining_script == 0
    assert answer.found is True and answer.complete is False


async def test_a_retry_that_covers_fewer_facets_never_replaces_the_answer(index: Index) -> None:
    """Dernier axe de la dominance : à nombre d'affirmations, `complete` et `unknown` égaux, une
    relance peut encore échanger une facette de la question contre une autre (revue Codex 1.5,
    tour 2, I2). Les facettes sont celles de la question, arrêtées une seule fois par *comprendre* :
    leurs rangs sont donc stables d'une ébauche à l'autre, là où les `claim_id` sont refaits à neuf
    par chaque appel de *rédiger*."""
    limite = "Je ne sais rien des frontaliers."
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=DEUX_FACETTES), _rediger(BONNE, BONNE_2, MAUVAISE, limite=limite),
        _verdicts(("c1", True), ("c2", True), facettes=[["c1"], ["c2"]]),
        _rediger(BONNE, BONNE_2, limite=limite),
        _verdicts(("c1", True), ("c2", True), facettes=[["c1"], []])])
    assert fake.remaining_script == 0
    assert answer.found is True and len(answer.claims) == 2 and answer.complete is False
    verifier_2 = [s for s in trace.steps if s.name == "verifier"][-1]
    (check,) = [c for c in verifier_2.checks if c.name == "relance_moins_bonne"]
    assert "1 facette(s) couverte(s) contre 2" in check.detail


async def test_a_retry_that_swaps_one_facet_for_another_never_replaces_the_answer(index: Index) -> None:
    """Compter les facettes ne suffisait pas : deux vérifications qui en couvrent chacune **une**,
    mais pas la même, ont le même compte, et la seconde était acceptée — elle échangeait une
    sous-question contre une autre (revue Codex 1.5, tour 3, I2). La dominance porte donc sur
    l'**ensemble** des rangs couverts, stables parce que le découpage vient de *comprendre*."""
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=DEUX_FACETTES), _rediger(BONNE, MAUVAISE),
        _verdicts(("c1", True), facettes=[["c1"], []]),
        _rediger(BONNE_2),
        _verdicts(("c2", True), facettes=[[], ["c2"]])])
    assert fake.remaining_script == 0
    # la première réponse est servie : la seconde couvre autant de facettes, mais pas les mêmes
    assert [c.claim_id for c in answer.claims] == ["c1"]
    verifier_2 = [s for s in trace.steps if s.name == "verifier"][-1]
    assert [c.name for c in verifier_2.checks if c.name == "relance_moins_bonne"]


async def test_a_retry_that_drops_a_cited_block_never_replaces_the_answer(index: Index) -> None:
    """Deuxième identité stable entre deux ébauches (revue Codex 1.5, tour 3, I2) : le `block_id`,
    produit par **notre** ingestion. Une relance qui répond à la même facette, en même nombre, mais
    en s'appuyant sur un autre passage, remplacerait une affirmation vérifiée par une autre sans que
    rien ne le dise. Les `claim_id` ne peuvent pas l'attraper (refaits à neuf) ; les blocs, si."""
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(BONNE, MAUVAISE),
        _verdicts(("c1", True)),
        _rediger(BONNE_2),          # même facette, même compte, **autre** bloc cité
        _verdicts(("c2", True))])
    assert fake.remaining_script == 0
    assert [c.claim_id for c in answer.claims] == ["c1"]
    assert {q.block_id for c in answer.claims for q in c.quotes} == {f"{DOC_ID}:f1:2"}
    verifier_2 = [s for s in trace.steps if s.name == "verifier"][-1]
    (check,) = [c for c in verifier_2.checks if c.name == "relance_moins_bonne"]
    assert "1 bloc(s) cité(s) contre 1" in check.detail
