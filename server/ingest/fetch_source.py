"""Téléchargement vérifié des sources non committées (AD-7) : `data/{doc_id}/source.url` + `source.sha256` → `source.pdf`.

    uv run python -m server.ingest.fetch_source axa-lu-optihome-2017
    uv run python -m server.ingest.fetch_source --all        # au build de l'image Docker
    uv run python -m server.ingest.fetch_source --all --private-source  # porte CI de main

Codes de sortie : 0 = téléchargé et vérifié ; 2 = hash différent du hash de référence (rien n'est écrit, aucun
repli : le contenu existe mais n'est pas celui attendu) ; 3 = URL publique injoignable **et** repli
`gs://foyer-retour-sources/{doc_id}.pdf` en échec ; 4 = usage (dossier, `source.url`/`source.sha256` absents).
`source.url` est une URL `https://` publique ou une URL `gs://bucket/objet` du bucket privé (AC 1.2) ; un objet
`gs://` est lu par HTTPS (`storage.googleapis.com`) avec le jeton `GOOGLE_OAUTH_ACCESS_TOKEN`
(local : `gcloud auth print-access-token`) ou celui du serveur de métadonnées (Cloud Build, Cloud Run),
sinon anonymement — le repli sur `gs://foyer-retour-sources/` passe par le même chemin.
`--private-source` ne tente aucune URL publique ni métadonnée : il exige le jeton explicite de la CI
et lit directement `gs://foyer-retour-sources/{doc_id}.pdf`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from server.app.config import get_settings
from server.app.corpus.racine import Lecture, lecture_de
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE
from server.app.domain.ingest import ManifestEntry

SOURCES_BUCKET = "foyer-retour-sources"
METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
PUBLIC_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GS_URL_RE = re.compile(r"^gs://([a-z0-9][a-z0-9._-]{1,221}[a-z0-9])/(.+)$")

EXIT_OK, EXIT_HASH, EXIT_UNREACHABLE, EXIT_USAGE, EXIT_CHANGED = 0, 2, 3, 4, 5


class FetchError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def gs_to_https(url: str) -> str:
    """`gs://bucket/objet` → `https://storage.googleapis.com/bucket/objet` (API XML, lecture avec jeton ou anonyme)."""
    m = GS_URL_RE.match(url)
    if not m:
        raise FetchError(EXIT_USAGE, f"URL gs://bucket/objet attendue : {url}")
    return f"https://storage.googleapis.com/{m.group(1)}/{m.group(2)}"


def read_reference(doc_dir: Path, *, lecture: Lecture | None = None) -> tuple[str, str]:
    """(`source.url`, sha256 attendu) ; un fichier absent ou un hash mal formé est une erreur d'usage."""
    url_path, sha_path = doc_dir / "source.url", doc_dir / "source.sha256"
    url_brute = lecture.texte(url_path) if lecture is not None else (
        url_path.read_text("utf-8") if url_path.is_file() else None)
    sha_brute = lecture.texte(sha_path) if lecture is not None else (
        sha_path.read_text("utf-8") if sha_path.is_file() else None)
    if url_brute is None or sha_brute is None:
        raise FetchError(EXIT_USAGE, f"{doc_dir} : source.url et source.sha256 requis")
    url = url_brute.strip()
    morceaux = sha_brute.split()
    expected = morceaux[0].lower() if morceaux else ""
    if not (url.startswith("https://") or GS_URL_RE.match(url)) or not _SHA256_RE.match(expected):
        raise FetchError(EXIT_USAGE, f"{doc_dir} : URL https:// ou gs://bucket/objet, et sha256 hexadécimal "
                                     "(64 caractères) attendus")
    return url, expected


