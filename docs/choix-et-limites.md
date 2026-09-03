# Choix et limites

## Le cas météo : trouver des mots ne suffit pas

« Il fait quel temps aujourd'hui ? » est le cas qui a orienté tout le démonstrateur. Le mot
« temps » existe bien dans le guide, au sens de temps de trajet. Un moteur lexical peut donc
retrouver une fiche sans rapport avec la météo et la servir avec une source authentique. La
citation ne répare pas l'erreur : elle lui donne seulement une apparence de preuve. La première
décision a été de comprendre l'intention avant de chercher, puis de distinguer quatre résultats :
réponse soutenue, réponse partielle, absence justifiée et échec terminal. **Présence de mots ≠
pertinence.**

Sur les deux surfaces servies — guide et sinistre — le modèle propose dans un contrat borné ; le
code relit les identifiants, citations et invariants avant d'afficher. L'intelligence est payée une
fois à l'ingestion quand elle peut l'être. À chaud, les contrôles entourent le modèle au lieu de lui
déléguer la preuve. Le troisième sujet reste un principe de conception : le modèle proposerait une
requête bornée et la base ferait le calcul ; aucun de ses contrôles n'est présenté comme servi.

## Trois sujets, trois choix

### Sujet 1 — rendre le guide retrouvable sans le vider dans un prompt

Le guide devient un classeur à blocs avec des IDs stables. Un étage déterministe exploite un
dictionnaire enrichi ; la navigation peut ensuite ouvrir le sommaire, des fiches et leurs sections
sous un budget commun. Chaque affirmation factuelle retenue cite un bloc relu dans le corpus, et une
réponse bornée dit ce qu'elle n'a pas lu. Ce choix garde le chatbot dans le site et rend visibles ses
refus, son mode dégradé et ses sources.

Les alternatives n'ont pas toutes une baseline comparable. Tout mettre dans le contexte aurait
fonctionné sur les quelque 23 000 tokens du guide, mais seulement parce que le corpus est petit :
cela ne démontre ni sélection ni preuve d'absence. Une base vectorielle seule classe des proximités,
pas la portée exacte d'une réponse ; les embeddings restent donc une voix possible, pas la décision.
Un chatbot séparé aurait esquivé l'intégration au site, et un parcours guidé seul aurait remplacé le
chatbot demandé plutôt que de l'éprouver. La campagne 4.4 destinée à comparer proprement les
variantes a été annulée : **aucune baseline 4.4 n'est conservée**.

### Sujet 2 — lire des contrats sans transformer le parsing en expertise

Le même classeur reçoit les conditions générales : pages, lignes, coordonnées, tables, définitions
et renvois. PyMuPDF lit d'abord la couche texte ; l'OCR n'intervient que pour une page qui en a
besoin. Le modèle peut étiqueter des blocs ou proposer une structure, mais ne renvoie pas le texte et
ne décide pas de la garantie : **le modèle propose, le code vérifie**. Une citation retrouvée, sa
pertinence, son applicabilité au cas et sa fraîcheur restent quatre statuts distincts. Le verdict à
quatre valeurs est calculé par une table et porte toujours sa limite : « au regard des conditions
générales seules ».

Le parsing niveau 1 seul a été écarté parce qu'il perd définitions et renvois ; l'OCR systématique
ajouterait du bruit à des PDF nés numériques ; un modèle de raisonnement chargé de l'extraction
ajouterait latence et dérive de schéma. La comparaison multi-assureurs n'était pas la demande : le
cas porte sur un assuré et son contrat. À l'inverse, un simple poste de recherche de clauses aurait
évité le verdict demandé ; le compromis retenu est un verdict conservateur qui nomme les pièces et
faits encore nécessaires.

Une **mesure historique** de story 4.2d, sur un seul sinistre répété trois fois, a comparé le rappel
`deterministe` à `outils` : recall `2/3` des deux côtés ; coût de série `0,1649 EUR` contre
`0,0703 EUR`. Les deux séries étaient rouges et instables. Elle explique pourquoi `outils` a été
servi dans ce contexte, mais ne vaut ni baseline générale ni preuve de justesse.

Les rapports versionnés couvrent les pages et publient aussi leurs alertes. Ils prouvent une chaîne
d'extraction inspectable, pas l'intégrité juridique du document, encore moins une validation
d'assurance. Les PDF réels ne sont pas redistribués dans le dépôt. Les deux artefacts servis ont
toutefois été réingérés depuis leurs sources publiques aux SHA-256 vérifiés avec le parseur courant ;
leurs tests de régénération rejouent cette identité à la porte de déploiement. Le registre technique
en annexe conserve le constat historique antérieur à cette fermeture.

### Sujet 3 — calculer sur des tables, pas faire calculer le modèle

Pour 100 000 lignes, le choix décrit est table → base SQL : profiler chaque colonne (type, valeurs
nulles, aberrants), donner au modèle le schéma et un échantillon, lui faire traduire la question en
SQL, afficher la requête, puis laisser la base calculer. Dans cette conception, le garde-fou
n'accepterait qu'un `SELECT` sur les tables autorisées, avec `LIMIT`, timeout et connexion en lecture
seule. Le texte libre se cherche dans les colonnes textuelles **après** le filtre structuré. La
restitution doit montrer le nombre de lignes, les lignes sources et ce que les données ne disent pas.

Ce sujet est une conception documentée, pas une capacité du produit servi. Un RAG générique sur les
lignes, ou un modèle qui additionne lui-même, masquerait le calcul et rendrait la réponse difficile
à reproduire. La mini-démo SQL, son jeu synthétique et son interface sont restés hors de la ligne de
coupe ; aucun résultat n'est donc revendiqué ici.

## Ce qui est tombé de la ligne de coupe

La ligne de coupe protège une démonstration lisible : un guide, deux contrats, une chaîne de preuve
et des évaluations bornées. Plusieurs fournisseurs réduiraient le point de panne par un repli, mais
ajouteraient intégration, qualification des écarts et exploitation ; Claude seul a été retenu pour
ce périmètre, avec une dépendance fournisseur unique assumée. Les embeddings, PDF.js et une
observabilité complète sont différés faute de mesure qui justifie leur coût. La mini-démo tableur
l'est aussi, parce qu'elle constitue un produit indépendant. SLO, supervision, reprise, circuit
breaker et test de charge appartiennent à une exploitation réelle, pas à une rustine de fin de
démonstration.

## Confidentialité et rétention

