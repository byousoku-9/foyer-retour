# Aucun résultat live publié par la story 4.2

> **Avertissement non expert :** les vérités de référence ne sont pas validées par un expert assurance et les contresignatures humaines indiquées comme dues restent absentes.

La story 4.2 versionne les cas, les grilles et la suite parsing locale. Elle ne lance pas de campagne guide/sinistre payante et ne fabrique donc aucun résultat courant. La génération et la publication des résultats appartiennent à la story 4.5.

Preuve locale disponible : `python -m server.evals.run --suite parsing --profile full --max-cost 0.01` compare les dix transcriptions avec un coût fournisseur nul. Le résultat local attendu est volontairement rouge : 7 `bonne_reponse` et 3 `parsing` sur les colonnes Baloise p30/p40/p48. Le code de sortie est donc 1, ce qui publie honnêtement la divergence sans en faire un incident.
