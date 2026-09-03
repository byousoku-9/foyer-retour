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
    ("cœur et ﬁn", "coeur et fin", "œ décomposé (reprise 1.0 tranchée en 1.2)"),
    ("Œuvre ex æquo Æ", "oeuvre ex aequo ae", "Œ/æ/Æ décomposés"),
    ("porte-\nfenêtre", "portefenetre", "limite conservée en 1.2 : un vrai trait d'union en fin de ligne est traité comme une césure"),
    # Story 5.6 T16 (`normalize_version` 3) : le bloc `baloise-lu-home-2-2024:p21:4` coupe sa ligne
    # après « et/ ». La coupure devenait un espace, le modèle qui recopie soude « et/ou », et la
    # citation cessait d'être une sous-chaîne de `text_norm` — la garantie fondatrice du cas
    # `b-congelateur` était rejetée `non_retrouvee` une répétition sur trois.
    ("congélateur et/\nou réfrigérateur", "congelateur et/ou refrigerateur",
     "coupure de ligne après une barre oblique : même règle que la césure -\\n"),
    ("congélateur et/ \n ou réfrigérateur", "congelateur et/ou refrigerateur",
     "coupure après une barre, avec espaces"),
    ("24/7 et\n/ou", "24/7 et /ou", "limite symétrique : une barre en début de ligne n'est pas une coupure"),
    ("", "", "vide"),
]


@pytest.mark.parametrize(("raw", "expected", "rule"), CASES, ids=[c[2] for c in CASES])
def test_normalize_table(raw: str, expected: str, rule: str) -> None:
    assert normalize(raw) == expected


def test_normalize_idempotent() -> None:
    for raw, _, _ in CASES:
        assert normalize(normalize(raw)) == normalize(raw)


def test_normalize_version_is_a_string() -> None:
    assert normalize_version == "3"
