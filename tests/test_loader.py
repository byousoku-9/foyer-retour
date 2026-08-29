"""AD-7 — le loader lit `data/` en lecture seule, recalcule les hashes et met en quarantaine par document."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest

from server.app.corpus import loader
from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
from server.app.domain import Document, GateContext
from server.ingest import kb_to_blocks as k

ROOT = Path(__file__).resolve().parents[1]
# Marge minimale exigée entre le périmètre réel du guide et `perimetre_max_chars` (story 2.1, revue
# coordonnée). Mesure du jour : 3 004 caractères sur 4 000, soit **996 de libres** — 25 % du plafond.
# L'alarme est à 15 % (3 400 caractères) : elle laisse ~400 caractères de croissance avant de sonner,
# donc six à huit titres de fiche, et il reste encore 600 caractères de vrai répit après. Plus haut,
# elle rougirait à chaque fiche ajoutée ; plus bas, elle sonnerait trop près de la coupure pour
# qu'on ait le temps de faire autre chose que relever le seuil dans l'urgence.
PERIMETRE_MARGE_MIN = 0.15
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
            "cases_hash": "c", "cases": 1, "countersigned": True,
            "pipeline_digest": "p", "prompts_digest": "q",
            "model_ids": {"micro": "m"},
            "evals_ok": True, "date": "2026-08-23", "overlay_hash": e.get("overlay_hash")} | over


@pytest.mark.parametrize("cases", [None, 0, -1])
def test_un_gate_sans_compte_de_cas_credible_met_le_document_en_quarantaine(data: Path,
                                                                           cases: int | None) -> None:
    """Revue Codex 1.10, I3 : le plancher appartient au domaine, pas à deux clients.

    `Gate.cases` était optionnel avec un défaut à 0. Un gate écrit à la main sans ce champ était donc
    accepté, le document servi, et `/api/v1/sante` publiait `gate_profile: "vertical"` avec
    `gate_cases: 0` — un corps que les deux fronts déclarent illisible (le runner refuse de tourner
    sur zéro cas, donc aucun run ne peut le produire). Un serveur vivant faisait dire aux pages
    « le serveur n'a pas répondu ». Un gate qui n'est pas écrit par un run est désormais une entrée
    invalide, et ce seul document part en quarantaine (AD-7).
    """
    m = _manifest(data)
    gate = _gate(m["lux-guide"])
    if cases is None:
        gate.pop("cases")
    else:
        gate["cases"] = cases
    m["lux-guide"]["gate"] = gate
    _write_manifest(data, m)
    for allow in (False, True):
        corpus = load_corpus(data, allow_ungated=allow)
        assert corpus.served == []
        assert "entrée de manifest invalide" in corpus.quarantine["lux-guide"]


def test_un_gate_qui_ne_dit_pas_la_contresignature_met_le_document_en_quarantaine(
        data: Path) -> None:
    """Revue Codex 1.10 tour 2, B2 : la phrase publiée par `/` bascule sur ce champ.

    `Gate.countersigned` dit si la relecture qu'AD-14 met dans la définition de `vertical` a été
    contresignée par un humain. Un gate qui ne le dit pas laisserait le loader — ou la page —
    choisir à la place du run, alors qu'AD-7 réserve l'écriture du gate au runner ; et le seul
    choix « optimiste » possible ferait afficher « relus à la main » sans qu'aucune main n'ait lu.
    Comme pour `cases`, l'entrée est invalide et ce seul document part en quarantaine.
    """
    m = _manifest(data)
    gate = _gate(m["lux-guide"])
    gate.pop("countersigned")
    m["lux-guide"]["gate"] = gate
    _write_manifest(data, m)
    for allow in (False, True):
        corpus = load_corpus(data, allow_ungated=allow)
        assert corpus.served == []
        assert "entrée de manifest invalide" in corpus.quarantine["lux-guide"]


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
    """Les trois documents dont les gates finaux correspondent à l'image sont servis."""
    c = load_corpus(ROOT / "data", allow_ungated=False)
    axa = [] if (ROOT / "data" / "axa-lu-optihome-2017" / "source.pdf").is_file() else ["source_absente"]
    baloise = ([] if (ROOT / "data" / "baloise-lu-home-2-2024" / "source.pdf").is_file()
               else ["source_absente"])
    assert c.quarantine == {}
    assert c.alerts == {
        "axa-lu-optihome-2017": axa,
        "baloise-lu-home-2-2024": baloise,
        "lux-guide": [],
    }
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


