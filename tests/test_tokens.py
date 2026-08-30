"""Le comptage de tokens est un lecteur de production payant — donc soumis à N1 et à N3.

Patch croisé 1/3, `N1-TOKENS-OMIS`. Ce module était le seul lecteur de la cartographie resté
byte-identique à la baseline : il lisait chaque chemin, **payait**, puis rouvrait le même chemin
pour en mesurer la longueur. Un lot de sommaires couverts pouvait donc faire compter les tokens
d'une génération et publier la longueur d'une autre, sans jamais vérifier qu'une racine était
installée avant de dépenser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.evals import tokens as tk
from server.evals.espace import EspacePublie


def _lot_couvert(tmp_path: Path) -> tuple[Path, EspacePublie]:
    data = tmp_path / "data" / "doc"
    data.mkdir(parents=True)
    espace = EspacePublie(tmp_path, tmp_path / "data")
    espace.installer([Path("data") / "doc" / "summary.md"])
    espace.basculer([(data / "summary.md", "un sommaire couvert")])
    return data / "summary.md", espace


def test_le_comptage_refuse_un_chemin_hors_racine_avant_tout_appel(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """N3 : un appel payant ne se fait pas sur une lecture non pincée.

    Le refus tombe **avant** la clé et avant le client : c'est ce qui le rend atteignable par un
    test déterministe hors réseau, et c'est ce qui garantit qu'aucun appel n'a été soumis.
    """
    nu = tmp_path / "hors-racine.md"
    nu.write_text("pas sous une racine", "utf-8")

    assert tk.main([str(nu)]) == 2
    erreur = capsys.readouterr().err
    assert "aucune racine de publication ne couvre" in erreur and "--depot" in erreur
    assert "aucun appel n'a été soumis" in erreur


def test_deux_racines_dans_un_meme_lot_ne_se_comptent_pas_ensemble(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Un lot n'a qu'un repère : deux racines ne se pincent pas ensemble, donc ne se lisent pas."""
    premier, _espace = _lot_couvert(tmp_path / "un")
    second, _autre = _lot_couvert(tmp_path / "deux")

    assert tk.main([str(premier), str(second)]) == 2
    assert "racines différentes" in capsys.readouterr().err


def test_les_octets_comptes_sont_les_octets_mesures(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """N1 : une seule lecture, un seul tampon — les tokens et les caractères viennent des mêmes octets.

    La sonde bascule la cible **pendant** le comptage, à l'endroit exact où l'ancienne forme
    rouvrait le fichier. La longueur publiée doit rester celle des octets comptés.
    """
    chemin, espace = _lot_couvert(tmp_path)
    initial = chemin.read_text("utf-8")

    monkeypatch.setattr(tk.get_settings(), "anthropic_api_key", "sk-de-test", raising=False)
    monkeypatch.setattr(tk, "get_settings", lambda: type("S", (), {"anthropic_api_key": "sk"})())

    comptes: list[str] = []

    async def _mesurer(lot: list[tuple[Path, str]]) -> list[tuple[Path, dict[str, int]]]:
        for _path, texte in lot:
            comptes.append(texte)
        # Publie une autre longueur au moment précis où l'ancienne forme rouvrait le fichier.
        espace.basculer([(chemin, "un sommaire couvert, mais bien plus long qu'avant")])
        return [(path, {"reason": 1, "micro": 1}) for path, _ in lot]

    monkeypatch.setattr(tk, "measure", _mesurer)
    assert tk.main([str(chemin)]) == 0
    monkeypatch.undo()

    assert comptes == [initial], comptes
    sortie = capsys.readouterr().out
    assert f"[{len(initial)} caractères]" in sortie, (
        f"la longueur publiée ne vient pas des octets comptés : {sortie!r}")


def test_le_pointeur_nest_resolu_quau_pincement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`N1-TOKENS-OMIS` : découvrir la racine ne doit pas traverser `courant`.

    `racine_couvrant` faisait un `os.path.realpath` sur chaque cible, qui résout le lien **et** le
    pointeur : appelée une fois par chemin, elle produisait une résolution du pointeur par cible —
    deux sommaires en donnaient **trois** avec le pincement, et les deux premières pouvaient tomber
    de part et d'autre d'une bascule. Le lien se lit désormais tel qu'il est écrit, et le pointeur
    n'est résolu qu'une fois, au pincement.
    """
    import os

    premier, espace = _lot_couvert(tmp_path)
    second = premier.parent / "autre.md"
    espace.installer([Path("data") / "doc" / "autre.md"])
    espace.basculer([(second, "un second sommaire couvert")])

    pointeur = os.path.abspath(str(espace.chemin / "courant"))
    touches: list[str] = []

    def _mouchard(nom: str, vrai: object) -> object:
        def _appel(chemin: object, *a: object, **k: object) -> object:
            if str(chemin) == pointeur:
                touches.append(nom)
            return vrai(chemin, *a, **k)  # type: ignore[operator]
        return _appel

    for module, nom in ((os, "readlink"), (os, "stat"), (os.path, "realpath")):
        monkeypatch.setattr(module, nom, _mouchard(nom, getattr(module, nom)))
    lecture = tk.repere_du_lot([premier, second])
    lecture.fermer()
    monkeypatch.undo()

    assert touches == ["readlink"], (
        f"{len(touches)} résolutions de `courant` pour un lot de deux cibles ({touches}) : le "
        "pointeur ne doit être résolu qu'au pincement, une seule fois pour tout le lot")


def test_une_reconstruction_avant_lappel_payant_refuse_au_lieu_de_payer(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """`N1-TOKENS-OMIS` : la fraîcheur se vérifie après le lot et **avant** le premier appel payant.

    `Lecture.reel` contrôle avant de *rendre* un chemin, et l'ouverture arrive séparément : une
    reconstruction tombant entre les deux fournissait des octets reconstruits, puis déclenchait les
    appels — payés sur une génération que personne n'avait pincée. Un comptage ne se rejoue pas :
    il refuse.
    """
    chemin, espace = _lot_couvert(tmp_path)
    monkeypatch.setattr(tk, "get_settings", lambda: type("S", (), {"anthropic_api_key": "sk"})())

    appels: list[str] = []

    async def _sentinelle(lot: list[tuple[Path, str]]) -> list[tuple[Path, dict[str, int]]]:
        appels.append("appel")
        raise AssertionError("aucun appel payant ne doit suivre un repère périmé")

    vrai_lire = tk.lire_le_lot

    def _lire_puis_reconstruire(paths: list[Path], lecture: object) -> object:
        lot = vrai_lire(paths, lecture)  # type: ignore[arg-type]
        # Deux bascules : la seconde reconstruit la génération que le lot vient de pincer.
        espace.basculer([(chemin, "v2")])
        espace.basculer([(chemin, "v3")])
        return lot

    monkeypatch.setattr(tk, "lire_le_lot", _lire_puis_reconstruire)
    monkeypatch.setattr(tk, "measure", _sentinelle)
    code = tk.main([str(chemin)])
    monkeypatch.undo()

    assert code == 2, "un repère périmé se refuse avant tout appel"
    assert "aucun appel n'a été soumis" in capsys.readouterr().err
    assert appels == [], "un appel payant a suivi une génération reconstruite sous le lot"
