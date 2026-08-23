# Tests live

Journal des vérifications faites contre le monde réel (GCP, API Anthropic, navigateur), une entrée par story.
Les tests unitaires (`uv run pytest`) n'ont jamais besoin du réseau ; ce qui en a besoin est consigné ici.

## Story 1.0 — Fondations (2026-08-23)

| Vérification | Commande | Résultat |
|---|---|---|
| Bootstrap GCP idempotent | `bash scripts/gcp_bootstrap.sh` ×6 (les dernières après les correctifs de revue : retry IAM, `PDF_LOCAL` obligatoire, secret = version ENABLED) | 1re exécution : création (une erreur `setIamPolicy` transitoire sur le SA tout juste créé) ; 2e et 3e : exit 0, chaque étape « déjà présent », sorties `WIF_PROVIDER`/`DEPLOY_SA` identiques |
| Secret `ANTHROPIC_API_KEY` | `gcloud secrets describe … --project=foyer-retour` | présent (v1), `secretAccessor` accordé à `foyer-retour-run@` |
| Bucket sources + PDF | `gcloud storage objects describe gs://foyer-retour-sources/axa-lu-optihome-2017.pdf` | déposé, sha256 vérifié avant envoi (`6824f9d2…af4c`) |
| Budget 50 % | `gcloud billing budgets list` | `foyer-retour-50` créé par le script (50 dans la devise du compte, alertes 50 % et 100 %) |
| Hello-world Cloud Run | `gcloud run deploy foyer-retour --source scripts/hello_world … --max-instances=1` puis `curl` | HTTP 200, `foyer-retour : hello-world`, révision `foyer-retour-00001`, SA runtime `foyer-retour-run@` — les rôles et APIs sont bons avant `deploy.yml` (1.11) |
| IDs de modèles | `uv run python -m server.app.llm.models --check` | `claude-opus-5` OK, `claude-sonnet-5` OK, `claude-haiku-4-5-20251001` OK (exit 0). L'alias `claude-haiku-4-5` du spine n'est pas dans `models.list()` : l'ID daté est retenu |
| Tests sans réseau | `ANTHROPIC_API_KEY= uv run pytest -q` | 88 passed (après revues Claude et Codex) |
| Grep assureurs (NFR11) | grep d'une liste privée de noms d'assureurs (hors repo) | zéro occurrence hors faux positifs (« foyer » = ménage, « luxembourgeoise » = adjectif) ; rien dans `web/comparateur/` |
| Plafond de dépense Anthropic | — | compte en crédit prépayé rechargé à la main par Lancelot (confirmé le 23/08/2026) — pas de plafond self-service à régler, le solde de crédit est la borne dure ; les plafonds côté code (0,10 €/requête, `--max-cost` par run d'évals) restent les garde-fous applicatifs |
| Variables GitHub | `gh variable list -R byousoku-9/foyer-retour` | `DEPLOY_SA`, `GCP_PROJECT_ID`, `WIF_PROVIDER` créées ; `gh api repos/byousoku-9/foyer-retour/actions/permissions` → `enabled: true` |
