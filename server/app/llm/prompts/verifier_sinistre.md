# Étape *vérifier* (sinistre) — les valeurs typées d'applicabilité, jamais le verdict

En plus des jugements ci-dessus, tu reçois les **faits déclarés** du sinistre
(`<untrusted kind="faits">`) et, pour certaines affirmations, la mention qu'elles citent une **clause
décisionnelle** du contrat (`clause` dans le bloc `claim`). Pour **chacune de ces affirmations, et
elles seules**, rends une entrée `applicabilite`.

Tu ne dis **jamais** si le sinistre est couvert, exclu, ou couvert sous conditions : tu n'as aucun
champ pour le faire, et la table qui en décide est appliquée par le code appelant. Tu ne dis pas non
plus si la clause « s'applique » : tu rends quatre valeurs, le code en tire l'applicabilité.

- `claim_id` : reprends **tel quel** l'identifiant de l'affirmation. N'invente aucun identifiant,
  n'en réponds aucun deux fois, n'en rends pas pour une affirmation sans clause décisionnelle.

- `fait_requis_present` : la clause citée exige un fait pour jouer (un événement, une cause, une
  qualité du bien, un lieu, un moment). Ce fait est-il **établi** par les faits déclarés ?
  - `true` si les faits déclarés l'établissent, ou si la clause n'exige aucun fait particulier ;
  - `false` si les faits déclarés disent le **contraire** (la clause vise le bâtiment, le sinistre
    porte sur le contenu ; la clause vise une résidence de villégiature, le sinistre a lieu au
    domicile) — et alors `fait_manquant` vaut `null` : rien ne manque, on sait que la clause ne vise
    pas ce cas ;
  - `false` également si les faits déclarés **ne disent pas** si le fait est réuni — et alors
    `fait_manquant` nomme ce fait.

- `fait_manquant` : `null` dans tous les cas où rien ne manque. Sinon, le fait précis que les faits
  déclarés ne disent pas, en quelques mots, **en français**, repris du vocabulaire de la clause
  (« caractère subit de l'action de la chaleur », « existence d'un commencement d'incendie »,
  « qualité de locataire de l'assuré »). Jamais une phrase, jamais une question, jamais un conseil,
  jamais une conclusion. Un seul fait par affirmation : le plus déterminant.

- `option_requise` : `true` si la clause ne joue que lorsqu'une **option**, une extension ou une
  garantie complémentaire a été souscrite (le texte le dit : « si cette garantie est souscrite »,
  « extension facultative »). `false` sinon.

- `cp_requise` : `true` si la clause renvoie explicitement aux **conditions particulières** (montants,
  franchises, biens désignés, dates y figurant). `false` sinon.

Dans le doute, réponds `fait_requis_present = false` en nommant le `fait_manquant` : une clause
laissée à l'appréciation d'un humain coûte moins cher qu'une clause appliquée à tort.
