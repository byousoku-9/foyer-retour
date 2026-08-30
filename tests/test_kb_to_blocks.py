"""kb.js → blocs : IDs et ordre d'AD-2, stabilité, `ids_disparus`, quarantaine sur invariant violé, source réelle."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from server.app.domain import Document, Report
from server.ingest import kb_to_blocks as k
from server.ingest.jsobject import parse_js_object
from server.ingest.report import build_report

ROOT = Path(__file__).resolve().parents[1]
MINI = Path(__file__).parent / "data" / "mini_kb.js"
REAL = ROOT / "data" / "lux-guide"


@pytest.fixture
def mini_kb() -> dict:
    return parse_js_object(MINI.read_text("utf-8"))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "lux-guide"
    d.mkdir(parents=True)
    shutil.copy(MINI, d / "source.js")
    return d


def test_block_order_ids_and_source_fields(mini_kb: dict) -> None:
    doc = k.build_document(mini_kb, edition="git:test", source_hash="h")
    assert (doc.doc_id, doc.kind, doc.lang, doc.edition, doc.source_url) == ("lux-guide", "guide", "fr", "git:test", k.SOURCE_URL)
    arrivee = [b for b in doc.blocks if b.loc == "farrivee"]
    assert [(b.seq, b.kind, b.source_field) for b in arrivee] == [
        (1, "heading", "titre"), (2, "para", "resume"), (3, "para", "corps[0]"), (4, "heading", "corps[1].h"),
        (5, "para", "corps[2]"), (6, "para", "corps[3]"), (7, "heading", "tableaux[0].titre"), (8, "table", "tableaux[0]"),
        (9, "para", "aRetenir[0]"), (10, "para", "aRetenir[1]"),
    ]
    assert arrivee[0].block_id == "lux-guide:farrivee:1" and arrivee[0].text == "Les huit premiers jours"
    assert arrivee[7].text == "Démarche | Délai\nDéclaration d'arrivée | 8 jours\nMatricule | immédiat"
    assert all(b.text_norm == "" for b in doc.blocks)  # jamais calculé à l'ingestion
    node = next(n for n in doc.nodes if n.node_id == "lux-guide:farrivee")
    assert node.blocks == [b.block_id for b in arrivee]  # Node.items = ordre
    assert node.sources[0].model_dump() == {"titre": "Guichet.lu", "url": "https://guichet.public.lu/"}
    assert [b.block_id for b in doc.blocks if b.loc == "fbail_test"] == [f"lux-guide:fbail_test:{i}" for i in (1, 2, 3)]


def test_tree_and_faq(mini_kb: dict) -> None:
    doc = k.build_document(mini_kb, edition="e", source_hash="h")
    by_id = {n.node_id: n for n in doc.nodes}
    assert by_id["lux-guide"].children == ["lux-guide:cat:administratif", "lux-guide:cat:logement", "lux-guide:faq"]
    assert by_id["lux-guide:cat:logement"].children == ["lux-guide:fbail_test"]
    assert by_id["lux-guide:faq"].children == ["lux-guide:q1", "lux-guide:q2"]
    assert by_id["lux-guide:q2"].blocks == ["lux-guide:q2:1", "lux-guide:q2:2"]
    q1, a1 = doc.block("lux-guide:q1:1"), doc.block("lux-guide:q1:2")
    assert (q1.source_field, q1.text) == ("q", "Quel délai pour la commune ?")
    assert (a1.source_field, a1.refs) == ("a", ["lux-guide:farrivee:1"])
    assert doc.block("lux-guide:q2:2").refs == ["lux-guide:fbail_test:1"]
    assert not any("timeline" in n.node_id for n in doc.nodes)


def test_faq_to_unknown_fiche_is_unresolved_alert(mini_kb: dict) -> None:
    mini_kb["faq"][0]["fiche"] = "inconnue"
    doc = k.build_document(mini_kb, edition="e", source_hash="h")
    assert doc.block("lux-guide:q1:2").unresolved_refs == ["lux-guide:finconnue"]
    report = build_report(doc, None, mini_kb, parcours_ignorees=0, parcours_alertes=[])
    assert [c.name for c in report.alerts] == ["unresolved_refs"]


def test_truncate_cuts_at_word_boundary_and_never_exceeds_limit() -> None:
    # revue P8 : résultat toujours <= limit, ellipse comprise, même quand le premier mot dépasse la limite.
    assert k._truncate("court", 10) == "court"
    assert k._truncate("un mot puis encore", 12) == "un mot…"  # coupe conservatrice au mot précédent
    long_word = "anticonstitutionnellement toujours"
    out = k._truncate(long_word, 10)
    assert out == "anticonst…" and len(out) <= 10  # repli : coupe dure
    for text in ("un mot puis encore", long_word, "exactement dix.", "a b c d e f g h i j k l"):
        for limit in range(10, 20):
            assert len(k._truncate(text, limit)) <= limit


def test_summary_and_fingerprint(mini_kb: dict) -> None:
    doc = k.build_document(mini_kb, edition="git:test", source_hash="abc")
    s = k.build_summary(doc, mini_kb)
    assert s.startswith(f"<!-- lux-guide · edition git:test · source_hash abc · ingest_fingerprint {k.ingest_fingerprint()} -->\n")
    assert "## Administratif\n\n- `lux-guide:farrivee` · Les huit premiers jours · Tout part de la commune. · tags : arrivée, commune" in s
    assert "## Questions fréquentes\n\n- `lux-guide:q1` · Quel délai pour la commune ?" in s
    assert re.fullmatch(r"[0-9a-f]{64}", k.ingest_fingerprint())
    assert k.ingest_fingerprint() == k.ingest_fingerprint()


def test_run_writes_artefacts_and_is_deterministic(data_dir: Path) -> None:
    report, entry = k.run(data_dir, edition="git:test")
    assert entry.status == "servi" and entry.gate is None
    assert not report.blocking and report.stats["fiches"] == 2 and report.stats["faq"] == 2
    assert [c.name for c in report.checks if c.level == "info"] == ["invariants_arbre", "parcours_ingere", "taille_sommaire"]
    first = {p.name: p.read_bytes() for p in data_dir.iterdir()} | {"manifest": (data_dir.parent / "manifest.json").read_bytes()}
    doc = Document.model_validate_json(first["document.json"])
    assert "text_norm" not in json.loads(first["document.json"])["blocks"][0]
    manifest = json.loads(first["manifest"])["lux-guide"]
    assert manifest["status"] == "servi" and manifest["gate"] is None and manifest["edition"] == "git:test"
    assert manifest["source_hash"] == hashlib.sha256(MINI.read_bytes()).hexdigest() == doc.source_hash
    assert manifest["ingest_fingerprint"] == k.ingest_fingerprint()
    assert manifest["document_hash"] == hashlib.sha256(first["document.json"]).hexdigest()
    Report.model_validate_json(first["report.json"])

    k.run(data_dir, edition="git:test")
    assert not list(data_dir.glob("*.tmp"))
    second = {p.name: p.read_bytes() for p in data_dir.iterdir()} | {"manifest": (data_dir.parent / "manifest.json").read_bytes()}
    assert first == second


def test_manifest_merge_keeps_other_docs_and_existing_gate(data_dir: Path) -> None:
    # Le gate ne peut être préservé que sur une reprise byte-identique : on produit d'abord l'entrée
    # qu'il certifie, puis on rejoue exactement la même ingestion.
    k.run(data_dir, edition="git:test")
    current = json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))["lux-guide"]
    gate = {"profile": "vertical", "source_hash": current["source_hash"],
            "ingest_fingerprint": current["ingest_fingerprint"], "cases_hash": "c", "pipeline_digest": "p",
            "prompts_digest": "q", "model_ids": {}, "evals_ok": True, "date": "2026-08-23", "overlay_hash": None,
            "cases": 1, "countersigned": False}
    other = {"status": "servi", "source_hash": "x", "ingest_fingerprint": "y", "document_hash": "z", "edition": "e",
             "overlay_hash": None, "gate": None}
    (data_dir.parent / "manifest.json").write_text(json.dumps({
        "lux-guide": {**current, "gate": gate}, "autre-doc": other}), "utf-8")
    k.run(data_dir, edition="git:test")
    m = json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))
    # Story 4.2b : `Gate` porte deux champs optionnels de plus (`decisions`, `run_digest`) ; la
    # revalidation du manifest les matérialise à leurs défauts sans rien changer au gate certifié.
    # Story 4.5 : quatre de plus (`plancher_digest`, `candidate_revision`, `report_digest`,
    # `structure_hash`), tous `null` sur un gate `vertical` — c'est bien le sujet du test :
    # l'ingestion **normalise** l'entrée au schéma en vigueur sans jamais changer une valeur.
    assert m["autre-doc"] == other
    assert m["lux-guide"]["gate"] == {
        **gate, "decisions": [], "run_digest": None, "pipeline_settings": {},
        "plancher_digest": None, "candidate_revision": None, "report_digest": None,
        "structure_hash": None,
    }
    assert m["lux-guide"]["status"] == "servi"


def test_manifest_merge_invalidates_gate_when_document_changes(data_dir: Path) -> None:
    k.run(data_dir, edition="git:test")
    path = data_dir.parent / "manifest.json"
    raw = json.loads(path.read_text("utf-8"))
    raw["lux-guide"]["gate"] = {
        "profile": "vertical", "source_hash": raw["lux-guide"]["source_hash"],
        "ingest_fingerprint": raw["lux-guide"]["ingest_fingerprint"], "cases_hash": "c",
        "pipeline_digest": "p", "prompts_digest": "q", "model_ids": {}, "evals_ok": True,
        "date": "2026-08-23", "overlay_hash": None, "cases": 1, "countersigned": False,
    }
    raw["lux-guide"]["document_hash"] = "ancien-document"
    path.write_text(json.dumps(raw), "utf-8")
    _, entry = k.run(data_dir, edition="git:test")
    assert entry.gate is None


def test_modified_source_only_shifts_ids_after_insertion(data_dir: Path) -> None:
    k.run(data_dir, edition="e")
    before = Document.model_validate_json((data_dir / "document.json").read_bytes())
    src = (data_dir / "source.js").read_text("utf-8")
    marker = '        { h: "Le matricule" },\n'
    (data_dir / "source.js").write_text(src.replace(marker, '        "Paragraphe inséré.",\n' + marker), "utf-8")
    report, _ = k.run(data_dir, edition="e")
    after = Document.model_validate_json((data_dir / "document.json").read_bytes())
    assert not report.blocking
    # les IDs de `farrivee` à partir de l'insertion sont réaffectés à un autre texte : « disparus » ; les autres loc intactes
    disparus = next(c for c in report.checks if c.name == "ids_disparus")
    assert disparus.level == "alerte"
    assert disparus.detail == ", ".join(f"lux-guide:farrivee:{i}" for i in range(4, 11))
    assert after.block("lux-guide:farrivee:4").text == "Paragraphe inséré."
    assert after.block("lux-guide:farrivee:5").text == before.block("lux-guide:farrivee:4").text
    for b in before.blocks:
        if b.loc != "farrivee":
            assert after.block(b.block_id).text == b.text
    assert report.stats["ids_disparus"] == 7 and report.stats["ids_nouveaux"] == 1
    # suppression : les IDs de fin de `farrivee` disparaissent et sont listés
    src = (data_dir / "source.js").read_text("utf-8")
    (data_dir / "source.js").write_text(src.replace('aRetenir: ["Huit jours pour la commune.", "Le matricule suit."]',
                                                    'aRetenir: []'), "utf-8")
    report, _ = k.run(data_dir, edition="e")
    disparus = next(c for c in report.checks if c.name == "ids_disparus")
    assert disparus.level == "alerte" and disparus.detail == "lux-guide:farrivee:10, lux-guide:farrivee:11"
    assert report.stats["ids_disparus"] == 2 and report.stats["ids_nouveaux"] == 0


def test_tree_invariant_violation_quarantines(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = (data_dir / "source.js").read_text("utf-8").replace('id: "bail_test"', 'id: "arrivee"')  # block_id dupliqué
    (data_dir / "source.js").write_text(src, "utf-8")
    assert k.main(["--data", str(data_dir)]) == 1
    report = json.loads((data_dir / "report.json").read_text("utf-8"))
    assert report["checks"][0]["level"] == "bloquant" and "dupliqué" in report["checks"][0]["detail"]
    assert json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))["lux-guide"]["status"] == "quarantaine"
    assert not (data_dir / "document.json").exists()


@pytest.mark.skipif(not (REAL / "source.js").is_file(), reason="source réelle absente")
def test_real_source_matches_committed_artefacts() -> None:
    kb = parse_js_object((REAL / "source.js").read_text("utf-8"))
    assert (REAL / "source.js").read_bytes() == (ROOT / "web" / "app" / "kb.js").read_bytes()
    source_hash = hashlib.sha256((REAL / "source.js").read_bytes()).hexdigest()
    doc = k.build_document(kb, edition=k.DEFAULT_EDITION, source_hash=source_hash)
    assert len(kb["fiches"]) == 36 and len(kb["faq"]) == 41 and len(doc.blocks) > 400
    assert k.document_json(doc) == (REAL / "document.json").read_text("utf-8")
    assert k.build_summary(doc, kb) == (REAL / "summary.md").read_text("utf-8")
    summary = k.build_summary(doc, kb)
    previous = Document.model_validate_json((REAL / "document.json").read_bytes())
    parcours = k.parcours_conditions(kb, {n.node_id for n in doc.nodes})
    assert build_report(doc, previous, kb, summary=summary, parcours_ignorees=parcours.ignorees,
                        parcours_alertes=parcours.alertes) == Report.model_validate_json((REAL / "report.json").read_bytes())
    # Story 2.3 : les neuf fiches que la `timeline` conditionne, dans l'ordre du parcours, sans
    # doublon (trois étapes différentes conditionnent `ecole` sur `{enfants: true}`). Les conditions
    # sont celles de la **source** — aucun texte d'étape n'a été ingéré, et aucun n'a de bloc.
    assert [c.node_id for c in doc.parcours] == [f"lux-guide:f{f}" for f in (
        "ecole", "garde", "recherche_logement", "achat", "assurance_auto", "allocations",
        "independant", "vehicule", "permis")]
    assert doc.parcours[0].si == {"enfants": True} and doc.parcours[3].si == {"logement": "Acheter"}
    assert (parcours.ignorees, parcours.alertes) == (29, [])  # 9 + 29 = les 38 étapes de la timeline
    assert not any(any(c.node_id == b.block_id for c in doc.parcours) for b in doc.blocks)
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text("utf-8"))["lux-guide"]
    assert manifest["source_hash"] == source_hash and manifest["ingest_fingerprint"] == k.ingest_fingerprint()
    # Story 1.10 : le gate `vertical` est désormais écrit — par `evals run --gate`, jamais par
    # l'ingestion (AD-7). C'est ce que ce test contrôle ici : le gate existe, il porte les empreintes
    # de **cette** entrée, et il n'a pas été fabriqué par le pipeline d'ingestion.
    gate = manifest["gate"]
    assert manifest["status"] == "servi" and gate is not None
    assert gate["profile"] == "vertical" and gate["evals_ok"] is True and gate["cases"] >= 1
    assert (gate["source_hash"], gate["ingest_fingerprint"], gate["overlay_hash"]) == (
        manifest["source_hash"], manifest["ingest_fingerprint"], manifest["overlay_hash"])
    report = Report.model_validate_json((REAL / "report.json").read_bytes())
    assert not report.blocking
    fiche_nodes = [n for n in doc.nodes if n.level == 2 and not n.node_id.startswith("lux-guide:q")]
    assert len(fiche_nodes) == 36 and all(n.sources and n.items for n in fiche_nodes)
    for i in range(1, 42):
        assert doc.block(f"lux-guide:q{i}:1").source_field == "q" and doc.block(f"lux-guide:q{i}:2").refs


def test_table_cells_none_and_pipe() -> None:
    t = {"titre": "T", "colonnes": ["a", None], "lignes": [["x | y", None], [None, 3]]}
    assert k.table_text(t) == "a | \nx \\| y | \n | 3"


def test_fingerprint_includes_normalize_version(monkeypatch: pytest.MonkeyPatch) -> None:
    before = k.ingest_fingerprint()
    monkeypatch.setattr(k, "normalize_version", "999")
    assert k.ingest_fingerprint() != before


def test_main_success_propagates_edition(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert k.main(["--data", str(data_dir), "--edition", "git:abc1234"]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("lux-guide : servi (edition git:abc1234, gate: null)")
    assert json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))["lux-guide"]["edition"] == "git:abc1234"


@pytest.mark.parametrize("mutation, fragment", [
    (lambda s: s.replace('id: "arrivee"', "id: 'arrivee'"), "JSObjectError"),  # parseur : simple-quote
    (lambda s: s.replace('id: "bail_test"', 'id: "bail:test"'), "sans ':'"),
    (lambda s: s.replace('id: "bail_test",\n', ""), "fiche.id"),
    (lambda s: s.replace('titre: "Signer un bail",\n', ""), "'titre' manquant"),
    (lambda s: s.replace('fiche: "bail_test" }', "}"), "'fiche' manquant"),
    (lambda s: s.replace('corps: ["La caution est plafonnée à deux mois de loyer."]', "corps: [42]"), "inattendu"),
    (lambda s: "window.KB = [1, 2];", "liste `fiches`"),
    (lambda s: "window.KB = {fiches: [\"x\"]};", "liste d'objets"),
    (lambda s: s.replace("timeline: [", "timeline: [1, "), "`timeline`"),
])
def test_malformed_source_is_blocking_not_a_traceback(data_dir: Path, mutation, fragment: str) -> None:
    k.run(data_dir, edition="e")  # artefacts sains d'abord
    assert (data_dir / "document.json").exists() and (data_dir / "summary.md").exists()
    (data_dir / "source.js").write_text(mutation((data_dir / "source.js").read_text("utf-8")), "utf-8")
    report, entry = k.run(data_dir, edition="e")
    assert [c.level for c in report.checks] == ["bloquant"], report
    assert fragment in report.checks[0].detail, report.checks[0].detail
    assert entry.status == "quarantaine" and entry.document_hash == ""
    assert not (data_dir / "document.json").exists() and not (data_dir / "summary.md").exists()
    assert (data_dir / "report.json").exists()
    assert json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))["lux-guide"]["status"] == "quarantaine"


def test_unreadable_source_is_blocking(data_dir: Path) -> None:
    (data_dir / "source.js").write_bytes(b"\xff\xfe window.KB = {}")
    report, _ = k.run(data_dir, edition="e")
    assert report.checks[0].level == "bloquant" and "UnicodeDecodeError" in report.checks[0].detail
    (data_dir / "source.js").unlink()
    report, _ = k.run(data_dir, edition="e")
    assert report.checks[0].level == "bloquant" and "FileNotFoundError" in report.checks[0].detail


def test_invalid_other_manifest_entry_is_kept_with_warning(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (data_dir.parent / "manifest.json").write_text(json.dumps({"autre-doc": {"status": "bizarre"}}), "utf-8")
    _, entry = k.run(data_dir, edition="e")
    assert entry.status == "servi"
    m = json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))
    assert m["autre-doc"] == {"status": "bizarre"} and m["lux-guide"]["status"] == "servi"
    assert "'autre-doc' du manifest invalide" in capsys.readouterr().err


def test_unreadable_manifest_blocks_without_touching_artefacts(data_dir: Path) -> None:
    k.run(data_dir, edition="e")
    snapshot = {p.name: p.read_bytes() for p in data_dir.iterdir()}
    (data_dir.parent / "manifest.json").write_text("{pas du json", "utf-8")
    report, entry = k.run(data_dir, edition="e")
    assert report.checks[0].name == "manifest_illisible" and report.blocking and entry.status == "quarantaine"
    assert {p.name: p.read_bytes() for p in data_dir.iterdir()} == snapshot
    assert (data_dir.parent / "manifest.json").read_text("utf-8") == "{pas du json"
    assert k.main(["--data", str(data_dir)]) == 1


def test_faq_to_unknown_fiche_never_uses_refs(mini_kb: dict) -> None:
    mini_kb["faq"][1]["fiche"] = "nulle_part"
    doc = k.build_document(mini_kb, edition="e", source_hash="h")
    a = doc.block("lux-guide:q2:2")
    assert a.refs == [] and a.unresolved_refs == ["lux-guide:fnulle_part"]
    assert doc.ingest_fingerprint == k.ingest_fingerprint()


# --- ce que la `timeline` donne, et ce qu'elle ne donne pas (story 2.3) ------
def test_le_parcours_du_mini_kb_dedoublonne_et_ignore_les_etapes_sans_condition(mini_kb: dict) -> None:
    """Quatre étapes, deux conditionnent la même fiche : ce que le parcours désigne est une **fiche**."""
    doc = k.build_document(mini_kb, edition="e", source_hash="h")
    assert [(c.node_id, c.si) for c in doc.parcours] == [
        ("lux-guide:fbail_test", {"logement": "Louer"}),
        ("lux-guide:farrivee", {"enfants": True})]
    parcours = k.parcours_conditions(mini_kb, {n.node_id for n in doc.nodes})
    assert (parcours.ignorees, parcours.alertes) == (2, [])  # l'étape sans `si` et le doublon
    # Aucun texte d'étape n'est ingéré (spec 1.1, « Never ») : il n'appartient à aucune fiche.
    assert not any("Relire le bail" in b.text for b in doc.blocks)


@pytest.mark.parametrize(("remplacement", "fragment"), [
    ('{ t: "x", fiche: "inconnue", si: { enfants: true } }', "fiche 'inconnue' inconnue"),
    ('{ t: "x", fiche: "arrivee", si: { enfants: { imbrique: 1 } } }', "non conforme"),
    ('{ t: "x", fiche: "arrivee", si: { marmotte: "Oui" } }', "hors du profil (marmotte)"),
])
def test_une_condition_inexploitable_est_une_alerte_jamais_un_bloquant(
        data_dir: Path, remplacement: str, fragment: str) -> None:
    """AD-8 : « une condition perdue dégrade un classement, elle ne rend pas le document illisible ».

    Les trois branches d'anomalie de `parcours_conditions` — fiche inconnue, `si` non conforme, clé
    hors `PROFIL_KEYS` — n'étaient exercées par aucun test (revue coordonnée 2.3, A7) : la règle
    n'était tenue par rien, et un futur durcissement en bloquant serait passé inaperçu. Le document
    reste **servi**, l'alerte est levée, et la condition n'entre pas dans `Document.parcours`.
    """
    src = (data_dir / "source.js").read_text("utf-8")
    ancienne = '      { t: "Relire le bail avant de signer.", fiche: "bail_test", si: { logement: "Louer" } }\n'
    assert ancienne in src
    (data_dir / "source.js").write_text(src.replace(ancienne, f"      {remplacement}\n"), "utf-8")

    report, entry = k.run(data_dir, edition="e")
    assert not report.blocking and entry.status == "servi"
    (alerte,) = [c for c in report.checks if c.name == "parcours_condition_ignoree"]
    assert alerte.level == "alerte" and fragment in alerte.detail
    doc = Document.model_validate_json((data_dir / "document.json").read_bytes())
    assert [c.node_id for c in doc.parcours] == ["lux-guide:fbail_test", "lux-guide:farrivee"]
    # Le compte du rapport ne gonfle pas d'une condition que rien ne pourra jamais satisfaire.
    assert report.stats["parcours_fiches"] == 2 and report.stats["parcours_etapes_ignorees"] == 2


def test_lingestion_declare_la_structure_presente_et_le_loader_sert_toujours(data_dir: Path) -> None:
    """Revue 4.5, P1 : l'écrivain d'ingestion renseigne `structure_hash` comme `overlay_hash`.

    Sans cela, le contrôle « déclaré ⟺ présent » du loader était un cul-de-sac : déposer un
    `structure.json` mettait le document en quarantaine avec « relancer l'ingestion », et la
    réingestion réécrivait l'entrée **sans** le champ — l'action indiquée ne corrigeait rien.
    """
    import hashlib

    from server.app.corpus.loader import load_corpus

    k.run(data_dir, edition="git:test")
    manifest = json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))
    assert manifest["lux-guide"]["structure_hash"] is None  # aucun artefact : rien n'est déclaré

    structure = data_dir / "structure.json"
    structure.write_text('{"doc_id": "lux-guide"}\n', "utf-8")
    _report, entry = k.run(data_dir, edition="git:test")
    attendu = hashlib.sha256(structure.read_bytes()).hexdigest()
    assert entry.structure_hash == attendu
    manifest = json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))
    assert manifest["lux-guide"]["structure_hash"] == attendu
    # Et le document reste **servi** : c'est toute la différence avec le cul-de-sac.
    corpus = load_corpus(data_dir.parent, allow_ungated=True)
    assert corpus.quarantine == {} and "lux-guide" in corpus.documents


def test_une_structure_qui_bouge_ne_conserve_pas_le_gate(data_dir: Path) -> None:
    """`merged_manifest` compare les trois empreintes certifiées, `structure_hash` comprise.

    Conserver un gate qui certifie une autre structure ferait servir un document sous une validation
    qui ne le décrit plus — le loader le neutraliserait ensuite en `sans_gate`, mais l'ingestion ne
    doit pas produire cette incohérence en premier lieu.
    """
    structure = data_dir / "structure.json"
    structure.write_text('{"doc_id": "lux-guide"}\n', "utf-8")
    k.run(data_dir, edition="git:test")
    current = json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))["lux-guide"]
    gate = {"profile": "vertical", "source_hash": current["source_hash"],
            "ingest_fingerprint": current["ingest_fingerprint"], "cases_hash": "c",
            "pipeline_digest": "p", "prompts_digest": "q", "model_ids": {}, "evals_ok": True,
            "date": "2026-08-29", "overlay_hash": None, "cases": 1, "countersigned": False,
            "structure_hash": current["structure_hash"]}
    (data_dir.parent / "manifest.json").write_text(
        json.dumps({"lux-guide": {**current, "gate": gate}}), "utf-8")
    # Réingestion à structure **identique** : le gate est conservé.
    k.run(data_dir, edition="git:test")
    assert json.loads((data_dir.parent / "manifest.json").read_text("utf-8")
                      )["lux-guide"]["gate"] is not None
    # Réingestion après un changement de structure : le gate ne survit pas.
    structure.write_text('{"doc_id": "lux-guide", "note": "autre"}\n', "utf-8")
    k.run(data_dir, edition="git:test")
    assert json.loads((data_dir.parent / "manifest.json").read_text("utf-8")
                      )["lux-guide"]["gate"] is None


def test_lingestion_atteste_larbre_quelle_a_construit(data_dir: Path) -> None:
    """Story 4.5 (revue B4, volet guide) : `invariants_arbre` **affirme**, et nomme de quoi il parle.

    Le check existait déjà, avec `detail: "ok"` — une déclaration sans empreinte, que n'importe quel
    `report.json` écrit à la main pouvait imiter. Le gate `full` en aurait tiré un fail-open sur le
    seul périmètre qui lui reste : le guide n'a pas de `structure.json`, donc pas de preuve de
    structure PDF, et sans cette attestation il n'aurait plus **aucune** exigence de structure.

    L'attestation lie l'arbre aux octets écrits (`document_hash`) et au code qui les a produits
    (`ingest_fingerprint`) — les deux valeurs que le manifest porte et que le loader recoupe.
    """
    from server.app.domain.ingest import lire_attestation_arbre

    report, entry = k.run(data_dir, edition="git:test")
    atteste = next(c for c in report.checks if c.name == "invariants_arbre")
    assert atteste.level == "info"
    assert lire_attestation_arbre(atteste.detail) == (entry.document_hash,
                                                      entry.ingest_fingerprint)
    # Et c'est bien ce qui est **écrit** sur disque, pas seulement ce que `run` rend.
    sur_disque = Report.model_validate_json((data_dir / "report.json").read_bytes())
    lu = next(c for c in sur_disque.checks if c.name == "invariants_arbre")
    assert lire_attestation_arbre(lu.detail) == (entry.document_hash, entry.ingest_fingerprint)
    manifest = json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))["lux-guide"]
    assert lire_attestation_arbre(lu.detail) == (manifest["document_hash"],
                                                 manifest["ingest_fingerprint"])
    # L'ingestion reste déterministe : ré-attester deux fois n'empile pas deux attestations.
    k.run(data_dir, edition="git:test")
    relu = Report.model_validate_json((data_dir / "report.json").read_bytes())
    assert next(c for c in relu.checks if c.name == "invariants_arbre").detail == lu.detail


def test_un_arbre_refuse_natteste_rien(data_dir: Path) -> None:
    """Une attestation ne se fabrique pas : un arbre refusé reste un bloquant nu.

    `attester_*` ne réécrit qu'un check **déjà présent et affirmatif** ; elle n'en ajoute jamais.
    Sans cette règle, un document en quarantaine aurait porté une preuve de structure.
    """
    from server.app.domain.ingest import lire_attestation_arbre

    (data_dir / "source.js").write_text("var kb = {fiches: [{}]};", "utf-8")
    report, entry = k.run(data_dir, edition="git:test")
    assert report.blocking and entry.status == "quarantaine" and entry.document_hash == ""
    assert all(lire_attestation_arbre(c.detail) is None for c in report.checks)


# --- Story 4.5, tour de racine unique : l'ingestion sous une racine posée --------------------------
#
# Revue du tour, constats 6 et 7. Le chemin de production neuf — lot complet constitué dans `run`,
# `merge_manifest(..., artefacts=…)`, `fusionner_et_publier`, `transaction()`, un seul `publier` —
# n'était exercé par aucun test sous un pointeur : tous les tests d'ingestion tournent sur un
# `tmp_path` qu'aucune racine ne couvre, donc sur le chemin ordinaire.


def _lot_du_document(data_dir: Path) -> list[Path]:
    return [data_dir / "document.json", data_dir / "summary.md", data_dir / "report.json",
            data_dir.parent / "manifest.json"]


def test_une_ingestion_sous_une_racine_publie_son_lot_en_un_seul_commit(
        data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constat 7 : le chemin de production sous pointeur, de bout en bout.

    Les quatre cibles — document, sommaire, rapport, manifest — doivent être publiées par **un
    seul** point de commit, rester des liens résolus par le pointeur, et ne jamais muter la
    génération que le pointeur publiait à l'entrée.
    """
    from server.evals.espace import EspacePublie, Transaction

    espace = EspacePublie(tmp_path, tmp_path / "data")
    espace.installer([c.relative_to(tmp_path) for c in _lot_du_document(data_dir)])
    espace.basculer([(data_dir.parent / "manifest.json", "{}\n")])

    generation_avant = espace.generation()
    slot_manifest = espace.chemin / generation_avant / "data" / "manifest.json"
    octets_avant, inode_avant = slot_manifest.read_bytes(), slot_manifest.stat().st_ino

    commits: list[list[str]] = []
    publier_reel = Transaction.publier

    def _publier(self: object, lot: list[tuple[Path, str | None]]) -> None:
        commits.append(sorted(Path(cible).name for cible, _contenu in lot))
        publier_reel(self, lot)  # type: ignore[arg-type]

    monkeypatch.setattr(Transaction, "publier", _publier)
    report, entry = k.run(data_dir, edition="git:test")

    assert not report.blocking
    assert commits == [["document.json", "manifest.json", "report.json", "summary.md"]], (
        "l'ingestion doit publier son lot complet en un seul commit")
    assert espace.generation() != generation_avant, "l'ingestion publie, elle ne mute pas"
    assert slot_manifest.read_bytes() == octets_avant and slot_manifest.stat().st_ino == inode_avant
    for cible in _lot_du_document(data_dir):
        assert cible.is_symlink(), f"{cible} : le lien couvert a été remplacé"
        assert espace.resolue_dans_lespace(cible), f"{cible} est sortie du pointeur"
    assert json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))["lux-guide"][
        "document_hash"] == entry.document_hash
    assert Document.model_validate_json((data_dir / "document.json").read_bytes()).doc_id == "lux-guide"
    assert espace.residus() == []


