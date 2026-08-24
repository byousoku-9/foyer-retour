"""AD-16 — L'enveloppe d'erreur, fabriquée à un seul endroit.

« Chaque route inventant ses codes » est précisément ce qu'AD-16 empêche : les codes sont l'`Enum`
de `domain/errors.py`, les statuts sa table `HTTP_STATUS`, et l'enveloppe
`{error: {code, message, request_id}, trace?}` est construite ici, par `envelope()`. Aucune route
n'écrit de `JSONResponse` d'erreur, aucune ne choisit un statut.

Trois gestionnaires suffisent à couvrir tout ce qui peut sortir de l'application :

- `RequestValidationError` (pydantic, 422 par défaut de FastAPI) ⇒ **400 `invalid_request`** — AD-16
  le demande mot pour mot (« un handler `RequestValidationError` convertit les 422 Pydantic ») ;
- `PipelineError` ⇒ `HTTP_STATUS[code]`, avec la `Trace` partielle si l'erreur en porte une —
  sauf le message d'un `internal`, remplacé par `MESSAGE_INTERNE` (revue Codex 1.6, NB1) ;
- `HTTPException` ⇒ le code d'AD-16 qui correspond au statut, **quand il en existe un**. Un 404 ou
  un 405 n'ont pas de code dans l'`Enum` : leur inventer un serait exactement ce qu'AD-16 interdit,
  et un fichier statique manquant doit rester un 404 nu (« 404 statique nu »). Ces statuts-là sortent
  donc en texte brut. Un **5xx** sans code, lui, ressort en `500 internal` enveloppé (revue Codex
  1.6, I1) : voir `gestionnaire_http`.

L'erreur inattendue (`Exception` ⇒ 500 `internal`) est rendue par `reponse_interne()`, appelée par
le middleware de `request_id.py` : c'est le seul endroit qui soit **à l'intérieur** de notre
middleware, donc le seul dont la réponse porte `X-Request-Id` et entre dans la ligne de log (AD-10).
Le `ServerErrorMiddleware` de Starlette, lui, est au-dessus de tout et n'en saurait rien.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from server.app.domain.errors import HTTP_STATUS, ErrorBody, ErrorCode, ErrorEnvelope, PipelineError
from server.app.domain.trace import Trace

# Message rendu sur une erreur inattendue : générique, toujours le même. AD-16 veut un échec
# terminal explicite, pas une trace technique publiée — le détail part dans le log serveur.
MESSAGE_INTERNE = "erreur interne du serveur"

# La pile et le détail d'un 5xx partent dans le log serveur, jamais dans la réponse. Logger séparé de
# `foyer.request`, qui n'émet que du JSON d'une ligne (cf. `request_id.py`).
logger_http = logging.getLogger("foyer.error")

# Statut HTTP → code d'AD-16, pour les `HTTPException` que Starlette lève sans passer par nous.
# Seuls les statuts dont `HTTP_STATUS` donne **un seul** code y figurent. 503 en est absent
# volontairement : cinq codes d'AD-16 y mènent (`llm_unavailable`, `llm_parse`, `timeout`,
# `budget_exceeded`, `corpus_unavailable`) et en choisir un ferait passer pour une panne du
# fournisseur de modèle un 503 levé par n'importe quel composant — inventer un code, ce qu'AD-16
# interdit. Nos propres 503 passent par `PipelineError`, qui porte son code avec lui ; un 503 venu
# d'ailleurs est enveloppé en `internal` par `gestionnaire_http` (revue Codex 1.6, I1), et non rendu
# nu : l'enveloppe d'AD-16 n'a pas de trou côté 5xx.
_CODE_PAR_STATUT: dict[int, ErrorCode] = {
    400: ErrorCode.invalid_request,
    413: ErrorCode.input_too_long,
    429: ErrorCode.rate_limited,
    500: ErrorCode.internal,
}


def envelope(code: ErrorCode, message: str, request_id: str, *, trace: Trace | None = None,
             headers: dict[str, str] | None = None) -> JSONResponse:
    """L'unique enveloppe d'erreur (AD-16). Le statut vient de `HTTP_STATUS`, jamais de l'appelant."""
    corps = ErrorEnvelope(error=ErrorBody(code=code, message=message, request_id=request_id), trace=trace)
    return JSONResponse(status_code=HTTP_STATUS[code],
                        content=corps.model_dump(mode="json", exclude_none=True), headers=headers)


def request_id_de(request: Request) -> str:
    """Le `request_id` posé par le middleware (AD-10). Vide s'il n'a pas encore tourné."""
    return getattr(request.state, "request_id", "")


def _noter(request: Request, **champs: Any) -> None:
    """Complète la ligne de log de la requête (AD-10) : jamais de texte utilisateur ici."""
    champs_existants = getattr(request.state, "log_fields", None)
    if champs_existants is None:
        champs_existants = {}
        request.state.log_fields = champs_existants
    champs_existants.update(champs)


def reponse_interne(request: Request) -> JSONResponse:
    """500 `internal` : message générique, `request_id` présent, aucune trace technique publiée."""
    _noter(request, error_code=ErrorCode.internal.value)
    return envelope(ErrorCode.internal, MESSAGE_INTERNE, request_id_de(request))


