# Choix et limites mesurées

## Story 3.6 — second contrat luxembourgeois

Le choix prioritaire est Baloise Luxembourg HOME, plutôt qu'un contrat de repli. La source officielle
est `https://www.baloise.lu/dam/baloise-lu/1890/particulier/documents/CG-CS/CG-HOME-2--LUFR-09-24.pdf`;
ses octets vérifiés donnent le SHA-256
`2c365b0ea59a47ddf86295b0e1ad65a0c23847bcc30db22ec47861b18ba4a5a6`. Le titre publié est
« Baloise Luxembourg HOME » et l'édition imprimée est `CG-HOME(2)-LUFR-09-24`. Le PDF téléchargé
reste ignoré par Git.

La procédure publique commune a été utilisée sans règle conditionnée par Baloise :

```bash
uv run python -m server.ingest.fetch_source baloise-lu-home-2-2024
uv run python -m server.ingest.pdf_to_blocks baloise-lu-home-2-2024 \
  --title "Baloise Luxembourg HOME" --edition "CG-HOME(2)-LUFR-09-24"
uv run python -m server.ingest.type_clauses baloise-lu-home-2-2024 \
  --transport standard --max-cost 10 --dry-run
uv run python -m server.ingest.type_clauses baloise-lu-home-2-2024 \
  --transport standard --max-cost 10
```

Le dry-run, joué avant le premier appel, affichait 69 requêtes de lecture 1 et au plus 69 de lecture
2, pour un majorant de 9,7482 EUR. L'unique campagne payante a utilisé Messages standard : 69 appels
T1 et 51 appels T2, aucun Batch, coût réel 4,2518 EUR sous le plafond de 10 EUR. Elle étiquette 513
blocs, dont 510 juridiques et 453 confirmés par deux lectures. Aucun gate n'a été lancé dans cette
story : Baloise est donc servi dans le fixture de développement qui autorise les documents sans gate,
mais mis en quarantaine `sans_gate` par le chargement de production.

### Écarts conservés

Le rapport couvre 48 pages sur 48, 692 blocs et 11 tables, sans bloquant. Il conserve les alertes
suivantes : deux blocs préliminaires non citables, numéros de la table des matières absents de
l'arbre, définitions sans cible résolue, exclusions sans marqueur, confiance de typage faible et 57
kinds juridiques non confirmés.

Le PDF montre des titres visuels numérotés et lettrés dans deux colonnes. Le parseur mesure pourtant
zéro numéro d'article et ne construit que la racine et le nœud de table des matières. Le texte reste
citable, mais la hiérarchie est plate et certains groupes reflètent l'ordre des colonnes. Étendre la
regex sur ce seul contrat aurait inventé des portées : aucune règle de structure n'a donc été ajoutée.

Le contrat fournit zéro page OCR, zéro page non française, zéro page de charabia et aucune image de
page. Les différés de calibration langue/OCR et de table rasterisée sont donc requalifiés sans
correctif : ce corpus n'apporte pas le signal requis. Les rapports sérialisés font 10 257 octets pour
Baloise et 12 135 pour AXA ; le second contrat ne relève pas le maximum observé, donc ni cache HTTP ni
pré-sérialisation supplémentaire n'est justifié sous la concurrence de démonstration actuelle.

### Relectures et témoins

L'agent a vérifié visuellement les pages 3, 10, 20, 30, 40 et 48 contre `document.json`, puis les
passages relatifs à la brûlure de cigarette, aux denrées en congélateur et à la responsabilité civile
vie privée. Trois témoins documentaires isolés sont versionnés : bougie/canapé, congélateur et
invité/cigarette. Leur attendu prudent accepte `sous_conditions` ou `ne_tranche_pas`; leur exécution
reste volontairement non faite car elle constituerait un gate payant.

Cette relecture est une inspection agent, pas une validation assurance. La relecture par Lancelot et
l'expertise restent toutes deux dues; les YAML portent `countersigned_by: null` et
`validated_by_expert: false`.
