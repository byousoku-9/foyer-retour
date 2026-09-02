from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from server.app.domain import Document, Report
from server.app.domain.artifact import document_artifact_uid
from server.ingest import replay_typing_audit as rejeu
from server.ingest import type_clauses as tc
from tests.test_type_clauses import settings, write_data


def _ligne(plan: tc.RequestPlan, doc: Document, *, texte: str | None,
           error_class: str | None = None) -> dict[str, Any]:
    """Une ligne d'audit dans la forme exacte qu'`append_ingest_audit` écrit."""
    response = (None if texte is None else
                {"content": [{"type": "text", "text": texte}], "stop_reason": "end_turn",
                 "usage": {"input_tokens": 100, "output_tokens": 20}})
    return {
        "run_uid": f"typing:{plan.custom_id}",
        "step": plan.custom_id.split("-", 2)[1],
        "model": tc.MODEL,
        "artifact_uid": document_artifact_uid(
            document_uid=doc.doc_id, source_hash=doc.source_hash,
            ingest_fingerprint=doc.ingest_fingerprint),
        "request": plan.request["params"],
        "response": response,
        "error_class": error_class,
    }


def _rendu(reading: int, labels: list[dict[str, Any]]) -> str:
    """La lecture 1 rend un objet indexé par `block_id`; les lectures 2 et 3, une liste."""
    rendered: object = ({value["block_id"]: value for value in labels} if reading == 1
                        else labels)
    return json.dumps({"labels": rendered})


def _audit(tmp_path: Path, doc: Document, configured: Any,
           *, echec_arbitrage: bool = False) -> tuple[Path, list[str]]:
    """Un lot payé archivé : T1 riche, T2/T3 limitées aux champs de leur prompt.

    Le seul désaccord de la surface contractuelle porte sur le `kind` du quatrième bloc, que la
    troisième lecture tranche en faveur de la première.
    """
    ordre = [block.block_id for block in doc.blocks]
    t1 = {
        ordre[0]: {"kind": "garantie", "confidence": 0.9, "article_refs": ["2"],
                   "scope_articles": ["3.1"], "defines": None, "overrides_article": None,
                   "relations": []},
        ordre[1]: {"kind": "definition", "confidence": 0.9, "article_refs": [],
                   "scope_articles": [], "defines": "contenu", "overrides_article": None,
                   "relations": []},
        ordre[2]: {"kind": "condition", "confidence": 0.9, "article_refs": [],
                   "scope_articles": [], "defines": None, "overrides_article": "1",
                   "relations": [{"kind": "specialise", "article": "1"}]},
        ordre[3]: {"kind": "exclusion", "confidence": 0.9, "article_refs": [],
                   "scope_articles": [], "defines": None, "overrides_article": None,
                   "relations": []},
    }
    # Exactement ce que `prompts/type_clauses_2.md` demande, à une confiance qui n'est pas celle
    # de T1 — et un `kind` divergent sur le dernier bloc.
    t2 = {
        ordre[0]: {"kind": "garantie", "confidence": 0.85},
        ordre[1]: {"kind": "definition", "confidence": 0.87, "defines": "contenu"},
        ordre[2]: {"kind": "condition", "confidence": 0.84},
        ordre[3]: {"kind": "franchise", "confidence": 0.86},
    }
    t3 = {ordre[3]: {"kind": "exclusion", "confidence": 0.88}}

    lignes: list[dict[str, Any]] = []
    for reading, valeurs in ((1, t1), (2, t2), (3, t3)):
        blocs = [doc.block(block_id) for block_id in valeurs]
        for plan in tc.requests_for(doc, blocs, reading, configured):
            labels = [{"block_id": block_id, **valeurs[block_id]} for block_id in plan.block_ids]
            rate = reading == 3 and echec_arbitrage
            lignes.append(_ligne(plan, doc, texte=None if rate else _rendu(reading, labels),
                                 error_class="BatchFailure" if rate else None))

    audit_path = tmp_path / "audit" / "llm-calls.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("w", encoding="utf-8") as flux:
        # Une rotation coupe la première ligne en deux, et un appel d'une autre étape la précède.
        flux.write('{"run_uid":"typing:clauses-r1-0001-abc","step":"r1","reque\n')
        flux.write(json.dumps({"run_uid": "structure:doc", "step": "structure",
                               "artifact_uid": "autre", "request": {}, "response": None,
                               "error_class": None}) + "\n")
        for ligne in lignes:
            flux.write(json.dumps(ligne, ensure_ascii=False) + "\n")
    return audit_path, ordre


