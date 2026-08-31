from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from tests.fixtures import (FixtureMissing, LLMRecorder, fixture_name, request_certificate,
                            request_key)


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


async def test_record_refuse_une_reponse_inter_modele_avant_compteur_et_ecriture(
        tmp_path: Path) -> None:
    messages = [{"role": "user", "content": "q"}]
    certificate = request_certificate("claude-x", messages)
    key = request_key("claude-x", messages)
    rec = LLMRecorder("record-model-mismatch", fixtures_dir=tmp_path, api_key="k")

    async def real() -> dict:
        return {"model": "claude-y", "content": []}

    with pytest.raises(FixtureMissing, match="réponse enregistrée.*claude-y.*claude-x"):
        await rec.call(key, real, request=certificate)

    assert rec.calls_made == 0
    assert not rec.path.exists()


@pytest.mark.parametrize("fixture_preexistante", [False, True])
async def test_record_refuse_un_modele_expose_sans_certificat_avant_tout_effet(
        tmp_path: Path, fixture_preexistante: bool) -> None:
    path = tmp_path / "record-model-without-certificate.json"
    if fixture_preexistante:
        path.write_text(json.dumps(_entry("claude-x:existant")), encoding="utf-8")
    contenu_initial = path.read_bytes() if path.exists() else None
    rec = LLMRecorder("record-model-without-certificate", fixtures_dir=tmp_path, api_key="k")

    async def real() -> dict:
        return {"model": "claude-x", "content": []}

    with pytest.raises(FixtureMissing, match="certificat de requête invalide"):
        await rec.call("claude-x:nouveau", real)

    assert rec.calls_made == 0
    assert (path.read_bytes() if path.exists() else None) == contenu_initial


async def test_record_accepte_une_reponse_du_modele_certifie(tmp_path: Path) -> None:
    messages = [{"role": "user", "content": "q"}]
    certificate = request_certificate("claude-x", messages)
    key = request_key("claude-x", messages)
    rec = LLMRecorder("record-model-match", fixtures_dir=tmp_path, api_key="k")

    async def real() -> dict:
        return {"model": "claude-x", "content": []}

    result = await rec.call(key, real, request=certificate)

    assert result == {"model": "claude-x", "content": []}
    assert rec.calls_made == 1
    assert json.loads(rec.path.read_text("utf-8"))[key]["response"] == result


async def test_replay_accepte_un_alias_explicitement_audite(tmp_path: Path) -> None:
    messages = [{"role": "user", "content": "requête canonique"}]
    certificate = request_certificate(
        "claude-x", messages,
        tools=[{"name": "lire", "input_schema": {"type": "object"}}],
        output_config={"format": {"type": "json_schema", "schema": {"type": "object"}}},
    )
    source = request_key(
        "claude-x", messages,
        tools=[{"name": "lire", "input_schema": {"type": "object"}}],
        output_config={"format": {"type": "json_schema", "schema": {"type": "object"}}},
    )
    target = "claude-x:1111111111111111"
    (tmp_path / "alias.json").write_text(json.dumps({
        "__aliases__": {source: {
            "request": source, "target": target, "certificate": dict(certificate),
        }},
        target: {"request": target, "certificate": dict(certificate), "response": {"ok": True}},
    }), encoding="utf-8")
    replay = LLMRecorder("alias", fixtures_dir=tmp_path, api_key="")

    async def fail() -> dict:
        raise AssertionError("aucun appel fournisseur en replay")

    assert await replay.call(source, fail, request=certificate) == {"ok": True}


def _alias_valide(*, source_model: str = "claude-x", target_model: str | None = None) -> dict:
    target_model = target_model or source_model
    certificate = request_certificate(source_model, [{"role": "user", "content": "q"}])
    source = f"{source_model}:1111111111111111"
    target = f"{target_model}:2222222222222222"
    return {
        "__aliases__": {source: {
            "request": source, "target": target, "certificate": dict(certificate),
        }},
        target: {"request": target, "certificate": dict(certificate), "response": {"ok": True}},
    }


@pytest.mark.parametrize("version", [
    pytest.param(True, id="bool"),
    pytest.param(2.0, id="float"),
])
def test_certificat_refuse_une_version_non_int(tmp_path: Path, version: object) -> None:
    entries = _alias_valide()
    source = next(iter(entries["__aliases__"]))
    target = entries["__aliases__"][source]["target"]
    entries["__aliases__"][source]["certificate"]["version"] = version
    entries[target]["certificate"]["version"] = version
    (tmp_path / "certificate-version.json").write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(FixtureMissing, match="certificat de requête invalide"):
        LLMRecorder("certificate-version", fixtures_dir=tmp_path, api_key="")


@pytest.mark.parametrize("mutation,fragment", [
    ("cible_absente", "cible ordinaire absente"),
    ("request_source", "request incohérente"),
    ("request_cible", "request incohérente"),
    ("response_absente", "response absente"),
    ("certificat_cible", "certificats incompatibles"),
])
def test_alias_refuse_cible_ou_certificat_incoherent(
        tmp_path: Path, mutation: str, fragment: str) -> None:
    entries = _alias_valide()
    source = next(iter(entries["__aliases__"]))
    target = entries["__aliases__"][source]["target"]
    if mutation == "cible_absente":
        del entries[target]
    elif mutation == "request_source":
        entries["__aliases__"][source]["request"] = "claude-x:3333333333333333"
    elif mutation == "request_cible":
        entries[target]["request"] = "claude-x:3333333333333333"
    elif mutation == "response_absente":
        del entries[target]["response"]
    else:
        entries[target]["certificate"]["messages_sha256"] = "0" * 64
    (tmp_path / "alias-bad.json").write_text(json.dumps(entries), encoding="utf-8")
    with pytest.raises(FixtureMissing, match=fragment):
        LLMRecorder("alias-bad", fixtures_dir=tmp_path, api_key="")


