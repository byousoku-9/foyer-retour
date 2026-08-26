"""Story 3.2 — typage Batch Opus des clauses, résolution déterministe des articles et portées.

La première lecture couvre tous les blocs citables. La seconde relit indépendamment chaque bloc que
la première a classé `definition|garantie|exclusion|condition|franchise`. Seul un accord exact sur
`kind` produit `kind_source=model_verified`; le reste demeure `model` et donc humain pour AD-6.

Le modèle ne rend que des étiquettes liées au bloc source. Les cibles restent des numéros d'article :
le code seul les transforme en `block_id`, refuse les ambiguïtés et calcule les scopes de nœuds.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from server.app.config import REPO_ROOT, Settings, cle_absente, get_settings
from server.app.corpus.text import normalize
from server.app.domain import Block, BlockKind, Document, ManifestEntry, Node, Report, is_citable
from server.app.domain.document import DOC_ID_RE, Relation
from server.app.llm.models import EFFORT, TIERS
from server.app.llm.pricing import BATCH_DISCOUNT, cost_from_usage, estimate_cost
from server.ingest.artifacts import document_json, merged_manifest, read_manifest
from server.ingest.report import enrich_typing_report, pages_charabia

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
TIER = "ingest"
MODEL = TIERS[TIER]
LEGAL_KINDS = frozenset({"definition", "garantie", "exclusion", "condition", "franchise"})
MODEL_KINDS = Literal["autre", "renvoi", "definition", "garantie", "exclusion", "condition", "franchise"]
RELATION_KINDS = Literal["exception_de", "specialise", "contredit"]
ARTICLE_RE = re.compile(r"^\d+(?:\.\d+)*$")
ARTICLE_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)*)\s*(?:-|à)\s*(\d+(?:\.\d+)*)$")
NORMAL_STOPS = frozenset({"end_turn", "stop_sequence", "tool_use"})
STRUCTURAL_KINDS = frozenset({"para", "heading", "table", "list", "autre"})
TYPING_RULES_VERSION = "3.2-review-1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RelationLabel(StrictModel):
    kind: RELATION_KINDS
    article: str


class ClauseLabel(StrictModel):
    block_id: str
    kind: MODEL_KINDS
    confidence: float = Field(ge=0, le=1)
    article_refs: list[str] = Field(default_factory=list)
    scope_articles: list[str] = Field(default_factory=list)
    defines: str | None = None
    overrides_article: str | None = None
    relations: list[RelationLabel] = Field(default_factory=list)


class ReadingOutput(StrictModel):
    labels: list[ClauseLabel]


class MigrationBlock(StrictModel):
    kind: BlockKind
    defines: str | None = None
    scope_node_id: str | None = None
    scope_node_ids: list[str] = Field(default_factory=list)
    kind_source: Literal["manual"]

    @model_validator(mode="after")
    def _required_for_kind(self) -> MigrationBlock:
        if self.kind == "definition" and not self.defines:
            raise ValueError("defines obligatoire pour une definition manuelle")
        if self.kind in LEGAL_KINDS - {"definition"} and not self.scope_node_id:
            raise ValueError(f"scope_node_id obligatoire pour une clause {self.kind} manuelle")
        return self


class MigrationOverlay(StrictModel):
    schema_version: Literal["1"]
    doc_id: str
    note: str | None = None
    blocks: dict[str, MigrationBlock] = Field(min_length=1)


@dataclass(frozen=True)
class RequestPlan:
    custom_id: str
    block_ids: tuple[str, ...]
    request: dict[str, Any]


@dataclass(frozen=True)
class TypingResult:
    document: Document
    report: Report
    entry: ManifestEntry
    cost_eur: float
    first_requests: int
    second_requests: int


class BatchFailure(RuntimeError):
    pass


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _schema(exact_block_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
    if exact_block_ids is None:
        return {"type": "json_schema", "schema": anthropic.transform_schema(ReadingOutput.model_json_schema())}
    # L'API ne supporte `minItems` que pour 0 ou 1. Un objet fermé dont chaque `block_id` est une
    # propriété obligatoire exprime en revanche exactement la couverture attendue, sans texte ni
    # index positionnel. L'étiquette répète son `block_id`; le code vérifie l'égalité avec sa clé.
    label_schema = anthropic.transform_schema(ClauseLabel.model_json_schema())
    definitions = label_schema.pop("$defs", {})
    definitions["ClauseLabel"] = label_schema
    schema = {
        "$defs": definitions,
        "type": "object",
        "properties": {
            "labels": {
                "type": "object",
                "properties": {block_id: {"$ref": "#/$defs/ClauseLabel"} for block_id in exact_block_ids},
                "required": list(exact_block_ids),
                "additionalProperties": False,
            },
        },
        "required": ["labels"],
        "additionalProperties": False,
    }
    return {"type": "json_schema", "schema": schema}


def _prompt(reading: int) -> str:
    return (PROMPTS_DIR / f"type_clauses_{reading}.md").read_text("utf-8")


def _structural_kind(block: Block) -> str:
    if block.structural_kind is not None:
        return block.structural_kind
    if block.kind_source is None and block.kind in STRUCTURAL_KINDS:
        return block.kind
    raise ValueError(
        f"{block.block_id}: structural_kind absent sur un bloc déjà typé; "
        "relancer pdf_to_blocks avant type_clauses"
    )


def campaign_fingerprint(doc: Document, settings: Settings) -> str:
    """Lie une campagne à toutes ses entrées et règles, sans inclure ses sorties automatiques."""
    projection = {
        "document": {
            "doc_id": doc.doc_id, "kind": doc.kind, "title": doc.title, "edition": doc.edition,
            "lang": doc.lang, "source_url": doc.source_url, "source_hash": doc.source_hash,
            "ingest_fingerprint": doc.ingest_fingerprint,
        },
        "nodes": [{
            "node_id": node.node_id, "level": node.level, "title": node.title,
            "items": [item.model_dump() for item in node.items], "sources": [source.model_dump() for source in node.sources],
        } for node in doc.nodes],
        "blocks": [{
            "block_id": block.block_id, "text": block.text, "lang": block.lang, "loc": block.loc,
            "seq": block.seq, "page": block.page, "bbox": block.bbox, "structural_kind": _structural_kind(block),
            "source_field": block.source_field, "continues": block.continues,
            "lines": [line.model_dump() for line in block.lines],
        } for block in doc.blocks],
        "prompts": {str(reading): _prompt(reading) for reading in (1, 2)},
        "model": MODEL,
        "schemas": {
            "first": [
                _schema(tuple(block.block_id for block in chunk))
                for chunk in _chunks(doc, [block for block in doc.blocks if is_citable(block)], settings)
            ],
            "second": _schema(),
        },
        "settings": {
            name: value for name, value in settings.thresholds().items()
            if name.startswith("type_clauses_") or name in {"kind_confidence_min", "usd_eur"}
        },
        "rules_version": TYPING_RULES_VERSION,
        # Toute modification de la résolution elle-même invalide aussi une reprise, même si un
        # développeur oublie de relever `TYPING_RULES_VERSION`.
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _legacy_custom_id(reading: int, index: int, block_ids: tuple[str, ...]) -> str:
    digest = hashlib.sha256("\n".join(block_ids).encode("utf-8")).hexdigest()[:10]
    return f"clauses-r{reading}-{index:04d}-{digest}"


def custom_id(reading: int, index: int, block_ids: tuple[str, ...], campaign: str) -> str:
    chunk = hashlib.sha256("\n".join(block_ids).encode("utf-8")).hexdigest()[:8]
    value = f"clauses-r{reading}-{index:04d}-{campaign[:12]}-{chunk}"
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
        raise ValueError(f"custom_id Batch invalide : {value!r}")
    return value


def _owner_paths(doc: Document) -> dict[str, list[str]]:
    by_id = {node.node_id: node for node in doc.nodes}
    parents = {child: node.node_id for node in doc.nodes for child in node.children}
    out: dict[str, list[str]] = {}
    for block in doc.blocks:
        current = doc.node_of(block.block_id)
        path = []
        while True:
            node = by_id[current]
            path.append(node.title)
            if current not in parents:
                break
            current = parents[current]
        out[block.block_id] = list(reversed(path))
    return out


def _payload(doc: Document, blocks: list[Block], paths: dict[str, list[str]]) -> str:
    values = []
    for block in blocks:
        values.append({
            "block_id": block.block_id,
            "owner_node_id": doc.node_of(block.block_id),
            "owner_titles": paths[block.block_id],
            "text": block.text,
        })
    return json.dumps({"document": {"title": doc.title, "edition": doc.edition}, "blocks": values},
                      ensure_ascii=False, separators=(",", ":"))


def _chunks(doc: Document, blocks: list[Block], settings: Settings) -> list[list[Block]]:
    """Regroupe sans jamais couper un bloc ; deux bornes indépendantes gouvernent chaque requête."""
    paths = _owner_paths(doc)
    chunks: list[list[Block]] = []
    current: list[Block] = []
    for block in blocks:
        candidate = [*current, block]
        too_many = len(candidate) > settings.type_clauses_max_blocks_per_request
        too_large = len(_payload(doc, candidate, paths)) > settings.type_clauses_max_input_chars
        if current and (too_many or too_large):
            chunks.append(current)
            current = [block]
        else:
            current = candidate
        if len(_payload(doc, current, paths)) > settings.type_clauses_max_input_chars:
            raise ValueError(
                f"{block.block_id} dépasse seul TYPE_CLAUSES_MAX_INPUT_CHARS="
                f"{settings.type_clauses_max_input_chars}; aucune coupe de texte n'est admise"
            )
    if current:
        chunks.append(current)
    return chunks


def requests_for(doc: Document, blocks: list[Block], reading: int, settings: Settings, *,
                 legacy_custom_ids: bool = False) -> list[RequestPlan]:
    paths = _owner_paths(doc)
    prompt = _prompt(reading)
    campaign = campaign_fingerprint(doc, settings)
    plans = []
    for index, chunk in enumerate(_chunks(doc, blocks, settings), 1):
        block_ids = tuple(block.block_id for block in chunk)
        cid = (_legacy_custom_id(reading, index, block_ids) if legacy_custom_ids
               else custom_id(reading, index, block_ids, campaign))
        # Lecture 1 : la couverture est dans le schéma fournisseur lui-même. Lecture 2 : une
        # omission reste le signal T2 « kind non confirmé », donc aucune longueur exacte.
        schema = _schema(block_ids if reading == 1 else None)
        params = {
            "model": MODEL,
            "max_tokens": settings.type_clauses_max_output_tokens,
            "system": [{"type": "text", "text": prompt}],
            "messages": [{"role": "user", "content": _payload(doc, chunk, paths)}],
            "output_config": {"format": schema, "effort": EFFORT[TIER]},
        }
        plans.append(RequestPlan(custom_id=cid, block_ids=block_ids,
                                 request={"custom_id": cid, "params": params}))
    if len(plans) > settings.type_clauses_max_requests_per_batch:
        raise ValueError(
            f"lecture {reading}: {len(plans)} requêtes dépassent le plafond "
            f"TYPE_CLAUSES_MAX_REQUESTS_PER_BATCH={settings.type_clauses_max_requests_per_batch}"
        )
    return plans


def majorant_eur(plans: list[RequestPlan], settings: Settings) -> float:
    total = 0.0
    for plan in plans:
        params = plan.request["params"]
        total += estimate_cost(params["model"], params["system"], params["messages"], params["max_tokens"], settings,
                               output_schema=params["output_config"]["format"]) * BATCH_DISCOUNT
    return round(total, 4)


def _cancel(client: Any, batch_id: str) -> str:
    try:
        client.messages.batches.cancel(batch_id)
    except Exception as exc:  # noqa: BLE001 - secours best-effort, la cause initiale reste prioritaire
        return f"annulation échouée ({type(exc).__name__}); annuler manuellement le lot {batch_id}"
    return "lot annulé"


def execute_batch(client: Any, plans: list[RequestPlan], settings: Settings, *, batch_id: str | None = None,
                  output: Any = sys.stdout, sleep: Any = time.sleep,
                  now: Any = time.monotonic) -> tuple[dict[str, str], float, list[str]]:
    """Soumet un lot, agrège par `custom_id`, compte l'usage réel et rend tous les échecs."""
    if not plans:
        return {}, 0.0, []
    expected = {plan.custom_id for plan in plans}
    if batch_id is None:
        try:
            batch = client.messages.batches.create(requests=[plan.request for plan in plans])
        except Exception as exc:
            raise BatchFailure(f"soumission Batch impossible ({type(exc).__name__}); rien n'a été écrit") from exc
        batch_id = _attr(batch, "id")
        if not batch_id:
            raise BatchFailure("la Batch API n'a rendu aucun id de lot; rien n'a été écrit")
        print(f"lot {batch_id} soumis ({len(plans)} requête(s))", file=output)
    else:
        print(f"reprise du lot {batch_id} ({len(plans)} requête(s) attendue(s))", file=output)
    started = now()
    try:
        while True:
            state = client.messages.batches.retrieve(batch_id)
            status = _attr(state, "processing_status")
            if status == "ended":
                break
            if now() - started > settings.type_clauses_batch_timeout_s:
                cancellation = _cancel(client, batch_id)
                raise BatchFailure(
                    f"lot {batch_id} non terminé après {settings.type_clauses_batch_timeout_s:.0f} s "
                    f"(état {status!r}); {cancellation}; rien n'a été écrit"
                )
            sleep(settings.type_clauses_batch_poll_s)
    except BatchFailure:
        raise
    except Exception as exc:
        cancellation = _cancel(client, batch_id)
        raise BatchFailure(f"récupération du lot {batch_id} impossible ({type(exc).__name__}); "
                           f"{cancellation}; rien n'a été écrit") from exc

    texts: dict[str, str] = {}
    failures: list[str] = []
    cost = 0.0
    seen: set[str] = set()
    try:
        entries = client.messages.batches.results(batch_id)
        for entry in entries:
            cid = str(_attr(entry, "custom_id", "?"))
            if cid in seen:
                failures.append(f"{cid}: résultat dupliqué")
                continue
            seen.add(cid)
            if cid not in expected:
                failures.append(f"{cid}: custom_id inconnu")
                continue
            result = _attr(entry, "result")
            result_type = _attr(result, "type")
            if result_type != "succeeded":
                failures.append(f"{cid}: {result_type or 'résultat absent'}")
                continue
            message = _attr(result, "message")
            usage = _attr(message, "usage")
            if usage is None:
                failures.append(f"{cid}: succeeded sans usage facturable")
                continue
            cost += cost_from_usage(MODEL, usage, settings.usd_eur, batch=True).cost_eur
            stop_reason = _attr(message, "stop_reason")
            if stop_reason is not None and stop_reason not in NORMAL_STOPS:
                failures.append(f"{cid}: réponse interrompue (stop_reason={stop_reason!r})")
                continue
            texts[cid] = "".join(
                str(_attr(part, "text", "")) for part in (_attr(message, "content") or [])
                if _attr(part, "type") == "text"
            )
    except Exception as exc:
        raise BatchFailure(f"résultats du lot {batch_id} illisibles ({type(exc).__name__}); rien n'a été écrit") from exc
    for missing in sorted(expected - seen):
        failures.append(f"{missing}: résultat absent")
    return texts, round(cost, 4), failures