def test_la_borne_injectee_gouverne_la_raison_dun_manifest_illisible(
        data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (data / "manifest.json").write_text("{", "utf-8")
    monkeypatch.setattr(loader, "_read_error", lambda exc: "x" * 200)
    raison = load_corpus(data, allow_ungated=True, raison_max_chars=37).quarantine["*"]
    assert len(raison) == 37


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


def test_la_borne_injectee_gouverne_la_raison_dun_overlay_illisible(
        data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _overlay(data, {"lux-guide:farrivee:2": {"kind": "definition"}})
    chemin = data / "lux-guide" / "typing.manual.json"
    chemin.write_text("{", encoding="utf-8")
    m = _manifest(data)
    m["lux-guide"]["overlay_hash"] = _sha(chemin)
    _write_manifest(data, m)
    monkeypatch.setattr(loader, "_read_error", lambda exc: "x" * 200)
    raison = load_corpus(data, allow_ungated=True, raison_max_chars=43).quarantine["lux-guide"]
    assert len(raison) == 43


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


def test_un_oserror_overlay_ne_publie_jamais_son_chemin(data: Path, monkeypatch: Any) -> None:
    path = data / "lux-guide" / "typing.manual.json"
    path.write_text(json.dumps({
        "schema_version": "1", "doc_id": "lux-guide",
        "blocks": {"lux-guide:farrivee:2": {
            "kind": "definition", "defines": "arrivée", "kind_source": "manual"}},
    }), "utf-8")
    m = _manifest(data)
    m["lux-guide"]["overlay_hash"] = _sha(path)
    _write_manifest(data, m)
    original = Path.read_bytes
    lectures = 0

    def lire(chemin: Path) -> bytes:
        nonlocal lectures
        if chemin == path:
            lectures += 1
            if lectures == 2:  # la première lecture calcule le hash, la seconde parse l'overlay
                raise OSError(f"échec privé sur {chemin}")
        return original(chemin)

    monkeypatch.setattr(Path, "read_bytes", lire)
    raison = load_corpus(data, allow_ungated=True).quarantine["lux-guide"]
    assert raison == "overlay illisible : OSError"
    assert str(data) not in raison


def test_un_oserror_manifest_ne_publie_jamais_son_chemin(data: Path, monkeypatch: Any) -> None:
    manifest = data / "manifest.json"
    original = Path.read_bytes

    def lire(chemin: Path) -> bytes:
        if chemin == manifest:
            raise OSError(f"échec privé sur {chemin}")
        return original(chemin)

    monkeypatch.setattr(Path, "read_bytes", lire)
    raison = load_corpus(data, allow_ungated=True).quarantine["*"]
    assert raison == "manifest invalide : OSError"
    assert str(data) not in raison


def test_un_oserror_document_garde_le_detail_au_log_seulement(
        data: Path, monkeypatch: Any, caplog: Any) -> None:
    document = data / "lux-guide" / "document.json"
    original = Path.read_bytes
    lectures = 0

    def lire(chemin: Path) -> bytes:
        nonlocal lectures
        if chemin == document:
            lectures += 1
            if lectures == 2:  # hash puis parsing du document
                raise OSError(f"échec privé sur {chemin}")
        return original(chemin)

    monkeypatch.setattr(Path, "read_bytes", lire)
    with caplog.at_level(logging.WARNING, logger="foyer.corpus.loader"):
        raison = load_corpus(data, allow_ungated=True).quarantine["lux-guide"]
    assert raison == "document.json illisible : OSError" and str(data) not in raison
    assert str(document) in caplog.text


def test_un_oserror_summary_garde_le_detail_au_log_seulement(
        data: Path, monkeypatch: Any, caplog: Any) -> None:
    summary = data / "lux-guide" / "summary.md"
    original = Path.read_text

    def lire(chemin: Path, *args: Any, **kwargs: Any) -> str:
        if chemin == summary:
            raise OSError(f"échec privé sur {chemin}")
        return original(chemin, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", lire)
    with caplog.at_level(logging.WARNING, logger="foyer.corpus.loader"):
        raison = load_corpus(data, allow_ungated=True).quarantine["lux-guide"]
    assert raison == "summary.md illisible : OSError" and str(data) not in raison
    assert str(summary) in caplog.text


# --- AD-8 : le bloquant statique est une propriété du **loader** (story 1.10, D6) ---------------

def _report(data: Path, checks: list[dict] | str, doc_id: str = "lux-guide") -> None:
    chemin = data / doc_id / "report.json"
    if isinstance(checks, str):
        chemin.write_text(checks, "utf-8")
        return
    chemin.write_text(json.dumps({"doc_id": doc_id, "checks": checks, "stats": {}}), "utf-8")


def test_le_rapport_dingestion_du_depot_ne_porte_aucun_bloquant(data: Path) -> None:
    """Point de départ : sans bloquant, le rapport ne change rien (le guide est servi)."""
    rapport = json.loads((data / "lux-guide" / "report.json").read_text("utf-8"))
    assert [c for c in rapport["checks"] if c["level"] == "bloquant"] == []
    assert load_corpus(data, allow_ungated=True).served == ["lux-guide"]


def test_un_check_bloquant_met_ce_seul_document_en_quarantaine(data: Path) -> None:
    """AD-8 : « un document est servi ssi **aucun bloquant statique** et `gate.evals_ok` ».

    La règle ne tenait jusqu'ici que transitivement, par le `status` que l'ingestion écrit : une main
    sur `manifest.json` (ou une réingestion partielle) remettait « servi » sur un document dont le
    rapport dit « page décisionnelle corrompue ». Le loader relit donc le rapport lui-même.
    """
    _report(data, [{"name": "page_sans_texte", "level": "bloquant", "detail": "p. 12"},
                   {"name": "couverture", "level": "info", "detail": ""}])
    c = load_corpus(data, allow_ungated=True)
    assert c.served == []
    assert c.quarantine["lux-guide"].startswith("bloquant_statique")
    assert "page_sans_texte" in c.quarantine["lux-guide"]


def test_le_bloquant_statique_suit_exclusivement_le_modele_report(data: Path) -> None:
    """D1 : une forme que l'ancien parseur manuel acceptait ne décide plus du service.

    Le check porte bien `level=bloquant`, mais un champ étranger rend le rapport invalide selon
    `Report(extra=forbid)`. Le loader ne lit pas une autre vérité à la main : le rapport entier est
    invalide et la décision fail-closed le met en quarantaine.
    """
    _report(data, [{"name": "page_sans_texte", "level": "bloquant", "detail": "p. 12",
                    "champ_inattendu": True}])
    corpus = load_corpus(data, allow_ungated=True)
    assert corpus.served == []
    assert "rapport_statique_illisible" in corpus.quarantine["lux-guide"]


def test_un_doc_id_trop_long_est_mis_en_quarantaine_par_le_domaine(data: Path) -> None:
    trop_long = "a" * 65
    dossier = data / trop_long
    shutil.copytree(data / "lux-guide", dossier)
    _rename_doc(dossier, trop_long, block_ids=True)
    manifest = _manifest(data)
    manifest[trop_long] = dict(manifest.pop("lux-guide"))
    manifest[trop_long]["document_hash"] = _sha(dossier / "document.json")
    _write_manifest(data, manifest)
    shutil.rmtree(data / "lux-guide")

    corpus = load_corpus(data, allow_ungated=True)
    assert corpus.served == []
    assert corpus.quarantine[trop_long].startswith("document.json invalide : ")


def test_un_bloquant_statique_ne_se_deroge_pas_par_allow_ungated(data: Path) -> None:
    """`ALLOW_UNGATED` déroge à l'absence de questions-témoins, jamais à un document illisible."""
    m = _manifest(data)
    m["lux-guide"]["gate"] = _gate(m["lux-guide"])
    _write_manifest(data, m)
    _report(data, [{"name": "invariant_arbre", "level": "bloquant", "detail": "bloc orphelin"}])
    for allow in (False, True):
        c = load_corpus(data, allow_ungated=allow)
        assert c.served == [] and c.quarantine["lux-guide"].startswith("bloquant_statique")


def test_un_rapport_absent_reste_compatible_avec_le_corpus_historique(data: Path) -> None:
    (data / "lux-guide" / "report.json").unlink()
    corpus = load_corpus(data, allow_ungated=True)
    assert corpus.served == ["lux-guide"] and corpus.quarantine == {}


@pytest.mark.parametrize("contenu", [
    "{ceci n'est pas du JSON",              # illisible : `api/etat` porte déjà l'alerte (D9 de 1.9)
    '[{"name": "x", "level": "bloquant"}]',  # forme inattendue : ce n'est pas un rapport
])
def test_un_rapport_present_mais_invalide_met_le_document_en_quarantaine(
        data: Path, contenu: str) -> None:
    """D1 : présent mais non prouvé conforme ⇒ fail-closed, indépendamment du manifest."""
    _report(data, contenu)
    corpus = load_corpus(data, allow_ungated=True)
    assert corpus.served == []
    assert "rapport_statique_illisible" in corpus.quarantine["lux-guide"]


def test_une_alerte_du_rapport_ne_retire_pas_le_document(data: Path) -> None:
    """AD-8 : seules les alertes de niveau `bloquant` retirent du service — pas `alerte`, pas `info`."""
    _report(data, [{"name": "unresolved_refs", "level": "alerte", "detail": "3 renvois"},
                   {"name": "tdm_pdf", "level": "info", "detail": ""}])
    assert load_corpus(data, allow_ungated=True).served == ["lux-guide"]


def test_un_rapport_etranger_nest_pas_un_bloquant(data: Path) -> None:
    """Un `report.json` qui décrit un **autre** document ne retire pas celui-ci du service.

    Copie de dossier, `doc_id` renommé sans réingestion : ses bloquants ne disent rien de ce
    document-ci, et les lui appliquer mettrait en quarantaine un document sain sur la foi d'un
    fichier qui ne le décrit pas. L'incohérence n'est pas tue pour autant : `api/etat` porte l'alerte
    `rapport_etranger` (revue 1.9).
    """
    _report(data, json.dumps({
        "doc_id": "un-autre-document",
        "checks": [{"name": "page_sans_texte", "level": "bloquant", "detail": "p. 12"}],
        "stats": {}}))
    c = load_corpus(data, allow_ungated=True)
    assert c.served == ["lux-guide"] and c.quarantine == {}


@pytest.mark.parametrize("doc_id, valide", [
    ("a" * 64, True),
    ("a" * 65, False),
    ("doc-valide\n", False),
])
def test_le_domaine_applique_les_vraies_frontieres_du_doc_id(doc_id: str, valide: bool) -> None:
    brut = {"doc_id": doc_id, "kind": "contrat", "title": "Test", "edition": "2026"}
    if valide:
        assert Document.model_validate(brut).doc_id == doc_id
    else:
        with pytest.raises(ValueError, match="doc_id"):
            Document.model_validate(brut)


@pytest.mark.parametrize("module_name, attendu", [
    ("pdf", 2), ("typing", 2), ("fetch", 4), ("dictionary", 2),
])
def test_les_cli_ingestion_refusent_un_doc_id_au_dela_de_la_borne(
        module_name: str, attendu: int, tmp_path: Path, monkeypatch: Any) -> None:
    """Les entrées utilisateur partagent le contrat de 64 caractères avant toute I/O ou API."""
    trop_long = "a" * 65
    if module_name == "pdf":
        from server.ingest.pdf_to_blocks import main
        code = main([trop_long, "--data", str(tmp_path)])
    elif module_name == "typing":
        from server.ingest.type_clauses import main
        code = main([trop_long, "--data", str(tmp_path)])
    elif module_name == "fetch":
        from server.ingest.fetch_source import main
        code = main([trop_long, "--data", str(tmp_path)])
    else:
        from server.ingest import enrich_dictionary
        monkeypatch.setattr(
            enrich_dictionary, "get_settings",
            lambda: (_ for _ in ()).throw(AssertionError(".env ne doit pas être lu")))
        code = enrich_dictionary.main(
            ["--doc-id", trop_long, "--data", str(tmp_path)], settings=None)
    assert code == attendu


def test_une_cle_manifest_avec_newline_ne_peut_pas_forger_une_ligne_de_log(
        data: Path, monkeypatch: Any, caplog: Any) -> None:
    doc_id = "doc\nforge"
    dossier = data / doc_id
    shutil.copytree(data / "lux-guide", dossier)
    manifest = _manifest(data)
    manifest[doc_id] = dict(manifest.pop("lux-guide"))
    _write_manifest(data, manifest)
    cible = dossier / "document.json"
    original = Path.read_bytes

    def lire(chemin: Path) -> bytes:
        if chemin == cible:
            raise OSError("lecture privée impossible")
        return original(chemin)

    monkeypatch.setattr(Path, "read_bytes", lire)
    with caplog.at_level(logging.WARNING, logger="foyer.corpus.loader"):
        corpus = load_corpus(data, allow_ungated=True)
    assert corpus.quarantine[doc_id] == "document.json illisible : OSError"
    messages = [record.getMessage() for record in caplog.records]
    assert any("doc\\nforge" in message for message in messages)
    assert all("doc\nforge" not in message for message in messages)


# --- story 2.1 : le périmètre dérivé du corpus ------------------------------

def test_le_perimetre_projette_les_titres_de_niveau_1_et_leurs_enfants(data: Path) -> None:
    """Reprise différée `target_story: 2.1` : la liste de périmètre de *comprendre* doit venir du
    corpus, pas d'une phrase écrite à la main dans un prompt."""
    corpus = load_corpus(data, allow_ungated=True)
    doc = corpus.documents["lux-guide"]
    lignes = corpus.perimetres["lux-guide"].splitlines()

    categories = [n for n in doc.nodes if n.level == 1 and n.title.strip()]
    assert len(lignes) == len(categories)
    par_id = {n.node_id: n for n in doc.nodes}
    for ligne, node in zip(lignes, categories, strict=True):
        assert ligne.startswith(f"- {node.title}")
        for enfant in node.children:
            assert par_id[enfant].title in ligne
    # AD-10 : aucune projection ne porte le texte d'un bloc — ce sont des titres, écrits par
    # l'ingestion, jamais par un modèle.
    for bloc in doc.blocks:
        if bloc.kind != "heading" and len(bloc.text) > 40:
            assert bloc.text not in corpus.perimetres["lux-guide"]


def test_le_perimetre_borne_perd_le_detail_avant_de_perdre_une_categorie(data: Path) -> None:
    """Revue Codex 2.1 (I2) : la borne retirait des **catégories entières**, en silence.

    Le prompt de *comprendre* dit de cette liste qu'« elle fait foi, aucune autre » : une catégorie
    qui en disparaît fait refuser `hors_perimetre` une question que le guide traite — le faux refus
    même que la story 2.1 corrige, réintroduit par un réglage. La borne perd donc d'abord le
    **détail** (les fiches), ce qui garde la liste *exhaustive*, et ne perd une catégorie qu'en
    dernier ressort — où elle le **dit** (`perimetre_tronque`).
    """
    corpus_entier = load_corpus(data, allow_ungated=True)
    entier = corpus_entier.perimetres["lux-guide"]
    categories = [n for n in corpus_entier.documents["lux-guide"].nodes
                  if n.level == 1 and n.title.strip()]
    assert "perimetre_tronque" not in corpus_entier.alerts["lux-guide"]

    # Palier 2 : trop court d'un seul caractère pour le détail, assez pour toutes les catégories.
    serre = load_corpus(data, allow_ungated=True, perimetre_max_chars=len(entier) - 1)
    court = serre.perimetres["lux-guide"]
    assert court.splitlines() == [f"- {n.title.strip()}" for n in categories]
    assert len(court.splitlines()) == len(entier.splitlines())  # aucune catégorie perdue
    assert "perimetre_tronque" not in serre.alerts["lux-guide"]  # rien à dire : rien n'a disparu

    # Palier 3 : même les titres seuls ne tiennent pas. Des catégories tombent — et l'alerte le dit.
    absurde = load_corpus(data, allow_ungated=True, perimetre_max_chars=len(court) - 1)
    reste = absurde.perimetres["lux-guide"]
    assert 0 < len(reste.splitlines()) < len(categories)
    assert "perimetre_tronque" in absurde.alerts["lux-guide"]
    for ligne in reste.splitlines():
        assert ligne in court.splitlines()  # on retire des lignes, on n'en coupe aucune

    # **La première ligne ne tombe jamais** (revue coordonnée 2.1). Elle peut donc dépasser la borne :
    # c'est assumé, et c'est le moindre mal. Le prompt de *comprendre* affirme juste après cette
    # liste « c'est la liste qui fait foi, aucune autre » — un périmètre **vide** ferait alors de
    # tout un hors-périmètre, c'est-à-dire le faux refus que la story vient de corriger, en pire et
    # sur toutes les questions. Un périmètre trop court est un réglage à revoir, qu'un test de dépôt
    # signale bien avant ; un périmètre vide est une panne.
    minimal = load_corpus(data, allow_ungated=True, perimetre_max_chars=1)
    minuscule = minimal.perimetres["lux-guide"]
    assert minuscule == f"- {categories[0].title.strip()}"
    assert "\n" not in minuscule  # une seule ligne, jamais coupée en son milieu
    assert "perimetre_tronque" in minimal.alerts["lux-guide"]


def test_le_defaut_du_loader_est_celui_de_settings() -> None:
    """`corpus` n'importe pas `config` (table des couches) : le littéral est recopié, donc il se
    vérifie. Sans ce test, régler `PERIMETRE_MAX_CHARS` ne changerait que la moitié du système."""
    from server.app.config import Settings
    from server.app.corpus.loader import PERIMETRE_MAX_CHARS

    assert PERIMETRE_MAX_CHARS == Settings(_env_file=None).perimetre_max_chars


def test_un_document_en_quarantaine_na_pas_de_perimetre(data: Path) -> None:
    """Un document non chargé n'est pas servi (AD-7) : lui prêter un périmètre reviendrait à
    annoncer à *comprendre* des fiches que personne ne peut ouvrir."""
    m = _manifest(data)
    m["lux-guide"]["status"] = "quarantaine"
    _write_manifest(data, m)
    corpus = load_corpus(data, allow_ungated=True)
    assert corpus.perimetres == {} and corpus.documents == {}


def test_le_perimetre_reel_du_guide_garde_une_marge_sous_son_seuil() -> None:
    """Le périmètre **livré** doit rester loin de `perimetre_max_chars`, et le dire avant la coupure.

    Mesuré (revue coordonnée 2.1) : le guide rend **3 004 caractères sur les 4 000** du seuil, pour
    10 catégories et 77 enfants directs — les 39 fiches plus les 38 entrées de « Questions
    fréquentes ». Le commentaire de `config.py` annonçait « 39 fiches, facteur trois de marge » ;
    il reste en réalité 996 caractères. La marge se dit ici **en fraction du plafond** (25 %) et non
    de la taille courante (33 %) : c'est le plafond qui coupe, c'est donc lui le dénominateur.

    Pourquoi une fraction plutôt que l'égalité stricte à la borne : quand le périmètre atteint le
    seuil, ce sont des **catégories entières** qui disparaissent d'une liste dont le prompt dit
    qu'elle « fait foi, aucune autre » — donc le faux `hors_perimetre` que cette story corrige,
    réintroduit par une borne trop serrée, et invisible (aucun test ne lit le prompt rendu). Le seuil
    d'alerte est posé à `PERIMETRE_MARGE_MIN` de marge : assez tôt pour qu'on ait le temps de relever
    `perimetre_max_chars` ou de compacter la projection, assez tard pour ne pas rougir à chaque fiche
    ajoutée. Ce que ce test demande, quand il rougit, n'est **pas** de retirer des fiches.
    """
    from server.app.config import REPO_ROOT, Settings

    reglages = Settings(_env_file=None)
    corpus = load_corpus(REPO_ROOT / "data", allow_ungated=True,
                         perimetre_max_chars=10 ** 9)  # non borné : on mesure la taille **réelle**
    reel = corpus.perimetres[reglages.guide_doc_id]
    marge = 1 - len(reel) / reglages.perimetre_max_chars
    assert marge >= PERIMETRE_MARGE_MIN, (
        f"le périmètre du guide fait {len(reel)} caractères pour un seuil de "
        f"{reglages.perimetre_max_chars} : il ne reste que {marge:.0%} de marge, sous les "
        f"{PERIMETRE_MARGE_MIN:.0%} exigés. Quelques fiches de plus feront tomber des catégories "
        f"entières du prompt de *comprendre*, qui affirme « c'est la liste qui fait foi, aucune "
        f"autre » — c'est le faux hors_perimetre de la story 2.1, réintroduit par la borne. "
        f"Relever PERIMETRE_MAX_CHARS, ou compacter `corpus/loader.perimetre()`.")
    # …et le périmètre réellement servi n'est pas tronqué : la borne ne mord pas aujourd'hui.
    assert corpus.perimetres[reglages.guide_doc_id] == load_corpus(
        REPO_ROOT / "data", allow_ungated=True,
        perimetre_max_chars=reglages.perimetre_max_chars).perimetres[reglages.guide_doc_id]


# --- story 4.5 : `structure.json` couverte par le manifest, gate compris ---------------------------

def test_une_structure_declaree_et_concordante_laisse_le_document_servi(data: Path) -> None:
    """Revue 4.5, P1 : le patron exact d'`overlay_hash`, écrivains d'ingestion compris.

    Le loader exige « `structure.json` déclaré ⟺ présent ». Sans écrivain qui renseigne le champ,
    déposer l'artefact mettait le document en quarantaine avec « relancer l'ingestion » — une action
    qui ne corrigeait rien, puisque la réingestion réécrivait l'entrée **sans** le champ. La dette
    4.2c sortait alors le document du service, et `structure_prouvee_rate` ne pouvait jamais verdir.
    """
    structure = data / "lux-guide" / "structure.json"
    structure.write_text('{"doc_id": "lux-guide"}\n', "utf-8")
    # Déclarée et concordante : servie.
    m = _manifest(data)
    m["lux-guide"]["structure_hash"] = _sha(structure)
    _write_manifest(data, m)
    corpus = load_corpus(data, allow_ungated=True)
    assert corpus.quarantine == {} and "lux-guide" in corpus.documents
    # L'artefact bouge sans réingestion ⇒ quarantaine nommée.
    structure.write_text('{"doc_id": "lux-guide", "note": "modifie"}\n', "utf-8")
    assert load_corpus(data, allow_ungated=True).quarantine == {
        "lux-guide": "structure_hash différent du manifest (relancer l'ingestion)"}
    # Présente mais non déclarée ⇒ quarantaine nommée.
    m = _manifest(data)
    m["lux-guide"].pop("structure_hash")
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).quarantine == {
        "lux-guide": "structure : structure.json présent mais non déclaré dans le manifest "
                     "(relancer l'ingestion)"}
    # Déclarée mais absente ⇒ quarantaine nommée.
    structure.unlink()
    m = _manifest(data)
    m["lux-guide"]["structure_hash"] = "a" * 64
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).quarantine == {
        "lux-guide": "structure : déclarée dans le manifest mais absente"}


def test_un_gate_qui_certifie_une_autre_structure_est_neutralise(data: Path) -> None:
    """Revue 4.5, P3 : `gate.structure_hash` est recoupé avec l'entrée, comme `overlay_hash`.

    Écrire l'empreinte dans le gate sans jamais la comparer laissait servir, **sans une alerte**, un
    document réingéré avec une autre structure sous un gate qui certifie l'ancienne.
    """
    structure = data / "lux-guide" / "structure.json"
    structure.write_text('{"doc_id": "lux-guide"}\n', "utf-8")
    m = _manifest(data)
    m["lux-guide"]["structure_hash"] = _sha(structure)
    gate = _gate(m["lux-guide"])
    gate["structure_hash"] = _sha(structure)
    m["lux-guide"]["gate"] = gate
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=False).alerts == {"lux-guide": []}
    # Réingestion : l'entrée porte une nouvelle structure, le gate certifie l'ancienne.
    structure.write_text('{"doc_id": "lux-guide", "note": "reingere"}\n', "utf-8")
    m = _manifest(data)
    m["lux-guide"]["structure_hash"] = _sha(structure)
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=False).quarantine == {"lux-guide": "sans_gate"}