def _identite_pincee(data_dir: Path, doc_id: str) -> tuple[str, str, ManifestEntry]:
    """URL, référence et identité canonique lues dans une unique génération validée."""
    doc_dir = data_dir / doc_id
    with lecture_de(data_dir) as lecture:
        url, expected = read_reference(doc_dir, lecture=lecture)
        manifeste = lecture.octets(data_dir / "manifest.json")
        try:
            brut = json.loads(manifeste) if manifeste is not None else None
            entree = ManifestEntry.model_validate(brut[doc_id])
        except (KeyError, TypeError, ValueError) as exc:
            raise FetchError(
                EXIT_USAGE, f"{doc_id} : identité canonique absente ou invalide dans le manifest") from exc
        if entree.source_hash != expected:
            raise FetchError(
                EXIT_USAGE,
                f"{doc_id} : source.sha256 ne correspond pas au source_hash canonique du manifest")
        lecture.verifier()
        return url, expected, entree


def _publier_si_identique(data_dir: Path, doc_id: str, content: bytes, *,
                          url_capturee: str, reference_capturee: str,
                          entree_capturee: ManifestEntry) -> None:
    """Compare sous le verrou puis publie PDF + manifest par l'unique commit."""
    from server.evals.espace import EspacePublie

    doc_dir = data_dir / doc_id
    manifest_path = data_dir / "manifest.json"
    espace = EspacePublie(data_dir.parent, data_dir)
    with espace.transaction() as transaction:
        url_courante = transaction.lire(doc_dir / "source.url")
        reference_courante = transaction.lire(doc_dir / "source.sha256")
        manifest_courant = transaction.lire(manifest_path)
        try:
            brut = json.loads(manifest_courant) if manifest_courant is not None else None
            entree_courante = ManifestEntry.model_validate(brut[doc_id])
        except (KeyError, TypeError, ValueError) as exc:
            raise FetchError(
                EXIT_CHANGED,
                f"{doc_id} : identité canonique absente ou invalide après téléchargement — rien "
                "n'est publié") from exc
        morceaux = reference_courante.split() if reference_courante is not None else []
        reference = morceaux[0].lower() if morceaux else ""
        if (url_courante is None or url_courante.strip() != url_capturee
                or reference != reference_capturee or entree_courante != entree_capturee):
            raise FetchError(
                EXIT_CHANGED,
                f"{doc_id} : URL, référence ou identité canonique modifiée pendant le "
                "téléchargement — rien n'est publié")
        transaction.publier([
            (doc_dir / "source.pdf", content),
            (manifest_path, manifest_courant),
        ])


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


def _required_access_token() -> str:
    """Jeton explicite de la porte CI privée ; aucun repli vers les métadonnées ou l'anonyme."""
    token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
    if not token:
        raise FetchError(EXIT_UNREACHABLE, "jeton GOOGLE_OAUTH_ACCESS_TOKEN requis pour la source privée")
    return token


def _download(client: httpx.Client, url: str, headers: dict[str, str], *, partial_ok: bool = False) -> bytes:
    r = client.get(url, headers=headers, follow_redirects=True)
    if r.status_code != 200 and not (partial_ok and r.status_code == 206):
        raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
    return r.content


def _download_public(client: httpx.Client, url: str) -> bytes:
    """Télécharge une URL publique, avec une seule renégociation bornée sur HTTP 406.

    Certains frontaux WAF/CDN refusent sur les IP de CI un client HTTP pourtant muni d'un
    User-Agent. Un 406 est précisément un refus de négociation : on rejoue alors la même URL comme
    navigation PDF du même site, avec `Range: bytes=0-`. Aucun autre statut n'est rejoué. Un 206 est
    admis uniquement sur cette voie; le SHA-256 de l'ensemble reste l'autorité plus bas, donc une
    plage tronquée ou une source changée échoue sans écriture et sans repli silencieux.
    """
    try:
        return _download(client, url, dict(PUBLIC_HEADERS))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 406:
            raise
    parsed = urlsplit(url)
    headers = dict(PUBLIC_HEADERS) | {
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
        "Range": "bytes=0-",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Upgrade-Insecure-Requests": "1",
    }
    return _download(client, url, headers, partial_ok=True)


