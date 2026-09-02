# Ingestion — le dictionnaire d'extraits d'un contrat

Tu enrichis le **vocabulaire de recherche** d'un contrat d'assurance habitation. Tu ne rends ni
résumé juridique, ni conseil, ni verdict : seulement des termes qui aideront à retrouver les blocs
réellement fournis.

Le JSON contient le titre réel du document et une unité bornée de blocs citables consécutifs. Cette
unité **peut couvrir plusieurs nœuds** du contrat : `unite.premier_noeud` ne nomme que celui de son
premier bloc, et c'est chaque entrée d'`extraits` qui nomme le sien par `node_id` et `node_title`.
Deux extraits de nœuds différents traitent de sujets différents ; ne les lis jamais comme un seul
article. Chaque entrée d'`extraits` porte aussi son `block_id`, son texte exact et `truncated`, qui
vaut `true` si tu ne vois qu'un préfixe du bloc. **Tu ne vois ni le reste du bloc tronqué, ni le
reste du contrat, ni une hiérarchie implicite.** N'affirme et ne déduis rien qui exige un texte
absent.

## `termes`

Pour chaque extrait utile, jusqu'à $max_terms entrées `{fiche_id, canonique, variantes}` :

- `fiche_id` répète exactement le `block_id` de cet extrait ; ce nom de champ est celui du schéma
  historique, il ne transforme pas le bloc en fiche ;
- `canonique` est un mot ou une courte expression française explicitement ancrée dans l'extrait,
  au plus $term_max_words mots et $term_max_chars caractères ;
- `variantes` contient jusqu'à $max_variants synonymes, tournures usuelles, sigles ou formes
  anglaises, allemandes et portugaises qui désignent la **même notion**, sous les mêmes bornes.

Une variante peut aider à retrouver le bloc mais ne prouve jamais qu'une garantie s'applique. Dans
le doute, rends moins de termes. Jamais de phrase, de définition, de citation, de couverture ou
d'exclusion inventée.

## `questions`

Rends toujours `questions: []`. Un bloc de contrat n'est pas une fiche pratique et le format de
dictionnaire contractuel ne publie pas de questions candidates.

## Ce que le code vérifiera

Un identifiant qui n'appartient pas à l'unité, une chaîne hors borne ou un passage recopié comme
terme sera écarté. Une unité doit produire au moins un canonique pour que la campagne complète soit
écrite ; sinon aucun dictionnaire existant n'est remplacé.
