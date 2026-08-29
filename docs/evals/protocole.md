# Protocole d'évaluation (story 4.2b) — public avant toute mesure

Ce document pré-enregistre le protocole : ce qui sera mesuré, contre quel plancher, sur quels
splits, avec quel budget et par qui — **avant** que la première mesure parte. Le plancher
lui-même est machine-lisible dans `server/evals/reference/plancher.yaml`, figé par digest
(`plancher_digest` dans l'identité de chaque run) : deux runs ne se comparent qu'à protocole égal.

## Splits A/B/C

| Split | Rôle | Localisation | Règles |
|---|---|---|---|
| A | développement | `server/evals/cases/` | public, exécuté librement, publié |
| B | holdout scellé | `~/foyer-retour-holdout/B/` (hors dépôt) | **jamais lu ni matérialisé par le builder** ; un seul verdict B par candidat ; la promotion ne publie **ni questions, ni réponses brutes, ni détail par cas** — uniquement le verdict et les agrégats ; création et scellement (`cases_hash` figé avant tout réglage) : action orchestrateur |
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
  1 série finale par témoin nommé, `LIVE_BUDGET_EUR` défaut **1,00 €**, refus de budget = rouge
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

Les cas de la suite `parsing` sont **exclus des métriques** `stabilite_*` : la suite est locale
et déterministe, sa « stabilité » ne mesure rien du modèle. L'exclusion est explicite dans le
rapport (`stability.exclusions`, `comptabilise: false` par cas) — leur détail reste publié.

**Frontière des gardes métamorphiques** (`tests/test_metamorphique.py`) : elles prouvent, hors
réseau, que les **décisions déterministes** (table du verdict, applicabilité, corroboration
lexicale, empreintes de preuve) sont invariantes sous permutation d'identifiants, d'ordre et de
formulations sur corpus synthétiques neutres. Les étages **LLM** — rappel, prompts, rédaction —
ne sont pas couverts par ces gardes : leur robustesse au changement de formulation est mesurée
par les témoins live (familles de formulations du golden set, série baseline).

## Budgets

- `LIVE_BUDGET_EUR` (défaut **1,00 €**) est le plafond global agrégé de la campagne identifiée par
  `LIVE_CAMPAIGN_ID`. Le ledger sous `.cache/evals/campaigns/`, verrouillé entre processus, conserve
  le coût fournisseur réel entre invocations. Une nouvelle invocation relit donc
  `accrued_cost_eur` ; elle ne réinitialise ni le cumul ni le compteur de séries.
- `--max-cost` est obligatoire sous `--producer orchestrator` et reste une borne **locale par run**,
  distincte. Le budget effectif du run est le minimum de cette borne et du reste global.
- **Préflight** (tout run payant, cache froid compris) : refus **avant le
  premier appel** quand le **majorant de référence** — plafond par requête de production (AD-9)
  × exécutions payantes — dépasse le budget effectif. Code de sortie 4, chiffres publiés
  (`configured_budget_eur`, `accrued_cost_eur`, `refused_cost_eur`). Ce majorant est une
  référence, pas une borne du chemin évals : une exécution y reçoit le restant du plafond de run
  (AD-9) et peut coûter jusqu'à ce restant — c'est le budget effectif qui borne. Un run simple
  adossé à un cache froid est donc refusé lui aussi quand son majorant dépasse une borne.
- **En campagne** : le runner s'arrête avant l'exécution qui déborderait ; le client refuse
  l'appel qui déborderait. Les deux publient les mêmes trois chiffres. Jamais une question
  humaine.
- `automation/plancher.yaml`, `epics.md`, la copie figée produit et ce protocole convergent à
  **1,00 €**. Relever ce plafond exige la distribution p50/p95/max, jamais un ajustement silencieux.

## Matrice baseline (série A, orchestrateur)

Comparées : uniquement des variantes **existantes**.

| Axe | Valeurs |
|---|---|
| variante guide | `outils` (défaut), `deterministe` |
| variante sinistre | `outils` (défaut), `deterministe` |
| tier par étape (`COMPRENDRE_TIER`, `REDIGER_TIER`, `VERIFIER_TIER`) | `micro`, `reason` |
| `RELANCE_SUR_NON_PERTINENCE` | `0`, `1` |

`full_context` n'existe pas et n'est pas simulé. Les points d'un même témoin partagent une seule
identité `--series-id` et une seule phase `--series-kind baseline`; le ledger autorise au plus un
identifiant baseline puis un identifiant final par témoin, tout en agrégeant les coûts de chaque
invocation. Chaque point utilise `--repeat 3` à paramètres épinglés. La série est **tirée après
passation, sur HEAD figé — jamais par le builder** ; les résultats sont consignés dans
`docs/evals/latest.md` et `docs/tests-live.md` quel que soit le résultat.

## Décisions et promotion

Le gate d'un document porte des **décisions chiffrées**
`{metric, producer, threshold, scope, n, run_digest, value, status}` — jamais un booléen écrasé.
Un run builder est toujours diagnostique et rouge. Pour un run orchestrateur, les témoins que le
runner ne mesure pas lui-même (tests hors ligne, A16, `decision_claim`) arrivent par
`--orchestrator-evidence <json>` ; le code dérive leur statut du plancher et garde rouge toute
preuve applicable absente. Le fichier porte le digest du plancher et seulement les mesures
`{metric, n, value, run_digest}` : il ne choisit ni seuil ni statut.
Un candidat rouge **ne mute jamais** le dernier vert : gate, révision et trafic du dernier vert
restent intacts ; le verdict candidat est publié dans le rapport avec raison et limites. Sous gate
`full`, des digests non concordants mettent le document en **quarantaine**. Le checkpoint de
finalisation a trois rôles : la règle mécanique (`server/evals/plancher.py::classer_configurations`
— admissibles au plancher, puis moins chère, puis plus rapide, sur des rapports dont elle recalcule
l'identité), un architecte neuf, et un relecteur
Claude Code sur abonnement (JSON accepte/rejette) ; un rejet ou une sortie mal formée conserve la
configuration précédente et rend la story suivante non promouvable — sans intervention humaine.

### Un classement n'accepte que des rapports, et recalcule leurs empreintes

`classer_configurations(candidats, *, candidate_revision)` ne prend
plus de `Configuration` déclaratives : chaque `CandidatClassement` porte un **nom** et les **octets**
de son rapport, et rien d'autre. Ce que l'appelant annonçait — admissibilité, coût, latence,
`candidate_revision`, `run_digest`, `report_digest` — est désormais **lu ou recalculé** :

| champ rendu | d'où il vient |
|---|---|
| `report_digest` | `sha256` des octets reçus |
| `run_digest` | `empreinte_canonique(identity privée de sa propre clé)`, recalculée puis opposée à celle que le rapport annonce |
| `candidate_revision` | `identity.candidate_revision` du rapport, qui doit égaler celle du classement |
| `admissible` | `complete`, `unexecuted_cases` vide, et toutes les décisions `green` d'un producteur orchestrateur |

S'y ajoutent, avant tout tri, les recoupements de `verifier_identite_externe` : `plancher_digest`
racine et les **cinq** champs de l'image (`pipeline_digest`, `prompts_digest`, `model_ids`,
`normalize_version`, `plancher_digest`). Le premier écart lève `ClassementInvalide` — jamais un tri
dégradé, jamais une tête par défaut ; la CLI le rend en code 2 sans imprimer de classement. Deux
candidats homonymes ferment aussi : un artefact de promotion où l'on ne sait pas lequel a été promu
n'est pas auditable. Le classement matérialise enfin son argument avant de le parcourir deux fois :
un générateur épuisé aurait rendu un classement vide, donc `aucun_admissible` sur des candidats
réels.

**Les deux références sont ancrées, pas reçues.** `classer_configurations` dérive elle-même le
`plancher_digest` (`charger_plancher()`) et l'image (`image_du_depot()`, miroir de
`run.image_du_run`) du processus qui l'exécute. Il n'existe **aucun paramètre** pour les remplacer,
ni de voie « pour les tests ».

