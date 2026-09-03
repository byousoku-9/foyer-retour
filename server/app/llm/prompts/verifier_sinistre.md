# Étape *vérifier* (sinistre) — les valeurs typées d'applicabilité, jamais le verdict

En plus des jugements ci-dessus, tu reçois les **faits déclarés** du sinistre
(`<untrusted kind="faits">`) et, pour certaines affirmations, la mention qu'elles citent une **clause
décisionnelle** du contrat (`clause` dans le bloc `claim`). Pour **chacune de ces affirmations, et
elles seules**, rends une entrée `applicabilite`.

Tu ne dis **jamais** si le sinistre est couvert, exclu, ou couvert sous conditions : tu n'as aucun
champ pour le faire, et la table qui en décide est appliquée par le code appelant. Tu ne dis pas non
plus si la clause « s'applique » : tu rends des valeurs typées et deux listes de libellés, le code
en tire l'applicabilité.

Pour le jugement `pertinente` demandé par les instructions communes, sépare toujours l'objet
contractuel de la question et l'applicabilité au dossier. Une affirmation qui rapporte fidèlement la
règle conditionnelle d'une clause répond à l'objet dès que cette règle traite la garantie,
l'exclusion ou la responsabilité interrogée, même si une qualité exigée manque dans les faits ou si
le périmètre est contraire. Rends alors `pertinente = true` : les champs ci-dessous enregistreront
ensuite `humain` pour une qualité manquante ou `non` pour un périmètre contraire. Ne transforme
jamais cette règle en « la clause s'applique » : sans preuve dans les faits ou dans la quote, cette
conclusion ajoutée vaut `pertinente = false` avec `raison = conclusion_ajoutee`.

La suffisance de la quote se lit grammaticalement : la citation retenue doit porter à la fois le
**sujet** dont l'affirmation parle et l'**opérateur** de couverture qu'elle revendique (couvre,
exclut, est garanti, est soumis à condition…). Un passage qui n'établit qu'une partie — le sujet
sans l'opérateur, ou l'opérateur sur un autre sujet — n'établit pas l'affirmation : rends
`pertinente = false` avec `raison = non_soutenue`, quel que soit le voisinage thématique.

Cette suffisance se lit **phrase par phrase** quand l'affirmation arrive découpée en unités
numérotées (`phrases`) : une unité dont le sujet ou l'opérateur manque à la réunion des passages est
un rang de `phrases_non_soutenues`, et non un rejet de l'affirmation entière. L'affirmation n'est
`pertinente = false, raison = non_soutenue` que si **aucune** de ses unités ne tient. Une claim de
sinistre n'en porte le plus souvent qu'une seule : la règle est alors exactement celle d'avant.

Le champ `clause` est le typage contractuel du passage, relu du corpus, et `clause_confirmee` dit
si ce typage est confirmé. Ils servent l'applicabilité que tu rends ; ni l'un ni l'autre ne
constitue une preuve textuelle, et la structure du contrat (rubrique parente, sommaire) n'en est
pas une non plus. La suffisance grammaticale s'applique sans exception : pour un item de liste
grammaticalement incomplet, l'affirmation n'est retenue que si un passage cité joint porte
réellement le sujet et l'opérateur qu'elle revendique — sinon rends `pertinente = false` avec
`raison = non_soutenue`. Une claim qui dit qu'une `exclusion` garantit ou couvre ce qu'elle
énumère — ou qu'une `garantie` l'exclut — inverse la règle : rends `pertinente = false`. Ne déduis
jamais l'opérateur de la seule étiquette ni de la seule structure.

Quand un segment factuel reprend exactement `Claim.text`, ses jugements ne posent pas deux questions
différentes : si la quote soutient cette claim et qu'elle répond à la question (`pertinente=true`),
le segment porte la même valeur de vérité (`soutenu=true`). Inversement, une claim non pertinente ne
rend pas son segment soutenu. Contrôle cette cohérence juste avant de rendre le JSON : pour un segment
factuel byte-identique à une claim elle-même byte-identique à sa quote, la paire
`pertinente=true, soutenu=false` est impossible. Ne produis jamais deux booléens opposés pour le même
texte.

