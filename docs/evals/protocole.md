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
| C | holdout final one-shot | stockage privé hors dépôt et workspace | généré par un Codex neuf depuis les seuls corpus normalisés autorisés, puis chiffré sans fichier clair ; la story ne conserve que le reçu public. Le verrou exige fin d'Epic 5, test utilisateur et derniers correctifs, puis consomme la tentative avant déchiffrement : aucun retry ni correctif après le résultat |

### Holdout C : reçu public et verrou fermé

`server.evals.holdout` valide le reçu, le payload chiffré et leur liaison sans lire le contenu. Le
reçu public expose uniquement les empreintes des cas, du schéma, des paramètres, des sources et du
payload, les comptages, la preuve de confinement et l'état `sealed/unexecuted`. La référence de clé
reste dans le reçu privé hors workspace ; le secret lui-même est placé dans le trousseau avec accès
interactif, jamais dans un argument, une variable, un log ou un fichier de travail.

L'armement exige trois attestations distinctes et strictes, chacune liée aux octets d'un fichier de
preuve nommé dont le SHA-256 est recalculé, ainsi que l'autorisation interactive du secret scellé :
elle produit un jeton dont seule l'empreinte figure dans le reçu. Une édition directe du verrou ne
peut donc ni passer la vérification publique ni consommer la tentative. À l'exécution, une garde
vérifie d'abord le lien au reçu, le digest/volume du payload chiffré, l'état armé, les trois
conditions, la tentative intacte et le jeton, avant toute lecture ou révocation de clé ; sous verrou
OS, la partie armement et l'absence de marque antérieure sont vérifiées à nouveau. Seulement alors,
le secret est récupéré en mémoire et l'entrée trousseau irréversiblement supprimée avant la prise
one-shot. La prise crée une marque avec `O_EXCL`, efface le jeton et met l'état à `consumed`; la
marque reçoit ensuite un HMAC du secret avant tout déchiffrement. Une panne à partir de la révocation
peut perdre l'unique résultat, mais effacer la marque ou réécrire le verrou ne restitue jamais la
clé. Le payload est authentifié avant déchiffrement, validé à nouveau par le schéma du runner et
transmis en mémoire ; le profil `vertical` continue d'exiger
`truth.source=lecture_humaine`, tandis que `codex` n'est admis que pour `full`.

Avant le scellement, un validateur runtime distinct charge les modèles Pydantic du produit, sans
réseau, accès aux corpus, au trousseau ou à la destination, et refuse le lot avant chiffrement. Le
générateur ne reçoit que les deux corpus aux chemins et empreintes autorisés, le schéma enrichi des
vocabulaires fermés et les paramètres publics. Son egress passe exclusivement par un relais CONNECT
qui n'autorise que `chatgpt.com`; le profil OS interdit tout accès direct et le relais refuse le dépôt
public. Le scelleur est un troisième processus en `deny default`, sans réseau et sans droit de lecture
sur les workspaces ou le parent du stockage privé. Les verdicts des contre-sondes sont transmis au
scelleur et inclus dans le reçu, sans viser ni parcourir aucun autre holdout.

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

Un cas est stable ssi ses `N` répétitions satisfont **les trois** :

- **(a) la preuve attendue du cas, par répétition** : les identifiants de son oracle `expected`, par
  sorte — `block_ids` parmi les blocs cités par les claims retenues, `fiche_ids` parmi les fiches
  citées. Une seule répétition qui perd une clause attendue rougit le cas ; un cas sans oracle de
  preuve est jugé sur (b) et (c) seuls.
- **(b) le même verdict, admissible** en sinistre ; le même statut (`reason.kind`),
  `found`/`complete` et label en guide — comparés à l'identique entre répétitions.
- **(c) aucune interruption** : une répétition manquante est rouge, jamais retirée du dénominateur.

Tout écart ⇒ cas instable, rouge au plancher `stabilite` ; la dispersion (signatures, **preuves par
répétition**, coûts, latences) est publiée, jamais masquée.