La règle vérifiée le 25/08/2026 est celle affichée sous les deux saisies : la question et le profil
sont envoyés au serveur puis à Anthropic ; sa politique publique prévoit la suppression des entrées
et sorties de l'API **sous 30 jours**, avec exceptions pour un service à rétention plus longue
contrôlé par le client, un accord de rétention différent, l'application de sa politique d'usage ou
une obligation légale ; un contenu signalé par ses systèmes de sécurité peut être conservé jusqu'à
deux ans. La [politique de conservation d'Anthropic](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data)
reste liée parce qu'une politique de tiers peut changer.

Le serveur ne persiste pas les conversations et ses logs excluent le texte des questions. Le
navigateur ne persiste pas les conversations non plus. Son stockage local conserve en revanche des
données de reprise et de configuration : profil et préférences d'affichage, avancement du parcours,
adresses et coordonnées de comparaison, sélections de sinistres et paramètres de simulation.
**Aucune donnée utilisateur n'entre dans `data/`**. Les contenus utilisateur et documentaire sont
délimités comme non fiables, après les instructions système : cela réduit l'injection
d'instructions, sans l'éliminer. Le démonstrateur public demande donc de ne saisir aucune donnée
réelle ou nominative de sinistre.

## Baseline et état courant

La règle reste simple : **si la baseline gagne, elle devient la variante par défaut**. Un chiffre
ne l'accompagne que si une campagne complète, reliée à son SHA, existe : 4.4 n'en a laissé aucune.

Le gate 4.5 du candidat `cf5c1ba…` s'est arrêté en **incident**, le 30/08/2026 : AXA est
**partiel** à `25/42`, le guide est **partiel** à `53/102`, Baloise est indisponible, pour un coût
fournisseur cumulé réel de `2,1127 EUR`. Il est **rouge et non promouvable**, et **c'est une mesure
datée, pas l'état courant** : **le gate et le verdict courants sont en cours de renouvellement**,
corpus réingérés et gates à rejouer derrière. **L'état servi se lit sur `GET /api/v1/sante`**,
jamais ici.

**Ce que Sonnet coûte** *(03/09/2026)*. Un sinistre nominal facture ≈ 0,199 € à préfixe chaud,
≈ 0,67 € à froid ; le plafond par requête passe à 1,30 €. L'écart tient au préfixe : depuis que le
modèle navigue, c'est le **premier tour** qui écrit le sommaire complet dans le cache, au tarif
d'écriture. Prochain gisement, inchangé : borner ce sommaire (AD-9). Mesures : `docs/tests-live.md`.

## Limites honnêtes

La porte de lecture change l'identité publiée. Un artefact committé non réingéré est déclaré périmé
ici (`empreinte-committee-perimee: <doc>`) jusqu'à sa réingestion. Au 03/09/2026, les deux contrats
sont déclarés périmés — `empreinte-committee-perimee: axa-lu-optihome-2017` et
`empreinte-committee-perimee: baloise-lu-home-2-2024` : la convention Texte passe à
`normalize_version` 3 (la coupure de ligne après une barre oblique est une coupure, plus un espace),
et `normalize_version` entre dans `ingest_fingerprint`. Les artefacts servis, eux, ne sont pas
périmés : `text_norm` est recalculé au chargement, donc les blocs servis portent bien la règle 3.
Seule la chaîne de provenance committée reste celle de la version 2, et elle ne se rétablit qu'à la
réingestion, qui exige les PDF réels (non committés).

- Le limiteur est **best-effort** et par instance : redémarrage, plusieurs instances ou identité
  réseau forgée empêchent d'en faire un quota global.
- Les corpus, dictionnaires et rapports sont des fichiers versionnés chargés en mémoire. C'est
  inspectable ici, mais cela ne tient pas à l'échelle ; la mémoire n'a été mesurée que sur deux
  documents.
- Les verdicts ne sont pas validés par un expert assurance. Ils ne constituent ni une décision de
  garantie ni un conseil ; les contresignatures humaines dues restent visibles.
- L'URL est publique, sans authentification ni cadre contractuel. Le plafond d'une instance protège
  une démonstration, pas un service de production.
- La couverture de parsing ne prouve pas l'intégrité juridique. Définitions, renvois, typage faible,
  colonnes et éditions restent des sources d'écart publiées dans les rapports.
- La copie du site suppose l'accord de son auteur. Les PDF servent de sources récupérées au build et
  ne sont pas redistribués dans le dépôt ; licence, droit de copie et droit de rediffusion restent
  deux questions distinctes.
- Anthropic est l'unique fournisseur de modèles : une panne ou un changement de politique est un
  point de dépendance unique. Les embeddings, PDF.js et l'observabilité sont différés, pas présents
  en silence.
- Il n'y a ni SLO, ni supervision applicative complète, ni reprise automatique, ni circuit breaker,
  ni test de charge. La rétention bornée, l'absence de données utilisateur dans `data/` et la
  réduction de l'injection ne remplacent pas ces garanties d'exploitation.

## Solo, équipe et IA

Ce démonstrateur est un travail solo : une seule personne produit, relit et accepte le code. Une
revue par un second modèle est un contrôle, pas une équipe. En équipe, je travaillerais par petits
PR, avec conventions partagées, propriétaires explicites, revue humaine des changements et des
preuves, et décisions d'architecture consignées avant de déplacer une frontière. Le travail dans le
code d'autrui est déjà traité ainsi : changements minimaux, raisons écrites, historique préservé.

J'ai utilisé l'IA pour coder, explorer les contre-exemples et relire. Je l'assume comme un levier,
avec une discipline : **rester au centre, garder les décisions**. Les contrats, les tests et les
mesures empêchent une suggestion plausible de devenir une vérité par simple fluidité. Il n'y a pas
eu de réécriture humaine intermédiaire de ce retour ; cette absence est visible et non bloquante,
mais la décision finale et l'acceptation restent humaines.

## Chez Foyer, avec mails et pièces de sinistre, par où je commencerais

Je commencerais par la frontière de preuve, pas par le choix d'un modèle : accès et rétention des
mails et pièces, jeu d'évaluation validé avec les experts assurance, extraction typée et traçable,
citations relues dans les originaux, puis décisions explicitement réservées aux humains. Ensuite
seulement viendraient les SLO, la supervision, les reprises et la charge, mesurés sur les volumes et
les risques réels. L'objectif serait d'amplifier l'expert sans lui emprunter sa signature : rendre
chaque réponse contestable, chaque absence visible et chaque bascule réversible.

<details>
<summary>Annexe technique — registre historique conservé à l'identique</summary>

# Choix et limites mesurées

## Story 4.2e — le contrôle demande le contexte qui lui manque, une fois

Un tour de vérification pouvait manquer d'une définition, d'un renvoi ou d'une qualité pour contrôler
une affirmation, sans avoir aucun moyen de le dire : son contrat ne portait que des jugements. Le
jugement tranchait alors sur un contexte qu'il n'avait pas relu, et rien, ni dans la réponse ni dans
la trace, ne le signalait. Le tour sémantique **sinistre** peut désormais rendre une *demande de
contexte* : une catégorie (`definition`, `renvoi`, `qualite`), une cible, l'affirmation concernée et
une raison — quatre valeurs, deux vocabulaires fermés.

**Ce qui borne la demande, et pourquoi ces bornes-là.**

- *La cible ne peut désigner que ce qui était déjà dans l'entrée du modèle* : un bloc parmi ceux qui
  lui ont été fournis, un terme présent dans le message qu'il a reçu, ou une qualité qu'il a
  lui-même énumérée pour cette affirmation. Sans ce contrôle, la demande serait une porte vers un
  rappel neuf, choisi par le modèle, hors de toute borne — c'est-à-dire le contraire de ce que la
  story cherche.
- *Une satisfaction, une reprise, jamais deux.* Le pipeline — jamais l'étape de vérification, qui ne
  touche aucun outil (AD-1) — transmet la demande à *retrouver*, qui la satisfait en **code pur**,
  sur un niveau, sans aucun appel modèle, puis reprend la vérification **une seule fois**. Une
  seconde demande est refusée sans être satisfaite : il n'y a pas de boucle à ouvrir.
- *La passe de satisfaction reçoit le même objet `RetrievalBudget` que la passe initiale*, compteurs
  amorcés avec ce qui a déjà été lu. AD-1 borne l'étape, pas la passe : une borne reconstruite à
  l'identique aurait doublé le rappel sans que rien ne bouge dans la trace.
- *Tout cas fermé l'est vers l'humain.* Demande mal formée, cible inconnue, contexte introuvable,
  place insuffisante sous le budget, seconde demande : l'affirmation visée vaut `humain` par le
  mécanisme qui existait déjà, la réponse porte `complete=False` et une lacune `contexte_non_relu`,
  et le verdict `ne_tranche_pas` est **calculé par la table d'AD-6**, inchangée. Aucun verdict n'est
  fabriqué ni substitué.

**Sa frontière avec la relance d'AD-3.** Ce sont deux mécanismes distincts, et le second n'est pas
une variante du premier. La relance corrige une **rédaction** — une citation mal recopiée, une clause
décisionnelle omise — et coûte deux appels : rédiger, puis vérifier. La demande de contexte ne
reproche rien au rédacteur : elle dit que le **contrôle** n'avait pas de quoi juger. Elle ne rédige
donc pas une seconde fois, ne consomme aucun appel pour se satisfaire, et n'en coûte qu'un pour sa
relecture. Le bloc du pipeline se pose **après** la relance, inchangée, et ne s'y mêle jamais.

**Ce que la reprise coûte, et quand elle n'a pas lieu.** Le tour sinistre peut gagner une passe
`retrouver` de code pur — aucun appel modèle — et au plus un appel `micro` de reprise. Cet appel
entre en concurrence avec le plafond d'appels par requête, que cette story ne relève pas : sur le
pire chemin, une navigation par outils **et** une relance d'AD-3, la place manque et la reprise est
refusée avant tout appel. La réponse acquise est alors servie sans être donnée pour complète, et la
trace le nomme. C'est le comportement voulu — la borne se dit, elle ne se contourne pas — mais c'est
aussi la limite la plus concrète du mécanisme : il relit le moins souvent là où le tour a déjà
beaucoup travaillé.

**Ce qui n'est pas mesuré ici.** `pipeline_digest` et `prompts_digest` bougent tous les deux, et ils
sont globaux : ils entrent dans l'identité de cache de **toutes** les suites, pas seulement du
sinistre. Les campagnes guide et parsing repartent donc froides elles aussi, avec le coût
correspondant. Coût, latence, justesse et dispersion sont des **états produits** : ils se mesurent
hors de cette story, avec le SHA, et ne deviennent des critères qu'au gate 4.5. Le libellé `guide` de
`retrouver.md` reste différé.

## Story 4.2f — une lecture partielle est un état, pas une panne

Quand la chaîne a réellement lu une partie **bornée** du corpus et qu'aucune affirmation ne survit à
la vérification, la réponse servie est désormais un 200 typé et non un 503. Ce n'était pas un
mauvais choix au départ : le seul objet disponible sous `found=False` était l'`AbsenceProof`, et il
publie `blocks_scanned = len(document.blocks)` — l'annonce d'un balayage exhaustif que la troncature
dément. Opposer cette preuve à l'utilisateur, c'était lui affirmer que le corpus ne dit rien de ce
que nous n'avions pas lu ; le 503 fermait ce mensonge, au prix d'un écran « assistant indisponible »
pendant que rien n'était en panne.

**L'état a maintenant son propre type.** `Answer.lecture_partielle` porte `nodes_read`,
`blocks_read` et `documents` : les nœuds d'où viennent les blocs transmis, les blocs *effectivement*
transmis, et le document lu. Jamais la taille du document. Deux bornes se lisent ensemble. Un nœud
dont le budget de blocs a écarté toute la fenêtre n'est pas compté — un compteur d'ouvertures aurait
gonflé le chiffre rendu à l'écran. Mais **tous** les blocs transmis comptent, y compris ceux entrés
par une définition ou une dépendance directe : AD-2 leur donne exactement un nœud propriétaire, et
ne compter que les fenêtres laissait le chemin servi annoncer « 0 section lue, N passages
transmis ». Enfin `blocks_read ≥ 1` : zéro bloc transmis n'est pas une lecture partielle, c'est
l'échec terminal qu'AD-1/NFR2 gardent en `BudgetExceeded`, levé avant *rédiger*.

**Le journal et le fil de conversation parlent la même langue.** Les deux routes nomment l'issue
`lecture_tronquee` — le mot que le harness d'évals posait déjà —, sans quoi ces requêtes, qui
sortaient auparavant en lignes d'erreur `budget_exceeded`, seraient devenues indistinguables de
n'importe quel autre 200 au moment même où elles cessaient d'être des erreurs. Et un tour de suivi
(story 3.7) reconduit le porteur **avec** ce qu'il annonçait manquer : le suivi ne relit rien, donc
la borne du premier tour vaut encore à chaque tour.

**Trois frontières, et elles sont vérifiables.**

- *Avec la preuve d'absence.* `AbsenceKind` reste fermé à ses quatre valeurs et `AbsenceProof` est
  inchangé : une lecture bornée n'en produit jamais. Le domaine exige **exactement un** porteur sur
  `found=False` — `reason` ou `lecture_partielle`, jamais les deux, jamais aucun —, et les deux
  fronts refont l'invariant à la lecture. Un refus dont la lecture était complète continue de porter
  sa preuve, inchangée.
- *Avec la panne.* Un retrieval vidé par le budget, un appel de relance commencé puis échoué, un
  corpus indisponible, un délai dépassé, un plafond de coût, un parse invalide : tous gardent leur
  exception, leur code et leur 503. La ligne de partage n'est pas une opinion — le zéro-bloc lève
  avant *rédiger*, le partage « un appel a-t-il démarré ? » décide des relances, et l'enveloppe
  d'erreur n'a pas changé d'une ligne.
- *Avec la réponse partielle.* Une réponse **retenue** dont la lecture a été bornée n'a pas ce
  porteur : sa borne se dit dans `unknown[]` par la lacune `lecture_bornee`, et `complete=False` la
  publie, comme avant. Le porteur n'existe que là où il n'y a rien à montrer.

**Ce que la réponse dit.** Elle ne dit pas ce qui n'existe pas, elle chiffre ce qui a été lu : une
phrase composée par le code (deux registres, quatre langues), les compteurs, les affirmations
écartées par la vérification — enfin visibles, le bloc qui les affiche n'ayant jamais été atteint
sous troncature — et un `unknown[]` qui porte au moins la lacune de lecture bornée. En sinistre, le
verdict reste le `ne_tranche_pas` que la table d'AD-6 rend sur zéro clause affichée : aucun verdict
de remplacement n'est fabriqué, et AD-6 est inchangée.

**Limite assumée, publiée avant d'être mesurée.** Cette story change un **état produit**, pas une
qualité mesurée. Les cas qui rendaient 503 sur ce chemin rendront un 200 typé ; ce n'est ni une
amélioration du rappel ni du jugement, et rien ici ne prétend qu'une réponse de plus est juste. Le
harness d'évals le dit d'ailleurs dans les mêmes mots qu'avant la bascule : label
`claim_non_soutenu`, `reason_kind = lecture_tronquee`, jamais `bonne_reponse`. `TruncatedRead` est
conservée sans producteur pour cette raison précise — elle reste le contrat qui distingue, côté
harness, une lecture bornée d'un plafond financier, lequel interrompt la campagne. Combien de cas
basculent, et si la réponse partielle sert mieux l'utilisateur que le message d'indisponibilité,
relève d'une mesure à part, avec son SHA.

## Story 4.2c — colonnes géométriques et structure proposée puis vérifiée

Trois limites de l'ingestion sont levées ensemble, et une quatrième est publiée avant d'être mesurée.

**Un registre de lignes source, figé avant toute mutation.** L'extraction produisait des lignes de
travail qu'elle mutait puis jetait ; plus rien n'était adressable en amont du découpage. Chaque ligne
visuelle retenue reçoit désormais un `SourceLine` immuable — `uid` (`p{page}:l{rang}`), texte extrait,
page, boîte et rang d'extraction — posé au **seul** point d'extraction du parseur, donc avant la
fusion des numéros, avant le retrait des bandes récurrentes et avant l'exclusion des lignes de table.
L'`uid` ne dérive d'aucun `block_id` : une proposition ne peut donc pas s'ancrer sur le découpage
qu'elle prétend décider. L'ingestion prouve à chaque exécution que chaque ligne du registre est soit
portée par **exactement un** bloc, soit retirée sous un motif explicite — union complète,
intersection vide. Le registre reste **interne à l'ingestion** : il ne traverse ni `Document`, ni
`document.json`, ni le loader, et le serveur continue de surligner par `Block.lines`.

**Un motif de retrait ne couvre que du contenu réellement non servi.** Une ligne dont le centre tombe
dans la boîte d'une table sort du flux des paragraphes — son texte serait sinon publié deux fois —,
mais elle n'est pas perdue pour autant : son contenu **est** servi, dans le bloc `table` reconstruit
à partir des rangées détectées. Elle est donc confiée à sa table, qui la porte exactement une fois
dans ce bloc, et elle figure au registre présenté au proposant, à la position de la table dans
l'ordre de lecture. La compter « retirée » revenait à servir du texte qu'aucun bloc ne réclamait :
l'invariant validait alors une ligne servie **sans liaison ligne/bloc**, c'est-à-dire le seul chemin
par lequel la bijection pouvait être contournée. Il ne reste qu'un cas de retrait par absorption, et
il est nommé : une table détectée qui ne rend finalement aucune rangée ne produit aucun bloc, et ses
lignes sont alors inscrites sous `table_sans_bloc`. Les deux motifs de retrait sont donc
`bande_recurrente` et `table_sans_bloc`, tous deux réservés à du contenu que rien ne publie.

**Les colonnes se détectent par la gouttière, pas par une mise en page supposée.** Le corps d'un
contrat à deux colonnes était lu entrelacé, parce que `get_text(sort=True)` ne connaît pas les
colonnes. La détection retenue est purement géométrique : une frontière candidate partage les lignes
en trois — entièrement à gauche, entièrement à droite, et celles qui la traversent — et la gouttière
mesurée entre les deux côtés n'est retenue que si elle dépasse `column_gutter_min_pt`, si chaque côté
porte au moins `column_min_lines` lignes et si chaque côté couvre au moins `column_min_span_ratio` de
la hauteur écrite. Une ligne qui traverse une gouttière est **pleine largeur** : elle ouvre une bande
et se lit au-dessus des colonnes qu'elle coiffe. La clé de lecture devient
`(bande, colonne, y0, x0)`, les continuations de paragraphe et de liste se rompent au changement de
colonne ou de bande, et les tables sont replacées dans la colonne de leur propre boîte. La règle
s'applique par récursion, donc à trois colonnes comme à deux, sans cas particulier.

**Une table est une boîte de la détection, et elle reste un bloc atomique.** La détection ne voyait
d'abord que les lignes : les tables n'intervenaient qu'au *bandage*, et seulement une fois qu'une
gouttière avait déjà été trouvée par du texte. Une page dominée par des tableaux — quatre tables à
gauche, quatre à droite, sans une seule ligne — n'avait donc aucune gouttière et se relisait en
rangées, en violation de l'acceptation « la lecture épuise une colonne avant d'entamer la suivante »,
puisqu'une table *est* un bloc. Les tables entrent désormais dans la détection des gouttières et dans
le calcul de la hauteur écrite, comme le texte. Chaque table y est représentée par autant de boîtes
que de rangées détectées — son contenu écrit pèse ce qu'il pèse, et non pour un objet unique, sans
quoi une colonne entièrement tabulaire resterait sous le nombre de lignes exigé de chaque côté ; à
défaut de rangées publiées par l'extracteur, sa seule boîte compte pour une. Ces boîtes sont bornées
par la boîte de la table et posées **à son aplomb** : elles en reprennent les bords gauche et droit et
n'en gardent que la hauteur de la rangée. L'atomicité est donc structurelle, jamais un contrôle
ajouté après coup — aucune frontière ne peut passer entre deux rangées d'une même table sans les
traverser toutes, si bien qu'une table est soit entièrement d'un côté, soit traversante, donc pleine
largeur et ouvrant une bande, exactement comme une ligne traversante. Une table détectée qui ne rend
aucune rangée (`table_sans_bloc`) ne sert rien et ne vote pas. La fusion des numéros observe la même
géométrie que la lecture, tables comprises : une page ne peut pas s'abîmer à la fusion pour se
corriger à la lecture. Sur une page sans table, la liste de boîtes est exactement celle des lignes :
le comportement y est inchangé à l'octet.

**La garde à deux signaux reste une garde sur les lignes.** Elle protège une rangée
« libellé … montant » que `find_tables()` **n'a pas vue** : rien d'autre que leur ligne de base ne
tient ensemble le libellé et le montant, et les lire comme deux colonnes les sépare. Une rangée que
`find_tables()` a vue n'a pas ce statut — c'est déjà un bloc atomique, son libellé et son montant
sont deux cellules de la même ligne du même bloc `table`, qu'aucune gouttière ne peut disjoindre.
Faire voter les rangées de table dans cette garde n'aurait donc rien protégé et aurait coûté :
la grille d'un tableau est appariée par construction, et elle aurait écarté les colonnes voisines.
La garde reste armée à l'identique sur les lignes d'une page qui porte aussi des tables ; elle se
tait pour une gouttière dont un côté n'a aucune ligne, n'ayant alors aucune paire à observer.

**Une rangée « libellé … montant » n'est pas une colonne, et il faut deux signaux pour le dire.**
Une liste de libellés à gauche et de montants à droite laisse un blanc vertical franc que rien
n'oppose, géométriquement, à une gouttière : elle satisfait les trois bornes ci-dessus, et la lire
comme deux colonnes séparerait chaque montant de son libellé. Deux critères la refusent, et
seulement lorsqu'ils sont vrais **ensemble** : les deux côtés sont appariés ligne de base à ligne de
base au-delà de `column_row_pairing_max_ratio` — la signature d'une rangée — *et* le côté le moins
rempli occupe moins de `column_min_fill_ratio` de la largeur dont il dispose, mesurée de la marge de
texte de la page au bord opposé de la gouttière. L'appariement seul ne suffirait pas : une mise en
page professionnelle à deux colonnes partage très souvent la même grille de lignes de base, et
l'écarter pour cela annulerait la correction sur les documents mêmes qu'elle vise. Le remplissage
seul ne suffirait pas non plus : une colonne courte reste une colonne. C'est leur conjonction qui
distingue la rangée de la colonne.

**Aucune gouttière retenue ⇒ rien ne bouge.** C'est la garantie qui rend la détection sûre : sur une
page mono-colonne, l'ordre de lecture est l'ordre d'extraction, à l'identique, jusqu'aux octets des
blocs, des identifiants, des boîtes et du sommaire. Le test qui le prouve rejoue l'ingestion complète
avec la détection rendue impossible et compare les artefacts. La garantie vaut aussi pour une page
qui ne porte que des tables : sans gouttière retenue, elles gardent leur ordre d'extraction.

**Le modèle propose une structure, le code la prouve — ou le document part en quarantaine.**
L'heuristique numérique (`_NUMBER_RE`) ne reconnaît que les décimaux pointés, et un contrat à titres
visuels non numérotés ressortait avec une hiérarchie plate. `server/ingest/structure.py` ouvre au
tier `ingest` une surface de proposition qui ne porte **que** des `uid` de lignes et des liens
parent/enfant : le schéma fournisseur énumère les `uid` autorisés, la réponse n'a ni champ de texte,
ni `kind`, ni portée, ni verdict, et le code revérifie l'appartenance « même si le schéma l'impose ».
Les titres servis sont **relus dans le registre** à partir de l'`uid` désigné et les `node_id` sont un
chemin positionnel (`{doc_id}:s1.2`) calculé par le code : renommer les intitulés ne déplace aucun
identifiant. Le vérificateur est en code pur, hors réseau, et rend un refus **nommé** :
`proposition_illisible`, `document_different`, `ligne_inconnue`, `titre_duplique`, `titre_ambigu`,
`cycle`, `profondeur_excessive`, `largeur_excessive`, `ordre_impossible`, `intervalles_croises`,
`parent_non_contenant`, `ligne_omise`, `affectation_non_prouvee`, `proposition_vide`,
`noeud_non_construit`. Un
nœud prouvé que l'arbre bâti ne porte pas est donc un refus, pas une note : annoncer « proposition
vérifiée » sur un arbre qui n'en contient qu'une partie serait le même mensonge que le repli
silencieux. Un refus est un check `bloquant` `structure_proposee` : le manifest passe
en `quarantaine` et `document.json`/`summary.md` sont purgés. Il n'existe **pas** de repli silencieux
vers l'heuristique — servir un arbre que personne n'a prouvé en laissant croire que la proposition a
été honorée est exactement ce qu'AD-16 interdit. Sans `structure.json`, l'heuristique reste le chemin
nominal, inchangée.

**« Présent mais illisible » n'est pas « absent ».** La seule chose qui rend la main à l'heuristique
est l'absence de `structure.json` au sens du système de fichiers. Un répertoire portant ce nom, un
lien pendant, un lien vers un répertoire, un tube : tous *existent*, aucun n'est lisible, et tous
sont le refus nommé `proposition_illisible`, donc la quarantaine. Le test de présence est donc
`lexists` — qui voit aussi le lien pendant, lequel n'existe pas au sens de sa cible — et non
« est-ce un fichier régulier », qui répondait « non » pour chacun de ces artefacts et faisait
retomber l'ingestion sur l'heuristique **en silence**, sur le chemin même bâti pour l'interdire. La
nature de l'entrée est ensuite jugée avant toute ouverture : un tube serait sinon lu jusqu'à ce qu'un
écrivain se présente, et un refus doit être rendu, jamais attendu.

**La couverture est totale, et la borne qui la nomme ne peut que la durcir.** L'AC exige une borne
explicite de couverture *et* que « toute ligne inconnue, dupliquée, omise » mette le document en
quarantaine : les deux ensemble n'admettent qu'une lecture, la couverture entière. La règle est donc
appliquée **ligne par ligne** — toute ligne du registre hors de tout intervalle proposé est un refus
`ligne_omise` qui nomme les `uid` concernés — et `STRUCTURE_MIN_COVERAGE` reste la borne publiée,
réglée à `1.0`. Elle ne peut que durcir cette règle, jamais la desserrer : l'abaisser ne rouvre aucun
trou, et un test le prouve. Mesuré en revue avec l'ancienne valeur `0.9` : dix lignes dont neuf
couvertes rendaient « proposition vérifiée », et la dixième était ensuite rattachée au nœud du groupe
voisin — l'arbre servi portait une affectation que la proposition n'avait ni portée ni prouvée. Cet
héritage a disparu : le nœud d'un bloc est déterminé par ses seules lignes source. Un bloc qui n'en
porterait aucune de prouvée, ou qui serait servi à cheval sur deux nœuds prouvés — une table atomique
que la proposition scinde, une ligne fusionnée dont les deux `uid` tombent de part et d'autre d'une
frontière —, est le refus `affectation_non_prouvee` : un bloc servi ne peut pas être moins prouvé que
la proposition qui l'annonce.

**Ce qui sera porté ensemble est refusé ensemble, dès l'acceptation.** Le registre publie l'*unité de
portage* de chaque ligne : l'ensemble des `uid` qu'un même bloc portera nécessairement d'un seul
tenant. Deux cas la produisent — les `uid` réunis en une seule ligne de travail par la fusion d'un
numéro et de son intitulé, et les `uid` d'une table, que le bloc `table` porte en entier — et une
seule règle générique les couvre ; l'identifiant de l'unité est positionnel, jamais le nom d'un
assureur, d'un document, d'une page, d'un titre ou d'une numérotation. Une frontière de nœud posée
**à l'intérieur** d'une unité demande de servir un bloc indivisible sous deux nœuds : c'est le refus
`affectation_non_prouvee`, rendu par le vérificateur lui-même et non plus seulement au moment de
bâtir. La distinction compte : `verifier()` rendait « accepté » sur une proposition que
`build_document` refusait à coup sûr, si bien que la CLI hors ligne écrivait un `structure.json` que
l'ingestion ne pourrait jamais accepter — le contraire de « le code accepte la proposition uniquement
s'il prouve ». Deux `uid` d'une même unité ne peuvent pas non plus intituler deux nœuds distincts :
le titre servi est relu sur la ligne *portée*, la même pour les deux, si bien que les deux nœuds
afficheraient le même intitulé et que l'arbre inspectable répondrait deux fois la même chose pour le
même endroit de la page. C'est ce que `titre_ambigu` nomme déjà — le contrôle par `(page, bbox)` ne
suffisait pas, un numéro et son intitulé n'ayant pas la même boîte **avant** fusion — et le
vocabulaire de refus reste donc fermé sur les mêmes mots.

**L'arbre bâti est reconfronté à la proposition, pas seulement compté.** Vérifier que chaque nœud
prouvé existe dans l'arbre (`noeud_non_construit`) ne dit rien de *qui porte quoi*. La réconciliation
finale compare, sur le document réellement construit, le **propriétaire effectif** de chaque `uid` —
le nœud du bloc qui le porte — à celui que la proposition prescrit ; un bloc servi sans aucune ligne
source, un bloc qu'aucun nœud ne réclame et un bloc dont le propriétaire diverge sont les trois faces
du même refus `affectation_non_prouvee`. La garde qui rompt les groupes à la frontière d'un nœud
demeure : elle protège le chemin, la réconciliation prouve le résultat, et tout chemin futur qui
contournerait la première serait arrêté par la seconde.

**La largeur est bornée comme la profondeur, et refusée au plus tôt.** La profondeur de l'arbre
proposé était bornée ; sa largeur ne l'était pas. Le vérificateur porte une boucle en O(n²) sur les
intervalles : une proposition très large faisait donc travailler indéfiniment un chemin dont toute la
valeur est d'être fail-closed et déterministe — refuser vite, ou ne rien valoir.
`STRUCTURE_MAX_NODES` borne le nombre total de nœuds et `STRUCTURE_MAX_CHILDREN` le nombre d'enfants
d'un même parent, **racines comprises** : une proposition « plate » de N nœuds sans parent est une
largeur de N, et c'est le cas qu'un compte par parent seul laisserait passer. Les deux bornes
s'appliquent aux cinq endroits par lesquels une proposition peut arriver — le schéma fournisseur
(`maxItems`), le modèle Pydantic, le parse local de la réponse, le chargement de l'artefact depuis le
disque et le vérificateur lui-même —, sous le motif dédié `largeur_excessive` du vocabulaire fermé.
Le chemin d'entrée est fermé avant le chemin de sortie : `STRUCTURE_MAX_OUTPUT_TOKENS` ne borne que
la réponse du **réseau**, si bien qu'un `structure.json` déposé à la main lui échappe entièrement. Un
tel fichier est donc jugé d'abord sur sa **taille**, sans être lu — il ne peut pas peser plus que la
charge utile qui l'a produit, `STRUCTURE_MAX_INPUT_CHARS`, puisqu'il ne fait que nommer des `uid` que
cette charge portait déjà avec leur page, leur colonne, leur ordre, leur boîte et leur texte —, puis
sur sa largeur comptée sur la charge brute, avant que le moindre modèle soit bâti. Un artefact de dix
millions de nœuds est ainsi rejeté sans avoir été désérialisé, et non après. Ces bornes entrent dans
`ingest_fingerprint` **seulement dans la branche où une proposition est appliquée** : elles peuvent y
faire basculer accepté/refusé, donc changer l'arbre servi, alors qu'un document sans `structure.json`
n'est concerné par aucune d'elles — les y faire entrer quand même périmerait ses artefacts pour un
réglage qui ne décide rien chez lui. `STRUCTURE_MAX_DEPTH`, `STRUCTURE_MIN_COVERAGE` et la version
des règles de vérification suivent le même partage ; `STRUCTURE_MAX_OUTPUT_TOKENS` et
`STRUCTURE_MAX_COST_EUR` restent dehors, car ils bornent la fabrication hors ligne de l'artefact,
jamais son acceptation.

**L'appel suit la convention LLM du spine, et le plafond ne se neutralise pas.** L'unique appel du
tier `ingest` passe par `messages.parse(..., output_config={"format": …})` **sans** `output_format`,
et la réponse est validée localement par un `TypeAdapter` sur la forme filaire, avant les contrôles
d'`uid` qu'il ne remplace pas. La raison est celle du spine : avec `output_format`, le SDK valide le
texte avant de rendre la réponse et lève, si bien que `usage`, `stop_reason` et le texte reçu — donc
le coût réel d'un appel pourtant facturé — seraient perdus. Ici, un refus de forme les porte tous les
trois. Le plafond, lui, est résolu et validé **avant** toute extraction et toute construction de
client : une valeur non finie ou nulle est refusée d'entrée, quelle que soit sa source. `nan` rendrait
la comparaison au majorant toujours fausse et `inf` ne bloquerait jamais — le plafond annoncé serait
neutralisé juste avant l'appel le plus cher du projet. Le réglage `STRUCTURE_MAX_COST_EUR` est borné
strictement positif par `Settings`, ce qui écarte `nan` mais laisse passer l'infini : la garde porte
donc sur la valeur résolue, pas sur le seul argument de ligne de commande.

**La proposition est un artefact, jamais un appel à chaud.** AD-2 exige que `source_hash` +
`ingest_fingerprint` égaux rendent les mêmes identifiants : rejouer le modèle à chaque ingestion
rendrait l'arbre instable et mettrait le document en quarantaine à chaque démarrage.
`data/{doc_id}/structure.json` est donc écrit **une fois**, hors ligne, par
`uv run python -m server.ingest.structure <doc_id>`, puis relu et revérifié à chaque ingestion. Sa
liaison cryptographique au manifest reste hors périmètre (différé, `target_story: 4.5`).

### Les douze réglages de la story, et où ils se règlent

Convention Seuils : aucune de ces valeurs n'est écrite en dur dans le parseur ou le vérificateur.
Chacune vit dans `server/app/config.py` avec la justification de sa valeur, est publiée par
`thresholds()` — donc lisible sur `/api/v1/sante` et dans `Trace.thresholds` — et se règle par la
variable d'environnement du même nom en majuscules. Les cinq bornes de colonne entrent en plus dans
`ingest_fingerprint`, parce qu'elles changent l'ordre de lecture, donc les identifiants ; les quatre
bornes du vérificateur et `STRUCTURE_MAX_INPUT_CHARS` n'y entrent que lorsqu'une proposition est
appliquée, parce qu'elles ne décident de rien pour un document qui n'en porte pas.

La liste ci-dessous est un **second texte faisant autorité sur les mêmes nombres** : une garde
(`test_les_seuils_des_colonnes_et_de_la_structure_sont_bornes_publies_et_documentes`) relit chaque
valeur par `Settings` et rougit si elle s'écarte du défaut, pour qu'une dérive comme celle qu'a
connue `STRUCTURE_MAX_INPUT_CHARS` — annoncée à 300 000 alors que les deux contrats déjà ingérés
mesurent 710 081 et 655 999 caractères — ne survive pas une story de plus.

Géométrie des colonnes du corps :

- `COLUMN_GUTTER_MIN_PT=18.0` — largeur minimale du blanc vertical entre deux colonnes.
- `COLUMN_MIN_LINES=4` — boîtes entièrement d'un côté, exigées de chaque côté : une ligne de texte,
  ou une rangée de table posée à l'aplomb de la boîte de sa table.
- `COLUMN_MIN_SPAN_RATIO=0.35` — part de la hauteur écrite — tables comprises — que chaque côté doit
  couvrir.
- `COLUMN_ROW_PAIRING_MAX_RATIO=0.5` — appariement des lignes de base au-delà duquel les deux côtés
  ressemblent à une rangée ; mesuré sur les lignes seules, et n'écarte la gouttière qu'avec le
  critère suivant.
- `COLUMN_MIN_FILL_RATIO=0.6` — part de sa largeur *disponible* qu'occupe le côté le moins rempli,
  mesurée elle aussi sur les lignes seules.

Structure proposée puis vérifiée :

- `STRUCTURE_MAX_DEPTH=6` — profondeur maximale de l'arbre proposé.
- `STRUCTURE_MAX_NODES=2000` — nombre total de nœuds. **Mesuré** sur les trois documents déjà
  ingérés : 751, 88 et 2 nœuds ; la borne est réglée à ~2,7 fois le maximum mesuré.
- `STRUCTURE_MAX_CHILDREN=256` — enfants d'un même parent, racines comprises. **Mesuré** de même :
  la fratrie la plus large en compte 87, puis 54 ; même marge.
- `STRUCTURE_MIN_COVERAGE=1.0` — part des lignes du registre que les intervalles doivent couvrir. La
  borne est nommée et publiée parce que l'AC l'exige ; la règle appliquée est par `uid` et
  inconditionnelle, si bien qu'abaisser ce réglage ne rend aucune ligne omise acceptable.
- `STRUCTURE_MAX_INPUT_CHARS=900000` — la charge utile est le registre du document entier et part en
  une seule requête : aucun découpage n'est possible pour un arbre qui porte sur tout le document.
- `STRUCTURE_MAX_OUTPUT_TOKENS=16000` — la réponse ne porte que des `uid` et des liens.
- `STRUCTURE_MAX_COST_EUR=8.0` — majorant du run (somme des segments), vérifié **avant** toute
  construction de client ; `--max-cost` le surcharge pour un run. Mesuré le 2026-09-02 avec la
  sortie dérivée : Baloise 5,99 €, AXA 6,28 € de majorant, pour un coût réel attendu bien inférieur.

### Limite assumée : les artefacts committés attendent une réingestion

Corriger l'entrelacement change l'ordre de lecture d'un document réellement à deux colonnes, donc ses
`seq`, donc ses `block_id`. Les cinq seuils géométriques entrent dans `ingest_fingerprint`, et
`PARSER_VERSION` passe de `9` à `10` : l'empreinte du parseur courant diffère donc de celle inscrite
dans les `document.json` committés. Les PDF sources ne sont pas versionnés ; **aucune mesure n'a donc
été prise sur un contrat réel dans cette story**, et aucun chiffre d'ingestion réelle n'est publié
ici. Les artefacts servis restent cohérents entre eux — `document.json` et son manifest portent la
même empreinte, ce que le loader vérifie —, mais ils ne sont plus reproductibles par le parseur
courant. Les deux tests de régénération sur PDF réels (`tests/test_parsing_axa.py`,
`tests/test_parsing_baloise.py`) sont `skip` sans les sources et rougiront à la porte de déploiement
(`REAL_PDF_TESTS_REQUIRED=1`) tant que l'ingestion n'aura pas été rejouée avec les PDF réels, suivie
du typage. C'est un état de la donnée servie, pas un verdict sur la règle.

La divergence est **déclarée ici, document par document**, et une garde toujours exécutée la
confronte à l'empreinte du parseur courant (`assert_empreinte_committee_declaree`, jouée par
`tests/test_parsing_axa.py` et `tests/test_parsing_baloise.py`). Liste active au 02/09/2026 :
aucune — les deux contrats sont réingérés avec le parseur courant.

