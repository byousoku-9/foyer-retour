"""Entrypoint opérateur du comptage de tokens — un lecteur de production, donc soumis à N1.

`uv run python -m server.evals.tokens data/lux-guide/summary.md data/axa-lu-optihome-2017/summary.md`
imprime, pour chaque fichier, le nombre de tokens compté par `count_tokens` pour les modèles des
tiers `reason` et `micro` (le fichier est envoyé comme unique message user ; le comptage inclut donc
quelques tokens d'enrobage du tour). Exit 0 si tout est compté, 2 sur refus avant appel, 3 sur
erreur API.

**Pourquoi cet entrypoint vit ici** (patch croisé 1/3, `N1-TOKENS-OMIS`). Le comptage est un appel
**payant** sur des artefacts **couverts** : c'est un lecteur de production au sens de N1, et il était
le seul de la cartographie resté byte-identique à la baseline. Il lisait, payait, puis **rouvrait**
le même chemin pour la longueur — tokens d'une génération, caractères d'une autre — sans jamais
vérifier qu'une racine était installée avant de dépenser.

La correction ne pouvait pas vivre dans `server/app/llm/` : la table des couches du spine interdit à
`llm` d'importer `corpus` (`tests/test_layers.py`), et l'API de repère y vit. Elle vit donc dans
`evals`, dont la table autorise déjà `evals → corpus, llm`. `server/app/llm/tokens.py` garde le
comptage pur, sans aucune E/S. Le contrat obtenu : une racine installée **avant** le client, **un**
repère pincé pour tout le lot, **une** lecture par fichier, et le même tampon pour les tokens et
pour les caractères.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from server.app.config import get_settings
from server.app.corpus.racine import (EspaceIllisible, EspaceNonInstalle, Lecture,
                                      LectureHorsGeneration, LecturePerimee, racine_couvrant)
from server.app.llm.models import TIERS
from server.app.llm.tokens import MEASURED_TIERS, measure


def repere_du_lot(paths: list[Path]) -> Lecture:
    """Un **seul** repère pour tout le lot, ou un refus avant tout coût.

    La racine se déduit des chemins eux-mêmes : ce sont des artefacts servis, donc couverts. Un lot
    dont un chemin ne relève d'aucune racine installée n'a pas d'opération de lecture cohérente à
    offrir — le refus tombe **avant** la construction du client, comme pour les six CLI d'ingestion
    et le runner. Deux racines dans un même lot ne sont pas un lot : elles ne se pincent pas
    ensemble, donc elles ne se comptent pas ensemble.
    """
    racines: dict[str, object] = {}
    for path in paths:
        racine = racine_couvrant(path)
        if racine is None:
            raise EspaceNonInstalle(
                f"{path} : aucune racine de publication ne couvre ce chemin — un comptage payant "
                "ne se fait pas sur une lecture non pincée. Poser la disposition (idempotente) : "
                "`python -m server.evals.espace --racine <racine> --data-dir <data> --depot`")
        racines[str(racine.chemin)] = racine
    if len(racines) != 1:
        raise EspaceNonInstalle(
            f"les chemins du lot relèvent de racines différentes ({sorted(racines)}) : aucun "
            "repère unique ne les lit ensemble")
    return next(iter(racines.values())).lecture()  # type: ignore[union-attr]


def lire_le_lot(paths: list[Path], lecture: Lecture) -> list[tuple[Path, str]]:
    """Les octets de chaque chemin, lus **une seule fois** à travers le repère pincé."""
    lu: list[tuple[Path, str]] = []
    for path in paths:
        octets = lecture.octets(path)
        if octets is None:
            raise FileNotFoundError(path)
        lu.append((path, octets.decode("utf-8")))
    return lu


def main(argv: list[str]) -> int:
    if not argv:
        print("usage : python -m server.evals.tokens <fichier> [...]", file=sys.stderr)
        return 2
    paths = [Path(a) for a in argv]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        print(f"fichier(s) introuvable(s) : {', '.join(map(str, missing))}", file=sys.stderr)
        return 2
    # **Le refus de racine tombe avant le client, avant la clé et avant le premier appel payant.**
    # L'ordre importe pour deux raisons : les deux refus sont d'avant coût, donc aucun n'est retardé
    # par l'autre ; et placer celui-ci en premier le rend atteignable par un test déterministe hors
    # réseau, `ANTHROPIC_API_KEY` absente — une garde qu'aucune sonde ne peut atteindre est une
    # garde qu'aucune sonde ne retient.
    try:
        lecture = repere_du_lot(paths)
    except (EspaceNonInstalle, EspaceIllisible) as exc:
        print(f"refus : {exc} — rien n'a été compté, aucun appel n'a été soumis", file=sys.stderr)
        return 2
    if not get_settings().anthropic_api_key:
        lecture.fermer()
        print("ANTHROPIC_API_KEY absente (environnement ou .env) : impossible de compter les tokens",
              file=sys.stderr)
        return 2
    try:
        try:
            lot = lire_le_lot(paths, lecture)
        except (LecturePerimee, LectureHorsGeneration) as exc:
            print(f"refus : {exc} — rien n'a été compté, aucun appel n'a été soumis",
                  file=sys.stderr)
            return 2
        try:
            results = asyncio.run(measure(lot))
        except Exception as exc:  # erreur API ou réseau : on signale sans distinguer
            print(f"comptage impossible : {type(exc).__name__}: {exc}", file=sys.stderr)
            return 3
    finally:
        lecture.fermer()
    textes = dict(lot)
    for path, counts in results:
        # **Les octets mesurés sont les octets comptés** : le même tampon, jamais une relecture.
        chars = len(textes[path])
        per_tier = "  ".join(f"{tier} ({TIERS[tier]}) : {counts[tier]} tokens"
                             for tier in MEASURED_TIERS)
        print(f"{path}  [{chars} caractères]  {per_tier}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
