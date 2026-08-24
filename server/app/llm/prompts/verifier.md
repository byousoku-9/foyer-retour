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
- Aucun texte libre, aucune justification, aucun autre champ : le motif de rejet et les compteurs
  sont composés par le code appelant.