**Et la révision candidate est opposée au checkout réellement exécuté.** `candidate_revision` reste
un paramètre parce que c'est la **question** posée — « classe les configurations de ce commit » —,
mais elle n'est plus crue sur parole. Le texte précédent affirmait qu'« un commit fantôme ne rend
aucune configuration » : c'était faux. Un rapport auto-cohérent fabriqué pour `'a' * 40`, révision
qui n'existe dans aucun dépôt, était accepté, admissible, et classé **en tête** — la révision
n'était opposée qu'à ce que le rapport en disait, et une auto-cohérence n'est pas une vérité.

Le classement interroge donc `server/evals/revision.py::revision_executee`, l'autorité **unique**
que le runner emploie déjà pour `--candidate-revision` (`git rev-parse HEAD`, repli `GIT_SHA`
seulement en l'absence de dépôt). Trois refus, et aucun n'est optionnel :

| ce qui ferme | pourquoi |
|---|---|
| aucune révision établissable | un classement qui ne sait pas quel code il regarde ne promeut rien |
| état de l'arbre non lisible (`ARBRE_NON_VERIFIABLE`) | ne pas pouvoir conclure, c'est refuser |
| révision demandée ≠ révision du checkout | un rapport peut se réclamer d'un commit qui n'existe nulle part, un classement ne le peut pas |

Ce que ce contrôle **ne** fait pas, et il vaut mieux le lire que le supposer : il ne refuse pas un
arbre de travail **sale**. Ce qu'un classement peut établir est que la révision nommée est celle que
ce checkout porte ; la propreté de l'arbre au moment du classement ne dit rien de l'arbre qui a
produit les rapports — c'est la **production** du rapport qui la contrôle (`run._main` refuse un gate
`full` sur un arbre sale), et l'image opposée à chaque rapport (`pipeline_digest`, `prompts_digest`)
ferme dès qu'une source mesurée a bougé.

