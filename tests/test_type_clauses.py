from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from server.app.config import Settings
from server.app.domain import Block, BlockRef, Document, ManifestEntry, Node, NodeRef, Report
from server.app.domain.ingest import Check, Gate
from server.app.llm.pricing import cost_from_usage
from server.evals.espace import EspacePublie
from server.ingest.artifacts import document_json
from server.ingest.report import enrich_typing_report
from server.ingest import type_clauses as tc


def settings(**overrides: Any) -> Settings:
    values = {"anthropic_api_key": "sk-test", "type_clauses_batch_poll_s": 0.001,
              "type_clauses_batch_timeout_s": 1} | overrides
    return Settings(_env_file=None, **values)


def miniature() -> Document:
    root = Node(node_id="contrat", items=[NodeRef(node_id="contrat:a1"), NodeRef(node_id="contrat:a2"),
                                           NodeRef(node_id="contrat:a3")])
    a1 = Node(node_id="contrat:a1", level=1, title="Garantie",
              items=[BlockRef(block_id="contrat:p1:1")])
    a2 = Node(node_id="contrat:a2", level=1, title="Définitions",
              items=[BlockRef(block_id="contrat:p2:1")])
    a3 = Node(node_id="contrat:a3", level=1, title="Extension",
              items=[NodeRef(node_id="contrat:a3.1"), BlockRef(block_id="contrat:p3:1")])
    a31 = Node(node_id="contrat:a3.1", level=2, title="Cas",
               items=[BlockRef(block_id="contrat:p3:2")])
    blocks = [
        Block(block_id="contrat:p1:1", text="Nous garantissons le dégât des eaux selon l'article 2.",
              loc="p1", seq=1, page=1),
        Block(block_id="contrat:p2:1", text="Le contenu désigne les biens meubles.", loc="p2", seq=1, page=2),
        Block(block_id="contrat:p3:1", text="Cette extension spécialise la garantie de l'article 1.",
              loc="p3", seq=1, page=3),
        Block(block_id="contrat:p3:2", text="Sont exclus les dommages jamais déclarés.", loc="p3", seq=2, page=3),
    ]
    return Document(doc_id="contrat", kind="contrat", title="Contrat", edition="2026",
                    nodes=[root, a1, a2, a3, a31], blocks=blocks,
                    source_hash="source", ingest_fingerprint="fp")


def label(block_id: str, kind: str, **extra: Any) -> tc.ClauseLabel:
    return tc.ClauseLabel(block_id=block_id, kind=kind, confidence=extra.pop("confidence", 0.9), **extra)


def test_t1_t2_resolution_et_scopes_sont_du_code() -> None:
    doc = miniature()
    first = {
        "contrat:p1:1": label("contrat:p1:1", "garantie", article_refs=["2"]),
        "contrat:p2:1": label("contrat:p2:1", "definition", defines="contenu"),
        "contrat:p3:1": label("contrat:p3:1", "condition",
                                relations=[{"kind": "specialise", "article": "1"}]),
        "contrat:p3:2": label("contrat:p3:2", "exclusion", scope_articles=["3.1"],
                                article_refs=["99"], relations=[{"kind": "exception_de", "article": "2"}]),
    }
    second = {
        "contrat:p1:1": label("contrat:p1:1", "garantie", confidence=0.8),
        "contrat:p2:1": label("contrat:p2:1", "definition", defines="valeur T2 ignorée", confidence=0.7),
        "contrat:p3:1": label("contrat:p3:1", "condition",
                                relations=[{"kind": "specialise", "article": "1"}]),
        # désaccord exact T2 : la première lecture reste la source du kind, non confirmé
        "contrat:p3:2": label(
            "contrat:p3:2", "franchise", article_refs=["1"], scope_articles=["3"],
            overrides_article="1", relations=[{"kind": "specialise", "article": "1"}],
        ),
    }
    typed = tc.assemble(doc, first, second, settings())

    warranty = typed.block("contrat:p1:1")
    assert warranty.kind_source == "model_verified" and warranty.kind_confidence == 0.9
    assert warranty.refs == ["contrat:p2:1"]  # l'article 2 devient son unique bloc juridique
    definition = typed.block("contrat:p2:1")
    assert definition.defines == "contenu" and definition.scope_node_id is None
    exclusion = typed.block("contrat:p3:2")
    assert exclusion.kind == "exclusion" and exclusion.kind_source == "model"
    assert exclusion.scope_node_id == "contrat:a3.1" and exclusion.scope_node_ids == ["contrat:a3.1"]
    assert exclusion.unresolved_refs == ["99"]
    assert exclusion.relation.exception_de == "contrat:p2:1"  # T1 conservé malgré le désaccord T2
    assert typed.node_scope_kind("contrat:a3") == "commun"
    assert typed.node_scope_kind("contrat:a3.1") == "special"  # signal enfant le plus proche prioritaire
    assert [(b.block_id, b.text, b.lines, b.bbox) for b in typed.blocks] == \
        [(b.block_id, b.text, b.lines, b.bbox) for b in doc.blocks]


def test_an_overriding_definition_is_scoped_to_the_decisional_branch_not_its_article() -> None:
    blocks = [
        Block(block_id="contrat:p1:1", text="Accident désigne un événement.", loc="p1", seq=1),
        Block(block_id="contrat:p2:1", text="Par dérogation à l'article 1, accident désigne un choc.",
              loc="p2", seq=1),
        Block(block_id="contrat:p3:1", text="La garantie couvre cet accident.", loc="p3", seq=1),
    ]
    nodes = [
        Node(node_id="contrat", items=[NodeRef(node_id="contrat:a1"),
                                         NodeRef(node_id="contrat:a2")]),
        Node(node_id="contrat:a1", level=1, title="1 Lexique",
             items=[BlockRef(block_id="contrat:p1:1")]),
        Node(node_id="contrat:a2", level=1, title="2 Responsabilité civile",
             items=[NodeRef(node_id="contrat:a2.1"), NodeRef(node_id="contrat:a2.2")]),
        Node(node_id="contrat:a2.1", level=2, title="2.1 Définitions",
             items=[NodeRef(node_id="contrat:a2.1.1")]),
        Node(node_id="contrat:a2.1.1", level=3, title="2.1.1 Accident",
             items=[BlockRef(block_id="contrat:p2:1")]),
        Node(node_id="contrat:a2.2", level=2, title="2.2 Etendue",
             items=[BlockRef(block_id="contrat:p3:1")]),
    ]
    doc = Document(doc_id="contrat", kind="contrat", title="Contrat", edition="2026",
                   nodes=nodes, blocks=blocks, source_hash="source", ingest_fingerprint="fp")
    first = {
        "contrat:p1:1": label("contrat:p1:1", "definition", defines="accident"),
        "contrat:p2:1": label("contrat:p2:1", "definition", defines="accident",
                                overrides_article="1"),
        "contrat:p3:1": label("contrat:p3:1", "garantie"),
    }
    second = {block_id: label(block_id, value.kind, defines=value.defines)
              for block_id, value in first.items()}

    typed = tc.assemble(doc, first, second, settings())

    assert typed.block("contrat:p1:1").scope_node_id is None
    assert typed.block("contrat:p2:1").scope_node_id == "contrat:a2"
    assert typed.node_scope_kind("contrat:a2") == "special"


def test_cible_ambigue_ou_trop_large_ne_produit_jamais_de_selection_partielle() -> None:
    doc = miniature()
    # Deux nœuds portent le même numéro canonique `2` : la cible est ambiguë.
    duplicate = Node(node_id="contrat:a2-2", level=1, title="Doublon",
                     items=[BlockRef(block_id="contrat:p4:1")])
    root = doc.nodes[0].model_copy(deep=True)
    root.items.append(NodeRef(node_id=duplicate.node_id))
    doc = Document.model_validate(doc.model_copy(update={"nodes": [root, *doc.nodes[1:], duplicate],
                                                   "blocks": [*doc.blocks, Block(
                                                       block_id="contrat:p4:1", text="Autre définition.",
                                                       loc="p4", seq=1, page=4)]}).model_dump())
    first = {block.block_id: label(block.block_id, "garantie" if block.block_id.endswith("p1:1") else "autre",
                                    article_refs=["2"] if block.block_id.endswith("p1:1") else [])
             for block in doc.blocks}
    typed = tc.assemble(doc, first, {"contrat:p1:1": label("contrat:p1:1", "garantie")}, settings())
    assert typed.block("contrat:p1:1").refs == []
    assert typed.block("contrat:p1:1").unresolved_refs == ["2"]


