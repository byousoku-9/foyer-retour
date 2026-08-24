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

# AD-7 : « `ALLOW_UNGATED` (dev / J+1 avant le premier gate) sert avec l'alerte `sans_gate` ».
# Aucun document n'a encore de gate (il s'écrit en story 1.10) et `allow_ungated` se dérive sinon de
# `ENV` : en `prod`, les deux documents partiraient en quarantaine et le conteneur servirait
# `documents_servis: []` — mesuré en exécutant le `CMD` ci-dessous hors conteneur. La ligne se retire
# à la fin de la story 1.10, quand le gate `vertical` existe ; `/sante` publie l'alerte en attendant.
ENV ENV=prod ALLOW_UNGATED=true PORT=8080
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