def fetch(
    doc_id: str,
    data_dir: Path | str = "data",
    *,
    client: httpx.Client | None = None,
    private_source: bool = False,
) -> Path:
    """Écrit `data/{doc_id}/source.pdf` (atomique) après vérification du sha256 ; lève `FetchError(code)`."""
    if len(doc_id) > DOC_ID_MAX or not DOC_ID_RE.fullmatch(doc_id):
        raise FetchError(
            EXIT_USAGE,
            f"doc_id invalide (slug [a-z0-9-]+ de {DOC_ID_MAX} caractères maximum attendu) : {doc_id!r}",
        )
    data = Path(data_dir)
    doc_dir = data / doc_id
    # Premier geste après la validation syntaxique : préflight complet et capture d'une seule
    # génération, avant Settings, référence, client ou réseau.
    url, expected, entree_capturee = _identite_pincee(data, doc_id)
    url_capturee = url
    own = client is None
    client = client or httpx.Client(timeout=get_settings().fetch_timeout_s)
    try:
        if private_source:
            origin = gs_to_https(f"gs://{SOURCES_BUCKET}/{doc_id}.pdf")
            headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {_required_access_token()}"}
            try:
                content = _download(client, origin, headers)
            except httpx.HTTPError as exc:
                raise FetchError(
                    EXIT_UNREACHABLE,
                    f"{doc_id} : source privée en échec ({type(exc).__name__}: {exc})",
                ) from exc
        else:
            try:
                if url.startswith("gs://"):
                    url = gs_to_https(url)
                    headers = {"User-Agent": USER_AGENT}
                    token = _metadata_token(client)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                    content = _download(client, url, headers)
                else:
                    content = _download_public(client, url)
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
                    raise FetchError(
                        EXIT_UNREACHABLE,
                        f"{doc_id} : repli en échec ({type(exc2).__name__}: {exc2})",
                    ) from exc2
                origin = fallback
    finally:
        if own:
            client.close()
    got = hashlib.sha256(content).hexdigest()
    if got != expected:
        raise FetchError(EXIT_HASH, f"{doc_id} : hash attendu {expected}, obtenu {got} ({origin}) — rien n'est écrit")
    target = doc_dir / "source.pdf"
    _publier_si_identique(
        data, doc_id, content, url_capturee=url_capturee, reference_capturee=expected,
        entree_capturee=entree_capturee)
    print(f"{doc_id} : {target} ({len(content)} octets, sha256 {got}) depuis {origin}")
    return target


def documents_to_fetch(data_dir: Path) -> tuple[list[str], list[str]]:
    """(documents avec `source.sha256`, documents dont la source est committée) parmi `data/*/`."""
    to_fetch, committed = [], []
    for d in sorted(p for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        (to_fetch if (d / "source.sha256").is_file() else committed).append(d.name)
    return to_fetch, committed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("doc_id", nargs="?")
    parser.add_argument("--all", action="store_true", help="tous les documents de data/ qui ont un source.sha256")
    parser.add_argument(
        "--private-source",
        action="store_true",
        help=f"lire directement gs://{SOURCES_BUCKET}/ avec le jeton explicite de la CI",
    )
    parser.add_argument("--data", default="data", type=Path)
    args = parser.parse_args(argv)
    # `--all` ne liste même pas les références d'un arbre partiel : le préflight complet précède
    # tout parcours de `data/`, toute lecture et tout coût.
    try:
        with lecture_de(args.data):
            pass
    except Exception as exc:  # noqa: BLE001 — refus opérateur, sans trace Python
        print(f"refus avant lecture et réseau : {exc}", file=sys.stderr)
        return EXIT_USAGE
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
            fetch(doc_id, args.data, private_source=args.private_source)
        except FetchError as exc:
            print(str(exc), file=sys.stderr)
            return exc.code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
