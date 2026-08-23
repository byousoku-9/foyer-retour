"""Chaque modèle du domaine porte exactement les champs et enums de son AD."""

from __future__ import annotations

from typing import Literal, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from server.app.domain import answer, document, errors, question, retrieval, trace, verdict


def fields(model: type[BaseModel]) -> set[str]:
    return set(model.model_fields)


def literal_values(model: type[BaseModel], field: str) -> set[str]:
    ann = model.model_fields[field].annotation
    for arg in (ann, *get_args(ann)):
        if get_origin(arg) is Literal:
            return set(get_args(arg))
    raise AssertionError(f"{model.__name__}.{field} n'est pas un Literal")


# AD-2
def test_document_fields() -> None:
    assert fields(document.Document) == {"doc_id", "kind", "title", "edition", "lang", "nodes", "blocks", "source_url", "source_hash", "ingest_fingerprint"}
    assert literal_values(document.Document, "kind") == {"guide", "contrat"}
    assert fields(document.Node) == {"node_id", "level", "title", "items", "scope", "sources"}
    assert literal_values(document.Scope, "kind") == {"commun", "special", "extension"}
    assert fields(document.Line) == {"line_id", "text", "bbox"}


def test_block_fields_and_kinds() -> None:
    assert fields(document.Block) == {
        "block_id", "text", "text_norm", "lang", "loc", "seq", "page", "bbox", "kind", "kind_confidence",
        "kind_source", "source_field", "continues", "refs", "unresolved_refs", "defines", "scope_node_id",
        "scope_node_ids", "overrides", "relation", "lines",
    }
    assert literal_values(document.Block, "kind") == {
        "para", "heading", "table", "list", "definition", "garantie", "exclusion", "condition",
        "franchise", "renvoi", "autre",
    }
    assert fields(document.Relation) == {"exception_de", "specialise", "contredit"}


def test_block_id_identity() -> None:
    b = document.Block(block_id="axa-lu-optihome-2017:p9:3", text="x", loc="p9", seq=3)
    assert b.block_id == "axa-lu-optihome-2017:p9:3"
    document.Block(block_id="lux-guide:fecole:1", text="x", loc="fecole", seq=1)
    document.Block(block_id="lux-guide:q4:2", text="x", loc="q4", seq=2)
    with pytest.raises(ValidationError):
        document.Block(block_id="AXA:p9:3", text="x", loc="p9", seq=3)
    with pytest.raises(ValidationError):
        document.Document(doc_id="Axa_2017", kind="contrat", title="t", edition="e")
    with pytest.raises(ValidationError, match="se terminer par"):
        document.Block(block_id="d:p9:3", text="x", loc="p9", seq=4)
    with pytest.raises(ValidationError):
        document.Block(block_id="d:p1:1", text="x", loc="p1", seq=1, bbox=[1, 2, 3])
    document.Block(block_id="d:p1:1", text="x", loc="p1", seq=1, bbox=[1, 2, 3, 4])


def test_kind_source_and_confirmation() -> None:
    assert literal_values(document.Block, "kind_source") == {"manual", "model", "model_verified"}
    b = document.Block(block_id="d:p1:1", text="x", loc="p1", seq=1, kind="exclusion", kind_source="manual")
    assert b.kind_source == "manual" and b.kind_confirmed
    assert document.Block(block_id="d:p1:1", text="x", loc="p1", seq=1, kind_source="model_verified").kind_confirmed
    assert not document.Block(block_id="d:p1:1", text="x", loc="p1", seq=1, kind_source="model").kind_confirmed
    assert not document.Block(block_id="d:p1:1", text="x", loc="p1", seq=1).kind_confirmed


@pytest.mark.parametrize("model", [document.Block, document.Node, document.Document, answer.Answer, answer.Claim,
                                   verdict.Verdict, trace.Trace, question.ParsedQuestion, retrieval.RetrievalResult,
                                   errors.ErrorEnvelope])
def test_domain_models_forbid_unknown_fields(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError, match="inconnu"):
        model.model_validate({"inconnu": 1})


def _doc(nodes: list[dict], blocks: list[dict]) -> document.Document:
    return document.Document(doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)


def _blk(i: int) -> dict:
    return {"block_id": f"d:p1:{i}", "text": "x", "loc": "p1", "seq": i}


