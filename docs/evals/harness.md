# Harness de questions-témoins

Le harness est `python -m server.evals.run`. Il valide tous les cas et la compatibilité suite/variante avant de construire le client. Une exécution réelle exige `ANTHROPIC_API_KEY` et un plafond fini strictement positif ; `--dry-run` valide le plan sans clé, corpus, client ni écriture.

```bash
uv run python -m server.evals.run --profile full --max-cost 1.0
uv run python -m server.evals.run --profile full --quick --max-cost 1.0
uv run python -m server.evals.run --suite guide --variant deterministe --max-cost 1.0
uv run python -m server.evals.run --help
```

`full` inclut les cas `vertical` et `full`. `--quick` choisit de façon stable le premier identifiant de chaque suite documentaire sélectionnée ; il est incompatible avec `--gate`, car un gate doit porter sur toute sa suite. Le guide accepte `outils` et `deterministe`; le sinistre accepte seulement `deterministe`. Un couple incompatible est refusé avant tout appel.

## Cache et invalidation

Le cache local vaut par défaut `<parent de data>/.cache/evals`; `--cache-dir` le déplace. La clé complète couvre le corps fournisseur exact (modèle, paramètres, outils, schéma et messages) et une namespace canonique : variante, digests pipeline/prompts, modèles, seuils actifs, `source_hash`, `ingest_fingerprint`, `normalize_version` et entrée du cas. Changer une seule composante crée une autre namespace. Un hit ne contacte pas Anthropic, vaut `cost_eur=0` et conserve `cost_eur_original`. Une entrée tronquée, corrompue ou d'une autre version est un incident explicite, jamais un miss silencieux. Chaque écriture utilise un fichier temporaire unique puis `os.replace`.

## Budget et artefacts

Le prochain appel est majoré avant envoi contre ce qui reste de `--max-cost`. Si le majorant dépasse le reste, le run s'arrête sans cet appel. Les cas terminés gardent leur unique label ; les cas non exécutés vivent dans `unexecuted_cases` et ne reçoivent aucun huitième label. Le JSON et le Markdown sont écrits atomiquement même sur cet arrêt, avec `complete=false` et `stop_reason`.

Les chemins par défaut sont `eval-results.json` et `eval-results.md` à côté de `data/`. Ils se déplacent avec `--output-json` et `--output-markdown`. Le JSON expose par cas label, variante, coûts courant/original, latence et claims. Le Markdown agrège les sept labels, les variantes, recall, coût moyen, latence p50, `cases_hash` et taux de `ne_tranche_pas`.

## Pytest et CI

`pytest -q` porte par défaut `-m 'not evals'` et reste entièrement hors réseau. Le profil payant est explicite :

```bash
ANTHROPIC_API_KEY=... uv run pytest -m evals -q --evals-max-cost 1.0
```

Sans clé ou plafond, la collecte refuse avant le premier test. Sur GitHub, le cache est restauré entre runs. Une pull request avec secret joue `full --quick`; `main` joue `full`. Sans secret, le résumé GitHub signale explicitement le skip. La table Markdown est ajoutée à `$GITHUB_STEP_SUMMARY` même si le test live rend un code non nul.

La campagne élargie et la suite parsing relèvent de 4.2 ; le holdout de 4.3, les baselines publiées de 4.4 et l'API/page d'accueil de 4.5 ne font pas partie de ce harness.
