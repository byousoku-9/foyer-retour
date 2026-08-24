"""Assemblage des étapes par sujet (AD-1) : `guide` en 1.5, `sinistre` en 1.8.

Un pipeline **enchaîne** des étapes, il ne fait rien d'autre : il ne lit pas le corpus, il n'appelle
pas de modèle. La table des couches du spine le dit (`pipelines → steps`) et `tests/test_layers.py`
le vérifie statiquement — `corpus`, `index` et `client` lui arrivent en paramètres. C'est ce qui rend
la chaîne d'AD-1 réellement fixe : un pipeline ne *peut pas* sauter la vérification pour appeler le
modèle en douce.
"""
