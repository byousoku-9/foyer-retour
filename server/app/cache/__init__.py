"""Story 5.6 (T5, décision de Lancelot du 03/09/2026) — les deux caches de la facture.

La navigation par le modèle (amendement AD-1) multiplie les écritures de préfixe et rend chaque
requête chère. Deux caches, et deux seulement, s'y attaquent — l'un chez le fournisseur, l'autre
ici :

1. **`prefixes`** — le préfixe cacheable d'un document servi (système + sommaire + outils + schéma
   de sortie) expire au bout d'une heure. La première requête qui suit paie une **écriture**
   (≈ 0,28 €) là où une requête à chaud paie une **lecture** (≈ 0,015 €). Un maintien périodique
   relit chaque préfixe déjà servi, avec l'appel le plus petit qui existe, avant qu'il ne refroidisse.
2. **`reponses`** — la même question, mot pour mot, sur le même document, la même image et les mêmes
   seuils, ne se repaie pas. La clé est **exacte** : une faute de frappe est une requête payée. Rien
   ici n'est sémantique, rien n'est « proche » ; c'est le seul cache qu'on puisse servir sans
   décider à la place du modèle.

**Pourquoi une couche à part, et pas `llm/` ni `pipelines/`.** `pipeline_digest` couvre exactement
`steps`, `pipelines`, `corpus`, `domain`, `llm` (`digests.PIPELINE_LAYERS`). Un cache logé dans
l'une de ces couches ferait de son propre code une composante de l'empreinte qui identifie l'image
mesurée : corriger une éviction invaliderait tous les gates et toutes les namespaces d'évals. La
couche `cache` est un service de la couche HTTP — elle n'importe que `domain` et `config`, reçoit le
client de modèle en paramètre annoté `Any` comme les pipelines, et sa propre version entre dans la
clé par `CACHE_SCHEMA_VERSION`.
"""

from .prefixes import (
    EtatMaintien,
    MaintienDesPrefixes,
    PrefixeServi,
    RegistreDesPrefixes,
)
from .reponses import (
    CACHE_SCHEMA_VERSION,
    CacheDeReponses,
    EntreeDeCache,
    composantes_de_cle,
    document_cachable,
    normaliser_question,
)

__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CacheDeReponses",
    "EntreeDeCache",
    "EtatMaintien",
    "MaintienDesPrefixes",
    "PrefixeServi",
    "RegistreDesPrefixes",
    "composantes_de_cle",
    "document_cachable",
    "normaliser_question",
]