def _validated_label(label: ClauseLabel, block: Block, settings: Settings) -> ClauseLabel:
    for name, values, limit in (
        ("article_refs", label.article_refs, settings.type_clauses_max_article_refs),
        ("scope_articles", label.scope_articles, settings.type_clauses_max_scope_articles),
        ("relations", label.relations, settings.type_clauses_max_relations),
    ):
        if len(values) > limit:
            raise ValueError(f"{block.block_id}.{name}: {len(values)} > borne {limit}")
    if label.defines is not None:
        value = " ".join(label.defines.split())
        if not value or len(value) > settings.type_clauses_definition_max_chars \
                or len(normalize(value).split()) > settings.type_clauses_definition_max_words:
            raise ValueError(f"{block.block_id}.defines hors bornes de configuration")
        label = label.model_copy(update={"defines": value})
    return label


def parse_reading(texts: dict[str, str], plans: list[RequestPlan], doc: Document, settings: Settings, *,
                  require_all_labels: bool) -> dict[str, ClauseLabel]:
    by_block = {block.block_id: block for block in doc.blocks}
    labels: dict[str, ClauseLabel] = {}
    for plan in plans:
        raw = texts.get(plan.custom_id)
        if raw is None:
            raise ValueError(f"{plan.custom_id}: résultat absent")
        expected = set(plan.block_ids)
        seen: set[str] = set()
        try:
            if require_all_labels:
                value = json.loads(raw)
                if not isinstance(value, dict) or set(value) != {"labels"} or not isinstance(value["labels"], dict):
                    raise ValueError("objet {labels: {block_id: étiquette}} attendu")
                if set(value["labels"]) != expected:
                    raise ValueError(f"clés absentes ou inattendues: {sorted(expected - set(value['labels']))}")
                parsed_labels = []
                for block_id, payload in value["labels"].items():
                    label = ClauseLabel.model_validate(payload)
                    if label.block_id != block_id:
                        raise ValueError(f"block_id {label.block_id!r} différent de sa clé {block_id!r}")
                    parsed_labels.append(label)
            else:
                parsed_labels = ReadingOutput.model_validate_json(raw).labels
        except (ValidationError, ValueError, TypeError) as exc:
            raise ValueError(f"{plan.custom_id}: JSON hors schéma strict ({type(exc).__name__})") from exc
        for label in parsed_labels:
            if label.block_id not in expected:
                raise ValueError(f"{plan.custom_id}: block_id inattendu {label.block_id!r}")
            if label.block_id in seen or label.block_id in labels:
                raise ValueError(f"{plan.custom_id}: block_id dupliqué {label.block_id!r}")
            seen.add(label.block_id)
            labels[label.block_id] = _validated_label(label, by_block[label.block_id], settings)
        if require_all_labels and seen != expected:  # défense locale, même si le schéma fournisseur l'impose
            raise ValueError(f"{plan.custom_id}: étiquettes absentes pour {sorted(expected - seen)}")
    return labels


