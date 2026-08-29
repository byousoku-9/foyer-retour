# Harness de questions-témoins

Le harness est `python -m server.evals.run`. Il valide tous les cas, les grilles compagnon et la compatibilité suite/variante avant de construire un éventuel client. Les suites guide et sinistre exigent `ANTHROPIC_API_KEY` et un plafond fini strictement positif. La suite parsing est entièrement locale : elle ne construit aucun client, ne lit aucune clé et coûte zéro. `--dry-run` valide le plan sans clé, corpus, client ni écriture.

```bash
uv run python -m server.evals.run --profile full --max-cost 1.0
uv run python -m server.evals.run --profile full --quick --max-cost 1.0
uv run python -m server.evals.run --suite guide --variant deterministe --max-cost 1.0
ANTHROPIC_API_KEY= uv run python -m server.evals.run --suite parsing --profile full --max-cost 0.01
uv run python -m server.evals.run --help
```

`full` inclut les cas `vertical` et `full`. `--quick` choisit de façon stable le premier identifiant de chaque suite documentaire sélectionnée ; il est incompatible avec `--gate`, car un gate doit porter sur toute sa suite. Le guide accepte `outils` et `deterministe`; le sinistre accepte `outils` et `deterministe` depuis la story 4.2d; le parsing porte la variante `local`. Sans `--variant`, les deux suites documentaires tournent la variante par défaut de leur pipeline, `outils` — ce que la production sert (AD-1, amendement du 25/08/2026) ; `deterministe` reste la baseline de comparaison, demandée explicitement. Un couple incompatible est refusé avant tout appel.

Sur un lot qui mêle plusieurs suites, `--variant` ne peut pas désigner une variante commune quand
les vocabulaires diffèrent. Il faut d'abord sélectionner la suite, par exemple
`--suite guide --variant deterministe` ou `--suite parsing --variant local`.

## Golden set et références

Le profil complet contient 34 cas guide, 16 cas sinistre sur AXA et Baloise, et 11 observations parsing sur les deux contrats. Les cinq YAML `vertical` historiques restent byte-identiques ; le champ optionnel `expected.clarification` est absent de ces cinq témoins et ne change aucun verdict admissible. Les cas guide `full` nomment leur famille : parcours, météo FR/EN/DE, suivi, multilingue, traversée de trois fiches ou hors-guide. Les sinistres couvrent les six robustesses, la perte d'exploitation à domicile, l'ordinateur professionnel en séjour temporaire, le téléphone en vacances, l'exclusion animale `non_couvert`, l'arbitrage habitation/auto/RC et l'acte volontaire attendu `non_couvert`.

`server/evals/reference/utilite-guide.yaml` porte, pour tout cas guide qui déclare un scénario — témoin vertical compris — l'ordre juste, les documents et l'interlocuteur. `server/evals/reference/retraductions.yaml` relie chaque cas multilingue à sa fixture, au test hors ligne, à la section du journal, au résultat, aux écarts et à la réserve de signature. Les compagnons sont validés strictement, figés sur les mêmes octets qui ont été parsés, puis revérifiés. `cases_hash` reste l'autorité unique des seuls YAML de cas, comme le gate le recalcule ; les compagnons ont leur propre `identity.scope.references_digest`, présent seulement sur un run `full` qui contient du guide.

Un cas parsing vit sous `cases/parsing/<doc_id>/`, référence exactement un bloc et une transcription déjà normalisée par l'unique `normalize()`. Bloc absent ou trouvé dans un autre document donne `citation_introuvable` avec `found=false`, texte différent donne `parsing`, égalité donne `bonne_reponse`. Aucun repli inter-document n'est admis. Les trois divergences visuelles Baloise p30/p40/p48 sont conservées rouges ; une miniature entièrement exacte prouve séparément `recall=1.0`.

## Cache et invalidation