def test_chaque_renvoi_reste_resolu_ou_signale_quand_une_plage_depasse_la_borne() -> None:
    doc = miniature()
    first = {block.block_id: label(block.block_id, "autre") for block in doc.blocks}
    first["contrat:p1:1"] = label(
        "contrat:p1:1", "garantie", article_refs=["2", "1-9"],
    )
    typed = tc.assemble(
        doc, first, {"contrat:p1:1": label("contrat:p1:1", "garantie")},
        settings(type_clauses_max_article_refs=2),
    )
    block = typed.block("contrat:p1:1")
    assert block.refs == ["contrat:p2:1"]
    assert block.unresolved_refs == ["1-9"]


def test_scope_explicite_rend_la_cible_et_ses_descendants_hors_socle() -> None:
    doc = miniature()
    child = Node(
        node_id="contrat:a3.1.1", level=3, title="Sous-cas",
        items=[BlockRef(block_id="contrat:p4:1")],
    )
    a31 = doc.nodes[-1].model_copy(deep=True)
    a31.items.append(NodeRef(node_id=child.node_id))
    doc = Document.model_validate(doc.model_copy(update={
        "nodes": [*doc.nodes[:-1], a31, child],
        "blocks": [*doc.blocks, Block(
            block_id="contrat:p4:1", text="Le dommage est garanti.", loc="p4", seq=1, page=4,
        )],
    }).model_dump())
    first = {block.block_id: label(block.block_id, "autre") for block in doc.blocks}
    first["contrat:p3:1"] = label(
        "contrat:p3:1", "exclusion", scope_articles=["3.1"],
    )
    typed = tc.assemble(
        doc, first, {"contrat:p3:1": label("contrat:p3:1", "exclusion")}, settings(),
    )
    assert typed.node_scope_kind("contrat:a3.1") == "special"
    assert typed.node_scope_kind("contrat:a3.1.1") == "special"
    assert typed.node_scope_kind("contrat:a3") == "commun"


def test_une_garantie_confirmee_referant_une_garantie_externe_devient_extension() -> None:
    """T4 : seuls des liens résolus entre garanties confirmées prouvent la branche hors socle."""
    doc = miniature()
    first = {block.block_id: label(block.block_id, "autre") for block in doc.blocks}
    first["contrat:p1:1"] = label("contrat:p1:1", "garantie")
    first["contrat:p3:1"] = label("contrat:p3:1", "garantie", article_refs=["1"])
    second = {
        "contrat:p1:1": label("contrat:p1:1", "garantie"),
        "contrat:p3:1": label("contrat:p3:1", "garantie"),
    }
    typed = tc.assemble(doc, first, second, settings())
    assert typed.node_scope_kind("contrat:a3") == "extension"
    assert typed.node_scope_kind("contrat:a3.1") == "extension"

    source_unconfirmed = tc.assemble(
        doc, first, second | {"contrat:p3:1": label("contrat:p3:1", "condition")}, settings(),
    )
    assert source_unconfirmed.node_scope_kind("contrat:a3") == "commun"

    target_unconfirmed = tc.assemble(
        doc, first, second | {"contrat:p1:1": label("contrat:p1:1", "condition")}, settings(),
    )
    assert target_unconfirmed.node_scope_kind("contrat:a3") == "commun"

    # Même graphe, mais cible non garantie : aucune promotion inventée depuis une simple ref.
    first["contrat:p1:1"] = label("contrat:p1:1", "definition", defines="garantie")
    second["contrat:p1:1"] = label("contrat:p1:1", "definition")
    conservative = tc.assemble(doc, first, second, settings())
    assert conservative.node_scope_kind("contrat:a3") == "commun"


def test_terme_defini_est_ancre_dans_le_bloc_ou_un_titre_ancetre() -> None:
    doc = miniature()
    first = {block.block_id: label(block.block_id, "autre") for block in doc.blocks}
    first["contrat:p2:1"] = label("contrat:p2:1", "definition", defines="contenu")
    anchored = tc.assemble(
        doc, first, {"contrat:p2:1": label("contrat:p2:1", "definition")}, settings(),
    )
    assert anchored.block("contrat:p2:1").defines == "contenu"

    first["contrat:p2:1"] = label("contrat:p2:1", "definition", defines="terme inventé")
    rejected = tc.assemble(
        doc, first, {"contrat:p2:1": label("contrat:p2:1", "definition")}, settings(),
    )
    block = rejected.block("contrat:p2:1")
    assert block.defines is None
    report = enrich_typing_report(
        Report(doc_id="contrat", stats={"pages_charabia": {}}), rejected,
        rejected_definitions=tc.definition_rejections(doc, first),
    )
    check = next(check for check in report.alerts if check.name == "definition_introuvable")
    assert "contrat:p2:1 → terme inventé" in check.detail


def test_un_titre_ne_devient_jamais_une_claim_decisionnelle() -> None:
    doc = miniature()
    heading = doc.block("contrat:p1:1").model_copy(update={"kind": "heading"}, deep=True)
    doc = Document.model_validate(doc.model_copy(
        update={"blocks": [heading, *doc.blocks[1:]]}, deep=True,
    ).model_dump())
    first = {block.block_id: label(block.block_id, "autre") for block in doc.blocks}
    first[heading.block_id] = label(heading.block_id, "garantie")
    typed = tc.assemble(doc, first, {heading.block_id: label(heading.block_id, "garantie")}, settings())
    block = typed.block(heading.block_id)
    assert block.kind == block.structural_kind == "heading"
    assert block.kind_source is None and block.scope_node_id is None


def test_une_plage_de_scope_decimale_est_developpee_par_le_code() -> None:
    assert tc._expand_article("3.1.8.3 à 3.1.8.6", 20) == [
        "3.1.8.3", "3.1.8.4", "3.1.8.5", "3.1.8.6",
    ]
    assert tc._expand_article("3.1.8.3-3.1.9.6", 20) is None
    assert tc._expand_article("3.1.8.1-3.1.8.30", 20) is None


def test_legal_reclasse_autre_restaure_le_kind_structurel_et_efface_tout_le_typage() -> None:
    doc = miniature()
    first = {block.block_id: label(block.block_id, "autre") for block in doc.blocks}
    first["contrat:p1:1"] = label(
        "contrat:p1:1", "definition", article_refs=["2"], scope_articles=["1"], defines="garantie",
        overrides_article="2", relations=[{"kind": "exception_de", "article": "2"}],
    )
    typed = tc.assemble(doc, first, {"contrat:p1:1": label("contrat:p1:1", "definition")}, settings())
    assert typed.block("contrat:p1:1").kind == "definition"

    all_other = {block.block_id: label(block.block_id, "autre") for block in typed.blocks}
    reset = tc.assemble(typed, all_other, {}, settings())
    block = reset.block("contrat:p1:1")
    assert block.kind == block.structural_kind == "para"
    assert block.kind_source is None and block.kind_confidence is None
    assert block.refs == [] and block.unresolved_refs == [] and block.defines is None
    assert block.scope_node_id is None and block.scope_node_ids == [] and block.overrides is None
    assert block.relation.model_dump(exclude_none=True) == {}
    assert {node.scope.kind for node in reset.nodes} == {"commun"}


def test_refs_override_et_relation_auto_referents_restent_non_resolus() -> None:
    doc = miniature()
    first = {block.block_id: label(block.block_id, "autre") for block in doc.blocks}
    first["contrat:p1:1"] = label(
        "contrat:p1:1", "definition", defines="garantie", article_refs=["1"],
        overrides_article="1", relations=[{"kind": "exception_de", "article": "1"}],
    )
    typed = tc.assemble(doc, first, {"contrat:p1:1": label("contrat:p1:1", "definition")}, settings())
    block = typed.block("contrat:p1:1")
    assert block.refs == [] and block.overrides is None
    assert block.relation.model_dump(exclude_none=True) == {}
    assert block.unresolved_refs == ["1"]