def _article_index(doc: Document) -> dict[str, list[str]]:
    prefix = f"{doc.doc_id}:a"
    out: dict[str, list[str]] = {}
    for node in doc.nodes:
        if not node.node_id.startswith(prefix):
            continue
        number = node.node_id[len(prefix):].split("-", 1)[0]
        if ARTICLE_RE.fullmatch(number):
            out.setdefault(number, []).append(node.node_id)
    return out


def _subtree_items(doc: Document, node_id: str) -> tuple[list[str], list[str]]:
    by_node = {node.node_id: node for node in doc.nodes}
    block_ids: list[str] = []
    node_ids: list[str] = []

    def walk(current: str) -> None:
        node_ids.append(current)
        for item in by_node[current].items:
            if hasattr(item, "block_id"):
                block_ids.append(item.block_id)
            else:
                walk(item.node_id)

    walk(node_id)
    return block_ids, node_ids


def _target_node(article: str, index: dict[str, list[str]]) -> str | None:
    if not ARTICLE_RE.fullmatch(article):
        return None
    candidates = index.get(article, [])
    return candidates[0] if len(candidates) == 1 else None


def _expand_article(article: str, limit: int) -> list[str] | None:
    """Développe une plage décimale dont seul le dernier segment varie, sous une borne stricte."""
    value = article.strip().strip(". ")
    if ARTICLE_RE.fullmatch(value):
        return [value]
    match = ARTICLE_RANGE_RE.fullmatch(value)
    if match is None:
        return None
    start, end = match.groups()
    start_parts, end_parts = start.split("."), end.split(".")
    if len(start_parts) != len(end_parts) or start_parts[:-1] != end_parts[:-1]:
        return None
    first, last = int(start_parts[-1]), int(end_parts[-1])
    if last < first or last - first + 1 > limit:
        return None
    prefix = ".".join(start_parts[:-1])
    return [f"{prefix}.{number}" if prefix else str(number) for number in range(first, last + 1)]


