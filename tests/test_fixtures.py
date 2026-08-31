from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from tests.fixtures import FixtureMissing, LLMRecorder, fixture_name, request_key


class Reply(BaseModel):
    texte: str
    n: int


class Other(BaseModel):
    texte: str
    n: int


def _entry(key: str, model: str = "tests.test_fixtures.Reply") -> dict:
    return {key: {"request": key, "response": {"__model__": model, "data": {"texte": "rejoué", "n": 1}}}}


async def test_replay_without_key_and_without_network(tmp_path: Path) -> None:
    key = request_key("claude-x", [{"role": "user", "content": "q"}])
    (tmp_path / "t.json").write_text(json.dumps(_entry(key)))
    rec = LLMRecorder("t", fixtures_dir=tmp_path, api_key="")

    async def boom() -> Reply:
        raise AssertionError("réseau appelé en mode replay")

    assert await rec.call(key, boom, model=Reply) == Reply(texte="rejoué", n=1)
    assert rec.calls_made == 0


async def test_missing_fixture_without_key_is_explicit(tmp_path: Path) -> None:
    rec = LLMRecorder("absent", fixtures_dir=tmp_path, api_key="")

    async def never() -> str:
        return "x"

    with pytest.raises(FixtureMissing, match="absent"):
        await rec.call("claude-x:abc", never)


def test_corrupt_fixture_is_explicit(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(FixtureMissing, match="corrompue"):
        LLMRecorder("bad", fixtures_dir=tmp_path, api_key="")
    (tmp_path / "list.json").write_text("[]")
    with pytest.raises(FixtureMissing, match="objet JSON"):
        LLMRecorder("list", fixtures_dir=tmp_path, api_key="")


async def test_model_mismatch_is_explicit(tmp_path: Path) -> None:
    key = "claude-x:k"
    (tmp_path / "m.json").write_text(json.dumps(_entry(key)))
    rec = LLMRecorder("m", fixtures_dir=tmp_path, api_key="")

    async def never() -> Reply:
        raise AssertionError

    with pytest.raises(FixtureMissing, match="Other"):
        await rec.call(key, never, model=Other)


async def test_record_with_key_writes_fixture_without_secret(tmp_path: Path) -> None:
    secret = "sk-ant-secret-value"
    rec = LLMRecorder("rec", fixtures_dir=tmp_path, api_key=secret)
    key = request_key("claude-x", [{"role": "user", "content": Reply(texte="q", n=0)}], temperature=0)

    async def real() -> Reply:
        return Reply(texte="réel", n=2)

    out = await rec.call(key, real, model=Reply)
    assert out.n == 2 and rec.calls_made == 1
    raw = (tmp_path / "rec.json").read_text("utf-8")
    assert secret not in raw
    replay = LLMRecorder("rec", fixtures_dir=tmp_path, api_key="")
    assert await replay.call(key, real, model=Reply) == out


async def test_record_plain_values_with_default_serializer(tmp_path: Path) -> None:
    rec = LLMRecorder("plain", fixtures_dir=tmp_path, api_key="k")

    async def real() -> dict:
        return {"reply": Reply(texte="x", n=1), "path": Path("/a")}

    await rec.call("k", real)
    assert json.loads((tmp_path / "plain.json").read_text())["k"]["response"] == {"reply": {"texte": "x", "n": 1}, "path": "/a"}


async def test_replay_accepte_un_alias_explicitement_audite(tmp_path: Path) -> None:
    (tmp_path / "alias.json").write_text(json.dumps({
        "__aliases__": {"requete-regeneree": "requete-source"},
        "requete-source": {"request": "requete-source", "response": {"ok": True}},
    }), encoding="utf-8")
    replay = LLMRecorder("alias", fixtures_dir=tmp_path, api_key="")

    async def fail() -> dict:
        raise AssertionError("aucun appel fournisseur en replay")

    assert await replay.call("requete-regeneree", fail) == {"ok": True}


def test_request_key_hides_message_text() -> None:
    k = request_key("claude-x", [{"role": "user", "content": "texte confidentiel"}])
    assert "confidentiel" not in k and k.startswith("claude-x:")


def test_recording_mode_follows_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from server.app import config

    monkeypatch.setattr(config.Settings, "model_config", {**config.Settings.model_config, "env_file": None})
    try:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        config.get_settings.cache_clear()
        assert LLMRecorder("t", fixtures_dir=tmp_path).recording is True
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        config.get_settings.cache_clear()
        assert LLMRecorder("t", fixtures_dir=tmp_path).recording is False
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        config.get_settings.cache_clear()
        assert LLMRecorder("t", fixtures_dir=tmp_path).recording is False
    finally:
        config.get_settings.cache_clear()


def test_recorder_reads_key_from_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from server.app import config

    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-from-dotenv\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config.Settings, "model_config", {**config.Settings.model_config, "env_file": env})
    config.get_settings.cache_clear()
    try:
        rec = LLMRecorder("t", fixtures_dir=tmp_path)
        assert rec.recording is True and rec.api_key == "sk-from-dotenv"
    finally:
        config.get_settings.cache_clear()


def test_empty_env_var_forces_replay_even_with_a_dotenv_key(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:
    # revue Codex 1.3 B2 : `ANTHROPIC_API_KEY= uv run pytest -q` doit être hermétique même quand `.env`
    # porte une clé — la variable d'environnement explicite (même vide) prime, sans repli vers
    # `get_settings()` (qui ignore les variables vides via `env_ignore_empty`).
    from server.app import config

    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-from-dotenv\n")
    monkeypatch.setattr(config.Settings, "model_config", {**config.Settings.model_config, "env_file": env})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    config.get_settings.cache_clear()
    try:
        rec = LLMRecorder("t", fixtures_dir=tmp_path)
        assert rec.recording is False and rec.api_key == ""
        assert config.get_settings().anthropic_api_key == "sk-from-dotenv"  # la fuite que B2 dénonçait
    finally:
        config.get_settings.cache_clear()


def test_fixture_name_is_module_dot_test_and_safe() -> None:
    assert fixture_name("tests/test_fixtures.py::test_x") == "test_fixtures.test_x"
    name = fixture_name("tests/sub/test_a.py::TestK::test_y[a/b]")
    assert "/" not in name and name.startswith("test_a.TestK.test_y")


def test_recorder_fixture_uses_module_and_test_name(llm_recorder: LLMRecorder) -> None:
    assert llm_recorder.path.name == "test_fixtures.test_recorder_fixture_uses_module_and_test_name.json"
