"""Couche `api` (AD-11/AD-12) : FastAPI, routes `/api/v1/*`, alias historiques, fichiers statiques.

Elle n'importe que `pipelines`, `corpus`, `domain`, `config`, `digests` et `llm` (table des couches
du spine, vérifiée par `tests/test_layers.py`) : le SDK Anthropic ne se voit qu'à travers `llm`.
"""