def _metadata(first: ClauseLabel) -> dict[str, Any]:
    """Les métadonnées viennent exclusivement de T1 ; T2 ne confirme que son `kind`."""
    return {
        "article_refs": list(dict.fromkeys(first.article_refs)),
        "scope_articles": list(dict.fromkeys(first.scope_articles)),
        "defines": first.defines,
        "overrides_articles": [first.overrides_article] if first.overrides_article is not None else [],
        "relations": list(dict.fromkeys((relation.kind, relation.article) for relation in first.relations)),
    }


def assemble(doc: Document, first: dict[str, ClauseLabel], second: dict[str, ClauseLabel],
             settings: Settings) -> Document:
    """Fusionne les lectures puis résout articles, relations et scopes sans heuristique documentaire."""
    original_identity = [
        (block.block_id, block.text, block.loc, block.seq, block.page, block.bbox,
         block.continues, [line.model_dump() for line in block.lines])
        for block in doc.blocks
    ]
    blocks: list[Block] = []
    metadata: dict[str, dict[str, Any]] = {}
    for block in doc.blocks:
        structural_kind = _structural_kind(block)
        label = first.get(block.block_id)
        if label is None or label.kind == "autre":
            blocks.append(block.model_copy(update={
                "kind": structural_kind, "structural_kind": structural_kind,
                "kind_source": None, "kind_confidence": None, "refs": [], "unresolved_refs": [],
                "defines": None, "scope_node_id": None, "scope_node_ids": [], "overrides": None,
                "relation": Relation(),
            }, deep=True))
            continue
        confirmation = second.get(block.block_id)
        verified = label.kind in LEGAL_KINDS and confirmation is not None and confirmation.kind == label.kind
        update: dict[str, Any] = {
            "kind": label.kind, "structural_kind": structural_kind,
            "kind_source": "model_verified" if verified else "model",
            "kind_confidence": label.confidence,
            "refs": [], "unresolved_refs": [], "defines": None, "scope_node_id": None,
            "scope_node_ids": [], "overrides": None, "relation": Relation(),
        }
        if label.kind in LEGAL_KINDS or label.kind == "renvoi":
            update["scope_node_id"] = doc.node_of(block.block_id)
        blocks.append(block.model_copy(update=update, deep=True))
        metadata[block.block_id] = _metadata(label)

    typed = doc.model_copy(update={"blocks": blocks}, deep=True)
    # `model_copy` ne rejoue pas les validateurs ni les index privés.
    typed = Document.model_validate(typed.model_dump())
    article_index = _article_index(typed)
    by_block = {block.block_id: block for block in typed.blocks}

    for block_id, meta in metadata.items():
        block = by_block[block_id]
        unresolved = list(block.unresolved_refs)
        refs: list[str] = []
        expanded_refs: list[tuple[str, str]] = []
        for article in meta["article_refs"]:
            expanded = _expand_article(article, settings.type_clauses_max_article_refs)
            if expanded is None or len(expanded_refs) + len(expanded) > settings.type_clauses_max_article_refs:
                unresolved.append(article)
                expanded_refs = []
                break
            expanded_refs.extend((article, value) for value in expanded)
        for original_article, article in expanded_refs:
            node_id = _target_node(article, article_index)
            if node_id is None:
                unresolved.append(original_article)
                continue
            subtree_blocks, _ = _subtree_items(typed, node_id)
            candidates = [bid for bid in subtree_blocks if by_block[bid].kind in LEGAL_KINDS
                          and is_citable(by_block[bid])]
            if not candidates:
                candidates = [bid for bid in subtree_blocks if is_citable(by_block[bid])][:1]
            if block_id in candidates:
                unresolved.append(original_article)
                continue
            if not candidates or len(candidates) > settings.type_clauses_ref_expansion_max_blocks:
                unresolved.append(original_article)
                continue
            refs.extend(candidates)
        block.refs = list(dict.fromkeys(refs))

        owner = block.scope_node_id
        scope_nodes: list[str] = []
        scope_ok = True
        owner_subtree = set(_subtree_items(typed, owner)[1]) if owner else set()
        expanded_scopes: list[tuple[str, str]] = []
        for article in meta["scope_articles"]:
            expanded = _expand_article(article, settings.type_clauses_max_scope_articles)
            if expanded is None or len(expanded_scopes) + len(expanded) > settings.type_clauses_max_scope_articles:
                unresolved.append(article)
                scope_ok = False
                continue
            expanded_scopes.extend((article, value) for value in expanded)
        for original_article, article in expanded_scopes:
            target = _target_node(article, article_index)
            if target is None or target not in owner_subtree:
                unresolved.append(original_article)
                scope_ok = False
            else:
                scope_nodes.append(target)
        if scope_ok:
            block.scope_node_ids = list(dict.fromkeys(scope_nodes))

        if block.kind == "definition":
            block.defines = meta["defines"]

        override_articles = meta["overrides_articles"]
        if len(override_articles) == 1:
            article = override_articles[0]
            target = _target_node(article, article_index)
            candidates = [] if target is None else [
                bid for bid in _subtree_items(typed, target)[0] if by_block[bid].kind == "definition"
            ]
            if candidates == [block_id]:
                unresolved.append(article)
            elif len(candidates) == 1:
                block.overrides = candidates[0]
            else:
                unresolved.append(article)
        elif len(override_articles) > 1:
            unresolved.extend(override_articles)

        relation_values: dict[str, str | None] = {"exception_de": None, "specialise": None, "contredit": None}
        for relation_kind in relation_values:
            articles = list(dict.fromkeys(article for kind, article in meta["relations"] if kind == relation_kind))
            if len(articles) != 1:
                if articles:
                    unresolved.extend(articles)
                continue
            article = articles[0]
            target = _target_node(article, article_index)
            candidates = [] if target is None else [
                bid for bid in _subtree_items(typed, target)[0] if by_block[bid].kind in LEGAL_KINDS
            ]
            if candidates == [block_id]:
                unresolved.append(article)
            elif len(candidates) == 1:
                relation_values[relation_kind] = candidates[0]
            else:
                unresolved.append(article)
        block.relation = Relation(**relation_values)
        block.unresolved_refs = list(dict.fromkeys(unresolved))

    typed = _apply_node_scopes(typed)
    typed = Document.model_validate(typed.model_dump())
    final_identity = [
        (block.block_id, block.text, block.loc, block.seq, block.page, block.bbox,
         block.continues, [line.model_dump() for line in block.lines])
        for block in typed.blocks
    ]
    if final_identity != original_identity:
        raise ValueError("le typage a modifié texte, lignes, coordonnées, ordre ou identifiants")
    return typed


