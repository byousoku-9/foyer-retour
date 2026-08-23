"""Téléchargement vérifié des sources non committées (AD-7) : `data/{doc_id}/source.url` + `source.sha256` → `source.pdf`.

    uv run python -m server.ingest.fetch_source axa-lu-optihome-2017
    uv run python -m server.ingest.fetch_source --all        # au build de l'image Docker

Codes de sortie : 0 = téléchargé et vérifié ; 2 = hash différent du hash de référence (rien n'est écrit, aucun
repli : le contenu existe mais n'est pas celui attendu) ; 3 = URL publique injoignable **et** repli
`gs://foyer-retour-sources/{doc_id}.pdf` en échec ; 4 = usage (dossier, `source.url`/`source.sha256` absents).
`source.url` est une URL `https://` publique ou une URL `gs://bucket/objet` du bucket privé (AC 1.2) ; un objet
`gs://` est lu par HTTPS (`storage.googleapis.com`) avec le jeton `GOOGLE_OAUTH_ACCESS_TOKEN`
(local : `gcloud auth print-access-token`) ou celui du serveur de métadonnées (Cloud Build, Cloud Run),
sinon anonymement — le repli sur `gs://foyer-retour-sources/` passe par le même chemin.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import httpx

from server.app.config import get_settings

SOURCES_BUCKET = "foyer-retour-sources"
METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GS_RE = re.compile(r"^gs://([a-z0-9][a-z0-9._-]{1,221}[a-z0-9])/(.+)$")

EXIT_OK, EXIT_HASH, EXIT_UNREACHABLE, EXIT_USAGE = 0, 2, 3, 4


class FetchError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def gs_to_https(url: str) -> str:
    """`gs://bucket/objet` → `https://storage.googleapis.com/bucket/objet` (API XML, lecture avec jeton ou anonyme)."""
    m = _GS_RE.match(url)
    if not m:
        raise FetchError(EXIT_USAGE, f"URL gs://bucket/objet attendue : {url}")
    return f"https://storage.googleapis.com/{m.group(1)}/{m.group(2)}"


def read_reference(doc_dir: Path) -> tuple[str, str]:
    """(`source.url`, sha256 attendu) ; un fichier absent ou un hash mal formé est une erreur d'usage."""
    url_path, sha_path = doc_dir / "source.url", doc_dir / "source.sha256"
    if not url_path.is_file() or not sha_path.is_file():
        raise FetchError(EXIT_USAGE, f"{doc_dir} : source.url et source.sha256 requis")
    url = url_path.read_text("utf-8").strip()
    expected = sha_path.read_text("utf-8").split()[0].lower() if sha_path.read_text("utf-8").split() else ""
    if not (url.startswith("https://") or _GS_RE.match(url)) or not _SHA256_RE.match(expected):
        raise FetchError(EXIT_USAGE, f"{doc_dir} : URL https:// ou gs://bucket/objet, et sha256 hexadécimal "
                                     "(64 caractères) attendus")
    return url, expected


def _metadata_token(client: httpx.Client) -> str | None:
    token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
    if token:
        return token
    try:
        r = client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"},
                       timeout=get_settings().metadata_timeout_s)
        if r.status_code == 200:
            return str(r.json().get("access_token") or "") or None
    except (httpx.HTTPError, ValueError):
        pass
    return None


def _download(client: httpx.Client, url: str, headers: dict[str, str]) -> bytes:
    r = client.get(url, headers=headers, follow_redirects=True)
    if r.status_code != 200:
        raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
    return r.content


def fetch(doc_id: str, data_dir: Path | str = "data", *, client: httpx.Client | None = None) -> Path:
    """Écrit `data/{doc_id}/source.pdf` (atomique) après vérification du sha256 ; lève `FetchError(code)`."""
    doc_dir = Path(data_dir) / doc_id
    url, expected = read_reference(doc_dir)
    own = client is None
    client = client or httpx.Client(timeout=get_settings().fetch_timeout_s)
    try:
        try:
            headers = {"User-Agent": USER_AGENT}
            if url.startswith("gs://"):
                url = gs_to_https(url)
                token = _metadata_token(client)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            content = _download(client, url, headers)
            origin = url
        except httpx.HTTPError as exc:
            print(f"{doc_id} : source injoignable ({type(exc).__name__}: {exc}) ; repli gs://{SOURCES_BUCKET}",
                  file=sys.stderr)
            fallback = gs_to_https(f"gs://{SOURCES_BUCKET}/{doc_id}.pdf")
            headers = {"User-Agent": USER_AGENT}
            token = _metadata_token(client)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                content = _download(client, fallback, headers)
            except httpx.HTTPError as exc2:
                raise FetchError(EXIT_UNREACHABLE, f"{doc_id} : repli en échec ({type(exc2).__name__}: {exc2})") from exc2
            origin = fallback
    finally:
        if own:
            client.close()
    got = hashlib.sha256(content).hexdigest()
    if got != expected:
        raise FetchError(EXIT_HASH, f"{doc_id} : hash attendu {expected}, obtenu {got} ({origin}) — rien n'est écrit")
    target = doc_dir / "source.pdf"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(target)
    print(f"{doc_id} : {target} ({len(content)} octets, sha256 {got}) depuis {origin}")
    return target


def documents_to_fetch(data_dir: Path) -> tuple[list[str], list[str]]:
    """(documents avec `source.sha256`, documents dont la source est committée) parmi `data/*/`."""
    to_fetch, committed = [], []
    for d in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        (to_fetch if (d / "source.sha256").is_file() else committed).append(d.name)
    return to_fetch, committed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("doc_id", nargs="?")
    parser.add_argument("--all", action="store_true", help="tous les documents de data/ qui ont un source.sha256")
    parser.add_argument("--data", default="data", type=Path)
    args = parser.parse_args(argv)
    if bool(args.doc_id) == args.all:
        parser.error("un doc_id ou --all")
    if args.all:
        doc_ids, committed = documents_to_fetch(args.data)
        for doc_id in committed:
            print(f"{doc_id} : source committée, rien à télécharger")
    else:
        doc_ids = [args.doc_id]
    for doc_id in doc_ids:
        try:
            fetch(doc_id, args.data)
        except FetchError as exc:
            print(str(exc), file=sys.stderr)
            return exc.code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
