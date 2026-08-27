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
    assert seen[0].headers["accept"].startswith("application/pdf")
    assert seen[0].headers["accept-encoding"] == "identity"
    assert not list(data.glob("doc-a/*.tmp"))


def test_public_406_is_renegotiated_once_as_a_complete_range(data: Path) -> None:
    seen: list[httpx.Request] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        seen.append(request)
        calls += 1
        if calls == 1:
            return httpx.Response(406)
        return httpx.Response(206, content=PDF,
                              headers={"Content-Range": f"bytes 0-{len(PDF) - 1}/{len(PDF)}"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert f.fetch("doc-a", data, client=client).read_bytes() == PDF
    assert [str(request.url) for request in seen] == [URL, URL]
    assert "range" not in seen[0].headers
    assert seen[1].headers["range"] == "bytes=0-"
    assert seen[1].headers["referer"] == "https://example.invalid/"
    assert seen[1].headers["sec-fetch-dest"] == "document"


def test_public_406_twice_falls_back_once_to_authenticated_bucket(
    data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le WAF peut refuser les deux négociations ; le seul troisième chemin est le bucket privé."""
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "jeton-ci-court")
    seen: list[httpx.Request] = []
    public_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal public_calls
        seen.append(request)
        if str(request.url) == URL:
            public_calls += 1
            return httpx.Response(406)
        if str(request.url) == GS:
            return httpx.Response(200, content=PDF)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert f.fetch("doc-a", data, client=client).read_bytes() == PDF
    assert [str(request.url) for request in seen] == [URL, URL, GS]
    assert "authorization" not in seen[0].headers
    assert "authorization" not in seen[1].headers
    assert seen[2].headers["authorization"] == "Bearer jeton-ci-court"


def test_private_source_reads_only_authenticated_bucket(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "jeton-ci-court")
    seen: list[httpx.Request] = []
    client = _client({GS: httpx.Response(200, content=PDF)}, seen)
    assert f.fetch("doc-a", data, client=client, private_source=True).read_bytes() == PDF
    assert [str(request.url) for request in seen] == [GS]
    assert seen[0].headers["authorization"] == "Bearer jeton-ci-court"


@pytest.mark.parametrize("token,status", [(None, None), ("jeton-refuse", 403)])
def test_private_source_fails_closed_without_usable_token(
    data: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    status: int | None,
) -> None:
    if token is None:
        monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", token)
    seen: list[httpx.Request] = []
    routes = {} if status is None else {GS: httpx.Response(status)}
    with pytest.raises(f.FetchError) as exc:
        f.fetch("doc-a", data, client=_client(routes, seen), private_source=True)
    assert exc.value.code == f.EXIT_UNREACHABLE
    assert not (data / "doc-a" / "source.pdf").exists()
    if token is None:
        assert seen == []
    else:
        assert len(seen) == 1 and str(seen[0].url) == GS


def test_private_source_hash_mismatch_writes_nothing(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "jeton-ci-court")
    seen: list[httpx.Request] = []
    with pytest.raises(f.FetchError) as exc:
        f.fetch(
            "doc-a",
            data,
            client=_client({GS: httpx.Response(200, content=b"octets-alteres")}, seen),
            private_source=True,
        )
    assert exc.value.code == f.EXIT_HASH
    assert [str(request.url) for request in seen] == [GS]
    assert not (data / "doc-a" / "source.pdf").exists()


def test_private_fallback_without_identity_fails_closed(
    data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une PR sans jeton ne transforme pas l'absence d'identité ou d'objet en faux succès."""
    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
    seen: list[httpx.Request] = []
    client = _client(
        {
            URL: httpx.Response(406),
            f.METADATA_TOKEN_URL: httpx.ConnectError("pas de serveur de métadonnées"),
            GS: httpx.Response(403),
        },
        seen,
    )
    with pytest.raises(f.FetchError) as exc:
        f.fetch("doc-a", data, client=client)
    assert exc.value.code == f.EXIT_UNREACHABLE
    assert not (data / "doc-a" / "source.pdf").exists()
    assert str(seen[-1].url) == GS and "authorization" not in seen[-1].headers


def test_public_406_then_partial_bytes_fails_the_hash_without_bucket_fallback(data: Path) -> None:
    seen: list[httpx.Request] = []
    # Le MockTransport route les deux requêtes par URL : la seconde réponse est remplacée ici par
    # une plage valide au niveau HTTP mais incomplète au regard de l'empreinte source.
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        seen.append(request)
        calls += 1
        return (httpx.Response(406) if calls == 1
                else httpx.Response(206, content=PDF[:-1]))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(f.FetchError) as exc:
        f.fetch("doc-a", data, client=client)
    assert exc.value.code == f.EXIT_HASH
    assert [str(request.url) for request in seen] == [URL, URL]
    assert not (data / "doc-a" / "source.pdf").exists()


def test_hash_mismatch_writes_nothing_and_no_fallback(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    precedent = PDF  # son hash correspond bien à `source.sha256` : c'est la source valide existante
    (data / "doc-a" / "source.pdf").write_bytes(precedent)
    seen: list[httpx.Request] = []
    client = _client({URL: httpx.Response(200, content=b"other"), GS: httpx.Response(200, content=PDF)}, seen)
    with pytest.raises(f.FetchError) as exc:
        f.fetch("doc-a", data, client=client)
    assert exc.value.code == 2 and "hash attendu" in str(exc.value) and "obtenu" in str(exc.value)
    assert (data / "doc-a" / "source.pdf").read_bytes() == precedent
    assert [str(r.url) for r in seen] == [URL]
    assert not list((data / "doc-a").glob("*.tmp"))


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
    monkeypatch.setattr(
        f,
        "fetch",
        lambda doc_id, d, **_: (_ for _ in ()).throw(f.FetchError(2, f"{doc_id} : hash")),
    )
    assert f.main(["--all", "--data", str(data)]) == 2
    out, err = capsys.readouterr()
    assert "lux-guide : source committée" in out and "doc-a : hash" in err
    monkeypatch.setattr(f, "fetch", lambda doc_id, d, **_: data / doc_id / "source.pdf")
    assert f.main(["doc-a", "--data", str(data)]) == 0
    with pytest.raises(SystemExit):
        f.main(["--data", str(data)])


def test_main_forwards_private_source_mode(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path, bool]] = []

    def fake_fetch(doc_id: str, data_dir: Path, *, private_source: bool = False) -> Path:
        calls.append((doc_id, data_dir, private_source))
        return data_dir / doc_id / "source.pdf"

    monkeypatch.setattr(f, "fetch", fake_fetch)
    assert f.main(["--all", "--private-source", "--data", str(data)]) == 0
    assert calls == [("doc-a", data, True)]


def test_real_reference_matches_spec() -> None:
    data = Path(__file__).resolve().parents[1] / "data"
    url, sha = f.read_reference(data / "axa-lu-optihome-2017")
    assert sha == "6824f9d2bbcb573b0b7c3816ea8a6e5f035b199bd885cf5b777e0978faa4af2c"
    assert url.startswith("https://luxembourg-axa.cdn.axa-contento-118412.eu/")
    url, sha = f.read_reference(data / "baloise-lu-home-2-2024")
    assert url == (
        "https://www.baloise.lu/dam/baloise-lu/1890/particulier/documents/"
        "CG-CS/CG-HOME-2--LUFR-09-24.pdf"
    )
    assert sha == "2c365b0ea59a47ddf86295b0e1ad65a0c23847bcc30db22ec47861b18ba4a5a6"


def test_gs_url_as_main_source_is_downloaded_with_token(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 1.2 : `source.url` « peut aussi être une URL gs:// du bucket privé » (revue Codex 1.2, B5)."""
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "tok")
    (data / "doc-a" / "source.url").write_text("gs://mon-bucket/dossier/cg.pdf\n", "utf-8")
    assert f.read_reference(data / "doc-a")[0] == "gs://mon-bucket/dossier/cg.pdf"
    seen: list[httpx.Request] = []
    https = "https://storage.googleapis.com/mon-bucket/dossier/cg.pdf"
    client = _client({https: httpx.Response(200, content=PDF)}, seen)
    assert f.fetch("doc-a", data, client=client).read_bytes() == PDF
    assert [str(r.url) for r in seen] == [https] and seen[0].headers["authorization"] == "Bearer tok"
    # objet gs:// injoignable ⇒ repli sur le bucket des sources, puis exit 3 si lui aussi échoue
    client = _client({https: httpx.Response(404), GS: httpx.Response(200, content=PDF)}, seen := [])
    assert f.fetch("doc-a", data, client=client).read_bytes() == PDF and [str(r.url) for r in seen] == [https, GS]
    client = _client({https: httpx.Response(200, content=b"wrong")}, [])
    with pytest.raises(f.FetchError) as exc:
        f.fetch("doc-a", data, client=client)
    assert exc.value.code == 2


@pytest.mark.parametrize("url", ["gs://", "gs://bucket", "ftp://x/y.pdf", "http://x/y.pdf", "gs://B/x.pdf"])
def test_reference_url_scheme_is_validated(data: Path, url: str) -> None:
    (data / "doc-a" / "source.url").write_text(url + "\n", "utf-8")
    with pytest.raises(f.FetchError) as exc:
        f.read_reference(data / "doc-a")
    assert exc.value.code == 4


def test_metadata_timeout_comes_from_settings(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
    seen: list[httpx.Request] = []
    client = _client({f.METADATA_TOKEN_URL: httpx.Response(200, json={"access_token": "meta"})}, seen)
    assert f._metadata_token(client) == "meta"
    assert seen[0].extensions["timeout"]["read"] == f.get_settings().metadata_timeout_s
