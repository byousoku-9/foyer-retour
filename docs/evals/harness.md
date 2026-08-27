# Harness de questions-témoins

Le harness est `python -m server.evals.run`. Il valide tous les cas, les grilles compagnon et la compatibilité suite/variante avant de construire un éventuel client. Les suites guide et sinistre exigent `ANTHROPIC_API_KEY` et un plafond fini strictement positif. La suite parsing est entièrement locale : elle ne construit aucun client, ne lit aucune clé et coûte zéro. `--dry-run` valide le plan sans clé, corpus, client ni écriture.

```bash
uv run python -m server.evals.run --profile full --max-cost 1.0
uv run python -m server.evals.run --profile full --quick --max-cost 1.0
uv run python -m server.evals.run --suite guide --variant deterministe --max-cost 1.0
ANTHROPIC_API_KEY= uv run python -m server.evals.run --suite parsing --profile full --max-cost 0.01
uv run python -m server.evals.run --help
```

`full` inclut les cas `vertical` et `full`. `--quick` choisit de façon stable le premier identifiant de chaque suite documentaire sélectionnée ; il est incompatible avec `--gate`, car un gate doit porter sur toute sa suite. Le guide accepte `outils` et `deterministe`; le sinistre accepte seulement `deterministe`; le parsing porte la variante `local`. Un couple incompatible est refusé avant tout appel.

Sur un lot qui mêle plusieurs suites, `--variant` ne peut pas désigner une variante commune quand
les vocabulaires diffèrent. Il faut d'abord sélectionner la suite, par exemple
`--suite guide --variant deterministe` ou `--suite parsing --variant local`.

## Golden set et références

Le profil complet contient 34 cas guide, 16 cas sinistre sur AXA et Baloise, et 11 observations parsing sur les deux contrats. Les cinq YAML `vertical` historiques restent byte-identiques ; le champ optionnel `expected.clarification` est absent de ces cinq témoins et ne change aucun verdict admissible. Les cas guide `full` nomment leur famille : parcours, météo FR/EN/DE, suivi, multilingue, traversée de trois fiches ou hors-guide. Les sinistres couvrent les six robustesses, la perte d'exploitation à domicile, la chaleur sans incendie, le téléphone en vacances, l'exclusion animale `non_couvert`, l'arbitrage habitation/auto/RC et l'acte volontaire.

`server/evals/reference/utilite-guide.yaml` porte, pour tout cas guide qui déclare un scénario — témoin vertical compris — l'ordre juste, les documents et l'interlocuteur. `server/evals/reference/retraductions.yaml` relie chaque cas multilingue à sa fixture, au test hors ligne, à la section du journal, au résultat, aux écarts et à la réserve de signature. Les compagnons sont validés strictement, figés sur les mêmes octets qui ont été parsés, puis revérifiés. `cases_hash` reste l'autorité unique des seuls YAML de cas, comme le gate le recalcule ; les compagnons ont leur propre `identity.scope.references_digest`, présent seulement sur un run `full` qui contient du guide.

Un cas parsing vit sous `cases/parsing/<doc_id>/`, référence exactement un bloc et une transcription déjà normalisée par l'unique `normalize()`. Bloc absent ou trouvé dans un autre document donne `citation_introuvable` avec `found=false`, texte différent donne `parsing`, égalité donne `bonne_reponse`. Aucun repli inter-document n'est admis. Les trois divergences visuelles Baloise p30/p40/p48 sont conservées rouges ; une miniature entièrement exacte prouve séparément `recall=1.0`.

## Cache et invalidation

Le cache local vaut par défaut `<parent de data>/.cache/evals`; `--cache-dir` le déplace. La clé complète couvre le corps fournisseur exact (modèle, paramètres, outils, schéma et messages) et une namespace canonique : variante, digests pipeline/prompts, modèles, seuils actifs, `source_hash`, `ingest_fingerprint`, `normalize_version` et entrée du cas. Changer une seule composante crée une autre namespace. Un hit ne contacte pas Anthropic, vaut `cost_eur=0` et conserve `cost_eur_original`. Une entrée tronquée, corrompue ou d'une autre version est un incident explicite, jamais un miss silencieux. Chaque écriture utilise un fichier temporaire unique puis `os.replace`.