Le cache local vaut par défaut `<parent de data>/.cache/evals`; `--cache-dir` le déplace. La clé complète couvre le corps fournisseur exact (modèle, paramètres, outils, schéma et messages) et une namespace canonique : variante, digests pipeline/prompts, modèles, seuils actifs, `source_hash`, `ingest_fingerprint`, `normalize_version` et entrée du cas. Changer une seule composante crée une autre namespace. Un hit ne contacte pas Anthropic, vaut `cost_eur=0` et conserve `cost_eur_original`. Une entrée tronquée, corrompue ou d'une autre version est un incident explicite, jamais un miss silencieux. Chaque écriture utilise un fichier temporaire unique puis `os.replace`.

## Budget et artefacts

Le prochain appel est majoré avant envoi contre ce qui reste de `--max-cost`. Si le majorant dépasse le reste, le run s'arrête sans cet appel. Les cas terminés gardent leur unique label ; les cas non exécutés vivent dans `unexecuted_cases` et ne reçoivent aucun huitième label. Le JSON et le Markdown sont écrits atomiquement même sur cet arrêt, avec `complete=false` et `stop_reason`.

Le défaut de configuration est 1 EUR, tandis qu'un `full` fournisseur porte 50 cas guide/sinistre.
Au plafond métier de 0,10 EUR par requête, le majorant arithmétique à cache froid est donc 5 EUR :
le défaut peut produire un rapport partiel, ce n'est pas une estimation de facture complète.
Toujours faire le dry-run, inspecter ou réchauffer le cache, puis passer explicitement
`--max-cost <euros>` selon le risque accepté. Le quick fournisseur porte trois cas documentaires,
soit un majorant de 0,30 EUR.
Le calibrage ou le changement du défaut appartient aux baselines de la story 4.4 ; la story 4.2
documente ce risque sans modifier le seuil métier ni la configuration.

Les chemins par défaut sont `eval-results.json` et `eval-results.md` à côté de `data/`. Ils se déplacent avec `--output-json` et `--output-markdown`. Le JSON expose par cas label, variante, coûts courant/original, latence et claims. Son objet `identity` fige l'image (digests, modèles, normalisation), les documents et le périmètre exact (profil, quick, suites, cas, variantes et digests de namespace). Le Markdown agrège les sept labels, les variantes, recall, coût moyen, latence p50, `cases_hash` et taux de `ne_tranche_pas`.

## Pytest et CI

`pytest -q` porte par défaut `-m 'not evals'` et reste entièrement hors réseau. Le profil payant est explicite :

```bash
ANTHROPIC_API_KEY=... uv run pytest -m evals -q --evals-max-cost 1.0
```

Cette entrée payante ajoute toujours `--exclude-suite parsing` : elle mesure guide/sinistre et
échoue si l'un de ces cas est rouge. Les divergences parsing ne sont ni masquées ni facturées ; elles
se rejouent par la commande locale dédiée ci-dessus, dont le code 1 est attendu tant que les trois
transcriptions visuelles divergent. Sans clé ou plafond, la collecte refuse avant le premier test.
Sur GitHub, le cache est restauré entre runs. Une pull request avec secret joue `full --quick`;
`main` joue `full`. Sans secret, le résumé GitHub signale explicitement le skip. La table Markdown
est ajoutée à `$GITHUB_STEP_SUMMARY` même si le test live rend un code non nul.

Variables lues par l'unique test live :

| Variable | Rôle | Défaut |
|---|---|---|
| `EVALS_PROFILE` | profil transmis au runner | `full` |
| `EVALS_QUICK` | active `--quick` pour `1`, `true` ou `yes` | désactivé |
| `EVALS_CACHE_DIR` | cache persistant restauré/sauvegardé par la CI | répertoire temporaire du test |
| `EVALS_OUTPUT_JSON` | rapport JSON | fichier temporaire du test |
| `EVALS_OUTPUT_MARKDOWN` | rapport Markdown et résumé GitHub | fichier temporaire du test |