`Configuration` est le **résultat**, pas l'entrée : ses trois empreintes y sont obligatoires et bien
formées, et leur valeur vient du recalcul, jamais d'un appelant. Il n'y a plus de garde d'identité
après la construction — toutes les empreintes viennent d'être recalculées et la révision est la même
pour toutes par construction, donc aucune branche d'un tel garde ne serait atteignable, et un
contrôle qu'aucun chemin ne peut faire échouer donne l'apparence d'une garantie sans en être une.

**Le classement rend son identité avec son ordre, et la CLI n'en relit aucune.** Il rend un
`ClassementOppose` — `candidate_revision`, `plancher_digest`, `configurations`. La CLI chargeait
jusqu'ici le plancher pour son propre compte **avant** le classement, puis publiait ce premier
digest dans l'artefact de promotion, alors que le classement dérive le sien du processus : deux
lectures, donc deux valeurs possibles, et c'est la seconde qui décidait pendant que la première
était publiée. L'artefact porte désormais la référence **effectivement opposée**, à la racine comme
par configuration ; il sort en code 0, en code 1 quand aucune configuration n'est admissible — un
rouge publié, jamais une question —, et en code 2 sur un refus de classer.

Quatre couches ont été nécessaires, et les trois premières ne suffisaient pas : la voie CLI
(`--candidate-revision` requis), puis la présence et la **syntaxe** des trois empreintes, puis leur
**recalcul** depuis les octets. Le recalcul ne prouvait encore que la cohérence interne du rapport :
un appelant qui fabriquait un rapport auto-cohérent — `empreinte_canonique` est publique — et
passait le même `plancher_digest` et la même `image_courante` obtenait toujours une tête
`admissible: true`, sur un plancher et une image qui n'existent nulle part. Ancrer les références au
processus a fermé cette couche-là ; il en restait une, la dernière et la seule qui sorte du
processus : rien n'obligeait encore la révision candidate à **correspondre à un commit qui existe**.
L'opposer au checkout réellement exécuté est ce qui ferme la cause. Reproduction :
`pytest -q tests/test_plancher.py -k "classement or checkout"`.

## Interdits

- Exécuter le split C ; lire ou matérialiser le split B côté builder.
- Coder un `block_id`, une page, un doc, une question ou un assureur particulier dans
  runtime/prompts/config (garde bloquante : `tests/test_anti_rustine.py`, allowlist juridique
  versionnée `server/evals/reference/allowlist-juridique.yaml` — règle générique + ≥ 2 cas
  indépendants).
