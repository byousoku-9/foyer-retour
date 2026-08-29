"""Story 4.2b — Le plancher pré-enregistré : chargement, digest, règle mécanique de classement.

Le protocole précède toute mesure : `server/evals/reference/plancher.yaml` fige, par témoin, le
plancher, le `N`, le numérateur, le dénominateur et les règles d'incident **avant** que le premier
appel payant parte. Ce module :

1. **charge et valide** le plancher — un témoin sans dénominateur, un `N` sous `n_minimum`, ou un
   plancher qui descend sous le floor 4.2a ou la règle trusted importés : refus, jamais une
   approximation ;
2. **calcule son digest** (SHA-256 des octets du fichier) — le runner l'inscrit dans l'identité de
   run, si bien que deux rapports ne se comparent qu'à protocole égal ;
3. porte la **règle mécanique du checkpoint** : le classement des configurations candidates —
   admissibles au plancher d'abord, puis la moins chère, puis la plus rapide. Aucun jugement, aucune
   question humaine : c'est le premier des trois rôles du checkpoint de finalisation.

Aucun seuil numérique n'est écrit ici : ils vivent tous dans `plancher.yaml` (Boundaries de la
spec 4.2b — `config.py` ou `plancher.yaml`, jamais en dur).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from server.app.corpus.text import normalize_version
from server.app.digests import pipeline_digest, prompts_digest
from server.app.llm.models import TIERS
from server.evals.cache import empreinte_canonique

REFERENCE_DIR = Path(__file__).resolve().parent / "reference"
PLANCHER_PATH = REFERENCE_DIR / "plancher.yaml"


class PlancherInvalide(Exception):
    """Le plancher ne peut pas être chargé : le protocole n'existe pas, aucune mesure ne part."""


class Temoin(BaseModel):
    """Un témoin du plancher : plancher, N, numérateur, dénominateur, règle d'incident (AC 4.2b)."""

    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1)
    pipeline: str = Field(min_length=1)
    famille: str = Field(min_length=1)
    criticite: Literal["bloquant", "alerte"]
    plancher: float = Field(ge=0, le=1)
    n: int = Field(ge=1)
    numerateur: str = Field(min_length=1)
    denominateur: str = Field(min_length=1)
    incident: str = Field(min_length=1)
    scope: Literal["repo", "run", "suite", "case", "live_http"]
    mesure_par: Literal["eval_runner", "orchestrator"]
    case_id: str | None = None
    note: str | None = None
    # **Quand** ce témoin est applicable (story 4.5). `toujours` est le comportement historique des
    # huit témoins de 4.2b ; `gate_full` réserve le témoin au seul gate qui revendique la politique
    # complète — `--gate {doc_id} --profile full`.
    #
    # Pourquoi ce champ, et pas un scope de plus : un `--profile full` **sans** `--gate` est un
    # diagnostic (c'est ce que la CI lance à chaque PR), et un gate `vertical` n'affirme que deux cas
    # relus à la main. Faire porter à l'un ou à l'autre les exigences de `full` rendrait rouge une
    # mesure qui ne les revendique pas — et rougir pour une raison qui n'a rien à voir avec le
    # candidat est la façon la plus sûre de faire ignorer un gate.
    arme_par: Literal["toujours", "gate_full"] = "toujours"


class ImportFloor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    n_minimum: int = Field(ge=1)
    thresholds: dict[str, float] = Field(min_length=1)


class ImportTrusted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: int
    proof_producer: Literal["orchestrator"]
    baseline_per_named_witness: int = Field(ge=1)
    final_per_named_witness: int = Field(ge=1)
    live_budget_default_eur: float = Field(gt=0)


