"""kb.js → arbre de blocs `lux-guide` (AD-2), artefacts `data/lux-guide/*` et `data/manifest.json` (AD-7).

    uv run python -m server.ingest.kb_to_blocks [--data data/lux-guide] [--edition git:a8e8593]

Ordre des blocs d'une fiche : `titre` (heading), `resume`, `corps[]` (str → para, {h} → heading),
`tableaux[]` (heading du titre + bloc `table`), `aRetenir[]`. FAQ : `q{i}:1` (question), `q{i}:2` (réponse,
`refs` = bloc titre de la fiche liée). Du `timeline`, seules les **conditions** sont reprises, dans
`Document.parcours` (story 2.3) : le couple (nœud de la fiche visée, `si`), jamais le texte des
étapes — il deviendrait citable alors qu'il n'appartient à aucune fiche (spec 1.1, « Never »).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError

from server.app.corpus.text import normalize, normalize_version
from server.app.domain.profil import PROFIL_KEYS
from server.app.domain import (Block, BlockRef, Check, Document, ManifestEntry, Node, NodeRef, ParcoursCondition, Report,
                               Source)
from server.ingest.artifacts import (SCHEMA_VERSION, document_json, load_previous, merge_manifest, overlay_hash, read_manifest,
                                     structure_hash)
from server.ingest.jsobject import parse_js_object
from server.ingest.report import attester_arbre, build_report, report_from_validation_error

DOC_ID = "lux-guide"
TITLE = "S'installer au Luxembourg"
SOURCE_URL = "https://lux-guide.github.io/app/kb.js"
DEFAULT_EDITION = "git:a8e8593"

# Entrent dans `ingest_fingerprint` : toute modification change les IDs attendus (AD-2, stabilité).
PARSER_VERSION = "1"
SEGMENTATION_RULES = "fiche:titre,resume,corps[str=para|h=heading],tableaux[titre=heading,table],aRetenir;faq:q,a"
# `timeline: "parcours"` (story 2.3) remplace `timeline: False` : les étapes ne sont toujours pas
# ingérées en blocs, mais leurs conditions le sont désormais dans `Document.parcours` — deux
# ingestions du même `source.js` avec et sans cette projection ne rendent pas le même document, et
# `ingest_fingerprint` doit le dire (AD-2, stabilité).
FLAGS = {"timeline": "parcours", "table_sep": " | "}

_NON_SLUG = re.compile(r"[^a-z0-9]+")


_FICHE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def ingest_fingerprint() -> str:
    # `normalize_version` entre dans l'empreinte : `slug()` (node_id des catégories) dérive de `normalize()`.
    # Les seuils de compactage du sommaire (story 1.3, revue P1) y entrent aussi : un `SUMMARY_*` personnalisé
    # produit un summary.md différent et doit changer l'empreinte.
    from server.app.config import get_settings

    settings = get_settings()
    payload = json.dumps({"parser": PARSER_VERSION, "schema": SCHEMA_VERSION, "segmentation": SEGMENTATION_RULES,
                          "flags": FLAGS, "normalize_version": normalize_version,
                          "summary": {"summary_max_tags": settings.summary_max_tags,
                                      "summary_resume_max_chars": settings.summary_resume_max_chars}},
                         sort_keys=True, ensure_ascii=False)
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


class Parcours(NamedTuple):
    """Ce que la `timeline` a donné : les conditions retenues, les étapes ignorées, les alertes.

    Un seul objet, calculé une seule fois et transporté jusqu'au rapport (revue coordonnée 2.3, A6) :
    `build_document` le calculait puis jetait les alertes, et `run` recalculait tout sur un ensemble
    de `node_id` construit autrement — deux vérités possibles pour un même `source.js`.
    """

    conditions: list[ParcoursCondition]
    ignorees: int
    alertes: list[str]


def parcours_conditions(kb: dict[str, Any], node_ids: set[str]) -> Parcours:
    """Projette la `timeline` en conditions de parcours (story 2.3).

    Une étape entre dans le parcours si — et seulement si — elle porte un `si` non vide, une `fiche`
    dont le nœud existe, et des clés que le profil peut réellement satisfaire. Les autres sont
    **ignorées**, ce qui est le cas nominal : 29 des 38 étapes du guide ne portent aucune condition,
    et elles n'ont rien à dire d'un profil. Les entrées sont dédupliquées par `node_id` : trois
    étapes différentes conditionnent la fiche `ecole` sur `{enfants: true}`, et ce que le parcours
    désigne est une **fiche**, pas une étape.

    Trois anomalies, toutes en **alerte** et jamais bloquantes (AD-8) : fiche inconnue, `si` non
    conforme, et clé hors `PROFIL_KEYS`. Le parcours ne fait qu'ordonner des nœuds déjà candidats, et
    perdre une condition dégrade le classement d'une réponse — cela ne rend pas le document illisible.
    Bloquer l'ingestion du guide entier sur une clé de profil mal écrite serait une quarantaine
    injustifiée.

    **Pourquoi la clé hors `PROFIL_KEYS` est une anomalie et non un silence** (revue coordonnée 2.3,
    A9). AD-11 filtre le profil par `PROFIL_KEYS` avant tout usage : une condition sur `marmotte` ne
    peut jamais être satisfaite, quel que soit le profil. L'ingérer gonflerait le compte du rapport
    d'une fiche qui ne sera jamais promue, et donnerait à lire « 10 fiches conditionnées » pour neuf
    fiches utiles. Elle est donc écartée, et dite.
    """
    conditions: list[ParcoursCondition] = []
    vus: set[str] = set()
    ignorees = 0
    alertes: list[str] = []
    for phase in kb.get("timeline", []):
        for i, item in enumerate(phase.get("items", [])):
            if not isinstance(item, dict) or not item.get("si"):
                ignorees += 1
                continue
            repere = f"{phase.get('phase', '?')}[{i}]"
            si = item["si"]
            fiche = item.get("fiche")
            node_id = f"{DOC_ID}:f{fiche}" if isinstance(fiche, str) and fiche else ""
            anomalie = None
            if node_id not in node_ids:
                anomalie = f"fiche {fiche!r} inconnue"
            elif not isinstance(si, dict) or not all(
                    isinstance(k, str) and k and isinstance(v, str | bool) for k, v in si.items()):
                anomalie = "condition `si` non conforme (attendu {clé: str|bool})"
            elif not set(si) <= PROFIL_KEYS:
                anomalie = (f"condition `si` sur des clés hors du profil ({', '.join(sorted(set(si) - PROFIL_KEYS))}) : "
                            "aucun profil ne pourra la satisfaire")
            if anomalie is not None:
                alertes.append(f"{repere} : {anomalie}")
                ignorees += 1
                continue
            if node_id in vus:
                ignorees += 1
                continue
            vus.add(node_id)
            conditions.append(ParcoursCondition(node_id=node_id, si=si))
    return Parcours(conditions, ignorees, alertes)


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


def build_document_et_parcours(kb: dict[str, Any], *, edition: str,
                               source_hash: str) -> tuple[Document, Parcours]:
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
    parcours = parcours_conditions(kb, {n.node_id for n in all_nodes})
    doc = Document(doc_id=DOC_ID, kind="guide", title=TITLE, edition=edition, lang="fr", nodes=all_nodes,
                   blocks=blocks, parcours=parcours.conditions, source_url=SOURCE_URL,
                   source_hash=source_hash, ingest_fingerprint=ingest_fingerprint())
    return doc, parcours


def build_document(kb: dict[str, Any], *, edition: str, source_hash: str) -> Document:
    """Le document seul, pour les appelants qui n'ont que faire du compte-rendu de la `timeline`."""
    return build_document_et_parcours(kb, edition=edition, source_hash=source_hash)[0]


