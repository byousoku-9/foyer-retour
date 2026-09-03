"""Convention Texte du spine : l'unique `normalize()`.

NFKC → `œ`/`æ` décomposés en `oe`/`ae` → retrait des diacritiques (NFD, marques combinantes) → minuscules →
apostrophes/guillemets/tirets unifiés (', ", -) et espaces internes aux guillemets retirés →
suppression de la coupure de ligne intérieure à un token, `-\\n` (césure) puis `/\\n` →
tout séparateur (\\s, puces, |) → un espace → trim.

Limites assumées (documentées par `tests/test_normalize.py`) :
- les ligatures de *compatibilité* (ﬁ, ﬂ, …) sont décomposées par NFKC ; `œ`/`æ` (et majuscules) le sont explicitement
  (reprise différée 1.0, tranchée en 1.2 : 3 `œ` dans le contrat AXA, « cœur » doit rejoindre « coeur ») ;
- la règle de césure `-\\n` supprime aussi un vrai trait d'union en fin de ligne (« porte-\\nfenêtre » → « portefenetre ») ;
  conservée en 1.2 : indécidable sans dictionnaire, 7 cas dans le contrat AXA, aucun sur les pages décisionnelles
  (9, 11, 34, 46) ;
- la règle `/\\n` (`normalize_version` 3) traite de même la coupure de ligne qui suit une **barre oblique**
  (« congélateur et/\\nou réfrigérateur » → « congelateur et/ou refrigerateur ») : en PDF, une ligne ne se
  coupe à l'intérieur d'un token qu'après `-` ou `/`, et le second cas laissait un espace là où le lecteur
  — le modèle qui recopie — n'en voit aucun, si bien que la citation n'était plus une sous-chaîne de
  `text_norm` (8 blocs Baloise, 3 AXA — mesuré sur les artefacts du 03/09). Comme pour `-\\n`, la barre est **conservée** et la limite est
  symétrique : un `/` réellement suivi d'un retour à la ligne perd cet espace ;
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

normalize_version = "3"

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
_SLASH_BREAK = re.compile(r"(?<=/)\s*\n\s*")
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

    Les caractères supprimés (marques combinantes, tiret conditionnel, coupures `-\\n` et `/\\n`) n'ont pas
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
    # Même règle, même endroit : seule la coupure disparaît, la barre est conservée. `_sub_spans` garde
    # les origines alignées, donc `text_start`/`text_end`/`line_ids` restent exacts de part et d'autre.
    chars, spans = _sub_spans(_SLASH_BREAK, "", chars, spans)
    chars, spans = _sub_spans(_SEPARATORS, " ", chars, spans)
    debut, fin = 0, len(chars)  # `str.strip()`, en gardant les origines alignées
    while debut < fin and chars[debut].isspace():
        debut += 1
    while fin > debut and chars[fin - 1].isspace():
        fin -= 1
    return "".join(chars[debut:fin]), spans[debut:fin]


def forme_de_nombre(mot: str) -> str | None:
    """L'autre nombre d'un mot **déjà normalisé**, par la règle régulière du français : `-s`/`-x`.

    Rien de plus : ni lemmatisation, ni pluriels irréguliers, ni vocabulaire. Une règle qu'on peut
    écrire en une ligne et vérifier hors ligne, appliquée **aux requêtes seulement** — `normalize` et
    `words` ne sont pas touchés, donc ni `question_uid`, ni `result_uid`, ni les digests, ni les
    fixtures enregistrées. C'est ce qui distingue cette règle de la lemmatisation d'index, restée
    différée pour cette raison exacte.

    Elle vit ici parce que **deux** appelants l'emploient et doivent l'employer à l'identique : la
    requête de facette de *retrouver* (tour 3, R2) et la clé de groupe du dictionnaire (tour 5b, E2).
    Deux copies auraient fini par diverger, et le dictionnaire aurait alors cherché autre chose que
    ce que la facette cherche.
    """
    if len(mot) <= 2:
        return None
    return mot[:-1] if mot.endswith(("s", "x")) else mot + "s"


def normalize(s: str) -> str:
    return normalize_spans(s)[0]
