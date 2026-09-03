from __future__ import annotations

import json
import logging
import multiprocessing
import stat
from pathlib import Path

import pytest
from pydantic import BaseModel

from server.app.config import Settings
from server.app.domain.trace import StepTrace
from server.app.domain.errors import LlmParse
from server.app.llm.audit import ExactLlmAuditEvent, JsonlAuditSink, MemoryAuditSink
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient, MemoryResponseCache
from server.app.llm.models import TIERS
from tests.llm_fake import FakeAnthropic, fake_message


class _Answer(BaseModel):
    mot: str


def _event(index: int) -> ExactLlmAuditEvent:
    return ExactLlmAuditEvent(
        call_uid=f"call:{index}", run_uid=f"run:{index}", artifact_uid=f"artifact:{index}",
        step="test", tier="micro", model="fixture", trusted_line_uids=(f"line:{index}",),
        request={"index": index}, response={"ok": True},
    )


def _append_in_process(path: str, index: int) -> None:
    JsonlAuditSink(Path(path)).append(_event(index))


def test_jsonl_est_0600_complet_et_verrouille_entre_instances_et_processus(
        tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)
    context = multiprocessing.get_context("fork")
    processes = [context.Process(target=_append_in_process, args=(str(path), index))
                 for index in range(8)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0

    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    assert {row["call_uid"] for row in rows} == {f"call:{index}" for index in range(8)}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_audit_exact_hors_ligne_applique_une_rotation_et_une_retention_bornees(
        tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path, max_bytes=700, retention_files=2)

    for index in range(12):
        sink.append(_event(index))

    files = sorted(tmp_path.glob("audit.jsonl*"))
    assert [item.name for item in files] == ["audit.jsonl", "audit.jsonl.1", "audit.jsonl.lock"]
    assert path.stat().st_size <= 700
    assert (tmp_path / "audit.jsonl.1").stat().st_size <= 700
    assert all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in files)


async def test_cache_hit_recrit_un_evenement_exact_sans_fuite_dans_la_trace_publique(
        tmp_path: Path) -> None:
    path = tmp_path / "llm.jsonl"
    sink = JsonlAuditSink(path)
    cache = MemoryResponseCache()
    fake = FakeAnthropic([fake_message(
        model=TIERS["micro"], text='{"mot":"bonjour"}', input_tokens=20, output_tokens=5,
    )])
    client = LlmClient(
        Settings(_env_file=None, anthropic_api_key=""), anthropic_client=fake,
        cache=cache, audit_sink=sink,
    )

    async def call(document_uid: str, line_uid: str):
        budget = RequestBudget(deadline_s=100, max_attempts=2, max_cost_eur=1)
        budget.bind_artifact(
            document_uid=document_uid, source_hash=f"source:{document_uid}",
            ingest_fingerprint=f"ingest:{document_uid}",
        )
        return await client.parse(
            tier="micro", system_prefix="préfixe contrôlé",
            messages=[{"role": "user", "content": "secret qui reste dans l'audit 0600"}],
            output_model=_Answer, budget=budget, step=StepTrace(name="eval"),
            trusted_line_uids=(line_uid,),
        )

    first = await call("document-a", "line:a")
    second = await call("document-b", "line:b")
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    assert len(fake.requests) == 1 and len(rows) == 2 and rows[1]["cache_hit"] is True
    assert rows[0]["call_uid"] != rows[1]["call_uid"]
    assert rows[0]["run_uid"] != rows[1]["run_uid"]
    assert rows[0]["artifact_uid"] != rows[1]["artifact_uid"]
    assert rows[0]["trusted_line_uids"] == ["line:a"]
    assert rows[1]["trusted_line_uids"] == ["line:b"]
    assert first.call.audit_persisted and second.call.audit_persisted
    assert "secret" not in json.dumps(first.call.model_dump(mode="json"))

    memory_client = LlmClient(
        Settings(_env_file=None, anthropic_api_key=""),
        anthropic_client=FakeAnthropic([fake_message(model=TIERS["micro"])]),
        audit_sink=MemoryAuditSink(),
    )
    memory = await memory_client.parse(
        tier="micro", system_prefix="p", messages=[{"role": "user", "content": "q"}],
        output_model=_Answer,
        budget=RequestBudget(deadline_s=100, max_attempts=2, max_cost_eur=1),
        step=StepTrace(name="api"),
    )
    assert not memory.call.audit_persisted


async def test_client_en_ligne_ne_persiste_ni_question_historique_termes_ni_bloc(
        tmp_path: Path) -> None:
    path = tmp_path / "interdit.jsonl"
    settings = Settings(_env_file=None, anthropic_api_key="", llm_audit_path=path)
    fake = FakeAnthropic([fake_message(
        model=TIERS["micro"], text='{"mot":"bonjour"}', input_tokens=20, output_tokens=5,
    )])
    client = LlmClient(settings, anthropic_client=fake)
    secrets = {
        "question": "QUESTION_CONFIDENTIELLE_7f3d",
        "historique": "HISTORIQUE_CONFIDENTIEL_4a91",
        "termes": "TERMES_CONFIDENTIELS_18bc",
        "bloc": "BLOC_CONFIDENTIEL_d243",
    }

    result = await client.parse(
        tier="micro", system_prefix=secrets["bloc"],
        messages=[{"role": "user", "content": json.dumps(secrets)}],
        output_model=_Answer,
        budget=RequestBudget(deadline_s=100, max_attempts=1, max_cost_eur=1),
        step=StepTrace(name="api"),
    )

    public = result.call.model_dump_json()
    assert not path.exists()
    assert not result.call.audit_persisted
    assert not hasattr(client.audit_sink, "events")
    sink_state = json.dumps(vars(client.audit_sink), ensure_ascii=False, default=str)
    assert all(value not in sink_state for value in secrets.values())
    assert all(value not in public for value in secrets.values())