Exemple normatif, étranger par construction à tout cas soumis : une clause couvre les dommages à un
bien **si** le choc qui l'atteint est violent. Si les faits disent seulement qu'un vase a basculé
sur le tapis, l'affirmation « la clause vise les dommages causés par un choc violent » reste
pertinente lorsqu'elle est soutenue par sa quote ; l'applicabilité vaut ensuite `humain`, car le
caractère violent du choc reste à établir. Écrire « ce dommage est couvert » ajoute au contraire une
conclusion que ni la quote ni les faits n'établissent. La règle vaut à l'identique quels que soient
le bien, l'événement et la qualité exigée : elle ne dépend d'aucun objet ni d'aucune condition
particulière.

Le verbe « couvrir » ne suffit donc pas à transformer une règle en conclusion : « le contrat couvre
les dommages **causés par un choc violent** » rapporte encore la règle conditionnelle et vaut
`pertinente = true` si la quote l'établit. Seule une formulation qui rattache cette couverture au
dossier (« ce dommage est couvert », « cette clause s'applique au sinistre décrit ») ajoute
l'application au cas.

**Le `rattachement` se lit à part de l'affirmation, et ne la juge jamais.** Un bloc `claim` peut
porter, à côté de son `affirmation`, un champ `rattachement` : une phrase qui nomme un fait déclaré
dans le **vocabulaire de la clause**. Troisième exemple normatif, étranger par construction à tout
cas soumis — la clause vise « l'affaissement du sol par suite de glissement ou d'effondrement de
terrain », les faits disent que le talus a cédé sous la terrasse, et le rattachement dit « Le talus
qui a cédé sous la terrasse est un glissement de terrain. ». Le sujet y est le fait, le prédicat le mot
que la clause écrit, et rien n'y est dit de la couverture.

Ce champ **ne fait pas partie de ce que les citations doivent soutenir**. Ton verdict `pertinente`
porte sur `affirmation` seule : ne rends ni `non_soutenue`, ni `conclusion_ajoutee` parce que le
rattachement dépasse ce que la quote établit — aucune quote ne pourra jamais établir ce qu'un assuré
a vécu, et rejeter l'affirmation pour cela ferait tomber la clause avec le lien. Une clause dont
l'affirmation est soutenue reste retenue quel que soit son rattachement.

C'est dans les **champs typés** ci-dessous que le rattachement se lit, et de trois façons :

- il se déduit des faits déclarés et n'emploie aucun **qualificatif** du contrat : la clause vise ce
  cas, le fait est présent — n'écris pas en `fait_manquant` le nom que le contrat donne à ce que le
  client vient de décrire ;
- il passe par un **qualificatif** (soudain, subit, accidentel, direct, immédiat, violent, avec
  effraction…) que les faits ne disent pas : la qualité reste à établir — nomme-la en
  `fait_manquant` ou en `qualites_exigees`, et le client la confirmera. L'affirmation, elle, reste
  `pertinente = true` : un appareil de chauffage posé près d'un rideau ne qualifie pas de *subite*
  l'action de la chaleur, mais la clause qui l'exige a bien été lue et rapportée ;
- les faits déclarés disent le **contraire** de ce que le rattachement affirme : c'est un périmètre
  contraire, `fait_requis_present = false` et `fait_manquant = null` (question 1 ci-dessous).

Un rattachement absent ne vaut aucun de ces trois cas : juge alors sur les seuls faits déclarés,
comme s'il n'y en avait pas.

Procède littéralement dans cet ordre : (1) relève toutes les conditions conservées dans
l'affirmation ; (2) demande-toi si son sujet est seulement le contrat/la clause et une catégorie de
dommages, ou si elle désigne le dossier reçu ; (3) n'utilise les faits que pour `applicabilite`. Une
affirmation dont le sujet est le contrat et qui conserve les conditions de la quote est la règle,
pas le verdict demandé par la question — même si elle emploie « couvre ». Ne rends jamais
`conclusion_ajoutee` pour cette seule forme.

**La clause voisine, citée pour montrer la limite, répond à l'objet.** Quand aucune clause lue ne
vise le dommage déclaré, l'affirmation qui rapporte ce qu'une clause **proche** couvre réellement est
ce qui répond à la question : c'est elle qui montre où passe la limite. Elle ne nomme ni le cas ni la
sous-question — c'est ce qu'on demande à la rédaction — et elle n'est pas `hors_objet` pour autant.
Son périmètre contraire se dit dans ses champs typés (question 1), pas dans son verdict de
pertinence.

L'objet peut être une décision finale **ou** l'identification des catégories de dommages, garanties,
exclusions ou règles à examiner. Dans ce second cas, une règle soutenue qui traite l'un des dommages
déclarés répond déjà à l'objet : elle reste `pertinente = true`, même si elle ne rattache pas encore
ce dommage à la catégorie contractuelle et même si une autre facette reste sans réponse. La
couverture des facettes mesure séparément ce qui manque ; elle ne rend pas hors objet la règle utile
à une seule facette.

Second exemple normatif, étranger par construction à tout cas soumis : une clause dit que la garantie
joue **même lorsque** tel événement n'a pas eu lieu, et les faits déclarés écartent justement cet
événement. L'affirmation qui rapporte cette clause ne nomme ni le bien ni la sous-question — elle
énonce une règle. Elle est `pertinente = true` : c'est elle qui dit que la garantie tient malgré
cette absence. La formuler sans nommer le cas est ce qu'on demande à la rédaction ; ce n'est jamais
un motif de `hors_objet`.

Contrôle enfin cette cohérence avant de rendre le JSON. Renseigner pour une affirmation un
`fait_manquant`, ou une seule `qualite_exigee`, c'est avoir déjà jugé que sa clause vise ce cas : son
verdict ne peut alors pas être `hors_objet`. De même, une affirmation que tu ranges dans une facette
y répond : elle ne peut pas être `hors_objet`.

**Un périmètre contraire n'est pas un hors-objet**, et confondre les deux est l'erreur la plus
coûteuse de ce formulaire. Ce sont deux questions différentes, posées dans deux champs différents :
« cette clause vise-t-elle ce sinistre ? » se répond dans `fait_requis_present` ; « cette
affirmation répond-elle à la question posée ? » se répond dans `pertinente`. Une clause **voisine** —
la garantie dont l'énumération ne comprend pas l'événement, la responsabilité civile qui ne couvre
que les dommages causés à autrui — est précisément ce qui répond quand aucune clause ne vise le
dommage : c'est elle qui montre où passe la limite. Elle est `pertinente = true` avec un périmètre
contraire dans ses champs typés. `hors_objet` reste réservé à l'affirmation qui parle d'autre chose
que de la demande — un délai de résiliation quand on demande une couverture.

- `claim_id` : reprends **tel quel** l'identifiant de l'affirmation. N'invente aucun identifiant,
  n'en réponds aucun deux fois, n'en rends pas pour une affirmation sans clause décisionnelle.

### `fait_requis_present` et `fait_manquant` : deux questions, dans cet ordre

Une clause ne joue que si des faits sont réunis. Pose-toi les deux questions ci-dessous **dans cet
ordre**, et n'examine pas les suivantes après la première qui répond.

**1. Le périmètre.** La clause vise-t-elle le bien, le lieu et la situation du sinistre décrit ?

Si les faits déclarés disent le **contraire**, alors `fait_requis_present = false` et `fait_manquant =
null` — rien ne manque, on **sait** que la clause ne vise pas ce cas. C'est le cas quand :

  - la clause vise le **bâtiment** et le sinistre porte sur le **contenu** (ou l'inverse) ;
  - la clause ne vaut que pour des **extensions** nommément désignées (résidence de villégiature,
    chambre d'étudiant, maison de repos, garage, local occupé pour une fête, sépulture, nouvelle
    adresse…) et le sinistre a lieu au domicile assuré — ou l'inverse ;
  - la clause vise une autre nature d'événement, une autre qualité d'assuré, un autre moment.

  Le périmètre prime : une clause qui ne vise pas ce cas n'a pas à être examinée plus loin, même si
  l'événement qu'elle décrit ressemble à celui du dossier.

  Le périmètre se tranche **sur les faits**, dans les deux sens, et il ne se renvoie jamais au
  client. Reprenons l'exemple étranger du glissement de terrain : une clause qui vise
  « l'affaissement du sol par suite de glissement ou d'effondrement de terrain » et des faits qui
  disent un talus cédé sous la terrasse décrivent le même événement — le périmètre est bon, et
  `fait_manquant` ne nomme pas « le glissement de terrain ». Ne demande jamais au client de
  confirmer ce que sa propre déclaration énonce : ce qui reste à établir, ce sont les
  **qualificatifs** de la question 2, pas le nom que le contrat donne à ce qu'il a décrit.

  L'inverse est tout aussi tranché, et il est plus fréquent. Une clause qui **énumère** ce que le
  contrat garantit — une liste de périls, une puce par garantie — et dont l'énumération ne comprend
  pas l'événement déclaré a un périmètre **contraire** : `fait_requis_present = false`,
  `fait_manquant = null`. Il en va de même d'une clause d'objet ou de préambule, qui annonce ce que
  le contrat fait sans nommer aucun péril : elle ne vise aucun sinistre en particulier, donc pas
  celui-ci. Ces clauses se citent — c'est même souvent elles qui montrent la limite —, mais elles ne
  visent pas ce cas et ne doivent jamais passer pour la garantie qui l'attrape.

  Une clause qui se limite à des **points nommés du contrat** (« pour les extensions mentionnées aux
  points 3.1.8.3 à 3.1.8.6 ») est une question de périmètre, et **les faits déclarés la tranchent** :
  le lieu et le bien disent si le sinistre relève de ces points. Ce n'est ni un `fait_manquant`, ni un
  renvoi aux conditions particulières — n'écris pas `cp_requise = true` pour dire « je ne sais pas si
  cette clause vise ce cas ». Si le sinistre a lieu au domicile assuré et que la clause ne vaut que
  pour des extensions désignées, alors `fait_requis_present = false` et `fait_manquant = null`.

**2. La qualité exigée.** Si le périmètre est bon, la clause subordonne-t-elle son effet à une
**qualité** de l'événement, du bien ou de l'assuré — soudain, subit, accidentel, imprévisible,
permanent, direct et immédiat, avec effraction, à titre principal… ? Si les faits déclarés ne
l'établissent pas **dans ces termes**, alors `fait_requis_present = false` et `fait_manquant` **nomme
cette qualité**. N'infère jamais une qualité des circonstances : « un vase a basculé sur le tapis »
dit ce qui s'est passé, pas que le choc ait été *violent* au sens de la
clause, ni que l'événement ait été *soudain*. C'est précisément ce qu'il faut faire préciser au
client.

**Sinon**, `fait_requis_present = true` et `fait_manquant = null` : le périmètre est bon et la qualité
exigée est établie — ou la clause n'exige aucun fait particulier.

Forme du `fait_manquant` : quelques mots, **en français**, repris du vocabulaire de la clause
(« caractère violent du choc », « contact direct avec le bien »,
« qualité de locataire de l'assuré »). Jamais une phrase, jamais une question, jamais un conseil,
jamais une conclusion. Un seul fait par affirmation : le plus déterminant. **Au plus
$fait_manquant_max_chars caractères** : au-delà, le code appelant ignore **tous** tes champs typés
pour cette affirmation et la renvoie à un humain — le fait manquant disparaît alors des questions
posées au client, ce qui est pire que de l'écrire court. Va donc droit au fait, sans le reformuler ni
l'expliquer.

### `qualites_exigees` et `qualites_etablies` : tu énumères et tu cites, le code compare

Ces deux listes sont le contrôle de la précédente : elles rendent vérifiable ce que
`fait_requis_present` se contente d'affirmer. Elles portent sur **la clause de cette affirmation-là**,
jamais sur une autre : deux affirmations qui citent deux clauses différentes n'ont aucune raison
d'avoir les mêmes qualités.

**Les deux listes sont obligatoires.** Une clause qui n'exige aucune qualité particulière rend
`"qualites_exigees": []` — la liste vide, écrite. Omettre l'une des deux listes n'est pas neutre : le
code appelant ignore alors **tous** tes champs typés pour cette affirmation et la renvoie à un humain.

**La liste vide ne te dispense de rien.** Le code appelant relit le **texte de la clause** : tout
qualificatif qu'elle écrit (soudain, subit, accidentel, fortuit, imprévisible, involontaire,
intentionnel, direct, immédiat, permanent, violent, anormal, avec effraction…) et que tu n'as nommé
nulle part — ni dans `qualites_exigees`, ni dans `qualites_etablies`, ni dans `fait_manquant` — est
ajouté par le code aux qualités **non établies**, et l'affirmation est renvoyée à un humain. Énumère
donc ce que la clause écrit : tu n'as rien à gagner à passer une qualité sous silence.

**Si le périmètre n'est pas bon (question 1), les deux listes sont vides** (`[]`, écrites). Une clause
qui ne vise pas ce cas n'exige rien de lui : le périmètre prime ici comme partout. Ne recopie pas les
qualités d'une clause qui, elle, vise le cas.

- `qualites_exigees` : les qualités que **la clause citée** subordonne à l'événement, au bien ou à
  l'assuré, chacune en quelques mots repris du **vocabulaire de la clause** (« caractère subit de
  l'action de la chaleur », « caractère soudain de l'événement », « effraction », « occupation
  permanente du bien »). N'y mets ni le périmètre (le bien, le lieu, la nature de l'événement : c'est
  la question 1), ni une option, ni un renvoi aux conditions particulières. Une énumération de
  formes que le sinistre peut prendre (« glissement ou effondrement de terrain ») est la **nature de
  l'événement**, donc la question 1 : elle n'entre pas dans cette liste.

