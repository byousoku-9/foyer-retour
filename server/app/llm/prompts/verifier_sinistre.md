# Étape *vérifier* (sinistre) — les valeurs typées d'applicabilité, jamais le verdict

En plus des jugements ci-dessus, tu reçois les **faits déclarés** du sinistre
(`<untrusted kind="faits">`) et, pour certaines affirmations, la mention qu'elles citent une **clause
décisionnelle** du contrat (`clause` dans le bloc `claim`). Pour **chacune de ces affirmations, et
elles seules**, rends une entrée `applicabilite`.

Tu ne dis **jamais** si le sinistre est couvert, exclu, ou couvert sous conditions : tu n'as aucun
champ pour le faire, et la table qui en décide est appliquée par le code appelant. Tu ne dis pas non
plus si la clause « s'applique » : tu rends des valeurs typées et deux listes de libellés, le code
en tire l'applicabilité.

Pour le jugement `pertinente` demandé par les instructions communes, une clause qui définit le
**sens de la responsabilité** (qui est l'Assuré, qui est le tiers, qui cause le dommage) apporte bien
un élément de réponse à une question qui compare deux responsabilités civiles, y compris quand ce
sens est l'inverse de celui du sinistre déclaré. Si l'affirmation reste exactement soutenue par la
quote, rends `pertinente = true` ; la question de périmètre ci-dessous enregistrera ensuite que la
clause ne vise pas le cas. Ne rejette pas cette preuve de sens comme « hors sujet ».

Quand un segment factuel reprend exactement `Claim.text`, ses jugements ne posent pas deux questions
différentes : si la quote soutient cette claim et qu'elle répond à la question (`pertinente=true`),
le segment porte la même valeur de vérité (`soutenu=true`). Inversement, une claim non pertinente ne
rend pas son segment soutenu. Ne produis jamais deux booléens opposés pour le même texte.

Exemple normatif : si un tiers a cassé le bien de l'Assuré et que la question demande « sa RC ou
mon contrat ? », l'affirmation « la RC vie privée fournie vise les dommages que l'Assuré cause à des
tiers » est `pertinente = true` lorsqu'elle est soutenue par sa quote. Elle répond en établissant le
sens de cette clause, sans conclure sur le contrat du tiers. Dans les champs d'applicabilité, son
périmètre est en revanche contraire au cas déclaré (`fait_requis_present = false`).

- `claim_id` : reprends **tel quel** l'identifiant de l'affirmation. N'invente aucun identifiant,
  n'en réponds aucun deux fois, n'en rends pas pour une affirmation sans clause décisionnelle.

### `fait_requis_present` et `fait_manquant` : deux questions, dans cet ordre

Une clause ne joue que si des faits sont réunis. Pose-toi les deux questions ci-dessous **dans cet
ordre**, et n'examine pas les suivantes après la première qui répond.

**1. Le périmètre.** La clause vise-t-elle le bien, le lieu et la situation du sinistre décrit ? Si
les faits déclarés disent le **contraire**, alors `fait_requis_present = false` et `fait_manquant =
null` — rien ne manque, on **sait** que la clause ne vise pas ce cas. C'est le cas quand :

  - la clause vise le **bâtiment** et le sinistre porte sur le **contenu** (ou l'inverse) ;
  - la clause ne vaut que pour des **extensions** nommément désignées (résidence de villégiature,
    chambre d'étudiant, maison de repos, garage, local occupé pour une fête, sépulture, nouvelle
    adresse…) et le sinistre a lieu au domicile assuré — ou l'inverse ;
  - la clause vise une autre nature d'événement, une autre qualité d'assuré, un autre moment.

  Le périmètre prime : une clause qui ne vise pas ce cas n'a pas à être examinée plus loin, même si
  l'événement qu'elle décrit ressemble à celui du dossier.

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
  la question 1), ni une option, ni un renvoi aux conditions particulières.

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
