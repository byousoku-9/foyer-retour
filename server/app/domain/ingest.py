"""AD-7 / AD-8 — Contrats partagés entre l'ingestion, le corpus et l'API : rapport, gate, manifest."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .document import DomainModel

CheckLevel = Literal["bloquant", "alerte", "info"]
GateProfile = Literal["vertical", "full"]
ManifestStatus = Literal["servi", "quarantaine"]


class Check(DomainModel):
    """Un contrôle statique d'ingestion (AD-8) : calculé sans pipeline."""

    name: str
    level: CheckLevel
    detail: str = ""


class Report(DomainModel):
    """`report.json` : liste de checks + statistiques descriptives."""

    doc_id: str
    checks: list[Check] = Field(default_factory=list)
    stats: dict[str, int | float | str | dict[str, int]] = Field(default_factory=dict)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.level == "bloquant"]

    @property
    def alerts(self) -> list[Check]:
        return [c for c in self.checks if c.level == "alerte"]


class GateDecision(DomainModel):
    """Une décision chiffrée du gate (story 4.2b) — jamais un booléen écrasable.

    Chaque décision dit **quoi** a été mesuré (`metric`), **par qui** (`producer` — la règle trusted
    ne reconnaît que l'orchestrateur comme producteur de preuve ; un run de builder est un
    diagnostic), **contre quel plancher** (`threshold`, tiré de `server/evals/reference/plancher.yaml`
    et couvert par son digest), **sur quel périmètre** (`scope`), **sur combien d'exécutions** (`n`,
    interruptions comprises au dénominateur), et **depuis quel run** (`run_digest`, l'empreinte
    canonique de l'identité du run). `value` et `status` sont le constat — un run interrompu ou
    `aucun_admissible` est `red`, jamais retiré du dénominateur.
    """

    metric: str
    producer: str
    threshold: float
    scope: str
    n: int = Field(ge=0)
    run_digest: str
    value: float
    status: Literal["green", "red"]
    # Pourquoi la décision est rouge quand la valeur seule ne le dit pas : une preuve
    # sous-échantillonnée (`n < N` du témoin pré-enregistré) ou un témoin bloquant que le run n'a
    # pas prouvé. `None` quand le statut se lit sur `value` contre `threshold` (revue 4.2b, HIGH 1).
    reason: str | None = None


class Gate(DomainModel):
    """Écrit uniquement par `evals run --gate {doc_id}` (AD-7) ; jamais par l'ingestion."""

    profile: GateProfile
    source_hash: str
    ingest_fingerprint: str
    cases_hash: str
    pipeline_digest: str
    prompts_digest: str
    model_ids: dict[str, str] = Field(default_factory=dict)
    evals_ok: bool
    date: str
    overlay_hash: str | None = None  # empreinte de `typing.manual.json` au moment du gate (revue Codex 1.2)
    # Nombre de cas de la suite réellement exécutés par le run qui a écrit ce gate (story 1.10).
    # L'accueil annonce « niveau de validation : vertical — N cas relus à la main » : ce N est une
    # propriété **du gate**, pas un littéral de la page — écrit là où il est constaté, publié par
    # `/api/v1/sante` (`gate_cases`), et couvert par `cases_hash` qui dit *quels* cas c'étaient.
    #
    # **Obligatoire, et ≥ 1** (revue Codex 1.10, I3). Un défaut à 0 rendait valide un gate écrit à la
    # main sans ce champ : le document était servi, `/api/v1/sante` publiait `gate_profile: "vertical"`
    # avec `gate_cases: 0`, et les deux fronts — qui savent qu'un run refuse de tourner sur zéro cas —
    # déclaraient ce corps illisible. Un serveur parfaitement vivant faisait donc dire aux pages
    # « le serveur n'a pas répondu ». Le plancher appartient au domaine, pas à deux clients : un gate
    # sans `cases`, ou à 0, est désormais une entrée de manifest invalide, et le loader met **ce seul**
    # document en quarantaine (AD-7) au lieu de le servir sous un contrat que personne ne sait lire.
    cases: int = Field(ge=1)
    # Les cas exécutés portent-ils **tous** la contresignature humaine de leur relecture ?
    # (amendement AD-7 / AD-14, revue Codex 1.10 tour 2, B2)
    #
    # AD-14 définit `vertical` comme « un cas guide et un cas sinistre **relus à la main** », et
    # exige que ce soit « affiché comme tel ». La relecture qui fonde ces deux cas a été faite par la
    # boucle autonome ; la contresignature de la personne à qui `epics.md` l'attribue reste due. Sans
    # ce champ, `/` affirmait « 2 cas relus à la main » sur la seule foi du nom du profil — une
    # affirmation de relecture humaine que rien dans le dépôt n'établissait, c'est-à-dire exactement
    # la classe d'invention qu'AD-16 interdit et que cette story combat.
    #
    # `truth.countersigned_by` porte la contresignature **par cas** ; ce booléen est la conjonction
    # sur les cas du run, écrite là où elle est constatée. Il est **obligatoire**, comme `cases` :
    # la phrase publiée par l'accueil bascule dessus, et un gate qui ne le dit pas laisserait le
    # loader choisir à la place du run — alors qu'AD-7 réserve l'écriture du gate au runner.
    countersigned: bool
    # Story 4.2b : les décisions chiffrées qui fondent `evals_ok`, et l'empreinte du run qui les a
    # produites. **Optionnels** (défauts vides) : les gates déjà écrits — antérieurs à cette story —
    # restent valides au schéma (cf. la compatibilité de `run.py::ecrire_gate`), et un gate sans
    # décisions se lit comme « mesuré avant le protocole 4.2b », jamais comme un vert par défaut.
    decisions: list[GateDecision] = Field(default_factory=list)
    run_digest: str | None = None
    pipeline_settings: dict[str, int | float | str | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _decision_coherente(self) -> Gate:
        if self.decisions and self.evals_ok != all(d.status == "green" for d in self.decisions):
            raise ValueError("evals_ok diverge des décisions chiffrées")
        return self


class GateContext(DomainModel):
    """Ce que l'image en cours sait d'elle-même ; comparé au gate du manifest ⇒ alerte `gate_perime` (AD-7)."""

    pipeline_digest: str = ""
    prompts_digest: str = ""
    model_ids: dict[str, str] = Field(default_factory=dict)
    pipeline_settings: dict[str, int | float | str | bool] = Field(default_factory=dict)


class ManifestEntry(DomainModel):
    status: ManifestStatus
    source_hash: str
    ingest_fingerprint: str
    document_hash: str  # sha256 de `document.json`, recalculé par le loader (AD-7)
    edition: str
    # sha256 de `typing.manual.json` (None si absent à l'ingestion) : l'overlay est couvert par le manifest et par
    # le gate comme `document.json` l'est par `document_hash` (amendement AD-7, revue Codex 1.2).
    overlay_hash: str | None = None
    gate: Gate | None = None


Manifest = dict[str, ManifestEntry]