def test_document_tree_invariants() -> None:
    doc = _doc([{"node_id": "root", "items": [{"block_id": "d:p1:1"}, {"node_id": "child"}]},
                {"node_id": "child", "items": [{"block_id": "d:p1:2"}]}], [_blk(1), _blk(2)])
    assert doc.block("d:p1:2").seq == 2
    with pytest.raises(ValidationError, match="dupliqué"):
        _doc([{"node_id": "root", "items": [{"block_id": "d:p1:1"}]}], [_blk(1), _blk(1)])
    with pytest.raises(ValidationError, match="bloc inconnu"):
        _doc([{"node_id": "root", "items": [{"block_id": "d:p1:9"}]}], [])
    with pytest.raises(ValidationError, match="nœud inconnu"):
        _doc([{"node_id": "root", "items": [{"node_id": "ghost"}]}], [])
    with pytest.raises(ValidationError, match="deux nœuds"):
        _doc([{"node_id": "a", "items": [{"block_id": "d:p1:1"}]}, {"node_id": "b", "items": [{"block_id": "d:p1:1"}]}], [_blk(1)])
    with pytest.raises(ValidationError, match="orphelins"):
        _doc([{"node_id": "root"}], [_blk(1)])
    with pytest.raises(ValidationError, match="deux parents"):
        _doc([{"node_id": "a", "items": [{"node_id": "c"}]}, {"node_id": "b", "items": [{"node_id": "c"}]}, {"node_id": "c"}], [])
    with pytest.raises(ValidationError, match="cycle"):
        _doc([{"node_id": "a", "items": [{"node_id": "b"}]}, {"node_id": "b", "items": [{"node_id": "a"}]}], [])
    with pytest.raises(ValidationError, match="une seule racine|exactement un nœud racine"):
        _doc([{"node_id": "a"}, {"node_id": "b"}], [])
    with pytest.raises(ValidationError, match="ne commence pas par 'd:'"):
        _doc([{"node_id": "r", "items": [{"block_id": "x:p1:1"}]}], [{"block_id": "x:p1:1", "text": "t", "loc": "p1", "seq": 1}])


@pytest.mark.parametrize("field, value, fragment", [
    ("refs", ["d:p1:9"], "refs vise un bloc inconnu"),
    ("continues", "d:p1:9", "continues vise un bloc inconnu"),
    ("overrides", "d:p1:9", "overrides vise un bloc inconnu"),
    ("relation", {"exception_de": "d:p1:9"}, "relation.exception_de vise un bloc inconnu"),
    ("relation", {"specialise": "d:p1:9"}, "relation.specialise vise un bloc inconnu"),
    ("relation", {"contredit": "d:p1:9"}, "relation.contredit vise un bloc inconnu"),
    ("scope_node_id", "ghost", "scope_node_id vise un nœud inconnu"),
    ("scope_node_ids", ["ghost"], "scope_node_ids vise un nœud inconnu"),
    ("scope_node_ids", ["root", "root"], "scope_node_ids contient un doublon"),
])
def test_document_referential_invariants(field: str, value: object, fragment: str) -> None:
    nodes = [{"node_id": "root", "items": [{"block_id": "d:p1:1"}, {"block_id": "d:p1:2"}]}]
    ok = _doc(nodes, [_blk(1), _blk(2) | {field: {"refs": ["d:p1:1"], "continues": "d:p1:1", "overrides": "d:p1:1",
                                                   "relation": {"contredit": "d:p1:1"}, "scope_node_id": "root",
                                                   "scope_node_ids": ["root"]}[field]}])
    assert ok.block("d:p1:2")
    with pytest.raises(ValidationError, match=fragment):
        _doc(nodes, [_blk(1), _blk(2) | {field: value}])


def test_scope_nodes_explicit_list_restricts_the_subtree() -> None:
    """Amendement AD-2 (revue Codex 1.2) : « pour les extensions 3.1.8.3 à 3.1.8.6 » ne couvre ni 3.1.8.1 ni 3.1.8.8."""
    nodes = [{"node_id": "d", "items": [{"node_id": "d:a3.1.8"}]},
             {"node_id": "d:a3.1.8", "items": [{"block_id": "d:p1:1"}] + [{"node_id": f"d:a3.1.8.{i}"} for i in range(1, 9)]},
             *[{"node_id": f"d:a3.1.8.{i}", "items": [{"node_id": "d:a3.1.8.3.1"}] if i == 3 else []} for i in range(1, 9)],
             {"node_id": "d:a3.1.8.3.1"}]
    narrow = _blk(1) | {"scope_node_id": "d:a3.1.8", "scope_node_ids": [f"d:a3.1.8.{i}" for i in (3, 4, 5, 6)]}
    doc = _doc(nodes, [narrow])
    assert doc.scope_nodes("d:p1:1") == {"d:a3.1.8.3", "d:a3.1.8.4", "d:a3.1.8.5", "d:a3.1.8.6", "d:a3.1.8.3.1"}
    assert not {"d:a3.1.8", "d:a3.1.8.1", "d:a3.1.8.2", "d:a3.1.8.7", "d:a3.1.8.8"} & doc.scope_nodes("d:p1:1")
    wide = _doc(nodes, [_blk(1) | {"scope_node_id": "d:a3.1.8"}])
    assert wide.scope_nodes("d:p1:1") == {"d:a3.1.8", *(f"d:a3.1.8.{i}" for i in range(1, 9)), "d:a3.1.8.3.1"}
    assert _doc(nodes, [_blk(1)]).scope_nodes("d:p1:1") == set()
    with pytest.raises(ValidationError, match="n'est pas sous"):
        _doc(nodes, [_blk(1) | {"scope_node_id": "d:a3.1.8.3", "scope_node_ids": ["d:a3.1.8.4"]}])