Une empreinte committée qui divergerait **sans** figurer dans cette liste fait rougir la garde ; et
le jour où la réingestion rétablit l'égalité, c'est la ligne restée ici qui la fait rougir, pour
qu'elle soit retirée plutôt qu'oubliée. Les deux valeurs comparées sont lues — l'une dans
`document.json`, l'autre au parseur — et aucune n'est écrite en dur dans un test.

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
blocs, dont 510 juridiques et 453 confirmés par deux lectures. Après convergence de la revue et du
dictionnaire, le gate Baloise a réussi ses trois cas : le contrat est désormais servi et
sélectionnable sans dérogation, y compris en production.

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
invité/cigarette. Leur attendu prudent accepte `sous_conditions` ou `ne_tranche_pas`; le gate final
les a exécutés 3/3 avec succès pour 0,1520 EUR.

Cette relecture est une inspection agent, pas une validation assurance. La relecture par Lancelot et
l'expertise restent toutes deux dues; les YAML portent `countersigned_by: null` et
`validated_by_expert: false`.

### Dictionnaire contractuel reproductible

Le premier `dictionary.json` Baloise contenait trois groupes candidats préparés pour les témoins. Il
**n'avait pas été généré** par `enrich_dictionary` : l'ancienne projection ne voyait que les
catégories de guide et rendait zéro unité sur cette racine plate. Il a été remplacé, sans le
certifier rétroactivement, par la sortie reproductible décrite ci-dessous.

