# Mesure produit 4.2d — comparaison sinistre `deterministe` vs `outils`, N=3 (orchestrateur)

> **Avertissement non expert :** les vérités de référence ne sont pas validées par un expert assurance et les contresignatures humaines indiquées comme dues restent absentes.

> **État produit, pas verdict.** Ces mesures sont publiées telles quelles, rouges compris. Elles sont
> **non bloquantes pour le verdict mono-cause de la story 4.2d** — lequel reste **vert**, prouvé hors
> réseau : la navigation par outils est bien la variante sinistre servie par défaut, `deterministe`
> reste sélectionnable et baseline, le repli est unique, borné et nommé dans la trace. Comme critères
> de promotion, ces rouges appartiennent **exclusivement au gate 4.5**. Aucune promotion n'est
> exécutée, le dernier vert et son trafic sont inchangés, et rien ici ne prétend le candidat E2E vert.

## Identité de la campagne

- SHA candidat figé : `b765e886c46a5f38b52494b3c10dbff83258061f`.
- Campagne `20260828-4.2d-b765e88`, producteur `orchestrator`, profil `vertical`, suite `sinistre`, cas `s-bougie-canape`, deux séries `--repeat 3` **sans cache de réponse** : une baseline `deterministe`, une finale `outils`.
- Transport Anthropic Messages **standard** exclusivement, **aucun Batch**, aucun parallélisme, **aucune correction entre répétitions**.
- Bornes : `LIVE_BUDGET_EUR=0.9`, `--max-cost 0.3` par série ; majorant certifié au préflight `0,3000 €` pour chacune.
- Digests communs aux deux séries : `pipeline_digest=ca04ac18ff38d093daf010dace6efbebcd4c2e64b742c6dc9183c2f0aba74445`, `prompts_digest=87e2393408cb5a35f9343be0472a3fd31500a6e520262d4a30c03730af6e3d32`, `plancher_digest=d6df020a4a07c583483d83b507a6346681ee71cf4c201741471810f8e5b61871`, `cases_hash=0b521262f9d9e4ed5da3923df9985e416f4ab6de6c911e959facf2b36eaa57e0`.
- Les deux séries sont **complètes** : 1/1 cas terminé, 3/3 répétitions exécutées, aucune interruption.

## Baseline `deterministe`

Série `4.2d-baseline-deterministe-b765e88`, `run_digest=a8aa84c5c1c6be723e36c2891861d4190427c62e7e34de16e17a5f0281be625a`.

| Répétition | Label | HTTP | Verdict | Preuve | Coût (€) | Latence (ms) |
|---:|---|---:|---|---|---:|---:|
| 1 | <code>bonne_reponse</code> | 200 | <code>ne_tranche_pas</code> | `p50:18` `exclusion` <code>63ab6213…</code> | 0,0738 | 12 678 |
| 2 | <code>claim_non_soutenu</code> | 503 | — | aucune | 0,0640 | 34 370 |
| 3 | <code>bonne_reponse</code> | 200 | <code>ne_tranche_pas</code> | `p50:18` `exclusion` <code>63ab6213…</code> | 0,0271 | 14 243 |

Agrégat : **2/3** (recall 0,6667), coût de série **0,1649 €** (moyen 0,0550 €, p95 0,0738 €), latence p50 **14 243 ms**, p95 **34 370 ms**. **Stabilité rouge** : signatures divergentes entre répétitions, verdict absent sur la répétition 2.

## Finale `outils` (la variante servie)

Série `4.2d-final-outils-b765e88`, `run_digest=a20f53cd63a224ce239ccd54c94ab2b7b2c397971374a04eaf59fe4b431ddf0b`.

| Répétition | Label | HTTP | Verdict | Preuve | Coût (€) | Latence (ms) |
|---:|---|---:|---|---|---:|---:|
| 1 | <code>claim_non_soutenu</code> | 503 | — | aucune | 0,0138 | 11 156 |
| 2 | <code>bonne_reponse</code> | 200 | <code>ne_tranche_pas</code> | `p34:12` `garantie` <code>2bc6b03a…</code> | 0,0232 | 15 342 |
| 3 | <code>bonne_reponse</code> | 200 | <code>ne_tranche_pas</code> | `p35:1` `garantie` <code>88043521…</code> + `p35:2` `exclusion` <code>095959be…</code> | 0,0333 | 17 853 |

Agrégat : **2/3** (recall 0,6667), coût de série **0,0703 €** (moyen 0,0234 €, p95 0,0333 €), latence p50 **15 342 ms**, p95 **17 853 ms**. **Stabilité rouge**, et pour une raison propre à cette variante : **les preuves divergent** d'une répétition à l'autre (`p34:12`, puis `p35:1`+`p35:2`), là où la baseline reproduisait `p50:18` à l'identique.

## Ce que la comparaison montre

| | `deterministe` (baseline) | `outils` (servie) |
|---|---:|---:|
| Recall | 0,6667 | 0,6667 |
| Coût de série (€) | 0,1649 | **0,0703** |
| Coût moyen (€) | 0,0550 | **0,0234** |
| Coût p95 (€) | 0,0738 | **0,0333** |
| Latence p50 (ms) | **14 243** | 15 342 |
| Latence p95 (ms) | 34 370 | **17 853** |
| Stabilité N/N | rouge | rouge |

La justesse est **identique** (2/3 des deux côtés), donc l'arbitrage n'est pas tranché par elle. À justesse égale, `outils` coûte **moins de la moitié** et resserre nettement la queue de latence. Les deux variantes sont **instables** sur `N=3`, mais pas de la même manière : la baseline échoue par une répétition sans preuve, la variante servie échoue **aussi** parce que ses preuves changent d'une répétition à l'autre. C'est le constat que la story 4.2d demandait de publier, et c'est exactement ce que le plancher `stabilite_sinistre` devra juger au gate 4.5.

Décisions de plancher, identiques sur les deux séries : `executions_completes=1,0` **verte** ; `cases_ok_rate=0,6667`, `stabilite_sinistre=0,0`, `bougie_post_success_rate=0,6667` **rouges**.

## Coûts

Ledger de la campagne `20260828-4.2d-b765e88` : **0,2352 €** pour les deux séries, sous le plafond global `0,9000 €`. Coût live réel global de la story, ré-enregistrement de fixture compris : **0,2501 €**. Aucun autre appel live n'a été lancé pour 4.2d.

La preuve locale parsing de la story 4.2 reste disponible : `python -m server.evals.run --suite parsing --profile full --max-cost 0.01` (résultat local attendu volontairement rouge : 8 `bonne_reponse`, 3 `parsing`).
