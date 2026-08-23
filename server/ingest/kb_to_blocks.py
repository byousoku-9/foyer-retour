"""kb.js → arbre de blocs `lux-guide` (AD-2), artefacts `data/lux-guide/*` et `data/manifest.json` (AD-7).

    uv run python -m server.ingest.kb_to_blocks [--data data/lux-guide] [--edition git:a8e8593]

Ordre des blocs d'une fiche : `titre` (heading), `resume`, `corps[]` (str → para, {h} → heading),
`tableaux[]` (heading du titre + bloc `table`), `aRetenir[]`. FAQ : `q{i}:1` (question), `q{i}:2` (réponse,
`refs` = bloc titre de la fiche liée). La `timeline` n'est pas ingérée (elle n'est qu'un compte dans `report.json`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from server.app.corpus.text import normalize, normalize_version
from server.app.domain import Block, BlockRef, Check, Document, ManifestEntry, Node, NodeRef, Report, Source
from server.ingest.jsobject import parse_js_object
from server.ingest.report import build_report, report_from_validation_error

DOC_ID = "lux-guide"
TITLE = "S'installer au Luxembourg"
SOURCE_URL = "https://lux-guide.github.io/app/kb.js"
DEFAULT_EDITION = "git:a8e8593"

# Entrent dans `ingest_fingerprint` : toute modification change les IDs attendus (AD-2, stabilité).
PARSER_VERSION = "1"
SCHEMA_VERSION = "1"
SEGMENTATION_RULES = "fiche:titre,resume,corps[str=para|h=heading],tableaux[titre=heading,table],aRetenir;faq:q,a"
FLAGS = {"timeline": False, "table_sep": " | "}

_NON_SLUG = re.compile(r"[^a-z0-9]+")


_FICHE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def ingest_fingerprint() -> str:
    # `normalize_version` entre dans l'empreinte : `slug()` (node_id des catégories) dérive de `normalize()`.
    payload = json.dumps({"parser": PARSER_VERSION, "schema": SCHEMA_VERSION, "segmentation": SEGMENTATION_RULES,
                          "flags": FLAGS, "normalize_version": normalize_version}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def slug(s: str) -> str:
    return _NON_SLUG.sub("-", normalize(s)).strip("-")


def _cell(c: Any) -> str:
    """`None` → cellule vide ; un `|` dans une cellule est échappé pour ne pas se confondre avec le séparateur."""
    return "" if c is None else str(c).replace("|", "\\|")


def table_text(tableau: dict[str, Any]) -> str:
    rows = [tableau.get("colonnes", []), *tableau.get("lignes", [])]
    return "\n".join(FLAGS["table_sep"].join(_cell(c) for c in row) for row in rows if row)


def _fiche_blocks(fiche: dict[str, Any]) -> list[Block]:
    if not isinstance(fiche.get("id"), str) or not _FICHE_ID_RE.match(fiche["id"]):
        raise ValueError(f"fiche.id doit être un slug [A-Za-z0-9_-]+ sans ':' : {fiche.get('id')!r}")
    for key in ("titre", "cat"):
        if not isinstance(fiche.get(key), str) or not fiche[key]:
            raise ValueError(f"fiche {fiche['id']} : champ {key!r} manquant ou vide")
    loc = f"f{fiche['id']}"
    out: list[Block] = []

    def add(text: str, kind: str, source_field: str) -> None:
        seq = len(out) + 1
        out.append(Block(block_id=f"{DOC_ID}:{loc}:{seq}", text=text, loc=loc, seq=seq, kind=kind,
                         source_field=source_field))

    add(fiche["titre"], "heading", "titre")
    if fiche.get("resume"):
        add(fiche["resume"], "para", "resume")
    for i, item in enumerate(fiche.get("corps", [])):
        if isinstance(item, str):
            add(item, "para", f"corps[{i}]")
        elif isinstance(item, dict) and "h" in item:
            add(item["h"], "heading", f"corps[{i}].h")
        else:
            raise ValueError(f"fiche {fiche['id']} : corps[{i}] inattendu ({item!r})")
    for j, tableau in enumerate(fiche.get("tableaux", [])):
        if tableau.get("titre"):
            add(tableau["titre"], "heading", f"tableaux[{j}].titre")
        add(table_text(tableau), "table", f"tableaux[{j}]")
    for k, phrase in enumerate(fiche.get("aRetenir", [])):
        add(phrase, "para", f"aRetenir[{k}]")
    return out


def check_structure(kb: Any) -> dict[str, Any]:
    """Structure attendue de `kb.js` : objet, `fiches` liste de dicts, `faq` liste de dicts, `timeline` liste de phases."""
    if not isinstance(kb, dict) or not isinstance(kb.get("fiches"), list):
        raise ValueError("source.js : objet avec une liste `fiches` attendu")
    for name in ("fiches", "faq"):
        if not isinstance(kb.get(name, []), list) or not all(isinstance(x, dict) for x in kb.get(name, [])):
            raise ValueError(f"source.js : `{name}` doit être une liste d'objets")
    timeline = kb.get("timeline", [])
    if not isinstance(timeline, list) or not all(isinstance(p, dict) and isinstance(p.get("items"), list) for p in timeline):
        raise ValueError("source.js : `timeline` doit être une liste de phases {phase, items: []}")
    return kb


def build_document(kb: dict[str, Any], *, edition: str, source_hash: str) -> Document:
    nodes: list[Node] = []
    blocks: list[Block] = []
    root = Node(node_id=DOC_ID, level=0, title=TITLE)
    cats: dict[str, Node] = {}
    for fiche in kb["fiches"]:
        cat = fiche["cat"]
        if cat not in cats:
            cats[cat] = Node(node_id=f"{DOC_ID}:cat:{slug(cat)}", level=1, title=cat)
            root.items.append(NodeRef(node_id=cats[cat].node_id))
        fiche_blocks = _fiche_blocks(fiche)
        node = Node(node_id=f"{DOC_ID}:f{fiche['id']}", level=2, title=fiche["titre"],
                    items=[BlockRef(block_id=b.block_id) for b in fiche_blocks],
                    sources=[Source(titre=s["t"], url=s["u"]) for s in fiche.get("sources", [])])
        cats[cat].items.append(NodeRef(node_id=node.node_id))
        nodes.append(node)
        blocks.extend(fiche_blocks)
    title_ids = {f"{DOC_ID}:f{f['id']}": f"{DOC_ID}:f{f['id']}:1" for f in kb["fiches"]}

    faq_root = Node(node_id=f"{DOC_ID}:faq", level=1, title="Questions fréquentes")
    faq_nodes: list[Node] = []
    for i, item in enumerate(kb.get("faq", []), start=1):
        for key in ("q", "a", "fiche"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"faq[{i - 1}] : champ {key!r} manquant ou vide")
        loc = f"q{i}"
        target = f"{DOC_ID}:f{item.get('fiche', '')}"
        refs, unresolved = ([title_ids[target]], []) if target in title_ids else ([], [target])
        q = Block(block_id=f"{DOC_ID}:{loc}:1", text=item["q"], loc=loc, seq=1, kind="para", source_field="q")
        a = Block(block_id=f"{DOC_ID}:{loc}:2", text=item["a"], loc=loc, seq=2, kind="para", source_field="a",
                  refs=refs, unresolved_refs=unresolved)
        node = Node(node_id=f"{DOC_ID}:{loc}", level=2, title=item["q"],
                    items=[BlockRef(block_id=q.block_id), BlockRef(block_id=a.block_id)])
        faq_root.items.append(NodeRef(node_id=node.node_id))
        faq_nodes.append(node)
        blocks.extend([q, a])
    if faq_nodes:
        root.items.append(NodeRef(node_id=faq_root.node_id))

    all_nodes = [root, *cats.values(), *nodes]
    if faq_nodes:
        all_nodes += [faq_root, *faq_nodes]
    return Document(doc_id=DOC_ID, kind="guide", title=TITLE, edition=edition, lang="fr", nodes=all_nodes,
                    blocks=blocks, source_url=SOURCE_URL, source_hash=source_hash,
                    ingest_fingerprint=ingest_fingerprint())


def build_summary(doc: Document, kb: dict[str, Any]) -> str:
    """Sommaire compact (préfixe cacheable du modèle en 1.4) : catégories → fiches, puis questions de la FAQ."""
    lines = [f"<!-- {doc.doc_id} · edition {doc.edition} · source_hash {doc.source_hash} · "
             f"ingest_fingerprint {ingest_fingerprint()} -->", f"# {doc.title}", ""]
    by_id = {n.node_id: n for n in doc.nodes}
    fiches = {f"{DOC_ID}:f{f['id']}": f for f in kb["fiches"]}
    for cat_id in by_id[DOC_ID].children:
        cat = by_id[cat_id]
        if cat_id == f"{DOC_ID}:faq":
            lines.append("## Questions fréquentes")
            lines.append("")
            for q_id in cat.children:
                lines.append(f"- `{q_id}` · {by_id[q_id].title}")
            lines.append("")
            continue
        lines.append(f"## {cat.title}")
        lines.append("")
        for fiche_id in cat.children:
            f = fiches[fiche_id]
            tags = ", ".join(f.get("tags", []))
            lines.append(f"- `{fiche_id}` · {f['titre']} · {f.get('resume', '')} · tags : {tags}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def document_json(doc: Document) -> str:
    """`text_norm` n'est jamais écrit : il est recalculé au chargement (convention Texte)."""
    data = doc.model_dump(exclude={"blocks": {"__all__": {"text_norm"}}})
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _load_previous(path: Path) -> Document | None:
    if not path.is_file():
        return None
    try:
        return Document.model_validate_json(path.read_bytes())
    except (ValidationError, ValueError, OSError):
        return None


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    """Lu et validé avant toute écriture : un manifest illisible bloque sans rien modifier sur disque."""
    raw: dict[str, Any] = json.loads(manifest_path.read_text("utf-8")) if manifest_path.is_file() else {}
    if not isinstance(raw, dict):
        raise ValueError(f"{manifest_path} : un objet JSON {{doc_id: entrée}} est attendu")
    return raw