La projection corrigée ne fabrique aucun article ni fiche : elle prend les seuls blocs citables du
document, conserve leur `block_id`, leur texte fourni et leur vrai nœud propriétaire, puis les groupe
sous les bornes de configuration. Un bloc trop long n'est fourni que par un préfixe marqué
`truncated=true`; le prompt interdit toute déduction sur le texte absent. Le guide structuré conserve
son prompt et son payload historiques. La voie de développement exacte est :

```bash
ANTHROPIC_API_KEY= uv run python -m server.ingest.enrich_dictionary \
  --doc-id baloise-lu-home-2-2024 --transport standard --max-cost 5.01 --dry-run
uv run python -m server.ingest.enrich_dictionary \
  --doc-id baloise-lu-home-2-2024 --transport standard --max-cost 5.01
```

La seconde commande a été exécutée exactement une fois le 27/08/2026. Le transport standard part de
zéro via `messages.create`, sans Batch ni retry; son plafond est vérifié avant le premier appel et le
fichier existant n'est remplacé que si toutes les unités et les intents reviennent complets et non
tronqués.

Le dry-run réel, clé neutralisée, prépare **35 unités de blocs + les intents**, soit **36 appels
Messages standard**, pour un majorant de **5,0002 EUR** sans remise sous le plafond de 5,01 EUR. La
campagne réelle a coûté **2,8518 EUR** et écrit 540 canoniques, 3 029 variantes, zéro question
candidate et 87 déclencheurs. Le fichier est chargeable, décrit l'empreinte courante du corpus et
reste `validated=false` : sa génération ne remplace ni la validation humaine ni le gate du document.

