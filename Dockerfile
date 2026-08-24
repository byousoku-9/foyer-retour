# foyer-retour — image Cloud Run (AD-12 : un seul service, une seule origine).
# Le PDF du contrat n'est pas dans le repo : `fetch_source --all` le télécharge au build (hash vérifié, repli
# gs://foyer-retour-sources/ via le jeton du serveur de métadonnées) ; un hash différent fait échouer le build (AD-7).
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY server ./server
COPY data ./data
COPY web ./web
COPY tools ./tools

RUN uv run --no-sync python -m server.ingest.fetch_source --all

# AD-11 : `/api/v1/sante` publie `version: sha7`. Le build l'injecte (`--build-arg GIT_SHA=$(git rev-parse --short HEAD)`,
# posé par `deploy.yml` en story 1.11) ; à défaut, `config.py` retombe sur `dev`.
ARG GIT_SHA=dev
ENV GIT_SHA=$GIT_SHA

# AD-7 : `ALLOW_UNGATED` n'est **plus** armé (story 1.10). Les deux documents portent désormais un
# gate `vertical` écrit par `python -m server.evals.run --gate {doc_id}`, et `allow_ungated` se dérive
# de `ENV` : en `prod`, elle vaut `False`. La dérogation reste possible (AD-7 la nomme « dev / J+1 »)
# mais elle est devenue explicite **et bruyante** — la poser dans l'environnement du service produit
# l'alerte `ungated_en_production` sur `/api/v1/sante`, affichée par la page d'accueil, plus un
# avertissement au journal de démarrage. Ce que l'image ne fait plus, c'est l'armer en silence : un
# document dont le gate serait invalidé par une réingestion part en quarantaine `sans_gate`, ce qui
# est le comportement qu'AD-8 demande.
ENV ENV=prod PORT=8080
EXPOSE 8080
# `--proxy-headers --forwarded-allow-ips=*` fait qu'uvicorn croit `X-Forwarded-Proto` et
# `X-Forwarded-For` du proxy : il en tire `scope["scheme"]` (donc les redirections de `StaticFiles`
# — `/guide` → `/guide/` — restent en `https`) et `scope["client"]`. Ce n'est **pas** ce qui fait
# marcher le limiteur : `api/limiter.py` lit l'en-tête `X-Forwarded-For` directement, comme AD-13
# le demande, et le middleware d'uvicorn ne réécrit jamais les en-têtes. `*` est la valeur juste
# ici : le seul chemin d'entrée du conteneur est le front de Cloud Run.
# Le port vient de l'environnement : Cloud Run injecte `PORT` et n'impose pas 8080. Forme `sh -c` +
# `exec` pour que la substitution ait lieu tout en gardant uvicorn en PID 1 (signaux d'arrêt).
CMD ["sh", "-c", "exec uv run --no-sync uvicorn server.app.api.main:app \
     --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips=*"]
