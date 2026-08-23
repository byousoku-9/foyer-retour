"""Parseur d'objet JS sans Node : constructions de kb.js couvertes, le reste refusé avec ligne et colonne."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.ingest.jsobject import JSObjectError, parse_js_object

MINI = Path(__file__).parent / "data" / "mini_kb.js"


def test_parses_mini_kb() -> None:
    kb = parse_js_object(MINI.read_text("utf-8"))
    assert kb["meta"] == {"verifie": "2026-08", "version": 3, "actif": True, "rien": None, "ratio": 1.5}
    assert [f["id"] for f in kb["fiches"]] == ["arrivee", "bail_test"]
    assert kb["fiches"][0]["corps"][1] == {"h": "Le matricule"}
    assert kb["fiches"][0]["corps"][3] == 'Il dit "bonjour"\tavec une tabulation et un \\ antislash.'
    assert kb["fiches"][0]["tableaux"][0]["lignes"][0] == ["Déclaration d'arrivée", "8 jours"]
    assert len(kb["faq"]) == 2 and len(kb["timeline"]) == 1


@pytest.mark.parametrize("text, expected", [
    ('{a: 1, "b": [1, 2,], c: {},}', {"a": 1, "b": [1, 2], "c": {}}),
    ('/* x */ window.KB = { a: "\\u00e9\\n" } ; // fin', {"a": "é\n"}),
    ("[-1, 2.5, 1e3, true, false, null]", [-1, 2.5, 1000.0, True, False, None]),
    ('"seule"', "seule"),
    ('"\\uD83D\\uDE00 \\u00e9"', "😀 é"),
])
def test_constructions(text: str, expected: object) -> None:
    assert parse_js_object(text) == expected


@pytest.mark.parametrize("text, line, col", [
    ("{a: 'x'}", 1, 5),
    ("{a: `x`}", 1, 5),
    ("{a: foo}", 1, 5),
    ('{a: "x"\n b: 1}', 2, 2),
    ('{\n  a: "non terminée\n}', 2, 6),
    ("{a: 1} {", 1, 8),
    ("/* ouvert", 1, 1),
    ('{a: "\\q"}', 1, 6),
    ('{a: "\\uD83Dx"}', 1, 6),
    ('{a: "\\uDE00"}', 1, 6),
    ('{a: 1,\n a: 2}', 2, 2),
    ("x.y.z = {}", 1, 1),
    ("window.AUTRE = {}", 1, 1),
])
def test_unknown_construction_reports_position(text: str, line: int, col: int) -> None:
    with pytest.raises(JSObjectError) as info:
        parse_js_object(text)
    assert (info.value.line, info.value.col) == (line, col), str(info.value)