def test_node_items_is_single_source_of_order() -> None:
    n = document.Node(node_id="n1", items=[document.BlockRef(block_id="d:p1:1"), document.NodeRef(node_id="n2"),
                                           document.BlockRef(block_id="d:p1:2")])
    assert n.blocks == ["d:p1:1", "d:p1:2"]
    assert n.children == ["n2"]


# AD-3 / AD-4
def test_answer_models() -> None:
    assert fields(answer.Quote) == {"block_id", "quote"}
    assert fields(answer.Claim) == {"claim_id", "text", "quotes"}
    assert fields(answer.ClaimStatus) == {"retrouvee", "pertinente", "applicable", "edition"}
    assert literal_values(answer.ClaimStatus, "applicable") == {"oui", "non", "humain"}
    assert fields(answer.AnswerSegment) == {"text", "kind", "claim_ids"}
    assert literal_values(answer.AnswerSegment, "kind") == {"factuel", "transition", "limite"}
    assert fields(answer.AnswerDraft) == {"segments", "claims"}
    assert fields(answer.AbsenceProof) == {"kind", "terms_searched", "variants_count", "blocks_scanned", "documents"}
    assert literal_values(answer.AbsenceProof, "kind") == {"hors_perimetre", "zero_hit", "claims_rejetes"}
    assert literal_values(answer.RejectedClaim, "rejection_kind") == {"non_retrouvee", "non_pertinente", "ambigue"}
    assert fields(answer.Answer) == {
        "found", "complete", "lang", "lang_fallback", "texte", "segments", "claims", "rejected_claims",
        "reason", "verdict", "unknown", "clarification",
    }


def test_claim_requires_one_quote_per_block() -> None:
    with pytest.raises(ValidationError):
        answer.Claim(claim_id="c1", text="t", quotes=[])
    with pytest.raises(ValidationError, match="une seule quote par bloc"):
        answer.Claim(claim_id="c1", text="t", quotes=[{"block_id": "d:p1:1", "quote": "a"},
                                                      {"block_id": "d:p1:1", "quote": "b"}])


def test_answer_found_coherence() -> None:
    with pytest.raises(ValidationError, match="reason"):
        answer.Answer(found=False, complete=False)
    answer.Answer(found=False, complete=False, reason={"kind": "zero_hit"})
    with pytest.raises(ValidationError, match="claim"):
        answer.Answer(found=True, complete=True)
    def claim(retrouvee: bool = True, pertinente: bool | None = True) -> dict:
        return {"claim_id": "c", "text": "t", "quotes": [{"block_id": "d:p1:1", "quote": "q"}],
                "status": {"retrouvee": retrouvee, "pertinente": pertinente, "edition": "e"}}

    assert answer.Answer(found=True, complete=True, claims=[claim()]).claims[0].status.retrouvee
    assert answer.Answer(found=True, complete=False, claims=[claim()], unknown=["x"]).unknown == ["x"]
    for bad in (claim(retrouvee=False), claim(pertinente=False), claim(pertinente=None)):
        with pytest.raises(ValidationError, match="retrouvee"):
            answer.Answer(found=True, complete=True, claims=[bad])
    with pytest.raises(ValidationError, match="claims=\\[\\]"):
        answer.Answer(found=False, complete=False, reason={"kind": "zero_hit"}, claims=[claim()])
    with pytest.raises(ValidationError, match="complete=True"):
        answer.Answer(found=False, complete=True, reason={"kind": "zero_hit"})
    with pytest.raises(ValidationError, match="complete=True"):
        answer.Answer(found=True, complete=True, claims=[claim()], unknown=["reste"])


# AD-6
def test_verdict_models() -> None:
    assert fields(verdict.Verdict) == {"value", "reason", "missing", "ask_client", "escalate"}
    assert literal_values(verdict.Verdict, "value") == {"couvert", "non_couvert", "sous_conditions", "ne_tranche_pas"}
    assert fields(verdict.MissingPackage) == {"conditions_particulieres", "options_souscrites", "avenants", "date_effet", "faits"}
    assert "claims" not in fields(verdict.Verdict)  # Verdict n'a pas de claims[] propre


