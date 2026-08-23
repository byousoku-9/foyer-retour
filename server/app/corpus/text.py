"""Convention Texte du spine : l'unique `normalize()`.

NFKC → `œ`/`æ` décomposés en `oe`/`ae` → retrait des diacritiques (NFD, marques combinantes) → minuscules →
apostrophes/guillemets/tirets unifiés (', ", -) et espaces internes aux guillemets retirés →
suppression de la césure `-\\n` → tout séparateur (\\s, puces, |) → un espace → trim.

Limites assumées (documentées par `tests/test_normalize.py`) :
- les ligatures de *compatibilité* (ﬁ, ﬂ, …) sont décomposées par NFKC ; `œ`/`æ` (et majuscules) le sont explicitement
  (reprise différée 1.0, tranchée en 1.2 : 3 `œ` dans le contrat AXA, « cœur » doit rejoindre « coeur ») ;
- la règle de césure `-\\n` supprime aussi un vrai trait d'union en fin de ligne (« porte-\\nfenêtre » → « portefenetre ») ;
  conservée en 1.2 : indécidable sans dictionnaire, 7 cas dans le contrat AXA, aucun sur les pages décisionnelles
  (9, 11, 34, 46) ;
- le tiret conditionnel U+00AD est supprimé ; un tiret (`-`, `–`, `—`) en début de ligne suivi d'un espace est une puce.

`normalize_version` entre dans la clé de cache des évals : l'incrémenter à chaque changement de règle.
"""

from __future__ import annotations

import re
import unicodedata

normalize_version = "2"

_APOSTROPHES = "’‘‛ʼ′´`"
_QUOTES = "«»“”„‟″〝〞"
_DASHES = "‐‑‒–—―−"
_BULLETS = "•‣◦⁃∙▪▫●○■□·➢→►"
_SOFT_HYPHEN = "­"

_TRANS = str.maketrans(
    {**{c: "'" for c in _APOSTROPHES}, **{c: '"' for c in _QUOTES}, **{c: "-" for c in _DASHES}}
)
_LIGATURES = str.maketrans({"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE"})
_OPEN_GUILLEMET = re.compile(r"«\s*")
_CLOSE_GUILLEMET = re.compile(r"\s*»")
_DASH_BULLET = re.compile(r"(?m)^[ \t]*[-–—][ \t]+")
_HYPHENATION = re.compile(r"-\s*\n\s*")
_SEPARATORS = re.compile(r"[\s|" + re.escape(_BULLETS) + r"]+")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_LIGATURES)
    s = "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace(_SOFT_HYPHEN, "")
    s = _DASH_BULLET.sub(" ", s)
    s = _CLOSE_GUILLEMET.sub('"', _OPEN_GUILLEMET.sub('"', s))
    s = s.translate(_TRANS)
    s = _HYPHENATION.sub("", s)
    s = _SEPARATORS.sub(" ", s)
    return s.strip()
