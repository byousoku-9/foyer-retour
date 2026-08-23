from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from tests.fixtures import FixtureMissing, LLMRecorder, request_key


class Reply(BaseModel):
    texte: str
    n: int


async def test_replay_without_key_and_without_network(tmp_path: Path) -> None:
    fixture = tmp_path / "t.json"
    key = request_key("claude-x", [{"role": "user", "content": "q"}])
    fixture.write_text(json.dumps({key: {"request": key, "response": {
        "__model__": "tests.test_fixtures.Reply", "data": {"texte": "rejoué", "n": 1}}}}))
    rec = LLMRecorder("t", fixtures_dir=tmp_path, api_key="")

    async def boom() -> Reply:
        raise AssertionError("réseau appelé en mode replay")

    out = await rec.call(key, boom, model=Reply)
    assert out == Reply(texte="rejoué", n=1)
    assert rec.calls_made == 0


async def test_missing_fixture_without_key_is_explicit(tmp_path: Path) -> None:
    rec = LLMRecorder("absent", fixtures_dir=tmp_path, api_key="")

    async def never() -> str:
        return "x"

    with pytest.raises(FixtureMissing, match="absent"):
        await rec.call("claude-x:abc", never)


async def test_record_with_key_writes_fixture_without_secret(tmp_path: Path) -> None:
    secret = "sk-ant-secret-value"
    rec = LLMRecorder("rec", fixtures_dir=tmp_path, api_key=secret)
    key = request_key("claude-x", [{"role": "user", "content": "q"}], temperature=0)

    async def real() -> Reply:
        return Reply(texte="réel", n=2)

    out = await rec.call(key, real, model=Reply)
    assert out.n == 2 and rec.calls_made == 1
    raw = (tmp_path / "rec.json").read_text("utf-8")
    assert secret not in raw
    replay = LLMRecorder("rec", fixtures_dir=tmp_path, api_key="")
    assert await replay.call(key, real, model=Reply) == out


def test_request_key_hides_message_text() -> None:
    k = request_key("claude-x", [{"role": "user", "content": "texte confidentiel"}])
    assert "confidentiel" not in k and k.startswith("claude-x:")


def test_recorder_fixture_uses_test_name(llm_recorder: LLMRecorder) -> None:
    assert llm_recorder.path.name == "test_recorder_fixture_uses_test_name.json"
