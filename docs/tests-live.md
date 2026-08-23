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
| Budget abaissé (23/08, avant la boucle autonome) | `gcloud billing budgets update … --budget-amount=10USD` | `foyer-retour-10` : alertes à 5 $ et 10 $ ; un budget n'arrête rien — le plafond dur reste `max-instances=1` / `--timeout=60` (AD-13) et le crédit prépayé Anthropic |
| Hello-world Cloud Run | `gcloud run deploy foyer-retour --source scripts/hello_world … --max-instances=1` puis `curl` | HTTP 200, `foyer-retour : hello-world`, révision `foyer-retour-00001`, SA runtime `foyer-retour-run@` — les rôles et APIs sont bons avant `deploy.yml` (1.11) |
| IDs de modèles | `uv run python -m server.app.llm.models --check` | `claude-opus-5` OK, `claude-sonnet-5` OK, `claude-haiku-4-5-20251001` OK (exit 0). L'alias `claude-haiku-4-5` du spine n'est pas dans `models.list()` : l'ID daté est retenu |
| Tests sans réseau | `ANTHROPIC_API_KEY= uv run pytest -q` | 88 passed (après revues Claude et Codex) |
| Grep assureurs (NFR11) | grep d'une liste privée de noms d'assureurs (hors repo) | zéro occurrence hors faux positifs (« foyer » = ménage, « luxembourgeoise » = adjectif) ; rien dans `web/comparateur/` |
| Plafond de dépense Anthropic | — | compte en crédit prépayé rechargé à la main par Lancelot (confirmé le 23/08/2026) — pas de plafond self-service à régler, le solde de crédit est la borne dure ; les plafonds côté code (0,10 €/requête, `--max-cost` par run d'évals) restent les garde-fous applicatifs |
| Variables GitHub | `gh variable list -R byousoku-9/foyer-retour` | `DEPLOY_SA`, `GCP_PROJECT_ID`, `WIF_PROVIDER` créées ; `gh api repos/byousoku-9/foyer-retour/actions/permissions` → `enabled: true` |

## Story 1.1 — Le guide devient un classeur à blocs (2026-08-23)

Aucun réseau ni modèle : l'ingestion est locale et déterministe. Exécution réelle consignée pour référence.

| Vérification | Commande | Résultat |
|---|---|---|
| Ingestion de la source réelle | `uv run python -m server.ingest.kb_to_blocks` | exit 0 ; 36 fiches, 41 FAQ, 88 nœuds, 506 blocs (391 `para`, 112 `heading`, 3 `table`) ; timeline (5 phases, 38 étapes) non ingérée (`info`) ; aucun bloquant, aucune alerte ; `status: servi`, `gate: null` |
| Stabilité | relance puis `git status --short data/` | aucune modification (`document.json`, `report.json`, `summary.md`, `manifest.json` octet-identiques) |
| Chargement | `load_corpus('data', allow_ungated=True)` | `quarantine={}`, `alerts={'lux-guide': ['sans_gate']}`, 506 blocs avec `text_norm` ; `allow_ungated=False` ⇒ `lux-guide` en quarantaine `sans_gate` |
| Taille du sommaire | `wc -c data/lux-guide/summary.md` ; `report.json` (`sommaire_chars`) | 12 892 octets UTF-8 = 12 419 caractères, soit ≈ 3 100 tokens à 4 car./token (estimation grossière ; l'AC vise ≈ 2–3 k tokens, la mesure réelle au tokenizer Anthropic est reportée à la story 1.3) — dans la fourchette 8–14 ko visée |
| Recherche | `chercher({"matricule": [], "commune": []}, limit=20)` | 20 résultats ; en tête `lux-guide:farrivee:2` (le résumé, les deux termes), puis `farrivee:6` et `farrivee:11`, avant tout bloc à un seul terme |
| Tests sans réseau | `ANTHROPIC_API_KEY= uv run pytest -q` | 165 passed (après revue du diff et revue Codex 1.1) |
| Revue Codex 1.1 | 6 bloquants traités : règle du gate (`gate_echoue`, gate invalide = `sans_gate`, `gate_perime` contre `GateContext`), `Document.ingest_fingerprint` vérifié par le loader, manifest validé entrée par entrée, `corpus` sans pydantic (`test_layers`), `ids_disparus` sur texte changé + `ids_nouveaux`, invariants référentiels du domaine ; + manifest lu avant toute écriture, `timeline` validée, préfixe `window.KB` seul admis | `data/` regénéré (`document.json` porte `ingest_fingerprint`), 165 tests verts |
