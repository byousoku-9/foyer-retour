# foyer-retour

Un retour à trois problématiques partagées par l'équipe IA de Foyer — un chatbot pour les nouveaux arrivants au Luxembourg, un sinistre confronté aux conditions générales d'une assurance habitation, un « RAG » sur un Excel de cent mille lignes — prouvé par deux prototypes déployés et une réponse d'architecture pour le troisième.

Le fil rouge : **on ne gagne pas sur « comment on retrouve », on gagne sur « comment on prouve ».** Chaque affirmation montrée à l'utilisateur a survécu à une vérification par du code ; ce que le système ne sait pas est dit, avec la preuve.

## État

Dépôt en construction (août 2026). Story 1.0 livrée : contrats typés du domaine, configuration, `normalize()`, outillage de tests sans réseau, copie du site, infrastructure GCP amorcée. Story 1.1 : le guide est ingéré en arbre de blocs (`data/lux-guide/`), chargé en lecture seule et indexé (`sommaire`, `ouvrir_noeud`, `chercher`). Story 1.2 : le contrat AXA OptiHome 2017 est téléchargé (hash vérifié), découpé en blocs par page avec lignes et boîtes (`data/axa-lu-optihome-2017/`), et ses quatre clauses du cas « bougie » sont typées par overlay manuel. Story 1.3 : le client Claude unique (`server/app/llm/`) — tiers, prix, budget par requête, prompts versionnés, erreurs typées. La section déploiement arrive avec la story 1.11.

## Lancer

```bash
uv sync                                   # Python 3.13 et dépendances épinglées (pyproject.toml, uv.lock)
cp .env.example .env                      # puis renseigner ANTHROPIC_API_KEY (jamais commité)
uv run python -m server.app.llm.models --check   # vérifie que les IDs de modèles existent (exit 2 sans clé)
```

Le serveur HTTP arrive en story 1.6 ; le site copié (`web/`) s'ouvre déjà tel quel (`web/index.html`).

## Tester

```bash
ANTHROPIC_API_KEY= uv run pytest -q       # unitaires, sans réseau : les appels modèle sont rejoués depuis tests/llm_fixtures/
uv run pytest -q                          # avec la clé, les tests qui appellent un modèle ré-enregistrent leur fixture
```

- `tests/test_layers.py` vérifie statiquement les couches du spine (`domain` n'importe rien ; une étape n'importe jamais une autre étape).
- `tests/test_domain.py` vérifie que chaque modèle porte exactement les champs et enums de son AD.
- Les seuils numériques vivent dans `server/app/config.py` et se surchargent par variable d'environnement.
- Ré-enregistrer une fixture LLM : supprimer `tests/llm_fixtures/{module}.{test}.json` puis relancer ce test avec `ANTHROPIC_API_KEY` renseignée (réseau, coût) ; committer le JSON avec le pourquoi.
- Les vérifications qui touchent le réseau (GCP, API) sont consignées dans `docs/tests-live.md`.

## Ingestion du guide

```bash
uv run python -m server.ingest.kb_to_blocks        # data/lux-guide/source.js → document.json, summary.md, report.json ; fusion de data/manifest.json
```