- `qualites_etablies` : parmi les précédentes, et **uniquement** parmi elles, celles que les faits
  déclarés établissent **dans ces termes**. Chaque entrée est un objet à deux champs :

  - `qualite` : la qualité, reprise **mot pour mot** de `qualites_exigees` ;
  - `fait_cite` : le **fragment des faits déclarés** qui l'établit, recopié **caractère pour
    caractère** depuis `<untrusted kind="faits">`. Le code le relit dans les faits soumis : un
    fragment introuvable, reformulé ou résumé fait tomber la qualité en « non établie ». Ne cite que
    ce que les faits disent — ni la clause, ni ton raisonnement, ni une paraphrase.

    Le code vérifie aussi que le fragment emploie **chacun** des mots de la qualité. « Un vase a
    basculé sur le tapis » ne contient ni *violent*, ni *direct*, ni *immédiat* : il
    n'établit aucune de ces qualités, et le citer trois fois pour trois qualités différentes les fait
    toutes tomber. Un mot partagé ne suffit pas non plus : « la chaleur a agi lentement » parle bien
    de la *chaleur*, mais il dit le contraire de l'*action subite de la chaleur* — il ne l'établit
    pas. Si les faits ne disent pas la qualité **avec tous ses mots**, elle n'est pas établie : c'est
    précisément ce qu'il faut faire préciser au client.

  Une qualité que tu n'as pas trouvée dans les faits déclarés n'y figure pas — ne l'infère jamais des
  circonstances : « un vase a basculé sur le tapis » ne dit pas que le choc ait été *violent* au
  sens de la clause, et « le tapis est abîmé » ne dit pas qu'il ait été atteint de façon *directe*.
  Dans le doute, **ne l'y mets pas** : une qualité laissée à confirmer coûte moins
  cher qu'une qualité tenue pour acquise.

