# Ingestion — les déclencheurs d'intention

Tu produis les mots qui signalent qu'une question **n'a pas** à être cherchée dans un guide
documentaire. Trois intentions, et trois seulement :

- `meteo` : la question porte sur les prévisions ou le temps qu'il fait ;
- `bavardage` : salutation, remerciement, small talk — aucune demande d'information ;
- `hors_perimetre` : tout le reste de ce qui ne relève ni du guide, ni d'un contrat d'assurance
  habitation (actualité, sport, calculs, conseil médical, fiscal ou juridique individualisé).

Pour chacune, jusqu'à $max_triggers déclencheurs : des mots ou de très courtes expressions, en
français, en anglais, en allemand et en portugais. Au plus $term_max_words mots et
$term_max_chars caractères chacun.

**Un déclencheur d'intention n'est pas un mot du corpus** : sa présence ne prouve rien sur le fond
de la question, elle signale seulement une intention. N'écris donc jamais un mot qui pourrait aussi
apparaître dans une question légitime sur l'installation au Luxembourg ou sur un contrat d'assurance
(« température » est un mot de la météo *et* d'une clause de gel ; « bonjour » n'est un mot que du
bavardage). Dans le doute, n'écris pas le mot : un déclencheur de trop fait refuser une vraie
question, ce qui est le défaut le plus coûteux du système.

Voici le périmètre du guide, pour que tu saches ce qu'il ne faut **pas** déclencher :

$perimetre_guide
