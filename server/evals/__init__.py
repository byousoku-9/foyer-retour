"""AD-14 — Questions-témoins : le golden set et le runner qui l'exécute.

Cette couche dépend de `pipelines`, `corpus`, `llm`, `domain` et `config` (table des couches du
spine, et le même assemblage que fait `api/etat.py` : corpus, index et client sont **construits ici**
et passés au pipeline, qui ne connaît le type d'aucun des trois). L'inverse est interdit :
`server/app/` n'importe jamais `server/evals/` — sans quoi le système mesuré dépendrait de ce qui le
mesure. `tests/test_layers.py` le vérifie statiquement.

La story 4.1 livre le profil `full`, le sous-ensemble quick, les variantes compatibles, le cache
persistant namespacé, le plafond par run et les rapports JSON/Markdown complets ou partiels. Ce qui
n'est pas là est **refusé**, jamais simulé : la suite `parsing` (story 4.2), le holdout et les
baselines committées (stories 4.3/4.4), `docs/evals/latest.md` et l'API latest (story 4.5).
"""