def _merge_manifest(manifest_path: Path, raw: dict[str, Any], entry: ManifestEntry) -> ManifestEntry:
    """Fusionne l'entrée `lux-guide` ; les autres documents sont conservés tels quels, même invalides (avertissement)."""
    adapter = TypeAdapter(ManifestEntry)
    previous = raw.get(DOC_ID)
    gate = None
    if previous:
        try:
            gate = adapter.validate_python(previous).gate  # jamais écrit par l'ingestion (AD-7) : conservé
        except ValidationError as exc:
            print(f"avertissement : entrée {DOC_ID} précédente invalide, gate ignoré ({exc.errors()[0].get('msg', '')})",
                  file=sys.stderr)
    for other, value in raw.items():
        if other == DOC_ID:
            continue
        try:
            adapter.validate_python(value)
        except ValidationError:
            print(f"avertissement : entrée {other!r} du manifest invalide, conservée telle quelle", file=sys.stderr)
    entry = entry.model_copy(update={"gate": gate})
    raw[DOC_ID] = entry.model_dump()
    _write_atomic(manifest_path, json.dumps(dict(sorted(raw.items())), indent=2, ensure_ascii=False) + "\n")
    return entry


def run(data_dir: Path, *, edition: str) -> tuple[Report, ManifestEntry]:
    """Ingère `data_dir/source.js` ; toute erreur de source devient un check bloquant, jamais une trace Python."""
    manifest_path = data_dir.parent / "manifest.json"
    try:
        raw_manifest = _read_manifest(manifest_path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        detail = f"{manifest_path} illisible, rien n'a été écrit : {type(exc).__name__}: {exc}"[:2000]
        report = Report(doc_id=DOC_ID, checks=[Check(name="manifest_illisible", level="bloquant", detail=detail)])
        return report, ManifestEntry(status="quarantaine", source_hash="", ingest_fingerprint=ingest_fingerprint(),
                                     document_hash="", edition=edition, gate=None)
    source_hash = ""
    doc: Document | None = None
    summary = ""
    previous = _load_previous(data_dir / "document.json")
    try:
        source_bytes = (data_dir / "source.js").read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        kb = check_structure(parse_js_object(source_bytes.decode("utf-8")))
        doc = build_document(kb, edition=edition, source_hash=source_hash)
        summary = build_summary(doc, kb)
        report = build_report(doc, previous, kb, summary=summary)
    except ValidationError as exc:
        report = report_from_validation_error(DOC_ID, exc)
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # JSObjectError est une ValueError ; KeyError/TypeError = champ manquant ou structure inattendue.
        detail = f"{type(exc).__name__}: {exc}"[:2000]
        report = Report(doc_id=DOC_ID, checks=[Check(name="source_illisible", level="bloquant", detail=detail)])
    status = "quarantaine" if report.blocking else "servi"
    document_hash = ""
    if doc is not None and not report.blocking:
        doc_text = document_json(doc)
        _write_atomic(data_dir / "document.json", doc_text)
        _write_atomic(data_dir / "summary.md", summary)
        document_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
    else:
        for stale in ("document.json", "summary.md"):  # jamais un artefact périmé à côté d'un manifest en quarantaine
            (data_dir / stale).unlink(missing_ok=True)
    _write_atomic(data_dir / "report.json", json.dumps(report.model_dump(), indent=2, ensure_ascii=False) + "\n")
    entry = _merge_manifest(manifest_path, raw_manifest,
                            ManifestEntry(status=status, source_hash=source_hash, ingest_fingerprint=ingest_fingerprint(),
                                          document_hash=document_hash, edition=edition, gate=None))
    return report, entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default="data/lux-guide", type=Path)
    parser.add_argument("--edition", default=DEFAULT_EDITION)
    args = parser.parse_args(argv)
    report, entry = run(args.data, edition=args.edition)
    for c in report.checks:
        print(f"[{c.level}] {c.name}: {c.detail}")
    print(f"stats: {json.dumps(report.stats, ensure_ascii=False)}")
    gate = "null" if entry.gate is None else f"{entry.gate.profile}, evals_ok={entry.gate.evals_ok}"
    line = f"{DOC_ID} : {entry.status} (edition {entry.edition}, gate: {gate})"
    if report.blocking:
        print(line, file=sys.stderr)
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
