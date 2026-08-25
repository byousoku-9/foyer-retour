"""Langues dans lesquelles le système sait rédiger une réponse complète."""

from __future__ import annotations


LANGUES_SERVIES: dict[str, str] = {
    "fr": "français",
    "en": "anglais",
    "de": "allemand",
    "pt": "portugais",
}


def est_langue_servie(code: str) -> bool:
    """Un code fourni désigne-t-il une langue servie, après normalisation de forme ?"""
    return (code or "").strip().lower() in LANGUES_SERVIES


def normaliser_langue(code: str) -> tuple[str, bool]:
    """Rend la langue servie et indique si une détection a dû retomber sur le français."""
    normalise = (code or "").strip().lower()
    if normalise in LANGUES_SERVIES:
        return normalise, False
    return "fr", True


def langues_servies_texte() -> str:
    """Liste stable destinée aux messages d'erreur du contrat HTTP et des pipelines."""
    return ", ".join(f"{code} ({nom})" for code, nom in LANGUES_SERVIES.items())


def normaliser_langue_forcee(code: str | None) -> str | None:
    """Valide puis normalise une langue explicitement demandée ; `None` signifie détection."""
    if code is None:
        return None
    if not est_langue_servie(code):
        raise ValueError(f"langue non servie : choisissez {langues_servies_texte()}")
    return normaliser_langue(code)[0]