def test_empreinte_de_campagne_lie_texte_structure_modele_schema_prompts_regles_et_seuils() -> None:
    doc = miniature()
    base_settings = settings()
    fingerprint = tc.campaign_fingerprint(doc, base_settings)
    changed_settings = tc.campaign_fingerprint(doc, settings(type_clauses_max_article_refs=11))
    changed_doc = doc.model_copy(deep=True)
    changed_doc.blocks[0].text += " Modification."
    changed_doc = Document.model_validate(changed_doc.model_dump())
    assert fingerprint != changed_settings != tc.campaign_fingerprint(changed_doc, base_settings)
    plans = tc.requests_for(doc, doc.blocks, 1, base_settings)
    assert fingerprint[:12] in plans[0].custom_id
    assert plans[0].custom_id != tc._legacy_custom_id(1, 1, plans[0].block_ids)


def test_output_schema_forbids_unknown_fields_and_bounds_confidence() -> None:
    with pytest.raises(ValidationError):
        tc.ReadingOutput.model_validate({"labels": [{"block_id": "contrat:p1:1", "kind": "garantie",
                                                       "confidence": 0.9, "texte": "copie interdite"}]})
    with pytest.raises(ValidationError):
        label("contrat:p1:1", "garantie", confidence=1.1)
    schema = tc._schema()["schema"]
    assert schema["additionalProperties"] is False
    assert "text" not in json.dumps(schema)


def test_premiere_lecture_exige_sa_couverture_dans_le_schema_fournisseur() -> None:
    first_schema = tc.requests_for(miniature(), miniature().blocks, 1, settings())[0].request["params"] \
        ["output_config"]["format"]["schema"]["properties"]["labels"]
    second_schema = tc.requests_for(miniature(), miniature().blocks, 2, settings())[0].request["params"] \
        ["output_config"]["format"]["schema"]["properties"]["labels"]
    assert first_schema["type"] == "object" and first_schema["additionalProperties"] is False
    assert set(first_schema["required"]) == {block.block_id for block in miniature().blocks}
    assert set(first_schema["properties"]) == set(first_schema["required"])
    assert second_schema["type"] == "array"


def test_requests_group_by_two_independent_bounds_and_keep_unique_custom_ids() -> None:
    doc = miniature()
    plans = tc.requests_for(doc, doc.blocks, 1, settings(type_clauses_max_blocks_per_request=2),)
    assert len(plans) == 2 and len({plan.custom_id for plan in plans}) == 2
    campaign = tc.campaign_fingerprint(doc, settings(type_clauses_max_blocks_per_request=2))
    assert all(repr(plan.custom_id) and tc.custom_id(1, i, plan.block_ids, campaign) == plan.custom_id
               for i, plan in enumerate(plans, 1))
    params = plans[0].request["params"]
    assert params["model"] == tc.MODEL and params["max_tokens"] == settings().type_clauses_max_output_tokens
    assert params["output_config"]["effort"] == "high"
    sent = json.loads(params["messages"][0]["content"])
    assert {item["block_id"] for item in sent["blocks"]} == set(plans[0].block_ids)


class FakeBatches:
    def __init__(self, kinds: dict[str, str], *, second_kinds: dict[str, str] | None = None,
                 fail_first: bool = False, fail_second: bool = False,
                 truncate: bool = False, invalid_json: bool = False, missing_usage: bool = False) -> None:
        self.kinds = kinds
        self.second_kinds = second_kinds or kinds
        self.fail_first = fail_first
        self.fail_second = fail_second
        self.truncate = truncate
        self.invalid_json = invalid_json
        self.missing_usage = missing_usage
        self.created: list[list[dict[str, Any]]] = []
        self._by_id: dict[str, list[dict[str, Any]]] = {}
        self.canceled: list[str] = []

    def create(self, *, requests: list[dict[str, Any]]) -> Any:
        batch_id = f"batch-{len(self.created) + 1}"
        self.created.append(requests)
        self._by_id[batch_id] = requests
        return SimpleNamespace(id=batch_id)

    def retrieve(self, batch_id: str) -> Any:
        return SimpleNamespace(processing_status="ended")

    def cancel(self, batch_id: str) -> None:
        self.canceled.append(batch_id)

    def results(self, batch_id: str) -> list[Any]:
        out = []
        reading = 1 if all(request["custom_id"].startswith("clauses-r1-")
                           for request in self._by_id[batch_id]) else 2
        for index, request in enumerate(reversed(self._by_id[batch_id])):  # hors ordre T1
            cid = request["custom_id"]
            if ((self.fail_first and reading == 1)
                    or (self.fail_second and reading == 2)) and index == 0:
                out.append(SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="errored")))
                continue
            payload = json.loads(request["params"]["messages"][0]["content"])
            labels = []
            for item in payload["blocks"]:
                block_id = item["block_id"]
                source = self.second_kinds if reading == 2 else self.kinds
                kind = source.get(block_id, "autre")
                value: dict[str, Any] = {"block_id": block_id, "kind": kind, "confidence": 0.9,
                                         "article_refs": [], "scope_articles": [], "defines": None,
                                         "overrides_article": None, "relations": []}
                if kind == "definition":
                    value["defines"] = "contenu"
                labels.append(value)
            response_labels: Any = ({value["block_id"]: value for value in labels} if reading == 1 else labels)
            message = SimpleNamespace(
                usage=None if self.missing_usage else {"input_tokens": 100, "output_tokens": 20},
                stop_reason="max_tokens" if self.truncate and reading == 1 else "end_turn",
                content=[SimpleNamespace(
                    type="text",
                    text="{json tronqué" if self.invalid_json and reading == 1
                    else json.dumps({"labels": response_labels}),
                )],
            )
            out.append(SimpleNamespace(custom_id=cid,
                                       result=SimpleNamespace(type="succeeded", message=message)))
        return out


class FakeClient:
    def __init__(self, batches: FakeBatches) -> None:
        self.messages = SimpleNamespace(batches=batches)


class RetryableStandardError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeStandardMessages:
    def __init__(self, batches: FakeBatches, kinds: dict[str, str], *,
                 failures: list[int] | None = None,
                 interrupted_reading: int | None = None,
                 interrupted_stop_reason: str = "max_tokens") -> None:
        self.batches = batches
        self.kinds = kinds
        self.failures = list(failures or [])
        self.interrupted_reading = interrupted_reading
        self.interrupted_stop_reason = interrupted_stop_reason
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        if self.failures:
            raise RetryableStandardError(self.failures.pop(0))
        payload = json.loads(params["messages"][0]["content"])
        reading_one = params["output_config"]["format"]["schema"]["properties"]["labels"]["type"] == "object"
        labels = []
        for item in payload["blocks"]:
            block_id = item["block_id"]
            kind = self.kinds.get(block_id, "autre")
            value = {
                "block_id": block_id, "kind": kind, "confidence": 0.9,
                "article_refs": [], "scope_articles": [], "defines": None,
                "overrides_article": None, "relations": [],
            }
            if kind == "definition":
                value["defines"] = "contenu"
            labels.append(value)
        rendered: Any = {value["block_id"]: value for value in labels} if reading_one else labels
        return SimpleNamespace(
            usage={"input_tokens": 100, "output_tokens": 20},
            stop_reason=(self.interrupted_stop_reason
                         if self.interrupted_reading == (1 if reading_one else 2)
                         else "end_turn"),
            content=[SimpleNamespace(type="text", text=json.dumps({"labels": rendered}))],
        )


class FakeStandardClient:
    def __init__(self, batches: FakeBatches, kinds: dict[str, str], *,
                 failures: list[int] | None = None,
                 interrupted_reading: int | None = None,
                 interrupted_stop_reason: str = "max_tokens") -> None:
        self.messages = FakeStandardMessages(
            batches, kinds, failures=failures, interrupted_reading=interrupted_reading,
            interrupted_stop_reason=interrupted_stop_reason,
        )