### Gates finaux

Les trois gates ont été exécutés une fois chacun après stabilisation du code, du dictionnaire et des
digests, tous sur le profil `vertical` et sans contresignature :

- `lux-guide` : 1/1, 0,0706 EUR ;
- `axa-lu-optihome-2017` : 1/1, 0,0900 EUR ;
- `baloise-lu-home-2-2024` : 3/3, 0,1520 EUR.

Le manifest porte le digest final `7e57eec6…`; ses trois entrées ont `evals_ok=true` et
`countersigned=false`. Cela certifie l'exécution des cas versionnés, pas une validation assurance :
la relecture par Lancelot et l'expertise restent dues.

## Correctif du 02/09/2026 — la porte de lecture

Le registre de lignes envoyé au modèle de structure a montré, sur le contrat luxembourgeois à deux
colonnes, quatre défauts de la **porte de lecture** — l'étage qui décide, avant toute segmentation,
ce qu'est une ligne, ce qui n'en est pas une et dans quel ordre elles se lisent. Aucun ne tenait à ce
document : chacun est une règle qui mesurait la mauvaise grandeur.

**Un titre courant n'est pas défini par sa bande.** Le retrait des en-têtes et pieds n'observait que
les bandes haute et basse. Un titre courant composé **tourné** dans une marge latérale — 47 pages sur
48 — n'y tombait jamais et ressortait au milieu du flux d'une colonne, entre deux moitiés d'une même
phrase. Hors bande, la position ne prouve rien ; deux autres choses le prouvent, et il faut les deux :
la **direction d'écriture** de la ligne, plus verticale qu'horizontale — elle n'appartient donc pas au
flux composé de la page —, et la **récurrence** de son texte de page en page, ses suites de chiffres
masquées, au-delà de `header_min_pages_ratio`. Une ligne tournée unique dans tout un document — une
mention d'édition en marge, comme en porte l'autre contrat — reste du contenu ; un refrain posé droit
au milieu d'une page reste du corps. Le motif de retrait est nommé `titre_courant_tourne` : il
s'ajoute à `bande_recurrente` et `table_sans_bloc`, qui n'étaient que deux plus haut.

**Un glyphe seul n'est pas une ligne.** Une puce, un point-virgule, un tiret composés dans un span
séparé sortent de l'extraction comme des lignes à part entière, ordonnées sur le haut de leur propre
boîte : une puce posée un point plus bas que son texte se lisait **après** lui. Elle ouvrait alors un
bloc muet, décalait tout ce que la segmentation compte ensuite, et le modèle de structure la
rattachait au titre voisin — d'où des propositions correctes refusées. La règle ne connaît ni puce ni
ponctuation : une ligne **sans aucun caractère alphanumérique** qui partage la bande d'une ligne de
texte voisine et en est disjointe en x lui appartient, à la place que sa géométrie lui donne — devant
si elle est à sa gauche, derrière si elle est à sa droite —, jamais au-delà d'une gouttière. Sa
partenaire est celle dont elle recouvre le plus la hauteur, la plus proche en x à recouvrement égal.
Un glyphe qui ne partage la bande d'aucune ligne reste une ligne : il n'y a rien à réparer, et
l'inventer serait une supposition. La ligne d'accueil absorbe ses `source_uids` — le registre ne perd
aucune ligne source, exactement comme à la fusion des numéros.

