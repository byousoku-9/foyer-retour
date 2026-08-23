"""Table de cas fixes de la convention Texte (partagée avec les évals)."""

import pytest

from server.app.corpus.text import normalize, normalize_version

CASES = [
    # (entrée, sortie attendue, règle)
    ("Dé-\nfinition  d’« assuré »\t| x", 'definition d\'"assure" x', "matrice spec 1.0"),
    ("Élève à l'école", "eleve a l'ecole", "diacritiques"),
    ("ﬁnance ﬂux", "finance flux", "ligatures (NFKC)"),
    ("assu-\nrance", "assurance", "césure -\\n"),
    ("assu- \n rance", "assurance", "césure avec espaces"),
    ("l’assuré l‘assuré l'assuré", "l'assure l'assure l'assure", "apostrophes unifiées"),
    ("« mot » “mot” \"mot\"", '"mot" "mot" "mot"', "guillemets unifiés"),
    ("2017 – 2018 — fin", "2017 - 2018 - fin", "tirets unifiés"),
    ("• un\n• deux\n‣ trois", "un deux trois", "puces"),
    ("cellule | cellule | x", "cellule cellule x", "tableaux |"),
    ("  A B C  ", "a b c", "espaces insécables et trim"),
    ("Garantie\r\nvol", "garantie vol", "retours à la ligne"),
    ("assu\u00adrance", "assurance", "tiret conditionnel U+00AD supprimé"),
    ("– un\n– deux\n- trois", "un deux trois", "tirets en début de ligne = puces"),
    ("sous-sol et porte – fenêtre", "sous-sol et porte - fenetre", "tiret intérieur conservé"),
    ("cœur et ﬁn", "cœur et fin", "limite documentée : œ/æ non décomposés, seules les ligatures de compatibilité le sont"),
    ("porte-\nfenêtre", "portefenetre", "limite documentée : un vrai trait d'union en fin de ligne est traité comme une césure"),
    ("", "", "vide"),
]


@pytest.mark.parametrize(("raw", "expected", "rule"), CASES, ids=[c[2] for c in CASES])
def test_normalize_table(raw: str, expected: str, rule: str) -> None:
    assert normalize(raw) == expected


def test_normalize_idempotent() -> None:
    for raw, _, _ in CASES:
        assert normalize(normalize(raw)) == normalize(raw)


def test_normalize_version_is_a_string() -> None:
    assert normalize_version == "1"