**Re-dérivation du 03/09/2026 (story 5.6 T12).** L'identité de 4.2b était « même preuve
`{doc_id, block_id, kind, quote_hash}` et même verdict admissible » (guide : même statut **et même
ensemble de fiches**). Elle supposait la retrouvaille déterministe d'avant l'amendement AD-1 :
le code choisissait les blocs, l'ensemble des citations était une propriété du corpus. Depuis, le
modèle navigue et **choisit ses citations** — les clauses décisives reviennent, les extraits
auxiliaires et les bornes de « la plus courte quote contiguë » varient. Le `quote_hash` (SHA-256 du
segment relu `Block.text_norm[start:end]`, préfixé de `normalize_version`) reste calculé et publié,
mais il ne se compare plus : AD-3 vérifie déjà chaque citation **au caractère près** contre le
corpus, sa borne est un choix du modèle et sa fidélité n'en est pas un. L'exigence de preuve n'est
pas affaiblie, elle est déplacée — de « la même entre répétitions » à « l'attendue, à chaque
répétition », ce qui rougit aussi trois répétitions qui perdent *toutes* la même clause. Le plancher
reste `1.0` ; le `plancher_digest` change, donc les campagnes d'avant et d'après ne se comparent
pas.

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
  La disposition publiée et, sous `--gate … --profile full`, la composition complète sont validées
  auparavant dans un repère unique. Cette composition précède donc aussi le dry-run et le refus de
  budget : aucun succès ni rapport budgétaire ne peut masquer un lot mal composé.
- **En campagne** : le runner s'arrête avant l'exécution qui déborderait ; le client refuse
  l'appel qui déborderait. Les deux publient les mêmes trois chiffres. Jamais une question
  humaine.
- `automation/plancher.yaml`, `epics.md`, la copie figée produit et ce protocole convergent à
  **1,00 €**. Relever ce plafond exige la distribution p50/p95/max, jamais un ajustement silencieux.

## Matrice baseline (série A, orchestrateur)

Comparées : uniquement des variantes **existantes**.

| Axe | Valeurs |
|---|---|
| variante guide | `outils` (défaut initial), `deterministe`, `full_context` |
| variante sinistre | `outils` (défaut), `deterministe` |
| tier par étape (`COMPRENDRE_TIER`, `REDIGER_TIER`, `VERIFIER_TIER`) | `micro`, `reason` |
| `RELANCE_SUR_NON_PERTINENCE` | `0`, `1` |

`full_context` place le sommaire et tous les passages citables dans le préfixe système cacheable,
jamais dans le message utilisateur, puis rend seulement des IDs résolus depuis le corpus. L'entrée,
le schéma et la sortie sont réservés avant appel dans la fenêtre réelle du modèle. Les mécanismes
ont l'ordre par défaut `dictionnaire → FAQ → sommaire → outils`; le régler ne retrie jamais les
hits internes de l'index. Les points d'un même témoin partagent une seule
identité `--series-id` et une seule phase `--series-kind baseline`; le ledger autorise au plus un
identifiant baseline puis un identifiant final par témoin, tout en agrégeant les coûts de chaque
invocation. Chaque point utilise `--repeat 3` à paramètres épinglés. La série est **tirée après
passation, sur HEAD figé — jamais par le builder** ; les résultats sont consignés dans
`docs/evals/latest.md` et `docs/tests-live.md` quel que soit le résultat.

La publication 4.4 garde toujours ses six cellules. Une cellule jamais exécutée porte `not_run`
et des mesures absentes, jamais zéro. Un rapport partiel ne recommande rien et ne modifie jamais
`data/retrieval-default.json`. Si une baseline gagne sur recall, coût et latence, elle devient la
variante par défaut. Le JSON et le Markdown sont d'abord rendus et validés depuis le même objet,
puis publiés comme une paire avec restauration des deux projections sur panne. Le triplet versionné
(variante, tier, prompt cache) n'est remplacé qu'après cette publication probante ; si sa finalisation
échoue, la paire et le défaut sont restaurés octet pour octet. Un run builder reste diagnostic et ne
peut jamais déclencher cette promotion, même si ses six cellules sont complètes.

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

**La frontière post-commit ne s'arrête pas au `return` de la bascule : elle court jusqu'à la sortie
de la section critique.** `flock(LOCK_UN)` et `close` sont des appels système, donc ils peuvent
lever, et une `OSError` — ou un `KeyboardInterrupt` tombant là — remontait avec le lot déjà publié.
Le verrou est donc pris et rendu **à la main**, sans `with` interne, pour que sa libération tombe du
bon côté de la frontière : après un pointeur effectivement remplacé, elle est absorbée comme le
`fsync`. Symétriquement, une exception de verrou survenue **avant** le commit continue de remonter,
et laisse zéro cible modifiée : il n'y a alors rien de publié qu'elle contredirait. Un filet ferme le
descripteur dans tous les cas — fermer le descripteur rend le `flock`, c'est la garantie du système —
sans quoi une exception levée pendant l'abandon d'un brouillon sortirait en laissant le verrou tenu.

