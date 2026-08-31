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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Event
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from server.app.config import REPO_ROOT, Settings, cle_absente, get_settings
from server.app.corpus.text import normalize
from server.app.domain import Block, BlockKind, Check, Document, ManifestEntry, Node, Report, is_citable
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE, Relation
from server.app.llm.models import EFFORT, TIERS
from server.app.llm.pricing import BATCH_DISCOUNT, cost_from_usage, estimate_cost
from server.app.corpus.racine import Lecture, relire
from server.ingest.artifacts import (STRUCTURE_FILE, TYPING_REUSED_IDS_STAT, LectureDuLot,
                                     document_json, exiger_espace_installe,
                                     fusionner_et_publier, publier_artefacts,
                                     verifier_couverture_du_lot)
from server.ingest.report import (attester_arbre, attester_structure,
                                  canoniser_transition_apres_typage, enrich_typing_report,
                                  pages_charabia)

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
TYPING_RULES_VERSION = "3.2-review-2"


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
    transport: str = "batch"
    reused_requests: int = 0
    replayed_requests: int = 0
    standard_requests: int = 0
    batch_cost_eur: float = 0.0
    standard_cost_eur: float = 0.0
    prior_cost_eur: float = 0.0
    cumulative_cost_eur: float = 0.0


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


def campaign_fingerprint(
        doc: Document, settings: Settings, citable_blocks: list[Block] | None = None,
) -> str:
    """Lie une campagne à toutes ses entrées et règles, sans inclure ses sorties automatiques."""
    planned = citable_blocks if citable_blocks is not None else [
        block for block in doc.blocks if is_citable(block)
    ]
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
                for chunk in _chunks(doc, planned, settings)
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


def _typing_reused_ids(report: Report, doc: Document) -> set[str]:
    """Valide le certificat de delta structurel avant d'exclure le moindre bloc des requêtes."""
    raw = report.stats.get(TYPING_REUSED_IDS_STAT, {})
    if not isinstance(raw, dict) or any(
            not isinstance(key, str) or isinstance(value, bool) or value != 1
            for key, value in raw.items()
    ):
        raise ValueError(f"report.stats.{TYPING_REUSED_IDS_STAT} doit être un objet block_id→1")
    by_id = {block.block_id: block for block in doc.blocks}
    unknown = sorted(set(raw) - set(by_id))
    non_citable = sorted(block_id for block_id in raw if block_id in by_id and not is_citable(by_id[block_id]))
    if unknown or non_citable:
        raise ValueError(
            "certificat de réutilisation invalide: "
            f"inconnus={unknown}, non_citables={non_citable}"
        )
    return set(raw)


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
                 legacy_custom_ids: bool = False, campaign_override: str | None = None) -> list[RequestPlan]:
    paths = _owner_paths(doc)
    prompt = _prompt(reading)
    campaign = campaign_override or campaign_fingerprint(doc, settings)
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


def standard_majorant_eur(plan: RequestPlan, settings: Settings) -> float:
    """Majorant d'un appel Messages standard, sans la remise Batch."""
    params = plan.request["params"]
    return estimate_cost(
        params["model"], params["system"], params["messages"], params["max_tokens"], settings,
        output_schema=params["output_config"]["format"],
    )


