# foyer-retour

Un retour à trois problématiques partagées par l'équipe IA de Foyer — un chatbot pour les nouveaux arrivants au Luxembourg, un sinistre confronté aux conditions générales d'une assurance habitation, un « RAG » sur un Excel de cent mille lignes — prouvé par deux prototypes déployés et une réponse d'architecture pour le troisième.

Le fil rouge : **on ne gagne pas sur « comment on retrouve », on gagne sur « comment on prouve ».** Chaque affirmation montrée à l'utilisateur a survécu à une vérification par du code ; ce que le système ne sait pas est dit, avec la preuve.

## État

Dépôt en construction (août 2026). Story 1.0 livrée : contrats typés du domaine, configuration, `normalize()`, outillage de tests sans réseau, copie du site, infrastructure GCP amorcée. Story 1.1 : le guide est ingéré en arbre de blocs (`data/lux-guide/`), chargé en lecture seule et indexé (`sommaire`, `ouvrir_noeud`, `chercher`). Story 1.2 : le contrat AXA OptiHome 2017 est téléchargé (hash vérifié), découpé en blocs par page avec lignes et boîtes (`data/axa-lu-optihome-2017/`), et ses quatre clauses du cas « bougie » sont typées par overlay manuel. Story 1.3 : le client Claude unique (`server/app/llm/`) — tiers, prix, budget par requête, prompts versionnés, erreurs typées. Story 1.4 : les trois premières étapes (`server/app/steps/`) — *comprendre*, *retrouver* (déterministe, sans modèle) et *rédiger* — produisent une ébauche dont chaque affirmation cite un bloc du corpus. La section déploiement arrive avec la story 1.11.

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
- `pricing.py` : la seule table de prix (USD/MTok, lecture de cache 0,1×, écriture 1,25× en 5 min / 2× en 1 h, `BATCH_DISCOUNT=0.5`) ; `cost_from_usage` chiffre en euros **uniquement** depuis l'`usage` renvoyé par l'API ; `estimate_cost` est la borne haute avant appel (préfixe et schéma de sortie au tarif d'écriture de cache du TTL du modèle, messages au tarif d'entrée, sortie à `max_tokens`, caractères par token calibrés sous le pire mesuré).
- `budget.py` : `RequestBudget(deadline_s, max_attempts, max_cost_eur)` — deadline monotone, compteur d'appels et cumul de coût partagés par toutes les étapes d'une requête.
- `prompting.py` + `prompts/` : `untrusted(kind, text)` délimite le contenu non fiable (balise fermante neutralisée), `load_prompt` lit les prompts versionnés (`commun.md` : règles de non-confiance) couverts par `prompts_digest`.
- `client.py` : `LlmClient.parse(tier, system_prefix, messages, output_model, budget, step, …)` — sorties structurées (schéma Pydantic), bloc système unique avec `cache_control` (préfixe byte-identique), timeout = min(`llm_timeout_s`, deadline restante), 1 retry sur parse invalide si la marge le permet, plafonds d'appels et de coût **avant** l'appel, mapping exhaustif des erreurs SDK vers `LlmUnavailable` (ou `Timeout` pour `APITimeoutError`) — `LlmParse` (parse invalide après retry, refus) et `BudgetExceeded` (plafonds) sont produits par le client lui-même, jamais mappés depuis une erreur SDK —, chaque appel tracé en `LLMCall` — y compris l'appel en échec (timeout, 429, 529, réseau : usage nul, durée réelle) ; `ResponseCache` (protocole) + `MemoryResponseCache` pour le cache d'évals ; `count_tokens` au tokenizer réel.
- `python -m server.app.llm.tokens <fichier…>` : tokens réels d'un fichier par tier (a servi à compacter les sommaires, voir `docs/tests-live.md`).
- `tests/test_client_live.py` fait un appel réel par tier quand la clé est là (fixtures brutes committées dans `tests/llm_fixtures/`, rejouées sans clé).

## Étapes du pipeline

Les étapes vivent dans `server/app/steps/` (story 1.4). Chacune est une fonction `Entrée → Sortie` qui crée et renvoie **son** `StepTrace` ; aucune étape n'en importe une autre (`tests/test_layers.py`), et `steps` ne connaît que `domain`, `corpus`, `llm`, `config`. L'enchaînement, le court-circuit et la relance sont l'affaire des pipelines (story 1.5).