**L'opération de production entière n'a qu'un seul commit.** Un run de gate préparait le couple
`eval-results.*` dans un premier atome, puis le gate, la publication et le manifest dans un second :
chaque atome était tout-ou-rien, l'opération ne l'était pas, et un échec du second laissait le
premier lot déjà changé. Le rapport, sa table, les surfaces publiées et `data/manifest.json` sont
donc préparés **depuis les mêmes octets en mémoire** — `gate.report_digest` est l'empreinte des
octets que ce commit publiera, plus celle d'un fichier déjà écrit — et remis en une seule fois.

Ce qui doit rester distinct le reste, et se décide **avant** le commit : un rapport inexploitable est
refusé sans qu'aucune surface bouge (code 1), un incident technique sort en code 3 sans gate écrit,
et la non-mutation du dernier vert retire le manifest du lot sans en retirer la publication.

### Une lecture pince une génération, et lit tout à travers elle

Tour de la racine vraiment unique (N1). Il n'existait **aucune API de lecture** : le seul objet qui
figeait une génération était `Transaction`, une API d'écriture qui exige le `flock` exclusif et meurt
à la sortie du `with`. Toute autre voie relisait le pointeur **à chaque appel système** — un
`readlink` par cible —, et le démarrage du service en résolvait une quarantaine. Une bascule tombant
entre deux de ces résolutions rendait un état composé de deux générations, que le contrôle
d'empreinte ne voyait pas : il portait sur des octets qu'un second `open()` avait pu remplacer.

`server/app/corpus/racine.py` ouvre le pendant **lecture** de la racine. `lecture_pincee(data_dir)`
résout `courant` une seule fois et rend un repère immuable ; toutes les cibles couvertes se lisent à
travers lui, **sans** prendre le verrou des écrivains — un lecteur ne doit pas sérialiser la
production pour être cohérent. Il n'y a pas de paramètre pour retomber sur une résolution vivante :
ou l'espace est installé **et complet** et tout passe par lui, ou la lecture publique refuse. Le
repli sans racine n'existe que comme primitive interne explicitement appelée par les tests
unitaires. Un regate ne l'atteint jamais : son pincement dédié lie une capacité à la génération, au
`data-dir` custom, au document ciblé et aux octets du manifest validé. Le loader oppose ces quatre
faits et `allow_ungated=True` avant de neutraliser **en mémoire** le seul gate ciblé ; tous les
artefacts restent lus depuis le `data-dir` original, sous le même repère.

Cette capacité n'est employée que si la qualification du runner trouve le gate rouge, périmé ou
hors schéma. Un gate vert et frais garde son jugement. Une autre entrée invalide, un repère sans
génération, étranger ou visant un autre document ferment avant toute mesure ; le manifest sur disque
n'est jamais modifié par la qualification.

Ce que le repère garantit : **une opération de lecture ne mêle jamais deux générations**, et les
octets **hachés** sont les octets **parsés** — une seule lecture, un seul tampon. Il survit à une
bascule concurrente (deux générations alternent ; celle qu'il a pincée n'est reconstruite qu'à la
*seconde* bascule). Deux bascules pendant une même passe la périment : le repère le **détecte** sur
l'identité de l'inode qu'il tient ouvert, et `relire` rejoue la passe sur un repère neuf ; après un
nombre borné de tentatives, le refus est dit plutôt que résolu par un état partiel.

Le contrôle de péremption vit **dans le repère**, pas chez ses appelants : `Lecture.reel` le fait à
chaque résolution. C'est délibéré — un contrôle que chaque lecteur doit penser à faire est un
contrôle qu'aucun ne fait, et c'est exactement ce qui avait été mesuré.

Deux refus nommés complètent la garantie, parce qu'un lecteur qui ne peut pas conclure doit le dire
plutôt que deviner. `LectureHorsGeneration` : la racine connaît le slot d'une cible, mais la cible ne
passe plus par le pointeur — disposition cassée, lien couvert remplacé par un fichier ordinaire. Le
repère refuse au lieu de rendre le chemin brut, c'est-à-dire au lieu de lire à travers le lien
vivant. `EspaceIllisible` : `courant` désigne une génération dont le répertoire est absent ou
illisible. Sans ce refus, chaque slot était vu absent et la passe rendait un corpus **vide sans
refuser** — un corpus vide et un corpus illisible ne sont pas le même fait, et seul le second ferme.