def test_une_quarantaine_sous_une_racine_retire_les_artefacts_dans_le_meme_lot(
        data_dir: Path, tmp_path: Path) -> None:
    """Constat 7, branche de quarantaine : les artefacts périmés partent **dans le même geste**.

    Quand le rapport est bloquant, `document.json` et `summary.md` entrent au lot avec `None` — leur
    slot est absent de la génération publiée, donc leur lien devient pendant, donc ils sont absents
    pour tout lecteur. Sans cela, retirer un artefact serait un `unlink` à un rang où une exception
    laisserait l'un fait et l'autre non, et un manifest en quarantaine pourrait cohabiter avec un
    `document.json` neuf.
    """
    from server.evals.espace import EspacePublie

    espace = EspacePublie(tmp_path, tmp_path / "data")
    espace.installer([c.relative_to(tmp_path) for c in _lot_du_document(data_dir)])
    espace.basculer([(data_dir.parent / "manifest.json", "{}\n")])

    k.run(data_dir, edition="git:test")  # une ingestion saine d'abord
    assert (data_dir / "document.json").is_file() and (data_dir / "summary.md").is_file()

    (data_dir / "source.js").write_text("ceci n'est pas un objet JS", "utf-8")
    report, entry = k.run(data_dir, edition="git:test")

    assert report.blocking and entry.status == "quarantaine"
    for retire in ("document.json", "summary.md"):
        cible = data_dir / retire
        assert cible.is_symlink(), f"{retire} doit rester un lien couvert"
        assert not cible.exists(), f"{retire} périmé est resté à côté d'un manifest en quarantaine"
    assert (data_dir / "report.json").is_file(), "le rapport de quarantaine, lui, est publié"
    assert json.loads((data_dir.parent / "manifest.json").read_text("utf-8"))[
        "lux-guide"]["status"] == "quarantaine"
    assert espace.residus() == []


