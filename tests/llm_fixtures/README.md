# Fixtures LLM enregistrées

Un fichier `{nom_du_test}.json` par test qui appelle un modèle, écrit par `tests/fixtures.py` (`LLMRecorder`).

- Avec `ANTHROPIC_API_KEY` : l'appel réel est exécuté et sa réponse écrite ici (réseau, coût).
- Sans clé (CI, `uv run pytest`) : la réponse est rejouée, zéro réseau ; fixture absente ⇒ `FixtureMissing`.

Contenu : `{clé_de_requête: {request, response}}` — la clé est `modèle:sha256(messages)[:16]`,
jamais la clé API ni le texte brut des messages ; la réponse est un `model_dump()` JSON.
Les fichiers sont committés : toute régénération est un commit qui dit pourquoi.