def test_le_rejeu_recalcule_les_decisions_terminales_hors_reseau_et_publie(tmp_path: Path) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=1)
    audit_path, ordre = _audit(tmp_path, doc, configured)
    journal = io.StringIO()

    resultat = rejeu.rejouer_depuis_audit(doc_dir, audit_path, settings=configured,
                                          publier_artefact=True, output=journal)

    # Aucun client n'est construit : la clé n'entre nulle part dans ce chemin.
    assert resultat.audit.lignes_illisibles == 1
    assert [decision.reason for decision in resultat.decisions] == ["KIND_MATCH"] * 4
    assert all(decision.state == "CONFIRMED" for decision in resultat.decisions)
    assert "coût 0.0000 €" in journal.getvalue()
    assert "kinds juridiques confirmés : 0/0 avant, 4/4 après" in journal.getvalue()

    publie = Document.model_validate_json((doc_dir / "document.json").read_bytes())
    assert [block.kind for block in publie.blocks] == [
        "garantie", "definition", "condition", "exclusion"]
    assert all(block.kind_source == "model_verified" for block in publie.blocks)
    # Les métadonnées publiées viennent de T1, jamais des lectures de confirmation.
    assert publie.block(ordre[0]).refs == [ordre[1]]
    rapport = Report.model_validate_json((doc_dir / "report.json").read_bytes())
    assert rapport.stats["blocs_juridiques_confirmes"] == 4
    assert len(rapport.stats["t2_terminal_decisions"]) == 4
    assert any(check.name == "typage_rejeu_audit" for check in rapport.checks)
    manifest = json.loads((doc_dir.parent / "manifest.json").read_text("utf-8"))
    assert manifest["contrat"]["document_hash"] == hashlib.sha256(
        (doc_dir / "document.json").read_bytes()).hexdigest()
    assert manifest["contrat"]["status"] == "servi" and manifest["contrat"]["gate"] is None


def test_un_arbitrage_absent_de_l_audit_reste_terminal_sans_effet(tmp_path: Path) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=1)
    audit_path, ordre = _audit(tmp_path, doc, configured, echec_arbitrage=True)

    resultat = rejeu.rejouer_depuis_audit(doc_dir, audit_path, settings=configured,
                                          output=io.StringIO())

    motifs = {decision.block_id: decision.reason for decision in resultat.decisions}
    assert motifs[ordre[3]] == "ARBITRATION_FAILED"
    assert [motifs[block_id] for block_id in ordre[:3]] == ["KIND_MATCH"] * 3
    perdue = next(d for d in resultat.decisions if d.block_id == ordre[3])
    assert perdue.state == "NON_CONFIRMED" and perdue.kind_t2 == "franchise"
    assert sum((perdue.retrieval_effect, perdue.citation_effect, perdue.applicability_effect,
                perdue.decision_effect, perdue.verdict_effect)) == 0


def test_sans_publier_le_rejeu_ne_modifie_aucun_octet(tmp_path: Path) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=1)
    audit_path, _ordre = _audit(tmp_path, doc, configured)
    cibles = [doc_dir / "document.json", doc_dir / "report.json", doc_dir.parent / "manifest.json"]
    avant = [cible.read_bytes() for cible in cibles]

    rejeu.rejouer_depuis_audit(doc_dir, audit_path, settings=configured, output=io.StringIO())

    assert [cible.read_bytes() for cible in cibles] == avant


def test_un_audit_qui_decrit_une_autre_ingestion_est_refuse_avant_toute_ecriture(
        tmp_path: Path) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=1)
    audit_path, _ordre = _audit(tmp_path, doc, configured)
    lignes = [json.loads(ligne) for ligne in audit_path.read_text("utf-8").splitlines()[2:]]
    for ligne in lignes:
        ligne["artifact_uid"] = "artifact-v1:0000"
    audit_path.write_text("".join(json.dumps(ligne) + "\n" for ligne in lignes), "utf-8")
    avant = (doc_dir / "document.json").read_bytes()

    with pytest.raises(ValueError, match="ne parlent pas de la même ingestion"):
        rejeu.rejouer_depuis_audit(doc_dir, audit_path, settings=configured, output=io.StringIO())
    assert (doc_dir / "document.json").read_bytes() == avant


def test_un_audit_multi_artefact_exige_de_nommer_celui_qu_on_rejoue(tmp_path: Path) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=1)
    audit_path, _ordre = _audit(tmp_path, doc, configured)
    attendu = document_artifact_uid(document_uid=doc.doc_id, source_hash=doc.source_hash,
                                    ingest_fingerprint=doc.ingest_fingerprint)
    lignes = audit_path.read_text("utf-8").splitlines()
    etranger = json.loads(lignes[-1])
    etranger["artifact_uid"] = "artifact-v1:0000"
    audit_path.write_text("\n".join([*lignes, json.dumps(etranger)]) + "\n", "utf-8")

    with pytest.raises(ValueError, match="préciser --artifact-uid"):
        rejeu.lire_audit(audit_path)
    assert rejeu.lire_audit(audit_path, artifact_uid=attendu).artifact_uid == attendu


def test_la_derniere_tentative_archivee_d_un_plan_est_celle_qui_compte(tmp_path: Path) -> None:
    doc_dir, doc = write_data(tmp_path)
    configured = settings(type_clauses_max_blocks_per_request=1)
    audit_path, ordre = _audit(tmp_path, doc, configured)
    lignes = audit_path.read_text("utf-8").splitlines()
    premiere = next(json.loads(ligne) for ligne in lignes[2:]
                    if json.loads(ligne)["step"] == "r2"
                    and ordre[0] in json.loads(ligne)["request"]["messages"][0]["content"])
    reprise = dict(premiere, response=None, error_class="BatchFailure")
    audit_path.write_text("\n".join([*lignes, json.dumps(reprise)]) + "\n", "utf-8")

    resultat = rejeu.rejouer_depuis_audit(doc_dir, audit_path, settings=configured,
                                          output=io.StringIO())

    motifs = {decision.block_id: decision.reason for decision in resultat.decisions}
    assert motifs[ordre[0]] == "FAILED"