def _apply_node_scopes(doc: Document) -> Document:
    parent = {child: node.node_id for node in doc.nodes for child in node.children}
    signals: dict[str, str] = {}
    for block in doc.blocks:
        source = doc.node_of(block.block_id)
        source_subtree = set(_subtree_items(doc, source)[1])
        special_targets = [block.overrides, block.relation.exception_de]
        if any(target is not None and doc.node_of(target) not in source_subtree for target in special_targets):
            signals[source] = "special"
            continue
        target = block.relation.specialise
        if target is not None and doc.block(target).kind == "garantie" and doc.node_of(target) not in source_subtree:
            signals.setdefault(source, "extension")

    nodes: list[Node] = []
    for node in doc.nodes:
        current = node.node_id
        signal = None
        while True:
            if current in signals:
                signal = signals[current]
                break
            if current not in parent:
                break
            current = parent[current]
        nodes.append(node.model_copy(
            update={"scope": node.scope.model_copy(update={"kind": signal or "commun"})}, deep=True,
        ))
    return Document.model_validate(doc.model_copy(update={"nodes": nodes}, deep=True).model_dump())


def _migration_overlay(doc_dir: Path, doc: Document) -> dict[str, dict[str, Any]]:
    path = doc_dir / "typing.manual.json"
    if not path.is_file():
        return {}
    try:
        overlay = MigrationOverlay.model_validate_json(path.read_bytes())
    except (ValidationError, ValueError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"typing.manual.json invalide; il n'est ni utilisé ni supprimé: {exc}") from exc
    if overlay.doc_id != doc.doc_id or overlay.doc_id != doc_dir.name:
        raise ValueError("typing.manual.json: doc_id différent du document")
    node_ids = {node.node_id for node in doc.nodes}
    block_ids = {block.block_id for block in doc.blocks}
    node_parent = {child: node.node_id for node in doc.nodes for child in node.children}
    values: dict[str, dict[str, Any]] = {}
    for block_id, entry in overlay.blocks.items():
        if block_id not in block_ids:
            raise ValueError(f"typing.manual.json: bloc inconnu {block_id}")
        if entry.scope_node_id is not None and entry.scope_node_id not in node_ids:
            raise ValueError(f"typing.manual.json: nœud inconnu {entry.scope_node_id} pour {block_id}")
        unknown_scopes = [node_id for node_id in entry.scope_node_ids if node_id not in node_ids]
        if unknown_scopes:
            raise ValueError(f"typing.manual.json: scope_node_ids inconnus pour {block_id}: {unknown_scopes}")
        if len(set(entry.scope_node_ids)) != len(entry.scope_node_ids):
            raise ValueError(f"typing.manual.json: scope_node_ids dupliqués pour {block_id}")
        for scoped in entry.scope_node_ids:
            current = scoped
            while entry.scope_node_id is not None and current != entry.scope_node_id and current in node_parent:
                current = node_parent[current]
            if current != entry.scope_node_id:
                raise ValueError(f"typing.manual.json: {scoped} n'est pas sous {entry.scope_node_id} pour {block_id}")
        values[block_id] = entry.model_dump(exclude_none=True)
    return values


