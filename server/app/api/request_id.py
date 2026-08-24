"""AD-10 / AD-15 — Un identifiant par requête, une ligne de log JSON, jamais un mot de la question.

`request_id` = uuid4 généré ici, posé sur `request.state`, rendu en `X-Request-Id`, repris tel quel
par la route dans la `Trace` et par l'enveloppe d'erreur : **une seule valeur, trois lectures**.

La ligne de log est émise à la fin de chaque requête. Elle ne porte que des champs déclarés
(`CHAMPS_DE_LOG`) : `intent`, `found`, `verdict`, `reason_kind`, `variants_count`, `blocks_scanned`,
`cost_eur` — la liste d'AD-10, mot pour mot — plus le code d'erreur quand il y en a un. Le filtre
n'est pas de la prudence décorative : AD-10 interdit `terms_searched` (« ils peuvent révéler santé,
famille, argent, sinistre ») et le texte, et une route future qui les poserait par erreur dans
`request.state.log_fields` ne pourrait pas les faire sortir par ici. Le middleware, lui, ne lit
**aucun** corps de requête.

`X-Cloud-Trace-Context` est logué dans son propre champ (AD-10) : il relie la ligne aux traces de
Cloud Run sans se confondre avec notre identifiant. Sa longueur est bornée par le seuil
`cloud_trace_max_chars` de `config.py` (convention Seuils, revue Codex 1.6, M2).
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server.app.api.errors import reponse_interne
from server.app.config import Settings, get_settings

logger = logging.getLogger("foyer.request")
# La pile d'une erreur inattendue part sur un logger **séparé** : `logger` n'émet que du
# JSON d'une seule ligne (c'est ce que Cloud Run relit en champs), et un `logger.exception`
# y aurait mêlé un message nu suivi d'une pile multi-lignes, éclatée en autant d'entrées.
logger_erreur = logging.getLogger("foyer.error")

ENTETE = "X-Request-Id"
ENTETE_CLOUD_TRACE = "x-cloud-trace-context"

# AD-10, littéralement. `error_code` s'y ajoute : c'est notre propre `Enum`, jamais un texte reçu.
CHAMPS_DE_LOG = ("intent", "found", "verdict", "reason_kind", "variants_count", "blocks_scanned",
                 "cost_eur", "error_code")


def configurer_journal(niveau: int = logging.INFO) -> None:
    """Fait sortir la ligne JSON sur la sortie standard — sans quoi elle n'existerait pas.

    Uvicorn configure ses propres journaux et laisse la racine sans gestionnaire : un `INFO` émis par
    un logger applicatif est alors **silencieusement jeté** (seul le gestionnaire de dernier recours
    de la stdlib parle, à partir de `WARNING`). Mesuré à la première vérification live de la story :
    les requêtes étaient servies et pas une ligne d'AD-10 n'apparaissait. Le format est le message
    nu, pour que la ligne reste du JSON pur — c'est ce que Cloud Run sait relire en champs.

    `propagate` reste vrai : la racine n'a pas de gestionnaire en production (aucun doublon), et les
    tests ont besoin de la propagation pour capturer les lignes.
    """
    if logger.handlers:
        return
    gestionnaire = logging.StreamHandler(sys.stdout)
    gestionnaire.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(gestionnaire)
    logger.setLevel(niveau)


class RequestIdMiddleware:
    """Middleware ASGI : identifiant, en-tête, ligne de log, et le filet des erreurs inattendues.

    Écrit en ASGI pur — et non en `BaseHTTPMiddleware` — pour une raison précise : c'est ici, et
    nulle part ailleurs, qu'une exception non prévue peut encore être convertie en réponse portant
    `X-Request-Id` et donnant sa ligne de log. Le `ServerErrorMiddleware` de Starlette, qui reçoit
    les gestionnaires `Exception`, est **au-dessus** de tous les middlewares applicatifs : sa réponse
    ne traverserait pas celui-ci, et le 500 sortirait sans identifiant — donc insituable dans les
    logs, ce qu'AD-16 refuse (« 500 `internal` … avec `request_id` »).
    """

    def __init__(self, app: ASGIApp, settings: Settings | None = None) -> None:
        self.app = app
        # Le seuil est lu une fois, au montage : `_journaliser` tourne à chaque requête et n'a pas à
        # relire les réglages (convention Seuils — la valeur vit dans `config.py`, pas ici).
        self.cloud_trace_max = (settings or get_settings()).cloud_trace_max_chars

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        etat = scope.setdefault("state", {})
        etat["request_id"] = request_id
        etat["log_fields"] = {}
        t0 = time.monotonic()
        statut = 500
        demarre = False
        entete = (ENTETE.encode("latin-1"), request_id.encode("latin-1"))

        async def envoyer(message: Message) -> None:
            nonlocal statut, demarre
            if message["type"] == "http.response.start":
                statut = message["status"]
                demarre = True
                message["headers"] = [*message.get("headers", []), entete]
            await send(message)

        try:
            await self.app(scope, receive, envoyer)
        except Exception:  # noqa: BLE001 — le filet d'AD-16 : 500 générique, rien de technique publié
            # Le détail part dans le log serveur (avec sa pile), jamais dans la réponse.
            logger_erreur.exception("erreur inattendue (request_id=%s)", request_id)
            if demarre:
                # L'en-tête de réponse est déjà parti : on ne peut plus rien envelopper. Le statut
                # déjà émis reste celui du log, et l'exception remonte au filet de Starlette.
                raise
            reponse = reponse_interne(Request(scope, receive))
            statut = reponse.status_code
            await reponse(scope, receive, envoyer)
        finally:
            self._journaliser(scope, request_id=request_id, statut=statut,
                             ms=int((time.monotonic() - t0) * 1000),
                             cloud_trace_max=self.cloud_trace_max)

    @staticmethod
    def _journaliser(scope: Scope, *, request_id: str, statut: int, ms: int,
                     cloud_trace_max: int) -> None:
        champs: dict[str, Any] = {}
        for cle, valeur in (scope.get("state") or {}).get("log_fields", {}).items():
            if cle in CHAMPS_DE_LOG:  # AD-10 : la liste des champs logués est close
                champs[cle] = valeur
        entetes = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        ligne = {
            "request_id": request_id,
            "method": scope.get("method", ""),
            # Le chemin est une route de notre application, jamais du contenu utilisateur : la
            # question voyage dans le corps d'un POST, pas dans l'URL.
            "path": scope.get("path", ""),
            "status": statut,
            # Cloud Logging classe une entrée JSON sur son champ `severity` ; sans lui, un 500 et un
            # 200 arrivent au même niveau et rien ne se filtre dans la console.
            "severity": "ERROR" if statut >= 500 else "WARNING" if statut >= 400 else "INFO",
            "ms": ms,
            # Valeur **cliente** : bornée avant d'entrer dans le log. Cloud Run en pose une de
            # quelques dizaines d'octets ; n'importe qui peut en poster une de plusieurs kilos, et
            # ce serait alors le journal qu'on ferait grossir à sa place.
            "cloud_trace": (entetes.get(ENTETE_CLOUD_TRACE) or "")[:cloud_trace_max] or None,
            **champs,
        }
        logger.info(json.dumps(ligne, ensure_ascii=False, sort_keys=True))