def write_data(tmp_path: Path, *, overlay: bool = False) -> tuple[Path, Document]:
    data = tmp_path / "data"
    doc_dir = data / "contrat"
    doc_dir.mkdir(parents=True)
    doc = miniature()
    text = document_json(doc)
    (doc_dir / "document.json").write_text(text, "utf-8")
    (doc_dir / "report.json").write_text(json.dumps(Report(
        doc_id="contrat", checks=[Check(name="invariants_arbre", level="info", detail="ok")],
        stats={"pages_charabia": {}},
    ).model_dump()), "utf-8")
    (doc_dir / "summary.md").write_text("# Contrat\n", "utf-8")
    overlay_hash = None
    if overlay:
        overlay_text = json.dumps({"schema_version": "1", "doc_id": "contrat", "blocks": {
            "contrat:p1:1": {"kind": "garantie", "scope_node_id": "contrat:a1", "kind_source": "manual"},
            "contrat:p2:1": {"kind": "definition", "defines": "contenu", "kind_source": "manual"},
        }})
        (doc_dir / "typing.manual.json").write_text(overlay_text, "utf-8")
        overlay_hash = hashlib.sha256(overlay_text.encode()).hexdigest()
    entry = ManifestEntry(status="servi", source_hash="source", ingest_fingerprint="fp",
                          document_hash=hashlib.sha256(text.encode()).hexdigest(), edition="2026",
                          overlay_hash=overlay_hash)
    (data / "manifest.json").write_text(json.dumps({"contrat": entry.model_dump()}), "utf-8")
    return doc_dir, doc