def _truncate(text: str, limit: int) -> str:
    """Coupe au dernier mot entier ; résultat toujours <= `limit` caractères, ellipse comprise.

    Repli (revue P8) : si le premier mot dépasse déjà `limit`, coupe dure à `limit - 1` + « … »."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "…"


def build_summary(doc: Document, kb: dict[str, Any]) -> str:
    """Sommaire compact (préfixe cacheable du modèle en 1.4) : catégories → fiches, puis questions de la FAQ.

    Compacté en story 1.3 (mesure au tokenizer réel, seuils dans config) : résumés tronqués à
    `summary_resume_max_chars`, tags limités aux `summary_max_tags` premiers.
    """
    from server.app.config import get_settings

    settings = get_settings()
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
            tags = ", ".join(f.get("tags", [])[: settings.summary_max_tags])
            resume = _truncate(f.get("resume", ""), settings.summary_resume_max_chars)
            lines.append(f"- `{fiche_id}` · {f['titre']} · {resume} · tags : {tags}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run(data_dir: Path, *, edition: str) -> tuple[Report, ManifestEntry]:
    """Ingère `data_dir/source.js` ; toute erreur de source devient un check bloquant, jamais une trace Python."""
    manifest_path = data_dir.parent / "manifest.json"
    try:
        # **Préflight seulement** : cette lecture refuse tôt un manifest illisible, avec un rapport
        # bloquant plutôt qu'une trace Python. Ce n'est **pas** l'état dont la fusion part — celui-là
        # est relu sous le verrou de la racine par `fusionner_et_publier` (tour de racine unique,
        # fait 2), sans quoi une ingestion concurrente publiée pendant le traitement serait écrasée.
        read_manifest(manifest_path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        detail = f"{manifest_path} illisible, rien n'a été écrit : {type(exc).__name__}: {exc}"[:2000]
        report = Report(doc_id=DOC_ID, checks=[Check(name="manifest_illisible", level="bloquant", detail=detail)])
        return report, ManifestEntry(status="quarantaine", source_hash="", ingest_fingerprint=ingest_fingerprint(),
                                     document_hash="", edition=edition, gate=None)
    source_hash = ""
    doc: Document | None = None
    summary = ""
    previous = load_previous(data_dir / "document.json")
    try:
        source_bytes = (data_dir / "source.js").read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        kb = check_structure(parse_js_object(source_bytes.decode("utf-8")))
        doc, parcours = build_document_et_parcours(kb, edition=edition, source_hash=source_hash)
        summary = build_summary(doc, kb)
        report = build_report(doc, previous, kb, summary=summary,
                              parcours_ignorees=parcours.ignorees, parcours_alertes=parcours.alertes)
    except ValidationError as exc:
        report = report_from_validation_error(DOC_ID, exc)
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # JSObjectError est une ValueError ; KeyError/TypeError = champ manquant ou structure inattendue.
        detail = f"{type(exc).__name__}: {exc}"[:2000]
        report = Report(doc_id=DOC_ID, checks=[Check(name="source_illisible", level="bloquant", detail=detail)])
    status = "quarantaine" if report.blocking else "servi"
    document_hash = ""
    # Story 4.5, tour de racine unique : le lot **complet** de l'ingestion est constitué ici, puis
    # publié en un seul geste avec le manifest. Écrire `document.json` et `summary.md` tout de
    # suite, comme avant, faisait autant de points de commit que d'artefacts : un échec au manifest
    # laissait deux artefacts neufs devant une entrée périmée. Une absence est membre du lot au
    # même titre qu'un contenu — `None` retire l'artefact, et jamais un artefact périmé ne reste à
    # côté d'un manifest en quarantaine.
    artefacts: list[tuple[Path, str | None]] = []
    if doc is not None and not report.blocking:
        doc_text = document_json(doc)
        artefacts += [(data_dir / "document.json", doc_text), (data_dir / "summary.md", summary)]
        document_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
    else:
        artefacts += [(data_dir / "document.json", None), (data_dir / "summary.md", None)]
    fingerprint = ingest_fingerprint()
    # Story 4.5 (revue B4, volet guide) : quand l'arbre a réellement été construit et écrit, le
    # rapport **atteste** de quoi il parle — `document_hash` et `ingest_fingerprint` observés. C'est
    # la preuve de structure déterministe applicable à une copie de site, qui n'a pas de
    # `structure.json` : sans elle, un `report.json` portant `invariants_arbre: ok` (la forme
    # historique, sans empreinte) suffirait à faire verdir le témoin du gate `full`.
    report = attester_arbre(report, document_hash=document_hash, ingest_fingerprint=fingerprint)
    artefacts.append((data_dir / "report.json",
                      json.dumps(report.model_dump(), indent=2, ensure_ascii=False) + "\n"))
    entry = merge_manifest(manifest_path, DOC_ID,
                            ManifestEntry(status=status, source_hash=source_hash, ingest_fingerprint=fingerprint,
                                          document_hash=document_hash, edition=edition,
                                          overlay_hash=overlay_hash(data_dir),
                                          # Story 4.5 : couverte par le manifest comme l'overlay.
                                          structure_hash=structure_hash(data_dir), gate=None),
                            artefacts=artefacts)
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
