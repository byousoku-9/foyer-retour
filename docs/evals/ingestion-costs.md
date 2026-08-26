# Coûts d'ingestion

Les montants de ce fichier sont des coûts facturés calculés depuis `usage` rendu par l'API, convertis
avec `USD_EUR` et la remise Batch de 50 %. Le majorant de `--dry-run` est un garde-fou avant appel ;
il n'est jamais consigné comme coût réel.

## Typage des clauses AXA — 26/08/2026

Modèle : `claude-opus-5`. Taux de conversion configuré : `USD_EUR=0.92`.

| Lecture | Blocs | Requêtes réussies | Tokens d'entrée | Tokens de sortie | Coût Batch |
|---|---:|---:|---:|---:|---:|
| 1 — tous les blocs citables | 1 396 | 140 | 667 626 | 134 622 | 3,0836 € |
| 2 — candidats juridiques de la lecture 1 | 970 | 97 | 414 213 | 56 238 | 1,5997 € |
| **Total de la campagne réussie** | — | **237** | **1 081 839** | **190 860** | **4,6833 €** |

Les 237 résultats ont tous le statut fournisseur `succeeded`. Aucun token de cache n'a été
facturé. Le coût additionne, requête par requête, les usages rendus par l'API avec les prix de
`server/app/llm/pricing.py` et la remise Batch de 50 % ; les arrondis par requête expliquent qu'il
ne faut pas le recalculer depuis les seuls totaux de tokens arrondis.

Un premier essai de lecture 1, regroupé en 35 requêtes, a rendu 35 réponses fournisseur réussies
mais une réponse omettait deux `block_id`. Le pipeline a refusé toute écriture, comme prévu ; cet
essai a coûté 1,8739 €. Deux essais intermédiaires de schéma ont été refusés avant inférence par
le fournisseur (`minItems` non supporté, puis schéma trop complexe) : usage nul, coût nul. Ces
incidents ne sont pas inclus dans le total de la campagne réussie.

Commande reproductible :

```bash
uv run python -m server.ingest.type_clauses axa-lu-optihome-2017 --dry-run
uv run python -m server.ingest.type_clauses axa-lu-optihome-2017
```

Le dry-run final annonce 1 396 blocs / 140 requêtes par lecture au pire et un majorant de
10,5133 € sous le plafond configuré de 12 €. Ce majorant n'est pas un coût réel.
