"""Rejeu **hors réseau** d'un typage déjà payé, depuis les couples requête/réponse de l'audit.

Une campagne de typage coûte des euros et des minutes. Quand c'est la *résolution* qui a fauté — la
règle qui décide, à partir des trois lectures, ce qui est confirmé — les lectures elles-mêmes, elles,
sont bonnes : elles sont dans `.audit/llm-calls.jsonl`, texte de réponse intact. Les rejouer ne
demande aucun client, aucune clé et aucun euro.

Ce module est cette voie, et rien d'autre : il ne construit jamais de client Anthropic, ne soumet
rien, et ne relit pas le PDF. Il reconstruit les plans **depuis les requêtes archivées** — jamais en
recalculant une empreinte de campagne, qui dépend de l'implémentation courante et ne correspondrait
donc plus aux `custom_id` du lot payé — puis rejoue exactement la chaîne de décision de production :
`parse_reading` / `parse_reading_tolerant` → `terminal_t2_decisions` → `assemble` → `terminal_effects`.

C'est aussi le banc de validation à 0 € d'une correction de cette résolution : le même audit, le même
document, deux règles, deux comptes de kinds juridiques confirmés.

Les primitives de chargement, de verrouillage et de publication sont importées telles quelles de
`type_clauses` : le rejeu doit publier par le **même** protocole que la voie payante, pas par une
copie qui pourrait en diverger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from server.app.config import REPO_ROOT, Settings, get_settings
from server.app.domain import Check, Document, ManifestEntry, Report, is_citable
from server.app.domain.artifact import document_artifact_uid
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE
from server.ingest.artifacts import (LectureDuLot, STRUCTURE_FILE, document_json,
                                     exiger_espace_installe, fusionner_et_publier,
                                     verifier_couverture_du_lot)
from server.ingest.report import (attester_arbre, attester_structure,
                                  canoniser_transition_apres_typage, enrich_typing_report)
from server.ingest.type_clauses import (BatchFailure, LEGAL_KINDS, RequestPlan,
                                        T2TerminalDecision, _exclusive_lock, _load,
                                        _manifest_du_verrou, assemble, decision_stats,
                                        definition_rejections, parse_reading,
                                        parse_reading_tolerant, terminal_effects,
                                        terminal_t2_decisions)

READING_RE = re.compile(r"^clauses-r(?P<reading>[123])-")
RUN_UID_PREFIX = "typing:"


@dataclass(frozen=True)
class AppelArchive:
    """Un couple requête/réponse du lot payé, ramené à ce dont la résolution a besoin."""

    custom_id: str
    reading: int
    block_ids: tuple[str, ...]
    params: dict[str, Any]
    text: str | None
    error_class: str | None

    @property
    def echec(self) -> bool:
        return self.text is None or self.error_class is not None


@dataclass(frozen=True)
class AuditLu:
    artifact_uid: str
    appels: dict[int, tuple[AppelArchive, ...]]
    lignes_illisibles: int
    lignes_ignorees: int

    def plans(self, reading: int) -> list[RequestPlan]:
        return [RequestPlan(custom_id=appel.custom_id, block_ids=appel.block_ids,
                            request={"custom_id": appel.custom_id, "params": appel.params})
                for appel in self.appels.get(reading, ())]

    def textes(self, reading: int) -> dict[str, str]:
        return {appel.custom_id: appel.text for appel in self.appels.get(reading, ())
                if appel.text is not None}

    def echecs(self, reading: int) -> set[str]:
        return {appel.custom_id for appel in self.appels.get(reading, ()) if appel.echec}


@dataclass(frozen=True)
class RejeuTypage:
    document: Document
    report: Report
    decisions: tuple[T2TerminalDecision, ...]
    audit: AuditLu
    reused_block_ids: frozenset[str]

    @property
    def blocs_juridiques(self) -> int:
        return sum(1 for block in self.document.blocks if block.kind in LEGAL_KINDS)

    @property
    def blocs_juridiques_confirmes(self) -> int:
        return sum(1 for block in self.document.blocks
                   if block.kind in LEGAL_KINDS and block.kind_source == "model_verified")


def _texte_de(response: Any) -> str | None:
    """Le texte rendu par le modèle, tel que l'audit l'a conservé — jamais un vide fabriqué."""
    if not isinstance(response, dict):
        return None
    parts = response.get("content")
    if not isinstance(parts, list):
        return None
    return "".join(str(part.get("text", "")) for part in parts
                   if isinstance(part, dict) and part.get("type") == "text")


def _blocs_de(params: Any) -> tuple[str, ...]:
    """Les `block_id` du plan, relus du payload utilisateur réellement envoyé."""
    messages = params.get("messages") if isinstance(params, dict) else None
    if not isinstance(messages, list) or not messages:
        raise ValueError("requête archivée sans message utilisateur")
    payload = json.loads(messages[0]["content"])
    blocks = payload["blocks"]
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("payload archivé sans bloc")
    return tuple(str(block["block_id"]) for block in blocks)


def lire_audit(audit_path: Path, *, artifact_uid: str | None = None) -> AuditLu:
    """Extrait d'un audit rotatif les appels de typage d'**un seul** artefact, sans en inventer.

    Un audit tourne : sa première ligne peut être coupée en deux par la rotation, et un même
    `custom_id` peut apparaître plusieurs fois (une tentative, puis sa reprise). Les lignes
    indécodables sont comptées plutôt que tues, et la **dernière** occurrence d'un `custom_id`
    l'emporte — c'est celle qui décrit ce que le lot a finalement obtenu.
    """
    par_id: dict[str, AppelArchive] = {}
    illisibles = 0
    ignorees = 0
    artefacts: set[str] = set()
    with audit_path.open("r", encoding="utf-8") as flux:
        for ligne in flux:
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                row = json.loads(ligne)
            except (json.JSONDecodeError, UnicodeDecodeError):
                illisibles += 1
                continue
            run_uid = str(row.get("run_uid", ""))
            if not run_uid.startswith(RUN_UID_PREFIX):
                ignorees += 1
                continue
            custom_id = run_uid[len(RUN_UID_PREFIX):]
            match = READING_RE.match(custom_id)
            if match is None:
                ignorees += 1
                continue
            uid = str(row.get("artifact_uid", ""))
            artefacts.add(uid)
            if artifact_uid is not None and uid != artifact_uid:
                continue
            error_class = row.get("error_class")
            params = row.get("request")
            par_id[custom_id] = AppelArchive(
                custom_id=custom_id, reading=int(match.group("reading")),
                block_ids=_blocs_de(params), params=params,
                text=_texte_de(row.get("response")),
                error_class=None if error_class is None else str(error_class))
    if artifact_uid is None:
        if len(artefacts) != 1:
            raise ValueError(
                f"{audit_path} porte {len(artefacts)} artefact(s) de typage "
                f"({sorted(artefacts)}); préciser --artifact-uid")
        artifact_uid = artefacts.pop()
    if not par_id:
        raise ValueError(f"{audit_path} ne contient aucun appel de typage pour {artifact_uid}")
    appels = {
        reading: tuple(appel for appel in par_id.values() if appel.reading == reading)
        for reading in (1, 2, 3)
    }
    if not appels[1]:
        raise ValueError(f"{audit_path} ne contient aucune première lecture pour {artifact_uid}")
    return AuditLu(artifact_uid=artifact_uid, appels=appels,
                   lignes_illisibles=illisibles, lignes_ignorees=ignorees)


def rejouer(doc: Document, report: Report, audit: AuditLu, settings: Settings) -> RejeuTypage:
    """La chaîne de décision de production, sur des textes archivés — sans client ni euro.

    Le périmètre réutilisé n'est pas relu du rapport mais **déduit de l'audit** : les blocs citables
    que la première lecture n'a pas planifiés sont exactement ceux que la campagne avait délégués au
    delta. Le rapport, lui, décrit déjà l'état d'après un typage et ne dit plus ce que celui-ci
    avait rejoué.
    """
    attendu = document_artifact_uid(document_uid=doc.doc_id, source_hash=doc.source_hash,
                                    ingest_fingerprint=doc.ingest_fingerprint)
    if attendu != audit.artifact_uid:
        raise ValueError(
            f"l'audit décrit l'artefact {audit.artifact_uid}, le document publié {attendu}; "
            "aucun rejeu : les deux ne parlent pas de la même ingestion")
    connus = {block.block_id for block in doc.blocks}
    inconnus = sorted({block_id for reading in (1, 2, 3)
                       for appel in audit.appels.get(reading, ())
                       for block_id in appel.block_ids} - connus)
    if inconnus:
        raise ValueError(f"l'audit porte des blocs absents du document : {inconnus[:5]}")

    first_plans, second_plans, third_plans = (audit.plans(reading) for reading in (1, 2, 3))
    first = parse_reading(audit.textes(1), first_plans, doc, settings, require_all_labels=True)
    second, echecs_t2 = parse_reading_tolerant(audit.textes(2), second_plans, doc, settings)
    arbitration, echecs_t3 = parse_reading_tolerant(audit.textes(3), third_plans, doc, settings)
    failed_t2 = audit.echecs(2) | echecs_t2
    failed_t3 = audit.echecs(3) | echecs_t3
    decisions = terminal_t2_decisions(
        first, second, second_plans, failed_plan_ids=failed_t2, arbitration=arbitration,
        arbitration_plans=third_plans, failed_arbitration_plan_ids=failed_t3,
        confidence_min=settings.type_clauses_arbitration_confidence_min,
        confidence_tolerance=settings.type_clauses_confidence_tolerance,
        model_run_uid=f"typing:rejeu:{audit.artifact_uid}")
    reused = frozenset(block.block_id for block in doc.blocks
                       if is_citable(block) and block.block_id not in first)
    typed = assemble(doc, first, second, settings, preserve_block_ids=set(reused),
                     decisions=decisions)
    decisions = terminal_effects(decisions, typed)
    typed_report = enrich_typing_report(
        canoniser_transition_apres_typage(report), typed,
        rejected_definitions=definition_rejections(doc, first))
    typed_report.stats.update(decision_stats(
        first, decisions, second_plans, third_plans,
        model_run_uid=f"typing:rejeu:{audit.artifact_uid}",
        consumed_plan_ids=set(audit.textes(2)) | failed_t2,
        failed_t2=failed_t2, timeout_t2=set(), failed_t3=failed_t3, timeout_t3=set()))
    typed_report.checks.append(Check(
        name="typage_rejeu_audit", level="info",
        detail=(f"décisions terminales recalculées hors réseau depuis {len(first_plans)}+"
                f"{len(second_plans)}+{len(third_plans)} appel(s) archivé(s) de "
                f"{audit.artifact_uid}; aucun appel, aucun euro")))
    return RejeuTypage(document=typed, report=typed_report, decisions=decisions, audit=audit,
                       reused_block_ids=reused)


def publier(doc_dir: Path, rejeu: RejeuTypage, old_entry: ManifestEntry) -> ManifestEntry:
    """Publie le document et le rapport recalculés, par le protocole de la voie payante."""
    doc_text = document_json(rejeu.document)
    document_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
    rapport_publie: Report | None = None

    def fabriquer(lecture: LectureDuLot) -> tuple[ManifestEntry, list[tuple[Path, str | None]]]:
        nonlocal rapport_publie
        brut = _manifest_du_verrou(lecture, doc_dir.parent / "manifest.json").get(
            rejeu.document.doc_id)
        entree_publiee = (TypeAdapter(ManifestEntry).validate_python(brut)
                          if isinstance(brut, dict) else None)
        if entree_publiee is None:
            raise BatchFailure(
                f"{rejeu.document.doc_id} a disparu du manifest pendant le rejeu : rien n'est publié")
        if (entree_publiee.source_hash, entree_publiee.ingest_fingerprint,
                entree_publiee.edition, entree_publiee.document_hash) != (
                old_entry.source_hash, old_entry.ingest_fingerprint,
                old_entry.edition, old_entry.document_hash):
            raise BatchFailure(
                "le document a été republié pendant le rejeu — rien n'est publié, relancer le rejeu")
        structure_courante = lecture.empreinte(doc_dir / STRUCTURE_FILE)
        rapport = attester_structure(rejeu.report, document_hash=document_hash,
                                     structure_hash=structure_courante or "", renouveler=True)
        rapport = attester_arbre(rapport, document_hash=document_hash,
                                 ingest_fingerprint=entree_publiee.ingest_fingerprint,
                                 renouveler=True)
        rapport_publie = rapport
        entree = ManifestEntry(
            status="quarantaine" if rapport.blocking else "servi",
            source_hash=entree_publiee.source_hash,
            ingest_fingerprint=entree_publiee.ingest_fingerprint,
            document_hash=document_hash, edition=entree_publiee.edition, overlay_hash=None,
            structure_hash=structure_courante, gate=None)
        report_text = json.dumps(rapport.model_dump(), indent=2, ensure_ascii=False) + "\n"
        return entree, [(doc_dir / "document.json", doc_text),
                        (doc_dir / "report.json", report_text)]

    entree = fusionner_et_publier(
        doc_dir.parent / "manifest.json", rejeu.document.doc_id, fabriquer,
        cibles=[doc_dir / "document.json", doc_dir / "report.json"])
    if rapport_publie is None:  # pragma: no cover — `assert` disparaîtrait sous `python -O`
        raise BatchFailure("la fabrique n'a produit aucun rapport : rien n'a été publié")
    return entree


def rejouer_depuis_audit(doc_dir: Path, audit_path: Path, *, settings: Settings,
                         artifact_uid: str | None = None, publier_artefact: bool = False,
                         output: Any = sys.stdout) -> RejeuTypage:
    """Point d'entrée : lit l'audit, recalcule, et ne publie que si on le lui demande."""
    audit = lire_audit(audit_path, artifact_uid=artifact_uid)
    with _exclusive_lock(doc_dir):
        doc, report, _raw_manifest, old_entry, migration = _load(doc_dir)
        if migration or old_entry.overlay_hash is not None:
            raise ValueError(
                "un overlay de typage manuel couvre ce document : le rejeu d'audit ne le rejoue "
                "pas et ne le retire pas; relancer le typage plutôt que le rejeu")
        avant_juridiques = sum(1 for block in doc.blocks if block.kind in LEGAL_KINDS)
        avant_confirmes = sum(1 for block in doc.blocks if block.kind in LEGAL_KINDS
                              and block.kind_source == "model_verified")
        rejeu = rejouer(doc, report, audit, settings)
        print(f"rejeu hors réseau de {audit.artifact_uid} : "
              f"{len(audit.appels[1])}+{len(audit.appels[2])}+{len(audit.appels[3])} appel(s) "
              f"archivé(s), {audit.lignes_illisibles} ligne(s) illisible(s); coût 0.0000 €",
              file=output)
        print(f"kinds juridiques confirmés : {avant_confirmes}/{avant_juridiques} avant, "
              f"{rejeu.blocs_juridiques_confirmes}/{rejeu.blocs_juridiques} après", file=output)
        for reason, total in sorted(_par_motif(rejeu.decisions).items()):
            print(f"  {reason}: {total}", file=output)
        if not publier_artefact:
            print("aucune publication (--publier absent) : aucun octet de data/ modifié",
                  file=output)
            return rejeu
        entree = publier(doc_dir, rejeu, old_entry)
        print(f"publié : document_hash={entree.document_hash}, statut={entree.status}",
              file=output)
    return rejeu


def _par_motif(decisions: tuple[T2TerminalDecision, ...]) -> dict[str, int]:
    totaux: dict[str, int] = {}
    for decision in decisions:
        totaux[decision.reason] = totaux.get(decision.reason, 0) + 1
    return totaux


def main(argv: list[str] | None = None, *, settings: Settings | None = None,
         output: Any = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("doc_id")
    parser.add_argument("--audit", type=Path, required=True,
                        help="audit JSONL contenant les couples requête/réponse du lot payé")
    parser.add_argument("--data", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--artifact-uid",
                        help="artefact à rejouer quand l'audit en porte plusieurs")
    parser.add_argument("--publier", action="store_true",
                        help="publier le document et le rapport recalculés (sinon : lecture seule)")
    args = parser.parse_args(argv)
    if len(args.doc_id) > DOC_ID_MAX or not DOC_ID_RE.fullmatch(args.doc_id):
        print(f"doc_id invalide (slug [a-z0-9-]+ de {DOC_ID_MAX} caractères maximum attendu): "
              f"{args.doc_id!r}", file=sys.stderr)
        return 2
    doc_dir = args.data / args.doc_id
    if args.publier:
        try:
            verifier_couverture_du_lot([doc_dir / "document.json", doc_dir / "report.json",
                                        args.data / "manifest.json"])
            exiger_espace_installe([doc_dir / "document.json", doc_dir / "report.json",
                                    args.data / "manifest.json"])
        except Exception as exc:  # noqa: BLE001 — une disposition absente n'est pas une trace Python
            print(f"refus : {exc}", file=sys.stderr)
            return 2
    try:
        rejouer_depuis_audit(doc_dir, args.audit, settings=settings or get_settings(),
                             artifact_uid=args.artifact_uid, publier_artefact=args.publier,
                             output=output)
    except BatchFailure as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        print(f"rejeu refusé, rien n'a été écrit: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