Les opérations de lecture qui le pincent : le démarrage du service (`load_corpus` +
`construire_etat`, manifest, documents, overlays, structures, sommaires, rapports, dictionnaires et
publication d'évals), `run.construire_contexte` et les deux preuves de structure et d'arbre — une
décision de gate ne peut plus être composée de trois générations —, `type_clauses._load`, et
`scripts/smoke.charger_attendus`, dont l'attendu autorise une promotion de trafic. Les sources
`source.url`, `source.pdf` et `source.js` suivent ce même repère ; le renderer conserve les octets
PDF pincés au démarrage et ne relit jamais le chemin vivant par requête.

### La marque du brouillon n'est plus best-effort, dans les deux sens

Tour de la racine vraiment unique (N2). La marque **ne peut pas échouer en silence à la pose** : si
elle ne peut pas être créée, la préparation refuse **avant toute mutation de la génération
inactive** — il n'y a donc rien à abandonner et rien à rendre visible. Elle est **conservée jusqu'au
commit établi** et n'est retirée que dans la frontière post-commit : elle couvre ainsi toute la
fenêtre de mutation, y compris celle qui allait du `fsync` du brouillon à l'atome, et l'abandon
prudent n'a plus rien à *reposer* (une repose best-effort partageait exactement le mode de
défaillance qu'on ferme).

Symétriquement, une marque périmée est assainie strictement **avant** le commit. Si son retrait
échoue, la publication refuse sans mutation. Après le commit, aucun nettoyage ne propage : la
marque qui subsiste reste toutefois visible dans `residus()`, indépendamment de la réussite d'une
trace auxiliaire ou de stderr. Elle est nommée
par pid, et rien ne la moissonnait : après deux bascules parfaitement saines — qui `rmtree`ent et
republient la génération qu'elle nomme —, `residus()` la rendait encore et désignait comme
« brouillon en cours » la génération que le pointeur publie. La transaction qui reconstruit une
génération inactive assainit donc ce qui la concerne. Ce que le nommage par pid ne peut pas garantir
seul s'écrit : une marque dont le
processus a disparu **entre** deux bascules reste jusqu'à la reconstruction suivante de sa
génération — c'est-à-dire jusqu'au prochain écrivain, pas jusqu'à un moissonneur qui n'existe pas.
Et `residus()` n'a **aucun observateur de production** : ses seuls appelants sont la CLI d'opérateur
(`python -m server.evals.espace`) et les tests.

### Aucun entrypoint de production n'écrit hors racine

Tour de la racine vraiment unique (N3). `espace_couvrant` rendant `None` pour *toutes* les cibles,
`_espace_du_lot` rendait `None` **sans lever**, et `publier_artefacts` prenait silencieusement le
repli rootless. Sept entrypoints l'atteignaient — dont trois **après** avoir payé des appels de
modèle, et quatre avec un lot d'**une seule cible**, pour lequel le refus « lot mixte » est
structurellement inatteignable. Une cible couverte dont le lien avait été cassé y était réécrite en
fichier ordinaire, silencieusement.

Chaque entrypoint de production exige donc une racine **installée** et refuse **avant tout travail et
avant tout appel payant** : les six CLI (`kb_to_blocks`, `pdf_to_blocks`, `type_clauses`,
`structure`, `enrich_dictionary` en enrichissement **et** en `--valider`) et le runner d'évals, dont
la vérification tombe désormais avant `_executer_puis_fermer`. La garde ne dépend plus d'un lot
mixte : **aucune** cible couverte est un refus. Elle n'interdit pas les `data-dir` custom — un custom
**installé** passe sans traitement particulier —, elle interdit les `data-dir` **non installés**.
`_publier_sans_racine`, `_atomic_artifacts` et `_lecture_interne_sans_racine` restent, comme
**primitives internes** qu'aucun entrypoint public de production n'atteint. `lecture_de`,
`load_corpus`, le smoke et la relecture refusent un pointeur absent, une génération absente, un
manifest illisible, un lien manquant ou remplacé, et un lien direct vers `a`/`b`. Les répertoires
conteneurs restent, eux, des répertoires ordinaires.

Ce qui décide du **contenu publié** se lit sous le verrou, depuis le repère que la transaction a
pincé : `overlay_hash`, `structure_hash`, le document précédent (`ids_disparus`) et les champs repris
de l'entrée antérieure (`source_hash`, `ingest_fingerprint`, `edition`). Le typage, qui dure des
minutes, **oppose** en outre ces champs à ceux qu'il a réellement typés : une réingestion publiée
entre-temps est un refus, jamais une publication. Pour `enrich_dictionary`, l'opérande est l'entrée
canonique entière capturée avant la campagne — source, document, ingestion, édition, statut,
overlay, structure et gate — sur les transports batch comme standard.

Enfin, sous une racine, un **lien pendant est une absence** : c'est la forme que prend tout artefact
jamais publié, et c'est l'état réel des `structure.json` du dépôt. `structure.presente()` le dit
comme les autres lecteurs le disent (`is_file()`) quand — et seulement quand — la cible est couverte
par une racine ; hors racine, la règle d'AD-16 est inchangée et un lien pendant reste « présent mais
illisible ».

### Ce que le protocole ne garantit pas

Ce qui s'écrit au lieu de se taire : un `SIGKILL` ou une coupure matérielle pendant l'unique
`rename(2)`, que l'espace utilisateur ne couvre pas ; et l'abandon du **brouillon** — la génération inactive
qu'un refus jette —, qui est un `rmtree` qu'une interruption peut couper en deux. Il ne touche aucune
cible, mais il peut laisser un reste. Ce reste est donc rendu **visible** : le brouillon est d'abord
sorti de son emplacement de génération par un `rename` unique, sous un nom en `.tmp`, de sorte que
`EspacePublie.residus()` le voie au lieu de l'ignorer. La bascule suivante reconstruit de toute façon
la génération inactive de zéro.

Le suffixe `.tmp` ne suffit pas comme unique signal, et c'est le second reste possible. Quand le
pointeur est **indécidable** au moment de conclure, l'abandon ne détruit rien — à raison, puisqu'une
génération publiée effacée rendrait toutes les cibles pendantes — mais il laissait alors la
génération inactive, peut-être à moitié écrite, sous son nom `a`/`b`, que rien ne distinguait d'un
bundle complet. La **marque** de brouillon (voir ci-dessus) est ce que `residus()` voit alors : une
génération inactive dont le sort est inconnu n'est jamais silencieuse, ni pour `residus()`, ni pour
un opérateur. La bascule suivante, elle, ne s'y fie pas : elle reconstruit de zéro.

Le **biais de lecteur** n'est plus dans cette liste : il est couvert par le repère de lecture. Ce qui
n'est pas couvert, et qui se dit : un lecteur qui n'emploierait pas le repère — un outil externe, un
script ad hoc — résout deux cibles de part et d'autre d'une bascule et voit un mélange.

### Tous les écrivains d'une cible couverte passent par le protocole, et sous le même verrou

`data/manifest.json` a **quatre** écrivains : trois d'ingestion (`kb_to_blocks`, `pdf_to_blocks`,
`type_clauses`) et le gate d'évals. Quatre défauts distincts les faisaient échapper à la racine, et
les quatre sont fermés.

