"""fetch_source : hash vérifié, écriture atomique, repli gs://, codes de sortie — sans réseau (httpx.MockTransport)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from server.ingest import fetch_source as f

PDF = b"%PDF-1.4 fake contract"
SHA = hashlib.sha256(PDF).hexdigest()
URL = "https://example.invalid/cg.pdf"
GS = f"https://storage.googleapis.com/{f.SOURCES_BUCKET}/doc-a.pdf"


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "doc-a"
    d.mkdir(parents=True)
    (d / "source.url").write_text(URL + "\n", "utf-8")
    (d / "source.sha256").write_text(SHA + "\n", "utf-8")
    (tmp_path / "data" / "lux-guide").mkdir()
    return tmp_path / "data"


def _client(routes: dict[str, httpx.Response | Exception], seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        r = routes.get(str(request.url), httpx.Response(404))
        if isinstance(r, Exception):
            raise r
        return r
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_nominal_writes_verified_pdf(data: Path) -> None:
    seen: list[httpx.Request] = []
    out = f.fetch("doc-a", data, client=_client({URL: httpx.Response(200, content=PDF)}, seen))
    assert out == data / "doc-a" / "source.pdf" and out.read_bytes() == PDF
    assert seen[0].headers["user-agent"].startswith("Mozilla/5.0")
    assert not list(data.glob("doc-a/*.tmp"))


def test_hash_mismatch_writes_nothing_and_no_fallback(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    client = _client({URL: httpx.Response(200, content=b"other"), GS: httpx.Response(200, content=PDF)}, seen)
    with pytest.raises(f.FetchError) as exc:
        f.fetch("doc-a", data, client=client)
    assert exc.value.code == 2 and "hash attendu" in str(exc.value) and "obtenu" in str(exc.value)
    assert not (data / "doc-a" / "source.pdf").exists() and [str(r.url) for r in seen] == [URL]


def test_unreachable_falls_back_to_bucket_with_token(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "tok")
    seen: list[httpx.Request] = []
    client = _client({URL: httpx.ConnectError("dns"), GS: httpx.Response(200, content=PDF)}, seen)
    assert f.fetch("doc-a", data, client=client).read_bytes() == PDF
    assert [str(r.url) for r in seen] == [URL, GS] and seen[1].headers["authorization"] == "Bearer tok"


def test_http_error_falls_back_anonymously_when_no_metadata(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
    seen: list[httpx.Request] = []
    client = _client({URL: httpx.Response(403), f.METADATA_TOKEN_URL: httpx.ConnectError("no metadata"),
                      GS: httpx.Response(200, content=PDF)}, seen)
    assert f.fetch("doc-a", data, client=client).read_bytes() == PDF
    assert "authorization" not in seen[-1].headers


def test_fallback_failure_is_exit_3(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "tok")
    client = _client({URL: httpx.Response(500), GS: httpx.Response(403)}, [])
    with pytest.raises(f.FetchError) as exc:
        f.fetch("doc-a", data, client=client)
    assert exc.value.code == 3 and not (data / "doc-a" / "source.pdf").exists()


def test_fallback_hash_mismatch_is_exit_2(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "tok")
    client = _client({URL: httpx.Response(500), GS: httpx.Response(200, content=b"wrong")}, [])
    with pytest.raises(f.FetchError) as exc:
        f.fetch("doc-a", data, client=client)
    assert exc.value.code == 2


def test_reference_files_validated(data: Path) -> None:
    (data / "doc-a" / "source.sha256").write_text("abc\n", "utf-8")
    with pytest.raises(f.FetchError) as exc:
        f.read_reference(data / "doc-a")
    assert exc.value.code == 4
    (data / "doc-a" / "source.sha256").unlink()
    with pytest.raises(f.FetchError):
        f.read_reference(data / "doc-a")


def test_documents_to_fetch_lists_committed_sources(data: Path) -> None:
    assert f.documents_to_fetch(data) == (["doc-a"], ["lux-guide"])


def test_main_codes(data: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(f, "fetch", lambda doc_id, d: (_ for _ in ()).throw(f.FetchError(2, f"{doc_id} : hash")))
    assert f.main(["--all", "--data", str(data)]) == 2
    out, err = capsys.readouterr()
    assert "lux-guide : source committée" in out and "doc-a : hash" in err
    monkeypatch.setattr(f, "fetch", lambda doc_id, d: data / doc_id / "source.pdf")
    assert f.main(["doc-a", "--data", str(data)]) == 0
    with pytest.raises(SystemExit):
        f.main(["--data", str(data)])


def test_real_reference_matches_spec() -> None:
    real = Path(__file__).resolve().parents[1] / "data" / "axa-lu-optihome-2017"
    url, sha = f.read_reference(real)
    assert sha == "6824f9d2bbcb573b0b7c3816ea8a6e5f035b199bd885cf5b777e0978faa4af2c"
    assert url.startswith("https://luxembourg-axa.cdn.axa-contento-118412.eu/")
