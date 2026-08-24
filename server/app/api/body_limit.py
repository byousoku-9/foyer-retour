"""AD-16 — `413 input_too_long` : le corps trop grand est refusé **avant** d'être lu en entier.

AD-16 nomme deux codes pour deux choses différentes, et la story les sépare explicitement :

- **400 `invalid_request`** pour toute violation des bornes du *contrat* (question > 1 000
  caractères, historique > 6 tours, variante inconnue) — le corps a été lu, il est bien formé, c'est
  son contenu qui sort des bornes ;
- **413 `input_too_long`** pour un corps HTTP dont la *taille* dépasse `request_max_bytes` — refusé
  sans être lu, sans quoi le serveur paierait la mémoire d'un corps qu'il allait rejeter, et `413`
  resterait un code mort de l'`Enum`.

Deux contrôles, parce qu'il y a deux façons d'envoyer un corps : `Content-Length` annoncé (rejet
immédiat, rien n'est lu) et transfert en morceaux sans longueur annoncée (les octets sont comptés à
mesure et la lecture s'arrête au premier dépassement).

Le middleware est placé **à l'intérieur** de `RequestIdMiddleware` : l'enveloppe qu'il rend porte
donc `X-Request-Id` et entre dans la ligne de log comme n'importe quelle autre réponse (AD-10).
"""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server.app.api.errors import envelope, request_id_de
from server.app.domain.errors import ErrorCode

# Un corps n'est attendu que là. Un GET géant n'existe pas, et un `Content-Length` sur une méthode
# sans corps n'a pas à provoquer un 413.
METHODES_AVEC_CORPS = frozenset({"POST", "PUT", "PATCH"})


def _message(taille: int, maximum: int) -> str:
    return f"corps de requête trop grand ({taille} octets) : la limite est {maximum}"


class LimiteDeCorps:
    """Middleware ASGI : `Content-Length` refusé d'emblée, flux sans longueur compté à la lecture."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() not in METHODES_AVEC_CORPS:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        annonce = request.headers.get("content-length")
        if annonce is not None and annonce.isdigit() and int(annonce) > self.max_bytes:
            # Le cas courant : la taille est annoncée, le corps n'est **jamais** lu.
            await self._refuser(scope, receive, send, int(annonce))
            return

        lus = 0

        async def compter() -> Message:
            nonlocal lus
            message = await receive()
            if message["type"] == "http.request":
                lus += len(message.get("body", b""))
                if lus > self.max_bytes:
                    # Transfert en morceaux sans longueur annoncée : la lecture s'arrête ici. Le
                    # signal est une `HTTPException` et non une exception à nous parce que FastAPI
                    # enveloppe **toute** autre erreur survenue pendant la lecture du corps en un
                    # 400 « error parsing the body » : notre 413 se serait perdu en 400. Une
                    # `HTTPException`, elle, est ré-émise telle quelle et rejoint `gestionnaire_http`,
                    # qui en fait l'enveloppe `input_too_long` d'AD-16.
                    raise HTTPException(status_code=413, detail=_message(lus, self.max_bytes))
            return message

        await self.app(scope, compter, send)

    async def _refuser(self, scope: Scope, receive: Receive, send: Send, taille: int) -> None:
        request = Request(scope, receive)
        champs = getattr(request.state, "log_fields", None)
        if champs is not None:
            champs["error_code"] = ErrorCode.input_too_long.value
        reponse = envelope(ErrorCode.input_too_long, _message(taille, self.max_bytes),
                           request_id_de(request))
        await reponse(scope, receive, send)