1. **Une voie qui ne voyait jamais la racine.** `type_clauses._atomic_artifacts` remettait
   `document.json`, `report.json`, `manifest.json` et la suppression de l'overlay à une file de
   `os.replace` directs : sur un manifest couvert, cela **remplaçait le lien statique par un fichier
   ordinaire** et sortait le manifest du pointeur pour de bon. Sa restauration séquentielle — relire
   les octets d'entrée, les réécrire cible par cible sur `except Exception` — était en outre la forme
   même que ce protocole refuse. Son lot complet passe désormais par la racine, d'un seul geste.
2. **Un read-modify-write dont la lecture précédait le verrou.** `merge_manifest` fusionnait un
   `raw` que l'appelant avait lu **avant** son traitement — des minutes plus tôt pour un typage — et
   ne prenait le verrou qu'à l'écriture finale : deux ingestions, ou une ingestion et un run,
   publiaient chacune une fusion partie du même état périmé, et la seconde écrasait l'entrée de la
   première sans l'avoir vue. Sérialiser les commits n'y changeait rien, puisque c'est la **lecture**
   qui était dehors. `EspacePublie.transaction()` tient donc la séquence entière — lecture, fusion,
   publication — sous un **unique `flock`** ; `merge_manifest` ne prend plus de `raw` en paramètre,
   précisément pour qu'aucun appelant ne puisse croire qu'une lecture hors verrou fait une base de
   fusion. Il n'existe pas de voie opt-in : pas de paramètre, pas de défaut permissif.
