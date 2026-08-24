"""Outils d'exploitation du dépôt — jamais importés par le serveur.

`.dockerignore` exclut `scripts/` de l'image : ce qui vit ici tourne sur un poste ou sur un runner
de CI, jamais dans le conteneur servi. Le paquet existe pour que `tests/test_smoke.py` importe
`scripts.smoke` comme n'importe quel module, et pour que la frontière soit dite plutôt que déduite.
"""
