from __future__ import annotations

import json
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
        budget = RequestBudget(deadline_s=30, max_attempts=2, max_cost_eur=1)
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
        budget=RequestBudget(deadline_s=30, max_attempts=2, max_cost_eur=1),
        step=StepTrace(name="api"),
    )
    assert not memory.call.audit_persisted


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
            budget=RequestBudget(deadline_s=30, max_attempts=1, max_cost_eur=1),
            step=StepTrace(name="api"),
        )
    row = json.loads(path.read_text("utf-8"))
    assert row["response"] is not None
    assert row["error_class"] == "LlmParse"