def _check_migration(doc: Document, expected: dict[str, dict[str, Any]]) -> None:
    for block_id, old in expected.items():
        try:
            block = doc.block(block_id)
        except KeyError as exc:
            raise ValueError(f"overlay: bloc attendu absent {block_id}") from exc
        if block.kind_source != "model_verified" or block.kind != old.get("kind"):
            raise ValueError(f"{block_id}: le golden manuel n'a pas reçu le même kind automatique confirmé")
        if old.get("defines") and normalize(block.defines or "") != normalize(str(old["defines"])):
            raise ValueError(f"{block_id}: définition automatique inexploitable après les deux lectures")
        old_scopes = old.get("scope_node_ids") or []
        if old_scopes and block.scope_node_ids != old_scopes:
            raise ValueError(f"{block_id}: portée explicite automatique différente du golden manuel")


def _atomic_artifacts(files: dict[Path, str], delete: list[Path]) -> None:
    """Prépare tous les temporaires, puis remplace; restaure les octets d'entrée sur toute erreur."""
    originals = {path: path.read_bytes() if path.is_file() else None for path in [*files, *delete]}
    temps: dict[Path, Path] = {}
    try:
        for path, text in files.items():
            temp = path.with_name(path.name + ".typing.tmp")
            temp.write_text(text, "utf-8")
            temps[path] = temp
        for path, temp in temps.items():
            os.replace(temp, path)
        for path in delete:
            path.unlink(missing_ok=True)
    except Exception:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                restore = path.with_name(path.name + ".typing.restore")
                restore.write_bytes(content)
                os.replace(restore, path)
        raise
    finally:
        for temp in temps.values():
            temp.unlink(missing_ok=True)