class Imports(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor_4_2a: ImportFloor
    regle_trusted: ImportTrusted


class BudgetPlancher(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment_variable: str = Field(min_length=1)
    default_eur: float = Field(gt=0)
    enforced_by: list[str] = Field(min_length=1)
    on_exceeded: dict[str, Any]


class SeriesPlancher(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_per_named_witness: int = Field(ge=1)
    final_per_named_witness: int = Field(ge=1)
    repeat_required: bool
    max_cost_required: bool
    # Borne de sûreté de la CLI : elle vit dans le protocole versionné, jamais dans le runtime.
    max_repeat: int = Field(ge=3)


class Plancher(BaseModel):
    """Le protocole entier, tel que figé — validé strictement, jamais complété par défaut."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    story: str = Field(min_length=1)
    effective_from: str = Field(min_length=1)
    producer_de_preuve: Literal["orchestrator"]
    n_minimum: int = Field(ge=3)
    splits: dict[str, dict[str, Any]]
    budget: BudgetPlancher
    series: SeriesPlancher
    regles_incident: list[str] = Field(min_length=1)
    imports: Imports
    temoins: list[Temoin] = Field(min_length=1)

    @model_validator(mode="after")
    def _sans_diminution(self) -> Plancher:
        """Le floor 4.2a et la règle trusted sont importés **sans diminution** (AC 4.2b)."""
        par_metric = {t.metric: t for t in self.temoins}
        if len(par_metric) != len(self.temoins):
            raise ValueError("deux témoins portent la même metric : le plancher serait ambigu")
        floor = self.imports.floor_4_2a
        for metric, seuil in floor.thresholds.items():
            temoin = par_metric.get(metric)
            if temoin is None:
                raise ValueError(f"témoin {metric!r} du floor 4.2a absent : retirer un témoin est interdit")
            if temoin.plancher < seuil:
                raise ValueError(f"témoin {metric!r} : plancher {temoin.plancher} < floor 4.2a {seuil}")
            if temoin.n < floor.n_minimum:
                raise ValueError(f"témoin {metric!r} : n {temoin.n} < n_minimum 4.2a {floor.n_minimum}")
        if self.n_minimum < floor.n_minimum:
            raise ValueError(f"n_minimum {self.n_minimum} < n_minimum du floor 4.2a {floor.n_minimum}")
        trusted = self.imports.regle_trusted
        if self.budget.default_eur != trusted.live_budget_default_eur:
            raise ValueError(
                f"budget.default_eur {self.budget.default_eur} ≠ règle trusted "
                f"{trusted.live_budget_default_eur} : la règle trusted fait foi")
        if self.budget.environment_variable != "LIVE_BUDGET_EUR":
            raise ValueError("la règle trusted nomme LIVE_BUDGET_EUR : la variable ne se renomme pas")
        if self.series.baseline_per_named_witness != trusted.baseline_per_named_witness:
            raise ValueError("series.baseline_per_named_witness diverge de la règle trusted")
        if self.series.final_per_named_witness != trusted.final_per_named_witness:
            raise ValueError("series.final_per_named_witness diverge de la règle trusted")
        stabilite = [t for t in self.temoins if t.famille == "stabilite"]
        if not stabilite:
            raise ValueError("au moins un témoin de stabilité est requis (répétitions N>=3 sans cache)")
        for temoin in stabilite:
            if temoin.n < self.n_minimum:
                raise ValueError(f"témoin {temoin.metric!r} : la stabilité exige n >= {self.n_minimum}")
        for split in ("A", "B", "C"):
            if split not in self.splits:
                raise ValueError(f"split {split!r} absent : le protocole pré-enregistre les trois splits")
        return self

    def temoin(self, metric: str) -> Temoin | None:
        return next((t for t in self.temoins if t.metric == metric), None)


class ChargePlancher(BaseModel):
    """Le plancher chargé et son empreinte, indissociables."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plancher: Plancher
    digest: str


def _racine_autorite(plancher: Plancher, path: Path) -> Path | None:
    """Trouve facultativement le dépôt de contrôle, sans l'imposer au produit autonome."""
    sources = (Path(plancher.imports.floor_4_2a.source),
               Path(plancher.imports.regle_trusted.source))
    for parent in path.resolve().parents:
        if all((parent / source).is_file() for source in sources):
            return parent
    return None


def _yaml_objet(path: Path) -> dict[str, Any]:
    try:
        valeur = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PlancherInvalide(
            f"source d'autorité {path} illisible ({type(exc).__name__})") from exc
    if not isinstance(valeur, dict):
        raise PlancherInvalide(f"source d'autorité {path} : un objet YAML est attendu")
    return valeur


def _valider_snapshot(path: Path, digest: str) -> dict[str, Any]:
    try:
        octets = path.read_bytes()
    except OSError as exc:
        raise PlancherInvalide(f"snapshot figé {path} introuvable") from exc
    observe = hashlib.sha256(octets).hexdigest()
    if observe != digest:
        raise PlancherInvalide(
            f"snapshot figé {path.name} divergent : digest {observe}, attendu {digest}")
    return _yaml_objet(path)


def _valider_sources_figees(plancher: Plancher, path: Path) -> None:
    """Valide les copies produit : la CI publique ne dépend jamais du dépôt parent."""
    reference_dir = path.parent
    floor_importe = plancher.imports.floor_4_2a
    trusted_importe = plancher.imports.regle_trusted
    if not (reference_dir / floor_importe.snapshot).is_file():
        reference_dir = REFERENCE_DIR
    floor = _valider_snapshot(reference_dir / floor_importe.snapshot,
                              floor_importe.snapshot_digest)
    trusted = _valider_snapshot(reference_dir / trusted_importe.snapshot,
                                trusted_importe.snapshot_digest)
    if (floor_importe.n_minimum != floor.get("n_minimum")
            or floor_importe.thresholds != floor.get("thresholds")):
        raise PlancherInvalide("l'import floor 4.2a diverge du snapshot figé")
    attendu_trusted = {
        "schema_version": trusted.get("schema_version"),
        "proof_producer": (trusted.get("live") or {}).get("proof_producer"),
        "baseline_per_named_witness": ((trusted.get("live") or {}).get("series") or {}).get(
            "baseline_per_named_witness"),
        "final_per_named_witness": ((trusted.get("live") or {}).get("series") or {}).get(
            "final_per_named_witness"),
        "live_budget_default_eur": (trusted.get("budget") or {}).get("default_eur"),
    }
    if trusted_importe.model_dump(exclude={"source", "snapshot", "snapshot_digest"}) != attendu_trusted:
        raise PlancherInvalide("l'import trusted diverge du snapshot figé")


def _valider_sources_autorite(plancher: Plancher, racine: Path) -> None:
    """Recoupe les snapshots avec le dépôt parent lorsqu'il est présent."""
    floor_relatif = Path(plancher.imports.floor_4_2a.source)
    trusted_relatif = Path(plancher.imports.regle_trusted.source)
    floor = _yaml_objet(racine / floor_relatif)
    trusted = _yaml_objet(racine / trusted_relatif)
    importe_floor = plancher.imports.floor_4_2a
    importe_trusted = plancher.imports.regle_trusted
    if (importe_floor.n_minimum != floor.get("n_minimum")
            or importe_floor.thresholds != floor.get("thresholds")):
        raise PlancherInvalide("l'import floor 4.2a diverge de sa source d'autorité")
    attendu_trusted = {
        "schema_version": trusted.get("schema_version"),
        "proof_producer": (trusted.get("live") or {}).get("proof_producer"),
        "baseline_per_named_witness": ((trusted.get("live") or {}).get("series") or {}).get(
            "baseline_per_named_witness"),
        "final_per_named_witness": ((trusted.get("live") or {}).get("series") or {}).get(
            "final_per_named_witness"),
        "live_budget_default_eur": (trusted.get("budget") or {}).get("default_eur"),
    }
    if importe_trusted.model_dump(exclude={"source", "snapshot", "snapshot_digest"}) != attendu_trusted:
        raise PlancherInvalide("l'import trusted diverge de sa source d'autorité")


def charger_plancher(path: Path = PLANCHER_PATH, *, producer: str = "builder") -> ChargePlancher:
    """Charge, valide et fige le plancher — sinon `PlancherInvalide`, jamais un défaut silencieux."""
    try:
        octets = path.read_bytes()
        brut = yaml.safe_load(octets.decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PlancherInvalide(f"{path} : plancher illisible ({type(exc).__name__})") from exc
    if not isinstance(brut, dict):
        raise PlancherInvalide(f"{path} : un objet YAML est attendu")
    try:
        plancher = Plancher.model_validate(brut)
    except ValidationError as exc:
        premier = exc.errors()[0]
        champ = ".".join(str(p) for p in premier.get("loc", ())) or "(racine)"
        raise PlancherInvalide(f"{path} : champ {champ} — {premier.get('msg', '')}") from exc
    except ValueError as exc:
        raise PlancherInvalide(f"{path} : {exc}") from exc
    _valider_sources_figees(plancher, path)
    racine = _racine_autorite(plancher, path)
    if racine is not None:
        _valider_sources_autorite(plancher, racine)
    elif producer == "orchestrator":
        raise PlancherInvalide(
            "preuve non vérifiable : sources d'autorité 4.2a/trusted absentes du dépôt de contrôle")
    return ChargePlancher(plancher=plancher, digest=hashlib.sha256(octets).hexdigest())


# --- M2 : la preuve trusted est liée à la révision qu'elle mesure ---------------------------------

# Les clés racine d'un fichier de preuve orchestrateur — **exactement** celles-ci, ni plus ni moins.
# Un contrôle « au moins celles-ci » laisserait passer un fichier qui porte en plus son propre
# `status`, `evals_ok` ou `note` : le lecteur en ignorerait la moitié, et l'auteur du fichier croirait
# l'avoir dit. Un vocabulaire fermé se contrôle par égalité (même règle que `Cas` et `Temoin`).
CLES_PREUVE_TRUSTED = frozenset({
    "plancher_digest", "candidate_revision", "report_digest", "run_digest", "decisions",
})
_HEX = frozenset("0123456789abcdef")


def _hex_exact(valeur: Any, longueur: int) -> bool:
    return (isinstance(valeur, str) and len(valeur) == longueur
            and all(c in _HEX for c in valeur))


# Les cinq champs que `run.identite_run` écrit dans `identity.image`, et que **tout** rapport
# externe doit porter pour être opposable. La liste est ici, en un seul endroit, parce que la faute
# qu'elle ferme est précisément d'en avoir comparé un sous-ensemble : trois champs côté appelant,
# cinq côté rapport, et deux qui n'étaient donc jamais confrontés — dont `normalize_version`, qui
# décide des `text_norm` et donc de tous les `quote_hash`.
CHAMPS_IMAGE: tuple[str, ...] = (
    "pipeline_digest", "prompts_digest", "model_ids", "normalize_version", "plancher_digest",
)


def verifier_identite_externe(rapport: dict[str, Any], *, plancher_digest: str,
                              image_courante: dict[str, Any] | None,
                              origine: str) -> None:
    """Le rapport porte-t-il une identité **complète** et concordante ? — sinon il ne prouve rien.

    Story 4.5, revue B1. La version précédente disait « si le champ est présent, je le compare » :
    un rapport sans `plancher_digest` racine et avec `identity.image = {}` passait donc **tous** les
    contrôles, avec zéro champ effectivement comparé. Quatre façons d'obtenir ce vide suffisaient —
    racine absente, clé `image` absente, champs à `None`, `image` non-dict rabattu sur `{}`.

    La règle est désormais l'inverse, et elle vaut pour tout chemin de confiance : **une identité
    obligatoire absente, vide, mal formée ou incohérente ferme.** Un rapport qui ne dit pas sous quel
    protocole et sur quelle image il a été mesuré n'est pas un rapport prudent, c'est un rapport
    qu'on ne peut pas opposer — et l'accepter revient à croire ce qu'il ne dit pas.

    `image_courante` est **obligatoire** et doit porter les cinq champs : la comparaison itère sur
    elle, donc un appelant qui en omet un le retire silencieusement du contrôle.
    """
    if image_courante is None or set(image_courante) != set(CHAMPS_IMAGE):
        raise PlancherInvalide(
            f"{origine} : l'image du run courant est incomplète "
            f"({sorted(image_courante or {})}, attendu {sorted(CHAMPS_IMAGE)}) — une comparaison "
            "qui n'oppose qu'une partie des champs ne prouve pas l'égalité des images")
    racine = rapport.get("plancher_digest")
    if not isinstance(racine, str) or not racine:
        raise PlancherInvalide(
            f"{origine} : le rapport référencé ne porte pas de plancher_digest à sa racine — il ne "
            "dit pas contre quels seuils il a été mesuré")
    if racine != plancher_digest:
        raise PlancherInvalide(
            f"{origine} : le rapport référencé a été mesuré sous le plancher {racine!r} "
            f"(racine du rapport), le run courant sous {plancher_digest}")
    identite = rapport.get("identity")
    image = identite.get("image") if isinstance(identite, dict) else None
    if not isinstance(image, dict):
        raise PlancherInvalide(
            f"{origine} : le rapport référencé ne porte pas d'identité d'image "
            f"({type(image).__name__}) — rien n'y dit quel code a été mesuré")
    manquants = [champ for champ in CHAMPS_IMAGE if image.get(champ) is None]
    if manquants:
        raise PlancherInvalide(
            f"{origine} : l'identité d'image du rapport est incomplète (manquants : "
            f"{manquants}) — un champ absent n'est pas un champ concordant")
    for champ in CHAMPS_IMAGE:
        if image[champ] != image_courante[champ]:
            raise PlancherInvalide(
                f"{origine} : le rapport référencé porte {champ}={image[champ]!r}, "
                f"le run courant {image_courante[champ]!r} — la preuve ne mesure pas cette image")


def verifier_liaison_preuve(brut: Any, *, plancher_digest: str, candidate_revision: str,
                            report_bytes: bytes | None,
                            image_courante: dict[str, Any] | None = None) -> str:
    """Relie une preuve externe à **ce** candidat, et rend son `run_digest` racine.

    Intention M2 (`deferred-work.md`) : « une modification produit rend la réutilisation d'une preuve
    invalide ». Une preuve orchestrateur est un fichier JSON que le runner croit sur parole pour les
    mesures qu'il ne fait pas lui-même (tests hors ligne, A16, gardes anti-rustine et métamorphique).
    Sans lien, la même preuve pouvait être rejouée sur n'importe quel commit ultérieur : elle
    validerait alors un code qu'elle n'a jamais vu.

    Quatre liens, et les quatre sont exigés :

    - le **protocole** (`plancher_digest`) — deux runs ne se comparent qu'à plancher égal ;
    - la **révision candidate** (`candidate_revision`) — la preuve nomme le commit qu'elle mesure ;
    - le **rapport** (`report_digest` = sha256 des octets du rapport) — un rapport modifié après coup
      détache la preuve de ce qu'elle prétend résumer ;
    - le **run** (`run_digest` racine, le même sur chaque décision, **et recalculé** depuis
      l'identité du rapport) — une preuve dont les mesures viennent de runs différents n'est pas une
      preuve, c'est une compilation ; et un `run_digest` cru sur parole n'est qu'une chaîne ;
    - l'**image et le protocole** du rapport référencé (`pipeline_digest`, `prompts_digest`,
      `model_ids`, `plancher_digest`) : deux runs ne se comparent qu'à code, prompts, modèles et
      plancher égaux.

    Lève `PlancherInvalide` au premier écart, avec le chiffre en cause. L'appelant en fait un refus
    de tourner (code 2) : aucun gate n'est écrit.
    """
    if not isinstance(brut, dict):
        raise PlancherInvalide("preuve orchestrateur : un objet JSON est attendu")
    presentes = set(brut)
    if presentes != CLES_PREUVE_TRUSTED:
        manquantes = sorted(CLES_PREUVE_TRUSTED - presentes)
        superflues = sorted(presentes - CLES_PREUVE_TRUSTED)
        raise PlancherInvalide(
            "preuve orchestrateur : clés racine exactes attendues "
            f"{sorted(CLES_PREUVE_TRUSTED)}"
            + (f" — manquantes : {manquantes}" if manquantes else "")
            + (f" — en trop : {superflues}" if superflues else ""))
    if brut["plancher_digest"] != plancher_digest:
        raise PlancherInvalide(
            f"preuve orchestrateur : plancher_digest divergent "
            f"({brut['plancher_digest']!r}, attendu {plancher_digest})")
    if not _hex_exact(brut["candidate_revision"], 40):
        raise PlancherInvalide(
            "preuve orchestrateur : candidate_revision doit être 40 caractères hexadécimaux")
    if brut["candidate_revision"] != candidate_revision:
        raise PlancherInvalide(
            f"preuve orchestrateur : candidate_revision {brut['candidate_revision']} ≠ révision du "
            f"run {candidate_revision} — une preuve d'une autre révision ne mesure pas ce candidat")
    if not _hex_exact(brut["report_digest"], 64):
        raise PlancherInvalide(
            "preuve orchestrateur : report_digest doit être 64 caractères hexadécimaux")
    if report_bytes is None:
        raise PlancherInvalide(
            "preuve orchestrateur : le rapport qu'elle référence est absent ou illisible")
    observe = hashlib.sha256(report_bytes).hexdigest()
    if observe != brut["report_digest"]:
        raise PlancherInvalide(
            f"preuve orchestrateur : rapport modifié — sha256 observé {observe}, report_digest "
            f"annoncé {brut['report_digest']}")
    # **Le rapport doit se reconnaître dans la preuve** (revue B1). Recouper les seuls octets
    # prouvait que le fichier n'avait pas bougé, pas qu'il décrivait ce run-là : une preuve pouvait
    # référencer le rapport d'un autre run, d'une autre révision, et passer.
    try:
        rapport = json.loads(report_bytes)
        if not isinstance(rapport, dict):
            raise ValueError("un objet JSON est attendu")
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlancherInvalide(
            f"preuve orchestrateur : rapport référencé illisible ({type(exc).__name__})") from exc
    identite = rapport.get("identity")
    if not isinstance(identite, dict):
        raise PlancherInvalide(
            "preuve orchestrateur : le rapport référencé ne porte pas d'identité de run")
    if identite.get("run_digest") != brut["run_digest"]:
        raise PlancherInvalide(
            f"preuve orchestrateur : le rapport référencé porte run_digest "
            f"{identite.get('run_digest')!r}, la preuve annonce {brut['run_digest']} — la preuve "
            "ne décrit pas ce rapport")
    # **Le `run_digest` est recalculé**, jamais cru sur parole. Il est défini comme l'empreinte
    # canonique de l'identité privée de sa propre clé (`run.identite_run`) : sans ce recalcul, un
    # rapport fabriqué pouvait déclarer n'importe quel couple `(run_digest, candidate_revision)`
    # auto-cohérent, et la preuve s'y raccrochait sans rien prouver du tout.
    sans_digest = {cle: valeur for cle, valeur in identite.items() if cle != "run_digest"}
    recalcule = empreinte_canonique(sans_digest)
    if recalcule != identite.get("run_digest"):
        raise PlancherInvalide(
            f"preuve orchestrateur : le run_digest du rapport ne se recalcule pas depuis son "
            f"identité (annoncé {identite.get('run_digest')!r}, recalculé {recalcule}) — ce "
            "rapport a été fabriqué ou modifié")
    if identite.get("candidate_revision") != brut["candidate_revision"]:
        raise PlancherInvalide(
            f"preuve orchestrateur : le rapport référencé a mesuré la révision "
            f"{identite.get('candidate_revision')!r}, la preuve annonce "
            f"{brut['candidate_revision']}")
    verifier_identite_externe(rapport, plancher_digest=plancher_digest,
                              image_courante=image_courante, origine="preuve orchestrateur")
    if not _hex_exact(brut["run_digest"], 64):
        raise PlancherInvalide(
            "preuve orchestrateur : run_digest doit être 64 caractères hexadécimaux")
    decisions = brut["decisions"]
    if not isinstance(decisions, list) or not decisions:
        raise PlancherInvalide(
            "preuve orchestrateur : decisions doit être une liste non vide")
    for mesure in decisions:
        if not isinstance(mesure, dict):
            raise PlancherInvalide("preuve orchestrateur : chaque mesure est un objet")
        if mesure.get("run_digest") != brut["run_digest"]:
            raise PlancherInvalide(
                f"preuve orchestrateur : la mesure {mesure.get('metric')!r} porte un run_digest "
                "différent de celui de la preuve — les mesures d'une preuve viennent d'un run")
    return str(brut["run_digest"])


# --- la règle mécanique du checkpoint (trois rôles, rôle 1) ---------------------------------------

# Les trois champs qui font l'identité d'une configuration classée, et leur longueur exacte. La
# liste est ici, en un seul endroit, parce que la faute que le cycle de récupération ferme est
# d'avoir contrôlé cette identité à un endroit (la CLI) et pas aux deux autres (le modèle et le
# classement lui-même).
IDENTITE_CLASSEMENT: tuple[tuple[str, int], ...] = (
    ("candidate_revision", 40), ("run_digest", 64), ("report_digest", 64),
)


class ClassementInvalide(Exception):
    """Un classement a été demandé sur des candidats qu'on ne peut pas opposer.

    Story 4.5, revues B1 successives. Les tours précédents ont fermé la voie **CLI**
    (`--candidate-revision` exigé), puis le **modèle** (`Configuration` refusant une identité
    absente ou mal formée). Il restait la faute de fond : le classement acceptait des
    `Configuration` **déclaratives**, si bien que trois chaînes hexadécimales inventées —
    `candidate_revision="a"*40`, `run_digest="b"*64`, `report_digest="c"*64` — suffisaient à obtenir
    une tête de classement. Présence et syntaxe ne sont pas une liaison : un domaine de valeur n'est
    pas une preuve.

    Le classement n'accepte donc plus que des `CandidatClassement`, c'est-à-dire les **octets** d'un
    rapport, dont il recalcule lui-même l'identité avant tout tri. Le refus est porté par le
    classement, jamais par ce que l'appelant a bien voulu fournir : il n'y a aucun paramètre pour le
    désarmer, et un drapeau « déjà vérifiée » n'en serait qu'un déguisé.
    """


class Configuration(BaseModel):
    """Le **résultat** d'un classement : l'admissibilité d'un candidat, son identité, ses deux coûts.

    Ce n'est plus une entrée : `classer_configurations` n'accepte que des `CandidatClassement`
    (nom + octets du rapport) et construit lui-même ces objets en recalculant leurs trois
    empreintes. Une `Configuration` fabriquée à la main reste donc parfaitement constructible — elle
    n'ouvre simplement aucune voie vers une décision de promotion, ce qui est la seule propriété qui
    compte (revue B1, tour correctif 1/3).

    `frozen` ferme l'affectation d'attribut, et rien de plus (revue R9) : `model_copy(update=…)` et
    `model_construct(…)` produisent toujours une copie amputée sans repasser par le validateur.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    admissible: bool
    cost_eur: float = Field(ge=0, allow_inf_nan=False)
    latency_ms: int = Field(ge=0)
    # **La révision opposée**, portée par l'artefact de promotion (revue B1). Sans elle, le JSON de
    # classement était inauditable après coup : rien dedans ne disait à quel candidat il se
    # rapportait — ni à la racine, ni par configuration. Les trois champs sont **obligatoires** : un
    # résultat de classement sans identité n'a pas de raison d'exister.
    candidate_revision: str
    run_digest: str
    report_digest: str

    @model_validator(mode="after")
    def _identite_opposable(self) -> Configuration:
        """L'identité est présente et bien formée — la liaison, elle, se prouve ailleurs.

        Ce validateur ne dit que la **forme**. Ce qui tient la propriété « aucune promotion
        anonyme ni fabriquée » est `classer_configurations`, qui recalcule les trois empreintes
        depuis les octets d'un rapport et les oppose à l'image candidate. Écrire l'inverse ferait
        croire à une garantie qu'un motif hexadécimal ne donne pas.
        """
        for champ, longueur in IDENTITE_CLASSEMENT:
            valeur = getattr(self, champ)
            if not _hex_exact(valeur, longueur):
                raise ValueError(
                    f"configuration {self.name!r} : {champ} doit être {longueur} caractères "
                    f"hexadécimaux (reçu {valeur!r})")
        return self


class CandidatClassement(BaseModel):
    """Ce qu'un classement accepte : un nom, et les **octets** du rapport qui le justifie.

    Story 4.5, tour correctif 1/3, revue B1. Le classement recevait des `Configuration`
    déclaratives : `admissible`, `cost_eur`, `latency_ms` et trois empreintes, tous **annoncés** par
    l'appelant. Rien n'était recalculé depuis un octet réel, si bien qu'un appelant bibliothèque
    obtenait une tête de classement avec trois empreintes bien formées et entièrement inventées.

    Un candidat ne peut donc plus rien annoncer. Il porte le rapport lui-même ; `report_digest` est
    le sha256 de ces octets, `run_digest` est recalculé depuis l'identité du rapport,
    `candidate_revision` est celle que le rapport a mesurée, et l'admissibilité est **lue** du
    rapport, jamais reçue.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    report_bytes: bytes = Field(min_length=1)


def _candidat_depuis_item(item: Any, *, base: Path) -> CandidatClassement:
    """`{name, report}` du JSON de la CLI → les octets du rapport nommé."""
    if not isinstance(item, dict) or set(item) != {"name", "report"}:
        raise ValueError("chaque configuration porte exactement name et report")
    report_path = Path(str(item["report"]))
    if not report_path.is_absolute():
        report_path = base / report_path
    return CandidatClassement(name=str(item["name"]), report_bytes=report_path.read_bytes())


def _configuration_depuis_octets(name: str, octets: bytes, *, plancher_digest: str,
                                 image_courante: dict[str, Any] | None,
                                 candidate_revision: str) -> Configuration:
    """Une configuration candidate, **dérivée des octets d'un rapport qu'on a d'abord opposé**.

    C'est la fonction qui décide de ce qui est promu au checkpoint : ce qu'elle déclare `admissible`
    décide de ce qui est promu. Aucun de ses trois identifiants ne vient de l'appelant —
    `report_digest` est le sha256 des octets reçus, `run_digest` est recalculé depuis l'identité du
    rapport, `candidate_revision` est celle que le rapport dit avoir mesurée et qui doit égaler
    celle du classement. Le protocole et l'image sont opposés par `verifier_identite_externe`.
    """
    # **La révision est exigée, validée, puis opposée** — dans cet ordre (revue B1). Le contrôle
    # existait mais restait *opt-in de l'appelant* : `--candidate-revision` n'était pas `required`,
    # la signature l'acceptait à `None`, et un rapport dont le seul écart était
    # `identity.candidate_revision = null` — exactement ce qu'écrit `identite_run` sans
    # `--candidate-revision` — était classé `admissible: true`. Une décision de promotion ne peut
    # pas dépendre du bon vouloir de qui l'appelle.
    if not _hex_exact(candidate_revision, 40):
        raise ValueError(
            "classement : la révision candidate est obligatoire et doit être 40 caractères "
            f"hexadécimaux (reçu {candidate_revision!r}) — promouvoir sans savoir quel candidat "
            "est promu n'est pas une promotion, c'est un tirage")
    origine = f"rapport de la configuration {name!r}"
    try:
        rapport = json.loads(octets)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{origine} illisible ({type(exc).__name__})") from exc
    if not isinstance(rapport, dict) or rapport.get("schema_version") != 3:
        raise ValueError(f"{origine} hors schéma 3")
    decisions = rapport.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError(f"{origine} sans décisions")
    try:
        verifier_identite_externe(rapport, plancher_digest=plancher_digest,
                                  image_courante=image_courante,
                                  origine=f"configuration {name!r}")
    except PlancherInvalide as exc:
        raise ValueError(str(exc)) from exc
    identite = rapport.get("identity") or {}
    run_digest = identite.get("run_digest")
    if not isinstance(run_digest, str) or len(run_digest) != 64:
        raise ValueError(f"{origine} sans run_digest")
    # **Recalculé**, jamais lu : `run.identite_run` définit le digest comme l'empreinte canonique de
    # l'identité privée de sa propre clé. Un digest cru sur parole n'est qu'une chaîne.
    recalcule = empreinte_canonique(
        {cle: valeur for cle, valeur in identite.items() if cle != "run_digest"})
    if recalcule != run_digest:
        raise ValueError(
            f"{origine} : run_digest {run_digest} ne se recalcule pas depuis son "
            f"identité (recalculé {recalcule}) — ce rapport a été fabriqué ou modifié")
    if identite.get("candidate_revision") != candidate_revision:
        raise ValueError(
            f"{origine} : mesuré sur la révision "
            f"{identite.get('candidate_revision')!r}, classement demandé pour {candidate_revision}")
    metrics = rapport.get("metrics") or {}
    cost = rapport.get("cost_eur")
    latency = metrics.get("latency_p50_ms")
    if (isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost)
            or isinstance(latency, bool) or not isinstance(latency, int)):
        raise ValueError(f"{origine} sans coût/latence finis")
    # `not rapport.get("unexecuted_cases")` faisait dire « aucune exécution manquante » à « la clé
    # n'est pas là », **dans la décision qui promeut** : un rapport qui omet la clé contribuait à
    # `admissible=True` (revue B5, chemin frère). La clé est donc exigée, comme partout ailleurs.
    if "unexecuted_cases" not in rapport or not isinstance(rapport["unexecuted_cases"], list):
        raise ValueError(
            f"{origine} : 'unexecuted_cases' absent ou mal typé — « la clé n'est pas "
            "là » n'est pas « aucune exécution manquante », et cette différence décide de la "
            "promotion")
    admissible = bool(rapport.get("complete") is True
                      and not rapport["unexecuted_cases"]
                      and all(isinstance(d, dict) and d.get("status") == "green"
                              and d.get("producer") == "orchestrator" for d in decisions))
    return Configuration(
        name=name, admissible=admissible, cost_eur=float(cost),
        latency_ms=latency, candidate_revision=candidate_revision, run_digest=run_digest,
        report_digest=hashlib.sha256(octets).hexdigest())


def _configuration_depuis_rapport(item: Any, *, base: Path, plancher_digest: str,
                                  image_courante: dict[str, Any] | None,
                                  candidate_revision: str) -> Configuration:
    """`{name, report}` → la configuration dérivée des octets de ce rapport (voie fichier)."""
    if not _hex_exact(candidate_revision, 40):
        raise ValueError(
            "classement : la révision candidate est obligatoire et doit être 40 caractères "
            f"hexadécimaux (reçu {candidate_revision!r}) — promouvoir sans savoir quel candidat "
            "est promu n'est pas une promotion, c'est un tirage")
    candidat = _candidat_depuis_item(item, base=base)
    return _configuration_depuis_octets(
        candidat.name, candidat.report_bytes, plancher_digest=plancher_digest,
        image_courante=image_courante, candidate_revision=candidate_revision)


def verifier_identite_classement(configurations: list[Configuration]) -> None:
    """Un classement se fait entre configurations **du même candidat**, toutes identifiées.

    Story 4.5, cycle de récupération, revue B1 — même forme que `verifier_liaison_preuve` et
    `verifier_identite_externe`, et pour la même raison : exiger la présence, puis le type, puis
    l'égalité. Trois refus, et ils sont tous des refus de classer :

    - une configuration dont l'identité **manque** : rien ne dit ce qui serait promu ;
    - une configuration dont l'identité est **mal formée** (longueur ou alphabet) : ce n'est pas une
      empreinte, c'est une chaîne qui y ressemble ;
    - deux **révisions candidates** différentes dans la même liste : comparer les coûts de deux
      commits et en promouvoir un n'est pas un classement, c'est une confusion.

    Ne rend rien : c'est un garde-fou, pas une lecture. Il est appelé par `classer_configurations`,
    et exposé pour qu'un futur chemin de promotion puisse s'y adosser au lieu d'en réinventer un.
    """
    revisions: set[str] = set()
    for configuration in configurations:
        if not isinstance(configuration, Configuration):
            raise ClassementInvalide(
                "classement : chaque élément doit être une Configuration "
                f"({type(configuration).__name__} reçu)")
        for champ, longueur in IDENTITE_CLASSEMENT:
            valeur = getattr(configuration, champ, None)
            if valeur is None:
                raise ClassementInvalide(
                    f"classement : la configuration {configuration.name!r} n'a pas de {champ} — "
                    "une liste où l'on ne sait pas ce qui serait promu ne se classe pas")
            if not _hex_exact(valeur, longueur):
                raise ClassementInvalide(
                    f"classement : la configuration {configuration.name!r} porte un {champ} mal "
                    f"formé ({valeur!r}, attendu {longueur} caractères hexadécimaux)")
        revisions.add(str(configuration.candidate_revision))
    if len(revisions) > 1:
        raise ClassementInvalide(
            f"classement : deux révisions candidates dans la même liste ({sorted(revisions)}) — "
            "classer des mesures de commits différents ne compare rien")


def classer_configurations(candidats: Any, *, plancher_digest: str,
                           image_courante: dict[str, Any],
                           candidate_revision: str) -> list[Configuration]:
    """Classement mécanique : admissibles au plancher d'abord, puis la moins chère, puis la plus rapide.

    C'est la règle du checkpoint de finalisation (spec 4.2b) : aucun jugement, aucune pondération.
    Une configuration inadmissible n'est jamais promue devant une admissible, quel que soit son
    coût ; `aucun_admissible` (liste sans admissible) est un résultat rouge que l'appelant publie
    tel quel, jamais une question humaine.

    **L'API publique accepte des rapports, pas des déclarations** (revue B1, tour correctif 1/3).
    Elle acceptait auparavant des `Configuration` : `admissible`, coût, latence et trois empreintes,
    tous annoncés par l'appelant. Les tours précédents avaient durci la *présence* et la *syntaxe*
    de ces empreintes, jamais le fait qu'elles correspondent à quelque chose de réel — si bien que
    `Configuration(admissible=True, candidate_revision="a"*40, run_digest="b"*64,
    report_digest="c"*64)` était classée **en tête**. Un objet de preuve marqué « vérifié », un
    drapeau ou un constructeur privé auraient été contournables de la même façon.

    Chaque candidat porte donc les **octets** de son rapport, et cette fonction fait elle-même,
    avant tout tri, ce que `_configuration_depuis_octets` fait déjà : sha256 des octets pour
    `report_digest`, recalcul du `run_digest` depuis l'identité privée de sa clé, opposition de
    `candidate_revision`, du `plancher_digest` et des cinq champs de l'image candidate. Une identité
    fabriquée ne se recalcule pas : elle ferme.

    Lève `ClassementInvalide` au premier écart, avant qu'aucun ordre ne soit rendu — jamais un
    classement partiel, jamais une tête par défaut, et aucun paramètre ne le désarme.
    """
    # **L'argument est matérialisé avant d'être parcouru deux fois** (revue R12). Le contrôle itère,
    # puis `sorted` itère à nouveau : un appelant passant un générateur — parfaitement légitime au
    # regard de la signature — obtenait un classement **vide**, donc `aucun_admissible: true` pour
    # des candidats bien réels. Dans la fonction dont tout l'objet est qu'aucune voie bibliothèque ne
    # produise une décision de promotion non opposée, un épuisement silencieux est la même faute que
    # celles que ce cycle ferme : une donnée absente rendue comme un résultat.
    liste = list(candidats)
    for candidat in liste:
        if not isinstance(candidat, CandidatClassement):
            raise ClassementInvalide(
                "classement : chaque candidat porte les octets de son rapport "
                f"(CandidatClassement attendu, {type(candidat).__name__} reçu) — une configuration "
                "déclarative ne prouve rien de ce qu'elle annonce")
    noms = [c.name for c in liste]
    if len(set(noms)) != len(noms):
        raise ClassementInvalide(
            "classement : les noms de configurations doivent être uniques "
            f"({sorted(nom for nom in set(noms) if noms.count(nom) > 1)} en double)")
    configurations: list[Configuration] = []
    for candidat in liste:
        try:
            configurations.append(_configuration_depuis_octets(
                candidat.name, candidat.report_bytes, plancher_digest=plancher_digest,
                image_courante=image_courante, candidate_revision=candidate_revision))
        except (ValueError, ValidationError) as exc:
            raise ClassementInvalide(f"classement : {exc}") from exc
    verifier_identite_classement(configurations)
    return sorted(configurations,
                  key=lambda c: (not c.admissible, c.cost_eur, c.latency_ms, c.name))


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m server.evals.plancher",
        description="Plancher 4.2b : digest du protocole et règle mécanique de classement.")
    parser.add_argument("--digest", action="store_true", help="afficher le digest du plancher chargé")
    parser.add_argument("--classer", type=Path, metavar="CONFIGS_JSON",
                        help="classer des configurations candidates (JSON : liste d'objets "
                             "{name, report})")
    parser.add_argument("--candidate-revision", metavar="SHA40", required=False,
                        help="OBLIGATOIRE avec --classer : révision produit dont on classe les "
                             "configurations ; chaque rapport doit l'avoir mesurée, et le JSON de "
                             "classement la porte")
    args = parser.parse_args(argv)
    try:
        charge = charger_plancher()
    except PlancherInvalide as exc:
        print(f"refus : {exc}", file=sys.stderr)
        return 2
    if args.classer is not None:
        if not _hex_exact(args.candidate_revision, 40):
            # Une promotion sans candidat nommé n'est pas une promotion. Le contrôle est ici **et**
            # dans `_configuration_depuis_rapport` : le second est l'invariant, le premier est le
            # message que l'opérateur lit.
            print("refus : --classer exige --candidate-revision <40 hexadécimaux> — un classement "
                  "qui ne nomme pas le candidat qu'il promeut est inauditable", file=sys.stderr)
            return 2
        try:
            brut = json.loads(args.classer.read_text(encoding="utf-8"))
            if not isinstance(brut, list):
                raise ValueError("une liste de configurations est attendue")
            candidats = [_candidat_depuis_item(item, base=args.classer.parent) for item in brut]
        except (OSError, ValueError, ValidationError) as exc:
            print(f"refus : configurations illisibles ({type(exc).__name__}: {exc})", file=sys.stderr)
            return 2
        try:
            # L'image du run courant, complète : la comparaison itère dessus, et un champ omis
            # serait un champ jamais opposé (revue B1).
            classement = classer_configurations(
                candidats, plancher_digest=charge.digest,
                image_courante={
                    "pipeline_digest": pipeline_digest(),
                    "prompts_digest": prompts_digest(),
                    "model_ids": dict(TIERS),
                    "normalize_version": normalize_version,
                    "plancher_digest": charge.digest,
                },
                candidate_revision=args.candidate_revision)
        except ClassementInvalide as exc:
            # Le refus du classement est **dit à l'appelant**, jamais rabattu sur un classement
            # dégradé : une promotion qu'on ne peut pas auditer ne se rend pas « au mieux ».
            print(f"refus : {exc}", file=sys.stderr)
            return 2
        aucun_admissible = not any(c.admissible for c in classement)
        print(json.dumps({
            "plancher_digest": charge.digest,
            # L'artefact de promotion dit **à quel candidat** il se rapporte, à la racine comme par
            # configuration : sans cela il est inauditable après coup (revue B1). La racine porte la
            # révision **que le classement a effectivement opposée** — chaque rapport l'a mesurée,
            # et `verifier_identite_classement` a vérifié qu'elles étaient toutes égales —, pas
            # seulement celle que l'opérateur a tapée.
            "candidate_revision": (classement[0].candidate_revision if classement
                                   else args.candidate_revision),
            "aucun_admissible": aucun_admissible,
            "classement": [c.model_dump() for c in classement],
        }, ensure_ascii=False, indent=2))
        # `aucun_admissible` est un rouge publié, jamais une question (Boundaries 4.2b).
        return 1 if aucun_admissible else 0
    print(f"plancher_digest={charge.digest} temoins={len(charge.plancher.temoins)} "
          f"n_minimum={charge.plancher.n_minimum}")
    return 0


if __name__ == "__main__":  # pragma: no cover — point d'entrée
    raise SystemExit(_main())
