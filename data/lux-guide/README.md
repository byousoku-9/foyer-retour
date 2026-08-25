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
De la `timeline` de `kb.js`, seules les **conditions** sont ingérées (story 2.3) : `Document.parcours`
porte, pour chaque fiche que le parcours conditionne, le couple `(node_id, si)` — 9 fiches pour les 38
étapes du guide, les 29 autres n'en portant aucune. Le **texte** des étapes n'est jamais ingéré : il
n'appartient à aucune fiche, et un bloc citable qui ne serait dans aucune fiche n'aurait pas de source
à afficher (spec 1.1). `server/app/domain/profil.py::noeuds_du_profil` lit ces conditions pour désigner
les fiches qu'un profil rend pertinentes ; *retrouver* leur réserve des places parmi `max_opens`, sans
jamais ouvrir une fiche qui n'était pas déjà candidate. Le check `parcours_ingere` de `report.json`
donne les deux comptes ; une fiche inconnue, un `si` non conforme ou une clé hors `PROFIL_KEYS` lèvent
l'alerte `parcours_condition_ignoree` — jamais un bloquant.
