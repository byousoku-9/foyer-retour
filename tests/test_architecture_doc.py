"""Contrat documentaire de la synthèse d’architecture, entièrement hors réseau."""

from __future__ import annotations

import re
import unicodedata

from server.app.config import REPO_ROOT

DOC = REPO_ROOT / "docs" / "architecture.md"


def _texte() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(texte: str, titre: str) -> str:
    section = re.search(
        rf"^{re.escape(titre)}\n(?P<body>.*?)(?=^## |\Z)",
        texte,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert section is not None
    return section.group("body")


def _paragraphe(texte: str, fragment: str) -> str:
    for paragraphe in texte.split("\n\n"):
        if fragment.casefold() in paragraphe.casefold():
            return paragraphe
    raise AssertionError(f"paragraphe absent pour {fragment!r}")


def test_la_synthese_reste_courte_et_structuree() -> None:
    texte = _texte()
    mots = re.findall(r"\b[\wÀ-ÿ’-]+\b", texte)
    assert 700 <= len(mots) <= 1_500, f"{len(mots)} mots : la cible est une synthèse d’environ deux pages"
    titres = re.findall(r"^#{1,6} .+$", texte, flags=re.MULTILINE)
    assert titres == [
        "# L’architecture en deux pages",
        "## Une requête : cinq étapes, dans le même ordre",
        "## Un classeur à blocs pour les deux sujets",
        "## Le verdict : une table conservatrice, pas une opinion du modèle",
        "## Intelligence payée à l’ingestion, lecture au service",
        "## Un service, une origine, une promotion conditionnelle",
    ]


def test_les_formulations_et_decisions_structurantes_sont_presentes() -> None:
    texte = _texte()
    for formulation in (
        "IDs stables",
        "intelligence payée à l’ingestion",
        "contrôles autour",
        "échec terminal",
        "le modèle propose, le code vérifie",
    ):
        assert formulation.casefold() in texte.casefold()

    ads = {int(numero) for numero in re.findall(r"\bAD-(\d+)\b", texte)}
    assert {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 16} <= ads


def test_les_decisions_restent_associees_a_leur_section() -> None:
    texte = _texte()
    requete = _section(texte, "## Une requête : cinq étapes, dans le même ordre")
    ingestion = _section(texte, "## Intelligence payée à l’ingestion, lecture au service")

    assert "AD-5" in _paragraphe(requete, "question autonome")
    assert "AD-10" in _paragraphe(requete, "`Trace`")
    structure = _paragraphe(ingestion, "`structure.json`")
    assert all(ad in structure for ad in ("AD-2", "AD-7", "AD-16"))
    assert "AD-8" in _paragraphe(ingestion, "contrôles statiques")
    assert "AD-14" in _paragraphe(ingestion, "cache persistant")


def test_les_trois_diagrammes_couvrent_les_vues_demandees() -> None:
    texte = _texte()
    diagrammes = re.findall(r"```mermaid\n(.*?)```", texte, flags=re.DOTALL)
    assert len(diagrammes) == 3
    aretes_requete = re.findall(
        r'^\s*([A-Z]\w*)(?:\[[^\n]*\])?\s*-->\s*(?:\|[^|]*\|\s*)?([A-Z]\w*)',
        diagrammes[0],
        flags=re.MULTILINE,
    )
    assert {
        ("Q", "C"), ("C", "G"), ("G", "R"), ("C", "R"),
        ("R", "D"), ("D", "V"), ("V", "S"), ("C", "S"), ("G", "S"), ("R", "S"),
    } <= set(aretes_requete)
    assert all(etape in diagrammes[0] for etape in ("comprendre", "retrouver", "rédiger", "vérifier", "restituer"))
    assert all(registre in diagrammes[1] for registre in ("Document.nodes", "Document.blocks", "Node.items"))
    assert all(reference in diagrammes[1] for reference in ("NodeRef", "BlockRef"))
    assert all(element in diagrammes[2] for element in ("Cloud Run", "Secret Manager", "smoke tests", "promotion"))


def test_les_statuts_de_citation_et_la_table_de_verdict_restent_distincts() -> None:
    texte = _texte()
    for statut in ("`retrouvee`", "`pertinente`", "`applicable`", "`edition`"):
        assert statut in texte

    section = re.search(
        r"^## Le verdict : une table conservatrice, pas une opinion du modèle\n(?P<body>.*?)(?=^## |\Z)",
        texte,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert section is not None
    table = re.search(r"^\| Priorité .*?(?=\n\n)", section.group("body"), flags=re.MULTILINE | re.DOTALL)
    assert table is not None
    lignes = table.group().splitlines()
    assert lignes[:2] == [
        "| Priorité | État prouvé sur les clauses affichées | Verdict calculé |",
        "| --- | --- | --- |",
    ]
    lignes_attendues = [
        ("0", "contradiction non résolue, renvoi décisionnel ouvert ou aucune clause fondatrice", "`ne_tranche_pas`"),
        ("1", "une exclusion applicable couvre le cas", "`non_couvert`"),
        (
            "2",
            "une garantie s’applique, mais une condition, franchise ou exclusion reste humaine ; "
            "ou la garantie sort du socle commun ou dépend d’une option ou de conditions particulières inconnues",
            "`sous_conditions`",
        ),
        ("3", "une garantie du socle s’applique et aucune clause ne reste ouverte", "`couvert`"),
        ("4", "aucun cas précédent ne tranche", "`ne_tranche_pas`"),
    ]
    lignes_observees = [tuple(cellule.strip() for cellule in ligne.strip("|").split("|")) for ligne in lignes[2:]]
    assert lignes_observees == lignes_attendues


def test_la_synthese_ne_reintroduit_pas_le_vocabulaire_interdit() -> None:
    texte = unicodedata.normalize("NFC", _texte()).casefold()
    mapping = "mapp" + "ing"
    assureur = "assu" + "reur"
    association_interdite = re.compile(
        rf"{mapping}.{{0,40}}{assureur}|{assureur}.{{0,40}}{mapping}",
        flags=re.DOTALL,
    )
    assert association_interdite.search(texte) is None


def test_la_synthese_ne_fabrique_pas_un_resultat_de_validation() -> None:
    texte = unicodedata.normalize("NFC", _texte()).casefold()
    affirmations_inventees = (
        r"\bgates?\s+(?:(?:est|sont|reste(?:nt)?|devient|deviennent)\s+)?"
        r"(?:(?:pass[eé](?:e|es|s)?|bascul[eé](?:e|es|s)?)\s+)?(?:au\s+)?vert(?:e)?s?\b",
        r"\b(?:tous\s+les|la\s+totalit[eé]\s+des|l[’']ensemble\s+des)\s+tests\s+"
        r"(?:(?:est|sont|ont)\s+)?(?:passent|pass[eé](?:e|es|s)?|r[eé]ussissent|r[eé]ussi(?:e|es|s)?)\b",
        r"\btests\s+(?:sont\s+)?tous\s+(?:pass[eé]s|r[eé]ussis)\b",
        r"\bd[eé]ploiements?\s+(?:(?:(?:est|sont|a\s+[eé]t[eé]|ont\s+[eé]t[eé])\s+)?"
        r"(?:r[eé]ussi(?:e)?s?|valid[eé]s?|achev[eé]s?)|(?:a|ont)\s+r[eé]ussi|"
        r"s[’']est\s+achev[eé]\s+avec\s+succ[eè]s)\b",
        r"\bholdouts?\s+(?:(?:(?:est|sont|a\s+[eé]t[eé]|ont\s+[eé]t[eé])\s+)?"
        r"(?:valid[eé]s?|r[eé]ussi(?:e)?s?|pass[eé](?:e|es|s)?)|(?:a|ont)\s+r[eé]ussi)\b",
        r"\bvalidation\s+(?:du|des)\s+holdouts?\s+(?:(?:est|sont)\s+)?(?:r[eé]ussi(?:e)?s?|acquise)\b",
    )
    variantes_couvertes = (
        "le gate est passé au vert",
        "les gates sont verts",
        "tous les tests ont réussi",
        "la totalité des tests est passée",
        "tests tous passés",
        "le déploiement a réussi",
        "les déploiements ont été validés",
        "le holdout a été validé",
        "validation du holdout réussie",
    )
    for variante in variantes_couvertes:
        assert any(re.search(motif, variante) for motif in affirmations_inventees), variante
    assert not [motif for motif in affirmations_inventees if re.search(motif, texte)]