def resume_payload_proofs(plans: list[RequestPlan], settings: Settings) -> dict[str, str]:
    """Une preuve par requête, afin de ne rejouer que les payloads réellement modifiés."""
    relevant_settings = {
        name: value for name, value in settings.thresholds().items()
        if name.startswith("type_clauses_") or name in {"kind_confidence_min", "usd_eur"}
    }
    out: dict[str, str] = {}
    for plan in plans:
        projection = {
            "block_ids": list(plan.block_ids),
            "params": plan.request["params"],
            "settings": relevant_settings,
        }
        encoded = json.dumps(
            projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        out[plan.custom_id] = hashlib.sha256(encoded).hexdigest()
    return out


def _payload_proofs_fingerprint(proofs: dict[str, str]) -> str:
    encoded = json.dumps(proofs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resume_payload_fingerprint(plans: list[RequestPlan], settings: Settings) -> str:
    """Certificat global des preuves par requête, indépendant du transport."""
    return _payload_proofs_fingerprint(resume_payload_proofs(plans, settings))


class _CostGuard:
    """Réserve le prochain majorant contre le coût réel déjà observé, même en concurrence."""

    def __init__(self, ceiling: float, spent: float = 0.0) -> None:
        self.ceiling = ceiling
        self.spent = spent
        self.reserved = 0.0
        self.condition = Condition()

    def reserve(self, estimate: float) -> None:
        with self.condition:
            while self.spent + self.reserved + estimate > self.ceiling and self.reserved > 0:
                self.condition.wait()
            if self.spent + self.reserved + estimate > self.ceiling:
                raise BatchFailure(
                    f"plafond {self.ceiling:.4f} € insuffisant pour la requête suivante "
                    f"(coût réel {self.spent:.4f} € + estimation {estimate:.4f} €); "
                    "aucun artefact écrit"
                )
            self.reserved += estimate

    def commit(self, estimate: float, actual: float) -> None:
        with self.condition:
            self.reserved -= estimate
            self.spent += actual
            self.condition.notify_all()

    def release(self, estimate: float) -> None:
        with self.condition:
            self.reserved -= estimate
            self.condition.notify_all()


def _retryable_standard(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


def _standard_response(message: Any, settings: Settings) -> tuple[str, float]:
    usage = _attr(message, "usage")
    if usage is None:
        raise BatchFailure("réponse standard sans usage facturable; aucun artefact écrit")
    cost = cost_from_usage(MODEL, usage, settings.usd_eur, batch=False).cost_eur
    stop_reason = _attr(message, "stop_reason")
    if stop_reason is not None and stop_reason not in NORMAL_STOPS:
        raise BatchFailure(
            f"réponse standard interrompue (stop_reason={stop_reason!r}, coût réel {cost:.4f} €); "
            "aucun artefact écrit"
        )
    text = "".join(
        str(_attr(part, "text", "")) for part in (_attr(message, "content") or [])
        if _attr(part, "type") == "text"
    )
    return text, cost


def execute_standard(client: Any, plans: list[RequestPlan], settings: Settings, *, guard: _CostGuard,
                     output: Any = sys.stdout, sleep: Any = time.sleep) -> tuple[dict[str, str], float]:
    """Messages standard explicites : concurrence, retries 429/5xx et coût tous bornés.

    Aucun appel à `messages.batches.create` n'est possible depuis cette fonction. Le dictionnaire
    rendu suit l'ordre des plans, indépendamment de l'ordre de terminaison des workers.
    """
    if not plans:
        return {}, 0.0
    stopped = Event()

    def one(plan: RequestPlan) -> tuple[str, str, float]:
        estimate = standard_majorant_eur(plan, settings)
        if stopped.is_set():
            raise BatchFailure(f"{plan.custom_id}: appel annulé avant démarrage")
        guard.reserve(estimate)
        reserved = True
        try:
            if stopped.is_set():
                raise BatchFailure(f"{plan.custom_id}: appel annulé avant démarrage")
            for attempt in range(settings.type_clauses_standard_max_retries + 1):
                if stopped.is_set():
                    raise BatchFailure(f"{plan.custom_id}: appel annulé avant nouvelle tentative")
                try:
                    message = client.messages.create(**plan.request["params"])
                    usage = _attr(message, "usage")
                    if usage is None:
                        # L'absence d'usage empêche un certificat exact. On consomme le majorant
                        # réservé avant de refuser, afin que les workers concurrents ne puissent pas
                        # dépenser à sa place après un appel potentiellement facturé.
                        guard.commit(estimate, estimate)
                        reserved = False
                        raise BatchFailure("réponse standard sans usage facturable; aucun artefact écrit")
                    actual = cost_from_usage(MODEL, usage, settings.usd_eur, batch=False).cost_eur
                    guard.commit(estimate, actual)
                    reserved = False
                    text, actual = _standard_response(message, settings)
                    return plan.custom_id, text, actual
                except Exception as exc:  # noqa: BLE001 - classification stricte par statut HTTP
                    if (not _retryable_standard(exc)
                            or attempt >= settings.type_clauses_standard_max_retries):
                        raise BatchFailure(
                            f"{plan.custom_id}: appel standard échoué après {attempt + 1} tentative(s) "
                            f"({type(exc).__name__}); aucun artefact écrit"
                        ) from exc
                    sleep(settings.type_clauses_standard_retry_base_s * (2 ** attempt))
        except Exception:
            if reserved:
                guard.release(estimate)
            raise
        raise AssertionError("boucle de retry standard impossible")

    completed: dict[str, tuple[str, float]] = {}
    failure: Exception | None = None
    with ThreadPoolExecutor(max_workers=settings.type_clauses_standard_concurrency) as executor:
        futures = {
            executor.submit(one, plan): plan.custom_id
            for plan in plans[:settings.type_clauses_standard_concurrency]
        }
        # La fenêtre seule est soumise : aucune file de dizaines d'appels n'est créée d'avance.
        next_index = len(futures)
        while futures and failure is None:
            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            succeeded = 0
            for future in done:
                futures.pop(future)
                try:
                    cid, text, cost = future.result()
                except Exception as exc:  # les workers restants sont arrêtés au premier terminal
                    failure = exc
                    stopped.set()
                    break
                completed[cid] = (text, cost)
                succeeded += 1
            if failure is None:
                for _ in range(succeeded):
                    if next_index >= len(plans):
                        break
                    plan = plans[next_index]
                    futures[executor.submit(one, plan)] = plan.custom_id
                    next_index += 1
        if failure is not None:
            stopped.set()
            for future in futures:
                future.cancel()
            # Les appels déjà entrés dans `messages.create` finissent et comptent leur usage dans
            # le garde. Les futures non démarrées sont annulées par l'executor.
            executor.shutdown(wait=True, cancel_futures=True)
    if failure is not None:
        raise BatchFailure(
            f"arrêt des appels standard au premier échec terminal; coût réel acquis "
            f"{guard.spent:.4f} €; cause: {failure}; aucun artefact écrit"
        ) from failure
    ordered = {plan.custom_id: completed[plan.custom_id][0] for plan in plans}
    cost = round(sum(completed[plan.custom_id][1] for plan in plans), 4)
    print(f"Messages standard: {len(plans)} appel(s), coût réel {cost:.4f} €", file=output)
    return ordered, cost


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


def _define_is_anchored(doc: Document, block: Block, term: str | None) -> bool:
    """Le modèle nomme un terme ; le code exige sa présence dans le corpus source."""
    normalized = normalize(term or "")
    if not normalized:
        return False
    titles = _owner_paths(doc)[block.block_id]
    needle = f" {normalized} "
    return any(needle in f" {normalize(text)} " for text in [block.text, *titles])


def definition_rejections(doc: Document, first: dict[str, ClauseLabel]) -> dict[str, str]:
    """Termes libres refusés, conservés seulement pour l'alerte AD-8 du rapport."""
    rejected: dict[str, str] = {}
    for block_id, label in first.items():
        if label.kind != "definition" or label.defines is None:
            continue
        if not _define_is_anchored(doc, doc.block(block_id), label.defines):
            rejected[block_id] = label.defines
    return rejected


def assemble(doc: Document, first: dict[str, ClauseLabel], second: dict[str, ClauseLabel],
             settings: Settings, *, preserve_block_ids: set[str] | None = None) -> Document:
    """Fusionne les lectures puis résout articles, relations et scopes sans heuristique documentaire."""
    preserve_block_ids = preserve_block_ids or set()
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
        if label is None and block.block_id in preserve_block_ids:
            blocks.append(block.model_copy(deep=True))
            continue
        # AD-2 : un titre structure l'arbre mais n'est jamais citable seul. Même si T1 l'étiquette
        # juridiquement, le code restaure sa nature structurelle et écarte toutes les métadonnées.
        if label is None or label.kind == "autre" or structural_kind == "heading":
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
        # Le propriétaire structurel n'est pas une portée. Pour une définition, la portée reste
        # vide tant qu'un `scope_articles` résolu ou une dérogation locale ne prouve pas la branche
        # qu'elle gouverne. Les autres clauses décisionnelles portent bien sur leur branche propre.
        if label.kind in LEGAL_KINDS - {"definition"} or label.kind == "renvoi":
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
                continue
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
            if target is None or (block.kind != "definition" and target not in owner_subtree):
                unresolved.append(original_article)
                scope_ok = False
            else:
                scope_nodes.append(target)
        if scope_ok:
            block.scope_node_ids = list(dict.fromkeys(scope_nodes))

        if block.kind == "definition":
            candidate = meta["defines"]
            block.defines = candidate if _define_is_anchored(typed, block, candidate) else None

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

    typed = recompute_semantic_scopes(typed)
    typed = Document.model_validate(typed.model_dump())
    final_identity = [
        (block.block_id, block.text, block.loc, block.seq, block.page, block.bbox,
         block.continues, [line.model_dump() for line in block.lines])
        for block in typed.blocks
    ]
    if final_identity != original_identity:
        raise ValueError("le typage a modifié texte, lignes, coordonnées, ordre ou identifiants")
    return typed


def _lowest_common_ancestor(doc: Document, node_ids: list[str]) -> str | None:
    if not node_ids:
        return None
    parent = {child: node.node_id for node in doc.nodes for child in node.children}

    def path(node_id: str) -> list[str]:
        out = [node_id]
        while out[-1] in parent:
            out.append(parent[out[-1]])
        return out

    paths = [path(node_id) for node_id in node_ids]
    common = set(paths[0]).intersection(*map(set, paths[1:]))
    return next((node_id for node_id in paths[0] if node_id in common), None)


def _definition_scope_root(doc: Document, block_id: str) -> str | None:
    """Branche prouvée d'une dérogation, ou `None` si elle ne peut être distinguée du document."""
    parent = {child: node.node_id for node in doc.nodes for child in node.children}
    current = doc.node_of(block_id)
    child_blocks = {block_id}
    while True:
        candidate_blocks = set(_subtree_items(doc, current)[0])
        outside_article = candidate_blocks - child_blocks
        if current in parent and any(
                doc.block(candidate).kind in {"garantie", "exclusion"}
                and doc.block(candidate).kind_confirmed
                for candidate in outside_article):
            return current
        if current not in parent:
            return None
        child_blocks = candidate_blocks
        current = parent[current]


def recompute_semantic_scopes(doc: Document) -> Document:
    """Reprojette les portées depuis les métadonnées résolues, sans nouvel appel de typage.

    Les sorties de modèle (`kind`, `overrides`, relations et `scope_node_ids`) restent inchangées.
    Cette fonction est donc aussi la voie de régénération d'un artefact déjà typé quand seule la
    projection déterministe de portée évolue.
    """
    projected = Document.model_validate(doc.model_dump())
    for block in projected.blocks:
        if block.kind != "definition":
            continue
        if block.scope_node_ids:
            block.scope_node_id = _lowest_common_ancestor(projected, block.scope_node_ids)
        elif block.overrides:
            block.scope_node_id = _definition_scope_root(projected, block.block_id)
        else:
            block.scope_node_id = None
    return _apply_node_scopes(projected)


def _apply_node_scopes(doc: Document) -> Document:
    """Écrit seul la portée sémantique, depuis des liens résolus et jamais depuis un numéro."""
    parent = {child: node.node_id for node in doc.nodes for child in node.children}
    signals: dict[str, str] = {}

    def signal(node_id: str, kind: str) -> None:
        # Une restriction/exception est plus conservatrice qu'une spécialisation si les deux
        # signaux convergent sur le même nœud.
        if signals.get(node_id) != "special":
            signals[node_id] = kind

    for block in doc.blocks:
        source = doc.node_of(block.block_id)
        source_subtree = set(_subtree_items(doc, source)[1])
        # Une clause restreinte à des racines explicites prouve que ces sous-arbres ne sont pas le
        # socle. Le signal se propage à tous leurs descendants, y compris aux garanties qu'ils portent.
        for target_node in block.scope_node_ids:
            signal(target_node, "special")
        if block.kind == "definition" and block.overrides:
            if block.scope_node_id:
                signal(block.scope_node_id, "special")
            continue
        special_targets = [block.overrides, block.relation.exception_de]
        if any(target is not None and doc.node_of(target) not in source_subtree for target in special_targets):
            signal(source, "special")
            continue
        # Une garantie confirmée qui dépend, par une relation ou un renvoi résolu, d'une garantie
        # confirmée hors de son propre sous-arbre est une extension. Les refs vers une définition,
        # une condition ou un bloc non confirmé ne portent pas ce signal ; l'absence de lien non plus.
        relation_target = block.relation.specialise
        relation_signal = (
            block.kind == "garantie"
            and block.kind_confirmed
            and relation_target is not None
            and doc.block(relation_target).kind == "garantie"
            and doc.block(relation_target).kind_confirmed
            and doc.node_of(relation_target) not in source_subtree
        )
        ref_signal = block.kind == "garantie" and block.kind_confirmed and any(
            target is not None
            and doc.block(target).kind == "garantie"
            and doc.block(target).kind_confirmed
            and doc.node_of(target) not in source_subtree
            for target in block.refs
        )
        if relation_signal or ref_signal:
            signal(source, "extension")

    nodes: list[Node] = []
    for node in doc.nodes:
        current = node.node_id
        inherited = None
        while True:
            if current in signals:
                inherited = signals[current]
                break
            if current not in parent:
                break
            current = parent[current]
        nodes.append(node.model_copy(
            update={"scope": node.scope.model_copy(update={"kind": inherited or "commun"})}, deep=True,
        ))
    return Document.model_validate(doc.model_copy(update={"nodes": nodes}, deep=True).model_dump())


def _migration_overlay(doc_dir: Path, doc: Document,
                       lecture: Lecture | None = None) -> dict[str, dict[str, Any]]:
    path = doc_dir / "typing.manual.json"
    if lecture is not None:
        path = lecture.reel(path)
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
    """Publie le lot **complet** de l'opération de typage — d'un seul geste, par la racine.

    Story 4.5, tour de racine unique, fait 1. Cette fonction faisait `os.replace(temp, path)` sur
    chacune de ses cibles, `data/manifest.json` compris : sur un manifest couvert par une racine de
    publication, cela **remplaçait le lien statique par un fichier ordinaire** et sortait le
    manifest du pointeur pour de bon — la destruction exacte que la docstring de `write_atomic`
    décrit comme « une seule fois suffirait », par un chemin qui n'appelle pas `write_atomic`.

    Et sa restauration séquentielle — relire les octets d'entrée, puis les réécrire cible par cible
    sur `except Exception` — est précisément la forme que les tours correctifs 1/3 et 2/3 ont
    refusée pour les évals : un protocole qui *défait* ce qu'il a déjà fait n'est pas tout-ou-rien,
    il admet qu'après épuisement une cible reste dans le nouvel état. Elle ne pouvait pas rester la
    forme de l'ingestion.

    Le lot est donc remis **entier** — les fichiers écrits *et* la suppression de l'overlay — à
    `publier_artefacts`, qui le bascule par la racine quand elle en couvre les cibles (même
    `flock`, même génération inactive, même unique `os.replace` du pointeur) et garde le chemin
    d'avant quand aucune racine ne les couvre. Une suppression est membre du lot au même titre
    qu'une écriture : sans cela, retirer l'overlay resterait un `unlink` à un rang où une exception
    laisserait l'un fait et l'autre non.

    Le typage continue de faire exactement ce qu'il faisait ; ce qui change est le chemin
    d'écriture.
    """
    publier_artefacts([*files.items(), *((path, None) for path in delete)])


def _load(doc_dir: Path) -> tuple[Document, Report, dict[str, Any], ManifestEntry, dict[str, dict[str, Any]]]:
    """Le corpus du typage, lu **d'une seule génération** (story 4.5, N1).

    Six résolutions indépendantes du pointeur — manifest, document, rapport, overlay — décidaient
    ensemble de ce que la transaction publierait : une bascule tombant entre deux d'entre elles
    faisait typer un document que le manifest ne décrit plus. Le repère est pincé une fois, ici, et
    toute la lecture passe par lui ; les octets **hachés** sont les octets **parsés**.
    """
    return relire(doc_dir.parent, lambda lecture: _load_pince(doc_dir, lecture))


def _load_pince(doc_dir: Path, lecture: Lecture) -> tuple[
        Document, Report, dict[str, Any], ManifestEntry, dict[str, dict[str, Any]]]:
    manifest_path = doc_dir.parent / "manifest.json"
    raw_manifest = _manifest_lu(lecture, manifest_path)
    raw_entry = raw_manifest.get(doc_dir.name)
    if raw_entry is None:
        raise ValueError(f"{doc_dir.name}: absent du manifest")
    entry = TypeAdapter(ManifestEntry).validate_python(raw_entry)
    doc_path = doc_dir / "document.json"
    report_path = doc_dir / "report.json"
    doc_bytes = lecture.reel(doc_path).read_bytes()
    if hashlib.sha256(doc_bytes).hexdigest() != entry.document_hash:
        raise ValueError("document_hash différent du manifest; relancer l'ingestion PDF")
    doc = Document.model_validate_json(doc_bytes)
    report = Report.model_validate_json(lecture.reel(report_path).read_bytes())
    pages_charabia(report)  # préflight avant client/API : un ancien rapport doit être réingéré
    if doc.doc_id != doc_dir.name or report.doc_id != doc.doc_id:
        raise ValueError("doc_id incohérent entre dossier, document et rapport")
    if entry.status != "servi" or report.blocking:
        raise ValueError("le document doit être servi et sans bloquant avant typage")
    if (doc.source_hash, doc.ingest_fingerprint, doc.edition) != (
            entry.source_hash, entry.ingest_fingerprint, entry.edition):
        raise ValueError("source_hash, ingest_fingerprint ou édition incohérent avec le manifest")
    overlay_path = doc_dir / "typing.manual.json"
    overlay_present = lecture.fichier(overlay_path)
    if overlay_present != (entry.overlay_hash is not None):
        raise ValueError("présence de typing.manual.json incohérente avec overlay_hash")
    if overlay_present and hashlib.sha256(
            lecture.reel(overlay_path).read_bytes()).hexdigest() != entry.overlay_hash:
        raise ValueError("overlay_hash différent du manifest; relancer l'ingestion PDF")
    return doc, report, raw_manifest, entry, _migration_overlay(doc_dir, doc, lecture)


def _manifest_lu(lecture: Lecture, manifest_path: Path) -> dict[str, Any]:
    """`read_manifest`, mais depuis le repère pincé — même contrôle, une seule génération."""
    octets = lecture.octets(manifest_path)
    return _manifest_valide(octets, manifest_path)


def _manifest_du_verrou(lecture: LectureDuLot, manifest_path: Path) -> dict[str, Any]:
    """Le manifest **réellement publié**, lu dans la transaction qui va le republier (N3)."""
    return _manifest_valide(lecture.octets(manifest_path), manifest_path)


def _manifest_valide(octets: bytes | None, manifest_path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(octets.decode("utf-8")) if octets else {}
    if not isinstance(raw, dict):
        raise ValueError(f"{manifest_path} : un objet JSON {{doc_id: entrée}} est attendu")
    return raw


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
                transport: Literal["batch", "standard", "standard-resume"] = "batch",
                resume_campaign: str | None = None,
                resume_payload_sha256: str | None = None,
                resume_payload_proofs_map: dict[str, str] | None = None,
                resume_justification: str | None = None,
                prior_cost_eur: float = 0.0,
                output: Any = sys.stdout, sleep: Any = time.sleep,
                now: Any = time.monotonic) -> TypingResult | None:
    if transport not in {"batch", "standard", "standard-resume"}:
        raise ValueError(f"transport inconnu {transport!r}; aucun appel ni lot soumis")
    if not math.isfinite(prior_cost_eur) or prior_cost_eur < 0:
        raise ValueError("--prior-cost-eur doit être un nombre fini >= 0; aucun appel soumis")
    # **Préflight de couverture, avant la moindre soumission** (revue du tour de racine unique,
    # constat 6). Le lot du typage est connu d'avance — document, rapport, retrait de l'overlay,
    # manifest — et le refus « lot mixte » tombait au tout dernier geste : un typage LLM entièrement
    # payé était jeté pour une disposition qu'on pouvait vérifier ici, gratuitement.
    verifier_couverture_du_lot([doc_dir / "document.json", doc_dir / "report.json",
                                doc_dir / "typing.manual.json", doc_dir.parent / "manifest.json"])
    doc, report, raw_manifest, old_entry, migration = _load(doc_dir)
    reused_block_ids = _typing_reused_ids(report, doc)
    citable = [
        block for block in doc.blocks
        if is_citable(block) and block.block_id not in reused_block_ids
    ]
    campaign = campaign_fingerprint(doc, settings, citable)
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
    if transport == "standard-resume":
        if not first_batch_id or second_batch_id or legacy_resume:
            raise ValueError(
                "--transport standard-resume exige --first-batch-id seul et interdit "
                "--second-batch-id/--legacy-resume"
            )
        if resume_campaign is None or re.fullmatch(r"[0-9a-f]{64}", resume_campaign) is None:
            raise ValueError("--resume-campaign doit être l'empreinte SHA-256 complète du lot repris")
        if (resume_payload_sha256 is None
                or re.fullmatch(r"[0-9a-f]{64}", resume_payload_sha256) is None):
            raise ValueError(
                "--resume-payload-sha256 doit prouver l'identité exacte des requêtes du lot repris"
            )
        resume_justification = " ".join((resume_justification or "").split())
        if not resume_justification or len(resume_justification) > 500:
            raise ValueError("--resume-justification doit contenir 1 à 500 caractères auditables")
        print(
            f"transport standard-resume: lot lecture 1 {first_batch_id}, campagne reprise "
            f"{resume_campaign}; aucune nouvelle soumission Batch",
            file=output,
        )
    elif any(value is not None for value in (
            resume_campaign, resume_payload_sha256, resume_payload_proofs_map,
            resume_justification)):
        raise ValueError(
            "les preuves --resume-* ne sont acceptées qu'avec --transport standard-resume"
        )
    if transport == "standard" and any(value is not None for value in (
            first_batch_id, second_batch_id, legacy_resume)):
        raise ValueError(
            "--transport standard interdit les IDs Batch et --legacy-resume"
        )
    plan_campaign = resume_campaign if transport == "standard-resume" else campaign
    first_plans = requests_for(
        doc, citable, 1, settings, legacy_custom_ids=legacy_resume is not None,
        campaign_override=plan_campaign,
    )
    current_payload_proofs = resume_payload_proofs(first_plans, settings)
    current_payload_sha256 = _payload_proofs_fingerprint(current_payload_proofs)
    resumed_payload_proofs = resume_payload_proofs_map or current_payload_proofs
    resumed_payload_sha256 = _payload_proofs_fingerprint(resumed_payload_proofs)
    changed_payload_ids: set[str] = set()
    if transport == "standard-resume":
        expected_ids = {plan.custom_id for plan in first_plans}
        if (set(resumed_payload_proofs) != expected_ids
                or any(re.fullmatch(r"[0-9a-f]{64}", value) is None
                       for value in resumed_payload_proofs.values())):
            raise ValueError("reprise refusée: manifeste de payload incomplet ou mal formé")
        if resumed_payload_sha256 != resume_payload_sha256:
            raise ValueError(
                "reprise refusée avant appel: certificat du manifeste payload "
                f"{resumed_payload_sha256} différent de la preuve {resume_payload_sha256}"
            )
        changed_payload_ids = {
            cid for cid in expected_ids
            if current_payload_proofs[cid] != resumed_payload_proofs[cid]
        }
        print(
            f"reprise auditée: campagne lot={resume_campaign}; campagne courante={campaign}; "
            f"payload_lot={resumed_payload_sha256}; payload_courant={current_payload_sha256}; "
            f"plans_modifiés={len(changed_payload_ids)}; justification={resume_justification}",
            file=output,
        )
    # Pire cas avant le premier euro : chaque bloc de lecture 1 est juridique et repasse en lecture 2.
    worst_second = requests_for(
        doc, citable, 2, settings, legacy_custom_ids=legacy_resume is not None,
        campaign_override=plan_campaign,
    )
    if transport in {"batch", "standard-resume"}:
        estimate = round(majorant_eur(first_plans, settings) + majorant_eur(worst_second, settings), 4)
        estimate_label = f"majorant Batch {estimate:.4f} € (remise {BATCH_DISCOUNT})"
    else:
        estimate = round(sum(standard_majorant_eur(plan, settings)
                             for plan in [*first_plans, *worst_second]), 4)
        estimate_label = f"majorant Messages standard {estimate:.4f} € (sans remise Batch)"
    ceiling = settings.type_clauses_max_cost_eur if max_cost is None else max_cost
    if not math.isfinite(ceiling) or ceiling <= 0:
        raise ValueError("--max-cost doit être un nombre fini > 0; aucun lot soumis")
    if prior_cost_eur >= ceiling:
        raise ValueError(
            f"coût réel déjà acquis {prior_cost_eur:.4f} € >= plafond {ceiling:.4f} €; aucun appel soumis"
        )
    print(f"delta: {len(reused_block_ids)} bloc(s) réutilisé(s); lecture 1: {len(citable)} bloc(s), "
          f"{len(first_plans)} requête(s); lecture 2: au plus "
          f"{len(citable)} bloc(s), {len(worst_second)} requête(s); {estimate_label} "
          f"/ plafond cumulé {ceiling:.4f} €", file=output)
    if transport in {"batch", "standard"} and prior_cost_eur + estimate > ceiling:
        raise ValueError(
            f"majorant cumulé {prior_cost_eur + estimate:.4f} € "
            f"({prior_cost_eur:.4f} € acquis + {estimate:.4f} € delta) > plafond "
            f"{ceiling:.4f} €; aucun lot soumis"
        )
    if dry_run:
        return None
    if client is None:
        if cle_absente(settings):
            raise PermissionError("ANTHROPIC_API_KEY absente; aucun lot soumis")
        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            max_retries=0 if transport in {"standard", "standard-resume"} else 2,
        )

    reused_requests = 0
    replayed_requests = 0
    standard_requests = 0
    batch_cost = 0.0
    standard_cost = 0.0
    if transport == "standard-resume":
        recovered, batch_cost, failures = execute_batch(
            client, first_plans, settings, batch_id=first_batch_id,
            output=output, sleep=sleep, now=now,
        )
        batch_missing_plans = [plan for plan in first_plans if plan.custom_id not in recovered]
        missing_ids = {plan.custom_id for plan in batch_missing_plans}
        represented = {
            cid for cid in missing_ids if sum(failure.startswith(f"{cid}:") for failure in failures) == 1
        }
        if len(failures) != len(batch_missing_plans) or represented != missing_ids:
            raise BatchFailure(
                "lot repris incohérent (résultat inconnu, dupliqué ou absent sans état); "
                f"coût Batch réel {batch_cost:.4f} €; aucun artefact écrit"
            )
        replayed_requests = sum(cid in recovered for cid in changed_payload_ids)
        reused_requests = len(recovered) - replayed_requests
        missing_plans = [
            plan for plan in first_plans
            if plan.custom_id not in recovered or plan.custom_id in changed_payload_ids
        ]
        trusted_recovered = {
            cid: text for cid, text in recovered.items() if cid not in changed_payload_ids
        }
        if prior_cost_eur and prior_cost_eur < batch_cost:
            raise BatchFailure(
                f"coût antérieur déclaré {prior_cost_eur:.4f} € < coût Batch récupéré "
                f"{batch_cost:.4f} €; aucun appel standard"
            )
        acquired_before = prior_cost_eur or batch_cost
        guard = _CostGuard(ceiling, spent=acquired_before)
        completed, first_standard_cost = execute_standard(
            client, missing_plans, settings, guard=guard, output=output, sleep=sleep,
        )
        first_texts = {
            plan.custom_id: (trusted_recovered | completed)[plan.custom_id]
            for plan in first_plans
        }
        standard_requests += len(missing_plans)
        standard_cost += first_standard_cost
        print(f"coût cumulé acquis après lecture 1: {guard.spent:.4f} €", file=output)
        first_cost = round(batch_cost + first_standard_cost, 4)
        print(
            f"lecture 1 reprise: {reused_requests} résultat(s) Batch réutilisé(s), "
            f"{replayed_requests} payload(s) succès rejoué(s), "
            f"{len(missing_plans)} appel(s) standard",
            file=output,
        )
    elif transport == "standard":
        guard = _CostGuard(ceiling, spent=prior_cost_eur)
        first_texts, first_cost = execute_standard(
            client, first_plans, settings, guard=guard, output=output, sleep=sleep,
        )
        standard_requests += len(first_plans)
        standard_cost += first_cost
        print(f"lecture 1 standard initiale: {len(first_plans)} appel(s), "
              f"coût cumulé acquis {guard.spent:.4f} €", file=output)
    else:
        first_texts, first_cost, failures = execute_batch(
            client, first_plans, settings, batch_id=first_batch_id,
            output=output, sleep=sleep, now=now,
        )
        if failures:
            raise BatchFailure(f"lecture 1 incomplète (coût réel {first_cost:.4f} €): " + "; ".join(failures))
        batch_cost = first_cost
    try:
        first = parse_reading(first_texts, first_plans, doc, settings, require_all_labels=True)
    except ValueError as exc:
        raise BatchFailure(
            f"lecture 1 hors schéma (coût réel {first_cost:.4f} €); aucun artefact écrit: {exc}"
        ) from exc
    legal_blocks = [doc.block(block_id) for block_id, label in first.items() if label.kind in LEGAL_KINDS]
    second_plans = requests_for(
        doc, legal_blocks, 2, settings, legacy_custom_ids=legacy_resume is not None,
        campaign_override=plan_campaign,
    )
    actual_estimate = round(majorant_eur(first_plans, settings) + majorant_eur(second_plans, settings), 4)
    if transport == "batch" and actual_estimate > ceiling:
        raise BatchFailure(f"les candidats de lecture 2 portent le majorant à {actual_estimate:.4f} € > plafond; "
                           "lecture 1 facturée, aucun artefact écrit")
    if transport in {"standard", "standard-resume"}:
        second_texts, second_cost = execute_standard(
            client, second_plans, settings, guard=guard, output=output, sleep=sleep,
        )
        standard_requests += len(second_plans)
        standard_cost += second_cost
        print(f"coût cumulé acquis après lecture 2: {guard.spent:.4f} €", file=output)
    else:
        second_texts, second_cost, failures = execute_batch(
            client, second_plans, settings, batch_id=second_batch_id,
            output=output, sleep=sleep, now=now,
        )
        if failures:
            total_cost = round(first_cost + second_cost, 4)
            raise BatchFailure(f"lecture 2 incomplète (coût réel cumulé {total_cost:.4f} €): "
                               + "; ".join(failures))
        batch_cost += second_cost
    total_cost = round(batch_cost + standard_cost, 4)
    cumulative_cost = round((prior_cost_eur or batch_cost) + standard_cost, 4)
    # Une omission de label **dans** une réponse valide est un désaccord T2, pas un lot T4 incomplet.
    try:
        second = parse_reading(second_texts, second_plans, doc, settings, require_all_labels=False)
        rejected_definitions = definition_rejections(doc, first)
        typed = assemble(doc, first, second, settings, preserve_block_ids=reused_block_ids)
        _check_migration(typed, migration)
    except ValueError as exc:
        raise BatchFailure(
            f"assemblage refusé après les deux lectures (coût réel cumulé {total_cost:.4f} €); "
            f"aucun artefact écrit: {exc}"
        ) from exc
    typed_report = enrich_typing_report(
        canoniser_transition_apres_typage(report), typed,
        rejected_definitions=rejected_definitions,
    )
    if transport in {"standard", "standard-resume"}:
        if transport == "standard":
            transport_detail = (
                f"standard initial; aucune API Batch; appels_standard={standard_requests}; "
                f"coût_standard={standard_cost:.4f} EUR; coût_antérieur={prior_cost_eur:.4f} EUR; "
                f"coût_cumulé={cumulative_cost:.4f} EUR; plafond={ceiling:.4f} EUR"
            )
        else:
            transport_detail = (
                f"standard-resume; lot lecture 1={first_batch_id}; campagne={resume_campaign}; "
                f"campagne_courante={campaign}; payload_lot={resumed_payload_sha256}; "
                f"payload_courant={current_payload_sha256}; justification={resume_justification}; "
                f"réutilisés={reused_requests}; rejoués_payload={replayed_requests}; "
                f"appels_standard={standard_requests}; coût_batch={batch_cost:.4f} EUR; "
                f"coût_standard={standard_cost:.4f} EUR; coût_antérieur={prior_cost_eur:.4f} EUR; "
                f"coût_cumulé={cumulative_cost:.4f} EUR; plafond={ceiling:.4f} EUR"
            )
        typed_report.checks.append(Check(
            name="typage_transport", level="info",
            detail=transport_detail,
        ))
        transport_stats: dict[str, int | float | str] = {
            "typage_transport": transport,
            "typage_current_campaign": campaign,
            "typage_current_payload_sha256": current_payload_sha256,
            "typage_replayed_payload_requests": replayed_requests,
            "typage_changed_payload_requests": len(changed_payload_ids),
            "typage_changed_payload_custom_ids": ",".join(sorted(changed_payload_ids)),
            "typage_reused_requests": reused_requests,
            "typage_standard_requests": standard_requests,
            "typage_batch_cost_eur": round(batch_cost, 4),
            "typage_standard_cost_eur": round(standard_cost, 4),
            "typage_total_cost_eur": total_cost,
            "typage_prior_cost_eur": round(prior_cost_eur, 4),
            "typage_cumulative_cost_eur": cumulative_cost,
            "typage_cost_ceiling_eur": round(ceiling, 4),
            "blocs_typage_reutilises": len(reused_block_ids),
            "blocs_typage_rejoues": len(citable),
        }
        if transport == "standard-resume":
            transport_stats.update({
                "typage_resume_batch_id": first_batch_id or "",
                "typage_resume_campaign": resume_campaign or "",
                "typage_resume_payload_sha256": resumed_payload_sha256,
                "typage_resume_justification": resume_justification or "",
            })
        typed_report.stats.update(transport_stats)
    doc_text = document_json(typed)
    document_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
    # **Le typage change `document.json`, donc l'arbre attesté** (story 4.5, revue B4). Les deux
    # attestations d'ingestion sont préservées par `enrich_typing_report` (elles ne sont pas des
    # checks de typage), mais elles nomment l'ancien `document_hash` : telles quelles, elles ne
    # décrivent plus rien, et un document typé perdrait sa preuve de structure **sans que rien ne le
    # dise** — le gate `full` la verrait simplement absente. Elles sont donc ré-attestées sur les
    # empreintes neuves, avec les mêmes fonctions que l'ingestion, dans le même ordre.
    #
    # `renouveler=True` : rien n'est **fabriqué** au passage. Le typage réécrit un rapport, il ne
    # re-vérifie ni l'arbre ni la proposition de structure ; il n'a donc rien à attester qui ne l'ait
    # déjà été. Un rapport sans attestation — parce qu'aucune structure n'a été proposée, parce que
    # l'arbre a été refusé, ou simplement parce qu'il date d'avant la story 4.5 — ressort exactement
    # tel quel, et le témoin du gate reste rouge jusqu'à une vraie réingestion.
    rapport_publie: Report | None = None

    def fabriquer(lecture: LectureDuLot) -> tuple[ManifestEntry, list[tuple[Path, str | None]]]:
        """L'entrée publiée, décidée **sous le verrou** (story 4.5, N3).

        `structure_hash` et les champs repris de l'entrée antérieure — `source_hash`,
        `ingest_fingerprint`, `edition` — venaient d'une lecture faite par `_load` **avant des
        minutes d'appels de modèle**. Une réingestion publiée entre-temps rendait donc l'entrée du
        typage descriptive d'un document qui n'existe plus, et le typage écrasait la réingestion.
        Les trois champs sont relus ici, dans la transaction qui publie, et **opposés** à ceux que
        le typage a effectivement typés : une divergence est un refus, jamais une publication.
        """
        nonlocal rapport_publie
        manifest_publie = _manifest_du_verrou(lecture, doc_dir.parent / "manifest.json")
        brut = manifest_publie.get(doc.doc_id)
        entree_publiee = (TypeAdapter(ManifestEntry).validate_python(brut)
                          if isinstance(brut, dict) else None)
        if entree_publiee is None:
            raise BatchFailure(
                f"{doc.doc_id} a disparu du manifest pendant le typage : rien n'est publié")
        repris = (entree_publiee.source_hash, entree_publiee.ingest_fingerprint,
                  entree_publiee.edition, entree_publiee.document_hash)
        attendus = (old_entry.source_hash, old_entry.ingest_fingerprint, old_entry.edition,
                    old_entry.document_hash)
        if repris != attendus:
            raise BatchFailure(
                "le document a été réingéré pendant le typage (source_hash, ingest_fingerprint, "
                "édition ou document_hash du manifest publié diffèrent de ceux qui ont été typés) "
                "— rien n'est publié, relancer le typage")
        structure_courante = lecture.empreinte(doc_dir / STRUCTURE_FILE)
        rapport = attester_structure(typed_report, document_hash=document_hash,
                                     structure_hash=structure_courante or "", renouveler=True)
        rapport = attester_arbre(rapport, document_hash=document_hash,
                                 ingest_fingerprint=entree_publiee.ingest_fingerprint,
                                 renouveler=True)
        rapport_publie = rapport
        report_text = json.dumps(rapport.model_dump(), indent=2, ensure_ascii=False) + "\n"
        entree = ManifestEntry(
            status="quarantaine" if rapport.blocking else "servi",
            source_hash=entree_publiee.source_hash,
            ingest_fingerprint=entree_publiee.ingest_fingerprint,
            document_hash=document_hash,
            edition=entree_publiee.edition,
            overlay_hash=None,
            # Story 4.5 : `structure.json` n'est ni écrit ni supprimé ici — l'empreinte est donc
            # relue du bundle publié, plutôt que recopiée de l'ancienne entrée (qui pourrait
            # décrire un fichier qui a bougé depuis).
            structure_hash=structure_courante,
            gate=None,
        )
        return entree, [(doc_dir / "document.json", doc_text),
                        (doc_dir / "report.json", report_text),
                        (doc_dir / "typing.manual.json", None)]

    # Tour de racine unique, fait 2 : le manifest est **relu et fusionné sous le verrou** de la
    # racine, jamais depuis le `raw_manifest` que `_load` avait lu au début de l'opération — le
    # typage dure des minutes, et une ingestion concurrente publiée entre-temps était écrasée par
    # cette fusion périmée. Le lot complet du typage — document, rapport, manifest et le retrait de
    # l'overlay — bascule au même appel.
    merged_entry = fusionner_et_publier(
        doc_dir.parent / "manifest.json", doc.doc_id, fabriquer,
        cibles=[doc_dir / "document.json", doc_dir / "report.json",
                doc_dir / "typing.manual.json"])
    if rapport_publie is None:  # pragma: no cover — `assert` disparaîtrait sous `python -O`
        raise BatchFailure("la fabrique n'a pas produit de rapport : rien n'a été publié")
    typed_report = rapport_publie
    return TypingResult(document=typed, report=typed_report, entry=merged_entry,
                        cost_eur=total_cost,
                        first_requests=len(first_plans), second_requests=len(second_plans),
                        transport=transport, reused_requests=reused_requests,
                        replayed_requests=replayed_requests,
                        standard_requests=standard_requests, batch_cost_eur=round(batch_cost, 4),
                        standard_cost_eur=round(standard_cost, 4),
                        prior_cost_eur=round(prior_cost_eur, 4),
                        cumulative_cost_eur=cumulative_cost)


def run(doc_dir: Path, *, settings: Settings, client: Any = None, dry_run: bool = False,
        max_cost: float | None = None, first_batch_id: str | None = None,
        second_batch_id: str | None = None, legacy_resume: str | None = None,
        transport: Literal["batch", "standard", "standard-resume"] = "batch",
        resume_campaign: str | None = None,
        resume_payload_sha256: str | None = None,
        resume_payload_proofs_map: dict[str, str] | None = None,
        resume_justification: str | None = None,
        prior_cost_eur: float = 0.0,
        output: Any = sys.stdout, sleep: Any = time.sleep,
        now: Any = time.monotonic) -> TypingResult | None:
    with _exclusive_lock(doc_dir):
        return _run_locked(
            doc_dir, settings=settings, client=client, dry_run=dry_run, max_cost=max_cost,
            first_batch_id=first_batch_id, second_batch_id=second_batch_id,
            legacy_resume=legacy_resume, transport=transport, resume_campaign=resume_campaign,
            resume_payload_sha256=resume_payload_sha256,
            resume_payload_proofs_map=resume_payload_proofs_map,
            resume_justification=resume_justification, prior_cost_eur=prior_cost_eur,
            output=output, sleep=sleep, now=now,
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
        "--transport", choices=("batch", "standard", "standard-resume"), default="batch",
        help=("transport explicite; standard part de zéro via Messages, standard-resume complète "
              "un lot existant; le défaut production reste Batch"),
    )
    parser.add_argument(
        "--resume-campaign",
        help="empreinte SHA-256 complète dont les custom_id du premier lot doivent être reconstruits",
    )
    parser.add_argument(
        "--resume-payload-sha256",
        help="preuve SHA-256 du payload exact du lot repris, hors transport et custom_id",
    )
    parser.add_argument(
        "--resume-payload-proof", type=Path,
        help="manifeste JSON custom_id→SHA-256 créé sur l'artefact exact du lot repris",
    )
    parser.add_argument(
        "--resume-justification",
        help="justification durable de l'écart entre campagne reprise et campagne courante",
    )
    parser.add_argument(
        "--prior-cost-eur", type=float, default=0.0,
        help="coût réel déjà acquis à compter dans le plafond cumulé avant tout nouvel appel",
    )
    parser.add_argument(
        "--legacy-resume", metavar="SHA256_CAMPAGNE",
        help="migration auditée de deux lots antérieurs; exige leurs deux IDs et l'empreinte courante",
    )
    args = parser.parse_args(argv)
    if len(args.doc_id) > DOC_ID_MAX or not DOC_ID_RE.fullmatch(args.doc_id):
        print(f"doc_id invalide (slug [a-z0-9-]+ de {DOC_ID_MAX} caractères maximum attendu): "
              f"{args.doc_id!r}", file=sys.stderr)
        return 2
    if args.max_cost is not None and (not math.isfinite(args.max_cost) or args.max_cost <= 0):
        print("--max-cost doit être un nombre fini > 0", file=sys.stderr)
        return 2
    # **Un entrypoint de production exige une racine installée** (story 4.5, N3), et le dit avant la
    # moindre soumission : le typage est le plus cher des chemins d'ingestion, et le refus d'un
    # `data-dir` non installé tombait après l'avoir payé. Un `--data` custom **installé** passe.
    doc_dir_pre = args.data / args.doc_id
    try:
        exiger_espace_installe([doc_dir_pre / "document.json", doc_dir_pre / "report.json",
                                doc_dir_pre / "typing.manual.json", args.data / "manifest.json"])
    except Exception as exc:  # noqa: BLE001 — une disposition absente n'est pas une trace Python
        print(f"refus : {exc}", file=sys.stderr)
        return 2
    settings = settings or get_settings()
    try:
        resume_payload_proofs_map = None
        if args.resume_payload_proof is not None:
            raw_proofs = json.loads(args.resume_payload_proof.read_text("utf-8"))
            if (not isinstance(raw_proofs, dict)
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           for key, value in raw_proofs.items())):
                raise ValueError("--resume-payload-proof doit être un objet JSON custom_id→SHA-256")
            resume_payload_proofs_map = raw_proofs
        result = run(args.data / args.doc_id, settings=settings, client=client, dry_run=args.dry_run,
                     max_cost=args.max_cost, first_batch_id=args.first_batch_id,
                     second_batch_id=args.second_batch_id, legacy_resume=args.legacy_resume,
                     transport=args.transport, resume_campaign=args.resume_campaign,
                     resume_payload_sha256=args.resume_payload_sha256,
                     resume_payload_proofs_map=resume_payload_proofs_map,
                     resume_justification=args.resume_justification,
                     prior_cost_eur=args.prior_cost_eur, output=output)
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
          f"{result.first_requests}+{result.second_requests} requête(s); "
          f"transport={result.transport}; réutilisés={result.reused_requests}; "
          f"appels_standard={result.standard_requests}", file=output)
    for check in result.report.checks:
        if check.name in {"corruption_decisionnelle", "unresolved_refs", "definition_introuvable",
                          "exclusion_sans_marqueur", "confiance_typage_faible", "kinds_non_confirmes",
                          "typage_clauses"}:
            print(f"[{check.level}] {check.name}: {check.detail}", file=output)
    return 1 if result.report.blocking else 0


if __name__ == "__main__":
    sys.exit(main())
