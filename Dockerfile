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

# AD-11 : `/api/v1/sante` publie `version: sha7`. **Ce n'est pas ce `ARG` qui le porte en production**
# (story 1.11) : `gcloud run deploy --source`, qui construit l'image, n'accepte aucun `--build-arg`,
# et `deploy.yml` pose donc `GIT_SHA=<sha7>` en **variable d'environnement du service** — laquelle
# recouvre le `ENV` de l'image au démarrage du conteneur. Le chemin est même meilleur : il permet de
# re-marquer une révision sans reconstruire, et `env_vars_update_strategy: overwrite` en fait
# l'autorité. Le `ARG` reste pour qui construit l'image à la main (`docker build --build-arg …`) ;
# à défaut de l'un comme de l'autre, `config.py` retombe sur `dev`.
ARG GIT_SHA=dev
ENV GIT_SHA=$GIT_SHA

# AD-7 : `ALLOW_UNGATED` n'est **plus** armé (story 1.10). Les deux documents portent désormais un
# gate `vertical` écrit par `python -m server.evals.run --gate {doc_id}`, et l'AC de la story ferme la
# dérogation : en `prod`, `config.py` force `allow_ungated=False`, que la variable soit absente ou
# posée à `true`. Retirer cette ligne ne suffisait pas — la vraie surface est la configuration du
# service (`--set-env-vars ALLOW_UNGATED=true`), hors de portée de tout test hors ligne (revue Codex
# 1.10, B3). Le refus n'est pas muet : `/api/v1/sante` porte l'alerte `ungated_refuse_en_production`,
# la page d'accueil l'affiche, et un avertissement part au journal de démarrage. Un document dont le
# gate serait invalidé par une réingestion part donc en quarantaine `sans_gate` — AD-8.
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