def _load(doc_dir: Path) -> tuple[Document, Report, dict[str, Any], ManifestEntry, dict[str, dict[str, Any]]]:
    manifest_path = doc_dir.parent / "manifest.json"
    raw_manifest = read_manifest(manifest_path)
    raw_entry = raw_manifest.get(doc_dir.name)
    if raw_entry is None:
        raise ValueError(f"{doc_dir.name}: absent du manifest")
    entry = TypeAdapter(ManifestEntry).validate_python(raw_entry)
    doc_path = doc_dir / "document.json"
    report_path = doc_dir / "report.json"
    doc_bytes = doc_path.read_bytes()
    if hashlib.sha256(doc_bytes).hexdigest() != entry.document_hash:
        raise ValueError("document_hash différent du manifest; relancer l'ingestion PDF")
    doc = Document.model_validate_json(doc_bytes)
    report = Report.model_validate_json(report_path.read_bytes())
    pages_charabia(report)  # préflight avant client/API : un ancien rapport doit être réingéré
    if doc.doc_id != doc_dir.name or report.doc_id != doc.doc_id:
        raise ValueError("doc_id incohérent entre dossier, document et rapport")
    if entry.status != "servi" or report.blocking:
        raise ValueError("le document doit être servi et sans bloquant avant typage")
    if (doc.source_hash, doc.ingest_fingerprint, doc.edition) != (
            entry.source_hash, entry.ingest_fingerprint, entry.edition):
        raise ValueError("source_hash, ingest_fingerprint ou édition incohérent avec le manifest")
    overlay_path = doc_dir / "typing.manual.json"
    if overlay_path.is_file() != (entry.overlay_hash is not None):
        raise ValueError("présence de typing.manual.json incohérente avec overlay_hash")
    if overlay_path.is_file() and hashlib.sha256(overlay_path.read_bytes()).hexdigest() != entry.overlay_hash:
        raise ValueError("overlay_hash différent du manifest; relancer l'ingestion PDF")
    return doc, report, raw_manifest, entry, _migration_overlay(doc_dir, doc)


@contextmanager
def _exclusive_lock(doc_dir: Path) -> Any:
    """Verrou OS par document ; le fichier persiste pour éviter la course unlink/recreate."""
    lock_path = doc_dir / ".type-clauses.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise BatchFailure(f"un typage concurrent tient déjà le verrou {lock_path}") from exc
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _run_locked(doc_dir: Path, *, settings: Settings, client: Any = None, dry_run: bool = False,
                max_cost: float | None = None, first_batch_id: str | None = None,
                second_batch_id: str | None = None, legacy_resume: str | None = None,
                output: Any = sys.stdout, sleep: Any = time.sleep,
                now: Any = time.monotonic) -> TypingResult | None:
    doc, report, raw_manifest, old_entry, migration = _load(doc_dir)
    campaign = campaign_fingerprint(doc, settings)
    print(f"empreinte de campagne: {campaign}", file=output)
    if legacy_resume is not None and (not first_batch_id or not second_batch_id):
        raise ValueError("--legacy-resume exige les deux IDs de lots existants; aucune soumission n'est autorisée")
    if legacy_resume is not None and legacy_resume != campaign:
        raise ValueError(
            f"--legacy-resume incompatible: empreinte attendue {campaign}; aucun lot récupé, aucune écriture"
        )
    if legacy_resume is not None:
        print("reprise legacy auditée: custom_id historiques vérifiés contre la campagne courante "
              f"{campaign}; aucune nouvelle soumission", file=output)
    citable = [block for block in doc.blocks if is_citable(block)]
    first_plans = requests_for(doc, citable, 1, settings, legacy_custom_ids=legacy_resume is not None)
    # Pire cas avant le premier euro : chaque bloc de lecture 1 est juridique et repasse en lecture 2.
    worst_second = requests_for(doc, citable, 2, settings, legacy_custom_ids=legacy_resume is not None)
    estimate = round(majorant_eur(first_plans, settings) + majorant_eur(worst_second, settings), 4)
    ceiling = settings.type_clauses_max_cost_eur if max_cost is None else max_cost
    if not math.isfinite(ceiling) or ceiling <= 0:
        raise ValueError("--max-cost doit être un nombre fini > 0; aucun lot soumis")
    print(f"lecture 1: {len(citable)} bloc(s), {len(first_plans)} requête(s); lecture 2: au plus "
          f"{len(citable)} bloc(s), {len(worst_second)} requête(s); majorant {estimate:.4f} € "
          f"(remise Batch {BATCH_DISCOUNT}) / plafond {ceiling:.4f} €", file=output)
    if estimate > ceiling:
        raise ValueError(f"majorant {estimate:.4f} € > plafond {ceiling:.4f} €; aucun lot soumis")
    if dry_run:
        return None
    if client is None:
        if cle_absente(settings):
            raise PermissionError("ANTHROPIC_API_KEY absente; aucun lot soumis")
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    first_texts, first_cost, failures = execute_batch(client, first_plans, settings, batch_id=first_batch_id,
                                                       output=output, sleep=sleep, now=now)
    if failures:
        raise BatchFailure(f"lecture 1 incomplète (coût réel {first_cost:.4f} €): " + "; ".join(failures))
    try:
        first = parse_reading(first_texts, first_plans, doc, settings, require_all_labels=True)
    except ValueError as exc:
        raise BatchFailure(
            f"lecture 1 hors schéma (coût réel {first_cost:.4f} €); aucun artefact écrit: {exc}"
        ) from exc
    legal_blocks = [doc.block(block_id) for block_id, label in first.items() if label.kind in LEGAL_KINDS]
    second_plans = requests_for(doc, legal_blocks, 2, settings, legacy_custom_ids=legacy_resume is not None)
    actual_estimate = round(majorant_eur(first_plans, settings) + majorant_eur(second_plans, settings), 4)
    if actual_estimate > ceiling:
        raise BatchFailure(f"les candidats de lecture 2 portent le majorant à {actual_estimate:.4f} € > plafond; "
                           "lecture 1 facturée, aucun artefact écrit")
    second_texts, second_cost, failures = execute_batch(client, second_plans, settings, batch_id=second_batch_id,
                                                         output=output, sleep=sleep, now=now)
    total_cost = round(first_cost + second_cost, 4)
    if failures:
        raise BatchFailure(f"lecture 2 incomplète (coût réel cumulé {total_cost:.4f} €): "
                           + "; ".join(failures))
    # Une omission de label **dans** une réponse valide est un désaccord T2, pas un lot T4 incomplet.
    try:
        second = parse_reading(second_texts, second_plans, doc, settings, require_all_labels=False)
        typed = assemble(doc, first, second, settings)
        _check_migration(typed, migration)
    except ValueError as exc:
        raise BatchFailure(
            f"assemblage refusé après les deux lectures (coût réel cumulé {total_cost:.4f} €); "
            f"aucun artefact écrit: {exc}"
        ) from exc
    typed_report = enrich_typing_report(report, typed)
    doc_text = document_json(typed)
    report_text = json.dumps(typed_report.model_dump(), indent=2, ensure_ascii=False) + "\n"
    document_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
    new_entry = ManifestEntry(
        status="quarantaine" if typed_report.blocking else "servi",
        source_hash=old_entry.source_hash,
        ingest_fingerprint=old_entry.ingest_fingerprint,
        document_hash=document_hash,
        edition=old_entry.edition,
        overlay_hash=None,
        gate=None,
    )
    manifest_text, merged_entry = merged_manifest(raw_manifest, doc.doc_id, new_entry)
    _atomic_artifacts(
        {doc_dir / "document.json": doc_text, doc_dir / "report.json": report_text,
         doc_dir.parent / "manifest.json": manifest_text},
        [doc_dir / "typing.manual.json"],
    )
    return TypingResult(document=typed, report=typed_report, entry=merged_entry,
                        cost_eur=total_cost,
                        first_requests=len(first_plans), second_requests=len(second_plans))


