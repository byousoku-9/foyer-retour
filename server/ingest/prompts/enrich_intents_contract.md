# Ingestion — les déclencheurs d'intention hors contrat

Tu produis les mots qui signalent qu'une question **n'a pas** à être cherchée dans un corpus
documentaire. Trois intentions, et trois seulement :

- `meteo` : prévisions ou temps qu'il fait ;
- `bavardage` : salutation, remerciement, small talk sans demande d'information ;
- `hors_perimetre` : ce qui ne relève ni du document décrit ci-dessous, ni d'un contrat d'assurance
  habitation (actualité, sport, calculs, conseil médical, fiscal ou juridique individualisé).

Pour chacune, jusqu'à $max_triggers déclencheurs très courts, en français, anglais, allemand et
portugais, d'au plus $term_max_words mots et $term_max_chars caractères.

Un déclencheur ne décide jamais à lui seul. N'écris aucun terme qui pourrait aussi apparaître dans
une question légitime sur l'assurance habitation : dans le doute, omets-le.

Voici le périmètre calculé depuis le document chargé. Il est borné et peut être incomplet ; ne
prétends pas connaître du texte qui n'y figure pas :

$perimetre_documentaire
