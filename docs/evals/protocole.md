# Protocole d'évaluation (story 4.2b) — public avant toute mesure

Ce document pré-enregistre le protocole : ce qui sera mesuré, contre quel plancher, sur quels
splits, avec quel budget et par qui — **avant** que la première mesure parte. Le plancher
lui-même est machine-lisible dans `server/evals/reference/plancher.yaml`, figé par digest
(`plancher_digest` dans l'identité de chaque run) : deux runs ne se comparent qu'à protocole égal.

## Splits A/B/C

| Split | Rôle | Localisation | Règles |
|---|---|---|---|
| A | développement | `server/evals/cases/` | public, exécuté librement, publié |
| B | holdout scellé | `~/foyer-retour-holdout/B/` (hors dépôt) | **jamais lu ni matérialisé par le builder** ; un seul verdict B par candidat ; publié avec ses résultats au run final |
| C | réserve | hors dépôt | **jamais exécuté** |

Risque accepté et documenté : le split B vit sur le poste de travail, hors contrôle
cryptographique — un contournement délibéré resterait possible. La règle organisationnelle (un
verdict B par candidat, tiré par l'orchestrateur) est la protection retenue pour ce projet.

## Plancher

Le plancher importe **sans diminution** :

- le floor 4.2a (`n_minimum: 3` ; `offline_tests_pass_rate`, `bougie_post_success_rate`,
  `a16_post_success_rate`, `decision_claim_rate` à `1.0`) — A16 est aujourd'hui **0/3 : rouge,
  publié tel quel**, jamais retiré du plancher ;
- la règle trusted (`automation/plancher.yaml` du dépôt parent) : producteur de preuve =
  **orchestrateur** (un run de builder est un diagnostic, jamais une preuve), 1 série baseline +
  1 série finale par témoin nommé, `LIVE_BUDGET_EUR` défaut **0,50 €**, refus de budget = rouge
  chiffré sans question humaine.

Chaque témoin porte plancher, `N >= 3`, numérateur, dénominateur et règle d'incident. Règles
transverses : `aucun_admissible` et **tout run interrompu sont rouges**, interruptions au
dénominateur. Abaisser un seuil ou retirer un témoin est refusé au chargement
(`server/evals/plancher.py`), donc avant tout appel.

## Stabilité (répétitions sans cache)

`--repeat 3` (au moins), cache de réponse désarmé, publication complète par répétition.

- **Sinistre** : « même claim » ⇔ même preuve `{doc_id, block_id, kind, quote_hash}` **et** même
  verdict admissible. `quote_hash` = SHA-256 du segment relu (`Block.text_norm[start:end]`)
  préfixé de `normalize_version` : stable entre répétitions à corpus égal, insensible aux
  `claim_id` refaits à chaque appel.
- **Guide** : même statut (`reason.kind`), `found`/`complete`, label et ensemble de fiches citées.

Tout écart ⇒ cas instable, rouge au plancher `stabilite` ; la dispersion (signatures, coûts,
latences par répétition) est publiée, jamais masquée.

## Budgets

- Budget effectif d'un run : `min(--max-cost, LIVE_BUDGET_EUR)` ; `LIVE_BUDGET_EUR` défaut 0,50 €
  (`server/app/config.py::live_budget_eur`).
- **Préflight** : majorant estimé de la campagne (exécutions payantes × majorant par requête
  d'AD-9) refusé **avant le premier appel** quand il dépasse le budget effectif — code de sortie
  4, chiffres publiés (`configured_budget_eur`, `accrued_cost_eur`, `refused_cost_eur`), acquis
  conservés.
- **En campagne** : le runner s'arrête avant l'exécution qui déborderait ; le client
  (`LlmClient.campaign_budget_eur`) refuse l'appel qui déborderait. Les deux publient les mêmes
  trois chiffres. Jamais une question humaine.
- Divergence documentée : la prose d'`epics.md` mentionne 1,00 € ; la règle trusted dit **0,50 €**
  — **la règle trusted fait foi**. Relever un plafond exige la mesure de distribution que l'AC
  demande (p50/p95/max publiés par le rapport), jamais un ajustement silencieux.

## Matrice baseline (série A, orchestrateur)

Comparées : uniquement des variantes **existantes**.

| Axe | Valeurs |
|---|---|
| variante guide | `outils` (défaut), `deterministe` |
| variante sinistre | `deterministe` |
| tier par étape (`COMPRENDRE_TIER`, `REDIGER_TIER`, `VERIFIER_TIER`) | `micro`, `reason` |
| `RELANCE_SUR_NON_PERTINENCE` | `0`, `1` |

`full_context` n'existe pas et n'est pas simulé. Chaque point de la matrice est un run
`--repeat 3` à paramètres épinglés (les tiers actifs entrent dans `thresholds()`, donc dans
l'identité de cache et le `run_digest`). La série est **tirée par l'orchestrateur après
passation, sur HEAD figé — jamais par le builder** ; les résultats sont consignés dans
`docs/evals/latest.md` et `docs/tests-live.md` quel que soit le résultat.

## Décisions et promotion

Le gate d'un document porte des **décisions chiffrées**
`{metric, producer, threshold, scope, n, run_digest, value, status}` — jamais un booléen écrasé.
Un candidat rouge **ne mute jamais** le dernier vert : gate, révision et trafic du dernier vert
restent intacts ; le verdict candidat est publié dans le rapport avec raison et limites. Sous gate
`full`, des digests non concordants mettent le document en **quarantaine**. Le checkpoint de
finalisation a trois rôles : la règle mécanique (`server/evals/plancher.py::classer_configurations`
— admissibles au plancher, puis moins chère, puis plus rapide), un architecte neuf, et un relecteur
Claude Code sur abonnement (JSON accepte/rejette) ; un rejet ou une sortie mal formée conserve la
configuration précédente et rend la story suivante non promouvable — sans intervention humaine.

## Interdits

- Exécuter le split C ; lire ou matérialiser le split B côté builder.
- Coder un `block_id`, une page, un doc, une question ou un assureur particulier dans
  runtime/prompts/config (garde bloquante : `tests/test_anti_rustine.py`, allowlist juridique
  versionnée `server/evals/reference/allowlist-juridique.yaml` — règle générique + ≥ 2 cas
  indépendants).
- Toute campagne live directe par le builder.