**Une gouttière se classe sur ce qu'elle coupe, pas sur ce qu'elle laisse.** La frontière retenue
était la plus large, mesurée entre les boîtes qu'elle avait **écartées** ; les boîtes qu'elle
**traversait** ne pesaient rien. Deux fausses gouttières gagnaient ainsi contre la vraie. Un retrait
intérieur à une colonne ouvre un blanc plus large que la gouttière de page, en déclarant pleine
largeur les dizaines de lignes qu'il traverse : la colonne entière ouvrait une bande par ligne et se
relisait entrelacée avec sa voisine. Et deux départs de colonne distants d'un centième de point
suffisaient — le second gagnait pour un centième de blanc de plus, en coupant toutes les lignes qui
commençaient au premier. Le classement devient un ordre total à trois clés : **le moins de boîtes
traversées**, puis le plus de blanc, puis la frontière la plus à gauche. L'admissibilité, elle, ne
change pas : l'ensemble des candidates reste exactement le même, seul le choix parmi elles change.

**Pleine largeur veut dire d'un bord à l'autre.** Une boîte était déclarée pleine largeur dès qu'elle
traversait **une** gouttière. Sur trois colonnes, un intertitre posé sur les deux de droite se lisait
donc avant la colonne de gauche qu'il ne coiffe pas. Elle ne l'est désormais que si elle les traverse
**toutes** ; sinon elle prend le rang de la colonne où elle commence. Avec une seule gouttière — deux
colonnes — les deux formulations se confondent : le comportement y est inchangé à l'octet.

**Une gouttière serrée se prouve par la disjonction, pas par le morcellement.** Sous
`column_gutter_min_pt`, la seconde preuve empruntée exigeait « assez de blocs source indépendants de
chaque côté », comptés contre `column_min_lines`. C'est le morcellement de l'extracteur qui était
mesuré, jamais la séparation : une colonne rendue **d'un seul tenant** — le cas le plus favorable, où
l'extracteur a lui-même reconnu un flux unique s'arrêtant à la gouttière — était rejetée pour n'être
pas assez fragmentée, et la page repartait sur une fausse frontière intérieure à sa colonne. Ce qui
est exigé est que les flux soient **disjoints** : chaque côté fait de blocs source qui lui
appartiennent, aucun d'eux dans une boîte traversante. Les autres preuves — départs séparés par le
seuil publié, deux côtés réellement remplis — sont inchangées.

`PARSER_VERSION` passe de `19` à `20` et `SEGMENTATION_RULES` porte les cinq énoncés : l'empreinte
d'ingestion change, et **les deux** artefacts committés deviennent périmés jusqu'à leur réingestion.
Mesuré hors ligne sur les deux PDF réels, sans un seul appel facturé. Contrat luxembourgeois : 47
titres courants retirés (`en_tetes_retires` 47 → 94), 759 lignes-glyphes ramenées à 1 — isolée dans sa
bande —, 42 pages à gouttière toutes lues colonne par colonne, 0 ligne de colonne déclarée pleine
largeur (68 avant), 1 039 → 962 blocs. Contrat AXA : son unique ligne tournée est conservée, 0
ligne-glyphe, ses trois pages de sommaire à deux colonnes retenaient une frontière qui coupait
jusqu'à 38 boîtes et retiennent désormais celle qui n'en coupe aucune, 1 407 → 1 400 blocs et 751
nœuds inchangés. Le typage déjà payé se réutilise sur l'identité source des blocs : **966 sur 966**
pour AXA — aucun bloc à retyper —, 459 sur 737 pour le contrat luxembourgeois, soit 278 à rejouer.

## Correctif du 02/09/2026 — un amendement d'AD-16, écrit plutôt que glissé

AD-16 dit qu'« un appel commencé qui échoue reste terminal », et cette règle protège une chose
juste : ne pas servir une réponse dont on ne sait plus ce qu'elle a coûté ni ce qu'elle contient.
Elle souffre désormais **une** exception, et une seule.

Quand la rédaction relancée a abouti et que seule sa **vérification** échoue, la première
vérification existe : elle est complète, elle a été payée, et elle est servable. La relance, elle,
est discrétionnaire — c'est le pipeline qui l'a décidée, pas l'utilisateur qui l'a demandée. Jeter
une réponse vérifiée parce qu'une amélioration facultative a expiré est le contraire de ce que la
règle protège. Le cas n'est pas théorique : un `APITimeoutError` sur un second *vérifier* mesuré à
26,3 s (témoin A16, deuxième réponse) transformait un 200 valide en 503, à 34 % de marge sous
`llm_timeout_s`.

La réponse acquise est donc servie, **jamais donnée pour complète** — elle porte la même cause typée
que la relance qui n'a pas pu démarrer —, et l'appel échoué reste dans la trace avec son étape et son
coût. Si l'acquis n'a rien trouvé, il n'y a rien à servir et la règle d'origine s'applique sans
exception. Le second *vérifier* de la reprise après demande de contexte, lui, garde la règle
d'origine : il relit la même ébauche, et son échec ne laisse aucune vérification neuve derrière lui.


## Correctif du 02/09/2026 — un titre n'est pas une clause, une phrase coupée reste une phrase

Le premier corpus dont l'arbre vient d'une **proposition vérifiée** — et non de l'heuristique
numérique — a montré trois défauts de la projection servie. Aucun ne tient à ce document : chacun est
une règle qui ne s'appliquait qu'à l'autre chemin de construction, ou qui mesurait la mauvaise chose.

**Un titre que la proposition désigne est un titre.** Sur le chemin numérique, la géométrie reconnaît
l'intitulé et le bloc naît `heading`. Avec une proposition, c'est elle qui dit quelles lignes font le
titre (`title_line_uids`) — et rien ne le projetait sur les blocs. Résultat mesuré : **0 bloc
`heading`** dans le contrat luxembourgeois contre 388 dans l'autre, et **267 blocs citables dont le
texte est exactement le titre de leur propre nœud**, dont 166 de moins de 25 caractères. Le refus du
vérificateur (« un titre ne se cite pas seul »), `Index.unite_de_renvoi` et `retrouver._unite_primaire`
testent tous les trois `kind == "heading"` : ils ne voyaient plus un seul titre du document, et une
question de définition se voyait servir le mot seul — « Bâtiment », « Vétusté », « Déchéance » —
comme la clause qui répond. Le seuil de longueur ne rattrapait rien : citer le bloc entier satisfait
toujours la seconde branche de `quote_min_ratio`. Un bloc qui ne porte **que** les lignes de titre de
son nœud reçoit donc `heading` ; celui qui colle le titre à son premier alinéa porte de l'information
au-delà de l'intitulé et reste citable. La réutilisation du typage applique la même règle que
`type_clauses` : un titre ne reprend jamais l'étiquette juridique décidée quand on le lisait comme un
paragraphe. **0 → 270 blocs `heading`, 267 → 0 blocs citables réduits à leur intitulé.**

**Page, colonne et bande sont la même rupture.** `continues` n'était posé qu'au changement de page,
alors que la segmentation rompt déjà un groupe au changement de colonne ou de bande : trois formes
d'un seul fait, « la mise en page s'arrête ici ». Et la règle exigeait l'**égalité des `kind`** : les
sept coupures à cheval sur une page laissent un item à puce ouvert et reprennent sans puce, donc en
`para`. Un bloc `list` rouvre un item — sa puce le dit — et ne poursuit que la liste dont il vient ;
un `para` ne rouvre rien et reprend aussi bien un paragraphe qu'un item. Vingt-trois phrases étaient
servies en deux clauses, dont `p15:19→p16:1` qui **inverse le sens** d'une exigence : la première
moitié semble n'en poser aucune, et « ce contrat d'entretien doit être en vigueur » tombe dans la
seconde. S'y ajoute ce que `continues` veut dire : une clause que la mise en page coupe reste une
clause, et hors du corps il n'y a pas de phrase à poursuivre — une entrée de sommaire est close par
son renvoi de page, et la dernière entrée d'une colonne s'accrochait à la première de la suivante.
**7 → 38 liens posés par l'ingestion sur le contrat luxembourgeois, les 4 de l'autre inchangés.**

**Le titre projeté n'est pas le texte du bloc.** Trente-trois intitulés arrivaient à l'écran avec la
mise en page de leur source : quatorze ouverts par une puce, dix-neuf portant une tabulation brute
(« a.<TAB> Vol / Vandalisme »). Deux règles de forme, aucun vocabulaire : un titre projeté n'ouvre pas
sur un mot de tête sans le moindre caractère alphanumérique — la définition de « glyphe » qu'a déjà
le parseur —, et une tabulation ou un saut de ligne y vaut une espace. Le `text` des blocs reste
fidèle à la source : c'est le titre projeté dans `Node.title` et dans le sommaire, et lui seul, qui
est rendu lisible. L'espace fine française d'un « les cours ; » n'est pas un blanc de mise en page et
reste — sans quoi 150 intitulés de l'autre contrat auraient été appauvris pour rien.

`PARSER_VERSION` passe de `21` à `22` et `SEGMENTATION_RULES` porte les trois énoncés. Mesuré hors
ligne sur les deux PDF réels, **sans un seul appel facturé**. Aucun identifiant de bloc ne change :
`ids_disparus = ids_nouveaux = 0` sur les deux documents, et le contrat AXA est **inchangé à l'octet**
(1 400 blocs, 751 nœuds, 388 `heading`, 4 `continues`) hors son empreinte d'ingestion. Le typage déjà
payé se réutilise sur l'identité source des blocs : **1 392 sur 1 392** pour AXA, **942 sur 964** pour
le contrat luxembourgeois. Les 22 qui restent sont exactement les moitiés que l'ingestion vient de
relier : leur typage avait été décidé sur un fragment de phrase, il doit être relu avec la phrase
entière — ce n'est pas une perte, c'est la conséquence voulue du lien restauré.

### Ce que ce tour n'a pas fermé, avec les lignes vues

