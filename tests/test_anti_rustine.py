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
   les **trigrammes rares** des questions et faits déclarés (« une bougie allumee ») ; le
   **vocabulaire distinctif** des faits déclarés — mots rares dans le golden set **et** rares dans
   le corpus servi (« machonne », « congelateur » : un mot que le corpus emploie partout, comme
   « mobilier », ne désigne aucun témoin) ; plus les motifs conditionnels hérités de
   `verify-4.2a.toml` (`(if|elif)…bougie|canapé|p34:12`).

L'unique échappatoire est l'allowlist juridique versionnée
(`server/evals/reference/allowlist-juridique.yaml`) : une entrée n'est valide que si elle nomme la
**règle générique** qu'elle sert et **au moins deux cas indépendants** couverts par cette règle —
une « règle » qu'un seul cas exerce est une rustine déguisée.
"""

from __future__ import annotations

import ast
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


def _chaines_statiques_hors_docstrings(source: str) -> str:
    """Valeurs de chaînes exécutables après composition statique par Python.

    Le balayage lexical voit `"s-bougie" + "-canape"` comme deux fragments. L'AST, lui, permet
    de reconstruire la valeur que le runtime comparera réellement, tout en excluant explicitement
    les docstrings de module, classe et fonction.
    """
    try:
        arbre = ast.parse(source)
    except SyntaxError:
        return source
    docstrings: set[int] = set()
    for noeud in ast.walk(arbre):
        corps = getattr(noeud, "body", None)
        if (isinstance(corps, list) and corps and isinstance(corps[0], ast.Expr)
                and isinstance(corps[0].value, ast.Constant)
                and isinstance(corps[0].value.value, str)):
            docstrings.add(id(corps[0].value))
    def _evaluer(noeud: ast.AST) -> str | None:
        if isinstance(noeud, ast.Constant) and isinstance(noeud.value, str):
            return noeud.value
        if isinstance(noeud, ast.BinOp) and isinstance(noeud.op, ast.Add):
            gauche, droite = _evaluer(noeud.left), _evaluer(noeud.right)
            return gauche + droite if gauche is not None and droite is not None else None
        if isinstance(noeud, ast.JoinedStr):
            morceaux: list[str] = []
            for valeur in noeud.values:
                cible = valeur.value if isinstance(valeur, ast.FormattedValue) else valeur
                rendu = _evaluer(cible)
                if rendu is None:
                    return None
                morceaux.append(rendu)
            return "".join(morceaux)
        if isinstance(noeud, (ast.List, ast.Tuple)):
            valeurs = [_evaluer(item) for item in noeud.elts]
            return "\0".join(valeurs) if all(v is not None for v in valeurs) else None
        if isinstance(noeud, ast.Call) and isinstance(noeud.func, ast.Attribute):
            base = _evaluer(noeud.func.value)
            args = [_evaluer(arg) for arg in noeud.args]
            if base is None or any(arg is None for arg in args):
                return None
            try:
                if noeud.func.attr == "format":
                    return base.format(*args)
                if noeud.func.attr == "replace" and len(args) in (2, 3):
                    return base.replace(*args)  # type: ignore[arg-type]
                if noeud.func.attr == "join" and len(args) == 1:
                    return base.join(args[0].split("\0"))
            except (IndexError, KeyError, ValueError):
                return None
        if isinstance(noeud, ast.Subscript):
            base = _evaluer(noeud.value)
            if base is not None and isinstance(noeud.slice, ast.Slice):
                def entier(valeur: ast.AST | None) -> int | None:
                    if valeur is None:
                        return None
                    if isinstance(valeur, ast.Constant) and isinstance(valeur.value, int):
                        return valeur.value
                    if (isinstance(valeur, ast.UnaryOp) and isinstance(valeur.op, ast.USub)
                            and isinstance(valeur.operand, ast.Constant)
                            and isinstance(valeur.operand.value, int)):
                        return -valeur.operand.value
                    raise ValueError
                try:
                    return base[slice(entier(noeud.slice.lower), entier(noeud.slice.upper),
                                      entier(noeud.slice.step))]
                except ValueError:
                    return None
        return None

    valeurs: list[str] = []
    for noeud in ast.walk(arbre):
        if id(noeud) in docstrings:
            continue
        valeur = _evaluer(noeud)
        if valeur is not None:
            valeurs.append(valeur)
    return " ".join(valeurs)


# Tokens distinctifs : mots normalisés d'au moins `TOKEN_MIN_CHARS` caractères, tirés des **faits
# déclarés** du golden set, présents dans au plus `TOKEN_RARETE_MAX` cas **et** au plus
# `TOKEN_CORPUS_MAX` fois dans le corpus servi. Le double filtre est ce qui sépare mécaniquement
# « congelateur » ou « machonne » (vocabulaire d'un témoin) de « mobilier » ou « domicile »
# (vocabulaire du domaine, partout dans le corpus) — sans liste écrite à la main.
TOKEN_MIN_CHARS = 6
TOKEN_RARETE_MAX = 2
TOKEN_CORPUS_MAX = 5
# Trigrammes de formulation (mots normalisés d'au moins 3 caractères, fenêtres de 3) tirés des
# questions **et** des faits : « une bougie allumee » désigne un témoin même quand aucun mot isolé
# ne le fait. Même seuil de rareté que les mots.
TRIGRAMME_MOT_MIN_CHARS = 3


def _texte_des_faits(brut: dict) -> str:
    faits = brut.get("faits") or {}
    return str(faits.get("description", "")) if isinstance(faits, dict) else ""


def _corpus_normalise(data_dir: Path) -> str:
    """Le texte normalisé de tous les `document.json` servis (chaîne vide si absent)."""
    import json

    morceaux: list[str] = []
    if not data_dir.is_dir():
        return ""
    for doc_dir in sorted(data_dir.iterdir()):
        document = doc_dir / "document.json"
        if not document.is_file():
            continue
        try:
            brut = json.loads(document.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for bloc in brut.get("blocks", []):
            if isinstance(bloc, dict):
                morceaux.append(normalize(str(bloc.get("text", ""))))
    return "\n".join(morceaux)


def _cas_du_golden_set(cases_dir: Path,
                       data_dir: Path) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    """(ids, block_ids + suffixes loc:seq, questions normalisées, mots distinctifs, trigrammes rares).

    Tout est **dérivé du golden set lui-même** — jamais d'une liste écrite à la main qui
    vieillirait avec les témoins (revue 4.2b, MEDIUM 5).
    """
    ids: set[str] = set()
    blocks: set[str] = set()
    questions: set[str] = set()
    porteurs_mots: dict[str, set[str]] = {}
    porteurs_trigrammes: dict[str, set[str]] = {}
    for fichier in sorted(cases_dir.rglob("*.yaml")):
        ids.add(fichier.stem)
        brut = yaml.safe_load(fichier.read_text(encoding="utf-8"))
        if not isinstance(brut, dict):
            continue
        question_norme = normalize(str(brut.get("question", "")))
        if len(question_norme) >= QUESTION_MIN_CHARS:
            questions.add(question_norme)
        faits_norme = normalize(_texte_des_faits(brut))
        for mot in set(faits_norme.split()):
            if len(mot) >= TOKEN_MIN_CHARS and mot.isalpha():
                porteurs_mots.setdefault(mot, set()).add(fichier.stem)
        mots_formulation = f"{question_norme} {faits_norme}".split()
        for fenetre in zip(mots_formulation, mots_formulation[1:], mots_formulation[2:]):
            if all(len(mot) >= TRIGRAMME_MOT_MIN_CHARS for mot in fenetre):
                porteurs_trigrammes.setdefault(" ".join(fenetre), set()).add(fichier.stem)
        expected = brut.get("expected") or {}
        for block_id in expected.get("block_ids") or []:
            blocks.add(str(block_id))
            morceaux = str(block_id).split(":")
            if len(morceaux) == 3 and len(f"{morceaux[1]}:{morceaux[2]}") >= SUFFIXE_MIN_CHARS:
                blocks.add(f"{morceaux[1]}:{morceaux[2]}")
    corpus = _corpus_normalise(data_dir)
    frequences: dict[str, int] = {}
    for mot in corpus.split():
        frequences[mot] = frequences.get(mot, 0) + 1
    distinctifs = {mot for mot, cas in porteurs_mots.items()
                   if len(cas) <= TOKEN_RARETE_MAX and frequences.get(mot, 0) <= TOKEN_CORPUS_MAX}
    # Un trigramme que le corpus servi écrit lui-même (« les conditions generales ») est du
    # vocabulaire du domaine, pas la formulation d'un témoin : seul un trigramme **absent du
    # corpus** désigne un témoin.
    trigrammes = {t for t, cas in porteurs_trigrammes.items()
                  if len(cas) <= TOKEN_RARETE_MAX and t not in corpus}
    return ids, blocks, questions, distinctifs, trigrammes


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
    motifs = [normalize(str(motif)) for motif in entree["motifs"]]
    for case_id in cas:
        brut = yaml.safe_load(next(CASES_DIR.rglob(f"{case_id}.yaml")).read_text(encoding="utf-8"))
        texte = normalize(f"{brut.get('question', '')} {_texte_des_faits(brut)}")
        assert any(motif in texte for motif in motifs), \
            f"allowlist : le cas {case_id!r} n'exerce aucun motif de l'entrée {entree.get('id')!r}"


def _autorisee(relatif: str, token: str, allowlist: list[dict]) -> bool:
    """Correspondance **stricte** : un motif n'autorise que le token exactement égal, après la
    même normalisation. Une correspondance par sous-chaîne blanchirait ce que l'entrée ne nomme
    pas — `bougie` aurait couvert l'id complet `s-bougie-canape` et toute formulation qui le
    contient (revue 4.2b, HIGH 3)."""
    norme = normalize(token)
    for entree in allowlist:
        if relatif in entree.get("fichiers", []) and any(
                normalize(str(motif)) == norme for motif in entree.get("motifs", [])):
            return True
    return False


def balayer(app_dir: Path, cases_dir: Path, allowlist: list[dict],
            *, repo_root: Path = REPO_ROOT, data_dir: Path | None = None) -> list[str]:
    """Toutes les occurrences interdites : `(fichier, motif)` hors allowlist. Vide = pas de rustine."""
    ids, blocks, questions, distinctifs, trigrammes = _cas_du_golden_set(
        cases_dir, data_dir if data_dir is not None else repo_root / "data")
    constats: list[str] = []
    surfaces: list[tuple[Path, str, bool]] = []
    for fichier in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in fichier.parts:
            continue
        source = fichier.read_text(encoding="utf-8")
        surfaces.append((fichier, f"{_sans_commentaires_ni_docstrings(source)} "
                                  f"{_chaines_statiques_hors_docstrings(source)}", True))
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
        tokens = re.findall(r"[a-z0-9]+", norme)
        mots = set(tokens)

        def contient_sequence(token: str) -> bool:
            attendus = re.findall(r"[a-z0-9]+", normalize(token))
            return bool(attendus) and any(
                tokens[index:index + len(attendus)] == attendus
                for index in range(len(tokens) - len(attendus) + 1))
        for token in sorted(ids | blocks):
            if contient_sequence(token) and not _autorisee(relatif, token, allowlist):
                constats.append(f"{relatif} : identifiant d'éval {token!r} dans le runtime")
        for question in sorted(questions):
            if question in norme and not _autorisee(relatif, question, allowlist):
                constats.append(f"{relatif} : formulation d'éval {question[:60]!r}…")
        # Le vocabulaire distinctif des témoins — dérivé du golden set et filtré par le corpus,
        # jamais une liste en dur (revue 4.2b, MEDIUM 5) — n'a rien à faire ni dans un prompt ni
        # dans une chaîne du code. Idem pour les trigrammes de formulation.
        for token in sorted(distinctifs & mots):
            if not _autorisee(relatif, token, allowlist):
                surface = "un prompt" if not est_code else "le code"
                constats.append(f"{relatif} : vocabulaire distinctif du témoin {token!r} dans {surface}")
        for trigramme in sorted(trigrammes):
            if trigramme in norme and not _autorisee(relatif, trigramme, allowlist):
                constats.append(f"{relatif} : formulation de témoin {trigramme!r}")
        if est_code:
            motifs_conditionnels = [
                re.compile(rf"(?i)(if|elif).{{0,80}}{re.escape(normalize(token))}")
                for token in sorted(ids | blocks | distinctifs) if len(normalize(token)) >= 5
            ]
            for motif in motifs_conditionnels:
                trouve = motif.search(norme)
                if trouve is not None and not _autorisee(relatif, trouve.group(0), allowlist):
                    constats.append(f"{relatif} : branchement conditionnel sur un cas témoin "
                                    f"({trouve.group(0)[:80]!r})")
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


def test_un_identifiant_assemble_par_python_reste_detecte(tmp_path: Path) -> None:
    """Une concaténation statique ne contourne pas la garde bloquante."""
    app = tmp_path / "app"
    (app / "steps").mkdir(parents=True)
    (app / "steps" / "verifier.py").write_text(
        'CAS_CIBLE = "s-bougie" + "-canape"\n'
        'def juger(case_id):\n'
        '    return case_id == CAS_CIBLE\n', encoding="utf-8")
    cas = tmp_path / "cases" / "sinistre"
    cas.mkdir(parents=True)
    (cas / "s-bougie-canape.yaml").write_text(
        "id: s-bougie-canape\nquestion: une bougie est tombée sur le canapé du salon\n",
        encoding="utf-8")
    constats = balayer(app, tmp_path / "cases", [], repo_root=tmp_path)
    assert any("s-bougie-canape" in constat for constat in constats)


@pytest.mark.parametrize("expression", [
    'f"s-bougie{\'-canape\'}"',
    '"s-{}-canape".format("bougie")',
    '"s-bougie_canape".replace("_", "-")',
    '"epanac-eiguob-s"[::-1]',
    'r"s-bougie.canape"',
])
def test_les_compositions_statiques_ne_contournent_pas_la_garde(
        tmp_path: Path, expression: str) -> None:
    app = tmp_path / "app"
    (app / "steps").mkdir(parents=True)
    (app / "steps" / "verifier.py").write_text(
        f"CIBLE = {expression}\ndef juger(case_id):\n    return case_id == CIBLE\n",
        encoding="utf-8")
    cas = tmp_path / "cases" / "sinistre"
    cas.mkdir(parents=True)
    (cas / "s-bougie-canape.yaml").write_text(
        "id: s-bougie-canape\nquestion: une bougie est tombée sur le canapé du salon\n",
        encoding="utf-8")
    assert any("identifiant d'éval" in constat
               for constat in balayer(app, tmp_path / "cases", [], repo_root=tmp_path))


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


def test_le_vocabulaire_distinctif_dun_temoin_non_bougie_est_detecte(tmp_path: Path) -> None:
    """Revue 4.2b, MEDIUM 5 : les motifs sont dérivés du golden set — pas d'une liste en dur.

    Le vocabulaire d'un témoin qui n'a rien à voir avec la bougie (« congélateur ») doit être
    détecté dans un prompt, mot isolé comme formulation (trigramme).
    """
    app = tmp_path / "app"
    (app / "llm" / "prompts").mkdir(parents=True)
    (app / "llm" / "prompts" / "rediger.md").write_text(
        "Quand les denrées du congélateur ont dégivré pendant une panne, cite la clause dédiée.\n",
        encoding="utf-8")
    cas = tmp_path / "cases" / "sinistre"
    cas.mkdir(parents=True)
    (cas / "b-congelateur.yaml").write_text(
        "id: b-congelateur\nquestion: Mes denrées sont perdues, suis-je couvert ?\n"
        "faits:\n  description: Les denrées du congélateur ont dégivré pendant une panne de courant.\n",
        encoding="utf-8")
    constats = balayer(app, tmp_path / "cases", [], repo_root=tmp_path)
    assert any("'congelateur'" in c and "vocabulaire distinctif" in c for c in constats)
    assert any("formulation de témoin" in c for c in constats)


def test_le_vocabulaire_du_corpus_nest_pas_un_temoin(tmp_path: Path) -> None:
    """Le double filtre : un mot que le corpus servi emploie partout ne désigne aucun témoin."""
    import json

    app = tmp_path / "app"
    (app / "llm" / "prompts").mkdir(parents=True)
    (app / "llm" / "prompts" / "rediger.md").write_text(
        "Le mobilier assuré est couvert selon la clause citée.\n", encoding="utf-8")
    cas = tmp_path / "cases" / "sinistre"
    cas.mkdir(parents=True)
    (cas / "s-temoin.yaml").write_text(
        "id: s-temoin\nquestion: Mon salon est abîmé, suis-je couvert ?\n"
        "faits:\n  description: Le mobilier du salon est abîmé.\n", encoding="utf-8")
    data = tmp_path / "data" / "doc-syntetique"
    data.mkdir(parents=True)
    (data / "document.json").write_text(json.dumps({
        "blocks": [{"text": "le mobilier assure " * 10}]}), encoding="utf-8")
    constats = balayer(app, tmp_path / "cases", [], repo_root=tmp_path)
    assert not any("mobilier" in c for c in constats)


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


def test_l_allowlist_est_stricte_un_motif_nautorise_que_son_token_exact() -> None:
    """Revue 4.2b, HIGH 3 : `bougie`/`canapé` n'autorisent ni l'id de cas ni une question entière."""
    entree = {"id": "exemple-negatif-qualites", "regle_generique": "une règle",
              "fichiers": ["server/app/llm/prompts/verifier_sinistre.md"],
              "motifs": ["bougie", "canapé"],
              "cas_independants": ["s-bougie-canape", "b-bougie-canape"]}
    fichier = "server/app/llm/prompts/verifier_sinistre.md"
    assert _autorisee(fichier, "bougie", [entree]) is True
    assert _autorisee(fichier, "canapé", [entree]) is True
    assert _autorisee(fichier, "canape", [entree]) is True  # même normalisation des deux côtés
    assert _autorisee(fichier, "s-bougie-canape", [entree]) is False
    assert _autorisee(fichier, "une bougie est tombee sur le canape du salon", [entree]) is False
    assert _autorisee("server/app/llm/prompts/autre.md", "bougie", [entree]) is False


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
    with pytest.raises(AssertionError, match="n'exerce aucun motif"):
        _valider_entree({"id": "cas-sans-lien", "regle_generique": "une règle",
                         "fichiers": ["x.md"], "motifs": ["bougie"],
                         "cas_independants": ["s-bougie-canape", "s-ado-baie-volontaire"]})