Le plafond n'est volontairement pas une variable implicite : `--evals-max-cost` reste obligatoire
sur la commande pytest. `ANTHROPIC_API_KEY` reste la clé fournisseur explicite. Le cache CI est
sauvegardé même après un échec de mesure, sauf lorsqu'une restauration a trouvé la clé exacte.

Le holdout de 4.3, les baselines publiées de 4.4 et l'API/page d'accueil de 4.5 ne font pas partie de ce livrable.

## Protocole 4.2b : plancher, répétitions, budget de campagne, décisions

Le protocole complet vit dans `docs/evals/protocole.md` ; le harness en applique quatre mécanismes.

**Plancher pré-enregistré.** `server/evals/reference/plancher.yaml` fige par témoin le plancher,
`N`, numérateur, dénominateur et règles d'incident. `server/evals/plancher.py` le charge, refuse
toute diminution du floor 4.2a ou de la règle trusted, et calcule son digest — inscrit dans
`identity.image.plancher_digest` et couvert par `identity.run_digest` (empreinte canonique de
l'identité du run). Sans plancher lisible, aucun run ne part.

**Répétitions sans cache.** `--repeat N` répète chaque cas `N` fois, cache de réponse désarmé :
chaque répétition est payée et publiée en entier (rapport `schema_version: 3` — blocs lus,
claims + applicabilité, `found`/`complete`, coût, latence, tiers/coût par étape, preuves
normalisées `{doc_id, block_id, kind, quote_hash}`). L'agrégat `stability` compare les répétitions :
sinistre = même preuve et même verdict admissible ; guide = même statut, `found`/`complete`, label
et ensemble de fiches. Une répétition manquante (interruption) reste **rouge au dénominateur** et
le rapport partiel est toujours écrit — sur tout incident, budget ou non.

**Budget de campagne.** `LIVE_BUDGET_EUR` (défaut 1,00 €) est agrégé dans un ledger persistant,
verrouillé et identifié par `LIVE_CAMPAIGN_ID`; `--max-cost` reste la borne locale obligatoire de
chaque run orchestrateur. Le client persiste le coût réel après chaque appel, et une invocation
suivante repart de cet `accrued_cost_eur`. Le préflight s'applique à **tout run payant** : il refuse
en code 4, avant le
premier appel, quand le **majorant de référence** (plafond par requête de production × exécutions
payantes — une référence d'estimation, pas une borne du chemin évals où chaque exécution reçoit le
restant du run) dépasse le budget effectif, avec `configured_budget_eur`, `accrued_cost_eur` et
`refused_cost_eur` — jamais une question. Un run simple adossé au cache n'est pas refusé au
préflight : il reste borné par l'arrêt en cours de run et par le budget de campagne du client.

**Décisions chiffrées du gate.** `--gate` n'écrit plus un booléen nu : `Gate.decisions[]` porte
`{metric, producer, threshold, scope, n, run_digest, value, status}`. Le runner mesure
`cases_ok_rate`, `stabilite_guide`/`stabilite_sinistre` et les témoins à `case_id` qui lui
appartiennent. Les mesures trusted externes (tests hors ligne, A16, `decision_claim`) sont injectées
par l'orchestrateur avec `--orchestrator-evidence <json>` ; le runner recalcule leurs seuils et
statuts depuis le plancher, et toute mesure applicable absente reste rouge. Un `builder` reste rouge
même si ses valeurs tiennent les seuils. `evals_ok` est la conjonction. Un gate candidat **rouge ne
remplace jamais** un gate
`evals_ok: true` : le verdict candidat est publié dans le rapport, le manifest reste intact. Sous
gate `full`, des digests pipeline/prompts/modèles non concordants mettent le document en
quarantaine au chargement au lieu d'une simple alerte. `--producer orchestrator` déclare la
provenance ; la règle trusted ne reconnaît que l'orchestrateur comme producteur de preuve. Le JSON
d'évidence porte `plancher_digest` et une liste `decisions` de
`{metric, n, value, run_digest}` — jamais un statut auto-déclaré.

## Story 4.5 — le gate `full` décide, et les résultats sont publiés

**Le profil `full` s'exécute sur un gate, et pas ailleurs.** `--gate {doc_id} --profile full` arme
**dix** témoins de plus ; `--profile full` sans `--gate` — ce que la CI lance à chaque PR — reste ce
qu'il était : un **diagnostic**. Le plancher le dit par témoin (`arme_par: gate_full`), et non le
code : un gate `vertical` n'affirme que deux cas relus à la main, et lui opposer la politique
complète ferait rougir une mesure qui ne la revendique pas.

Trois gardes s'arment alors, **avant tout appel** (code 2, manifest intact) :

- `--repeat >= n_minimum` (3) — une politique complète prouvée sur une exécution ne prouve pas la
  stabilité ;
- `--candidate-revision <40 hex>` — un gate qui ne nomme pas la révision qu'il mesure peut être
  réutilisé par une révision qu'il n'a jamais vue ;
- `--orchestrator-report <rapport.json>` dès que `--orchestrator-evidence` est donné.

**Le périmètre change sous `full`** : la suite `parsing` du document entre dans le lot, à côté de sa
suite documentaire. Un `cases_hash` de gate `full` n'est donc pas celui d'un gate `vertical` du même
document — deux profils, deux périmètres, deux hashes, ce que `cases_hash` existe précisément pour
dire.

**La révision est une valeur complète, jamais un préfixe** (story 4.5, revue B2). `deploy.yml` pose
`GIT_SHA=<sha40>` sur le service ; `GET /api/v1/sante` continue de publier le `sha7` qu'AD-11
promet, mais comme **projection** de cette valeur (`Settings.version_publiee`) — une seule source de
vérité, une forme d'affichage. La comparaison qu'un gate `full` subit au chargement est donc une
identité stricte : comparée à sept caractères, elle ne discriminait que 16⁷ classes, et un gate d'un
**autre** commit partageant le sha7 était servi sans alerte. En production, une révision inconnue,
tronquée ou ambiguë met le document en quarantaine (`gate_perime`) ; hors production, elle ne
conclut rien.

Trois gardes de plus ferment la **liaison à la révision** et la **composition du lot** :

- la révision annoncée doit être celle du checkout (`git rev-parse HEAD`, ou `GIT_SHA` en 40 hex
  quand il n'y a pas de dépôt), sur un arbre sans modification non commise. Un arbre que
  `git status` n'a pas su décrire est traité **comme un arbre sale** : un garde-fou qui ne peut pas
  conclure refuse, il n'affirme pas ;
- le document gaté doit porter une **source sur le disque** : sans elle, la règle `SOURCE_FILES` ne
  dit pas ce qu'il est, aucun des deux témoins de structure ne le compte, et le loader le sert
  pourtant (alerte `source_absente`). C'est l'état d'un checkout frais, où les `source.pdf` sont
  téléchargés au build ;
- le témoin de structure qui couvre X doit être **armé par le lot**. Un document ingéré depuis un PDF
  sans cas `parsing` ne peut prouver ni son extraction ni sa structure ; un document qui n'en est pas
  issu doit être mesuré par la suite qui le sert.

Sans ces gardes, un lot mal composé aurait produit un gate `full` sans aucune exigence de structure.

### Les dix témoins ajoutés

Tous `criticite: bloquant`, `plancher: 1.0`, `n: 3`, et tous dans
`server/evals/reference/plancher.yaml` — jamais en dur. Le mécanisme de fermeture par vacuité les
rend fail-closed : un témoin bloquant applicable qu'un run n'émet pas produit sa décision rouge
`n=0, value=0.0`.

| témoin | scope | mesuré par | rouge quand |
|---|---|---|---|
| `parsing_ok_rate` | `suite:parsing` | `eval_runner` | un cas `parsing` rend `label=parsing` |
| `blocs_attendus_ouverts_rate` | `run` | `eval_runner` | `expected_block_ids ⊄ opened_block_ids` |
| `citations_retrouvees_rate` | `run` | `eval_runner` | une exécution porte `citation_introuvable`, **même si `mode_attendu` le prévoyait** |
| `zero_5xx_technique_rate` | `run` | `eval_runner` | une exécution porte `http >= 500` (branche `TruncatedRead` comprise) |
| `typage_confirme_rate` | `suite:sinistre` | `eval_runner` | une exécution sinistre cite une **clause fondatrice** (`garantie`, `exclusion`) dont `kind_confirmed` est faux |
| `structure_prouvee_rate` | `suite:parsing` | `eval_runner` | un document **issu d'un PDF** n'a pas de `structure_hash` au manifest, un artefact non concordant, un rapport absent/illisible/étranger, ou pas d'attestation `structure_proposee` affirmative rattachée |
| `arbre_prouve_rate` | `suite:guide` | `eval_runner` | un document **non issu d'un PDF** n'a pas de rapport d'ingestion portant une attestation `invariants_arbre` affirmative rattachée (`document_hash` **et** `ingest_fingerprint` de l'entrée du manifest) |
| `stabilite_claim_decisionnelle` | `suite:sinistre` | `eval_runner` | les N répétitions divergent sur le prédicat décisionnel |
| `anti_rustine_pass_rate` | `repo` | `orchestrator` | la garde n'est pas fournie par une preuve trusted |
| `metamorphique_pass_rate` | `repo` | `orchestrator` | idem |

`_signature_stabilite` n'est **pas** touchée : son numérateur est écrit dans le plancher importé
(« la meme preuve {doc_id, block_id, kind, quote_hash} et le meme verdict admissible »). La stabilité
du prédicat décisionnel est un témoin **distinct**, additif, dont le rouge se lit séparément.

**Deux périmètres, deux preuves, aucune branche par document.** `typage_confirme_rate`,
`structure_prouvee_rate` et `arbre_prouve_rate` ne s'appliquent pas au même univers, et c'est ce qui
les rend satisfaisables plutôt que décoratifs :

- le **typage juridique** qu'AD-6 exige est celui des clauses fondatrices, l'univers exact du
  prédicat décisionnel. Le guide n'a aucune clause à typer — aucun de ses blocs ne porte de
  `kind_source` —, et lui opposer ce témoin l'aurait rendu **structurellement rouge** quelle que soit
  la qualité du candidat. Ce que le guide prouve, il le prouve par `citations_retrouvees_rate`,
  `blocs_attendus_ouverts_rate` et sa preuve de structure ;
- la **preuve de structure** se partage par la règle `SOURCE_FILES` du loader (première source
  présente) : `structure_prouvee_rate` pour un document issu d'un PDF — la proposition de structure
  de 4.2c —, `arbre_prouve_rate` pour une copie de site, dont l'ingestion émet le même contrôle
  déterministe d'arbre. Les deux dénominateurs sont **complémentaires** : aucun périmètre servi n'est
  laissé sans exigence de structure, et aucun document n'en subit deux.

**Une attestation, pas une déclaration.** Les deux preuves de structure exigent que le rapport
d'ingestion **affirme** et **rattache** : `structure acceptee document_hash=… structure_hash=…`, ou
`arbre atteste document_hash=… ingest_fingerprint=…`, et le couple doit être celui de l'entrée du
manifest. Un `structure.json` arbitraire, un `report.json` écrit à la main, ou la forme historique
`invariants_arbre: ok` ne verdissent rien. Les attestations sont écrites par les deux ingestions
(`kb_to_blocks`, `pdf_to_blocks`) et **renouvelées** — jamais fabriquées — par le typage, qui réécrit
`report.json` après avoir changé `document.json` : sans ce renouvellement, un document typé perdrait
sa preuve de structure sans que rien ne le dise.

### Le format de preuve trusted, lié à la révision

Le fichier `--orchestrator-evidence` porte **exactement** cinq clés racine — une clé en trop est
refusée, parce qu'un vocabulaire fermé se contrôle par égalité :

```json
{
  "plancher_digest": "<64 hex>",
  "candidate_revision": "<40 hex>",
  "report_digest": "<sha256 des octets de --orchestrator-report>",
  "run_digest": "<64 hex>",
  "decisions": [{"metric": "...", "n": 3, "value": 1.0, "run_digest": "<le même>"}]
}
```

Sept recoupements sont exigés (`server/evals/plancher.py::verifier_liaison_preuve`), et le premier
écart refuse **avant toute décision** — aucun gate n'est écrit :

1. les cinq clés racine, exactement ;
2. `plancher_digest` = celui du plancher chargé par ce run ;
3. `candidate_revision` = celle du run (elle-même recoupée avec la révision réellement exécutée) ;
4. `sha256(octets du rapport)` = `report_digest` ;
5. `identity.run_digest` du rapport référencé = `run_digest` racine = celui de chaque décision — le
   rapport doit **se reconnaître** dans la preuve, sans quoi une preuve pouvait référencer le rapport
   d'un autre run et passer ;
6. `identity.run_digest` **recalculé** depuis l'identité privée de sa propre clé (la définition
   d'`identite_run`) = la valeur annoncée — un `run_digest` cru sur parole n'est qu'une chaîne, et un
   rapport fabriqué peut toujours être auto-cohérent ;
7. `identity.candidate_revision`, `plancher_digest` (racine **et** `identity.image`) et
   `identity.image.{pipeline_digest, prompts_digest, model_ids}` = ceux du run courant — deux runs ne
   se comparent qu'à code, prompts, modèles et protocole égaux.

C'est l'intention M2 : une modification produit rend la réutilisation d'une preuve invalide. Le
`candidate_revision` fait partie de l'identité canonique du run, donc de son `run_digest` : deux runs
de deux commits ne peuvent plus porter la même empreinte. Toute preuve produite avant la story 4.5
est invalide.

### L'artefact de publication et ses quatre surfaces

Un seul objet — `server/app/domain/evals.py::PublicationEvals` — construit par le runner et publié
**inconditionnellement**, rouge compris (FR41). Publier ne promeut rien : seul `gate.evals_ok`
décide de ce qui est servi (AD-8).

**Publication et promotion basculent ensemble** : les trois surfaces *et* l'entrée de manifest sont
préparées dans des temporaires de leurs répertoires cibles, puis basculées en une seule file de
`os.replace`. Il ne reste rien d'échouable après la première bascule, donc aucun état où une surface
affirme un verdict que le manifest ne porte pas — ni l'inverse.

**Et un refus de préparation ne laisse aucun temporaire résiduel.** Tout ce qui peut lever — au
premier rang, la lecture du `docs/evals/latest.md` à archiver — est lu et validé *avant* le premier
temporaire, et la préparation entière est entourée d'un rollback local qui supprime chaque
temporaire déjà accumulé sur toute exception, quel qu'en soit le rang. Les chemins frères
(`ecrire_rapports`, la préparation de l'entrée de manifest) tiennent la même garantie. Sans elle,
des refus répétés remplissaient `data/` de fichiers `.tmp` que plus rien ne nettoyait.

| surface | chemin | dans l'image ? |
|---|---|---|
| artefact machine servi | `data/evals-latest.json` | oui (`COPY data`) |
| rendu lisible du dépôt | `docs/evals/latest.md` | non (`docs/` n'est pas copié) |
| résumé de CI | appendu à `eval-results.md`, concaténé dans `$GITHUB_STEP_SUMMARY` | — |
| API | `GET /api/v1/evals/latest` | oui |
| accueil | `/` sonde la route et compose la vue | oui |

Les **limites** publiées sont *dérivées* du run — décisions rouges chiffrées, réserves à `false`,
exécutions manquantes, écarts de parsing, état incomplet — jamais rédigées. Un artefact absent,
illisible ou hors schéma rend un état typé `publie: false` avec sa raison : jamais 5xx, jamais un
chiffre inventé.

#### La validation canonique du rapport, avant toute surface

`server/evals/publication.py::valider_rapport_publiable` est **la** lecture du rapport : tous les
chemins qui alimentent les quatre surfaces l'appellent — `construire_publication`,
`stabilite_du_rapport`, `limites_du_rapport`, et `run.rendre_markdown` pour le résumé de CI. Aucune
seconde lecture permissive ne subsiste, et plus aucun repli `or 0.0`, `or {}` ou `or 0` ne peut
transformer une donnée invalide en chiffre plausible. Le refus est un `RapportInexploitable` qui
**nomme la clé et sa raison typée**, levé *avant* qu'aucune surface n'affiche quoi que ce soit.

Sont refusés :

| donnée | refus |
|---|---|
| `profile` | absent, non-chaîne, ou vide |
| `complete`, `unexecuted_cases`, `identity`, `results` | absents ou mal typés ; une entrée de `results` non-objet |
| `cost_eur` | absent, `None`, booléen, non numérique ou non fini |
| `metrics.recall`, `.average_cost_eur`, `.cost_p95_eur`, `.ne_tranche_pas_rate` | idem |
| `metrics.latency_p50_ms`, `.latency_p95_ms` | idem, et non entiers (arrondir une latence l'inventerait) |
| `metrics.labels`, `metrics.variants` | absents, `None` ou non-objets — jamais rabattus sur `{}` |
| `stability` présent | sans `cases`, `cases` non-objet, ou `n` absent/non entier ≥ 1 — jamais un `0/0` fabriqué |
| `decisions` | non vides sans `plancher_digest` racine ; ou mal typées ; ou absentes alors que `plancher_digest` est présent |

Ce qui peut **légitimement** manquer reste distinct de ce qui manque à tort : `stability` sous un run
sans répétition (le `repeat` du rapport donne alors le N, et il est exigé), les réserves d'un
diagnostic, et le gate d'un `--profile full` sans `--gate`. Quand un run n'a pris aucune décision, le
rendu l'écrit en toutes lettres au lieu d'une ligne de tableau à zéros.

### La seconde lecture (FR47)

`server/evals/relecture.py` produit le **plan** déterministe (par bloc clé : `doc_id`, `block_id`,
page, bbox, `text_norm`, URL de l'image de page servie par la route de la story 3.4) et **contrôle**
le verdict rempli. Il ne rasterise rien et n'appelle aucun modèle : l'image vient de la route qui la
sert déjà, et le verdict vient de l'orchestrateur.

Le contrôle porte **cinq** liens, et les cinq sont exigés : schéma exact (aucune clé en trop),
`candidate_revision`, `plan_digest`, couverture exacte du plan (chaque bloc une fois), et l'égalité
de chaque `image_sha256` avec l'empreinte **recalculée** des octets réellement regardés. Ce dernier
lien porte tout le poids de FR47 : sans lui, une empreinte inventée était acceptée puis publiée
« seconde lecture concordante » sur les quatre surfaces. `--relecture-verdict` exige donc
`--relecture-images <dossier>` (un fichier par bloc, nommé `{block_id}` dont les `:` sont remplacés
par `_`, suffixé `.png`) ; une image manquante est un refus — un bloc qu'on n'a pas pu regarder ne
peut pas avoir été relu. La commande complète est dans `docs/tests-live.md`, section « Story 4.5 ».
