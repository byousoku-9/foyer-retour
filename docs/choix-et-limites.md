# Choix et limites mesurées

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
- `STRUCTURE_MAX_COST_EUR=5.0` — majorant du seul appel, vérifié **avant** toute construction de
  client ; `--max-cost` le surcharge pour un run.

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
`tests/test_parsing_axa.py` et `tests/test_parsing_baloise.py`) :

- `empreinte-committee-perimee: axa-lu-optihome-2017`
- `empreinte-committee-perimee: baloise-lu-home-2-2024`

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