- `comprendre.py` — un appel `micro` : question + historique + profil → `ParsedQuestion` (intent, question résolue autonome, langue, termes toujours en français, `scope` dérivé du profil déclaré). Le modèle répond dans un schéma plat à champs requis, converti ensuite dans le modèle du domaine : un schéma sans valeur par défaut force le modèle à tout remplir. `lang` explicite l'emporte sur la détection ; toute autre valeur retombe sur `fr`. Le préfixe système est statique (`prompts/commun.md` + `prompts/comprendre.md`) — pas de sommaire, l'intent et les termes n'en ont pas besoin. Mesuré en live : ce préfixe (≈ 900 tokens) est **sous la taille minimale cacheable de Haiku 4.5** (2 048 tokens), donc jamais mis en cache — c'est pourquoi le client ne compte un préfixe au tarif de lecture que si le fournisseur confirme l'avoir caché.
- `retrouver.py` — variante `deterministe` de J+1 : **code pur, zéro appel modèle**. `chercher(terms + scope.themes)`, ouverture groupée des nœuds candidats par score (`ouvrir_noeud`, fenêtre centrée sur le meilleur hit du nœud), puis un niveau de renvois (`Block.refs`) et les `definitions()` — des termes de la question **et** des termes rencontrés dans les blocs ouverts, résolues dans leur portée — hors quota `max_opens`. La trace porte le tier qu'AD-9 affecte à l'étape (`reason`) ; c'est `calls=[]` qui dit qu'aucun modèle n'a été appelé. Les blocs sont relus depuis le corpus et jamais modifiés ; `discarded_block_ids` liste exactement les candidats de `chercher` non transmis (AD-10) ; une fenêtre coupée, des nœuds candidats au-delà du quota ou les bornes `max_blocks`/`max_tokens` mettent `truncated=True`. Ces deux bornes de coût (`retrieval_max_blocks`, `retrieval_max_tokens`) s'appliquent par **unités de dépendance** : un bloc de fenêtre voyage avec les cibles de ses renvois, jamais l'inverse — une cible sans le passage qui la cite est inutilisable. Une unité qui ne tient pas est sautée, les suivantes sont essayées. L'étape n'affirme jamais qu'une information est absente du corpus. La navigation par outils est une variante de J+2.
- `rediger.py` — un appel `reason`, sortie `AnswerDraft`. Préfixe cacheable 1 h byte-identique = `prompts/commun.md` + `prompts/rediger.md` + **sommaire versionné** du document ; historique, blocs (précédés de leur `block_id`) et question résolue viennent après le breakpoint, chacun délimité par `untrusted()`. Les invariants du domaine (`claim_id` uniques, `claim_ids` connus, segment `factuel` ⇒ au moins une claim, une quote par bloc) sont validés **au parse** : un draft incohérent déclenche la relance motivée du client, puis `LlmParse`. `motif` (relance de la story 1.5) s'ajoute au dernier message utilisateur, lui aussi délimité par `untrusted()`. Les bornes chiffrées annoncées au modèle (`quote_min_chars`, `quote_max_chars`, `draft_max_segments`, `draft_max_claims`) sont rendues dans le prompt depuis `config.py` (`prompting.render_prompt`) : régler un seuil ne peut plus désynchroniser ce que le modèle produit et ce que le code acceptera. *Rédiger* refuse un `doc_id` qui ne recouvre pas les blocs reçus, avant tout appel : le sommaire du préfixe situe les blocs, il ne peut pas venir d'un autre document.
- Coût : `comprendre_max_tokens` et `rediger_max_tokens` bornent la sortie de chaque appel, `retrieval_max_blocks` et `retrieval_max_tokens` bornent ce que *retrouver* envoie à *rédiger* — ce sont les postes qui font tenir la chaîne sous le plafond de 0,10 € par requête, majorant `estimate_cost` compris. `RequestBudget` mémorise les préfixes déjà écrits dans la requête : un second appel au même préfixe est estimé au tarif de lecture de cache (0,1×) et non d'écriture.
- `tests/test_steps_live.py` rejoue trois scénarios réels sur le corpus `lux-guide` (profil famille → `scope`, question météo → `intent` seul, chaîne complète → ébauche citée sur deux fiches).

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
