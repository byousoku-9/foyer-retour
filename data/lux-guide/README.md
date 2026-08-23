# `lux-guide` — le guide « S'installer au Luxembourg » en blocs

| Fichier | Origine |
|---|---|
| `source.js` | copie octet-identique de `web/app/kb.js`, lui-même issu de [lux-guide/lux-guide.github.io](https://github.com/lux-guide/lux-guide.github.io) au commit `a8e8593` (`edition = git:a8e8593`) |
| `source.url` | URL publique du fichier d'origine |
| `document.json` | arbre de blocs (`Document → Node → Block`, AD-2), généré ; `text_norm` absent (recalculé au chargement) |
| `summary.md` | sommaire compact (catégories → fiches, questions de la FAQ) : futur préfixe cacheable du modèle |
| `report.json` | checks statiques d'ingestion (AD-8) et statistiques |

Régénérer (déterministe, sans Node) : `uv run python -m server.ingest.kb_to_blocks`.
`source_hash` et `ingest_fingerprint` sont écrits dans `document.json` et repris dans `../manifest.json` avec
`document_hash` ; le serveur recalcule les hashes au démarrage, compare les empreintes et met ce document en
quarantaine à la moindre différence (AD-7).
La `timeline` de `kb.js` n'est pas ingérée (comptée dans `report.json`).
