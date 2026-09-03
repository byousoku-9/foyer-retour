# Baselines retrieval du guide

Run **partiel** — budget global 1.0000 €.

Défaut servi au départ : `outils/micro` (prompt_cache=true).

| Cellule | État | Bonne fiche / recall | citation_introuvable | Coût moyen (€) | Latence p50 (ms) |
|---|---|---:|---:|---:|---:|
| `deterministe/reason` | `not_run` | — | — | — | — |
| `deterministe/micro` | `not_run` | — | — | — | — |
| `outils/reason` | `not_run` | — | — | — | — |
| `outils/micro` | `not_run` | — | — | — | — |
| `full_context/reason` | `not_run` | — | — | — | — |
| `full_context/micro` | `not_run` | — | — | — | — |

## Comparaison sinistre 4.2d (archive close)

Référence 2026-08-28 : `deterministe` a8aa84c5c1c6be723e36c2891861d4190427c62e7e34de16e17a5f0281be625a, `outils` a20f53cd63a224ce239ccd54c94ab2b7b2c397971374a04eaf59fe4b431ddf0b.

Comparaison **close** : preuve structurée absente ; rejeu orchestré requis, et les deux variantes qu'elle mesurait (`deterministe`, `outils`) ont été retirées le 03/09/2026 — aucun rejeu ne peut plus les produire. Les chiffres de référence ci-dessus restent visibles comme archive ; aucune mesure courante n'est reprise, et la question qu'ils tranchaient est reprise par la matrice ci-dessus, sur `navigation` et `full_context`.

Aucune recommandation : preuve non trusted, cellules non comparables ou aucune domination triple.

Règle : si une baseline gagne sur recall, coût et latence, elle devient la variante par défaut.