async def test_reponse_reellement_recue_mais_invalide_conserve_enveloppe_et_erreur(
        tmp_path: Path) -> None:
    path = tmp_path / "invalid.jsonl"
    fake = FakeAnthropic([fake_message(
        model=TIERS["micro"], text='{"champ_inattendu":true}',
        input_tokens=20, output_tokens=5,
    )])
    client = LlmClient(
        Settings(_env_file=None, anthropic_api_key=""),
        anthropic_client=fake, audit_sink=JsonlAuditSink(path),
    )
    with pytest.raises(LlmParse):
        await client.parse(
            tier="micro", system_prefix="p", messages=[{"role": "user", "content": "q"}],
            output_model=_Answer,
            budget=RequestBudget(deadline_s=100, max_attempts=1, max_cost_eur=1),
            step=StepTrace(name="api"),
        )
    row = json.loads(path.read_text("utf-8"))
    assert row["response"] is not None
    assert row["error_class"] == "LlmParse"


class _SinkQuiLeve:
    """Sink de persistance qui échoue à l'écriture — la classe levée est le sujet du témoin."""

    persistent = True

    def __init__(self, erreur: Exception) -> None:
        self._erreur = erreur
        self.appels = 0

    def append(self, event: ExactLlmAuditEvent) -> dict[str, object]:
        self.appels += 1
        raise self._erreur


async def test_un_disque_plein_perd_l_artefact_d_audit_jamais_la_reponse_deja_payee(
        caplog: pytest.LogCaptureFixture) -> None:
    """AD-10 : l'audit exact est un artefact d'exploitation, pas une dépendance de la réponse.

    Signature reproduite de l'incident du 02/09/2026 : l'appel modèle a abouti, il est **déjà
    facturé**, et l'écriture de `llm-calls.jsonl` lève `ENOSPC`. Avant le correctif, l'`OSError`
    sortait nu de `tool_turn` et devenait un 500 — l'appel payé n'entrant même pas dans la trace.
    Ce que le témoin exige : la réponse est servie, `step.calls` est **complet** (empreintes et
    tailles viennent de la projection sûre, calculée sans E/S), `audit_persisted` dit `False` — la
    trace ne ment pas sur l'artefact manquant —, et la pile part au journal.
    """
    sink = _SinkQuiLeve(OSError(28, "No space left on device", "/tmp/.audit/llm-calls.jsonl"))
    client = LlmClient(
        Settings(_env_file=None, anthropic_api_key=""),
        anthropic_client=FakeAnthropic([fake_message(
            model=TIERS["micro"], stop_reason="end_turn", content=[], input_tokens=2500,
            output_tokens=147)]),
        audit_sink=sink,
    )
    budget = RequestBudget(deadline_s=100, max_attempts=4, max_cost_eur=1)
    step = StepTrace(name="retrouver")

    with caplog.at_level(logging.WARNING, logger="foyer.llm"):
        resultat = await client.tool_turn(
            tier="micro", system_prefix="préfixe stable", messages=[],
            tools=[{"name": "chercher", "input_schema": {"type": "object"}}],
            budget=budget, step=step, max_tokens=200)

    assert resultat.message.stop_reason == "end_turn"
    assert sink.appels == 1
    assert budget.cost_eur > 0  # l'appel a été facturé…
    (call,) = step.calls          # …et il est bien dans la trace, contrairement à l'incident
    assert call.audit_persisted is False
    assert call.usage.output == 147
    assert len(call.input_sha256) == 64 and len(call.response_sha256) == 64
    assert call.input_bytes > 0 and call.response_bytes > 0
    (ligne,) = [r for r in caplog.records if r.name == "foyer.llm"]
    assert "audit exact non persisté" in ligne.getMessage()
    assert ligne.exc_info is not None and ligne.exc_info[1].errno == 28


async def test_un_defaut_de_notre_code_dans_l_audit_reste_terminal() -> None:
    """Contre-sonde de mutation : la borne est `OSError`, jamais `Exception`.

    `audit.py` lève une `ValueError` quand un événement dépasse sa borne de taille — c'est un défaut
    de **notre** code, pas du disque, et rien ne justifie de servir la réponse en l'avalant. Élargir
    l'`except` de `_audit_call` à `Exception` rendrait le témoin précédent vert *et* celui-ci rouge :
    c'est exactement ce que cette assertion existe pour empêcher.
    """
    sink = _SinkQuiLeve(ValueError("événement d'audit > borne"))
    client = LlmClient(
        Settings(_env_file=None, anthropic_api_key=""),
        anthropic_client=FakeAnthropic([fake_message(model=TIERS["micro"])]),
        audit_sink=sink,
    )
    with pytest.raises(ValueError, match="borne"):
        await client.parse(
            tier="micro", system_prefix="p", messages=[{"role": "user", "content": "q"}],
            output_model=_Answer,
            budget=RequestBudget(deadline_s=100, max_attempts=1, max_cost_eur=1),
            step=StepTrace(name="api"))