- Toute campagne live directe par le builder.

## Story 4.5 — ce que le gate `full` exige, et ce qui est publié

Le profil `full` ne s'arme que sur `--gate {doc_id} --profile full`. Il ajoute **dix témoins
bloquants** au plancher (`parsing_ok_rate`, `blocs_attendus_ouverts_rate`,
`citations_retrouvees_rate`, `zero_5xx_technique_rate`, `typage_confirme_rate`,
`structure_prouvee_rate`, `arbre_prouve_rate`, `stabilite_claim_decisionnelle`,
`anti_rustine_pass_rate`, `metamorphique_pass_rate`), tous à `plancher: 1.0` et `n: 3`, tous marqués
`arme_par: gate_full`. Aucun n'abaisse ni ne retire un témoin importé, et les digests des snapshots
figés sont inchangés.

**Chaque périmètre servi a son exigence de structure, et aucun n'en a deux.** La règle est celle du
loader (`SOURCE_FILES`, première source présente), jamais un `doc_id` : un document issu d'un PDF
prouve sa proposition de structure (4.2c, `structure_prouvee_rate`) ; un document qui n'en est pas
issu — la copie de site — prouve l'arbre que son ingestion a construit (`arbre_prouve_rate`). De
même, `typage_confirme_rate` ne porte que sur les clauses fondatrices du parcours sinistre, l'univers
exact qu'AD-6 vise : l'opposer au guide, dont aucun bloc ne porte de `kind_source`, aurait été un mur
et non une exigence.

Une preuve de structure est une **attestation rattachée**, pas une déclaration : le rapport
d'ingestion nomme le `document_hash` (et le `structure_hash` ou l'`ingest_fingerprint`) de l'entrée
du manifest. Un artefact arbitraire ou un rapport écrit à la main ne verdit rien. Les deux ingestions
l'écrivent ; le typage la **renouvelle** après avoir changé `document.json`, mais n'en fabrique
jamais.

Six refus **avant tout appel** (code 2, manifest intact) : `--repeat < n_minimum`,
`--candidate-revision` absente ou mal formée, une **révision annoncée qui n'est pas celle réellement
exécutée** — ou un arbre de travail sale, ou un arbre que `git status` n'a pas su décrire, les trois
étant traités pareil —, `--orchestrator-report` absent alors qu'une preuve trusted est fournie, un
document **sans source sur le disque** (rien ne dit alors ce qu'il est ni quelle preuve de structure
lui opposer ; le refus nomme `server.ingest.fetch_source`), et un **lot mal composé** — celui dont le
témoin de structure couvrant le document gaté n'est pas armé (un document PDF sans cas `parsing`, ou
un document mesuré par une suite qui n'oppose pas sa preuve). Sous `full`, la suite `parsing` du
document entre dans le lot : le `cases_hash` d'un gate `full` diffère de celui d'un gate `vertical`
du même document.

La preuve trusted porte **exactement** `{plancher_digest, candidate_revision, report_digest,
run_digest, decisions}`, et le rapport qu'elle référence doit **se reconnaître** en elle. Sept
recoupements précèdent toute décision : les cinq clés racine, le plancher, la révision candidate
(elle-même comparée à la révision réellement exécutée), les octets du rapport, l'égalité du
`run_digest` racine avec celui du rapport et de chaque mesure, le **recalcul** de ce `run_digest`
depuis l'identité du rapport, et l'égalité de l'image mesurée (`pipeline_digest`, `prompts_digest`,
`model_ids`, `plancher_digest`). Un écart refuse le run sans écrire de gate — un `run_digest` cru sur
parole n'est qu'une chaîne, et un rapport fabriqué peut toujours être auto-cohérent (reproduction :
`pytest -q tests/test_plancher.py -k candidate_revision`). Toute preuve produite avant cette story
est invalide.