Le code appelant fait la différence des deux listes : toute qualité exigée qui n'est pas établie —
parce que tu ne l'as pas listée, ou parce que son `fait_cite` ne se relit pas dans les faits — rend
l'affirmation incertaine et devient une question posée au client, **même si tu as coché
`fait_requis_present = true`**. Tu n'as donc rien à gagner à trancher : énumère juste, et cite.

**Au plus $qualites_exigees_max qualités par liste, au plus $fait_manquant_max_chars caractères par
libellé (`qualite` comme `fait_cite`).** Au-delà, l'ensemble de tes champs typés pour cette
affirmation est ignoré et la clause est renvoyée à un humain.

### Les deux autres champs

- `option_requise` : `true` si la clause ne joue que lorsqu'une **option**, une extension ou une
  garantie complémentaire a été souscrite (le texte le dit : « si cette garantie est souscrite »,
  « extension facultative »). `false` sinon.

- `cp_requise` : `true` si la clause renvoie explicitement aux **conditions particulières** (montants,
  franchises, biens désignés, dates y figurant). `false` sinon.

Dans le doute, réponds `fait_requis_present = false` en nommant le `fait_manquant` : une clause
laissée à l'appréciation d'un humain coûte moins cher qu'une clause appliquée à tort.

### `demande_contexte` : dire ce qui te manque plutôt que juger sans

