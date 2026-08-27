# Cas d'évaluation versionnés

> **Avertissement non expert :** aucune attente de ce golden set n'est validée par un expert assurance. `truth.validated_by_expert` reste toujours `false`. Une lecture préparée par la boucle ou une provenance déclarée ne vaut pas contresignature humaine.

Le profil `full` contient 32 cas guide, 14 cas sinistre et 10 observations parsing. Il inclut les cinq témoins `vertical` historiques sans en modifier les octets.

## Guide

Les 31 nouveaux cas se répartissent entre parcours réel (10), météo (4), suivis avec ou sans historique (4), formulations multilingues enregistrées (6), questions traversant trois fiches (3) et hors-guide (4). La grille `server/evals/reference/utilite-guide.yaml` renseigne pour chacun l'ordre utile, les documents à montrer et l'interlocuteur à nommer. Tous les `countersigned_by` restent à `null` tant qu'une personne n'a pas réellement contresigné.

Les six contrôles EN/DE/PT pointent vers les fixtures de `tests/test_langues_live.py`, le test rejouable et la section correspondante de `docs/tests-live.md`. `server/evals/reference/retraductions.yaml` conserve le résultat, les écarts et la réserve de signature — aucune « retraduction de réponse » n'est inventée et aucun appel fournisseur n'a été rejoué.

## Sinistre

Les 14 cas couvrent AXA et Baloise. Les cas `full` nomment les robustesses absurde, multiple, hors habitation, vide, contradictoire et clairement couvert. Quatre témoins supplémentaires couvrent l'ordinateur professionnel volé à l'hôtel, la vitre d'insert avec fumée sans incendie, le téléphone volé par effraction pendant des vacances et l'exclusion attendue `non_couvert` pour les dégâts causés par un animal. Ils mesurent des écarts ; ils ne déclarent pas le produit corrigé et ne transforment aucun verdict en avis juridique.

## Parsing

Quatre observations AXA réutilisent les références de `tests/data/axa/` sur les pages 9, 11, 34 et 46. Six observations Baloise couvrent par rôle les pages rendues 3, 10, 20, 30, 40 et 48. Chaque YAML pointe exactement un `block_id`. La suite compare `normalize(bloc.text)` à `expected.text_norm` et ne construit jamais de client.

Les pages Baloise ont été lues visuellement par la boucle ; la contresignature de Lancelot Oudin reste due et n'est simulée nulle part. La lecture indépendante des colonnes révèle trois divergences honnêtes (`p30`, `p40`, `p48`) avec l'extraction intercalée : le run local courant rend donc 7 égalités et 3 labels `parsing`, pas un 10/10 auto-confirmant.
