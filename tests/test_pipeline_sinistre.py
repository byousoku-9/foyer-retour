"""Matrice I/O de `pipelines/sinistre.py` (spec 1.8), LLM mocké : les cinq étapes, le verdict d'AD-6,
la relance unique d'AD-3, les bornes d'entrée et la trace d'AD-10.

Comme pour le guide, `FakeAnthropic` lève sur tout appel non scripté : la **longueur du script est
une assertion**. C'est ainsi que « *vérifier* n'a fait qu'**un** appel `micro` » (AD-9 amendé) se
vérifie sans compter les appels à la main.
"""

from __future__ import annotations

import json

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.document import Document, Node
from server.app.domain.errors import CorpusUnavailable, InvalidRequest
from server.app.domain.ingest import ManifestEntry
from server.app.domain.question import Faits
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines import sinistre
from tests.llm_fake import FakeAnthropic, fake_message

DOC_ID = "cg"
GARANTIE = ("Les dégâts occasionnés au mobilier assuré et au bâtiment désigné par un événement "
            "soudain, résultant de l'action subite de la chaleur, sont couverts.")
EXCLUSION = ("Pour les extensions mentionnées aux points 2.1 et 2.2, les dégâts occasionnés au "
             "bâtiment par l'action subite de la chaleur sont exclus.")
EXCLUSION_SOCLE = "Sont exclus les dommages que l'Assuré a causés intentionnellement."
CONDITION = "La garantie n'est acquise que si le bien est occupé de manière permanente."
DEFINITION = "Le contenu comprend le mobilier de jardin et les objets confiés à un Assuré."

Q_GARANTIE = "dégâts occasionnés au mobilier assuré et au bâtiment désigné par un événement soudain"
Q_EXCLUSION = "dégâts occasionnés au bâtiment par l'action subite de la chaleur sont exclus"
Q_EXCLUSION_SOCLE = "exclus les dommages que l'Assuré a causés intentionnellement"
Q_CONDITION = "n'est acquise que si le bien est occupé de manière permanente"
Q_DEFINITION = "contenu comprend le mobilier de jardin et les objets confiés"

QUESTION = "Ce sinistre est-il couvert par le contrat ?"
FAITS = Faits(date="2026-08-01", lieu="salon du domicile", montant_eur=1200.0,
              description="Une bougie posée sur une table a brûlé le mobilier de salon, "
                          "sans embrasement ni commencement d'incendie.")


@pytest.fixture(scope="module")
def index() -> Index:
    """Mini-contrat à l'image du contrat AXA : un socle `commun`, une branche `extension`."""
    blocs = [
        {"block_id": f"{DOC_ID}:p1:1", "loc": "p1", "seq": 1, "kind": "heading", "text": "Incendie"},
        {"block_id": f"{DOC_ID}:p1:2", "loc": "p1", "seq": 2, "kind": "garantie", "text": GARANTIE,
         "kind_source": "manual", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p1:3", "loc": "p1", "seq": 3, "kind": "condition", "text": CONDITION,
         "kind_source": "manual", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p1:4", "loc": "p1", "seq": 4, "kind": "definition", "text": DEFINITION,
         "kind_source": "manual", "defines": "contenu", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p1:5", "loc": "p1", "seq": 5, "kind": "exclusion",
         "text": EXCLUSION_SOCLE, "kind_source": "manual", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p2:1", "loc": "p2", "seq": 1, "kind": "exclusion", "text": EXCLUSION,
         "kind_source": "manual", "scope_node_id": f"{DOC_ID}:ext"},
    ]
    doc = Document(
        doc_id=DOC_ID, kind="contrat", title="Mini contrat", edition="juin 2017",
        nodes=[Node(node_id=f"{DOC_ID}:socle", level=1, title="Socle commun",
                    items=[{"block_id": f"{DOC_ID}:p1:1"}, {"block_id": f"{DOC_ID}:p1:2"},
                           {"block_id": f"{DOC_ID}:p1:3"}, {"block_id": f"{DOC_ID}:p1:4"},
                           {"block_id": f"{DOC_ID}:p1:5"}]),
               Node(node_id=f"{DOC_ID}:ext", level=1, title="Extensions", scope={"kind": "extension"},
                    items=[{"block_id": f"{DOC_ID}:p2:1"}]),
               Node(node_id=f"{DOC_ID}:root", level=0, title="Contrat",
                    items=[{"node_id": f"{DOC_ID}:socle"}, {"node_id": f"{DOC_ID}:ext"}])],
        blocks=blocs)
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    manifest = {DOC_ID: ManifestEntry(status="servi", source_hash="sha-source", ingest_fingerprint="fp-1",
                                      document_hash="sha-doc", edition="juin 2017")}
    return Index(Corpus(documents={DOC_ID: doc}, manifest=manifest,
                        summaries={DOC_ID: "# Mini contrat\n- socle Socle commun\n- ext Extensions"}))