# AD-10 / AD-9
def test_trace_models() -> None:
    assert {"request_id", "pipeline", "variant", "steps", "total_cost_eur", "source_hash", "ingest_fingerprint",
            "pipeline_digest", "prompts_digest", "thresholds", "retries", "truncations", "deadline_remaining_s"} <= fields(trace.Trace)
    assert fields(trace.StepTrace) == {"name", "tier", "ms", "usage", "opened_block_ids", "discarded_block_ids", "checks", "calls"}
    assert fields(trace.Usage) == {"input", "cached", "output", "cost_eur", "cached_response", "cost_eur_original"}
    assert {"name", "ok", "detail"} == fields(trace.CheckResult)
    assert {"model", "ms", "usage", "cache_read", "cache_write", "tools"} == fields(trace.LLMCall)


# AD-16
def test_error_codes() -> None:
    assert {e.value for e in errors.ErrorCode} == {
        "invalid_request", "input_too_long", "rate_limited", "llm_unavailable", "llm_parse", "timeout",
        "budget_exceeded", "corpus_unavailable", "internal",
    }
    assert errors.HTTP_STATUS[errors.ErrorCode.invalid_request] == 400
    assert errors.HTTP_STATUS[errors.ErrorCode.input_too_long] == 413
    assert errors.HTTP_STATUS[errors.ErrorCode.rate_limited] == 429
    assert errors.HTTP_STATUS[errors.ErrorCode.internal] == 500
    for code in ("llm_unavailable", "llm_parse", "timeout", "budget_exceeded", "corpus_unavailable"):
        assert errors.HTTP_STATUS[errors.ErrorCode(code)] == 503
    assert set(errors.HTTP_STATUS) == set(errors.ErrorCode)
    env = errors.ErrorEnvelope(error={"code": "timeout", "message": "m", "request_id": "r"},
                               trace={"request_id": "r", "pipeline": "guide"})
    assert env.model_dump()["error"]["code"] == "timeout"
    assert isinstance(env.trace, trace.Trace)


# AD-5 / AD-11 / AD-1
def test_question_and_retrieval() -> None:
    assert fields(question.Turn) == {"role", "texte"}
    assert literal_values(question.Turn, "role") == {"user", "assistant"}
    assert fields(question.ParsedQuestion) == {"question_resolue", "intent", "language", "terms", "scope"}
    assert {"meteo", "bavardage", "hors_perimetre"} <= literal_values(question.ParsedQuestion, "intent")
    assert fields(question.Faits) == {"date", "lieu", "montant_eur", "description"}
    assert fields(retrieval.RetrievalResult) == {"blocs", "opened_block_ids", "discarded_block_ids", "truncated"}
    assert {"max_opens", "node_window", "search_limit", "max_llm_turns"} <= fields(retrieval.RetrievalBudget)
    with pytest.raises(ValidationError):
        question.Turn(role="user", texte="x" * 2001)
    with pytest.raises(ValidationError):
        question.Faits(description="d", montant_eur=-1)
    assert question.QuestionScope is not document.Scope


def test_profil_extra_allow_and_filter() -> None:
    from server.app.domain.profil import PROFIL_KEYS, Profil

    p = Profil(enfants=2, inconnu="x")
    assert p.model_dump()["inconnu"] == "x"
    assert p.filtered() == {"enfants": 2}
    assert "enfants" in PROFIL_KEYS


# AD-7 / AD-8 (story 1.1)
def test_ingest_models() -> None:
    from server.app.domain import ingest

    assert fields(ingest.Check) == {"name", "level", "detail"}
    assert literal_values(ingest.Check, "level") == {"bloquant", "alerte", "info"}
    assert fields(ingest.Report) == {"doc_id", "checks", "stats"}
    assert fields(ingest.Gate) == {"profile", "source_hash", "ingest_fingerprint", "cases_hash", "pipeline_digest",
                                   "prompts_digest", "model_ids", "evals_ok", "date", "overlay_hash"}
    assert literal_values(ingest.Gate, "profile") == {"vertical", "full"}
    assert fields(ingest.ManifestEntry) == {"status", "source_hash", "ingest_fingerprint", "document_hash", "edition",
                                            "overlay_hash", "gate"}
    assert literal_values(ingest.ManifestEntry, "status") == {"servi", "quarantaine"}
    assert fields(retrieval.NodeWindow) == {"node_id", "blocks", "truncated", "next_cursor"}
    r = ingest.Report(doc_id="d", checks=[{"name": "a", "level": "bloquant"}, {"name": "b", "level": "alerte"}])
    assert [c.name for c in r.blocking] == ["a"] and [c.name for c in r.alerts] == ["b"]
