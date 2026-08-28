"""Story 4.2b — La garde anti-rustine, bloquante.

Une **rustine** est un branchement du système mesuré sur ce qui le mesure : un `block_id` d'éval
dans un prompt, un id de cas dans le runtime, une page ou une formulation de question-témoin dans
`config.py`. Elle rend le témoin vert sans rendre le système juste — c'est exactement ce que la
suite d'évals est censée empêcher, retourné contre elle.

La garde balaie les surfaces du **runtime** (`server/app/**/*.py`, commentaires et docstrings
retirés — un commentaire n'exécute rien —, `server/app/llm/prompts/*.md` en texte intégral) à la
recherche de trois familles de motifs, dérivées du golden set lui-même (jamais d'une liste écrite à
la main qui vieillirait) :

1. les **ids de cas** (`s-bougie-canape`, …) — radicaux des fichiers de `server/evals/cases/` ;
2. les **block_ids attendus** des cas (`axa-…:p34:12`, …) et leurs suffixes `loc:seq` ;
3. les **formulations d'évals** : toute question d'un cas, normalisée, retrouvée dans une surface ;
   plus les motifs conditionnels hérités de `verify-4.2a.toml` (`(if|elif)…bougie|canapé|p34:12`).

L'unique échappatoire est l'allowlist juridique versionnée
(`server/evals/reference/allowlist-juridique.yaml`) : une entrée n'est valide que si elle nomme la
**règle générique** qu'elle sert et **au moins deux cas indépendants** couverts par cette règle —
une « règle » qu'un seul cas exerce est une rustine déguisée.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest
import yaml

from server.app.config import REPO_ROOT
from server.app.corpus.text import normalize

APP_DIR = REPO_ROOT / "server" / "app"
PROMPTS_DIR = APP_DIR / "llm" / "prompts"
CASES_DIR = REPO_ROOT / "server" / "evals" / "cases"
ALLOWLIST = REPO_ROOT / "server" / "evals" / "reference" / "allowlist-juridique.yaml"

# Motifs de départ hérités de la vérification 4.2a (`verify-4.2a.toml:41`) : un branchement
# conditionnel à moins de 80 caractères d'un token du cas témoin.
MOTIFS_CONDITIONNELS = [
    re.compile(r"(?i)(if|elif).{0,80}(s-bougie-canape|p34:12|bougie|canap[eé])"),
]
# Longueur minimale d'une formulation de question comparée (normalisée) : en dessous, une phrase
# banale (« comment faire ? ») collisionnerait avec n'importe quel prompt.
QUESTION_MIN_CHARS = 25
SUFFIXE_MIN_CHARS = 5


def _sans_commentaires_ni_docstrings(source: str) -> str:
    """Le code exécutable seul : commentaires et chaînes en position d'instruction retirés.

    Un commentaire ou une docstring qui *raconte* un cas (provenance d'une mesure, exemple de
    revue) n'exécute rien ; une chaîne dans une expression — un prompt assemblé, une comparaison —
    exécute. La distinction est faite au tokenizer, pas à la regex.
    """
    morceaux: list[str] = []
    precedent = tokenize.NEWLINE
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and precedent in (
                    tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
                precedent = tokenize.NEWLINE
                continue
            if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                                tokenize.ENDMARKER):
                morceaux.append(tok.string)
            if tok.type not in (tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
                precedent = tok.type
    except tokenize.TokenError:
        return source  # un fichier intokenizable est balayé en entier plutôt qu'ignoré
    return " ".join(morceaux)


def _cas_du_golden_set(cases_dir: Path) -> tuple[set[str], set[str], set[str]]:
    """(ids de cas, block_ids attendus + suffixes loc:seq, questions normalisées)."""
    ids: set[str] = set()
    blocks: set[str] = set()
    questions: set[str] = set()
    for fichier in sorted(cases_dir.rglob("*.yaml")):
        ids.add(fichier.stem)
        brut = yaml.safe_load(fichier.read_text(encoding="utf-8"))
        if not isinstance(brut, dict):
            continue
        question = str(brut.get("question", ""))
        norme = normalize(question)
        if len(norme) >= QUESTION_MIN_CHARS:
            questions.add(norme)
        expected = brut.get("expected") or {}
        for block_id in expected.get("block_ids") or []:
            blocks.add(str(block_id))
            morceaux = str(block_id).split(":")
            if len(morceaux) == 3 and len(f"{morceaux[1]}:{morceaux[2]}") >= SUFFIXE_MIN_CHARS:
                blocks.add(f"{morceaux[1]}:{morceaux[2]}")
    return ids, blocks, questions


def _charger_allowlist(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    brut = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(brut, dict) and brut.get("schema_version") == 1, \
        f"{path} : allowlist hors schéma"
    entrees = brut.get("entrees") or []
    assert isinstance(entrees, list)
    for entree in entrees:
        _valider_entree(entree)
    return entrees


def _valider_entree(entree: dict) -> None:
    """Règle générique + ≥ 2 cas indépendants **existants** : sans quoi l'entrée est une rustine."""
    assert str(entree.get("regle_generique", "")).strip(), \
        f"allowlist : l'entrée {entree.get('id')!r} doit nommer sa règle générique"
    cas = entree.get("cas_independants") or []
    assert len(set(cas)) >= 2, \
        f"allowlist : l'entrée {entree.get('id')!r} exige au moins deux cas indépendants"
    existants = {p.stem for p in CASES_DIR.rglob("*.yaml")}
    inconnus = sorted(set(cas) - existants)
    assert not inconnus, \
        f"allowlist : l'entrée {entree.get('id')!r} cite des cas inexistants : {inconnus}"
    assert entree.get("fichiers"), f"allowlist : l'entrée {entree.get('id')!r} doit cibler des fichiers"
    assert entree.get("motifs"), f"allowlist : l'entrée {entree.get('id')!r} doit nommer ses motifs"


