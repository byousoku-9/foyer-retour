"""FR41 / FR42 — L'artefact **unique** des résultats d'évals, partagé par les quatre surfaces.

Une story qui publie « les mêmes résultats » sur quatre surfaces peut le faire de deux façons : quatre
rendus qui lisent le même rapport, ou **un type** que quatre surfaces projettent. Le premier chemin
produit des divergences que rien ne voit — un arrondi ici, un champ oublié là — et l'AC de la story
4.5 demande précisément l'inverse : « les mêmes valeurs des douze champs, à l'octet des chiffres
près ».

Ce modèle est donc l'objet que le runner construit une fois, écrit dans `data/evals-latest.json`, et
dont dérivent : le Markdown de `docs/evals/latest.md`, celui que la CI concatène dans
`$GITHUB_STEP_SUMMARY`, la réponse de `GET /api/v1/evals/latest`, et la vue composée par `/`.

**Pourquoi il vit dans `domain`.** `tests/test_layers.py` interdit à `api` d'importer `server/evals`
(le gate serait juge et partie). L'écrivain est dans `evals`, le lecteur dans `api` ; le seul endroit
d'où les deux peuvent lire le même type est `domain`, qui n'importe que pydantic.

**Ce que ce modèle ne fait pas.** Il ne promeut rien et ne décide rien : `gate.evals_ok` reste seul à
dire ce qui est servi (AD-8). Un résultat rouge est publié avec ses limites — la publication est
inconditionnelle (FR41), et publier n'est pas promouvoir.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from .document import DomainModel
from .ingest import GateDecision

SHA256 = r"^[0-9a-f]{64}$"
REVISION = r"^[0-9a-f]{40}$"


class FrozenModel(DomainModel):
    """Un artefact publié ne se modifie pas après construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CoutPublie(FrozenModel):
    """Le coût, en trois chiffres et pas un de moins.

    `froid_eur` est le coût **sans cache** du run publié (c'est celui qu'une campagne paie
    réellement : le gate désarme le cache sous `--repeat`), `moyen_eur` le coût par exécution
    planifiée, `p95_eur` la queue de la distribution. La médiane seule cachait exactement ce qu'une
    comparaison majorant/réel doit voir.
    """

    froid_eur: float = Field(ge=0, allow_inf_nan=False)
    moyen_eur: float = Field(ge=0, allow_inf_nan=False)
    p95_eur: float = Field(ge=0, allow_inf_nan=False)


class LatencePubliee(FrozenModel):
    p50_ms: int = Field(ge=0)
    p95_ms: int = Field(ge=0)


class StabilitePubliee(FrozenModel):
    """« N/N » : combien de cas comptabilisés sont stables sur les N répétitions du run."""

    n: int = Field(ge=1)
    cas_stables: int = Field(ge=0)
    cas_comptabilises: int = Field(ge=0)


class ReservesPubliees(FrozenModel):
    """Les trois réserves de l'AC 3 — publiées partout, décisionnelles nulle part.

    Aucune décision de gate n'en dépend (AD-14 fixe `validated_by_expert: false` pour tout ce que ce
    projet produit ; la contresignature et la validation du dictionnaire sont des entrées humaines
    dues). Les taire serait l'invention qu'AD-16 interdit ; les rendre bloquantes ferait d'une dette
    connue un refus de servir.
    """

    countersigned: bool
    validated_by_expert: bool
    dictionary_validated: bool


class SecondeLecturePubliee(FrozenModel):
    """FR47 — Où en est la seconde lecture sur images de pages.

    Le builder livre le **plan** (déterministe, dérivé du corpus servi) et le **contrôle** du verdict
    rempli ; le verdict lui-même est produit par l'orchestrateur, qui seul peut regarder les images.
    `statut` dit lequel des trois états est le vrai, sans jamais en fabriquer un quatrième.
    """

    statut: Literal["absente", "planifiee", "concordante", "divergente"]
    blocs_planifies: int = Field(ge=0)
    blocs_verifies: int = Field(ge=0)


class PublicationEvals(FrozenModel):
    """Le résultat d'un run de gate, tel qu'il est publié — rouge compris.

    Les douze champs que l'AC 4 compare d'une surface à l'autre sont : `labels`, `variantes`,
    `recall`, `stabilite`, `cout.froid_eur`, `cout.moyen_eur`, `latence.p50_ms`, `latence.p95_ms`,
    `cases_hash`, `ne_tranche_pas_rate`, `limites` et `run_digest`. Les autres champs (identité,
    décisions, réserves, seconde lecture) situent la mesure ; aucun n'est facultatif.
    """

    schema_version: Literal[1] = 1
    profile: str = Field(min_length=1)
    candidate_revision: str | None = Field(default=None, pattern=REVISION)
    run_digest: str = Field(pattern=SHA256)
    report_digest: str = Field(pattern=SHA256)
    plancher_digest: str = Field(pattern=SHA256)
    cases_hash: str = Field(pattern=SHA256)
    date: str = Field(min_length=1)
    evals_ok: bool
    variantes: dict[str, int] = Field(default_factory=dict)
    labels: dict[str, int] = Field(default_factory=dict)
    recall: float = Field(ge=0, le=1, allow_inf_nan=False)
    stabilite: StabilitePubliee
    cout: CoutPublie
    latence: LatencePubliee
    ne_tranche_pas_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    reserves: ReservesPubliees
    decisions: list[GateDecision] = Field(default_factory=list)
    # **Dérivées mécaniquement** du run (décisions rouges chiffrées, réserves à `false`, exécutions
    # manquantes, écarts de parsing, état incomplet) : aucune prose n'est fabriquée ici.
    limites: list[str] = Field(default_factory=list)
    seconde_lecture: SecondeLecturePubliee


class EtatPublication(FrozenModel):
    """Ce que le serveur sait d'un run publié — y compris « aucun ».

    `publie: false` est un **état typé**, pas une erreur : un artefact absent, illisible ou hors
    schéma ne rend jamais 5xx et n'invente aucun chiffre. `raison` dit laquelle des trois, pour que
    la page puisse le dire sans le déduire.
    """

    publie: bool
    raison: Literal["absent", "illisible", "hors_schema"] | None = None
    publication: PublicationEvals | None = None