## Budget et artefacts

Le prochain appel est majoré avant envoi contre ce qui reste de `--max-cost`. Si le majorant dépasse le reste, le run s'arrête sans cet appel. Les cas terminés gardent leur unique label ; les cas non exécutés vivent dans `unexecuted_cases` et ne reçoivent aucun huitième label. Le JSON et le Markdown sont écrits atomiquement même sur cet arrêt, avec `complete=false` et `stop_reason`.

Le défaut de configuration est 1 EUR, tandis qu'un `full` fournisseur porte 50 cas guide/sinistre.
Au plafond métier de 0,10 EUR par requête, le majorant arithmétique à cache froid est donc 5 EUR :
le défaut peut produire un rapport partiel, ce n'est pas une estimation de facture complète.
Toujours faire le dry-run, inspecter ou réchauffer le cache, puis passer explicitement
`--max-cost <euros>` selon le risque accepté. Le quick fournisseur porte trois cas documentaires,
soit un majorant de 0,30 EUR.
Le calibrage ou le changement du défaut appartient aux baselines de la story 4.4 ; la story 4.2
documente ce risque sans modifier le seuil métier ni la configuration.

Les chemins par défaut sont `eval-results.json` et `eval-results.md` à côté de `data/`. Ils se déplacent avec `--output-json` et `--output-markdown`. Le JSON expose par cas label, variante, coûts courant/original, latence et claims. Son objet `identity` fige l'image (digests, modèles, normalisation), les documents et le périmètre exact (profil, quick, suites, cas, variantes et digests de namespace). Le Markdown agrège les sept labels, les variantes, recall, coût moyen, latence p50, `cases_hash` et taux de `ne_tranche_pas`.

## Pytest et CI

`pytest -q` porte par défaut `-m 'not evals'` et reste entièrement hors réseau. Le profil payant est explicite :

```bash
ANTHROPIC_API_KEY=... uv run pytest -m evals -q --evals-max-cost 1.0
```

Cette entrée payante ajoute toujours `--exclude-suite parsing` : elle mesure guide/sinistre et
échoue si l'un de ces cas est rouge. Les divergences parsing ne sont ni masquées ni facturées ; elles
se rejouent par la commande locale dédiée ci-dessus, dont le code 1 est attendu tant que les trois
transcriptions visuelles divergent. Sans clé ou plafond, la collecte refuse avant le premier test.
Sur GitHub, le cache est restauré entre runs. Une pull request avec secret joue `full --quick`;
`main` joue `full`. Sans secret, le résumé GitHub signale explicitement le skip. La table Markdown
est ajoutée à `$GITHUB_STEP_SUMMARY` même si le test live rend un code non nul.

Variables lues par l'unique test live :

| Variable | Rôle | Défaut |
|---|---|---|
| `EVALS_PROFILE` | profil transmis au runner | `full` |
| `EVALS_QUICK` | active `--quick` pour `1`, `true` ou `yes` | désactivé |
| `EVALS_CACHE_DIR` | cache persistant restauré/sauvegardé par la CI | répertoire temporaire du test |
| `EVALS_OUTPUT_JSON` | rapport JSON | fichier temporaire du test |
| `EVALS_OUTPUT_MARKDOWN` | rapport Markdown et résumé GitHub | fichier temporaire du test |

Le plafond n'est volontairement pas une variable implicite : `--evals-max-cost` reste obligatoire
sur la commande pytest. `ANTHROPIC_API_KEY` reste la clé fournisseur explicite. Le cache CI est
sauvegardé même après un échec de mesure, sauf lorsqu'une restauration a trouvé la clé exacte.

Le holdout de 4.3, les baselines publiées de 4.4 et l'API/page d'accueil de 4.5 ne font pas partie de ce livrable.
