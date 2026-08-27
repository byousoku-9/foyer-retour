# Cas d'évaluation versionnés

> **Avertissement non expert :** aucune attente de ce golden set n'est validée par un expert assurance. `truth.validated_by_expert` reste toujours `false`. Une lecture préparée par la boucle ou une provenance déclarée ne vaut pas contresignature humaine.

Le profil `full` contient 34 cas guide, 16 cas sinistre et 11 observations parsing. Il inclut les cinq témoins `vertical` historiques sans en modifier les octets ; aucun d'eux ne déclare `expected.clarification`, et leurs verdicts admissibles restent inchangés.

## Guide

Les 33 nouveaux cas se répartissent entre parcours réel (11), météo (4), suivis avec ou sans historique (4), formulations multilingues enregistrées (6), questions traversant trois fiches (4) et hors-guide (4). Ils incluent mot pour mot les repros « nounou après l'école » et « ma femme commence son travail un mois avant moi ». La grille `server/evals/reference/utilite-guide.yaml` renseigne pour chacun l'ordre utile, les documents à montrer et l'interlocuteur à nommer. `countersigned_by` accepte un nom/date non blanc, mais reste `null` tant qu'une personne n'a pas réellement contresigné.

Les six contrôles EN/DE/PT pointent vers les fixtures de `tests/test_langues_live.py`, le test rejouable et la section correspondante de `docs/tests-live.md`. `server/evals/reference/retraductions.yaml` conserve le résultat, les écarts et la réserve de signature — aucune « retraduction de réponse » n'est inventée et aucun appel fournisseur n'a été rejoué.

## Sinistre

Les 16 cas couvrent AXA et Baloise. Les cas `full` nomment les robustesses absurde, multiple, hors habitation, vide, contradictoire et clairement couvert. Les témoins complémentaires couvrent la perte d'exploitation trois semaines d'un indépendant à domicile, la vitre d'insert avec fumée sans incendie, le téléphone volé par effraction pendant des vacances, l'exclusion animale attendue `non_couvert`, une tuile sur la voiture d'un invité et l'acte volontaire d'un adolescent. Ils mesurent des écarts ; ils ne déclarent pas le produit corrigé et ne transforment aucun verdict en avis juridique. Le cas clairement couvert n'accepte que `couvert` ; les cas réellement indécidables conservent leurs bornes prudentes.

## Parsing

Quatre observations AXA réutilisent les références de `tests/data/axa/` sur les pages 9, 11, 34 et 46. Sept observations Baloise couvrent par rôle les pages rendues 3, 10, 20, 30, 40 et 48. L'introduction RC `p30:5` et la puce chiens `p30:6` sont deux observations attribuables, au lieu de concaténer deux blocs sous un seul identifiant. Chaque YAML pointe exactement un `block_id`. La suite compare `normalize(bloc.text)` à `expected.text_norm` et ne construit jamais de client.

Les pages Baloise ont été lues visuellement par la boucle ; la contresignature de Lancelot Oudin reste due et n'est simulée nulle part. La lecture indépendante des colonnes révèle trois divergences honnêtes (`p30`, `p40`, `p48`) avec l'extraction intercalée : le run local courant rend donc 8 égalités et 3 labels `parsing`, pas un 11/11 auto-confirmant.