Il arrive que tu ne puisses pas contrôler une affirmation parce qu'un élément du texte te manque :
un mot employé par la clause dont tu n'as pas la définition, un renvoi dont tu n'as pas la cible, ou
une qualité que tu viens d'énumérer et que rien de ce que tu as sous les yeux ne permet de confronter
aux faits déclarés. **Ne devine pas.** Rends le champ `demande_contexte` et laisse tous tes autres
jugements inchangés.

Le code appelant ira chercher lui-même, dans le contrat, ce que tu auras nommé, puis te fera relire
une fois — et une seule. Il ne te posera aucune question et n'attend de toi aucune conclusion.
L'affirmation visée est, entre-temps, renvoyée à un humain : demander du contexte n'est jamais un
moyen de faire passer une clause, c'est un moyen de ne pas la trancher à l'aveugle.

Quatre champs, tous obligatoires :

- `kind` : `definition` (il te manque ce qu'un terme désigne dans ce contrat), `renvoi` (un passage
  qui t'a été fourni renvoie ailleurs et tu n'as pas lu la cible), `qualite` (tu ne peux pas vérifier
  qu'une qualité exigée est établie). Aucune autre valeur n'existe.
- `cible` : **reprise de ce qui t'a été soumis**, et de rien d'autre.
  - `definition` → le terme, recopié du texte que tu as reçu ;
  - `renvoi` → le `block_id` d'un passage joint à une affirmation, tel quel ;
  - `qualite` → une qualité que tu as toi-même écrite dans `qualites_exigees` de cette affirmation,
    mot pour mot.

  Une cible que le code ne retrouve pas dans ce qu'il t'a envoyé ne peut pas être satisfaite : la
  demande entière est ignorée et l'affirmation reste renvoyée à un humain. N'invente donc rien, ne
  reformule pas, et ne nomme aucun identifiant que tu n'as pas reçu.
- `claim_id` : l'affirmation que ce manque empêche de contrôler, reprise **telle quelle**.
- `raison` : `definition_manquante`, `renvoi_non_lu` ou `qualite_non_verifiable`. Aucune autre valeur
  n'existe.

**Une seule demande pour toute la réponse, et une seule pour toute la requête.** Si la relecture ne
suffit pas, n'en redemande pas : le code refuse la seconde, sans la satisfaire. Si rien ne te manque,
**omets le champ** — une demande sans manque réel coûte une lecture et laisse une affirmation
incertaine pour rien.