def write_overlay(doc_dir: Path, raw: Any) -> None:
    path = doc_dir / "typing.manual.json"
    path.write_text(json.dumps(raw), "utf-8")
    manifest_path = doc_dir.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["contrat"]["overlay_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), "utf-8")


def test_t4_lot_incomplet_ou_tronque_ne_modifie_aucun_octet(tmp_path: Path) -> None:
    cases = (
        FakeBatches({}, fail_first=True),
        FakeBatches({}, truncate=True),
        FakeBatches({}, invalid_json=True),
        FakeBatches({}, missing_usage=True),
        FakeBatches({"contrat:p1:1": "garantie"}, fail_second=True),
    )
    for index, batches in enumerate(cases):
        doc_dir, _ = write_data(tmp_path / str(index))
        files = [doc_dir / "document.json", doc_dir / "report.json", doc_dir.parent / "manifest.json"]
        before = {path: path.read_bytes() for path in files}
        with pytest.raises(tc.BatchFailure, match=r"coût réel(?: cumulé)? \d+\.\d{4} €"):
            tc.run(doc_dir, settings=settings(), client=FakeClient(batches), output=io.StringIO())
        assert {path: path.read_bytes() for path in files} == before


@pytest.mark.parametrize("bad_blocks", [
    {"contrat:p1:1": []},
    {"contrat:p404:1": {"kind": "garantie", "scope_node_id": "contrat:a1", "kind_source": "manual"}},
])
def test_overlay_malforme_ou_bloc_absent_est_refuse_avant_toute_soumission(
    tmp_path: Path, bad_blocks: dict[str, Any],
) -> None:
    doc_dir, _ = write_data(tmp_path, overlay=True)
    write_overlay(doc_dir, {"schema_version": "1", "doc_id": "contrat", "blocks": bad_blocks})
    batches = FakeBatches({})
    tracked = [doc_dir / "document.json", doc_dir / "report.json", doc_dir / "typing.manual.json",
               doc_dir.parent / "manifest.json"]
    before = {path: path.read_bytes() for path in tracked}
    with pytest.raises(ValueError, match="typing.manual.json"):
        tc.run(doc_dir, settings=settings(), client=FakeClient(batches), output=io.StringIO())
    assert batches.created == [] and {path: path.read_bytes() for path in tracked} == before


@pytest.mark.parametrize("failure", ["kind", "confirmation", "defines", "portee"])
def test_migration_incompatible_conserve_tous_les_octets(tmp_path: Path, failure: str) -> None:
    doc_dir, _ = write_data(tmp_path, overlay=True)
    overlay_path = doc_dir / "typing.manual.json"
    overlay = json.loads(overlay_path.read_text("utf-8"))
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    second_kinds = dict(kinds)
    if failure == "kind":
        overlay["blocks"]["contrat:p1:1"]["kind"] = "exclusion"
    elif failure == "confirmation":
        second_kinds["contrat:p1:1"] = "condition"
    elif failure == "defines":
        overlay["blocks"]["contrat:p2:1"]["defines"] = "autre terme"
    else:
        overlay["blocks"]["contrat:p1:1"]["scope_node_ids"] = ["contrat:a1"]
    write_overlay(doc_dir, overlay)
    tracked = [doc_dir / "document.json", doc_dir / "report.json", overlay_path,
               doc_dir.parent / "manifest.json"]
    before = {path: path.read_bytes() for path in tracked}
    batches = FakeBatches(kinds, second_kinds=second_kinds)
    with pytest.raises(tc.BatchFailure, match="aucun artefact écrit"):
        tc.run(doc_dir, settings=settings(), client=FakeClient(batches), output=io.StringIO())
    assert {path: path.read_bytes() for path in tracked} == before


def test_rapport_sans_pages_charabia_est_refuse_avant_batch(tmp_path: Path) -> None:
    doc_dir, _ = write_data(tmp_path)
    report = Report(doc_id="contrat", checks=[Check(name="invariants_arbre", level="info")], stats={})
    (doc_dir / "report.json").write_text(json.dumps(report.model_dump()), "utf-8")
    batches = FakeBatches({})
    with pytest.raises(ValueError, match="pages_charabia.*relancer pdf_to_blocks"):
        tc.run(doc_dir, settings=settings(), client=FakeClient(batches), output=io.StringIO())
    assert batches.created == []


def test_verrou_exclusif_refuse_un_typage_concurrent_et_se_libere(tmp_path: Path) -> None:
    doc_dir, _ = write_data(tmp_path)
    with tc._exclusive_lock(doc_dir), pytest.raises(tc.BatchFailure, match="typage concurrent"):
        tc.run(doc_dir, settings=settings(), client=FakeClient(FakeBatches({})), dry_run=True,
               output=io.StringIO())
    assert tc.run(doc_dir, settings=settings(), client=FakeClient(FakeBatches({})), dry_run=True,
                  output=io.StringIO()) is None


def test_un_echec_de_remplacement_restaure_tous_les_artefacts(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    paths = [tmp_path / name for name in ("document.json", "report.json", "manifest.json")]
    overlay = tmp_path / "typing.manual.json"
    for index, path in enumerate([*paths, overlay]):
        path.write_text(f"avant-{index}", "utf-8")
    original_replace = tc.os.replace
    calls = 0

    def fail_once(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("panne simulée")
        original_replace(source, target)

    monkeypatch.setattr(tc.os, "replace", fail_once)
    with pytest.raises(OSError, match="panne simulée"):
        tc._atomic_artifacts({path: f"après-{index}" for index, path in enumerate(paths)}, [overlay])
    assert [path.read_text("utf-8") for path in paths] == ["avant-0", "avant-1", "avant-2"]
    assert overlay.read_text("utf-8") == "avant-3"


def _etat_observable(cibles: list[Path]) -> dict[str, tuple[bool, str | None, str | None, str | None]]:
    """Les quatre dimensions de l'AC : présence, contenu, type `lstat`, cible de lien.

    Le même repère que les sondes de `tests/test_publication_evals.py` : comparer les octets seuls
    laisserait passer un lien couvert remplacé par un fichier ordinaire de même contenu — ce qui est
    exactement le fait 1 du tour de racine unique.
    """
    etat: dict[str, tuple[bool, str | None, str | None, str | None]] = {}
    for cible in cibles:
        existe = cible.is_symlink() or cible.exists()
        try:
            contenu: str | None = cible.read_text("utf-8")
        except OSError:
            contenu = None
        if cible.is_symlink():
            type_entree: str | None = "lien"
            lien: str | None = os.readlink(cible)
        elif cible.is_dir():
            type_entree, lien = "repertoire", None
        elif cible.exists():
            type_entree, lien = "fichier", None
        else:
            type_entree, lien = None, None
        etat[str(cible)] = (existe, contenu, type_entree, lien)
    return etat


def test_le_typage_ne_remplace_jamais_un_lien_couvert_par_un_fichier_ordinaire(tmp_path: Path) -> None:
    """Fait 1 du tour de racine unique : `_atomic_artifacts` sortait le manifest du pointeur.

    `os.replace(temp, path)` sur un lien symbolique le **remplace** par un fichier ordinaire. Sur
    `data/manifest.json`, couvert par la racine de publication, une seule exécution du typage
    suffisait : le manifest quittait le pointeur pour de bon, et la bascule suivante d'un run
    publiait ses autres surfaces sans que le manifest en voie rien — le faux vert exact que
    `write_atomic` documente, obtenu par un chemin qui ne l'appelait pas.

    La sonde compare les **quatre dimensions** avant/après, et vérifie en plus que la génération
    publiée n'a pas été mutée en place : c'est la différence entre « écrire à travers le lien » et
    « passer par le protocole ».
    """
    espace = EspacePublie(tmp_path)
    doc_dir = tmp_path / "data" / "contrat"
    cibles = [doc_dir / "document.json", doc_dir / "report.json",
              tmp_path / "data" / "manifest.json", doc_dir / "typing.manual.json"]
    espace.installer([c.relative_to(tmp_path) for c in cibles])
    espace.basculer([(cibles[0], "doc-avant"), (cibles[1], "rapport-avant"),
                     (cibles[2], "manifest-avant"), (cibles[3], "overlay-avant")])

    generation_avant = espace.generation()
    slot_manifest = espace.chemin / generation_avant / "data" / "manifest.json"
    octets_avant, inode_avant = slot_manifest.read_bytes(), os.stat(slot_manifest).st_ino
    types_avant = {str(c): os.lstat(c).st_mode for c in cibles}

    tc._atomic_artifacts({cibles[0]: "doc-après", cibles[1]: "rapport-après",
                          cibles[2]: "manifest-après"}, [cibles[3]])

    assert espace.generation() != generation_avant, "le typage doit publier, jamais muter"
    assert slot_manifest.read_bytes() == octets_avant, "la génération publiée a été mutée en place"
    assert os.stat(slot_manifest).st_ino == inode_avant
    for cible in cibles:
        assert cible.is_symlink(), f"{cible} : le lien statique a été remplacé"
        assert os.lstat(cible).st_mode == types_avant[str(cible)]
        assert espace.resolue_dans_lespace(cible), f"{cible} est sortie du pointeur"
    assert cibles[0].read_text("utf-8") == "doc-après"
    assert cibles[1].read_text("utf-8") == "rapport-après"
    assert cibles[2].read_text("utf-8") == "manifest-après"
    # La suppression de l'overlay est **membre du lot** : elle devient une absence par le même geste.
    assert not cibles[3].exists()
    assert espace.residus() == []


def test_un_echec_du_typage_sur_un_lot_couvert_ne_laisse_aucune_cible_modifiee(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le lot du typage bascule d'un seul geste, ou pas du tout — plus aucune restauration.

    La forme d'avant préparait ses temporaires, remplaçait cible par cible, puis **défaisait** sur
    erreur en réécrivant les octets d'entrée. C'est précisément la forme que les tours correctifs
    1/3 et 2/3 ont refusée pour les évals : un protocole qui défait ce qu'il a fait admet qu'après
    épuisement une cible reste dans le nouvel état. Ici il n'y a rien à défaire.
    """
    espace = EspacePublie(tmp_path)
    doc_dir = tmp_path / "data" / "contrat"
    cibles = [doc_dir / "document.json", doc_dir / "report.json",
              tmp_path / "data" / "manifest.json", doc_dir / "typing.manual.json"]
    espace.installer([c.relative_to(tmp_path) for c in cibles])
    espace.basculer([(cibles[0], "doc-avant"), (cibles[1], "rapport-avant"),
                     (cibles[2], "manifest-avant"), (cibles[3], "overlay-avant")])
    avant = _etat_observable(cibles)

    from server.evals import espace as espace_module

    def _boum(chemin: Path, contenu: str) -> None:
        raise OSError("panne simulée")

    monkeypatch.setattr(espace_module, "_ecrire_dans_bundle", _boum)
    with pytest.raises(OSError, match="panne simulée"):
        tc._atomic_artifacts({cibles[0]: "doc-après", cibles[1]: "rapport-après",
                              cibles[2]: "manifest-après"}, [cibles[3]])
    assert _etat_observable(cibles) == avant
    assert espace.residus() == []


def test_success_writes_automatic_document_removes_overlay_and_invalidates_gate(tmp_path: Path) -> None:
    doc_dir, _ = write_data(tmp_path, overlay=True)
    manifest = json.loads((doc_dir.parent / "manifest.json").read_text("utf-8"))
    entry = ManifestEntry.model_validate(manifest["contrat"])
    gate = Gate(profile="vertical", source_hash="source", ingest_fingerprint="fp", overlay_hash=entry.overlay_hash,
                cases_hash="cases", cases=1, countersigned=False, pipeline_digest="p", prompts_digest="q",
                model_ids={}, evals_ok=True, date="2026-08-26")
    manifest["contrat"]["gate"] = gate.model_dump()
    (doc_dir.parent / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    result = tc.run(doc_dir, settings=settings(), client=FakeClient(FakeBatches(kinds)), output=io.StringIO())
    assert result is not None and result.entry.gate is None and result.entry.overlay_hash is None
    per_request = cost_from_usage(
        tc.MODEL, {"input_tokens": 100, "output_tokens": 20}, settings().usd_eur, batch=True,
    ).cost_eur
    assert result.first_requests == result.second_requests == 1
    assert result.cost_eur == round(per_request * 2, 4)  # T1 + T2, taux EUR et remise Batch inclus
    assert not (doc_dir / "typing.manual.json").exists()
    saved = Document.model_validate_json((doc_dir / "document.json").read_bytes())
    assert saved.block("contrat:p1:1").kind_source == "model_verified"
    assert saved.block("contrat:p2:1").defines == "contenu"
    assert [check.name for check in result.report.checks][-1] == "typage_clauses"


def test_t6_byte_identical_rerun_preserves_an_existing_gate(tmp_path: Path) -> None:
    doc_dir, _ = write_data(tmp_path)
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    first = tc.run(doc_dir, settings=settings(), client=FakeClient(FakeBatches(kinds)), output=io.StringIO())
    assert first is not None
    manifest_path = doc_dir.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["contrat"]["gate"] = Gate(
        profile="vertical", source_hash="source", ingest_fingerprint="fp", overlay_hash=None,
        cases_hash="cases", cases=1, countersigned=False, pipeline_digest="p", prompts_digest="q",
        model_ids={}, evals_ok=True, date="2026-08-26",
    ).model_dump()
    manifest_path.write_text(json.dumps(manifest), "utf-8")
    before_doc = (doc_dir / "document.json").read_bytes()
    second = tc.run(doc_dir, settings=settings(), client=FakeClient(FakeBatches(kinds)), output=io.StringIO())
    assert second is not None and second.entry.gate is not None
    assert (doc_dir / "document.json").read_bytes() == before_doc


def test_report_typing_checks_and_structured_corruption() -> None:
    doc = miniature()
    first = {block.block_id: label(block.block_id, "exclusion", confidence=0.4) for block in doc.blocks}
    second = {block.block_id: label(block.block_id, "condition") for block in doc.blocks}
    typed = tc.assemble(doc, first, second, settings())
    report = Report(doc_id="contrat", checks=[Check(name="structure", level="info", detail="inchangé")],
                    stats={"pages_charabia": {"1": 1}})
    enriched = enrich_typing_report(report, typed)
    names = {check.name: check.level for check in enriched.checks}
    assert names["structure"] == "info" and names["corruption_decisionnelle"] == "bloquant"
    assert names["confiance_typage_faible"] == "alerte" and names["kinds_non_confirmes"] == "alerte"
    assert names["exclusion_sans_marqueur"] == "alerte"  # au moins les blocs sans négation


def test_rapport_compte_les_tables_depuis_le_kind_structurel() -> None:
    doc = miniature()
    table = doc.block("contrat:p1:1").model_copy(update={
        "kind": "garantie", "structural_kind": "table", "kind_source": "model_verified",
        "kind_confidence": 0.9, "scope_node_id": "contrat:a1",
    }, deep=True)
    doc = Document.model_validate(doc.model_copy(
        update={"blocks": [table, *doc.blocks[1:]]}, deep=True,
    ).model_dump())
    report = enrich_typing_report(
        Report(doc_id="contrat", stats={"pages_charabia": {}, "tables": 0}), doc,
    )
    assert report.stats["tables"] == 1
    assert sum(
        (block.structural_kind or block.kind) == "table" for block in doc.blocks
    ) == report.stats["tables"]


def test_dry_run_plans_two_readings_without_key_or_submission(tmp_path: Path) -> None:
    doc_dir, _ = write_data(tmp_path)
    batches = FakeBatches({})
    assert tc.run(doc_dir, settings=settings(anthropic_api_key=""), client=FakeClient(batches), dry_run=True,
                  output=io.StringIO()) is None
    assert batches.created == []


def test_un_lot_deja_soumis_est_recupere_sans_nouvelle_soumission() -> None:
    doc = miniature()
    plans = tc.requests_for(doc, doc.blocks, 1, settings())
    batches = FakeBatches({})
    # Le double connaît ce lot comme le ferait l'API après une interruption locale.
    batches._by_id["batch-existing"] = [plan.request for plan in plans]
    texts, _cost, failures = tc.execute_batch(
        FakeClient(batches), plans, settings(), batch_id="batch-existing", output=io.StringIO(),
    )
    assert not failures and set(texts) == {plan.custom_id for plan in plans}
    assert batches.created == []


def test_standard_initial_execute_t1_t2_sans_endpoint_batch_et_publie_atomiquement(
    tmp_path: Path,
) -> None:
    doc_dir, _doc = write_data(tmp_path)
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    batches = FakeBatches(kinds)
    client = FakeStandardClient(batches, kinds)

    result = tc.run(
        doc_dir, settings=settings(type_clauses_standard_concurrency=2), client=client,
        transport="standard", max_cost=12, output=io.StringIO(),
    )

    assert result is not None and result.transport == "standard"
    assert result.batch_cost_eur == 0 and result.standard_cost_eur == result.cost_eur
    assert result.standard_requests == result.first_requests + result.second_requests
    assert len(client.messages.calls) == result.standard_requests
    assert batches.created == []
    report = Report.model_validate_json((doc_dir / "report.json").read_bytes())
    assert report.stats["typage_transport"] == "standard"
    proof = next(check for check in report.checks if check.name == "typage_transport")
    assert "aucune API Batch" in proof.detail


def test_standard_initial_respecte_plafond_avant_appel_et_echec_terminal_necrit_rien(
    tmp_path: Path,
) -> None:
    doc_dir, _doc = write_data(tmp_path / "cap")
    tracked = [doc_dir / "document.json", doc_dir / "report.json", doc_dir.parent / "manifest.json"]
    before = {path: path.read_bytes() for path in tracked}
    no_call = FakeStandardClient(FakeBatches({}), {})
    with pytest.raises(ValueError, match="majorant .* > plafond"):
        tc.run(
            doc_dir, settings=settings(), client=no_call, transport="standard",
            max_cost=0.000001, output=io.StringIO(),
        )
    assert no_call.messages.calls == [] and no_call.messages.batches.created == []
    assert {path: path.read_bytes() for path in tracked} == before

    failed_dir, _doc = write_data(tmp_path / "failure")
    failed_paths = [failed_dir / "document.json", failed_dir / "report.json",
                    failed_dir.parent / "manifest.json"]
    failed_before = {path: path.read_bytes() for path in failed_paths}
    failed = FakeStandardClient(FakeBatches({}), {}, failures=[400])
    with pytest.raises(tc.BatchFailure, match="premier échec terminal"):
        tc.run(
            failed_dir, settings=settings(), client=failed, transport="standard",
            max_cost=12, output=io.StringIO(),
        )
    assert failed.messages.batches.created == []
    assert {path: path.read_bytes() for path in failed_paths} == failed_before


def test_standard_initial_t1_facturee_puis_t2_terminale_reste_atomique(tmp_path: Path) -> None:
    doc_dir, _doc = write_data(tmp_path)
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    client = FakeStandardClient(
        FakeBatches(kinds), kinds, interrupted_reading=2, interrupted_stop_reason="max_tokens",
    )
    tracked = [doc_dir / "document.json", doc_dir / "report.json", doc_dir.parent / "manifest.json"]
    before = {path: path.read_bytes() for path in tracked}

    with pytest.raises(tc.BatchFailure, match=r"coût réel acquis 0\.[0-9]*[1-9][0-9]* €.*aucun artefact écrit"):
        tc.run(
            doc_dir, settings=settings(type_clauses_standard_concurrency=1), client=client,
            transport="standard", max_cost=12, output=io.StringIO(),
        )

    assert len(client.messages.calls) >= 2
    assert client.messages.batches.created == []
    assert {path: path.read_bytes() for path in tracked} == before


@pytest.mark.parametrize("transport", ["batch", "standard"])
def test_preuves_payload_interdites_hors_standard_resume(
    tmp_path: Path, transport: str,
) -> None:
    doc_dir, _doc = write_data(tmp_path)
    client = FakeStandardClient(FakeBatches({}), {})
    with pytest.raises(ValueError, match="preuves --resume-\\*"):
        tc.run(
            doc_dir, settings=settings(), client=client,
            transport=transport,  # type: ignore[arg-type]
            resume_payload_proofs_map={"inattendu": "a" * 64}, output=io.StringIO(),
        )
    assert client.messages.calls == [] and client.messages.batches.created == []


def test_run_et_cli_restent_batch_par_defaut_et_standard_est_explicite(tmp_path: Path) -> None:
    batch_dir, _doc = write_data(tmp_path / "batch")
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    batches = FakeBatches(kinds)
    result = tc.run(batch_dir, settings=settings(), client=FakeClient(batches), output=io.StringIO())
    assert result is not None and result.transport == "batch" and len(batches.created) == 2

    standard_dir, _doc = write_data(tmp_path / "standard")
    standard_batches = FakeBatches(kinds)
    standard_client = FakeStandardClient(standard_batches, kinds)
    code = tc.main(
        ["contrat", "--data", str(standard_dir.parent), "--transport", "standard", "--max-cost", "12"],
        client=standard_client, settings=settings(), output=io.StringIO(),
    )
    assert code == 0 and standard_batches.created == [] and standard_client.messages.calls


def test_standard_resume_reutilise_lot_complete_manquants_et_ne_cree_aucun_batch(
    tmp_path: Path,
) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=2,
                          type_clauses_standard_concurrency=2,
                          type_clauses_standard_retry_base_s=0.001)
    campaign = "a" * 64
    first = tc.requests_for(doc, doc.blocks, 1, configured, campaign_override=campaign)
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    batches = FakeBatches(kinds, fail_first=True)
    batches._by_id["partial-first"] = [plan.request for plan in first]
    client = FakeStandardClient(batches, kinds)

    result = tc.run(
        doc_dir, settings=configured, client=client, max_cost=12,
        first_batch_id="partial-first", transport="standard-resume",
        resume_campaign=campaign,
        resume_payload_sha256=tc.resume_payload_fingerprint(first, configured),
        resume_justification="fixture : même payload, transport de reprise explicite",
        output=io.StringIO(),
    )

    assert result is not None and result.transport == "standard-resume"
    assert result.reused_requests == 1 and result.standard_requests == 2
    assert len(client.messages.calls) == 2 and batches.created == []
    assert result.cost_eur == round(result.batch_cost_eur + result.standard_cost_eur, 4)
    saved_report = Report.model_validate_json((doc_dir / "report.json").read_bytes())
    assert saved_report.stats["typage_resume_batch_id"] == "partial-first"
    assert saved_report.stats["typage_reused_requests"] == 1
    assert saved_report.stats["typage_standard_requests"] == 2
    proof = next(check for check in saved_report.checks if check.name == "typage_transport")
    assert "aucune" not in proof.detail and campaign in proof.detail


def test_standard_resume_rejoue_un_succes_dont_le_payload_a_change(tmp_path: Path) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=2,
                          type_clauses_standard_concurrency=1)
    campaign = "e" * 64
    first = tc.requests_for(doc, doc.blocks, 1, configured, campaign_override=campaign)
    resumed_proofs = tc.resume_payload_proofs(first, configured)
    changed_id = first[0].custom_id
    resumed_proofs[changed_id] = "0" * 64
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    batches = FakeBatches(kinds)
    batches._by_id["complete-old-first"] = [plan.request for plan in first]
    client = FakeStandardClient(batches, kinds)

    result = tc.run(
        doc_dir, settings=configured, client=client, max_cost=12,
        first_batch_id="complete-old-first", transport="standard-resume",
        resume_campaign=campaign,
        resume_payload_sha256=tc._payload_proofs_fingerprint(resumed_proofs),
        resume_payload_proofs_map=resumed_proofs,
        resume_justification="fixture : un plan propriétaire a changé et doit être rejoué",
        output=io.StringIO(),
    )

    assert result is not None
    assert result.replayed_requests == 1 and result.reused_requests == len(first) - 1
    assert len(client.messages.calls) == 2  # un plan T1 rejoué + la lecture T2
    report = Report.model_validate_json((doc_dir / "report.json").read_bytes())
    assert report.stats["typage_changed_payload_custom_ids"] == changed_id
    assert report.stats["typage_changed_payload_requests"] == 1


def test_standard_retries_seulement_429_et_5xx_et_conserve_lordre() -> None:
    doc = miniature()
    configured = settings(type_clauses_max_blocks_per_request=2,
                          type_clauses_standard_concurrency=1,
                          type_clauses_standard_max_retries=2,
                          type_clauses_standard_retry_base_s=0.001)
    plans = tc.requests_for(doc, doc.blocks, 1, configured)
    client = FakeStandardClient(FakeBatches({}), {}, failures=[429, 503])
    guard = tc._CostGuard(12)
    texts, cost = tc.execute_standard(
        client, plans, configured, guard=guard, output=io.StringIO(), sleep=lambda _delay: None,
    )
    assert list(texts) == [plan.custom_id for plan in plans]
    assert len(client.messages.calls) == len(plans) + 2 and cost > 0


def test_standard_garde_le_cout_avant_appel_et_echec_necrit_rien(tmp_path: Path) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=2,
                          type_clauses_standard_concurrency=1,
                          type_clauses_standard_max_retries=1,
                          type_clauses_standard_retry_base_s=0.001)
    campaign = "b" * 64
    first = tc.requests_for(doc, doc.blocks, 1, configured, campaign_override=campaign)
    batches = FakeBatches({}, fail_first=True)
    batches._by_id["partial-first"] = [plan.request for plan in first]
    client = FakeStandardClient(batches, {}, failures=[500, 500])
    tracked = [doc_dir / "document.json", doc_dir / "report.json", doc_dir.parent / "manifest.json"]
    before = {path: path.read_bytes() for path in tracked}

    with pytest.raises(tc.BatchFailure, match="premier échec terminal"):
        tc.run(
            doc_dir, settings=configured, client=client, max_cost=12,
            first_batch_id="partial-first", transport="standard-resume",
            resume_campaign=campaign,
            resume_payload_sha256=tc.resume_payload_fingerprint(first, configured),
            resume_justification="fixture : même payload avant l'échec standard",
            output=io.StringIO(), sleep=lambda _delay: None,
        )
    assert len(client.messages.calls) == 2 and batches.created == []
    assert {path: path.read_bytes() for path in tracked} == before

    plan = first[0]
    no_call = FakeStandardClient(FakeBatches({}), {})
    with pytest.raises(tc.BatchFailure, match="coût réel .* estimation .*aucun artefact"):
        tc.execute_standard(
            no_call, [plan], configured, guard=tc._CostGuard(0.000001), output=io.StringIO(),
        )
    assert no_call.messages.calls == []


def test_garde_cout_concurrente_reserve_avant_chaque_create() -> None:
    doc = miniature()
    configured = settings(type_clauses_max_blocks_per_request=2,
                          type_clauses_standard_concurrency=2)
    plans = tc.requests_for(doc, doc.blocks, 1, configured)
    estimates = [tc.standard_majorant_eur(plan, configured) for plan in plans]
    actual = cost_from_usage(
        tc.MODEL, {"input_tokens": 100, "output_tokens": 20}, configured.usd_eur, batch=False,
    ).cost_eur
    ceiling = max(estimates) + actual + 0.0001
    assert all(estimate < ceiling for estimate in estimates) and sum(estimates) > ceiling

    class BlockingMessages(FakeStandardMessages):
        def __init__(self) -> None:
            super().__init__(FakeBatches({}), {})
            self.started = 0
            self.lock = Lock()
            self.first_started = Event()
            self.second_started = Event()
            self.release = Event()

        def create(self, **params: Any) -> Any:
            with self.lock:
                self.started += 1
                (self.first_started if self.started == 1 else self.second_started).set()
            assert self.release.wait(2)
            return super().create(**params)

    messages = BlockingMessages()
    client = SimpleNamespace(messages=messages)
    guard = tc._CostGuard(ceiling)
    outcome: list[object] = []

    def run_standard() -> None:
        try:
            outcome.append(tc.execute_standard(
                client, plans, configured, guard=guard, output=io.StringIO(),
            ))
        except BaseException as exc:  # pragma: no cover - rendu explicite dans l'assertion
            outcome.append(exc)

    worker = Thread(target=run_standard)
    worker.start()
    assert messages.first_started.wait(1)
    assert not messages.second_started.wait(0.05)
    messages.release.set()
    worker.join(2)

    assert not worker.is_alive() and outcome and not isinstance(outcome[0], BaseException)
    assert messages.started == 2 and guard.spent <= ceiling and guard.reserved == 0


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_lecture_2_standard_interrompue_ne_publie_rien(
    tmp_path: Path, stop_reason: str,
) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=2,
                          type_clauses_standard_concurrency=1)
    campaign = "c" * 64
    first = tc.requests_for(doc, doc.blocks, 1, configured, campaign_override=campaign)
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    batches = FakeBatches(kinds)
    batches._by_id["complete-first"] = [plan.request for plan in first]
    client = FakeStandardClient(
        batches, kinds, interrupted_reading=2, interrupted_stop_reason=stop_reason,
    )
    tracked = [doc_dir / "document.json", doc_dir / "report.json", doc_dir.parent / "manifest.json"]
    before = {path: path.read_bytes() for path in tracked}

    with pytest.raises(tc.BatchFailure, match="premier échec terminal"):
        tc.run(
            doc_dir, settings=configured, client=client, max_cost=12,
            first_batch_id="complete-first", transport="standard-resume",
            resume_campaign=campaign,
            resume_payload_sha256=tc.resume_payload_fingerprint(first, configured),
            resume_justification="fixture : lecture 1 identique, lecture 2 standard interrompue",
            output=io.StringIO(),
        )

    assert client.messages.calls and {path: path.read_bytes() for path in tracked} == before


def test_reprise_refuse_un_payload_different_avant_tout_appel_standard(tmp_path: Path) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=2)
    campaign = "d" * 64
    original_plans = tc.requests_for(doc, doc.blocks, 1, configured, campaign_override=campaign)
    original_proof = tc.resume_payload_fingerprint(original_plans, configured)

    changed_blocks = list(doc.blocks)
    changed_blocks[0] = changed_blocks[0].model_copy(update={"text": "Payload courant différent."})
    changed = Document.model_validate(doc.model_copy(update={"blocks": changed_blocks}).model_dump())
    changed_text = document_json(changed)
    (doc_dir / "document.json").write_text(changed_text, "utf-8")
    manifest_path = doc_dir.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["contrat"]["document_hash"] = hashlib.sha256(changed_text.encode()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), "utf-8")

    batches = FakeBatches({})
    batches._by_id["old-first"] = [plan.request for plan in original_plans]
    client = FakeStandardClient(batches, {})
    with pytest.raises(ValueError, match="certificat du manifeste payload"):
        tc.run(
            doc_dir, settings=configured, client=client, max_cost=12,
            first_batch_id="old-first", transport="standard-resume",
            resume_campaign=campaign, resume_payload_sha256=original_proof,
            resume_justification="fixture : cette justification ne rend pas le payload identique",
            output=io.StringIO(),
        )
    assert client.messages.calls == [] and batches.created == []


def test_transport_programmatique_inconnu_est_refuse_avant_appel(tmp_path: Path) -> None:
    doc_dir, _doc = write_data(tmp_path)
    batches = FakeBatches({})
    client = FakeStandardClient(batches, {})
    with pytest.raises(ValueError, match="transport inconnu"):
        tc.run(
            doc_dir, settings=settings(), client=client, transport="surprise",  # type: ignore[arg-type]
            output=io.StringIO(),
        )
    assert client.messages.calls == [] and batches.created == []


def test_premier_echec_terminal_n_amorce_aucune_future_supplementaire() -> None:
    doc = miniature()
    configured = settings(type_clauses_max_blocks_per_request=1,
                          type_clauses_standard_concurrency=2,
                          type_clauses_standard_max_retries=1)
    plans = tc.requests_for(doc, doc.blocks, 1, configured)

    class StopMessages(FakeStandardMessages):
        def __init__(self) -> None:
            super().__init__(FakeBatches({}), {})
            self.started = 0
            self.lock = Lock()

        def create(self, **params: Any) -> Any:
            with self.lock:
                self.started += 1
                current = self.started
            if current == 1:
                raise RetryableStandardError(400)
            return super().create(**params)

    messages = StopMessages()
    guard = tc._CostGuard(12)
    with pytest.raises(tc.BatchFailure, match="premier échec terminal"):
        tc.execute_standard(
            SimpleNamespace(messages=messages), plans, configured,
            guard=guard, output=io.StringIO(),
        )
    assert messages.started <= configured.type_clauses_standard_concurrency
    assert guard.spent <= guard.ceiling and guard.reserved == 0


def test_reprise_incompatible_echoue_et_voie_legacy_auditee_ne_soumet_rien(tmp_path: Path) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings()
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    batches = FakeBatches(kinds)
    first = tc.requests_for(doc, doc.blocks, 1, configured, legacy_custom_ids=True)
    legal = [doc.block(block_id) for block_id in kinds]
    second = tc.requests_for(doc, legal, 2, configured, legacy_custom_ids=True)
    batches._by_id["existing-first"] = [plan.request for plan in first]
    batches._by_id["existing-second"] = [plan.request for plan in second]
    tracked = [doc_dir / "document.json", doc_dir / "report.json", doc_dir.parent / "manifest.json"]
    before = {path: path.read_bytes() for path in tracked}

    with pytest.raises(tc.BatchFailure, match="custom_id inconnu"):
        tc.run(doc_dir, settings=configured, client=FakeClient(batches), first_batch_id="existing-first",
               second_batch_id="existing-second", output=io.StringIO())
    assert {path: path.read_bytes() for path in tracked} == before

    output = io.StringIO()
    campaign = tc.campaign_fingerprint(doc, configured)
    result = tc.run(
        doc_dir, settings=configured, client=FakeClient(batches), first_batch_id="existing-first",
        second_batch_id="existing-second", legacy_resume=campaign, output=output,
    )
    assert result is not None and batches.created == []
    assert "reprise legacy auditée" in output.getvalue() and campaign in output.getvalue()


def test_reprise_legacy_exige_empreinte_courante_avant_de_lire_un_lot(tmp_path: Path) -> None:
    doc_dir, _ = write_data(tmp_path)
    batches = FakeBatches({})
    with pytest.raises(ValueError, match="legacy-resume incompatible"):
        tc.run(
            doc_dir, settings=settings(), client=FakeClient(batches), first_batch_id="existing-first",
            second_batch_id="existing-second", legacy_resume="0" * 64, output=io.StringIO(),
        )
    assert batches.created == []


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_main_refuse_max_cost_non_positif_ou_non_fini_avant_toute_action(
    tmp_path: Path, value: str, capsys: pytest.CaptureFixture[str],
) -> None:
    batches = FakeBatches({})
    code = tc.main(
        ["contrat", "--data", str(tmp_path), f"--max-cost={value}"],
        client=FakeClient(batches), settings=settings(), output=io.StringIO(),
    )
    assert code == 2 and batches.created == []
    assert "nombre fini > 0" in capsys.readouterr().err


def _attester_avant_typage(doc_dir: Path) -> tuple[str, str]:
    """Le rapport d'un document réellement ingéré : les deux attestations de structure de 4.5.

    Rend `(structure_hash, ingest_fingerprint)` — les deux valeurs qui doivent **survivre** au
    typage, à côté d'un `document_hash` qui, lui, change.
    """
    from server.app.domain.ingest import detail_attestation_arbre, detail_attestation_structure

    manifest_path = doc_dir.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    entry = manifest["contrat"]
    structure_text = '{"doc_id": "contrat", "noeuds": []}\n'
    (doc_dir / "structure.json").write_text(structure_text, "utf-8")
    structure_hash = hashlib.sha256(structure_text.encode()).hexdigest()
    entry["structure_hash"] = structure_hash
    manifest_path.write_text(json.dumps(manifest), "utf-8")
    (doc_dir / "report.json").write_text(json.dumps(Report(
        doc_id="contrat",
        checks=[
            Check(name="invariants_arbre", level="info",
                  detail=detail_attestation_arbre(document_hash=entry["document_hash"],
                                                  ingest_fingerprint=entry["ingest_fingerprint"])),
            Check(name="structure_proposee", level="info",
                  detail=detail_attestation_structure(document_hash=entry["document_hash"],
                                                      structure_hash=structure_hash)),
        ],
        stats={"pages_charabia": {}},
    ).model_dump()), "utf-8")
    return structure_hash, entry["ingest_fingerprint"]


def test_les_attestations_de_structure_survivent_au_typage(tmp_path: Path) -> None:
    """Story 4.5 (revue B4) : le typage réécrit `report.json` — la preuve de structure doit rester vraie.

    `enrich_typing_report` conserve les checks qui ne sont pas des checks de typage, donc les deux
    attestations traversaient déjà la réécriture **en tant que texte**. Mais le typage change
    `document.json`, donc le `document_hash` que le manifest porte : telles quelles, les attestations
    nommaient l'ancien arbre et ne décrivaient plus rien. Le gate `full` aurait alors vu la preuve
    simplement absente — un document typé perdant sa preuve de structure **sans que rien ne le dise**.

    Ce que le test vérifie est exactement le prédicat du gate : le couple attesté est celui de
    l'entrée du manifest écrite par ce même typage.
    """
    from server.app.domain.ingest import lire_attestation_arbre, lire_attestation_structure

    doc_dir, _doc = write_data(tmp_path)
    structure_hash, fingerprint = _attester_avant_typage(doc_dir)
    avant = json.loads((doc_dir.parent / "manifest.json").read_text("utf-8"))["contrat"]

    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    client = FakeStandardClient(FakeBatches(kinds), kinds)
    result = tc.run(doc_dir, settings=settings(type_clauses_standard_concurrency=2), client=client,
                    transport="standard", max_cost=12, output=io.StringIO())
    assert result is not None

    entry = json.loads((doc_dir.parent / "manifest.json").read_text("utf-8"))["contrat"]
    # Le typage a bien changé l'arbre : sans cela, le test ne prouverait rien.
    assert entry["document_hash"] != avant["document_hash"]
    assert entry["document_hash"] == hashlib.sha256(
        (doc_dir / "document.json").read_bytes()).hexdigest()
    assert entry["structure_hash"] == structure_hash

    report = Report.model_validate_json((doc_dir / "report.json").read_bytes())
    arbre = next(c for c in report.checks if c.name == "invariants_arbre")
    structure = next(c for c in report.checks if c.name == "structure_proposee")
    assert lire_attestation_arbre(arbre.detail) == (entry["document_hash"], fingerprint)
    assert lire_attestation_structure(structure.detail) == (entry["document_hash"], structure_hash)
    # Une seule attestation par check : la ré-attestation remplace, elle n'empile pas.
    assert arbre.detail.count("document_hash=") == 1
    assert structure.detail.count("document_hash=") == 1


def test_le_typage_ne_fabrique_jamais_une_attestation_absente(tmp_path: Path) -> None:
    """L'autre sens, et il compte autant : absente avant ⇒ absente après.

    `write_data` écrit le rapport tel que le corpus servi le porte aujourd'hui —
    `invariants_arbre: ok`, sans empreinte, et aucun `structure_proposee`. Si le typage attestait
    « par défaut », il fabriquerait une preuve de structure pour un document dont l'ingestion n'en a
    jamais produit : le gate `full` verdirait sur une affirmation que personne n'a vérifiée.
    """
    from server.app.domain.ingest import lire_attestation_arbre, lire_attestation_structure

    doc_dir, _doc = write_data(tmp_path)
    kinds = {"contrat:p1:1": "garantie", "contrat:p2:1": "definition"}
    client = FakeStandardClient(FakeBatches(kinds), kinds)
    assert tc.run(doc_dir, settings=settings(type_clauses_standard_concurrency=2), client=client,
                  transport="standard", max_cost=12, output=io.StringIO()) is not None

    report = Report.model_validate_json((doc_dir / "report.json").read_bytes())
    assert [c.name for c in report.checks if c.name == "structure_proposee"] == []
    arbre = next(c for c in report.checks if c.name == "invariants_arbre")
    assert arbre.detail == "ok"
    assert all(lire_attestation_arbre(c.detail) is None
               and lire_attestation_structure(c.detail) is None for c in report.checks)