def _autorisee(relatif: str, token: str, allowlist: list[dict]) -> bool:
    minuscule = token.lower()
    for entree in allowlist:
        if relatif in entree.get("fichiers", []) and any(
                str(motif).lower() in minuscule or minuscule in str(motif).lower()
                for motif in entree.get("motifs", [])):
            return True
    return False


def balayer(app_dir: Path, cases_dir: Path, allowlist: list[dict],
            *, repo_root: Path = REPO_ROOT) -> list[str]:
    """Toutes les occurrences interdites : `(fichier, motif)` hors allowlist. Vide = pas de rustine."""
    ids, blocks, questions = _cas_du_golden_set(cases_dir)
    constats: list[str] = []
    surfaces: list[tuple[Path, str, bool]] = []
    for fichier in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in fichier.parts:
            continue
        surfaces.append((fichier, _sans_commentaires_ni_docstrings(
            fichier.read_text(encoding="utf-8")), True))
    prompts_dir = app_dir / "llm" / "prompts"
    if prompts_dir.is_dir():
        for fichier in sorted(prompts_dir.iterdir()):
            if fichier.suffix in (".md", ".txt", ".j2", ".jinja"):
                surfaces.append((fichier, fichier.read_text(encoding="utf-8"), False))
    for fichier, texte, est_code in surfaces:
        try:
            relatif = fichier.relative_to(repo_root).as_posix()
        except ValueError:
            relatif = fichier.as_posix()
        norme = normalize(texte)
        for token in sorted(ids | blocks):
            if token.lower() in texte.lower() and not _autorisee(relatif, token, allowlist):
                constats.append(f"{relatif} : identifiant d'éval {token!r} dans le runtime")
        for question in sorted(questions):
            if question in norme and not _autorisee(relatif, question, allowlist):
                constats.append(f"{relatif} : formulation d'éval {question[:60]!r}…")
        if est_code:
            for motif in MOTIFS_CONDITIONNELS:
                trouve = motif.search(texte)
                if trouve is not None and not _autorisee(relatif, trouve.group(0), allowlist):
                    constats.append(f"{relatif} : branchement conditionnel sur un cas témoin "
                                    f"({trouve.group(0)[:80]!r})")
        else:
            for motif in MOTIFS_CONDITIONNELS:
                # Dans un prompt, la partie sensible est le token du cas, pas le mot-clé `if`.
                for token in ("s-bougie-canape", "p34:12", "bougie", "canapé", "canape"):
                    if token in texte.lower() and not _autorisee(relatif, token, allowlist):
                        constats.append(f"{relatif} : vocabulaire du cas témoin {token!r} dans un prompt")
                break
    return sorted(set(constats))


# --- les tests -------------------------------------------------------------------------------------

def test_le_runtime_les_prompts_et_la_config_sont_sans_rustine() -> None:
    """AC 4.2b : aucune occurrence interdite hors allowlist — la garde est bloquante."""
    allowlist = _charger_allowlist(ALLOWLIST)
    constats = balayer(APP_DIR, CASES_DIR, allowlist)
    assert constats == [], "rustine(s) détectée(s) :\n" + "\n".join(constats)


