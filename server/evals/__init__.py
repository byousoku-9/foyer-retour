"""AD-14 — Questions-témoins : le golden set et le runner qui l'exécute.

Cette couche dépend de `pipelines`, `corpus`, `llm`, `domain` et `config` (table des couches du
spine, et le même assemblage que fait `api/etat.py` : corpus, index et client sont **construits ici**
et passés au pipeline, qui ne connaît le type d'aucun des trois). L'inverse est interdit :
`server/app/` n'importe jamais `server/evals/` — sans quoi le système mesuré dépendrait de ce qui le
mesure. `tests/test_layers.py` le vérifie statiquement.

La story 1.10 en livre la version **minimale bornée** : le schéma de cas d'AD-14, les sept labels
fixes, l'exécution d'un cas par le pipeline réel et `--gate {doc_id} --profile vertical`. Ce qui
n'est pas là est **refusé**, jamais simulé : le profil `full` (story 4.1), la suite `parsing`
(story 4.2), le cache de réponses, les baselines, la table Markdown et `docs/evals/latest.md`.
"""