- `data/lux-guide/source.js` est la copie octet-identique de `web/app/kb.js` (filiation dans `data/lux-guide/README.md`) ; le parseur est en Python pur, sans Node.
- Sortie déterministe : relancer ne modifie rien ; `document.json` ne contient jamais `text_norm`, recalculé au chargement.
- Exit 1 et `status: quarantaine` dans `manifest.json` si un check bloquant (invariant d'arbre) est levé ; `ids_disparus` est une alerte calculée contre le `document.json` précédent.
- `manifest.gate` reste `null` jusqu'au premier gate des questions-témoins (story 1.10) ; en dev, `ALLOW_UNGATED` sert le document avec l'alerte `sans_gate`.
- Le serveur lit `data/` par `server/app/corpus/loader.py` (hashes recalculés, quarantaine par document) et l'indexe par `server/app/corpus/index.py` ; les seuils (`search_limit`, `node_window`) viennent de `server/app/config.py`.

## Ingestion du contrat AXA

```bash
uv run python -m server.ingest.fetch_source axa-lu-optihome-2017   # source.url + source.sha256 → source.pdf (non committé)
uv run python -m server.ingest.pdf_to_blocks axa-lu-optihome-2017  # source.pdf → document.json, summary.md, report.json ; manifest fusionné
```

- Le PDF n'est **pas redistribué** : `fetch_source` le télécharge depuis `source.url` (URL publique d'AXA, ou `gs://bucket/objet` du bucket privé), vérifie le sha256 de référence (`source.sha256`) et se replie sur `gs://foyer-retour-sources/` si la source est morte ; hash différent ⇒ exit 2 sans rien écrire, repli en échec ⇒ exit 3. Le `Dockerfile` exécute `fetch_source --all` au build. En local, les objets `gs://` sont lus avec le jeton `GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)`. `pdf_to_blocks` exige `source.sha256` et le compare au PDF avant toute extraction.
- `pdf_to_blocks` (PyMuPDF, `get_text("dict", sort=True)`) : un bloc par paragraphe/liste/titre avec `page`, `lines` (`line_id`, `text`, `bbox`) et `bbox` = union ; en-têtes et numéros de page retirés ; nœuds construits par la numérotation des articles (`a1.12`, `a3.1.1.1.6`, parent = préfixe le plus long) ; `scope.kind` : `1`, `2`, `3.1` ⇒ `commun`, `3.1.8` ⇒ `extension`, le reste ⇒ `special` ; un paragraphe ou une liste à cheval sur deux pages est scindé, le second bloc porte `continues` (règle structurelle : même kind, pas de numéro d'article, phrase non terminée). Les signets du PDF (`get_toc()`), s'il y en a, donnent leur titre aux nœuds qui n'en ont pas et sont confrontés à la numérotation (`tdm_pdf_ecart`). Texte brut, seules altérations déclarées dans `FLAGS` (puces `•`, glyphe de tabulation retiré, espaces de fin et de tête de ligne, numéro d'article joint par une espace à son titre).
- `report.json` (AD-8) : `page_sans_texte` (page portant une image ou un tracé vectoriel mais aucun texte : bloquant ⇒ quarantaine), `numerotation_non_monotone`, `numerotation_dupliquee`, `pages_mixtes`, `tdm_pdf_ecart`, `ids_disparus` (alertes), statistiques (pages, blocs par kind, `continues`, en-têtes retirés, TdM du PDF).
- `data/axa-lu-optihome-2017/typing.manual.json` : typage manuel des clauses (`kind`, `defines`, `scope_node_id`, `scope_node_ids`, `kind_source=manual`), fusionné par le loader **avant** validation sans modifier `document.json` ; `schema_version`, `doc_id` et `kind` sont obligatoires ; un `block_id` ou un nœud inconnu, un `kind_source` ≠ `manual` ou un champ inattendu met ce seul document en quarantaine. Son empreinte est écrite dans `manifest.json` (`overlay_hash`) par l'ingestion et vérifiée par le loader, et elle fait partie de ce que le gate des témoins couvre. `scope_node_ids` restreint la portée d'une clause à une partie du sous-arbre de `scope_node_id` (l'exclusion p. 46 ne vise que les extensions 3.1.8.3 à 3.1.8.6 ; `Document.scope_nodes(block_id)` donne les nœuds couverts). Remplacé par le typage automatique (Epic 3).
- Les seuils d'ingestion (`header_band_pt`, `footer_band_pt`, `header_min_pages_ratio`, `para_gap_ratio`, `article_number_max_x`, `title_min_size_pt`, `header_caps_max_size_pt`, `baseline_tolerance_pt`, `number_gap_tolerance_pt`, `list_indent_pt`, `fetch_timeout_s`, `metadata_timeout_s`) vivent dans `server/app/config.py` ; ceux qui changent la segmentation entrent dans `ingest_fingerprint`.
- `tests/test_parsing_axa.py` compare les pages 9, 11, 34 et 46 à des extraits relus (`tests/data/axa/`) contre le `document.json` committé ; si `source.pdf` est présent, l'ingestion doit le regénérer à l'identique.
- Les artefacts de tous les documents sont écrits sans valeurs par défaut ni `text_norm` (`SCHEMA_VERSION=2`, `server/ingest/artifacts.py`).

## Client LLM

Le client Claude unique des étapes vit dans `server/app/llm/` (story 1.3) ; aucune étape n'appelle le SDK directement.

- `models.py` : `TIERS` (`ingest` = Opus 5 Batch, `reason` = Sonnet 5 cache 1 h, `micro` = Haiku 4.5 daté), `STEP_TIERS`, `MODEL_CAPS` (effort/temperature/TTL par modèle), `EFFORT`, `model_for(tier)` ; `--check` vérifie les IDs contre l'API.
- `pricing.py` : la seule table de prix (USD/MTok, lecture de cache 0,1×, écriture 1,25× en 5 min / 2× en 1 h, `BATCH_DISCOUNT=0.5`) ; `cost_from_usage` chiffre en euros **uniquement** depuis l'`usage` renvoyé par l'API ; `estimate_cost` est la borne haute avant appel.
- `budget.py` : `RequestBudget(deadline_s, max_attempts, max_cost_eur)` — deadline monotone, compteur d'appels et cumul de coût partagés par toutes les étapes d'une requête.
- `prompting.py` + `prompts/` : `untrusted(kind, text)` délimite le contenu non fiable (balise fermante neutralisée), `load_prompt` lit les prompts versionnés (`commun.md` : règles de non-confiance) couverts par `prompts_digest`.
- `client.py` : `LlmClient.parse(tier, system_prefix, messages, output_model, budget, step, …)` — sorties structurées (schéma Pydantic), bloc système unique avec `cache_control` (préfixe byte-identique), timeout = min(`llm_timeout_s`, deadline restante), 1 retry sur parse invalide si la marge le permet, plafonds d'appels et de coût **avant** l'appel, mapping exhaustif des erreurs SDK vers `LlmUnavailable` (ou `Timeout` pour `APITimeoutError`) — `LlmParse` (parse invalide après retry, refus) et `BudgetExceeded` (plafonds) sont produits par le client lui-même, jamais mappés depuis une erreur SDK —, chaque appel tracé en `LLMCall` ; `ResponseCache` (protocole) + `MemoryResponseCache` pour le cache d'évals ; `count_tokens` au tokenizer réel.
- `python -m server.app.llm.tokens <fichier…>` : tokens réels d'un fichier par tier (a servi à compacter les sommaires, voir `docs/tests-live.md`).
- `tests/test_client_live.py` fait un appel réel par tier quand la clé est là (fixtures brutes committées dans `tests/llm_fixtures/`, rejouées sans clé).

## Infrastructure

`scripts/gcp_bootstrap.sh` (idempotent, `--project=foyer-retour`, `europe-west1`) crée les comptes de service, la fédération d'identité GitHub, le secret, les buckets et le budget ; il affiche les trois variables GitHub à poser (`GCP_PROJECT_ID`, `WIF_PROVIDER`, `DEPLOY_SA`). `scripts/hello_world/` est le conteneur minimal qui a validé les rôles sur Cloud Run avant le déploiement automatique ; il est déployé volontairement sous le nom final du service, `foyer-retour`, pour que `deploy.yml` (story 1.11) le remplace en place sans changer d'URL. Le PDF du contrat n'est pas dans le repo : le script le dépose dans `gs://foyer-retour-sources/` depuis `PDF_LOCAL` (chemin fourni à la main).

## Ce que contiendra ce dépôt

| Dossier | Rôle |
|---|---|
| `server/` | Le serveur Python (Cloud Run) : assistant du guide, outil sinistre / conditions générales, ingestion |
| `web/` | Le site « S'installer au Luxembourg » forké depuis [lux-guide/lux-guide.github.io](https://github.com/lux-guide/lux-guide.github.io), branché sur le serveur ; modifications minimales et justifiées, une par commit |
| `tools/` | L'outil sinistre / conditions générales, la page d'accueil, la réponse au sujet 3 |
| `tests/` | Tests unitaires sans réseau (fixtures LLM enregistrées) ; les **questions-témoins** vivent dans `server/evals/` |
| `scripts/` | Bootstrap GCP idempotent, hello-world Cloud Run |
| `docs/` | `architecture.md`, `choix-et-limites.md`, résultats des questions-témoins |

## Principes

- **Restitution d'abord** : source par affirmation, état explicite (sûr / partiel / inconnu), trace consultable par réponse, coût affiché.
- **Déterminisme sur les vrais invariants** : le modèle propose, le code vérifie ; pas de verdict sans clause citée et retrouvée mot pour mot dans la source.
- **L'intelligence se paie une fois, à l'ingestion** ; la requête reste légère.
- **Un document mal ingéré n'est pas servi** : rapport d'ingestion visible, quarantaine.
- **Écrit pour être relu** : petits commits qui disent pourquoi, README pour le mainteneur qui arrive.
