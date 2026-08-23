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
    assert fields(document.Document) == {"doc_id", "kind", "title", "edition", "lang", "nodes", "source_url", "source_hash"}
    assert literal_values(document.Document, "kind") == {"guide", "contrat"}
    assert fields(document.Node) == {"node_id", "level", "title", "items", "scope", "sources"}
    assert literal_values(document.Scope, "kind") == {"commun", "special", "extension"}
    assert fields(document.Line) == {"line_id", "text", "bbox"}


def test_block_fields_and_kinds() -> None:
    assert fields(document.Block) == {
        "block_id", "text", "text_norm", "lang", "loc", "seq", "page", "bbox", "kind", "kind_confidence",
        "source_field", "continues", "refs", "unresolved_refs", "defines", "scope_node_id", "overrides",
        "relation", "lines",
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


def test_claim_requires_one_quote() -> None:
    with pytest.raises(ValidationError):
        answer.Claim(claim_id="c1", text="t", quotes=[])


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
    env = errors.ErrorEnvelope(error={"code": "timeout", "message": "m", "request_id": "r"})
    assert env.model_dump()["error"]["code"] == "timeout"


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


def test_profil_extra_allow_and_filter() -> None:
    from server.app.domain.profil import PROFIL_KEYS, Profil

    p = Profil(enfants=2, inconnu="x")
    assert p.model_dump()["inconnu"] == "x"
    assert p.filtered() == {"enfants": 2}
    assert "enfants" in PROFIL_KEYS