- **Un fragment de tableau précède son en-tête, page 38.** `p38:6` (`'7 | 82 / 8 | 79 …'`) est servi
  avant `p38:7`, qui porte l'en-tête « Mois | Limite de garantie ». Les deux tables sont côte à côte
  et la page ne retient aucune gouttière, donc l'ordre retombe sur `y` : 627 pour celle de droite,
  631 pour celle de gauche. Deux bornes la refusent, indépendamment : la gouttière mesure **16,3 pt**
  pour `column_gutter_min_pt = 18,0`, et la colonne de droite couvre **161 pt** de hauteur écrite
  pour un minimum de `column_min_span_ratio × 660,5 = 231,2 pt`. La page ne porte que **3 lignes de
  texte** : sa géométrie de colonnes est prouvée par des tables seules, et la seconde preuve des
  gouttières serrées, qui exige des blocs source de chaque côté, se tait faute de lignes. Descendre
  l'un des deux seuils rejugerait l'ordre de lecture de **toutes** les pages des deux contrats : ce
  n'est pas un réglage local, et le faire pour une page serait le calibrer sur elle.
- **Quatre phrases restent coupées** (trois dans l'autre contrat), pour deux causes distinctes. Deux
  le sont par un interligne que `para_gap_ratio` juge trop grand : p7 `18,74 pt` pour un seuil de
  `16,29`, p24 `18,75` pour `16,29` (AXA : p32 `26,20` pour `16,89`, p43 `26,00` pour `16,59`). Deux
  autres reprennent la phrase **dans un item à puce** (p7, p31 ; AXA p36) : les lier reviendrait à
  faire poursuivre un paragraphe par une liste, ce qui recollerait toute amorce d'énumération à son
  premier item.
- **`Node.scope` est nul sur les 274 nœuds** du contrat luxembourgeois (93 sur 751 pour l'autre) :
  `ClauseCitee.socle` vaut donc `True` partout et la règle (3) d'AD-6 traite les garanties
  optionnelles comme du socle. La portée est dérivée par le typage, hors périmètre de ce tour.
- **Le témoin live `test_sinistre_live` demande une réécriture de sa fixture.** `build_summary` écrit
  l'`ingest_fingerprint` dans l'en-tête de `summary.md`, et le sommaire part dans le prompt système :
  une génération de parseur change donc la clé d'une requête enregistrée. Un seul appel sur cinq est
  concerné, et son texte est identique une fois l'empreinte masquée — mais le mécanisme d'alias du
  dépôt exige des certificats **égaux** et ne peut pas l'absorber, à raison. La fixture se réenregistre
  avec la clé (`pytest tests/test_sinistre_live.py`). En amont, faire entrer une empreinte de parseur
  dans un prompt facturé et mis en cache reste un choix à rejuger.


## Correctif du 03/09/2026 — le dictionnaire d'un contrat par article

`data/axa-lu-optihome-2017/dictionary.json` n'avait jamais été généré : deux groupes préparés à la
main, zéro intent. Chaque requête sinistre le disait — « dictionnaire : 0 variante(s) ajoutée(s) à
0 terme(s) » — et le refus « zéro hit » d'AD-5 restait désarmé (`dictionnaire_non_valide`). C'est la
cause commune des échecs lexicaux mesurés dans la nuit du 02 au 03/09/2026 : « fumée » ne
rencontrait pas « fumées et suies », « vitre » pas « vitrages », « insert » pas « foyer ».

**La coupure mesurait la mauvaise grandeur.** Une unité d'enrichissement s'arrêtait à chaque
frontière de nœud. Le nombre de requêtes suivait donc la finesse de l'arbre et non la taille du
texte : l'arbre AXA porte **1 392 blocs citables répartis sur 750 nœuds** — une feuille par article
—, et le dry-run rendait **751 requêtes pour un majorant de 81,53 €** contre un plafond de 8 €. Les
deux bornes existaient déjà (`dictionary_flat_max_blocks_per_request = 20`,
`dictionary_flat_max_input_chars = 12000`) et n'étaient jamais atteintes.

Une unité enchaîne maintenant les nœuds consécutifs, dans l'ordre du document, tant qu'aucune des
deux bornes n'est atteinte. Les mesures, clé neutralisée, en transport standard :

| document | blocs citables | nœuds porteurs | requêtes avant | requêtes après | majorant `ingest` | majorant `reason` |
| --- | --- | --- | --- | --- | --- | --- |
| `axa-lu-optihome-2017` | 1 392 | 750 | 751 | **71** | 9,68 € | **6,11 €** |
| `baloise-lu-home-2-2024` | 964 | 270 | 271 | **50** | 6,73 € | **4,25 €** |

Les 35 unités que ce registre consigne pour Baloise au 27/08/2026 ne sont plus atteignables et ne
sont pas un objectif : elles datent d'une racine plate, avant que le document ne reçoive sa
structure. À 964 blocs citables et 20 blocs par requête, 49 unités sont le plancher arithmétique ;
la mesure ci-dessus s'y tient à une unité près. Ce qui a changé de sens, c'est le nombre **avant** :
270 unités, une par nœud, pour un majorant de 30,00 €.

**Le payload dit ce que l'unité est devenue.** Une unité pleine couvre plusieurs nœuds ; elle nomme
donc son `premier_bloc` et son `premier_noeud`, jamais un propriétaire commun, et chaque extrait
porte déjà son `node_id` et son `node_title`. Le prompt de la voie contrat, qui présentait « le vrai
nœud propriétaire », dit maintenant que deux extraits de nœuds différents traitent de sujets
différents et ne se lisent pas comme un seul article. La voie guide — prompt, payload, `max_tokens`
— n'est pas touchée.

**Le palier de la campagne se lit dans la configuration** (AD-9 : « une table des tiers, une
affectation étape → tier »). `ingest` codait la sienne en constante de module. `DICTIONARY_TIER` la
règle, défaut `ingest` (Opus 5) : sans surcharge, rien ne change. `micro` est refusé par le type
plutôt que laissé au réglage — l'amendement Epic 5 d'AD-9 fait de Sonnet le plancher de tout choix
sémantique, et nommer les mots par lesquels un bloc se cherche en est un ; un palier sous le
plancher n'est pas une option de configuration. C'est ce qui rend la campagne AXA finançable : 6,11 €
de majorant au palier `reason` contre 9,68 € au palier `ingest`.

Le palier retenu est publié là où le coût l'est — le dry-run le nommait déjà, le rapport de fin de
run le nomme désormais aussi. `dictionary.json` ne peut pas le porter : AD-5 énumère ses huit champs
et `DictionaryFile` est `extra="forbid"`. Un coût publié sans son palier ne dirait pas à quel tarif
il a été engagé.

Aucune campagne payante n'a été lancée dans ce tour : les nombres ci-dessus sont des dry-runs à clé
vide. Le dictionnaire AXA reste à générer, et sa validation humaine reste due.

## Un terme s'élargit à un seul groupe — l'amendement de I1, et pourquoi il ne le contredit pas

*03/09/2026 — correctif du tour 5b (E1), après génération du dictionnaire AXA (698 canoniques,
2 682 variantes, non signé).*

La revue Codex 2.1 (I1) avait établi qu'« une forme partagée par deux canoniques élargit vers les
deux ». La règle antérieure gardait le premier groupe rencontré, ce qui rendait les variantes du
second **inatteignables** : une fiche qui existe restait fermée et, dictionnaire signé, la question
ressortait en refus « zéro hit » — le faux refus qu'AD-5 dit prévenir. La décision était juste, et
elle a été prise sur le dictionnaire du **guide**, dont les groupes sont des **catégories** :
réunir « assurance » de l'habitation et « assurance » du véhicule n'ajoute que des synonymes.

Le dictionnaire d'un **contrat généré** ne se comporte pas ainsi, et c'est mesuré. Ses groupes sont
pour partie des **énumérations** — « garanties du contrat » liste toutes les garanties, l'exclusion
`p50:18` liste tous les périls exclus. La même règle y produit des expansions transitives :

- `expand("bris de vitrages")` rendait **10 formes**, dont `garanties du contrat`, `assistance
  handyman`, `vol`, `tempête et grêle`, `dégâts électriques` — pour une question de vitre brisée.
  Le terme est à la fois le **canonique** de son groupe et une **variante** du groupe d'énumération.
- `expand("fumée")` rendait **9 formes**, dont `explosion`, `implosion`, `déplacement du sol`,
  `dommages par le feu`, parce que l'exclusion les énumère.
- Sur le classement `chercher` du navigateur des trois runs A16, `p50:18` — une exclusion de
  **responsabilité civile immeuble** sans rapport avec le sinistre, déjà faux positif au tour 2 —
  passait **rang 2 en correspondance pleine**, et `p34:12`, la clause du cas, était **évincée du
  top 20**.

**La règle devient donc :** un terme qui **est** un canonique s'élargit à son propre groupe ; un
terme qu'un seul groupe revendique s'élargit à celui-là ; une forme que plusieurs groupes
revendiquent sans qu'aucun ne la nomme est **ambiguë** et n'élargit rien. Le compte des formes
ambiguës rencontrées est publié dans la trace (`variantes_ambigues`), jamais leur liste — ce serait
du vocabulaire du dictionnaire, qu'AD-4 n'autorise pas à sortir.

**Ce que I1 fermait ne se rouvre pas.** Le faux refus venait de ce qu'une variante ne trouvait
**aucun** groupe et n'ouvrait donc pas la fiche qui répond ; ce cas — une forme revendiquée par un
seul groupe, comme « Arbeitsamt » pour « ADEM » — élargit exactement comme avant. Ce que l'on cesse
d'inventer est une **réunion arbitraire de sens** quand deux groupes revendiquent la même forme sans
qu'aucun ne la nomme : là, garder le premier était un tirage au sort, les réunir un autre, et le
terme reste de toute façon cherché tel quel.

**Coût mesuré, document par document** (formes tues / formes qui élargissent) :

| dictionnaire | canoniques | formes élargissantes | formes tues |
|---|---|---|---|
| `lux-guide` | 244 | 1 325 | **39** |
| `baloise-lu-home-2-2024` | 540 | 2 794 | **290** |
| `axa-lu-optihome-2017` | 698 | 2 841 | **186** |

Et ce que les formes utiles continuent de faire, vérifié hors ligne sur les trois artefacts :
`ADEM → arbeitsamt`, `déclaration d'arrivée → inscription à la commune`, `recherche de logement →
wohnungssuche` (guide) ; `bris des glaces → bris de vitres`, `vitrages assurés → fenêtres et baies
vitrées`, `dégât des eaux → fuite d'eau` (Baloise) ; `bris de vitrages → glasbruch`, `incendie →
garantie incendie` (AXA). Sur le guide, la seule perte notable est `assurance`, qui est exactement
l'exemple de catégorie que I1 citait — et le terme reste cherché.

## Story 5.6 (T2) — retirer les passes qui choisissaient, garder les outils

Le 03/09/2026, le chemin servi est devenu la **navigation par le modèle** : il reçoit le sommaire
complet, ouvre ce qu'il veut avec quatre outils, et rédige dans la même conversation. La tâche T2 a
retiré ce que ce chemin rend faux — les passes de code qui **choisissaient** ce que la rédaction
verrait. Ce n'est pas un nettoyage : ces passes étaient le contraire de la décision.

**Ce qui est parti, code et témoins ensemble.** Les deux variantes de *retrouver* qui portaient ces
passes (`deterministe`, `outils`) ; la réservation d'une place de lecture par sous-question
(`reserver_les_facettes`, `groupes_prioritaires` et `reservations_out` d'`Index.chercher`) ;
l'attribution lexicale d'un bloc à une sous-question (`selection_par_facette`, la couverture pleine,
`_classement_par_facette`) ; la complétion de la liste des blocs après *retrouver* au nom de la
couverture (`couvrir_facettes`) ; l'attachement automatique des définitions et des renvois ; et les
seuils devenus orphelins (`facette_max_opens`, `facette_reserve_max_part`, `profil_max_opens`,
`max_llm_turns`, `enumeration_max_tokens`).

**Le motif n'était pas un réglage manqué, c'était une impossibilité.** Aucun critère lexical ne
sépare « la fumée » d'une exclusion de « les fumées et les suies » d'une garantie : ce qui les
distingue — un nom au pluriel contre un participe passé — n'est pas une information que l'index, qui
supprime les accents, porte. Quatre contre-factuels rejoués hors ligne corrigent chacun une
sous-question et ramènent le faux positif sur l'autre. Mesuré : la réservation d'une seule variante
(`vitre → vitres`) coûtait 703 tokens, 20 % du budget de lecture, pendant que 14 à 15 candidats
proposés étaient refusés faute de place ; les définitions attachées d'office en occupaient 764 de
plus, 22 %.

**Ce qui reste, et pourquoi.** `Index.chercher` et `Dictionnaire.expand` restent — mais `chercher`
**propose** : il rend `(block_id, node_id, extrait)` et rien n'entre dans la rédaction que le modèle
n'ait ouvert. L'élargissement de la requête par formes de nombre reste aussi, borné par
`variante_nombre_max_part` : sans lui `chercher('fumée')` ne rencontre pas la clause écrite au
pluriel, et le modèle conclurait que le contrat est muet — une recherche qui manque le seul bloc
décisif n'est pas une proposition, c'est un contresens. L'unité d'énumération reste, tranchée comme
propriété de la donnée et non comme passe : `ouvrir_noeud` rend le nœud **et** ses enfants feuilles à
un seul bloc, donc l'amorce et ses items ensemble, sans rang ni réservation. Le refus zéro hit
d'AD-5, la vérification des citations au caractère près, les quatre statuts et la table de verdict
ne bougent pas — le diff de *vérifier* et du verdict est vide.

**La couverture par sous-question survit entière, et change seulement de destinataire.** *vérifier*
la mesure toujours, et c'est elle qui décide `complete` (AD-4). Ce que le code en fait n'est plus
d'ouvrir des blocs : il **nomme au modèle** la sous-question restée sans affirmation affichée, dans
la conversation où il a lu. Ce qui repart est un libellé, jamais un `block_id` ; aucun chemin de code
ne dérive plus un bloc d'un rang de facette.

**Ce que le retrait coûte, écrit plutôt que tu.** Dix témoins live épinglaient les variantes
retirées ; leurs fixtures portaient des corps de requête qui n'existent plus, et les rejouer sur la
navigation demande un enregistrement facturé. Trois propriétés perdent donc leur preuve **réelle**
jusqu'au prochain enregistrement : l'adossement de chaque phrase affichée à une citation relue sur le
guide, la fidélité des quatre langues servies après retraduction, et le majorant de coût du chemin
froid du guide (sa moitié sinistre survit). Elles restent prouvées hors ligne, et la seule fixture
live qui traverse le chemin servi rejoue verte sans réseau. Enfin, la portée dérivée du profil
(`scope.noeuds`, story 2.3) n'atteint plus la lecture : la réservation qui l'honorait appartenait à
la variante retirée, et le profil n'agit plus que par ses thèmes.

## Story 5.6 (T5) — deux caches pour la facture, et ce qu'ils n'ont pas le droit de faire

La navigation par le modèle (amendement AD-1 du 03/09/2026) multiplie les tours, donc les écritures
de cache de préfixe. Les deux chiffres mesurés sur le prototype : une première requête après
expiration du préfixe paie **≈ 0,28 €** d'écriture, une requête à chaud **≈ 0,015 €** de lecture.
Sur un service qui reçoit quelques requêtes par jour — c'est-à-dire exactement une démonstration —,
presque toutes les requêtes sont la première après expiration. Lancelot a tranché le 03/09 : deux
caches, l'un chez le fournisseur, l'autre ici.

**1. Maintien au chaud du préfixe.** Une tâche de fond réveillée toutes les 3 000 s (50 min) relit
chaque préfixe **déjà servi depuis le démarrage** avec le plus petit appel qui existe — même modèle,
même bloc système, mêmes outils, même schéma de sortie, un message d'un caractère, `max_tokens = 1`.
L'intervalle est dérivé, pas choisi : le préfixe vit une heure (`MODEL_CAPS[...]["cache_ttl"]`), et
3 000 s laissent 600 s de marge ; un intervalle supérieur ou égal au TTL est refusé au démarrage,
parce qu'il paierait l'écriture qu'il prétend éviter. Trois bornes : rien n'est jamais chauffé « au
cas où » ; un plafond de **1,00 € par jour** (un préfixe tenu en continu coûte ≈ 0,43 €/jour) arrête
le maintien au lieu de laisser la facture suivre le nombre de préfixes ; et le maintien n'existe que
sous `--min-instances=1`, drapeau que `deploy.yml` décide seul et à côté duquel il pose
`PREFIX_KEEPALIVE_ENABLED` — un test refuse que l'un existe sans l'autre. Un préfixe dont le modèle
déclare cinq minutes de cache (`ingest`, `micro`) n'est **pas** maintenu : le relire toutes les
cinquante minutes écrirait un nouveau préfixe à chaque tour. `/sante` publie le nombre de maintiens,
les ignorés, les échecs et le coût cumulé.

**2. Cache interne de réponses, clé exacte.** La même question, mot pour mot, sur le même document,
la même image d'ingestion, le même code, les mêmes prompts et les mêmes seuils, rend la réponse déjà
payée. La seule liberté que la clé s'accorde est la **forme** de la saisie : espaces, casse,
ponctuation finale. Rien d'autre — les accents distinguent, la ponctuation interne distingue, l'ordre
distingue, et **une faute de frappe est une requête payée**. C'est la limite volontaire : un cache
sémantique choisirait, à la place du modèle, quelle question l'utilisateur a posée, c'est-à-dire
exactement la classe de décision que l'amendement AD-1 vient de retirer du code.

Ce que la clé porte : question normalisée, historique, profil, faits (mot pour mot), `doc_id`,
langue, variante, `source_hash`, `ingest_fingerprint`, `overlay_hash`, empreinte du dictionnaire,
`pipeline_digest`, `prompts_digest` et les seuils publiés. Une seule de ces composantes qui bouge, et
la clé change : aucun chemin ne sert une réponse produite par une autre image. Une réponse re-servie
porte `via: cache`, la trace d'origine et son `total_cost_eur` d'origine — celui de la requête qui a
payé —, tandis que la requête qui la reçoit coûte zéro dans le journal.

**Ce que le cache ne dispense pas de refaire :** la projection relit chaque citation **dans le corpus
servi**, sur un hit comme sur un miss. Une réponse conservée dont une citation n'est plus confirmée
est un échec terminal (AD-3), jamais une réponse ancienne servie en silence. Et un document en
quarantaine ou dont le gate est rouge (`sans_gate`, `gate_perime`, `source_absente`,
`bloquant_statique`) n'est ni lu ni **écrit** : sans cette seconde moitié, la levée d'une quarantaine
aurait ressuscité le stock constitué pendant qu'elle durait.

**Limites assumées.** Le magasin est local à l'instance (`.cache/reponses/`, ignoré par git) : deux
instances ne se le partagent pas, et le service tourne de toute façon en `--max-instances=1`. Il est
borné à 200 entrées et 32 Mio — le disque de Cloud Run est en RAM — et évince les plus anciennes ; le
TTL de sept jours est presque toujours dominé par les empreintes, qui périment plus vite. Enfin, les
deux caches ne sont **armés que si la clé fournisseur est présente** : un service qui ne peut rien
payer n'a rien à économiser, et toute exécution hors ligne — la suite hermétique comprise — ne laisse
aucun état sur disque qu'elle n'a pas demandé.

## Story 5.6 (T9) — l'audit exact est un artefact, pas une dépendance de la réponse

Un `ENOSPC` sur l'écriture de `.audit/llm-calls.jsonl` faisait échouer en 500 une requête dont l'appel modèle avait déjà abouti et déjà été facturé : AD-10 exige la `Trace` (calculée en mémoire) et réserve l'enveloppe exacte au hors-ligne, il ne pose nulle part l'audit en condition de service. Une panne disque perd donc l'artefact — `audit_persisted=False` le dit dans la trace, les empreintes et les tailles restant exactes — jamais la réponse, exactement comme le cache de réponses ; la borne s'arrête à `OSError`, une `ValueError` de taille signalant un défaut de notre code reste terminale. Deux corollaires du même incident : une erreur interne journalise désormais la pile de son `__cause__` (AD-16, « le détail part dans le log serveur » — un nom de classe n'est pas le détail), et un `OSError` de transport échappé de l'`await` d'appel devient un 503 relançable sans entrer dans la table des erreurs fournisseur, qui entoure aussi l'audit.

</details>