def test_une_rustine_plantee_dans_le_runtime_est_detectee(tmp_path: Path) -> None:
    """AC 4.2b : un id de cas branché dans le runtime fait échouer la garde."""
    app = tmp_path / "app"
    (app / "steps").mkdir(parents=True)
    (app / "steps" / "verifier.py").write_text(
        'def juger(question):\n'
        '    if question == "s-bougie-canape":\n'
        '        return True\n', encoding="utf-8")
    cas = tmp_path / "cases" / "sinistre"
    cas.mkdir(parents=True)
    (cas / "s-bougie-canape.yaml").write_text(
        "id: s-bougie-canape\nquestion: une bougie est tombée sur le canapé du salon\n"
        "expected:\n  block_ids: [doc-syntetique:p34:12]\n", encoding="utf-8")
    constats = balayer(app, tmp_path / "cases", [], repo_root=tmp_path)
    assert any("s-bougie-canape" in c for c in constats)
    assert any("branchement conditionnel" in c for c in constats)


def test_un_block_id_deval_dans_un_prompt_est_detecte(tmp_path: Path) -> None:
    """AC 4.2b : un `block_id` d'éval dans un prompt fait échouer la garde."""
    app = tmp_path / "app"
    (app / "llm" / "prompts").mkdir(parents=True)
    (app / "llm" / "prompts" / "rediger.md").write_text(
        "Cite toujours le bloc doc-syntetique:p34:12 quand la question parle de feu.\n",
        encoding="utf-8")
    cas = tmp_path / "cases" / "sinistre"
    cas.mkdir(parents=True)
    (cas / "s-temoin.yaml").write_text(
        "id: s-temoin\nquestion: un incendie a endommagé le salon de la maison\n"
        "expected:\n  block_ids: [doc-syntetique:p34:12]\n", encoding="utf-8")
    constats = balayer(app, tmp_path / "cases", [], repo_root=tmp_path)
    assert any("doc-syntetique:p34:12" in c for c in constats)


def test_une_formulation_deval_dans_un_prompt_est_detectee(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "llm" / "prompts").mkdir(parents=True)
    (app / "llm" / "prompts" / "comprendre.md").write_text(
        "Exemple : « Quel est le délai pour immatriculer une voiture importée ? »\n",
        encoding="utf-8")
    cas = tmp_path / "cases" / "guide"
    cas.mkdir(parents=True)
    (cas / "g-temoin.yaml").write_text(
        "id: g-temoin\nquestion: Quel est le délai pour immatriculer une voiture importée ?\n",
        encoding="utf-8")
    constats = balayer(app, tmp_path / "cases", [], repo_root=tmp_path)
    assert any("formulation d'éval" in c for c in constats)


def test_les_commentaires_et_docstrings_ne_sont_pas_des_rustines(tmp_path: Path) -> None:
    """Un commentaire n'exécute rien : la provenance d'une mesure reste citable dans le code."""
    app = tmp_path / "app"
    (app / "steps").mkdir(parents=True)
    (app / "steps" / "verifier.py").write_text(
        '"""Mesuré sur le cas s-bougie-canape (run réel)."""\n'
        "# le fragment « une bougie est tombée sur le canapé » n'établit rien\n"
        "def juger(question):\n"
        "    return None\n", encoding="utf-8")
    cas = tmp_path / "cases" / "sinistre"
    cas.mkdir(parents=True)
    (cas / "s-bougie-canape.yaml").write_text(
        "id: s-bougie-canape\nquestion: une bougie est tombée sur le canapé du salon\n",
        encoding="utf-8")
    assert balayer(app, tmp_path / "cases", [], repo_root=tmp_path) == []


def test_l_allowlist_exige_regle_generique_et_deux_cas_independants() -> None:
    """Boundaries 4.2b : allowlist juridique versionnée = règle générique + ≥ 2 cas indépendants."""
    entrees = _charger_allowlist(ALLOWLIST)
    for entree in entrees:
        assert str(entree["regle_generique"]).strip()
        assert len(set(entree["cas_independants"])) >= 2
    with pytest.raises(AssertionError, match="deux cas indépendants"):
        _valider_entree({"id": "solitaire", "regle_generique": "une règle",
                         "fichiers": ["x.md"], "motifs": ["y"],
                         "cas_independants": ["s-bougie-canape"]})
    with pytest.raises(AssertionError, match="règle générique"):
        _valider_entree({"id": "sans-regle", "regle_generique": "  ",
                         "fichiers": ["x.md"], "motifs": ["y"],
                         "cas_independants": ["s-bougie-canape", "b-bougie-canape"]})