@pytest.mark.parametrize("cycle", [False, True])
def test_alias_refuse_chaine_et_cycle(tmp_path: Path, cycle: bool) -> None:
    entries = _alias_valide()
    source = next(iter(entries["__aliases__"]))
    target = entries["__aliases__"][source]["target"]
    chained_target = source if cycle else "claude-x:3333333333333333"
    entries["__aliases__"][target] = {
        "request": target, "target": chained_target,
        "certificate": entries["__aliases__"][source]["certificate"],
    }
    del entries[target]
    (tmp_path / "alias-chain.json").write_text(json.dumps(entries), encoding="utf-8")
    with pytest.raises(FixtureMissing, match="cycle" if cycle else "chaîne"):
        LLMRecorder("alias-chain", fixtures_dir=tmp_path, api_key="")


def test_alias_refuse_cles_reservees(tmp_path: Path) -> None:
    entries = _alias_valide()
    entries["__reponse__"] = {"request": "__reponse__", "response": {"ok": True}}
    (tmp_path / "alias-reserved.json").write_text(json.dumps(entries), encoding="utf-8")
    with pytest.raises(FixtureMissing, match="clé réservée"):
        LLMRecorder("alias-reserved", fixtures_dir=tmp_path, api_key="")


def test_alias_inter_modele_est_refuse_meme_sans_modele_passe_a_call(tmp_path: Path) -> None:
    entries = _alias_valide(target_model="claude-y")
    (tmp_path / "alias-model.json").write_text(json.dumps(entries), encoding="utf-8")
    with pytest.raises(FixtureMissing, match="inter-modèle"):
        LLMRecorder("alias-model", fixtures_dir=tmp_path, api_key="")


async def test_alias_refuse_un_certificat_dappel_different(tmp_path: Path) -> None:
    entries = _alias_valide()
    source = next(iter(entries["__aliases__"]))
    (tmp_path / "alias-call.json").write_text(json.dumps(entries), encoding="utf-8")
    replay = LLMRecorder("alias-call", fixtures_dir=tmp_path, api_key="")

    async def never() -> dict:
        raise AssertionError("aucun appel fournisseur en replay")

    different = request_certificate("claude-x", [{"role": "user", "content": "autre"}])
    with pytest.raises(FixtureMissing, match="absent ou incohérent"):
        await replay.call(source, never, request=different)


def test_request_key_hides_message_text() -> None:
    k = request_key("claude-x", [{"role": "user", "content": "texte confidentiel"}])
    assert "confidentiel" not in k and k.startswith("claude-x:")


def test_certificat_distingue_le_prompt_systeme_des_messages() -> None:
    messages = [{"role": "user", "content": "question stable"}]
    premier = request_certificate("claude-x", messages, system="règle A")
    second = request_certificate("claude-x", messages, system="règle B")

    assert premier["messages_sha256"] == second["messages_sha256"]
    assert premier["system_sha256"] != second["system_sha256"]


async def test_replay_refuse_le_modele_expose_different_du_certificat_sans_modele_pydantic(
        tmp_path: Path) -> None:
    messages = [{"role": "user", "content": "q"}]
    certificate = request_certificate("claude-x", messages, system="règle")
    key = request_key("claude-x", messages, system="règle")
    (tmp_path / "response-model.json").write_text(json.dumps({
        key: {
            "request": key,
            "certificate": certificate,
            "response": {"model": "claude-y", "content": []},
        },
    }), encoding="utf-8")
    replay = LLMRecorder("response-model", fixtures_dir=tmp_path, api_key="")

    async def never() -> dict:
        raise AssertionError("aucun appel fournisseur en replay")

    with pytest.raises(FixtureMissing, match="réponse enregistrée.*claude-y.*claude-x"):
        await replay.call(key, never, request=certificate)


async def test_replay_refuse_un_modele_expose_sans_certificat_avant_retour(tmp_path: Path) -> None:
    messages = [{"role": "user", "content": "q"}]
    certificate = request_certificate("claude-x", messages)
    key = request_key("claude-x", messages)
    (tmp_path / "response-model-without-certificate.json").write_text(json.dumps({
        key: {
            "request": key,
            "certificate": certificate,
            "response": {"model": "claude-x", "content": []},
        },
    }), encoding="utf-8")
    replay = LLMRecorder("response-model-without-certificate", fixtures_dir=tmp_path, api_key="")

    async def never() -> dict:
        raise AssertionError("aucun appel fournisseur en replay")

    with pytest.raises(FixtureMissing, match="certificat de requête invalide"):
        await replay.call(key, never)

    assert replay.calls_made == 0


async def test_replay_accepte_le_modele_expose_egal_au_certificat(tmp_path: Path) -> None:
    messages = [{"role": "user", "content": "q"}]
    certificate = request_certificate("claude-x", messages)
    key = request_key("claude-x", messages)
    response = {"model": "claude-x", "content": []}
    (tmp_path / "response-model-match.json").write_text(json.dumps({
        key: {"request": key, "certificate": certificate, "response": response},
    }), encoding="utf-8")
    replay = LLMRecorder("response-model-match", fixtures_dir=tmp_path, api_key="")

    async def never() -> dict:
        raise AssertionError("aucun appel fournisseur en replay")

    assert await replay.call(key, never, request=certificate) == response


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
