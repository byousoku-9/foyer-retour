# `axa-lu-optihome-2017` — les conditions d'assurances OptiHome (AXA Luxembourg, juin 2017) en blocs

| Fichier | Origine |
|---|---|
| `source.url` | URL publique du PDF sur le CDN d'AXA Luxembourg |
| `source.sha256` | sha256 de référence du PDF ; un contenu différent fait échouer le téléchargement (exit 2), jamais la mise en ligne d'un autre texte |
| `source.pdf` | **non committé** (`.gitignore`) : téléchargé par `uv run python -m server.ingest.fetch_source axa-lu-optihome-2017` (repli `gs://foyer-retour-sources/axa-lu-optihome-2017.pdf` si l'URL est morte) ; l'image Docker le fait au build |
| `typing.manual.json` | typage manuel des quatre clauses du cas bougie (`kind`, `defines`, `scope_node_id`, `kind_source=manual`), fusionné par le loader **avant** validation, sans toucher `document.json` |
| `document.json` | arbre de blocs (`Document → Node → Block → Line`, AD-2), généré ; `text_norm` et valeurs par défaut absents |
| `summary.md` | sommaire compact (nœuds de niveau 1 à 3, première page) |
| `report.json` | checks statiques d'ingestion (AD-8) et statistiques |

Régénérer (déterministe) : `uv run python -m server.ingest.pdf_to_blocks axa-lu-optihome-2017`.

- `block_id = axa-lu-optihome-2017:p{page}:{seq}` ; `bbox` = union des `lines` (points PDF, origine haut-gauche).
- Nœuds = numérotation des articles (`a1.12`, `a3.1.1.1.6`) ; `scope.kind` : `1`, `2`, `3.1` et descendants ⇒ `commun`, `3.1.8` et descendants ⇒ `extension`, le reste ⇒ `special`.
- Pages 2–4 (table des matières imprimée) : un bloc `autre` par page sous `axa-lu-optihome-2017:tdm`, non interprété (story 3.1).
- Texte brut sauf : puces Wingdings → `•`, glyphe de tabulation retiré, fins de ligne nettoyées (déclaré dans `FLAGS`, donc dans `ingest_fingerprint`).
- Extraits relus des pages 9, 11, 34, 46 : `tests/data/axa/*.txt` (`tests/test_parsing_axa.py`).
