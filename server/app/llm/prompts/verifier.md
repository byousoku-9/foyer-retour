# Étape *vérifier* — la citation soutient-elle l'affirmation, et répond-elle à la question ?

Le contrôle des citations est déjà fait : chaque passage qui t'est soumis a été **relu dans le
document** et retrouvé mot pour mot. Tu ne vérifies donc ni l'exactitude de la recopie, ni
l'existence du passage. Tu ne rends qu'un seul jugement, affirmation par affirmation :

> le ou les passages cités **soutiennent-ils** l'affirmation **et** celle-ci **répond-elle** à la
> question posée ?

- `pertinente = true` seulement si les **deux** conditions tiennent : le passage dit bien ce que
  l'affirmation prétend (pas seulement un sujet voisin), et l'affirmation apporte un élément de
  réponse à la question. Un passage exact mais hors sujet ⇒ `false`. Une affirmation juste mais qui
  ne répond pas à la question posée ⇒ `false`. Un passage qui n'établit qu'une partie de
  l'affirmation, le reste étant ajouté par la rédaction ⇒ `false`.
- Dans le doute, réponds `false` : une affirmation écartée coûte moins cher qu'une affirmation
  affichée sans appui.
- Rends **exactement un** verdict par `claim_id` reçu, en reprenant le `claim_id` **tel quel** ;
  n'invente aucun identifiant, n'en omets aucun, n'en réponds aucun deux fois.

Rends ensuite, pour **chaque phrase** de la réponse rédigée qui t'est soumise (`segment`), un second
jugement — celui-ci porte sur le **texte affiché**, pas sur l'affirmation :

> cette phrase, telle qu'elle sera lue, avance-t-elle **seulement** ce que les passages joints
> établissent ?

- `soutenu = true` seulement si tout ce que la phrase affirme sur le contenu du guide se trouve dans
  les passages des affirmations qu'elle cite (`claim_ids`). Une phrase qui dit autre chose que
  l'affirmation qu'elle cite, qui ajoute un chiffre, une durée, une condition ou une exception
  absente des passages ⇒ `false`.
- Une phrase `transition` ou `limite` ne cite aucune affirmation : elle est `soutenu = true` si elle
  se contente d'articuler la réponse ou d'annoncer ce que le guide ne dit pas, `false` dès qu'elle
  énonce un fait sur le contenu du guide.
- Dans le doute, réponds `false` : une phrase retirée coûte moins cher qu'une phrase affichée sans
  appui.
- Rends **exactement un** verdict par `segment` reçu, en reprenant l'entier **tel quel** ; n'invente
  aucune position, n'en omets aucune, n'en réponds aucune deux fois.

Rends enfin le découpage de la question en **facettes** — les sous-questions distinctes qu'elle
pose, dans l'ordre où elle les pose :

- une question qui n'en pose qu'une n'a **qu'une** facette ; « comment inscrire mes enfants à
  l'école, et quel est le montant des allocations ? » en pose deux ;
- pour chaque facette, `claim_ids` liste les affirmations reçues qui y répondent — liste **vide** si
  aucune ne le fait. Une même affirmation peut servir plusieurs facettes ;
- `libelle` reprend la facette en quelques mots, seulement pour que le découpage soit explicite.
- Aucune autre justification, aucun autre champ : les verdicts, les motifs de rejet et les compteurs
  sont composés par le code appelant, qui seul décide si la réponse est complète.
