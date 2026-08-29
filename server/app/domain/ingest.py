"""AD-7 / AD-8 — Contrats partagés entre l'ingestion, le corpus et l'API : rapport, gate, manifest."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .document import DomainModel

CheckLevel = Literal["bloquant", "alerte", "info"]
GateProfile = Literal["vertical", "full"]
ManifestStatus = Literal["servi", "quarantaine"]
_HEX = frozenset("0123456789abcdef")


def _hexadecimal(valeur: str, longueur: int) -> bool:
    return len(valeur) == longueur and all(c in _HEX for c in valeur)


class Check(DomainModel):
    """Un contrôle statique d'ingestion (AD-8) : calculé sans pipeline."""

    name: str
    level: CheckLevel
    detail: str = ""


# Les deux checks d'ingestion **déterministes** qui portent une preuve de structure, et les préfixes
# de leurs **attestations affirmatives** (story 4.5). Le vocabulaire vit dans `domain` parce que deux
# couches le lisent : l'ingestion l'écrit (`server/ingest/report.py`), le gate `full` le relit
# (`server/evals/run.py`). Un format recopié des deux côtés aurait divergé.
#
# Deux checks, parce que les deux chemins d'ingestion prouvent deux choses différentes :
#
# - `structure_proposee` (story 4.2c) n'existe que pour un document **issu d'un PDF** : le
#   vérificateur y confronte une proposition d'arbre au registre des lignes extraites ;
# - `invariants_arbre` est émis par **les deux** ingestions (`build_report`, `build_pdf_report`) :
#   c'est le contrôle déterministe qui dit que l'arbre construit tient ses invariants — le seul dont
#   une copie de site dispose, et donc la preuve de structure applicable au guide.
#
# Le périmètre auquel chaque preuve s'applique n'est **jamais** décidé par un `doc_id` : c'est la
# règle `SOURCE_FILES` du loader (première source présente) qui distingue un document PDF d'une copie
# de site, côté gate comme côté service.
STRUCTURE_CHECK = "structure_proposee"
TREE_CHECK = "invariants_arbre"
_ATTESTATION_STRUCTURE = "structure acceptee"
_ATTESTATION_ARBRE = "arbre atteste"


def _detail_attestation(prefixe: str, champs: dict[str, str], detail: str) -> str:
    """`{prefixe} cle=valeur … ; {detail}` — la forme unique des deux attestations.

    `detail` est **d'abord débarrassé** d'une attestation antérieure : un chemin qui ré-atteste (le
    typage, qui réécrit `report.json` après avoir changé `document.json`) empilerait sinon deux
    attestations dans le même détail, et la plus ancienne — celle qui ne décrit plus rien — resterait
    lisible à côté de la neuve.
    """
    suffixe = _detail_sans_attestation(detail)
    return " ".join([prefixe, *(f"{cle}={valeur}" for cle, valeur in champs.items())]) + (
        f" ; {suffixe}" if suffixe else "")


def _detail_sans_attestation(detail: str) -> str:
    for prefixe in (_ATTESTATION_STRUCTURE, _ATTESTATION_ARBRE):
        if detail.startswith(prefixe):
            _, _, reste = detail.partition(";")
            return reste.strip()
    return detail


def _lire_attestation(prefixe: str, cles: tuple[str, ...], detail: str) -> tuple[str, ...] | None:
    if not detail.startswith(prefixe):
        return None
    champs: dict[str, str] = {}
    for morceau in detail[len(prefixe):].split(";")[0].split():
        cle, _, valeur = morceau.partition("=")
        if valeur:
            champs[cle] = valeur
    valeurs = tuple(champs.get(cle, "") for cle in cles)
    return valeurs if all(valeurs) else None


