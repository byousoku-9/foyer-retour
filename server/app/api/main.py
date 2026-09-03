"""AD-12 — Un seul service, une seule origine : l'API, la copie du site, l'accueil, l'outil sinistre.

`/` (accueil provisoire), `/guide/` (la copie du site, servie telle quelle), `/sinistre/` (page
« bientôt »), `/api/v1/*` et les alias historiques `/sante` et `/chat`. Tout sur la **même origine**,
donc **aucune configuration CORS** : il n'y a pas de seconde origine à autoriser. C'est le point
d'AD-12, et c'est aussi ce qui permet à `chat.js` de parler au serveur en changeant presque rien.

**L'ordre des montages compte.** Les routes sont enregistrées d'abord, puis `/guide` et `/sinistre`,
puis `/` en dernier : Starlette essaie dans l'ordre d'enregistrement, et un montage sur `/` attrape
tout ce qui reste. Les fichiers de `web/` sont servis **sans être renommés** (leurs chemins sont
relatifs : `app/styles.css`, `app/kb.js`).

**L'ordre des middlewares compte aussi.** `RequestIdMiddleware` est le plus extérieur : toute
réponse — y compris l'enveloppe d'une erreur et le 500 d'une exception non prévue — passe par lui,
donc porte `X-Request-Id` et donne sa ligne de log (AD-10). `LimiteDeCorps` vient juste en dessous :
il refuse un corps trop grand avant que quiconque ne le lise, mais après que l'identifiant existe.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from server.app.api.body_limit import LimiteDeCorps
from server.app.api.errors import gestionnaire_http, gestionnaire_pipeline, gestionnaire_validation
from server.app.api.etat import construire_etat
from server.app.api.request_id import RequestIdMiddleware, configurer_journal
from server.app.api.routes import chat as route_chat
from server.app.api.routes import documents as route_documents
from server.app.api.routes import evals as route_evals
from server.app.api.routes import sante as route_sante
from server.app.api.routes import sinistre as route_sinistre
from server.app.config import REPO_ROOT, Settings, get_settings
from server.app.domain.errors import PipelineError

logger = logging.getLogger("foyer.api")

API_V1 = "/api/v1"
WEB_DIR = REPO_ROOT / "web"
ACCUEIL_DIR = REPO_ROOT / "tools" / "accueil"
SINISTRE_DIR = REPO_ROOT / "tools" / "sinistre"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Charge **une fois** ce qui est constant : digests, corpus, index, client (AD-7, AD-9, reprise 1.6)."""
    settings: Settings = getattr(app.state, "settings", None) or get_settings()
    data_dir: Path | None = getattr(app.state, "data_dir", None)
    etat = construire_etat(settings, data_dir=data_dir)
    app.state.foyer = etat
    # Story 5.6 (T5) : le maintien au chaud des préfixes est une tâche de fond de **l'application**,
    # donc démarrée et arrêtée par le seul endroit qui connaît la vie du processus. Il n'existe que
    # sous `--min-instances=1` avec une clé fournisseur (`api/etat._caches`) ; partout ailleurs
    # l'attribut est `None` et ces deux lignes ne font rien.
    if etat.maintien_prefixes is not None:
        etat.maintien_prefixes.demarrer()
    try:
        yield
    finally:
        if etat.maintien_prefixes is not None:
            # Avant `aclose()` : la tâche parle au client, et un pool fermé sous elle rendrait une
            # erreur d'arrêt là où il n'y a qu'un arrêt.
            await etat.maintien_prefixes.fermer()
        # Le client Anthropic détient un pool de connexions HTTP. Cloud Run recycle ses instances et
        # une suite de tests ouvre l'application plusieurs fois : sans fermeture, les sockets restent
        # ouvertes jusqu'au ramasse-miettes, et le fait de ne rien fermer serait un état muté qu'on
        # ne voit qu'en production.
        await etat.client.aclose()