def _settings(**kw) -> Settings:
    kw.setdefault("sinistre_doc_id", DOC_ID)
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget(deadline_s: float = 30.0) -> RequestBudget:
    return RequestBudget(deadline_s=deadline_s, max_attempts=6, max_cost_eur=0.10)


def _comprendre(intent: str = "question", *, terms: list[str] | None = None,
                clarification: str | None = None) -> dict:
    resolue = None if clarification else "Le mobilier de salon brûlé par une bougie est-il couvert ?"
    return fake_message(model=TIERS["micro"], text=json.dumps({
        "intent": intent, "question_resolue": resolue, "clarification": clarification,
        "language": "fr", "terms": terms if terms is not None else ["mobilier", "chaleur", "contenu"],
        "themes": [], "facettes": ["couverture du sinistre"], "bien": "mobilier de salon",
        "evenement": "incendie sans embrasement", "lieu": "domicile", "cause": "bougie",
        "moment": "2026-08-01"}))


def _rediger(*claims: tuple[str, str, list[tuple[str, str]]]) -> dict:
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": [{"text": f"Clause {cid}.", "kind": "factuel", "claim_ids": [cid]}
                     for cid, _, _ in claims],
        "claims": [{"claim_id": cid, "text": texte,
                    "quotes": [{"block_id": b, "quote": q} for b, q in quotes]}
                   for cid, texte, quotes in claims]}))


def _verifier(*entrees: tuple[str, bool, bool, bool, bool, str | None], nb_segments: int = 8) -> dict:
    """`(claim_id, pertinente, fait_requis_present, option_requise, cp_requise, fait_manquant)`."""
    return fake_message(model=TIERS["micro"], text=json.dumps({
        "verdicts": [{"claim_id": c, "pertinente": p} for c, p, *_ in entrees],
        "facettes": [{"facette": 0, "claim_ids": [c for c, p, *_ in entrees if p]}],
        "segments": [{"segment": i, "soutenu": True} for i in range(nb_segments)],
        "applicabilite": [{"claim_id": c, "fait_requis_present": present, "option_requise": option,
                           "cp_requise": cp, "fait_manquant": manquant}
                          for c, _p, present, option, cp, manquant in entrees]}))


async def _run(index: Index, script: list, *, settings: Settings | None = None,
               budget: RequestBudget | None = None, faits=FAITS, doc_id: str | None = None,
               variant: str = "deterministe"):
    settings = settings or _settings()
    fake = FakeAnthropic(script)
    client = LlmClient(settings, anthropic_client=fake)
    answer, trace = await sinistre.run(doc_id, QUESTION, faits, corpus=index.corpus, index=index,
                                       client=client, settings=settings, request_id="req-sinistre",
                                       variant=variant, budget=budget or _budget())
    return answer, trace, fake


GAR = ("c1", "Les dégâts au mobilier par action subite de la chaleur sont couverts.",
       [(f"{DOC_ID}:p1:2", Q_GARANTIE)])
DEF = ("c2", "Le contenu comprend le mobilier.", [(f"{DOC_ID}:p1:4", Q_DEFINITION)])
EXC_EXT = ("c3", "Les extensions excluent les dégâts au bâtiment.", [(f"{DOC_ID}:p2:1", Q_EXCLUSION)])
EXC_SOCLE = ("c4", "Le dommage intentionnel est exclu.", [(f"{DOC_ID}:p1:5", Q_EXCLUSION_SOCLE)])
COND = ("c5", "Le bien doit être occupé de manière permanente.", [(f"{DOC_ID}:p1:3", Q_CONDITION)])
MAUVAISE = ("c9", "Le sinistre est couvert à 100 %.", [(f"{DOC_ID}:p1:2", "couvert à cent pour cent")])


