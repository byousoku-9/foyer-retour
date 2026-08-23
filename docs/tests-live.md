# Tests live

Journal des vérifications faites contre le monde réel (GCP, API Anthropic, navigateur), une entrée par story.
Les tests unitaires (`uv run pytest`) n'ont jamais besoin du réseau ; ce qui en a besoin est consigné ici.

## Story 1.0 — Fondations (2026-08-23)

| Vérification | Commande | Résultat |
|---|---|---|
| Bootstrap GCP idempotent | `bash scripts/gcp_bootstrap.sh` ×3 | 1re exécution : création (une erreur `setIamPolicy` transitoire sur le SA tout juste créé) ; 2e et 3e : exit 0, chaque étape « déjà présent », sorties `WIF_PROVIDER`/`DEPLOY_SA` identiques |
| Secret `ANTHROPIC_API_KEY` | `gcloud secrets describe … --project=foyer-retour` | présent (v1), `secretAccessor` accordé à `foyer-retour-run@` |
| Bucket sources + PDF | `gcloud storage objects describe gs://foyer-retour-sources/axa-lu-optihome-2017.pdf` | déposé, sha256 vérifié avant envoi (`6824f9d2…af4c`) |
| Budget 50 % | `gcloud billing budgets list` | `foyer-retour-50` créé par le script (50 dans la devise du compte, alertes 50 % et 100 %) |
| Hello-world Cloud Run | `gcloud run deploy foyer-retour --source scripts/hello_world … --max-instances=1` puis `curl` | HTTP 200, `foyer-retour : hello-world`, révision `foyer-retour-00001`, SA runtime `foyer-retour-run@` — les rôles et APIs sont bons avant `deploy.yml` (1.11) |
| IDs de modèles | `uv run python -m server.app.llm.models --check` | `claude-opus-5` OK, `claude-sonnet-5` OK, `claude-haiku-4-5-20251001` OK (exit 0). L'alias `claude-haiku-4-5` du spine n'est pas dans `models.list()` : l'ID daté est retenu |
| Tests sans réseau | `ANTHROPIC_API_KEY= uv run pytest -q` | 49 passed |
| Grep assureurs (NFR11) | `git grep -iE 'baloise\|lalux\|luxembourgeoise\|allianz\|bâloise\|foyer'` | aucune occurrence dans `web/comparateur/` ; seules occurrences ailleurs : « foyer » = ménage et « luxembourgeoise » = adjectif (administration, banque, assurance) dans `kb.js`/`index.html` — faux positifs |
| Plafond de dépense Anthropic | console Anthropic | réglé dans la console : **à confirmer par Lancelot** |
| Variables GitHub | `gh variable list -R byousoku-9/foyer-retour` | `DEPLOY_SA`, `GCP_PROJECT_ID`, `WIF_PROVIDER` créées ; `gh api repos/byousoku-9/foyer-retour/actions/permissions` → `enabled: true` |