def run(doc_dir: Path, *, settings: Settings, client: Any = None, dry_run: bool = False,
        max_cost: float | None = None, first_batch_id: str | None = None,
        second_batch_id: str | None = None, legacy_resume: str | None = None,
        output: Any = sys.stdout, sleep: Any = time.sleep,
        now: Any = time.monotonic) -> TypingResult | None:
    with _exclusive_lock(doc_dir):
        return _run_locked(
            doc_dir, settings=settings, client=client, dry_run=dry_run, max_cost=max_cost,
            first_batch_id=first_batch_id, second_batch_id=second_batch_id,
            legacy_resume=legacy_resume, output=output, sleep=sleep, now=now,
        )


def main(argv: list[str] | None = None, *, client: Any = None, settings: Settings | None = None,
         output: Any = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("doc_id")
    parser.add_argument("--data", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-cost", type=float)
    parser.add_argument("--first-batch-id", help="reprendre un premier lot déjà soumis, sans le refacturer")
    parser.add_argument("--second-batch-id", help="reprendre un second lot déjà soumis, sans le refacturer")
    parser.add_argument(
        "--legacy-resume", metavar="SHA256_CAMPAGNE",
        help="migration auditée de deux lots antérieurs; exige leurs deux IDs et l'empreinte courante",
    )
    args = parser.parse_args(argv)
    if not DOC_ID_RE.fullmatch(args.doc_id):
        print(f"doc_id invalide (slug [a-z0-9-]+ attendu): {args.doc_id!r}", file=sys.stderr)
        return 2
    if args.max_cost is not None and (not math.isfinite(args.max_cost) or args.max_cost <= 0):
        print("--max-cost doit être un nombre fini > 0", file=sys.stderr)
        return 2
    settings = settings or get_settings()
    try:
        result = run(args.data / args.doc_id, settings=settings, client=client, dry_run=args.dry_run,
                     max_cost=args.max_cost, first_batch_id=args.first_batch_id,
                     second_batch_id=args.second_batch_id, legacy_resume=args.legacy_resume, output=output)
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except BatchFailure as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        print(f"typage refusé, rien n'a été écrit: {exc}", file=sys.stderr)
        return 3
    if result is None:
        print("dry-run: aucune soumission, aucune écriture", file=output)
        return 0
    print(f"coût réel des deux lectures: {result.cost_eur:.4f} €; "
          f"{result.first_requests}+{result.second_requests} requête(s)", file=output)
    for check in result.report.checks:
        if check.name in {"corruption_decisionnelle", "unresolved_refs", "definition_introuvable",
                          "exclusion_sans_marqueur", "confiance_typage_faible", "kinds_non_confirmes",
                          "typage_clauses"}:
            print(f"[{check.level}] {check.name}: {check.detail}", file=output)
    return 1 if result.report.blocking else 0


if __name__ == "__main__":
    sys.exit(main())
