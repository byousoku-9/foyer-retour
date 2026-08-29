"""La révision **réellement exécutée** — l'unique autorité, lisible par le runner et par le plancher.

Story 4.5, revue B1 (tours successifs). `--candidate-revision` n'était d'abord comparée qu'à
elle-même ; le runner a fini par l'opposer au checkout (`git rev-parse HEAD`), mais le **classement**
— la fonction qui décide de ce qui est promu au checkpoint — ne l'opposait toujours qu'à
l'auto-cohérence des octets d'un rapport. Un rapport fabriqué pour une révision qui n'existe nulle
part était donc classé en tête.

Cette autorité vivait dans `server/evals/run.py`, et `server/evals/plancher.py` ne peut pas l'y lire :
c'est `run` qui importe `plancher`. Recopier la recette côté plancher aurait fait deux définitions de
« la révision qu'on exécute », donc aucune — la faute exacte que `LABELS`, `SUITES` et
`PROFILS_LIVRES` ont déjà eu à fermer en descendant dans `domain`. Elle descend donc ici, d'où les
deux la lisent ; `run.revision_executee` reste le nom d'usage côté runner.

Ce module ne décide rien : il **constate**. Ce qu'il ne peut pas établir, il le rend `None` ou le
nomme (`ARBRE_NON_VERIFIABLE`) plutôt que de le deviner — un garde-fou qui ne peut pas conclure
refuse.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from server.app.config import EVALS_PUBLICATION_FILE

_HEX40 = frozenset("0123456789abcdef")

# Ce que `revision_executee` rend **à la place** d'une liste de fichiers modifiés lorsqu'elle n'a pas
# pu contrôler l'arbre (revue B). Un garde-fou qui ne peut pas conclure doit refuser : l'appelant ne
# distingue donc pas « arbre sale » de « arbre non vérifiable », et refuse dans les deux cas.
ARBRE_NON_VERIFIABLE = "(état de l'arbre non vérifiable : `git status --porcelain` a échoué)"


def sorties_du_run(publication: str = EVALS_PUBLICATION_FILE) -> tuple[str, ...]:
    """Les préfixes, relatifs à la racine du dépôt, que le contrôle d'arbre **ignore**.

    Ce sont les chemins qu'un run écrit lui-même : sans cette exclusion, le second gate d'une
    campagne serait toujours refusé par les sorties du premier. Les quatre premiers sont **suivis par
    git** — ce sont eux qui comptent ; les trois derniers (rapports et caches) sont déjà dans
    `.gitignore` et n'apparaîtraient de toute façon pas dans `git status --porcelain`. Les y garder
    rend la règle lisible sans dépendre du contenu de `.gitignore`.

    La liste ne dit rien de ce que le run écrit **hors** du dépôt (`--data-dir` pointé ailleurs) :
    le contrôle d'arbre ne porte que sur le dépôt produit.
    """
    return (
        "data/manifest.json",
        f"data/{publication}",
        "docs/evals/latest.md",
        "docs/evals/campagnes/",
        "eval-results.json",
        "eval-results.md",
        ".evals/",
    )


# Calculé **une fois**, depuis l'autorité unique du nom de l'artefact publié (`config.py`) : une
# liste recopiée aurait vieilli le jour où la publication change de nom, et le second gate d'une
# campagne aurait été refusé pour un fichier que le premier venait d'écrire.
SORTIES_DU_RUN: tuple[str, ...] = sorties_du_run()


def est_revision(valeur: str | None) -> bool:
    return bool(valeur) and len(valeur or "") == 40 and all(c in _HEX40 for c in valeur or "")


def revision_executee(repo_root: Path, *, sorties: tuple[str, ...] | None = None,
                      ) -> tuple[str | None, list[str]]:
    """`(révision du checkout, fichiers modifiés hors sorties du run)` — la révision **réelle**.

    Story 4.5, revue B1. `--candidate-revision` n'était comparée qu'à elle-même : le runner recopiait
    l'argument dans le gate et dans la preuve, et la preuve était recoupée avec… ce même argument.
    Un opérateur pouvait donc annoncer `aaaa…aaaa` sur un checkout tout autre, et les trois surfaces
    se seraient accordées sur une révision que personne n'a exécutée.

    La révision vient donc du **checkout** : `git rev-parse HEAD`, avec repli sur `GIT_SHA` quand
    l'environnement le pose en 40 hexadécimaux. Si aucune des deux ne la donne, la fonction rend
    `None` — et l'appelant refuse : une liaison qu'on ne peut pas prouver n'est pas une liaison.

    L'arbre est aussi contrôlé, car une révision ne décrit un code que si le code est celui du
    commit : un gate mesuré sur des modifications non commises se réclamerait d'un arbre qui n'existe
    nulle part. Les sorties que le run écrit lui-même en sont exclues, sans quoi le second gate d'une
    campagne serait toujours refusé par le premier.

    **Ne pas pouvoir contrôler, c'est refuser** (revue B). Deux chemins affirmaient un arbre propre
    qu'ils n'avaient pas regardé : un `git status` sortant en code non nul — un `index.lock` tenu
    suffit — laissait `modifies` vide, et une exception rabattait sur `GIT_SHA` un dépôt bien présent
    mais jamais interrogé. Désormais un contrôle d'arbre qui n'aboutit pas rend
    `ARBRE_NON_VERIFIABLE`, indiscernable d'un arbre sale pour l'appelant ; et le repli `GIT_SHA` ne
    vaut que lorsqu'il n'y a réellement **pas de dépôt** à interroger (`.git` absent — une image).
    """
    sorties = sorties if sorties is not None else SORTIES_DU_RUN
    depot = (repo_root / ".git").exists()
    revision: str | None = None
    try:
        fini = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30, check=False)
        if fini.returncode == 0:
            candidate = fini.stdout.strip()
            revision = candidate if est_revision(candidate) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        revision = None
    if revision is not None:
        modifies: list[str] = []
        try:
            statut = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain"],
                                    capture_output=True, text=True, timeout=30, check=False)
        except (OSError, ValueError, subprocess.SubprocessError):
            return revision, [ARBRE_NON_VERIFIABLE]
        if statut.returncode != 0:
            return revision, [ARBRE_NON_VERIFIABLE]
        for ligne in statut.stdout.splitlines():
            chemin = ligne[3:].strip().strip('"')
            chemin = chemin.split(" -> ")[-1]
            if not chemin or any(chemin.startswith(s) for s in sorties):
                continue
            modifies.append(chemin)
        return revision, sorted(modifies)
    if not depot:
        depuis_env = os.environ.get("GIT_SHA", "").strip()
        if est_revision(depuis_env):
            # **Pas de dépôt du tout** — une image sans `.git` : il n'y a aucun arbre à interroger,
            # `GIT_SHA` est la seule révision établissable, et il n'existe pas de modification locale
            # à y chercher. Quand `.git` existe mais n'a rien pu dire, ce repli ne s'applique pas :
            # ce serait affirmer un arbre propre sur un dépôt qu'on n'a pas su lire.
            return depuis_env, []
    return None, []