# --- nominal : le cas bougie -------------------------------------------------
async def test_the_candle_case_runs_the_five_steps_and_carries_its_verdict(index: Index) -> None:
    """AC : cinq étapes, `pipeline="sinistre"`, un seul appel `micro` dans *vérifier*, verdict complet."""
    answer, trace, fake = await _run(index, [
        _comprendre(),
        _rediger(GAR, DEF, EXC_EXT),
        _verifier(("c1", True, False, False, False, "caractère subit de l'action de la chaleur"),
                  ("c2", True, False, False, False, None),
                  ("c3", True, False, False, False, None))])
    assert fake.remaining_script == 0 and len(fake.requests) == 3  # micro, reason, micro
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier", "restituer"]
    assert trace.pipeline == "sinistre" and trace.variant == "deterministe"
    assert len(next(s for s in trace.steps if s.name == "verifier").calls) == 1

    assert answer.found is True
    verdict = answer.verdict
    assert verdict is not None
    # exclusion p2:1 écartée (`non` : elle vise les extensions), garantie incertaine (`humain`)
    assert verdict.value in ("sous_conditions", "ne_tranche_pas")
    statuts = {c.claim_id: c.status.applicable for c in answer.claims}
    assert statuts == {"c1": "humain", "c2": None, "c3": "non"}
    assert verdict.missing.faits == ["caractère subit de l'action de la chaleur"]
    # matrice I/O : `ask_client` cite les options / conditions particulières **et** la nature « subite »
    assert any("caractère subit" in q for q in verdict.ask_client)
    assert any("options" in q for q in verdict.ask_client)
    assert any("conditions particulières" in q for q in verdict.ask_client)
    assert verdict.reason and "conditions générales seules" in verdict.reason
    # AD-6 : le paquet manquant est toujours là, et le verdict n'est jamais une décision d'indemnisation
    assert verdict.missing.conditions_particulieres and verdict.missing.options_souscrites


async def test_every_displayed_claim_carries_a_typed_applicability(index: Index) -> None:
    """AC : chaque claim affichée porte `oui|non|humain`, ou `None` si elle ne cite aucune clause."""
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, DEF),
        _verifier(("c1", True, True, False, False, None), ("c2", True, False, False, False, None))])
    for claim in answer.claims:
        assert claim.status.applicable in ("oui", "non", "humain", None)
        assert claim.status.retrouvee is True and claim.status.pertinente is True


# --- les autres lignes de la table -------------------------------------------
async def test_an_applicable_exclusion_over_the_case_is_not_covered(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, EXC_SOCLE),
        _verifier(("c1", True, True, False, False, None), ("c4", True, True, False, False, None))])
    assert answer.verdict is not None and answer.verdict.value == "non_couvert"


async def test_a_baseline_guarantee_alone_is_covered(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR), _verifier(("c1", True, True, False, False, None))])
    assert answer.verdict is not None and answer.verdict.value == "couvert"
    assert answer.verdict.escalate == [] and answer.verdict.missing.faits == []
    # AD-6 : même un `couvert` s'accompagne du paquet manquant et des questions à poser
    assert len(answer.verdict.ask_client) == 2


async def test_an_open_condition_keeps_the_verdict_conditional(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, COND),
        _verifier(("c1", True, True, False, False, None),
                  ("c5", True, False, False, False, "occupation permanente du bien"))])
    assert answer.verdict is not None and answer.verdict.value == "sous_conditions"


async def test_a_guarantee_depending_on_an_option_is_conditional(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR), _verifier(("c1", True, True, True, False, None))])
    assert answer.verdict is not None and answer.verdict.value == "sous_conditions"
    assert any("options" in q for q in answer.verdict.ask_client)