3. **La génération courante elle-même, lue hors du verrou.** C'est le même défaut au cœur de
   l'espace, et le plus grave : un écrivain parti d'une génération périmée prend pour « inactive »
   celle que le pointeur publie, la reconstruit de zéro et la republie — il mute la génération active
   *et* efface la publication du concurrent. La génération courante est donc lue **sous** le verrou ;
   la lecture faite avant n'est qu'un préflight, qui sert à refuser un espace non installé sans rien
   créer.
4. **Le gate, quatrième écrivain, lisait lui aussi dehors.** `preparer_gate` relisait le manifest
   entier hors verrou, puis remettait sa sérialisation à la bascule : une ingestion publiée
   entre-temps disparaissait du manifest que le gate republiait. Le même défaut portait sur une
   seconde surface couverte, `docs/evals/latest.md` : la décision d'archiver le rendu précédent était
   prise sur une lecture hors verrou, donc un rendu publié entre la décision et le commit était
   écrasé **sans avoir été archivé**. L'opération de gate ouvre donc sa propre transaction et y fait
   tout : relire le manifest, décider la non-mutation du dernier vert, décider l'archivage, publier
   le lot complet. Cela reste **un seul point de commit**.

Une transaction publie **une fois** : republier dans la même section critique reconstruirait la
génération que le pointeur vient de publier et défairait le commit précédent en silence. Le refus est
dit, comme celui de deux cibles au même slot, et avant de toucher quoi que ce soit.

Le lot d'une opération d'ingestion est, comme celui d'un run, l'ensemble complet des cibles qu'elle
implique — `document.json`, `summary.md`, `report.json`, le retrait de l'overlay et le manifest — et
il a **un seul point de commit**. Une suppression est membre du lot au même titre qu'une écriture :
son slot est absent de la génération publiée, donc son lien devient pendant, donc la cible est
absente pour tout lecteur. `ecrire_gate` reste l'unique écrivain du champ `gate` (AD-7) : l'ingestion
écrit l'entrée et **préserve** le gate qui était là, ce qui ne change pas — ce qui change est le
chemin d'écriture, pas qui écrit quoi.

**Ce que la racine couvre** est donc *tout ce qu'un écrivain de production publie*, et rien d'autre :
les surfaces de racine (`data/manifest.json`, `data/dictionary.json`, `data/evals-latest.json`,
`docs/evals/latest.md`, `docs/evals/campagnes`) et, dans chaque répertoire de document,
`document.json`, `summary.md`, `report.json`, `structure.json`, `typing.manual.json` et
`dictionary.json`, `source.js`, `source.pdf`, `source.url` et `source.sha256`. Ces sources sont des entrées, mais leur
sélection et leur lecture décident de ce qui est servi : elles doivent donc appartenir à la même
génération. `source.sha256` est la référence qui autorise la publication du PDF : elle appartient
donc au bundle même si elle n'est pas servie directement. `fetch_source --all` capture son lot par
une unique lecture validée/pincée du manifest et de ces références avant tout réseau.
`server/evals/espace.py::cibles_du_depot` énumère cette disposition **structurellement**, en listant
`data/` — aucun `doc_id` n'est écrit nulle part —, et `python -m server.evals.espace --depot` la pose.
Un lot **moitié couvert** est refusé avant de toucher quoi que ce soit, en nommant les cibles
manquantes : le pointeur ne déplacerait que sa moitié, et l'autre serait écrite à un autre rang.

**Un arbre qu'aucune racine ne couvre** — un `data/` de test, une arborescence jetable — n'a pas de
pointeur à déplacer, donc pas d'atome fort. Il tient néanmoins la même propriété observable vis-à-vis
d'une exception : l'état d'avant est capturé avant la moindre écriture, tous les temporaires sont
préparés ensuite, les `rename` s'enchaînent, et **toute** exception rétablit en ordre inverse les
rangs effectués avant de propager la cause d'origine ; aucun temporaire ne subsiste. Ce qu'il ne
garantit pas, et qui s'écrit : POSIX n'offre aucun renommage multiple atomique, donc une coupure
**hors exception Python** entre deux appels système peut y laisser un lot à moitié publié. C'est
exactement la raison d'être de la racine, qui elle n'a rien à défaire — et une limite non couvrable
ne justifie jamais une cible modifiée après une exception propagée.

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
