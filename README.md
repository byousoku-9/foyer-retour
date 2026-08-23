# foyer-retour

Un retour à trois problématiques partagées par l'équipe IA de Foyer — un chatbot pour les nouveaux arrivants au Luxembourg, un sinistre confronté aux conditions générales d'une assurance habitation, un « RAG » sur un Excel de cent mille lignes — prouvé par deux prototypes déployés et une réponse d'architecture pour le troisième.

Le fil rouge : **on ne gagne pas sur « comment on retrouve », on gagne sur « comment on prouve ».** Chaque affirmation montrée à l'utilisateur a survécu à une vérification par du code ; ce que le système ne sait pas est dit, avec la preuve.

## État

Dépôt en construction (août 2026). Story 1.0 livrée : contrats typés du domaine, configuration, `normalize()`, outillage de tests sans réseau, copie du site, infrastructure GCP amorcée. Story 1.1 : le guide est ingéré en arbre de blocs (`data/lux-guide/`), chargé en lecture seule et indexé (`sommaire`, `ouvrir_noeud`, `chercher`). La section déploiement arrive avec la story 1.11.

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