# --- claims rejetées, relance, refus ------------------------------------------
async def test_a_rejected_claim_triggers_one_retry_then_refuses_with_a_verdict(index: Index) -> None:
    """AD-3 puis AD-16 : relance unique, puis refus motivé — **avec** `ne_tranche_pas`, jamais rien."""
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(MAUVAISE), _rediger(("c9", "Autre tentative, aussi fausse.",
                                                     [(f"{DOC_ID}:p1:2", "couvert à quatre-vingt pour cent")]))])
    assert fake.remaining_script == 0 and len(fake.requests) == 3  # comprendre, rédiger, rédiger
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "rediger", "verifier", "restituer"]
    assert answer.found is False
    assert answer.reason is not None and answer.reason.kind == "claims_rejetes"
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert answer.rejected_claims and all(c.rejection_kind == "non_retrouvee"
                                          for c in answer.rejected_claims)


async def test_a_claim_mixing_two_clauses_is_sent_back_to_the_writer(index: Index) -> None:
    """D6 : la claim ambiguë déclenche la relance, et la seconde ébauche éclatée passe."""
    melangee = ("c1", "La garantie joue sauf pour les extensions.",
                [(f"{DOC_ID}:p1:2", Q_GARANTIE), (f"{DOC_ID}:p2:1", Q_EXCLUSION)])
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(melangee), _rediger(GAR, EXC_EXT),
        _verifier(("c1", True, True, False, False, None), ("c3", True, False, False, False, None))])
    assert fake.remaining_script == 0
    # la première vérification n'a fait **aucun** appel : rien n'avait survécu au contrôle de citation
    assert [s.name for s in trace.steps].count("verifier") == 2
    assert answer.found is True and answer.verdict is not None
    assert answer.verdict.value == "couvert"  # l'exclusion des extensions ne couvre pas le cas
    motifs = [c.motif for c in answer.rejected_claims]
    assert not motifs or all("clause" in m for m in motifs)


# --- bornes d'entrée : rien de facturé -----------------------------------------
async def test_an_unknown_document_is_refused_before_any_billed_call(index: Index) -> None:
    with pytest.raises(CorpusUnavailable):
        await _run(index, [], doc_id="inconnu")


async def test_an_unknown_variant_is_refused_before_any_billed_call(index: Index) -> None:
    with pytest.raises(InvalidRequest, match="variante"):
        await _run(index, [], variant="autre")


async def test_an_oversized_description_is_rejected_never_truncated(index: Index) -> None:
    with pytest.raises(InvalidRequest, match="hors bornes"):
        await _run(index, [], faits={"description": "x" * 2001})
    # le domaine porte la même borne : le pipeline ne fait que lui donner son code d'erreur
    with pytest.raises(ValueError):
        Faits(description="x" * 2001)


# --- court-circuits d'AD-5, toujours avec un verdict ----------------------------
async def test_an_out_of_scope_request_is_refused_after_one_micro_call(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre("hors_perimetre")])
    assert fake.remaining_script == 0 and len(fake.requests) == 1  # l'étage `reason` n'est pas atteint
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.reason is not None
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert answer.verdict.escalate  # AD-16 : le dossier repart à la main, ce n'est pas un repli


async def test_a_search_without_a_single_block_refuses_with_a_verdict(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre(terms=["zzzz"])])
    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "restituer"]
    assert answer.reason is not None and answer.reason.kind == "zero_hit"
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"


async def test_a_request_that_cannot_be_made_autonomous_still_carries_a_verdict(index: Index) -> None:
    answer, trace, _fake = await _run(index, [_comprendre(clarification="De quel bien parlez-vous ?")])
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.clarification == "De quel bien parlez-vous ?"
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"


# --- retrieval : les clauses passent devant à score égal (D7) --------------------
async def test_decisional_blocks_come_first_at_equal_score(index: Index) -> None:
    """AC : « cherche les blocs garantie|exclusion|condition|franchise candidats » — priorité, pas filtre."""
    answer, trace, _fake = await _run(index, [
        _comprendre(terms=["mobilier"]), _rediger(GAR), _verifier(("c1", True, True, False, False, None))])
    ouverts = next(s for s in trace.steps if s.name == "rediger").opened_block_ids
    assert f"{DOC_ID}:p1:2" in ouverts  # la garantie est bien transmise
    assert f"{DOC_ID}:p1:4" in ouverts  # la définition aussi : le typage ne **filtre** pas
    assert answer.found is True