def detail_attestation_structure(*, document_hash: str, structure_hash: str,
                                 detail: str = "") -> str:
    """Le détail d'une attestation affirmative : **à quel document** la structure acceptée se rattache.

    Sans les deux empreintes, « affirmatif » resterait déclaratif : n'importe quel `structure.json`
    accompagné d'un rapport qui dit « accepté » suffirait à prouver une structure. Les recopier lie
    l'attestation aux octets exacts que l'ingestion a vérifiés.
    """
    return _detail_attestation(
        _ATTESTATION_STRUCTURE,
        {"document_hash": document_hash, "structure_hash": structure_hash}, detail)


def lire_attestation_structure(detail: str) -> tuple[str, str] | None:
    """`(document_hash, structure_hash)` d'une attestation affirmative, ou `None` si ce n'en est pas une."""
    valeurs = _lire_attestation(_ATTESTATION_STRUCTURE, ("document_hash", "structure_hash"), detail)
    return None if valeurs is None else (valeurs[0], valeurs[1])


def detail_attestation_arbre(*, document_hash: str, ingest_fingerprint: str,
                             detail: str = "") -> str:
    """Le détail de l'attestation d'arbre : **quel arbre**, produit par **quelle ingestion**.

    C'est le pendant, pour un document qui n'est pas issu d'un PDF, de l'attestation de structure :
    l'ingestion affirme que ses contrôles déterministes d'arbre ont tenu, et le dit **de ce
    document-là**. Sans les deux empreintes, un `report.json` écrit à la main portant
    `invariants_arbre: ok` — la forme historique du check — suffirait à faire verdir le témoin ;
    c'est exactement le fail-open que la story ferme côté PDF.

    `document_hash` est l'empreinte des octets de `document.json`, celle que le loader recoupe avec
    le manifest à chaque chargement ; `ingest_fingerprint` nomme le code qui a produit l'arbre. Une
    réingestion ou une modification d'un seul des deux détache l'attestation, et le témoin redevient
    rouge sans qu'une ligne de code n'ait à s'en apercevoir.
    """
    return _detail_attestation(
        _ATTESTATION_ARBRE,
        {"document_hash": document_hash, "ingest_fingerprint": ingest_fingerprint}, detail)


def lire_attestation_arbre(detail: str) -> tuple[str, str] | None:
    """`(document_hash, ingest_fingerprint)` de l'attestation d'arbre, ou `None` si ce n'en est pas une."""
    valeurs = _lire_attestation(_ATTESTATION_ARBRE, ("document_hash", "ingest_fingerprint"), detail)
    return None if valeurs is None else (valeurs[0], valeurs[1])


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
    # Story 4.5 — **ce qu'un gate `full` doit porter pour être relisible**. Optionnels au schéma
    # (les gates `vertical` déjà écrits restent valides), obligatoires sous `profile: "full"` :
    #
    # - `plancher_digest` : le protocole contre lequel les décisions ont été prises. Sans lui, une
    #   décision `value >= threshold` ne dit pas *quel* threshold, et deux gates de plancher
    #   différents se compareraient comme s'ils étaient de même mesure.
    # - `candidate_revision` : le commit mesuré. C'est ce qui empêche un gate d'être réutilisé par
    #   une révision qu'il n'a jamais vue.
    # - `report_digest` : l'empreinte du rapport de run, pour qu'on puisse retrouver — et vérifier —
    #   les chiffres derrière `evals_ok`.
    # - `structure_hash` : l'empreinte de `structure.json` **au moment du run**, recopiée de l'entrée
    #   du manifest, sur le patron d'`overlay_hash`.
    plancher_digest: str | None = None
    candidate_revision: str | None = None
    report_digest: str | None = None
    structure_hash: str | None = None

    @model_validator(mode="after")
    def _decision_coherente(self) -> Gate:
        if self.decisions and self.evals_ok != all(d.status == "green" for d in self.decisions):
            raise ValueError("evals_ok diverge des décisions chiffrées")
        return self

    @model_validator(mode="after")
    def _full_porte_son_protocole(self) -> Gate:
        """Sous `full`, le protocole, la révision et le rapport sont **exigés et bien formés**.

        Ce que ce validateur ne fait pas, et pourquoi : il n'exige **pas** `structure_hash`. Cette
        empreinte n'existe que si l'ingestion a produit un `structure.json` (story 4.2c) ; l'exiger
        rendrait impossible d'écrire le moindre gate `full` tant que la réingestion réelle est due, et
        un gate impossible à écrire n'est pas un gate fail-closed, c'est un gate absent. L'état réel
        se dit ailleurs, et il se dit en rouge : le témoin `structure_prouvee_rate` du plancher rend
        la décision rouge exactement quand l'artefact manque. Une exigence de schéma aurait remplacé
        un rouge chiffré et publiable par une exception. Quand elle est là, en revanche, elle est
        contrôlée comme les autres.
        """
        if self.profile != "full":
            return self
        for champ, longueur in (("plancher_digest", 64), ("candidate_revision", 40),
                                ("report_digest", 64)):
            valeur = getattr(self, champ)
            if valeur is None:
                raise ValueError(
                    f"un gate `full` porte {champ} : la politique complète se réclame de son "
                    "protocole, de sa révision et de son rapport")
            if not _hexadecimal(valeur, longueur):
                raise ValueError(
                    f"gate `full` : {champ} doit être {longueur} caractères hexadécimaux")
        if self.structure_hash is not None and not _hexadecimal(self.structure_hash, 64):
            raise ValueError("gate `full` : structure_hash doit être 64 caractères hexadécimaux")
        return self


