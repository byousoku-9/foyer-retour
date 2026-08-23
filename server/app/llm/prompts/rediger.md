# Étape *rédiger* — une ébauche sourcée

Tu rédiges une ébauche de réponse (`AnswerDraft`) à partir, **exclusivement**, des blocs de document
fournis dans le message — chaque bloc ouvre par son `block_id` sur sa première ligne. Le sommaire qui
suit ces instructions sert seulement à situer les blocs dans le document ; il ne se cite jamais.

- `segments` : la réponse découpée en segments dans l'ordre de lecture, chacun typé :
  - `factuel` — affirme quelque chose que les blocs fournis soutiennent ; porte **au moins un**
    `claim_ids` ;
  - `transition` — articulation sans contenu factuel ; `claim_ids` vide ;
  - `limite` — ce que les blocs fournis ne permettent pas d'affirmer ; `claim_ids` vide.
- `claims` : une entrée par affirmation vérifiable — `claim_id` unique (`c1`, `c2`, …), `text` = la
  phrase affirmée, `quotes` = une à trois citations qui la soutiennent.
- Chaque `quote` est **recopiée mot pour mot, caractère par caractère**, depuis le texte d'un bloc
  fourni — jamais du sommaire, jamais reformulée, abrégée ni traduite — avec le `block_id` exact du
  bloc d'origine. Au plus **une quote par bloc** dans une même claim : si un même bloc soutient deux
  idées, choisis le passage qui les couvre toutes deux, ou fais-en deux claims distinctes. Vise au
  moins 25 caractères.
- N'utilise que des `block_id` présents dans le message ; n'invente ni bloc, ni chiffre : **aucun
  calcul numérique** de ton cru (pas d'addition, de conversion, de comparaison chiffrée) — recopie
  les valeurs telles qu'écrites dans les blocs.
- Rédige les segments dans la langue de rédaction indiquée en fin de message ; les quotes restent
  dans la langue du bloc.
- Ce que les blocs ne couvrent pas se dit dans un segment `limite` — n'affirme jamais qu'une
  information est absente du document entier.
- Si un motif de relance est indiqué en fin de message, corrige précisément ce qu'il décrit et
  change réellement l'ébauche.
- **La place est comptée** : au plus six segments et quatre claims, une à deux quotes par claim, des
  quotes de 25 à 250 caractères. Va au fait dès le premier segment — une ébauche coupée en cours de
  route est perdue, elle ne se reprend pas.
