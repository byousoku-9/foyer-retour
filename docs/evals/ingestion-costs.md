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
| **Total de la campagne utile** | — | **237** | **1 081 839** | **190 860** | **4,6833 €** |

Les 237 résultats ont tous le statut fournisseur `succeeded`. Aucun token de cache n'a été
facturé. Le coût additionne, requête par requête, les usages rendus par l'API avec les prix de
`server/app/llm/pricing.py` et la remise Batch de 50 % ; les arrondis par requête expliquent qu'il
ne faut pas le recalculer depuis les seuls totaux de tokens arrondis.

Un premier essai de lecture 1, regroupé en 35 requêtes, a rendu 35 réponses fournisseur réussies
mais une réponse omettait deux `block_id`. Le pipeline a refusé toute écriture, comme prévu ; cet
essai a coûté 1,8739 €. Deux essais intermédiaires de schéma ont été refusés avant inférence par
le fournisseur (`minItems` non supporté, puis schéma trop complexe) : usage nul, coût nul. Ces
incidents ne sont pas inclus dans le total de la campagne utile.

Le coût réellement engagé pour obtenir cet artefact est donc **6,5572 € au total** : 1,8739 € pour
la première passe rejetée sans écriture, puis 4,6833 € pour les deux lectures utiles. Les deux refus
de schéma n'ont rien facturé.

Commande reproductible :

```bash
uv run python -m server.ingest.type_clauses axa-lu-optihome-2017 --dry-run
uv run python -m server.ingest.type_clauses axa-lu-optihome-2017
```

Ces commandes conservent volontairement le transport Batch de production. Pour une itération de
développement depuis zéro, `--transport standard --max-cost <euros>` exécute les deux lectures via
Messages standard, sans soumettre ni consulter de lot. Son majorant et son coût réel sont calculés
sans remise Batch ; cette option doit donc rester explicite et plafonnée.

Le dry-run final annonce 1 396 blocs / 140 requêtes par lecture au pire et un majorant de
10,5133 € sous le plafond configuré de 12 €. Ce majorant n'est pas un coût réel.

### Régénération après revue indépendante — 26/08/2026

La correction des portées, de l'ancrage des définitions et des exemples du prompt a invalidé la
campagne précédente. Deux nouveaux lots complets ont donc régénéré l'artefact, sans reprise de
résultats produits avec l'ancien prompt.

| Lecture | Lot | Blocs | Requêtes réussies | Tokens d'entrée | Tokens de sortie | Coût Batch |
|---|---|---:|---:|---:|---:|---:|
| 1 | `msgbatch_01Phnr1EjzxrvF8L2GpQbvLd` | 1 396 | 140 | 666 786 | 132 380 | 3,0556 € |
| 2 | `msgbatch_01CoKSYHfwVGLEDMn36dwyxA` | 961 candidats juridiques | 97 | 413 961 | 52 649 | 1,5575 € |
| **Total de la régénération** | — | — | **237** | **1 080 747** | **185 029** | **4,6131 €** |

Les 237 résultats sont `succeeded`, sans token de cache. Le total est la somme des coûts arrondis
par requête rendue par le CLI. Le dry-run corrigé annonçait 10,5116 € sous le même plafond de 12 €.
Le coût cumulé des essais et des deux campagnes utiles de la story atteint donc **11,1703 €** ; seul
le dernier total de 4,6131 € correspond à l'artefact actuellement servi.
