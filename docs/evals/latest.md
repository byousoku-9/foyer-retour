# Mesure produit 4.2a-bis — bougie N=3 (série `final`, orchestrateur)

> **Avertissement non expert :** les vérités de référence ne sont pas validées par un expert assurance et les contresignatures humaines indiquées comme dues restent absentes.

> **État produit, pas verdict.** Cette mesure est publiée telle quelle, rouge compris. Elle est
> **non bloquante pour le verdict mono-cause de la story 4.2a-bis** ; comme critère de promotion,
> elle appartient **exclusivement au gate 4.5**. Aucune promotion n'est exécutée, et rien ici ne
> prétend le candidat E2E vert.

## Identité du run

- SHA candidat figé : `664431a20eec3a7e637484b1f33346b77f237a4a` (la mesure reste attachée à ce SHA, même si le HEAD de documentation a changé depuis).
- Producteur `orchestrator`, série `final` (`4.2a-bis-bougie-final-664431a`), campagne `20260828-4.2a-bis-664431a`, un **seul** processus `--repeat 3`, sans cache de réponse, profil `vertical`, suite `sinistre`, cas `s-bougie-canape`, variante `deterministe`.
- Transport Anthropic Messages **standard** exclusivement, aucun Batch. Modèles : `micro=claude-haiku-4-5-20251001`, `reason=claude-sonnet-5`, `normalize_version=2`.
- Bornes : `LIVE_BUDGET_EUR=0.5`, `--max-cost 0,2974 €` (le plafond local représente le solde du budget global de la story après les `0,2026 €` de fixture) ; majorant certifié au préflight `0,2970 €`.
- Digests : `run_digest=77f18d8067d1d672ebd58ef59c7c8568c74f0368dc587f03fda184e8280409ff`, `plancher_digest=d6df020a4a07c583483d83b507a6346681ee71cf4c201741471810f8e5b61871`, `cases_hash=0b521262f9d9e4ed5da3923df9985e416f4ab6de6c911e959facf2b36eaa57e0`, `pipeline_digest=a9631378…`.

## Résultat des questions-témoins

Run **complet** — 1/1 cas terminés, 3/3 répétitions exécutées, aucune interruption.

| cases_hash | recall | coût moyen (€) | latence p50 (ms) | ne_tranche_pas |
|---|---:|---:|---:|---:|
| <code>0b521262f9d9e4ed5da3923df9985e416f4ab6de6c911e959facf2b36eaa57e0</code> | 0.6667 | 0.0423 | 17430 | 0.6667 |

| Label | Nombre |
|---|---:|
| <code>bonne_reponse</code> | 2 |
| <code>mauvais_doc</code> | 0 |
| <code>doc_manque</code> | 0 |
| <code>claim_non_soutenu</code> | 1 |
| <code>faux_refus</code> | 0 |
| <code>citation_introuvable</code> | 0 |
| <code>parsing</code> | 0 |

| Répétition | Label | HTTP | Verdict | Preuve (`quote_hash`) | Coût (€) | Latence (ms) |
|---:|---|---:|---|---|---:|---:|
| 1 | <code>claim_non_soutenu</code> | 503 | — (`TruncatedRead`, `reason_kind=lecture_tronquee`) | aucune | 0.0703 | 30439 |
| 2 | <code>bonne_reponse</code> | 200 | <code>ne_tranche_pas</code> | <code>5949edb17c5ed5dbde150083e2e0facb79c406a22595b4ec71cacdd0a87fb46a</code> | 0.0334 | 17430 |
| 3 | <code>bonne_reponse</code> | 200 | <code>ne_tranche_pas</code> | <code>5949edb17c5ed5dbde150083e2e0facb79c406a22595b4ec71cacdd0a87fb46a</code> | 0.0233 | 11020 |

- **2/3 bonnes réponses** (recall 0,6667). Les répétitions 2 et 3 portent la **même** preuve `{doc_id, block_id, kind, quote_hash}` et le même verdict admissible.
- **Stabilité complète : rouge** à cause de la répétition 1 (aucune preuve, aucun verdict, signatures divergentes entre répétitions). Décisions du run : `cases_ok_rate=0,6667` rouge, `stabilite_sinistre=0,0` rouge, `bougie_post_success_rate=0,6667` rouge, `executions_completes=1,0` verte ; les témoins sans mesure applicable dans ce run (`offline_tests_pass_rate`, `a16_post_success_rate`, `decision_claim_rate`) restent rouges par construction.
- Coûts : série `0,1270 €` ; cumul live réel global de la story `0,3296 €` (dont `0,2026 €` de fixture), sous le plafond `0,5000 €`. Latences : 30439 / 17430 / 11020 ms, p50 17430 ms, p95 30439 ms.

La preuve locale parsing de la story 4.2 reste disponible : `python -m server.evals.run --suite parsing --profile full --max-cost 0.01` (résultat local attendu volontairement rouge : 8 `bonne_reponse`, 3 `parsing`).
