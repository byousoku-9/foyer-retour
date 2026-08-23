"""AD-7 — le loader lit `data/` en lecture seule, recalcule les hashes et met en quarantaine par document."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
from server.app.domain import GateContext
from server.ingest import kb_to_blocks as k

ROOT = Path(__file__).resolve().parents[1]
MINI = Path(__file__).parent / "data" / "mini_kb.js"


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "lux-guide"
    d.mkdir(parents=True)
    shutil.copy(MINI, d / "source.js")
    k.run(d, edition="git:test")
    return d.parent


def _manifest(data: Path) -> dict:
    return json.loads((data / "manifest.json").read_text("utf-8"))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _rename_doc(doc_dir: Path, doc_id: str, *, block_ids: bool = False) -> None:
    """Change `doc_id` (et, si demandé, le préfixe des block_id pour rester un Document valide)."""
    p = doc_dir / "document.json"
    text = p.read_text("utf-8").replace('"doc_id": "lux-guide"', f'"doc_id": "{doc_id}"')
    if block_ids:
        text = text.replace('"lux-guide:', f'"{doc_id}:')
    p.write_text(text, "utf-8")


def _set_hash(data: Path, doc_id: str = "lux-guide") -> None:
    m = _manifest(data)
    m[doc_id]["document_hash"] = _sha(data / doc_id / "document.json")
    _write_manifest(data, m)


def _write_manifest(data: Path, m: dict) -> None:
    (data / "manifest.json").write_text(json.dumps(m), "utf-8")


def test_ungated_document_served_with_alert_or_quarantined(data: Path) -> None:
    c = load_corpus(data, allow_ungated=True)
    assert c.quarantine == {} and c.alerts == {"lux-guide": ["sans_gate"]} and c.served == ["lux-guide"]
    doc = c.documents["lux-guide"]
    assert doc.edition == "git:test" and all(b.text_norm == normalize(b.text) for b in doc.blocks)
    assert c.summaries["lux-guide"].startswith("<!-- lux-guide")
    c = load_corpus(data, allow_ungated=False)
    assert c.documents == {} and c.quarantine == {"lux-guide": "sans_gate"}


def test_modified_document_json_is_quarantined_alone(data: Path) -> None:
    m = _manifest(data)
    other = data / "autre-doc"
    shutil.copytree(data / "lux-guide", other)
    _rename_doc(other, "autre-doc", block_ids=True)
    m["autre-doc"] = dict(m["lux-guide"]) | {"document_hash": _sha(other / "document.json")}
    _write_manifest(data, m)
    p = data / "lux-guide" / "document.json"
    p.write_text(p.read_text("utf-8").replace("Les huit premiers jours", "Les neuf premiers jours"), "utf-8")
    c = load_corpus(data, allow_ungated=True)
    assert c.quarantine == {"lux-guide": "document_hash différent du manifest"}
    assert c.served == ["autre-doc"] and c.alerts == {"autre-doc": ["sans_gate"]}


def test_modified_source_is_quarantined(data: Path) -> None:
    (data / "lux-guide" / "source.js").write_bytes(b"window.KB = {};")
    assert load_corpus(data, allow_ungated=True).quarantine == {"lux-guide": "source_hash différent du manifest (source.js)"}


def test_manifest_status_quarantaine_is_not_loaded(data: Path) -> None:
    m = _manifest(data)
    m["lux-guide"]["status"] = "quarantaine"
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).quarantine == {"lux-guide": "quarantaine (manifest)"}


def _gate(e: dict, **over) -> dict:
    return {"profile": "vertical", "source_hash": e["source_hash"], "ingest_fingerprint": e["ingest_fingerprint"],
            "cases_hash": "c", "pipeline_digest": "p", "prompts_digest": "q", "model_ids": {"micro": "m"},
            "evals_ok": True, "date": "2026-08-23", "overlay_hash": e.get("overlay_hash")} | over


def test_valid_gate_serves_without_alert(data: Path) -> None:
    m = _manifest(data)
    m["lux-guide"]["gate"] = _gate(m["lux-guide"])
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=False).alerts == {"lux-guide": []}
    same = GateContext(pipeline_digest="p", prompts_digest="q", model_ids={"micro": "m"})
    assert load_corpus(data, allow_ungated=False, current=same).alerts == {"lux-guide": []}


def test_failed_gate_is_never_served(data: Path) -> None:
    m = _manifest(data)
    m["lux-guide"]["gate"] = _gate(m["lux-guide"], evals_ok=False)
    _write_manifest(data, m)
    for allow in (False, True):
        assert load_corpus(data, allow_ungated=allow).quarantine == {"lux-guide": "gate_echoue"}


@pytest.mark.parametrize("field", ["source_hash", "ingest_fingerprint", "overlay_hash"])
def test_gate_with_other_hashes_is_invalid_hence_sans_gate(data: Path, field: str) -> None:
    m = _manifest(data)
    m["lux-guide"]["gate"] = _gate(m["lux-guide"], **{field: "ancien"})
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=False).quarantine == {"lux-guide": "sans_gate"}
    c = load_corpus(data, allow_ungated=True)
    assert c.alerts == {"lux-guide": ["sans_gate"]} and c.served == ["lux-guide"]


@pytest.mark.parametrize("current", [
    GateContext(pipeline_digest="autre", prompts_digest="q", model_ids={"micro": "m"}),
    GateContext(pipeline_digest="p", prompts_digest="autre", model_ids={"micro": "m"}),
    GateContext(pipeline_digest="p", prompts_digest="q", model_ids={"micro": "autre"}),
])
def test_gate_perime_only_against_current_image(data: Path, current: GateContext) -> None:
    m = _manifest(data)
    m["lux-guide"]["gate"] = _gate(m["lux-guide"])
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=False, current=current).alerts == {"lux-guide": ["gate_perime"]}
    assert load_corpus(data, allow_ungated=False).alerts == {"lux-guide": []}  # sans `current`, pas de comparaison


def test_manifest_fingerprint_mismatch_quarantines(data: Path) -> None:
    m = _manifest(data)
    m["lux-guide"]["ingest_fingerprint"] = "faux"
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).quarantine == {"lux-guide": "ingest_fingerprint du document différent du manifest"}


def test_invalid_manifest_entry_quarantines_only_that_doc(data: Path) -> None:
    m = _manifest(data)
    m["autre-doc"] = {"status": "bizarre"}
    _write_manifest(data, m)
    c = load_corpus(data, allow_ungated=True)
    assert c.served == ["lux-guide"] and c.quarantine["autre-doc"].startswith("entrée de manifest invalide : ")
    assert "autre-doc" not in c.manifest


def test_missing_manifest_gives_empty_corpus(tmp_path: Path) -> None:
    c = load_corpus(tmp_path, allow_ungated=True)
    assert c.documents == {} and c.quarantine == {}


def test_repo_data_loads() -> None:
    c = load_corpus(ROOT / "data", allow_ungated=True)
    axa = ["sans_gate"] + ([] if (ROOT / "data" / "axa-lu-optihome-2017" / "source.pdf").is_file() else ["source_absente"])
    assert c.quarantine == {} and c.alerts == {"axa-lu-optihome-2017": axa, "lux-guide": ["sans_gate"]}
    doc = c.documents["lux-guide"]
    assert doc.doc_id == "lux-guide" and doc.edition == "git:a8e8593" and len(doc.blocks) > 400
    assert all(b.text_norm == normalize(b.text) for b in doc.blocks)


def test_each_inconsistency_has_its_reason(data: Path) -> None:
    doc_dir = data / "lux-guide"
    p = doc_dir / "document.json"
    original = p.read_bytes()

    p.unlink()
    assert load_corpus(data, allow_ungated=True).quarantine == {"lux-guide": "document.json absent"}

    p.write_text(original.decode("utf-8").replace('"lux-guide:farrivee:1"', '"lux-guide:farrivee:1x"', 1), "utf-8")
    _set_hash(data)
    assert load_corpus(data, allow_ungated=True).quarantine["lux-guide"].startswith("document.json invalide :")

    p.write_text("{pas du json", "utf-8")
    _set_hash(data)
    assert load_corpus(data, allow_ungated=True).quarantine["lux-guide"].startswith("document.json invalide :")

    p.write_bytes(original)
    _rename_doc(doc_dir, "autre-id", block_ids=True)
    _set_hash(data)
    assert load_corpus(data, allow_ungated=True).quarantine == {"lux-guide": "doc_id 'autre-id' différent de la clé du manifest"}

    p.write_bytes(original)
    _set_hash(data)
    (doc_dir / "source.js").unlink()
    m = _manifest(data)
    m["lux-guide"]["source_hash"] = "autre"
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).quarantine == {"lux-guide": "source_hash du document différent du manifest"}
    m["lux-guide"]["source_hash"] = json.loads(original)["source_hash"]
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).alerts == {"lux-guide": ["sans_gate", "source_absente"]}

    m["lux-guide"]["edition"] = "git:autre"
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).quarantine["lux-guide"].startswith("edition 'git:test' différente")
    m["lux-guide"]["edition"] = "git:test"
    _write_manifest(data, m)

    (doc_dir / "summary.md").unlink()
    assert load_corpus(data, allow_ungated=True).quarantine == {"lux-guide": "sommaire_absent"}


def test_invalid_manifest_gives_empty_corpus_with_reason(data: Path) -> None:
    (data / "manifest.json").write_text("{", "utf-8")
    c = load_corpus(data, allow_ungated=True)
    assert c.documents == {} and c.quarantine["*"].startswith("manifest invalide : ")
    (data / "manifest.json").write_text("[1, 2]", "utf-8")
    assert load_corpus(data, allow_ungated=True).quarantine["*"].startswith("manifest invalide : ")


def test_folder_name_must_match_doc_id(data: Path) -> None:
    m = _manifest(data)
    m["autre-doc"] = m.pop("lux-guide")
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).quarantine == {"autre-doc": "document.json absent"}


# --- overlay `typing.manual.json` (story 1.2, FR20) -------------------------------------------------------------

def _overlay(data: Path, blocks: dict, *, declare: bool = True, **extra) -> None:
    """Écrit l'overlay et, comme le ferait une relance de l'ingestion, son empreinte dans le manifest."""
    path = data / "lux-guide" / "typing.manual.json"
    path.write_text(json.dumps({"schema_version": "1", "doc_id": "lux-guide", **extra, "blocks": blocks}), "utf-8")
    if declare:
        m = _manifest(data)
        m["lux-guide"]["overlay_hash"] = _sha(path)
        _write_manifest(data, m)


def test_overlay_is_merged_before_validation_without_touching_document_json(data: Path) -> None:
    before = _sha(data / "lux-guide" / "document.json")
    _overlay(data, {"lux-guide:farrivee:2": {"kind": "definition", "defines": "arrivée", "kind_source": "manual",
                                             "scope_node_id": "lux-guide:farrivee",
                                             "scope_node_ids": ["lux-guide:farrivee"]}}, note="test")
    c = load_corpus(data, allow_ungated=True)
    assert c.quarantine == {}
    b = c.documents["lux-guide"].block("lux-guide:farrivee:2")
    assert b.kind == "definition" and b.defines == "arrivée" and b.kind_confirmed and b.scope_node_id == "lux-guide:farrivee"
    assert b.scope_node_ids == ["lux-guide:farrivee"]
    assert b.text_norm == normalize(b.text)
    assert _sha(data / "lux-guide" / "document.json") == before


def test_overlay_is_covered_by_manifest_and_gate(data: Path) -> None:
    """Revue Codex 1.2 (B7) : kind et portée ne changent pas après les évals sans quarantaine ni perte du gate."""
    entry = {"lux-guide:farrivee:2": {"kind": "definition", "defines": "arrivée", "kind_source": "manual"}}
    _overlay(data, entry, declare=False)
    assert "non déclaré dans le manifest" in load_corpus(data, allow_ungated=True).quarantine["lux-guide"]
    _overlay(data, entry)
    m = _manifest(data)
    m["lux-guide"]["gate"] = _gate(m["lux-guide"])
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=False).alerts == {"lux-guide": []}
    # l'overlay est modifié sans relancer l'ingestion ⇒ quarantaine
    (data / "lux-guide" / "typing.manual.json").write_text(json.dumps(
        {"schema_version": "1", "doc_id": "lux-guide", "blocks": {"lux-guide:farrivee:2": {"kind": "exclusion", "scope_node_id": "lux-guide:farrivee", "kind_source": "manual"}}}), "utf-8")
    assert load_corpus(data, allow_ungated=False).quarantine == {"lux-guide": "overlay_hash différent du manifest (relancer l'ingestion)"}
    # relance de l'ingestion (manifest mis à jour) ⇒ le gate ne couvre plus cet overlay : sans_gate
    m = _manifest(data)
    m["lux-guide"]["overlay_hash"] = _sha(data / "lux-guide" / "typing.manual.json")
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=False).quarantine == {"lux-guide": "sans_gate"}
    # overlay déclaré mais absent
    (data / "lux-guide" / "typing.manual.json").unlink()
    assert load_corpus(data, allow_ungated=True).quarantine == {"lux-guide": "overlay : déclaré dans le manifest mais absent"}


@pytest.mark.parametrize("blocks, fragment", [
    ({"lux-guide:fnope:1": {"kind": "definition", "kind_source": "manual"}}, "bloc inconnu"),
    ({"lux-guide:farrivee:2": {"kind": "definition", "kind_source": "model"}}, "kind_source ≠ manual"),
    ({"lux-guide:farrivee:2": {"kind": "definition"}}, "kind_source ≠ manual"),
    ({"lux-guide:farrivee:2": {"kind": "definition", "kind_source": "manual", "scope_node_id": "lux-guide:x"}}, "nœud inconnu"),
    ({"lux-guide:farrivee:2": {"kind": "definition", "kind_source": "manual", "text": "remplacé"}}, "champs inattendus"),
    ({"lux-guide:farrivee:2": {"kind": "heading!", "kind_source": "manual"}}, "kind inconnu"),
    # champs obligatoires par kind (revue Codex 1.2, I3 tour 2)
    ({"lux-guide:farrivee:2": {"kind": "definition", "kind_source": "manual"}}, "defines obligatoire pour un bloc definition"),
    ({"lux-guide:farrivee:2": {"kind": "definition", "defines": "", "kind_source": "manual"}}, "defines obligatoire"),
    ({"lux-guide:farrivee:2": {"kind": "exclusion", "kind_source": "manual"}}, "scope_node_id obligatoire pour un bloc exclusion"),
    ({"lux-guide:farrivee:2": {"kind": "garantie", "kind_source": "manual"}}, "scope_node_id obligatoire"),
    ({"lux-guide:farrivee:2": {"kind_source": "manual"}}, "kind obligatoire"),
    ({"lux-guide:farrivee:2": {"kind": "definition", "kind_source": "manual", "scope_node_ids": ["lux-guide:x"]}},
     "scope_node_ids"),
    ({}, "aucun bloc typé"),
])
def test_invalid_overlay_quarantines_only_this_document(data: Path, blocks: dict, fragment: str) -> None:
    _overlay(data, blocks)
    c = load_corpus(data, allow_ungated=True)
    assert list(c.quarantine) == ["lux-guide"] and fragment in c.quarantine["lux-guide"]


@pytest.mark.parametrize("extra, fragment", [
    ({"schema_version": "2"}, "schema_version"),
    ({"doc_id": "autre"}, "doc_id"),
    ({"texte": "x"}, "champs inattendus"),
])
def test_overlay_header_is_strict(data: Path, extra: dict, fragment: str) -> None:
    path = data / "lux-guide" / "typing.manual.json"
    path.write_text(json.dumps({"schema_version": "1", "doc_id": "lux-guide", **extra,
                                "blocks": {"lux-guide:farrivee:2": {"kind": "definition", "defines": "arrivée", "kind_source": "manual"}}}), "utf-8")
    m = _manifest(data)
    m["lux-guide"]["overlay_hash"] = _sha(path)
    _write_manifest(data, m)
    assert fragment in load_corpus(data, allow_ungated=True).quarantine["lux-guide"]


def test_overlay_unreadable_or_malformed(data: Path) -> None:
    path = data / "lux-guide" / "typing.manual.json"
    path.write_text("{", "utf-8")
    m = _manifest(data)
    m["lux-guide"]["overlay_hash"] = _sha(path)
    _write_manifest(data, m)
    assert "overlay illisible" in load_corpus(data, allow_ungated=True).quarantine["lux-guide"]
    path.write_text("[]", "utf-8")
    m["lux-guide"]["overlay_hash"] = _sha(path)
    _write_manifest(data, m)
    assert "overlay : objet" in load_corpus(data, allow_ungated=True).quarantine["lux-guide"]
