"""AD-7 — le loader lit `data/` en lecture seule, recalcule les hashes et met en quarantaine par document."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
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


def _rename_doc(doc_dir: Path, doc_id: str) -> None:
    p = doc_dir / "document.json"
    p.write_text(p.read_text("utf-8").replace('"doc_id": "lux-guide"', f'"doc_id": "{doc_id}"'), "utf-8")


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
    _rename_doc(other, "autre-doc")
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


def test_gate_present_serves_without_alert_and_stale_gate_alerts(data: Path) -> None:
    m = _manifest(data)
    e = m["lux-guide"]
    e["gate"] = {"profile": "vertical", "source_hash": e["source_hash"], "ingest_fingerprint": e["ingest_fingerprint"],
                 "cases_hash": "c", "pipeline_digest": "p", "prompts_digest": "q", "model_ids": {}, "evals_ok": True,
                 "date": "2026-08-23"}
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=False).alerts == {"lux-guide": []}
    e["gate"]["source_hash"] = "ancien"
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=False).alerts == {"lux-guide": ["gate_perime"]}


def test_missing_manifest_gives_empty_corpus(tmp_path: Path) -> None:
    c = load_corpus(tmp_path, allow_ungated=True)
    assert c.documents == {} and c.quarantine == {}


def test_repo_data_loads() -> None:
    c = load_corpus(ROOT / "data", allow_ungated=True)
    assert c.quarantine == {} and c.alerts == {"lux-guide": ["sans_gate"]}
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
    _rename_doc(doc_dir, "autre-id")
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
    (data / "manifest.json").write_text(json.dumps({"lux-guide": {"status": "bizarre"}}), "utf-8")
    assert load_corpus(data, allow_ungated=True).quarantine["*"].startswith("manifest invalide : ")


def test_folder_name_must_match_doc_id(data: Path) -> None:
    m = _manifest(data)
    m["autre-doc"] = m.pop("lux-guide")
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).quarantine == {"autre-doc": "document.json absent"}