**Publication et promotion basculent ensemble, et c'est désormais littéralement vrai** (story 4.5,
B7). Ni « publier puis promouvoir » ni l'inverse ne suffisent : dans un sens un échec d'écriture du
manifest laisse des surfaces affirmant un verdict que le manifest ne porte pas, dans l'autre un échec
de publication laisse un vert déjà servable. Les tours précédents avaient répondu par une file de
`os.replace` suivie d'une **restauration** en cas d'échec — ce qui améliorait le *signalement* d'un
état mêlé (`BasculePartielle` nommait les cibles laissées dans le nouvel état) sans l'empêcher : une
interruption pendant la restauration laissait la cible du premier rang publiée.

La règle est maintenant qu'**aucune cible n'est modifiée du tout tant que la transaction n'est pas
acquise**, et l'acquisition est un **unique `os.replace`**. Le lot — les surfaces publiées *et*
`data/manifest.json`, remis au même appel — est écrit dans une génération inactive du bundle
`data/.publie/`, puis publié en déplaçant le pointeur `data/.publie/courant`. Chaque cible est un
chemin dont la résolution traverse ce pointeur ; les liens sont **statiques et committés** pour
`data/` et `docs/`, posés par la CI pour `.evals/`, et la bascule n'en crée, n'en migre ni n'en change
jamais le type — elle refuse ce que le pointeur ne résout pas. Cette forme sert AD-14 (la publication
des questions-témoins) et AD-8 (« seul `gate.evals_ok` décide de ce qui est servi ») : promouvoir et
publier ne peuvent plus devenir visibles séparément.

Ce que le protocole garantit : après **toute** exception, à n'importe quel rang, `BaseException` et
interruption comprises, zéro cible du lot n'est modifiée ni visible dans le nouvel état, et aucun
temporaire ne subsiste. Il n'y a plus de restauration, donc plus rien qui puisse échouer en
défaisant.

### Où est le point de commit, et ce qui est garanti de part et d'autre

Le point de commit est **l'unique `os.replace` du pointeur** `data/.publie/courant`. Avant lui, rien
de ce qui a été écrit n'est une cible : tout vit dans la génération inactive. Après lui, le lot est
publié — et **aucune exception n'est propagée une fois qu'il a eu lieu**. C'est une garantie à part
entière, parce qu'une exception remontée après un pointeur effectivement remplacé rendrait « le lot
est publié » et « l'opération a échoué » vrais en même temps. Deux conséquences, écrites dans le
code :

- le chemin de nettoyage décide sur **l'état réel du pointeur sur disque**, jamais sur un drapeau
  Python qu'une interruption peut couper à la frontière d'instruction qui suit le remplacement. Il
  ne supprime donc jamais la génération devenue active — ce qui rendrait les cibles pendantes, une
  destruction et non un état mêlé ;
- le `fsync` du répertoire de l'espace, qui suit le commit, est **absorbé** s'il échoue : un `fsync`
  raté coûte de la durabilité après coupure, jamais l'atomicité. Une interruption arrivée après le
  commit est absorbée pour la même raison — la transaction est acquise, et il n'y a rien à annuler.

**L'opération de production entière n'a qu'un seul commit.** Un run de gate préparait le couple
`eval-results.*` dans un premier atome, puis le gate, la publication et le manifest dans un second :
chaque atome était tout-ou-rien, l'opération ne l'était pas, et un échec du second laissait le
premier lot déjà changé. Le rapport, sa table, les surfaces publiées et `data/manifest.json` sont
donc préparés **depuis les mêmes octets en mémoire** — `gate.report_digest` est l'empreinte des
octets que ce commit publiera, plus celle d'un fichier déjà écrit — et remis en une seule fois.

Ce qui doit rester distinct le reste, et se décide **avant** le commit : un rapport inexploitable est
refusé sans qu'aucune surface bouge (code 1), un incident technique sort en code 3 sans gate écrit,
et la non-mutation du dernier vert retire le manifest du lot sans en retirer la publication.

### Ce que le protocole ne garantit pas

