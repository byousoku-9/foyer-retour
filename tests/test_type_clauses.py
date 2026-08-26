from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from server.app.config import Settings
from server.app.domain import Block, BlockRef, Document, ManifestEntry, Node, NodeRef, Report
from server.app.domain.ingest import Check, Gate
from server.app.llm.pricing import cost_from_usage
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
    assert definition.defines == "contenu" and definition.scope_node_id == "contrat:a2"
    exclusion = typed.block("contrat:p3:2")
    assert exclusion.kind == "exclusion" and exclusion.kind_source == "model"
    assert exclusion.scope_node_id == "contrat:a3.1" and exclusion.scope_node_ids == ["contrat:a3.1"]
    assert exclusion.unresolved_refs == ["99"]
    assert exclusion.relation.exception_de == "contrat:p2:1"  # T1 conservé malgré le désaccord T2
    assert typed.node_scope_kind("contrat:a3") == "extension"
    assert typed.node_scope_kind("contrat:a3.1") == "special"  # signal enfant le plus proche prioritaire
    assert [(b.block_id, b.text, b.lines, b.bbox) for b in typed.blocks] == \
        [(b.block_id, b.text, b.lines, b.bbox) for b in doc.blocks]


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
