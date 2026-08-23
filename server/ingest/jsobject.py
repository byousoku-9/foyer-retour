"""Parseur minimal d'un littéral d'objet JavaScript, sans Node.

Couvre exactement ce que `kb.js` utilise : commentaires `//` et `/* */`, préfixe `window.KB = … ;`,
clés non citées (ou citées), chaînes à guillemets doubles avec les échappements JSON, nombres,
`true` / `false` / `null`, virgules finales. Toute autre construction (chaîne simple-quote, template,
expression, fonction) lève `JSObjectError(ligne, col)` : on s'arrête, on ne se replie pas sur Node.
"""

from __future__ import annotations

import re
from typing import Any

_WS_OR_COMMENT = re.compile(r"(?:\s+|//[^\n]*|/\*.*?\*/)+", re.DOTALL)
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_PREFIX = re.compile(r"(?:[A-Za-z_$][A-Za-z0-9_$.]*)\s*=\s*")
_HEX4 = re.compile(r"[0-9a-fA-F]{4}")
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class JSObjectError(ValueError):
    def __init__(self, message: str, line: int, col: int) -> None:
        super().__init__(f"{message} (ligne {line}, colonne {col})")
        self.line = line
        self.col = col


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    # --- utilitaires ---------------------------------------------------
    def _linecol(self, pos: int | None = None) -> tuple[int, int]:
        pos = self.pos if pos is None else pos
        line = self.text.count("\n", 0, pos) + 1
        col = pos - (self.text.rfind("\n", 0, pos) + 1) + 1
        return line, col

    def error(self, message: str, pos: int | None = None) -> JSObjectError:
        return JSObjectError(message, *self._linecol(pos))

    def skip(self) -> None:
        m = _WS_OR_COMMENT.match(self.text, self.pos)
        if m:
            self.pos = m.end()
        if self.text.startswith("/*", self.pos):
            raise self.error("commentaire /* non fermé")

    def peek(self) -> str:
        return self.text[self.pos : self.pos + 1]

    def expect(self, ch: str) -> None:
        self.skip()
        if self.peek() != ch:
            raise self.error(f"attendu {ch!r}, trouvé {self.peek() or 'fin de fichier'!r}")
        self.pos += 1

    # --- grammaire -----------------------------------------------------
    def document(self) -> Any:
        self.skip()
        m = _PREFIX.match(self.text, self.pos)
        if m:  # `window.KB = `
            self.pos = m.end()
        value = self.value()
        self.skip()
        if self.peek() == ";":
            self.pos += 1
            self.skip()
        if self.pos != len(self.text):
            raise self.error("contenu inattendu après la valeur")
        return value

    def value(self) -> Any:
        self.skip()
        ch = self.peek()
        if ch == "{":
            return self.obj()
        if ch == "[":
            return self.array()
        if ch == '"':
            return self.string()
        if ch in ("'", "`"):
            raise self.error("chaîne simple-quote ou template non supportée")
        m = _NUMBER.match(self.text, self.pos)
        if m:
            self.pos = m.end()
            s = m.group()
            return float(s) if any(c in s for c in ".eE") else int(s)
        m = _IDENT.match(self.text, self.pos)
        if m:
            word = m.group()
            if word in ("true", "false", "null"):
                self.pos = m.end()
                return {"true": True, "false": False, "null": None}[word]
            raise self.error(f"identifiant inattendu {word!r}")
        raise self.error(f"caractère inattendu {ch or 'fin de fichier'!r}")

    def obj(self) -> dict[str, Any]:
        self.expect("{")
        out: dict[str, Any] = {}
        while True:
            self.skip()
            if self.peek() == "}":
                self.pos += 1
                return out
            key_pos = self.pos
            key = self.key()
            if key in out:
                raise self.error(f"clé dupliquée {key!r}", key_pos)
            self.expect(":")
            out[key] = self.value()
            self.skip()
            if self.peek() == ",":
                self.pos += 1
                continue
            if self.peek() == "}":
                self.pos += 1
                return out
            raise self.error("attendu ',' ou '}' dans un objet")

    def key(self) -> str:
        self.skip()
        if self.peek() == '"':
            return self.string()
        m = _IDENT.match(self.text, self.pos)
        if not m:
            raise self.error("clé d'objet attendue")
        self.pos = m.end()
        return m.group()

    def array(self) -> list[Any]:
        self.expect("[")
        out: list[Any] = []
        while True:
            self.skip()
            if self.peek() == "]":
                self.pos += 1
                return out
            out.append(self.value())
            self.skip()
            if self.peek() == ",":
                self.pos += 1
                continue
            if self.peek() == "]":
                self.pos += 1
                return out
            raise self.error("attendu ',' ou ']' dans un tableau")

    def string(self) -> str:
        start = self.pos
        self.pos += 1  # guillemet ouvrant
        parts: list[str] = []
        while True:
            ch = self.peek()
            if ch == "":
                raise self.error("chaîne non terminée", start)
            if ch == '"':
                self.pos += 1
                return "".join(parts)
            if ch == "\n":
                raise self.error("retour à la ligne dans une chaîne", start)
            if ch == "\\":
                esc = self.text[self.pos + 1 : self.pos + 2]
                if esc in _ESCAPES:
                    parts.append(_ESCAPES[esc])
                    self.pos += 2
                elif esc == "u":
                    parts.append(self._unicode_escape())
                else:
                    raise self.error(f"échappement inconnu \\{esc}")
                continue
            parts.append(ch)
            self.pos += 1

    def _unicode_escape(self) -> str:
        """`\\uXXXX` ; une paire de substitution (`\\uD83D\\uDE00`) donne un seul caractère ; un substitut isolé est refusé."""
        hexa = self.text[self.pos + 2 : self.pos + 6]
        if not _HEX4.fullmatch(hexa):
            raise self.error("échappement \\u invalide")
        code = int(hexa, 16)
        self.pos += 6
        if 0xD800 <= code <= 0xDBFF:
            low = self.text[self.pos + 2 : self.pos + 6]
            if self.text.startswith("\\u", self.pos) and _HEX4.fullmatch(low) and 0xDC00 <= int(low, 16) <= 0xDFFF:
                self.pos += 6
                return chr(0x10000 + ((code - 0xD800) << 10) + (int(low, 16) - 0xDC00))
            raise self.error("substitut haut sans substitut bas", self.pos - 6)
        if 0xDC00 <= code <= 0xDFFF:
            raise self.error("substitut bas isolé", self.pos - 6)
        return chr(code)


def parse_js_object(text: str) -> Any:
    """Convertit un littéral JS (éventuellement précédé de `window.KB =`) en structure Python."""
    return _Parser(text).document()
