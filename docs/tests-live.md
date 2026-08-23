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

## Story 1.2 — Le contrat AXA devient un classeur à blocs (2026-08-23)

| Vérification | Commande | Résultat |
|---|---|---|
| Téléchargement réel | `uv run python -m server.ingest.fetch_source axa-lu-optihome-2017` puis `shasum -a 256 data/axa-lu-optihome-2017/source.pdf` | exit 0 ; 544 969 octets depuis le CDN AXA Luxembourg (`source.url`) ; sha256 `6824f9d2…af2c` identique à la référence ; `source.pdf` ignoré par git |
| Repli `gs://` réel | `source.url` volontairement morte (copie temporaire) + `GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)` | HTTP 403 sur l'URL ⇒ repli `storage.googleapis.com/foyer-retour-sources/axa-lu-optihome-2017.pdf`, hash vérifié, exit 0 |
| Ingestion réelle | `uv run python -m server.ingest.pdf_to_blocks axa-lu-optihome-2017` | exit 0 ; 109 pages (108 avec blocs, p. 108 blanche = info), 1 466 blocs (890 `para`, 419 `heading`, 154 `list`, 3 `autre` = TdM p. 2–4), 751 nœuds, 749 numéros d'article monotones, 2 `continues`, 214 en-têtes/pieds retirés, `get_toc()` vide ; aucun bloquant, aucune alerte ; `status: servi`, `gate: null`. **Après revue Codex 1.2** (parseur 2) : 1 457 blocs (884 `para`, 419 `heading`, 151 `list`, 3 `autre`), 5 `continues` (p30→31, p53→54, p81→82, p82→83, p101→102), 31 IDs réaffectés sur les p. 54, 80, 89 (continuations de liste fusionnées), aucune page sans texte dessinée ; une alerte `pages_mixtes` (p. 1, couverture avec image) ; `overlay_hash` écrit dans le manifest |
| Stabilité | relance puis `git status --short data/` | aucune modification (`document.json`, `report.json`, `summary.md`, `manifest.json` octet-identiques) |
| Chargement + overlay | `load_corpus('data', allow_ungated=True)` | `quarantine={}`, alertes `sans_gate` ×2 ; 4 blocs confirmés : `p9:2` definition « contenu », `p11:12` definition « mobilier de jardin », `p34:12` garantie (scope `a3.1.1`), `p46:1` exclusion (scope `a3.1.8`, restreinte par `scope_node_ids` aux seules extensions `a3.1.8.3`–`a3.1.8.6` — revue Codex 1.2) ; `document.json` intact ; 1 457 blocs |
| Relecture des pages 9, 11, 34, 46 | rendu PNG des pages (110 dpi) relu visuellement, extraits saisis dans `tests/data/axa/` | `normalize(extrait) == normalize(blocs)` pour §1.12, §1.28, §3.1.1.1.6, alinéa p. 46 par l'agent. **Relecture humaine de Lancelot : en attente** (input bloquant de la story, revue Codex 1.2 B1) — les rendus PNG des quatre pages sont dans `automation/runs/20260823-171404-story-1.2/pages/` (hors repo) ; à consigner ici une fois faite |
| Tests sans réseau | `ANTHROPIC_API_KEY= uv run pytest -q` | 209 passed avec `source.pdf` ; 208 passed, 1 skipped sans. Après revue Codex 1.2 : 241 passed ; 240 passed, 1 skipped sans `source.pdf` |
| Fetch réel (revue Codex 1.2) | `uv run python -m server.ingest.fetch_source axa-lu-optihome-2017` | exit 0, 544 969 octets, sha256 `6824f9d2…af2c` (le chemin `gs://` en source principale est couvert par `MockTransport`, non rejoué en réel) |
| Revue Codex | boucle autonome | Revue Codex : 8 bloquants / 7 importants, convergé en 2 tour(s) (boucle autonome) |
| Image Docker | `docker build -t foyer-retour .` | non exécuté : démon Docker absent sur le poste ; le build réel sera validé par `deploy.yml` en 1.11 (l'étape `fetch_source --all` est testée ici via les codes de sortie) |