def test_une_disposition_incomplete_refuse_avant_toute_ingestion(
        data_dir: Path, tmp_path: Path) -> None:
    """Constat 6 : le refus « lot mixte » tombait au **dernier** geste, en trace non rattrapée.

    Le lot complet est connu dès l'entrée : si sa disposition n'est pas posée, il n'y a aucune raison
    de faire tout le travail pour le jeter ensuite, ni de sortir en trace Python là où tous les
    autres refus d'ingestion sont des checks bloquants. Ici le manifest est couvert et les artefacts
    du document ne le sont pas — le lot mixte exact que la pose d'un document neuf oublie.
    """
    from server.evals.espace import EspacePublie

    espace = EspacePublie(tmp_path, tmp_path / "data")
    espace.installer([Path("data") / "manifest.json"])
    espace.basculer([(data_dir.parent / "manifest.json", "{}\n")])
    manifest_avant = (data_dir.parent / "manifest.json").read_bytes()

    report, entry = k.run(data_dir, edition="git:test")

    assert report.blocking and entry.status == "quarantaine"
    noms = {c.name for c in report.checks}
    assert noms == {"espace_de_publication_incomplet"}, noms
    detail = next(c.detail for c in report.checks if c.name == "espace_de_publication_incomplet")
    assert "lot mixte" in detail and "--depot" in detail, detail
    # **Rien n'a été écrit** : ni artefact, ni manifest.
    assert not (data_dir / "document.json").exists() and not (data_dir / "summary.md").exists()
    assert (data_dir.parent / "manifest.json").read_bytes() == manifest_avant
    assert espace.residus() == []
