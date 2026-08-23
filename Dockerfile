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

RUN uv run --no-sync python -m server.ingest.fetch_source --all

ENV ENV=prod PORT=8080
EXPOSE 8080
# L'application FastAPI arrive en story 1.6 ; le déploiement continu en 1.11.
CMD ["uv", "run", "--no-sync", "uvicorn", "server.app.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