def test_un_gate_full_preprotocole_est_nomme_et_non_confondu_avec_un_manifest_casse(
        data: Path) -> None:
    """Revue 4.5, P15 : un gate `full` d'avant le protocole rend `gate_preprotocole`, pas un message
    qui accuse le manifest.

    Le validateur `Gate` refuse un `full` sans `plancher_digest`/`candidate_revision`, ce qui rendait
    **toute l'entrée** invalide : le document partait en quarantaine « entrée de manifest invalide »,
    un diagnostic qui parle du manifest alors que le manifest va bien, et qui masque exactement le
    correctif que `gate_preprotocole` existe pour donner — refaire le gate.
    """
    m = _manifest(data)
    gate = _gate(m["lux-guide"])
    gate.update({"profile": "full", "decisions": [], "run_digest": None})
    m["lux-guide"]["gate"] = gate
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).quarantine == {"lux-guide": "gate_preprotocole"}
    # Une entrée réellement abîmée garde son message générique : le détour ne blanchit rien.
    m = _manifest(data)
    m["lux-guide"].pop("document_hash")
    _write_manifest(data, m)
    raison = load_corpus(data, allow_ungated=True).quarantine["lux-guide"]
    assert raison.startswith("entrée de manifest invalide :")
    # Un gate `vertical` invalide pour une autre raison n'est pas requalifié non plus : le détour
    # ne vaut que pour le protocole d'un gate `full`, jamais comme fourre-tout.
    m = _manifest(data)
    m["lux-guide"]["document_hash"] = _sha(data / "lux-guide" / "document.json")
    gate = _gate(m["lux-guide"])
    gate["cases"] = 0
    m["lux-guide"]["gate"] = gate
    _write_manifest(data, m)
    assert load_corpus(data, allow_ungated=True).quarantine["lux-guide"].startswith(
        "entrée de manifest invalide :")