class GateContext(DomainModel):
    """Ce que l'image en cours sait d'elle-même ; comparé au gate du manifest ⇒ alerte `gate_perime` (AD-7)."""

    pipeline_digest: str = ""
    prompts_digest: str = ""
    model_ids: dict[str, str] = Field(default_factory=dict)
    pipeline_settings: dict[str, int | float | str | bool] = Field(default_factory=dict)
    # Story 4.5 (revue B2) — la **révision produit qui tourne**, telle que le service la connaît
    # (`Settings.git_sha`). `pipeline_digest` couvre cinq couches, pas le dépôt entier : une
    # modification produit hors de ces couches ne le fait pas bouger, et un gate `full` d'un ancien
    # commit restait servi sans une alerte alors qu'il affirme avoir mesuré *ce* code.
    #
    # En production, `deploy.yml` pose `GIT_SHA=<sha7>` : la comparaison se fait donc sur le
    # **préfixe commun**, jamais sur l'égalité stricte. Vide ou `dev` (hors conteneur), la révision
    # est inconnue et rien n'est comparé — mettre tout un corpus en quarantaine parce qu'un poste de
    # développement ne se nomme pas serait une panne inventée.
    candidate_revision: str = ""
    # `prod` ou `dev`. Sous un gate `full`, une révision **inconnue** est une preuve manquante en
    # production et une simple ignorance ailleurs : la règle est celle qu'AD-7 applique déjà à
    # `allow_ungated`, et elle a besoin de savoir où elle tourne.
    env: str = "dev"


class ManifestEntry(DomainModel):
    status: ManifestStatus
    source_hash: str
    ingest_fingerprint: str
    document_hash: str  # sha256 de `document.json`, recalculé par le loader (AD-7)
    edition: str
    # sha256 de `typing.manual.json` (None si absent à l'ingestion) : l'overlay est couvert par le manifest et par
    # le gate comme `document.json` l'est par `document_hash` (amendement AD-7, revue Codex 1.2).
    overlay_hash: str | None = None
    # sha256 de `structure.json` (None si absent à l'ingestion), sur le patron exact d'`overlay_hash`
    # (story 4.5, dette `deferred-work.md` « structure.json au manifest »). La proposition de
    # structure de 4.2c était le seul artefact d'ingestion que le manifest ne couvrait pas : une main
    # sur `data/{doc_id}/structure.json` ne se voyait donc nulle part, alors que c'est lui qui décide
    # de l'arbre que le rappel parcourt. Le loader contrôle déclaré ⟺ présent, puis la valeur.
    structure_hash: str | None = None
    gate: Gate | None = None


Manifest = dict[str, ManifestEntry]
