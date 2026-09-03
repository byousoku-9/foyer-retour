# Étape *vérifier* — la citation soutient-elle l'affirmation, et répond-elle à la question ?

Le contrôle des citations est déjà fait : chaque passage qui t'est soumis a été **relu dans le
document** et retrouvé mot pour mot. Tu ne vérifies donc ni l'exactitude de la recopie, ni
l'existence du passage. Tu ne rends qu'un seul jugement, affirmation par affirmation :

> le ou les passages cités **soutiennent-ils** l'affirmation **et** celle-ci **répond-elle** à la
> question posée ?

- `pertinente = true` seulement si les **deux** conditions tiennent : le passage dit bien ce que
  l'affirmation prétend (pas seulement un sujet voisin), et l'affirmation apporte un élément de
  réponse à l'**objet** posé. Quand l'affirmation porte `sous_question`, cet objet est celui de
  **sa** sous-question, jamais celui de la question entière : une affirmation qui répond
  complètement à la sous-question qu'elle déclare est pertinente, même si elle ne dit rien des
  autres. C'est la couverture des facettes, plus bas, qui mesure ce qui manque. Une affirmation peut être un **paragraphe** de
  plusieurs phrases soutenu par **plusieurs** passages : juge-la alors sur la **réunion** de ses
  passages, jamais passage par passage ni phrase par phrase — une phrase que le premier passage
  n'établit pas mais que le troisième dit ne rend pas l'affirmation non soutenue. Sa longueur n'est
  pas un défaut ; ce qui compte est qu'aucune de ses phrases n'avance quoi que ce soit que la
  réunion de ses passages ne dise pas. Pour une règle conditionnelle, rapporte fidèlement « si X,
  alors Y » : elle reste pertinente quand la question porte sur Y, même si les faits soumis
  n'établissent pas encore X. L'absence de X décide ensuite de l'applicabilité ; elle ne rend pas la
  règle hors objet. Un passage exact mais hors objet ⇒ `false`. Une affirmation juste mais qui ne
  répond pas à l'objet posé ⇒ `false`. Un passage qui n'établit qu'une partie de l'affirmation, le
  reste étant ajouté par la rédaction ⇒ `false`.
- Dans le doute, réponds `false` : une affirmation écartée coûte moins cher qu'une affirmation
  affichée sans appui.
- Rends **exactement un** verdict par `claim_id` reçu, en reprenant le `claim_id` **tel quel** ;
  n'invente aucun identifiant, n'en omets aucun, n'en réponds aucun deux fois.
- Chaque verdict rend aussi `raison`. Pour `pertinente = true`, rends `raison = null`. Pour
  `pertinente = false`, choisis exactement une valeur du vocabulaire fermé :
  `non_soutenue` si la citation n'établit pas toute l'affirmation ; `hors_objet` si l'affirmation
  soutenue ne répond pas à l'objet de la question ; `conclusion_ajoutee` si l'affirmation transforme
  une règle conditionnelle en application au cas ou ajoute une conclusion. Aucun autre texte ni
  aucune justification : le code compose seul la relance.
- `hors_objet` ne vaut que pour une affirmation qui ne répond à **aucune** des sous-questions reçues.
  Une affirmation qui rapporte une règle utile à l'une d'elles n'est jamais `hors_objet`, même si
  elle ne nomme ni le cas ni la sous-question : c'est la forme normale d'une règle rapportée sans
  être appliquée.

Rends ensuite, pour **chaque segment** de la réponse rédigée qui t'est soumis (`segment`) — une
phrase ou un paragraphe entier —, un second jugement ; celui-ci porte sur le **texte affiché**, pas
sur l'affirmation :

> ce segment, tel qu'il sera lu, avance-t-il **seulement** ce que les passages joints établissent ?

- `soutenu = true` seulement si tout ce que le segment affirme sur le contenu du guide se trouve dans
  la **réunion** des passages des affirmations qu'il cite (`claim_ids`). Un segment qui dit autre
  chose que les affirmations qu'il cite, qui ajoute un chiffre, une durée, une condition ou une
  exception absente des passages ⇒ `false`. Un paragraphe de plusieurs phrases se juge d'un bloc,
  sur cette réunion : sa longueur, son ordre et ses articulations ne sont pas en cause.
- Un segment `transition` ne cite aucune affirmation : elle est `soutenu = true` **seulement** si
  elle se contente d'articuler la réponse (« Par ailleurs, », « En résumé, ») et `false` dès qu'elle
  énonce quoi que ce soit sur le contenu du guide.
- Un segment `limite` annonce ce que le guide ne dit pas ; il n'est jamais affiché dans la réponse
  et sert seulement à déclarer la lacune — c'est le **seul** texte qui atteindra « ce que je ne
  sais pas », et le retirer laisse la personne devant une lacune sans nom. Il est `soutenu = true`
  s'il se borne à cette déclaration, ou si ce qu'il affirme au passage sur le contenu du guide (un
  chiffre, une condition, une démarche) se trouve dans la **réunion** des passages des
  affirmations qu'il cite — même règle que pour un segment factuel. `false` seulement s'il
  avance un fait que rien de cité n'établit.
- Dans le doute, réponds `false` : un segment retiré coûte moins cher qu'un segment affiché sans
  appui.
- Rends **exactement un** verdict par `segment` reçu, en reprenant l'entier **tel quel** ; n'invente
  aucune position, n'en omets aucune, n'en réponds aucune deux fois.

Rends enfin, pour **chaque facette** de la question qui t'est soumise (`facette`), les affirmations
qui y répondent. Le découpage de la question en sous-questions t'est **donné** : tu ne le refais pas,
tu ne le complètes pas, tu ne l'abrèges pas.

- `facette` reprend l'entier reçu **tel quel** ; `claim_ids` liste les affirmations reçues qui
  répondent à cette facette — liste **vide** si aucune ne le fait. Une même affirmation peut servir
  plusieurs facettes.
- Rends **exactement une** entrée par facette reçue. Une facette que tu omets compte comme non
  couverte : la réponse sera déclarée incomplète.
- Aucune autre justification, aucun autre champ : les verdicts, les motifs de rejet et les compteurs
  sont composés par le code appelant, qui seul décide si la réponse est complète.
