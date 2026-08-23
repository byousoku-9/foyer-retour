"""AD-15 — Le contenu utilisateur et documentaire est délimité, typé comme non fiable, placé après
le préfixe système. La délimitation réduit les injections, elle ne les élimine pas ; le texte
délimité n'est jamais logué. Les prompts versionnés vivent dans `llm/prompts/` (couverts par
`prompts_digest`, AD-10)."""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_KIND = re.compile(r"^[a-z][a-z0-9_]*$")
# Toute tentative de fermer (`</untrusted ...>`) ou de rouvrir (`<untrusted kind=...>`) la balise depuis
# le contenu, espaces et casse compris : fausses frontières et spoof du `kind` (revue P2).
_CLOSING = re.compile(r"<\s*/\s*untrusted", re.IGNORECASE)
_OPENING = re.compile(r"<\s*untrusted", re.IGNORECASE)


def untrusted(kind: str, text: str) -> str:
    """Délimite `text` comme contenu non fiable de type `kind` ; balises fermantes et ouvrantes neutralisées."""
    if not _KIND.match(kind):
        raise ValueError(f"kind invalide : {kind!r} (attendu : [a-z][a-z0-9_]*)")
    safe = _CLOSING.sub("<\\/untrusted", text)
    safe = _OPENING.sub("<\\\\untrusted", safe)
    return f'<untrusted kind="{kind}">\n{safe}\n</untrusted>'


def load_prompt(name: str) -> str:
    """Texte d'un prompt versionné (`llm/prompts/{name}.md`) ; absent ⇒ FileNotFoundError explicite."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"nom de prompt invalide : {name!r}")
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt absent : {path}")
    return path.read_text("utf-8")
