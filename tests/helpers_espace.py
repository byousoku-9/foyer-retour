"""Pose l'espace de publication d'une racine de test — le geste d'opérateur, joué par les tests.

Story 4.5, B7. La bascule ne pose jamais sa disposition : elle refuse quand une cible n'est pas
résolue par le pointeur unique (`EspaceNonInstalle`). C'est ce qui rend vraie la phrase « aucun
chemin de bascule ne crée, ne migre ni ne change le type d'une cible à l'exécution ».

Dans le dépôt, la disposition est **committée** (`data/manifest.json`, `data/evals-latest.json`,
`docs/evals/latest.md` et `docs/evals/campagnes` sont des liens statiques). Dans un `tmp_path`, elle
doit être posée par le test, exactement comme un opérateur ou la CI la posent — d'où ce module, et
non un repli complaisant dans le runner.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from server.evals.espace import EspacePublie, cibles_du_depot

# Les cibles que tout run peut publier, relatives à la racine. Les sorties de run par défaut de
# `run.main` (`eval-results.json` / `eval-results.md`) en font partie : un run sans `--gate` bascule
# déjà ce couple, et il est soumis au même invariant que la publication.
CIBLES_STANDARD = (
    Path("docs") / "evals" / "latest.md",
    Path("docs") / "evals" / "campagnes",
    Path("eval-results.json"),
    Path("eval-results.md"),
)


def poser_espace(racine: Path, *, data_dir: Path | None = None,
                 cibles: Iterable[Path] = ()) -> EspacePublie:
    """Installe l'espace de `racine` et rend l'objet. Idempotent, et il **migre** ce qui existe déjà.

    `migrer=True` est le mode opérateur : dans un test, un `data/manifest.json` déjà écrit en fichier
    ordinaire est déplacé dans le bundle et remplacé par son lien, une fois, avant tout run. Aucun
    chemin de production n'atteint ce mode.
    """
    espace = EspacePublie(racine, data_dir)
    voulues = [*CIBLES_STANDARD, *cibles_du_depot(racine, espace.data_dir), *cibles]
    voulues = list(dict.fromkeys(voulues))
    espace.installer(voulues, migrer=True)
    return espace
