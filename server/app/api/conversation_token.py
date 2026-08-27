"""Jeton de continuation signé de la story 3.7.

Le contenu est transporté par la page et reste lisible pour elle, mais toute altération est détectée
par HMAC avant validation métier. Le serveur ne conserve aucun état de session ni replay store ; en
production, les instances dérivent la même clé du secret fournisseur déjà partagé.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re

from pydantic import ValidationError

from server.app.domain.conversation import ContinuationState
from server.app.domain.errors import InvalidRequest

MAX_TOKEN_CHARS = 60_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(data: str) -> bytes:
    if not data or re.fullmatch(r"[A-Za-z0-9_-]+", data) is None:
        raise ValueError("base64url invalide")
    raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
    # Refuse aussi les encodages alternatifs qui décodent vers les mêmes octets (bits de bourrage
    # non nuls, alphabet ou padding différents) : un jeton n'a qu'une représentation canonique.
    if _b64(raw) != data:
        raise ValueError("base64url non canonique")
    return raw


def signer(state: ContinuationState, secret: bytes) -> str:
    payload = json.dumps(
        state.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    token = f"{_b64(payload)}.{_b64(signature)}"
    if len(token) > MAX_TOKEN_CHARS:
        raise InvalidRequest("état de continuation trop volumineux : recommencer un dossier")
    return token


def verifier(token: str, secret: bytes) -> ContinuationState:
    if not token or len(token) > MAX_TOKEN_CHARS or token.count(".") != 1:
        raise InvalidRequest("état de continuation invalide")
    encoded, signed = token.split(".", 1)
    try:
        payload = _unb64(encoded)
        signature = _unb64(signed)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise InvalidRequest("état de continuation invalide") from exc
    expected = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidRequest("état de continuation altéré ou signé par un autre serveur")
    try:
        raw = json.loads(payload)
        return ContinuationState.model_validate(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as exc:
        raise InvalidRequest("état de continuation illisible") from exc
