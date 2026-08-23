"""Convention Texte du spine : l'unique `normalize()`.

NFKC → retrait des diacritiques (NFD, marques combinantes) → minuscules →
apostrophes/guillemets/tirets unifiés (', ", -) et espaces internes aux guillemets retirés → suppression de la césure `-\\n` →
tout séparateur (\\s, puces, |) → un espace → trim.

`normalize_version` entre dans la clé de cache des évals : l'incrémenter à chaque changement de règle.
"""

from __future__ import annotations

import re
import unicodedata

normalize_version = "1"

_APOSTROPHES = "’‘‛ʼ′´`"
_QUOTES = "«»“”„‟″〝〞"
_DASHES = "‐‑‒–—―−­"
_BULLETS = "•‣◦⁃∙▪▫●○■□–·➢→►"

_TRANS = str.maketrans(
    {**{c: "'" for c in _APOSTROPHES}, **{c: '"' for c in _QUOTES}, **{c: "-" for c in _DASHES}}
)
_HYPHENATION = re.compile(r"-\s*\n\s*")
# Espaces typographiques à l'intérieur des guillemets français (« mot ») retirés avant unification.
_OPEN_GUILLEMET = re.compile(r"«\s*")
_CLOSE_GUILLEMET = re.compile(r"\s*»")
_SEPARATORS = re.compile(r"[\s|" + re.escape(_BULLETS) + r"]+")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))
    s = s.lower()
    s = _CLOSE_GUILLEMET.sub('"', _OPEN_GUILLEMET.sub('"', s))
    s = s.translate(_TRANS)
    s = _HYPHENATION.sub("", s)
    s = _SEPARATORS.sub(" ", s)
    return s.strip()