Ce qui s'écrit au lieu de se taire : un `SIGKILL` ou une coupure matérielle pendant l'unique
`rename(2)`, que l'espace utilisateur ne couvre pas ; le biais de lecteur, un lecteur qui résout deux
cibles de part et d'autre d'une bascule ; et l'abandon du **brouillon** — la génération inactive
qu'un refus jette —, qui est un `rmtree` qu'une interruption peut couper en deux. Il ne touche aucune
cible, mais il peut laisser un reste. Ce reste est donc rendu **visible** : le brouillon est d'abord
sorti de son emplacement de génération par un `rename` unique, sous un nom en `.tmp`, de sorte que
`EspacePublie.residus()` le voie au lieu de l'ignorer. La bascule suivante reconstruit de toute façon
la génération inactive de zéro.

### Tous les écrivains du manifest passent par le protocole

`data/manifest.json` a trois écrivains d'ingestion (`server/ingest/artifacts.py::write_atomic`, via
`merge_manifest`) en plus du gate. Ils écrivaient **à travers** le lien, c'est-à-dire dans la
génération que le pointeur publie, et hors du verrou : le bundle n'était donc pas immuable, et une
ingestion concurrente courait avec la reconstruction et la bascule d'un run. Toute écriture d'une
cible couverte par un pointeur passe désormais par `EspacePublie.basculer` — même `flock`, même
génération inactive, même unique `os.replace` —, et la génération active n'est jamais mutée.
`ecrire_gate` reste l'unique écrivain du champ `gate` (AD-7) : l'ingestion écrit l'entrée et
**préserve** le gate qui était là, ce qui ne change pas. Une cible ordinaire — `document.json`,
`structure.json` — n'est couverte par aucun pointeur et garde son écriture atomique d'avant.

Pourquoi cette forme l'emporte sur l'autre branche envisagée — durcir encore la restauration : parce
qu'un protocole qui *défait* ce qu'il a déjà fait n'est pas tout-ou-rien. Rejouer un rollback, fût-ce
un nombre borné de fois, admet qu'après épuisement une cible reste dans le nouvel état. Ici il n'y a
rien à défaire. Le prix est réel et se dit : `docs/evals/latest.md` ne se rend plus dans l'interface
web de GitHub (un lien y est affiché comme chemin), le bundle suit deux générations dans le dépôt, un
`git diff` de gate n'est plus seulement un gate, et le verrou différé en `target_story: 4.1` est payé
pour le chemin des évals.

Le résultat est **publié quel qu'il soit** (FR41) dans un
artefact unique — `data/evals-latest.json`, `docs/evals/latest.md`, le résumé de CI et
`GET /api/v1/evals/latest`, que `/` compose (FR42). Publier ne promeut rien : seul `gate.evals_ok`
décide de ce qui est servi (AD-8). Les limites y sont dérivées du run ; les trois réserves
(contresignature humaine, validation par un expert assurance, dictionnaire validé) y sont publiées
sans qu'aucune décision n'en dépende.

### Archivage du rendu lisible

`docs/evals/latest.md` est le rendu du **dernier** run publié. Avant d'être remplacé, son contenu
précédent est archivé sous `docs/evals/campagnes/<date>-<empreinte courte>.md`, où la date est celle
de sa dernière modification et l'empreinte les douze premiers caractères du sha256 de ses octets.
Deux archivages du même contenu retombent sur le même fichier : rien ne s'accumule inutilement, et
rien ne se perd.

La règle existe parce qu'un journal de campagnes qui perd les précédentes ne prouve plus rien sur la
durée : les mesures live qu'il contient ne se reproduisent qu'en repayant.

### Seconde lecture (FR47)

Le plan se produit par `python -m server.evals.relecture --report … --candidate-revision … --out …`
(déterministe, hors réseau, sans clé) ; le verdict rempli se repasse au gate par
`--relecture-verdict`, qui **exige** `--relecture-images <dossier>`. Il est recoupé avec le plan
dérivé du rapport du run sur **cinq** liens — schéma exact, révision, `plan_digest`, couverture
exacte, et l'égalité de chaque `image_sha256` avec l'empreinte recalculée des octets réellement
regardés — et un écart fait échouer la publication. Sans ce dernier lien, une empreinte inventée
était acceptée puis publiée « concordante » sur les quatre surfaces. Sans verdict déposé, le statut
publié est `planifiee`, jamais « concordante ». Les commandes exactes — y compris la récupération des
octets d'images par la route de la story 3.4 — sont dans `docs/tests-live.md`.