def _monter(app: FastAPI, chemin: str, dossier: Path, nom: str) -> None:
    """Monte un dossier statique s'il existe (AD-12). Un dossier absent n'empêche pas de servir l'API.

    Il le dit tout de même : sans cette ligne, une image où `COPY tools` ou `COPY web` manquerait
    rendrait des 404 nus sur `/`, `/guide/` et `/sinistre/` pendant que `/sante` continuerait de
    répondre `ok: true` — une panne muette que seuls les smoke tests de la story 1.11 verraient.
    """
    if dossier.is_dir():
        app.mount(chemin, StaticFiles(directory=dossier, html=True), name=nom)
    else:
        logger.warning("dossier statique absent, %s ne sera pas servi : %s", chemin, dossier)


def create_app(settings: Settings | None = None, *, data_dir: Path | None = None) -> FastAPI:
    configurer_journal()  # AD-10 : sans gestionnaire, la ligne JSON par requête n'est jamais émise
    app = FastAPI(title="foyer-retour", version="1", lifespan=lifespan)
    if settings is not None:
        app.state.settings = settings
    if data_dir is not None:
        # Injection de dossier réservée au boot/tests : les routes ne le lisent jamais et résolvent
        # toujours le document dans le corpus construit par le lifespan.
        app.state.data_dir = data_dir
    reglages = settings or get_settings()

    # AD-16 : une seule enveloppe, trois gestionnaires, aucun code inventé.
    app.add_exception_handler(RequestValidationError, gestionnaire_validation)
    app.add_exception_handler(PipelineError, gestionnaire_pipeline)
    app.add_exception_handler(StarletteHTTPException, gestionnaire_http)
    # Pas de gestionnaire pour `Exception` : Starlette le confierait à `ServerErrorMiddleware`, qui
    # est **au-dessus** de nos middlewares — sa réponse ne porterait ni `X-Request-Id` ni ligne de
    # log (AD-10). Le filet est dans `RequestIdMiddleware`, qui appelle `errors.reponse_interne()`.

    # Ajouté en premier ⇒ le plus intérieur des deux (Starlette empile à l'envers).
    app.add_middleware(LimiteDeCorps, max_bytes=reglages.request_max_bytes)
    app.add_middleware(RequestIdMiddleware, settings=reglages)

    for routeur in (route_chat.router, route_sante.router):
        app.include_router(routeur, prefix=API_V1)
        # AD-11 : `/chat` et `/sante` sont les chemins historiques attendus par `reponseApi()` et
        # `testerApi()`. Ce sont des alias, pas une seconde API : la même fonction, le même contrat.
        app.include_router(routeur, include_in_schema=False)

    # Story 1.9 — hors de la boucle, et volontairement : AD-11 ne nomme d'alias historique que pour
    # `/sante` et `/chat`, « attendus par `testerApi()`/`reponseApi()` » du site copié. Rien
    # n'attend `/sinistre` ni `/documents` à la racine, et un alias de plus serait une route
    # inventée par story — sans compter que `/sinistre` y entrerait en collision avec le montage
    # statique de la page du même nom.
    # Story 4.5 : `/api/v1/evals/latest` rejoint les routes sans alias historique, pour la même
    # raison qu'elles — AD-11 ne nomme d'alias racine que pour `/sante` et `/chat`, « attendus par
    # `testerApi()`/`reponseApi()` » du site copié. Rien n'attend `/evals` à la racine.
    for routeur in (route_documents.router, route_sinistre.router, route_evals.router):
        app.include_router(routeur, prefix=API_V1)

    # La page d'audit porte le ``doc_id`` dans une URL lisible et partage exactement la validation
    # du paramètre de la route JSON. Elle doit être enregistrée avant le montage statique
    # ``/sinistre`` : sinon StaticFiles traite ``ingestion/{doc_id}`` comme un chemin de fichier et
    # rend un 404 avant que l'application ne puisse servir l'hôte autonome.
    @app.get("/sinistre/ingestion/{doc_id}", include_in_schema=False,
             response_class=FileResponse)
    async def page_ingestion(doc_id: route_documents.DocId) -> FileResponse:
        del doc_id  # validé par FastAPI ; le script lit le segment depuis ``location.pathname``
        return FileResponse(SINISTRE_DIR / "ingestion.html", media_type="text/html")

    _monter(app, "/guide", WEB_DIR, "guide")
    _monter(app, "/sinistre", SINISTRE_DIR, "sinistre")
    _monter(app, "/", ACCUEIL_DIR, "accueil")  # en dernier : un montage sur `/` attrape le reste
    return app


app = create_app()
