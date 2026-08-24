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

`normalize_spans()` rend la **même** chaîne que `normalize()` et, en plus, l'origine de chacun de ses
caractères dans le texte d'entrée. C'est ce qui permet de remonter d'une occurrence prouvée dans
`text_norm` au passage **brut** du bloc (AD-3 : « le texte affiché comme source est toujours relu
depuis `corpus` » — relu, et dans sa forme d'origine, pas dans sa forme normalisée ni dans la chaîne
rendue par le modèle). `normalize()` en est l'unique projection : une seule implémentation, donc
aucune dérive possible entre les deux.

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


Span = tuple[int, int]


def _sub_spans(pattern: re.Pattern[str], repl: str, chars: list[str],
               spans: list[Span]) -> tuple[list[str], list[Span]]:
    """`pattern.sub(repl, texte)` en conservant l'origine de chaque caractère.

    Le ou les caractères de `repl` héritent du segment source **entier** que le match consomme : un
    espace né de trois blancs et d'un retour à la ligne pointe sur les quatre.
    """
    texte = "".join(chars)
    sortie_c: list[str] = []
    sortie_s: list[Span] = []
    pos = 0
    for m in pattern.finditer(texte):
        sortie_c.extend(chars[pos:m.start()])
        sortie_s.extend(spans[pos:m.start()])
        if repl:
            origine = (spans[m.start()][0], spans[m.end() - 1][1])
            sortie_c.extend(repl)
            sortie_s.extend([origine] * len(repl))
        pos = m.end()
    sortie_c.extend(chars[pos:])
    sortie_s.extend(spans[pos:])
    return sortie_c, sortie_s


def normalize_spans(s: str) -> tuple[str, list[Span]]:
    """`normalize(s)` et, pour chaque caractère rendu, le segment `[début, fin)` de `s` dont il vient.

    Les caractères supprimés (marques combinantes, tiret conditionnel, césure `-\\n`) n'ont pas
    d'image ; ceux qu'une règle a fusionnés partagent le segment qui les a produits. Un intervalle
    normalisé `[a, b)` se retraduit donc en `(spans[a][0], spans[b - 1][1])`.
    """
    chars: list[str] = []
    spans: list[Span] = []
    for i, c in enumerate(s):
        # NFKD par caractère = NFD(NFKC(s)) sur la chaîne entière (NFKC compose ce que NFD
        # redécompose), et l'ordre canonique des marques combinantes n'importe pas puisqu'elles sont
        # retirées juste après. C'est ce qui rend la projection caractère par caractère légitime.
        t = unicodedata.normalize("NFKD", c).translate(_LIGATURES)
        t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
        for ch in t:
            if ch == _SOFT_HYPHEN:
                continue
            chars.append(ch)
            spans.append((i, i + 1))
    chars, spans = _sub_spans(_DASH_BULLET, " ", chars, spans)
    chars, spans = _sub_spans(_OPEN_GUILLEMET, '"', chars, spans)
    chars, spans = _sub_spans(_CLOSE_GUILLEMET, '"', chars, spans)
    chars = [c.translate(_TRANS) for c in chars]  # 1 → 1 : les origines ne bougent pas
    chars, spans = _sub_spans(_HYPHENATION, "", chars, spans)
    chars, spans = _sub_spans(_SEPARATORS, " ", chars, spans)
    debut, fin = 0, len(chars)  # `str.strip()`, en gardant les origines alignées
    while debut < fin and chars[debut].isspace():
        debut += 1
    while fin > debut and chars[fin - 1].isspace():
        fin -= 1
    return "".join(chars[debut:fin]), spans[debut:fin]


def normalize(s: str) -> str:
    return normalize_spans(s)[0]