async def gestionnaire_validation(request: Request, exc: RequestValidationError) -> Response:
    """422 pydantic ⇒ 400 `invalid_request` (AD-16), avec le **chemin** du champ fautif.

    Le message ne recopie jamais la valeur reçue : `question` peut faire 1 001 caractères de texte
    utilisateur, et l'enveloppe d'erreur n'est pas un endroit où le renvoyer (AD-15). Le chemin
    (`body.historique`) et le motif produit par le schéma suffisent à corriger l'appel.
    """
    details = []
    for e in exc.errors():
        chemin = ".".join(str(p) for p in e.get("loc", ()))
        details.append(f"{chemin}: {e.get('msg', 'invalide')}" if chemin else str(e.get("msg", "invalide")))
    message = "requête invalide — " + " ; ".join(details[:5]) if details else "requête invalide"
    _noter(request, error_code=ErrorCode.invalid_request.value)
    return envelope(ErrorCode.invalid_request, message, request_id_de(request))


async def gestionnaire_pipeline(request: Request, exc: PipelineError) -> Response:
    """`PipelineError` ⇒ son statut d'AD-16, et sa `Trace` partielle si le pipeline avait commencé.

    **Le message d'un `internal` n'est jamais publié (revue Codex 1.6, NB1, tour 3).** Les autres
    codes d'AD-16 décrivent une situation que l'appelant peut corriger ou attendre — leur message lui
    est dû (« historique de 8 tours », « document non servi »). `internal` ne décrit rien de tel : un
    invariant du code a cédé, et ce que l'étape a écrit dans son message est un diagnostic **interne**
    — un `block_id`, un nom de champ, l'erreur brute d'un fournisseur. Deux raisons de le taire, et
    non une : ce diagnostic peut recopier une chaîne **du modèle** (le `block_id` d'une citation que
    le corpus servi ne confirme pas est justement celui qu'aucun index ne connaît, donc rien ne dit
    qu'il vienne de notre ingestion — AD-15 : le contenu documentaire est du non-fiable, il ne
    ressort pas par l'enveloppe d'erreur) ; et un 500 rendait jusqu'ici un message différent selon
    son chemin — générique par `reponse_interne`, détaillé par ici — là où AD-16 veut **une** forme
    d'échec terminal. Le détail part dans `foyer.error`, avec le `request_id` qui relie les deux.
    """
    _noter(request, error_code=exc.code.value)
    message = exc.message
    if exc.code is ErrorCode.internal:
        logger_http.error("erreur interne (%s) : %s", request_id_de(request) or "sans request_id",
                          exc.message)
        message = MESSAGE_INTERNE
    if exc.trace is not None:
        # AD-10 : le coût déjà engagé par une requête qui échoue reste mesurable.
        _noter(request, intent=exc.trace.intent, cost_eur=exc.trace.total_cost_eur)
    entetes = None
    if exc.code is ErrorCode.rate_limited:
        # AD-13 : « 429 → enveloppe d'erreur `rate_limited` + `Retry-After` ». La valeur est calculée
        # par le limiteur, qui seul sait quand la fenêtre retombe ; il l'attache à l'erreur.
        retry_after = getattr(exc, "retry_after_s", None)
        if retry_after is not None:
            entetes = {"Retry-After": str(int(retry_after))}
    return envelope(exc.code, message, request_id_de(request), trace=exc.trace, headers=entetes)


async def gestionnaire_http(request: Request, exc: StarletteHTTPException) -> Response:
    """`HTTPException` ⇒ le code d'AD-16 correspondant ; un 4xx sans code sort nu, un 5xx en `internal`.

    Le 4xx nu, c'est « 404 statique nu » (matrice d'E/S) : un fichier absent sous `/guide/` n'est pas
    une erreur du pipeline, il n'a pas de code dans l'`Enum`, et lui en attribuer un
    (`invalid_request` ? `internal` ?) mentirait sur sa nature.

    Le 5xx, non (revue Codex 1.6, I1). AD-16 veut une **enveloppe unique** pour l'échec terminal, et
    un 5xx qui n'est pas passé par `PipelineError` est exactement ce que `internal` décrit : une
    panne du serveur qu'aucune étape n'a su nommer. Le laisser sortir en texte brut ouvrait un trou
    dans l'enveloppe — une route future aurait pu rendre un 503 hors contrat sans qu'aucun test ne le
    voie. Lui prêter `llm_unavailable` parce que c'est le 503 le plus fréquent aurait été pire : cinq
    codes d'AD-16 mènent à 503 (`HTTP_STATUS` n'est pas injectif), et en choisir un ferait passer
    pour une panne du fournisseur de modèle un 503 levé par n'importe quel composant. `internal` ne
    dit rien de faux : il dit que le serveur a échoué sans savoir dire pourquoi. Nos propres 503,
    eux, passent par `PipelineError`, qui porte son code avec lui — leur statut est intact.
    """
    code = _CODE_PAR_STATUT.get(exc.status_code)
    if code is None:
        if exc.status_code >= 500:
            logger_http.error("HTTPException %s hors des codes d'AD-16, rendue en 500 internal",
                              exc.status_code)
            return reponse_interne(request)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code, headers=exc.headers)
    _noter(request, error_code=code.value)
    return envelope(code, str(exc.detail), request_id_de(request), headers=exc.headers)
