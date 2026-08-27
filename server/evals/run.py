"""AD-14 / AD-7 — Harness reproductible des questions-témoins et écrivain unique du gate.

Ce que ce module fait, et rien de plus :

1. **lit et valide strictement** les cas YAML de `server/evals/cases/{guide,sinistre}/` au schéma
   partagé d'AD-14 (`{id, suite, profile, question, lang, historique?, profil?|faits?, scenario?,
   expected, truth, mode_attendu}`) — un champ inconnu, un `truth.source` hors des trois valeurs, un
   `id` qui ne correspond pas au nom du fichier : refus **avant** tout appel facturé ;
2. **exécute** chaque cas par le pipeline réel (`pipelines.guide.repondre_guide` ou
   `pipelines.sinistre.run`), avec le corpus, l'index et le client construits ici — exactement comme
   `api/etat.py` les construit pour les routes ;
3. **juge** le résultat : un `label` du vocabulaire fixe d'AD-14, et un booléen `ok` accompagné de la
   liste des `ecarts` (D2 : les sept labels décrivent des défauts de *forme* ; aucun ne dit « la
   réponse est bien formée mais l'attente n'est pas satisfaite », et inventer un huitième label ferait
   d'un vocabulaire fixe un vocabulaire extensible) ;
4. **cache** les réponses brutes dans une namespace exhaustive, puis rend un JSON détaillé et une
   table Markdown, y compris lorsque le plafond conserve seulement un préfixe de cas terminés ;
5. **écrit `manifest.gate`** quand `--gate {doc_id}` est demandé — l'unique endroit du projet qui le
   fasse (AD-7 : « `status`/`gate` par `evals run --gate {doc_id}` » ; l'ingestion ne l'écrit jamais).

Codes de sortie, et pourquoi ils sont quatre (D4) :

| code | situation | manifest |
|---|---|---|
| 0 | tous les cas `ok` | gate écrit `evals_ok: true` (avec `--gate`) |
| 1 | un cas rend un mauvais label ou laisse une attente inassouvie | gate écrit `evals_ok: false` (avec `--gate`) |
| 2 | refus de tourner : pas de clé, profil ou suite non livrés, cas invalide, document non servi | **non modifié** |
| 3 | incident technique : `Timeout`, `LlmUnavailable`, `BudgetExceeded`, plafond de run atteint | **non modifié** |

La ligne de partage entre 1 et 3 est celle d'AD-8 : un mauvais label est un **résultat** — le gate le
consigne et le document part en `gate_echoue` au prochain démarrage, ce que le gate est fait pour
faire. Un incident réseau ne dit rien du système mesuré, et le laisser retirer un document du service
serait une panne inventée.

Ce qui n'est **pas** ici et qui est refusé plutôt que simulé : la suite `parsing` (story 4.2), les
campagnes élargies, les baselines et le holdout, `docs/evals/latest.md` et
`GET /api/v1/evals/latest` (stories 4.2 à 4.5).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
import traceback
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from server.app.config import REPO_ROOT, Settings
from server.app.config import cle_absente as config_cle_absente
from server.app.corpus.dictionary import Dictionnaire, load_dictionary
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize_version
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.answer import Answer
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE
from server.app.domain.errors import ErrorCode, PipelineError
from server.app.domain.ingest import Gate, GateContext, ManifestEntry
from server.app.domain.profil import Profil
from server.app.domain.question import Faits, Turn
from server.app.domain.trace import Trace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines import sinistre as pipeline_sinistre
from server.app.pipelines.guide import VARIANTS as VARIANTES_GUIDE
from server.app.pipelines.guide import repondre_guide
from server.evals.cache import PersistentResponseCache, empreinte_canonique, json_canonique

CASES_DIR = Path(__file__).resolve().parent / "cases"
DATA_DIR = REPO_ROOT / "data"
MANIFEST = "manifest.json"

# AD-14, mot pour mot : « labels fixes ». Un vocabulaire qu'une story élargit n'est plus fixe — ce
# qu'ils ne couvrent pas s'appelle `ecarts` et ne porte pas de nom de label (D2).
Label = Literal["bonne_reponse", "mauvais_doc", "doc_manque", "claim_non_soutenu", "faux_refus",
                "citation_introuvable", "parsing"]
LABELS: tuple[str, ...] = ("bonne_reponse", "mauvais_doc", "doc_manque", "claim_non_soutenu",
                           "faux_refus", "citation_introuvable", "parsing")

Suite = Literal["guide", "sinistre", "parsing"]
SUITES_LIVREES: tuple[str, ...] = ("guide", "sinistre")
# Ce que la story ne livre pas est **refusé**, jamais simulé, et le message dit où c'est traité.
SUITES_DIFFEREES: dict[str, str] = {"parsing": "4.2"}

GateProfile = Literal["vertical", "full"]
PROFILS_LIVRES: tuple[str, ...] = ("vertical", "full")

VARIANTES_PAR_SUITE: dict[str, tuple[str, ...]] = {
    "guide": tuple(sorted(VARIANTES_GUIDE)),
    "sinistre": tuple(sorted(pipeline_sinistre.VARIANTES)),
}
VARIANTES_LIVREES: tuple[str, ...] = tuple(sorted({
    variante for variantes in VARIANTES_PAR_SUITE.values() for variante in variantes
}))

TruthSource = Literal["lecture_humaine", "codex", "claude"]
CACHE_NAMESPACE_SCHEMA = 1

# AD-16 : les échecs terminaux des étapes. Ce sont des **incidents**, pas des verdicts (D4).
CODES_INCIDENT: frozenset[ErrorCode] = frozenset({
    ErrorCode.timeout, ErrorCode.llm_unavailable, ErrorCode.llm_parse, ErrorCode.budget_exceeded,
    ErrorCode.corpus_unavailable, ErrorCode.internal,
})


class RefusDeTourner(Exception):
    """Le runner refuse de démarrer, ou de continuer, sur une faute d'appel — code 2, manifest intact."""


class IncidentTechnique(Exception):
    """Un incident pendant un cas — code 3, manifest intact : un incident n'est pas un verdict (D4)."""

    def __init__(self, message: str, *, resultats: list[Resultat] | None = None,
                 non_executes: list[str] | None = None, arret_budget: bool = False,
                 cost_eur_engaged: float | None = None) -> None:
        super().__init__(message)
        self.resultats = list(resultats or [])
        self.non_executes = list(non_executes or [])
        self.arret_budget = arret_budget
        self.cost_eur_engaged = cost_eur_engaged


def _valider_doc_id(doc_id: str) -> str:
    """Valide l'identifiant avant qu'il puisse influencer un chemin de cas ou de données."""
    if len(doc_id) > DOC_ID_MAX or DOC_ID_RE.fullmatch(doc_id) is None:
        raise RefusDeTourner(
            f"doc_id invalide : {doc_id!r} (attendu : {DOC_ID_RE.pattern}, "
            f"{DOC_ID_MAX} caractères maximum)")
    return doc_id


def _dans_cases(cases_dir: Path, cible: Path, *, objet: str) -> Path:
    """Résout une cible et refuse tout lien/traversée qui sort de la racine des cas."""
    try:
        racine = cases_dir.resolve(strict=True)
        resolue = cible.resolve(strict=True)
        resolue.relative_to(racine)
    except (OSError, ValueError) as exc:
        raise RefusDeTourner(
            f"{objet} hors de la racine des cas {cases_dir}: {cible}") from exc
    return resolue


class _ErreurInterneFacturee(Exception):
    """Exception inattendue après démarrage d'un cas, avec le coût réellement engagé."""

    def __init__(self, type_erreur: str, cout_eur: float) -> None:
        super().__init__(type_erreur)
        self.type_erreur = type_erreur
        self.cout_eur = cout_eur


# --- le schéma des cas (AD-14, définition partagée d'`epics.md`) ---------------------------------
#
# Les modèles vivent **ici** et non dans `domain/` : ce sont les entrées d'un outil de mesure, pas des
# contrats du système mesuré. `server/app/` ne doit rien savoir des cas — sans quoi le gate
# dépendrait, par un import, de ce qu'il valide.

class Attendu(BaseModel):
    """`expected` : ce que le cas exige de la réponse. Tout y est optionnel sauf `found`."""

    model_config = ConfigDict(extra="forbid")

    found: bool
    complete: bool | None = None
    block_ids: list[str] = Field(default_factory=list)
    fiche_ids: list[str] = Field(default_factory=list)
    # Les valeurs de verdict **admissibles** (AD-6 en a quatre) : une liste, parce qu'un cas relu à la
    # main borne souvent un intervalle conservateur (« sous_conditions ou ne_tranche_pas ») plutôt
    # qu'il ne désigne une valeur unique. Vide = le cas ne dit rien du verdict.
    verdict: list[Literal["couvert", "non_couvert", "sous_conditions", "ne_tranche_pas"]] = \
        Field(default_factory=list)
    # « la réponse attendue est un refus **justifié** » : `found=False` **et** une `AbsenceProof`.
    # Le domaine lie déjà les deux (`Answer._found_coherence`) ; le dire séparément garde le cas
    # lisible par un humain et rend l'attente explicite si le domaine changeait.
    refusal: bool | None = None
    # Suite `parsing` seulement (AD-14 : « compare le texte de blocs clés à une lecture visuelle ») —
    # la suite est refusée par ce runner (story 4.2), le champ n'est ici que pour que le schéma reste
    # celui de la définition partagée, et non une variante locale.
    text_norm: str | None = None

    @model_validator(mode="after")
    def _identifiants_uniques(self) -> Attendu:
        for champ, valeurs in (("block_ids", self.block_ids), ("fiche_ids", self.fiche_ids)):
            if len(valeurs) != len(set(valeurs)):
                raise ValueError(f"expected.{champ} ne peut pas contenir deux fois le même identifiant")
        return self


class Verite(BaseModel):
    """`truth` : qui a établi l'attente, et ce qui reste dû.

    AD-14 fixe `validated_by_expert: false` pour tout ce que produit ce projet : les verdicts ne sont
    pas validés par un expert assurance, et le publier autrement serait la promesse que le brief
    interdit. Le champ est donc contraint à `False` — un cas qui l'affirmerait vrai serait refusé.
    """

    model_config = ConfigDict(extra="forbid")

    source: TruthSource
    validated_by_expert: Literal[False] = False
    # **Qui a contresigné la relecture de ce cas, et quand** — `null` tant qu'elle est due
    # (amendement AD-14, revue Codex 1.10 tour 2, B2).
    #
    # `source: lecture_humaine` dit *comment* l'attente a été établie : en lisant le corpus, et non
    # en demandant à un modèle. Il ne dit pas *par qui* — et la boucle autonome qui a préparé ces
    # deux cas n'est pas la personne à qui `epics.md` attribue la relecture (« relire à la main les
    # 2 cas vertical », entrée humaine bloquante). Tant que ce champ est `null`, la relecture est
    # celle de la boucle : le gate le publie (`Gate.countersigned`) et l'accueil écrit « relus par
    # la boucle, contresignature humaine en attente » au lieu de « relus à la main ».
    #
    # Le champ est libre — le nom et la date, comme `validated_by`/`validated_at` du dictionnaire
    # (story 2.1) — et non un booléen : « contresigné » sans dire par qui ne se vérifie pas.
    countersigned_by: str | None = None
    note: str = ""

    @model_validator(mode="after")
    def _contresignature_nommee(self) -> Verite:
        # `countersigned_by: ""` (ou des espaces) serait une contresignature par personne : elle
        # ferait pourtant basculer le gate à `countersigned: true` et la page à « relus à la main ».
        if self.countersigned_by is not None and not self.countersigned_by.strip():
            raise ValueError("truth.countersigned_by doit nommer qui a contresigné (et quand), "
                             "ou valoir `null` tant que la contresignature est due")
        return self


class Cas(BaseModel):
    """Un cas du golden set, au schéma partagé d'AD-14."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    suite: Suite
    profile: GateProfile
    question: str = Field(min_length=1)
    lang: str | None = None
    historique: list[Turn] = Field(default_factory=list)
    profil: Profil | None = None
    faits: Faits | None = None
    scenario: str = ""
    expected: Attendu
    truth: Verite
    mode_attendu: Label
    _doc_id: str | None = PrivateAttr(default=None)
    _case_path: Path | None = PrivateAttr(default=None)

    @property
    def doc_id(self) -> str | None:
        """Document propriétaire, dérivé du dossier et jamais recopié dans le YAML."""
        return self._doc_id

    @property
    def case_path(self) -> Path | None:
        return self._case_path

    @property
    def text_norm_declare(self) -> bool:
        return self.expected.text_norm is not None

    @model_validator(mode="after")
    def _coherence(self) -> Cas:
        if self.suite == "guide" and self.faits is not None:
            raise ValueError("un cas de la suite guide ne porte pas de `faits` (le guide n'a pas de sinistre)")
        if self.suite == "sinistre":
            if self.faits is None:
                raise ValueError("un cas de la suite sinistre exige des `faits`")
            if self.profil is not None or self.historique:
                raise ValueError("le pipeline sinistre ne reçoit ni `profil` ni `historique` : "
                                 "les déclarer ferait croire qu'ils sont pris en compte")
        if self.text_norm_declare and self.suite != "parsing":
            # Toutes les autres attentes produisent un écart quand elles ne sont pas tenues ; celle-ci
            # n'est lue par `juger()` que dans la suite `parsing`, qui n'est pas livrée (story 4.2).
            # L'accepter ailleurs laissait un cas passer au vert sur une attente que personne ne
            # vérifie — un golden set qui dit mesurer ce qu'il ne mesure pas.
            raise ValueError("expected.text_norm n'a de sens que dans la suite `parsing` "
                             "(story 4.2) : la retirer, ou changer de suite")
        if self.profile == "vertical" and self.truth.source != "lecture_humaine":
            # AD-14 : « `vertical` = un cas guide et un cas sinistre **relus à la main** ». C'est la
            # propriété que le profil affirme à qui lit `/sante` ; elle se contrôle ici, à l'écriture.
            raise ValueError("un cas `profile: vertical` exige `truth.source: lecture_humaine` "
                             "(c'est ce que le profil annonce)")
        return self


def charger_cas(cases_dir: Path, *, suites: tuple[str, ...] | None = None) -> list[Cas]:
    """Tous les cas des suites demandées, validés strictement — sinon `RefusDeTourner`.

    Le nom du fichier fait foi : `id` doit être le radical du fichier. C'est ce qui rend un cas
    dupliqué impossible à cacher (deux fichiers ne peuvent pas porter le même radical dans le même
    dossier), et ce qui fait que `cases_hash` — un hash de chemins et de contenus — désigne sans
    ambiguïté les cas dont un gate se réclame.
    """
    balayage_complet = suites is None
    if suites is None:
        # **Tous** les dossiers présents, pas seulement les suites livrées : un `cases/parsing/`
        # déposé dans l'arborescence doit être **vu** puis refusé nommément (story 4.2), et non
        # ignoré en silence — un golden set dont une partie ne tourne pas sans qu'on le dise est
        # exactement ce qu'AD-14 veut empêcher.
        suites = tuple(sorted(p.name for p in cases_dir.iterdir() if p.is_dir())) \
            if cases_dir.is_dir() else ()
    cas: list[Cas] = []
    for suite_locator in sorted(suites):
        morceaux = Path(suite_locator).parts
        suite = morceaux[0] if morceaux else ""
        if suite not in {"guide", "sinistre", "parsing"} or len(morceaux) > 2:
            raise RefusDeTourner(f"suite documentaire invalide : {suite_locator!r}")
        doc_id = morceaux[1] if len(morceaux) == 2 else None
        if doc_id is not None and suite != "sinistre":
            raise RefusDeTourner(
                f"seule la suite sinistre accepte un sous-dossier documentaire ({suite_locator})")
        if doc_id is not None:
            _valider_doc_id(doc_id)
        dossier = cases_dir.joinpath(*morceaux)
        if not dossier.is_dir():
            continue
        _dans_cases(cases_dir, dossier, objet="suite documentaire")
        # Tout ce que `glob("*.yaml")` (non récursif) ne ramènerait pas : un cas déposé en `.yml`,
        # et un cas rangé dans un sous-dossier. Les deux seraient **ignorés en silence**, et le gate
        # se réclamerait d'une suite amputée sans qu'un mot le dise. Un golden set muet est pire
        # qu'un golden set rouge (AD-14).
        etrangers = sorted(
            f.name + ("/" if f.is_dir() else "")
            for f in dossier.iterdir()
            if not f.name.startswith(".")
            and (
                f.suffix != ".yaml"
                and not (suite == "sinistre" and doc_id is None and f.is_dir())
            )
        )
        if etrangers:
            raise RefusDeTourner(f"{dossier} : entrées hors schéma de nommage : "
                                 f"{', '.join(etrangers)} (les cas sont des `*.yaml` à plat ; "
                                 "seuls les sous-dossiers documentaires de `sinistre/` sont admis)")
        for fichier in sorted(dossier.glob("*.yaml")):
            _dans_cases(cases_dir, fichier, objet="cas YAML")
            lu = _lire_cas(fichier, suite)
            lu._doc_id = doc_id
            lu._case_path = fichier
            cas.append(lu)
        if balayage_complet and suite == "sinistre" and doc_id is None:
            for sous_dossier in sorted(p for p in dossier.iterdir() if p.is_dir() and not p.name.startswith(".")):
                cas.extend(charger_cas(cases_dir, suites=(f"sinistre/{sous_dossier.name}",)))
    vus: set[str] = set()
    for c in cas:
        if c.id in vus:
            raise RefusDeTourner(f"deux cas portent l'identifiant {c.id!r}")
        vus.add(c.id)
    return cas


def _lire_cas(fichier: Path, suite_du_dossier: str) -> Cas:
    try:
        brut = yaml.safe_load(fichier.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RefusDeTourner(f"{fichier} : YAML illisible ({type(exc).__name__})") from exc
    if not isinstance(brut, dict):
        raise RefusDeTourner(f"{fichier} : un objet YAML est attendu (clés du schéma d'AD-14)")
    try:
        cas = Cas.model_validate(brut)
    except ValidationError as exc:
        premier = exc.errors()[0]
        champ = ".".join(str(p) for p in premier.get("loc", ())) or "(racine)"
        raise RefusDeTourner(f"{fichier} : champ {champ} — {premier.get('msg', '')}") from exc
    if cas.suite in SUITES_DIFFEREES:
        # Avant le contrôle de dossier : le message dû est celui de la suite, où qu'elle soit posée.
        raise RefusDeTourner(f"suite `{cas.suite}` : story {SUITES_DIFFEREES[cas.suite]} ({fichier})")
    if cas.id != fichier.stem:
        raise RefusDeTourner(f"{fichier} : champ id — {cas.id!r} devrait être {fichier.stem!r} "
                             "(le nom du fichier fait foi : c'est lui qu'agrège `cases_hash`)")
    if cas.suite != suite_du_dossier:
        raise RefusDeTourner(f"{fichier} : champ suite — {cas.suite!r} dans le dossier "
                             f"{suite_du_dossier!r}")
    return cas


def refuser_ce_qui_nest_pas_livre(cas: list[Cas], profil: str) -> None:
    """Les profils inconnus et suites différées sont refusés, jamais approximés.

    Le profil ``full`` inclut explicitement les cas ``vertical`` et ``full`` ; le profil
    ``vertical`` ne garde que les premiers. Le filtre est appliqué dans ``main`` après que tous les
    cas ont été strictement chargés, donc aucun fichier inconnu ou suite différée ne disparaît.
    """
    if profil not in PROFILS_LIVRES:
        raise RefusDeTourner(f"profil `{profil}` inconnu (livré : {', '.join(PROFILS_LIVRES)})")
    for c in cas:
        if c.suite in SUITES_DIFFEREES:
            raise RefusDeTourner(f"suite `{c.suite}` : story {SUITES_DIFFEREES[c.suite]} (cas {c.id})")


def selection_profil(cas: list[Cas], profil: str) -> list[Cas]:
    """Projection explicite du profil : ``full`` contient vertical **et** full."""
    return [c for c in cas if c.profile == "vertical" or profil == "full"]


def selection_quick(cas: list[Cas]) -> list[Cas]:
    """Sous-ensemble CI stable : le premier id exact de chaque suite documentaire."""
    premiers: dict[str, Cas] = {}
    for c in sorted(cas, key=lambda item: item.id):
        cle_suite = f"{c.suite}:{c.doc_id or ''}"
        premiers.setdefault(cle_suite, c)
    return sorted(premiers.values(), key=lambda item: item.id)


# --- le jugement d'un cas (AD-14, D2) ------------------------------------------------------------

@dataclass
class Resultat:
    id: str
    suite: str
    label: str
    variant: str = "deterministe"
    ecarts: list[str] = field(default_factory=list)
    cost_eur: float = 0.0
    cost_eur_original: float = 0.0
    ms: int = 0
    found: bool = False
    verdict: str | None = None
    claims: list[dict[str, Any]] = field(default_factory=list)
    expected_found: bool = False
    expected_block_ids: list[str] = field(default_factory=list)
    expected_fiche_ids: list[str] = field(default_factory=list)
    cited_fiche_ids: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Le gate exige `ok`, pas seulement le label (D2) : une attente inassouvie est un échec."""
        return not self.ecarts


@dataclass(frozen=True)
class CasesSnapshot:
    cases_hash: str
    files: dict[Path, str] = field(default_factory=dict)


def snapshot_cas(cas: list[Cas], cases_dir: Path) -> CasesSnapshot:
    """Fige les octets réellement validés avant le premier appel fournisseur."""
    candidats: list[tuple[Cas, Path | None]] = []
    for c in cas:
        path = c.case_path
        if path is None:
            if c.doc_id is not None:
                raise RefusDeTourner(
                    f"cas documentaire {c.id!r} sans case_path : impossible de certifier son hash")
            path = cases_dir / c.suite / f"{c.id}.yaml"
            if not path.is_file():
                # Un lot peut mêler cas chargés du disque et cas synthétiques. Ne jamais faire
                # disparaître les octets disque de l'identité du lot au premier cas en mémoire.
                path = None
        candidats.append((c, path))
    root = cases_dir.resolve(strict=True) if any(path is not None for _, path in candidats) else None
    h = hashlib.sha256()
    fichiers: dict[Path, str] = {}
    for c, candidat in sorted(
            candidats, key=lambda item: item[1].as_posix() if item[1] is not None
            else f"@memory/{item[0].suite}/{item[0].id}"):
        if candidat is None:
            relatif = f"@memory/{c.suite}/{c.id}.json"
            contenu = json_canonique(c.model_dump(mode="json")).encode("utf-8")
            h.update(relatif.encode("utf-8") + b"\0")
            h.update(contenu + b"\0")
            continue
        path = _dans_cases(cases_dir, candidat, objet=f"cas {c.id!r}")
        contenu = path.read_bytes()
        assert root is not None
        relatif = path.relative_to(root).as_posix()
        h.update(relatif.encode("utf-8") + b"\0")
        h.update(contenu + b"\0")
        fichiers[path] = hashlib.sha256(contenu).hexdigest()
    return CasesSnapshot(h.hexdigest(), fichiers)


def verifier_snapshot_cas(snapshot: CasesSnapshot) -> None:
    modifies = []
    for path, attendu in snapshot.files.items():
        try:
            courant = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            courant = "absent"
        if courant != attendu:
            modifies.append(path.name)
    if modifies:
        raise IncidentTechnique(
            "cas modifiés pendant le run : " + ", ".join(sorted(modifies))
            + " — rapports et gate non certifiés")


def _bloc(index: Index, block_id: str) -> Any | None:
    """Le bloc du corpus servi, ou `None` s'il n'y est pas (jamais une exception)."""
    try:
        doc_id = index.doc_of(block_id)
        return index.corpus.documents[doc_id].block(block_id)
    except KeyError:
        return None


def juger(cas: Cas, answer: Answer, *, doc_id: str, index: Index) -> tuple[str, list[str]]:
    """(label du vocabulaire fixe d'AD-14, écarts restants).

    **Précédence des labels**, dans cet ordre (D2) : `citation_introuvable` → `claim_non_soutenu` →
    `faux_refus` → `mauvais_doc` → `doc_manque` → `bonne_reponse`. Elle va du défaut le plus
    structurel — une citation que le corpus ne confirme pas rend tout le reste indécidable — au plus
    contextuel. Les `ecarts`, eux, sont **tous** relevés quel que soit le label : le label dit de
    quoi la réponse souffre, les écarts disent ce qu'elle n'a pas tenu.
    """
    ecarts: list[str] = []
    quotes = [(c, q) for c in answer.claims for q in c.quotes]
    ids_cites = [q.block_id for _, q in quotes]
    fiches_citees: set[str] = set()
    for block_id in ids_cites:
        try:
            fiches_citees.add(index.parent_node(block_id))
        except KeyError:
            continue

    # --- écarts : toutes les attentes du cas, indépendamment du label -----------------------------
    if answer.found is not cas.expected.found:
        ecarts.append(f"found={answer.found} (attendu {cas.expected.found})")
    if cas.expected.complete is not None and answer.complete is not cas.expected.complete:
        ecarts.append(f"complete={answer.complete} (attendu {cas.expected.complete})")
    if cas.expected.refusal is not None:
        refus = (answer.found is False and answer.reason is not None)
        if refus is not cas.expected.refusal:
            ecarts.append(f"refus justifié={refus} (attendu {cas.expected.refusal})")
    blocs_manquants = sorted(set(cas.expected.block_ids) - set(ids_cites))
    if blocs_manquants:
        ecarts.append(f"blocs attendus non cités : {', '.join(blocs_manquants)}")
    fiches_manquantes = sorted(set(cas.expected.fiche_ids) - fiches_citees)
    if fiches_manquantes:
        ecarts.append(f"fiches attendues non citées : {', '.join(fiches_manquantes)}")
    valeur_verdict = answer.verdict.value if answer.verdict is not None else None
    if cas.expected.verdict and valeur_verdict not in cas.expected.verdict:
        ecarts.append(f"verdict {valeur_verdict} hors des valeurs admissibles "
                      f"({', '.join(cas.expected.verdict)})")

    # --- label : la précédence de D2 --------------------------------------------------------------
    absents = [b for b in cas.expected.block_ids if _bloc(index, b) is None]
    non_confirmees = []
    for _, q in quotes:
        bloc = _bloc(index, q.block_id)
        if bloc is None or q.quote != bloc.text[q.text_start:q.text_end]:
            non_confirmees.append(q.block_id)
    survivantes = {c.claim_id for c in answer.claims}
    orphelins = [s for s in answer.segments
                 if s.kind == "factuel" and not (set(s.claim_ids) & survivantes)]
    autres_docs = sorted({index.doc_of(b) for b in ids_cites if _bloc(index, b) is not None}
                         - {doc_id})
    attendus = set(cas.expected.block_ids) | set(cas.expected.fiche_ids)
    touche = bool(set(ids_cites) & set(cas.expected.block_ids)) or \
        bool(fiches_citees & set(cas.expected.fiche_ids))

    label = "bonne_reponse"
    if absents:
        # FR34 le nomme littéralement : « `block_id` attendu disparu ⇒ `citation_introuvable` ».
        ecarts.append(f"blocs attendus absents du corpus : {', '.join(absents)}")
        label = "citation_introuvable"
    elif non_confirmees:
        ecarts.append(f"citations non confirmées par le corpus : {', '.join(sorted(set(non_confirmees)))}")
        label = "citation_introuvable"
    elif orphelins:
        ecarts.append(f"{len(orphelins)} segment(s) factuel(s) affiché(s) sans affirmation survivante")
        label = "claim_non_soutenu"
    elif cas.expected.found and not answer.found:
        label = "faux_refus"
    elif valeur_verdict == "ne_tranche_pas" and cas.expected.verdict \
            and "ne_tranche_pas" not in cas.expected.verdict:
        label = "faux_refus"
    elif autres_docs:
        ecarts.append(f"blocs cités venus d'un autre document : {', '.join(autres_docs)}")
        label = "mauvais_doc"
    elif attendus and not touche:
        label = "doc_manque"

    if label != cas.mode_attendu:
        ecarts.append(f"label {label} (mode_attendu {cas.mode_attendu})")
    return label, ecarts


# --- exécution ------------------------------------------------------------------------------------

@dataclass
class Contexte:
    """Ce que le runner construit une fois — exactement ce qu'`api/etat.py` construit pour les routes."""

    settings: Settings
    index: Index
    client: LlmClient
    pipeline_digest_hex: str
    prompts_digest_hex: str
    # Story 2.1 : le dictionnaire enrichi, chargé exactement comme `api/etat.py` le charge. Sans lui,
    # le gate mesurerait un pipeline **sans** variantes et sans le court-circuit d'AD-5, alors que la
    # production les a : le gate est censé juger l'image, pas une variante de l'image.
    dictionnaire: Dictionnaire = field(default_factory=Dictionnaire)
    dictionnaires: dict[str, Dictionnaire] = field(default_factory=dict)
    response_cache: PersistentResponseCache | None = None


def _empreinte_dictionnaire(dictionnaire: Dictionnaire | None) -> str:
    return empreinte_canonique(asdict(dictionnaire or Dictionnaire()))


def suite_du_document(settings: Settings, doc_id: str, *, cases_dir: Path = CASES_DIR) -> str:
    """La suite dont le pipeline sert ce document (D5).

    Le schéma de cas d'AD-14 ne porte **pas** de `doc_id`, et lui en ajouter un amenderait une
    définition partagée pour un besoin que cette story n'a pas : `--gate lux-guide` exécute donc la
    suite `guide`, le contrat historique la suite `sinistre` à plat, et chaque autre contrat sa
    sous-suite livrée `sinistre/<doc_id>`. L'existence du dossier fait foi ; aucun slug assureur
    n'est codé dans le runner.
    """
    _valider_doc_id(doc_id)
    if doc_id == settings.guide_doc_id:
        return "guide"
    if doc_id == settings.sinistre_doc_id:
        return "sinistre"
    suite_documentaire = cases_dir / "sinistre" / doc_id
    if suite_documentaire.is_dir():
        _dans_cases(cases_dir, suite_documentaire, objet="suite documentaire")
        return f"sinistre/{doc_id}"
    raise RefusDeTourner(
        f"aucune suite ne sert le document {doc_id!r} (suite attendue : "
        f"{suite_documentaire.relative_to(cases_dir)}/)")


def document_de_la_suite(settings: Settings, suite: str) -> str:
    morceaux = Path(suite).parts
    if len(morceaux) == 2 and morceaux[0] == "sinistre":
        return morceaux[1]
    return settings.guide_doc_id if suite == "guide" else settings.sinistre_doc_id


def variante_du_cas(cas: Cas, variante_demandee: str | None) -> str:
    """Résout la variante avant tout appel, en refusant les couples suite/variante impossibles."""
    suite = cas.suite.split("/", 1)[0]
    connues = VARIANTES_PAR_SUITE.get(suite, ())
    variante = variante_demandee or ("outils" if suite == "guide" else "deterministe")
    if variante not in connues:
        raise RefusDeTourner(
            f"variante `{variante}` incompatible avec la suite `{suite}` "
            f"(variantes compatibles : {', '.join(connues) or 'aucune'})")
    return variante


def namespace_cache(cas: Cas, ctx: Contexte, *, doc_id: str, variant: str) -> dict[str, Any]:
    """Toutes les composantes normatives qui entourent la clé exacte du corps fournisseur."""
    entry = ctx.index.corpus.manifest.get(doc_id)
    if entry is None:
        raise RefusDeTourner(f"cas {cas.id} : document {doc_id!r} absent du manifest")
    entree = {
        "id": cas.id,
        "suite": cas.suite,
        "question": cas.question,
        "lang": cas.lang,
        "historique": [turn.model_dump(mode="json") for turn in cas.historique],
        "profil": cas.profil.model_dump(mode="json") if cas.profil is not None else None,
        "faits": cas.faits.model_dump(mode="json") if cas.faits is not None else None,
    }
    dictionnaire = (ctx.dictionnaire if cas.suite == "guide"
                     else ctx.dictionnaires.get(doc_id))
    return {
        "schema": CACHE_NAMESPACE_SCHEMA,
        "models": dict(TIERS),
        # Les paramètres du corps et les schémas structurés restent dans la clé dérivée par le
        # client. Les seuils actifs couvrent en plus tout réglage de pipeline qui peut changer ce
        # corps ou la réponse sans changer l'entrée humaine.
        "parameters": {"thresholds": ctx.settings.thresholds(), "usd_eur": ctx.settings.usd_eur},
        "variant": variant,
        "pipeline_digest": ctx.pipeline_digest_hex,
        "prompts_digest": ctx.prompts_digest_hex,
        "source_hash": entry.source_hash,
        "ingest_fingerprint": entry.ingest_fingerprint,
        "overlay_hash": entry.overlay_hash,
        "dictionary_fingerprint": _empreinte_dictionnaire(dictionnaire),
        "normalize_version": normalize_version,
        "input": entree,
        "input_digest": empreinte_canonique(entree),
    }


def identite_run(cas: list[Cas], ctx: Contexte, *, profile: str, quick: bool,
                 variant: str | None) -> dict[str, Any]:
    """Identité publiable de l'image mesurée et du périmètre exact du run.

    Les digests de namespace par cas englobent aussi document, dictionnaire, seuils et entrée. Ils
    permettent de comparer deux rapports sans recopier dans ceux-ci les questions ou faits métier.
    """
    namespaces: dict[str, str] = {}
    documents: dict[str, dict[str, Any]] = {}
    variantes: dict[str, str] = {}
    for c in cas:
        doc_id = c.doc_id or document_de_la_suite(ctx.settings, c.suite)
        variante = variante_du_cas(c, variant)
        ns = namespace_cache(c, ctx, doc_id=doc_id, variant=variante)
        namespaces[c.id] = empreinte_canonique(ns)
        variantes[c.id] = variante
        documents[doc_id] = {
            champ: ns[champ] for champ in (
                "source_hash", "ingest_fingerprint", "overlay_hash",
                "dictionary_fingerprint",
            )
        }
    return {
        "image": {
            "pipeline_digest": ctx.pipeline_digest_hex,
            "prompts_digest": ctx.prompts_digest_hex,
            "model_ids": dict(TIERS),
            "normalize_version": normalize_version,
        },
        "scope": {
            "profile": profile,
            "quick": quick,
            "case_ids": [c.id for c in cas],
            "suites": sorted({c.suite for c in cas}),
            "variants": variantes,
        },
        "documents": dict(sorted(documents.items())),
        "cache_namespace_digests": namespaces,
    }


def _cout_original(trace: Trace) -> float:
    return round(sum(call.usage.cost_eur_original for step in trace.steps for call in step.calls), 4)


def _couts_logiques_non_caches(trace: Trace) -> list[float]:
    couts: list[float] = []
    for step in trace.steps:
        retries = sum(1 for check in step.checks if check.name == "parse_retry")
        if retries:
            # Le second appel peut être un hit de clé de relance après une interruption antérieure.
            # Son coût courant est nul, mais son coût original appartient toujours au coût logique
            # que l'alias du premier corps doit conserver.
            appels = list(step.calls)
            # `LlmClient.parse` autorise une relance par invocation : chaque check correspond donc
            # aux deux derniers appels contigus de cette invocation (invalide, puis valide). Les
            # appels logiques antérieurs de la même étape restent des entrées distinctes du cache.
            debut_paires = max(0, len(appels) - 2 * retries)
            couts.extend(round(call.usage.cost_eur_original, 4)
                         for call in appels[:debut_paires]
                         if not call.usage.cached_response)
            for index in range(debut_paires, len(appels), 2):
                paire = appels[index:index + 2]
                couts.append(round(sum(call.usage.cost_eur_original for call in paire), 4))
        else:
            appels = [call for call in step.calls if not call.usage.cached_response]
            couts.extend(round(call.usage.cost_eur_original, 4) for call in appels)
    return couts


def _finaliser_cache(ctx: Contexte, trace: Trace | None) -> None:
    if ctx.response_cache is not None and trace is not None:
        ctx.response_cache.finalize_logical_costs(_couts_logiques_non_caches(trace))


async def executer_cas(cas: Cas, ctx: Contexte, *, doc_id: str,
                       budget_restant_eur: float,
                       variant: str | None = None) -> tuple[Answer, Trace, float]:
    """Un cas par le pipeline réel. Rend (answer, trace, coût du cas).

    Le budget de la requête est réglé sur ce qui **reste du plafond de run** : AD-9 dit que « en
    évals, [le plafond par requête] est remplacé par un plafond par run (`--max-cost`) ». Un cas qui
    déborderait le reste du run est coupé par le budget lui-même, avant l'appel qui déborde.
    """
    budget = RequestBudget(deadline_s=ctx.settings.deadline_s,
                           max_attempts=ctx.settings.max_llm_attempts,
                           max_cost_eur=budget_restant_eur)
    commun = dict(corpus=ctx.index.corpus, index=ctx.index, client=ctx.client,
                  settings=ctx.settings, request_id=f"eval-{cas.id}", lang=cas.lang, budget=budget,
                  pipeline_digest_hex=ctx.pipeline_digest_hex,
                  prompts_digest_hex=ctx.prompts_digest_hex)
    try:
        if cas.suite == "guide":
            answer, trace = await repondre_guide(cas.question, list(cas.historique),
                                                 cas.profil or Profil(), doc_id=doc_id,
                                                 dictionnaire=ctx.dictionnaire,
                                                 variant=variant or "outils", **commun)
        else:
            assert cas.faits is not None  # garanti par `Cas._coherence`
            answer, trace = await pipeline_sinistre.run(
                doc_id, cas.question, cas.faits,
                dictionnaire=ctx.dictionnaires.get(doc_id), variant=variant or "deterministe", **commun)
    except PipelineError as exc:
        # Une erreur terminale peut survenir après un ou plusieurs appels facturés. Le pipeline
        # attache sa trace partielle à l'erreur ; le budget reste l'autorité de coût même si cette
        # trace n'a pas encore été assemblée. Le runner conserve les deux faits sur l'exception afin
        # que l'incident ne retombe jamais artificiellement à 0,0000 €.
        exc.eval_cost_eur = round(budget.cost_eur, 4)
        if exc.trace is not None:
            exc.trace.total_cost_eur = round(budget.cost_eur, 4)
            _finaliser_cache(ctx, exc.trace)
        raise
    except Exception as exc:
        # Une faute interne n'a pas la couture ``PipelineError.trace`` mais elle peut survenir après
        # un appel facturé. Ne pas la laisser sortir brute (ni remettre son coût à zéro) : le runner
        # la convertira en incident, sans inventer de verdict.
        raise _ErreurInterneFacturee(type(exc).__name__, round(budget.cost_eur, 4)) from exc
    variante_attendue = variant or ("outils" if cas.suite == "guide" else "deterministe")
    if trace.variant != variante_attendue:
        if ctx.response_cache is not None:
            ctx.response_cache.discard_namespace()
        raise _ErreurInterneFacturee("TraceVariantMismatch", round(budget.cost_eur, 4))
    _finaliser_cache(ctx, trace)
    return answer, trace, round(budget.cost_eur, 4)


async def executer(cas: list[Cas], ctx: Contexte, *, max_cost_eur: float,
                   sortie: Any = sys.stdout, variant: str | None = None) -> list[Resultat]:
    """Exécute les cas dans l'ordre, en s'arrêtant **avant** le cas qui dépasserait le plafond de run."""
    resultats: list[Resultat] = []
    cumul = 0.0
    for c in cas:
        restant = round(max_cost_eur - cumul, 4)
        if restant <= 0 and ctx.response_cache is None:
            raise IncidentTechnique(
                f"plafond de run atteint ({cumul:.4f} € sur {max_cost_eur:.4f} €) avant le cas "
                f"{c.id} : {len(cas) - len(resultats)} cas non exécutés",
                resultats=resultats, non_executes=[x.id for x in cas[len(resultats):]],
                arret_budget=True, cost_eur_engaged=cumul)
        doc_id = c.doc_id or document_de_la_suite(ctx.settings, c.suite)
        variante = variante_du_cas(c, variant)
        if ctx.response_cache is not None:
            ctx.response_cache.set_namespace(namespace_cache(c, ctx, doc_id=doc_id, variant=variante))
        depart = time.monotonic()
        try:
            answer, trace, cout = await executer_cas(
                c, ctx, doc_id=doc_id, budget_restant_eur=restant, variant=variante)
        except _ErreurInterneFacturee as exc:
            cout_total = round(cumul + exc.cout_eur, 4)
            raise IncidentTechnique(
                f"cas {c.id} : internal ({exc.type_erreur}) — coût engagé {cout_total:.4f} €") from exc
        except PipelineError as exc:
            cout_engage = exc.eval_cost_eur
            cout_total = round(cumul + cout_engage, 4)
            if exc.code is ErrorCode.budget_exceeded:
                # Le plafond de run **atteint pendant** un cas, et non avant lui : le budget de la
                # requête est réglé sur ce qui reste du run (AD-9), donc `BudgetExceeded` ici veut
                # dire « ce cas déborderait le plafond », pas « le fournisseur est en panne ». C'est
                # la même condition prévue que l'arrêt avant le cas suivant, vue une étape plus tard.
                raise IncidentTechnique(
                    f"plafond de run atteint pendant le cas {c.id} ({cout_total:.4f} € engagés sur "
                    f"{max_cost_eur:.4f} €) : {exc.message}", resultats=resultats,
                    non_executes=[x.id for x in cas[len(resultats):]], arret_budget=True,
                    cost_eur_engaged=cout_total) from exc
            if exc.code in CODES_INCIDENT:
                # D4 : un incident ne dit rien du système mesuré. Le manifest n'est pas touché.
                raise IncidentTechnique(
                    f"cas {c.id} : {exc.code.value} — {exc.message} — coût engagé "
                    f"{cout_total:.4f} €") from exc
            # `invalid_request` : le cas est hors des bornes du pipeline. C'est une faute d'écriture
            # du cas, pas une mesure ; refus, et rien n'est écrit non plus.
            raise RefusDeTourner(
                f"cas {c.id} refusé par le pipeline : {exc.message} — coût engagé "
                f"{cout_total:.4f} €") from exc
        cumul = round(cumul + cout, 4)
        label, ecarts = juger(c, answer, doc_id=doc_id, index=ctx.index)
        resultat = Resultat(id=c.id, suite=c.suite, label=label, variant=trace.variant,
                            ecarts=ecarts, cost_eur=cout, cost_eur_original=_cout_original(trace),
                            ms=int((time.monotonic() - depart) * 1000), found=answer.found,
                            verdict=answer.verdict.value if answer.verdict is not None else None,
                            claims=[claim.model_dump(mode="json") for claim in answer.claims],
                            expected_found=c.expected.found,
                            expected_block_ids=list(c.expected.block_ids),
                            expected_fiche_ids=list(c.expected.fiche_ids),
                            cited_fiche_ids=sorted({
                                ctx.index.parent_node(quote.block_id)
                                for claim in answer.claims for quote in claim.quotes
                                if _bloc(ctx.index, quote.block_id) is not None
                            }))
        resultats.append(resultat)
        _ligne(resultat, sortie)
    return resultats


def _ligne(r: Resultat, sortie: Any) -> None:
    print(f"{r.id:<28} {r.label:<21} {'ok' if r.ok else 'ECART':<6} "
          f"{r.cost_eur:>7.4f} €  {r.ms:>6} ms", file=sortie)
    for e in r.ecarts:
        print(f"{'':<28} · {e}", file=sortie)


def resume(resultats: list[Resultat], *, max_cost_eur: float, sortie: Any = sys.stdout) -> None:
    total = round(sum(r.cost_eur for r in resultats), 4)
    verts = sum(1 for r in resultats if r.ok)
    labels: dict[str, int] = {}
    for r in resultats:
        labels[r.label] = labels.get(r.label, 0) + 1
    detail = ", ".join(f"{nom}×{n}" for nom, n in sorted(labels.items()))
    print(f"— {len(resultats)} cas, {verts} ok, {len(resultats) - verts} en écart ; "
          f"coût {total:.4f} € (plafond de run {max_cost_eur:.4f} €) ; labels : {detail or '—'}",
          file=sortie)


def _recall(resultats: list[Resultat]) -> float:
    trouves = 0
    attendus = 0
    for r in resultats:
        cites = {
            quote.get("block_id")
            for claim in r.claims
            for quote in claim.get("quotes", [])
            if isinstance(quote, dict)
        }
        if r.expected_block_ids or r.expected_fiche_ids:
            attendus += len(r.expected_block_ids) + len(r.expected_fiche_ids)
            trouves += sum(1 for block_id in r.expected_block_ids if block_id in cites)
            trouves += sum(1 for fiche_id in r.expected_fiche_ids
                           if fiche_id in r.cited_fiche_ids)
        elif r.expected_found:
            attendus += 1
            trouves += int(r.found)
    return round(trouves / attendus, 4) if attendus else 0.0


def construire_rapport(resultats: list[Resultat], cas: list[Cas], *, cases_dir: Path,
                       profile: str, max_cost_eur: float, complete: bool,
                       stop_reason: str | None = None,
                       non_executes: list[str] | None = None,
                       cost_eur_engaged: float | None = None,
                       snapshot: CasesSnapshot | None = None,
                       run_identity: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = snapshot or snapshot_cas(cas, cases_dir)
    verifier_snapshot_cas(snapshot)
    labels = {label: sum(1 for r in resultats if r.label == label) for label in LABELS}
    variants = {variant: sum(1 for r in resultats if r.variant == variant)
                for variant in sorted({r.variant for r in resultats})}
    sinistres = [r for r in resultats if r.suite.startswith("sinistre")]
    cout_cas_termines = round(sum(r.cost_eur for r in resultats), 4)
    cout_total = round(cost_eur_engaged, 4) if cost_eur_engaged is not None else cout_cas_termines
    cout_original = round(sum(r.cost_eur_original for r in resultats), 4)
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "profile": profile,
        "identity": run_identity or {
            "scope": {"profile": profile, "case_ids": [c.id for c in cas]},
        },
        "complete": complete,
        "stop_reason": stop_reason,
        "unexecuted_cases": list(non_executes or []),
        "cases_hash": snapshot.cases_hash,
        "cases_planned": len(cas),
        "cases_completed": len(resultats),
        "max_cost_eur": max_cost_eur,
        "cost_eur": cout_total,
        "cost_eur_original": cout_original,
        "metrics": {
            "labels": labels,
            "variants": variants,
            "recall": _recall(resultats),
            "average_cost_eur": round(cout_cas_termines / len(resultats), 4) if resultats else 0.0,
            "latency_p50_ms": int(statistics.median(r.ms for r in resultats)) if resultats else 0,
            "ne_tranche_pas_rate": (
                round(sum(1 for r in sinistres if r.verdict == "ne_tranche_pas") / len(sinistres), 4)
                if sinistres else 0.0
            ),
        },
        "results": [
            {
                "id": r.id,
                "suite": r.suite,
                "label": r.label,
                "variant": r.variant,
                "ok": r.ok,
                "ecarts": r.ecarts,
                "cost_eur": r.cost_eur,
                "cost_eur_original": r.cost_eur_original,
                "latency_ms": r.ms,
                "found": r.found,
                "verdict": r.verdict,
                "claims": r.claims,
            }
            for r in resultats
        ],
    }


def _markdown_value(value: Any) -> str:
    """Une valeur dynamique ne peut ni ouvrir du code, ni casser une cellule ou une ligne."""
    return (html.escape(str(value), quote=False)
            .replace("|", "&#124;")
            .replace("`", "&#96;")
            .replace("\r\n", "<br>")
            .replace("\r", "<br>")
            .replace("\n", "<br>"))


def _markdown_code(value: Any) -> str:
    """Code inline sûr : les entités sont interprétées, pas affichées littéralement."""
    return f"<code>{_markdown_value(value)}</code>"


def rendre_markdown(rapport: dict[str, Any]) -> str:
    m = rapport["metrics"]
    etat = "complet" if rapport["complete"] else "partiel"
    lignes = [
        "# Résultat des questions-témoins",
        "",
        f"Run **{etat}** — profil {_markdown_code(rapport['profile'])}, "
        f"{rapport['cases_completed']}/{rapport['cases_planned']} cas terminés.",
        "",
    ]
    if rapport["stop_reason"]:
        lignes.extend([f"Arrêt : {_markdown_value(rapport['stop_reason'])}", ""])
    lignes.extend([
        "| cases_hash | recall | coût moyen (€) | latence p50 (ms) | ne_tranche_pas |",
        "|---|---:|---:|---:|---:|",
        f"| {_markdown_code(rapport['cases_hash'])} | {m['recall']:.4f} | "
        f"{m['average_cost_eur']:.4f} | "
        f"{m['latency_p50_ms']} | {m['ne_tranche_pas_rate']:.4f} |",
        "",
        "| Label | Nombre |",
        "|---|---:|",
    ])
    lignes.extend(f"| {_markdown_code(label)} | {m['labels'][label]} |" for label in LABELS)
    lignes.extend(["", "| Variante | Nombre |", "|---|---:|"])
    lignes.extend(f"| {_markdown_code(variant)} | {count} |"
                  for variant, count in m["variants"].items())
    lignes.extend(["", "| Cas | Suite | Variante | Label | Coût (€) | Coût original (€) | Latence (ms) |",
                   "|---|---|---|---|---:|---:|---:|"])
    for r in rapport["results"]:
        lignes.append(f"| {_markdown_code(r['id'])} | {_markdown_code(r['suite'])} | "
                      f"{_markdown_code(r['variant'])} | {_markdown_code(r['label'])} | "
                      f"{r['cost_eur']:.4f} | {r['cost_eur_original']:.4f} | {r['latency_ms']} |")
    if rapport["unexecuted_cases"]:
        lignes.extend(["", "Cas non exécutés : "
                       + ", ".join(_markdown_code(case_id)
                                   for case_id in rapport["unexecuted_cases"]), ""])
    return "\n".join(lignes).rstrip() + "\n"


def _ecrire_atomique(path: Path, contenu: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporaire = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(contenu)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporaire, path)
    except BaseException:
        try:
            os.unlink(temporaire)
        except FileNotFoundError:
            pass
        raise


def ecrire_rapports(rapport: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    _ecrire_atomique(json_path, json_canonique(rapport) + "\n")
    _ecrire_atomique(markdown_path, rendre_markdown(rapport))


# --- le gate (AD-7) --------------------------------------------------------------------------------

def construire_gate(entry: ManifestEntry, ctx: Contexte, *, profil: str, cas: list[Cas],
                    cases_dir: Path, evals_ok: bool,
                    snapshot: CasesSnapshot | None = None) -> Gate:
    """Le gate d'un document : les empreintes de **l'entrée du manifest**, pas celles recalculées.

    AD-7 : « le gate porte `source_hash`, `ingest_fingerprint`, `overlay_hash` de l'entrée du manifest
    au moment du run, sans quoi le loader le neutralise en `sans_gate` ». Les recalculer ici donnerait
    un gate qui se valide lui-même : le loader compare le gate à l'entrée, et deux valeurs issues du
    même calcul seraient toujours d'accord, y compris sur un artefact qui a bougé sans réingestion.

    `cases_hash` couvre **la suite réellement exécutée** (D5), et `cases` en donne le nombre : deux
    runs ne sont comparables qu'à hash égal (AD-14), et l'accueil affiche « N cas relus à la main »
    sans coder N en dur.

    `countersigned` est la **conjonction** des `truth.countersigned_by` des cas exécutés (revue Codex
    1.10 tour 2, B2) : c'est ce qui autorise, ou non, la page d'accueil à écrire « relus à la main ».
    Un seul cas sans contresignature suffit à mettre le gate à `false` — « 2 cas relus à la main »
    serait faux dès qu'un des deux ne l'est pas.
    """
    snapshot = snapshot or snapshot_cas(cas, cases_dir)
    verifier_snapshot_cas(snapshot)
    return Gate(
        profile=profil, source_hash=entry.source_hash,
        ingest_fingerprint=entry.ingest_fingerprint, overlay_hash=entry.overlay_hash,
        cases_hash=snapshot.cases_hash, cases=len(cas),
        countersigned=all(c.truth.countersigned_by is not None for c in cas),
        pipeline_digest=ctx.pipeline_digest_hex, prompts_digest=ctx.prompts_digest_hex,
        model_ids=dict(TIERS), evals_ok=evals_ok,
        date=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"))


def ecrire_gate(manifest_path: Path, doc_id: str, gate: Gate) -> None:
    """Écrit `manifest[doc_id].gate` — l'unique écriture de `data/` de tout ce module (AD-7).

    Le manifest entier est relu et **validé** avant la moindre écriture : un fichier illisible ou une
    entrée hors schéma arrête tout sans rien modifier sur disque. L'écriture est atomique (fichier
    temporaire puis `replace`) et garde la forme des autres écrivains de `data/` — clés triées,
    `indent=2`, `ensure_ascii=False`, saut de ligne final —, pour qu'un `git diff` de gate ne soit
    qu'un gate.
    """
    try:
        brut = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RefusDeTourner(f"{manifest_path} illisible : {type(exc).__name__}") from exc
    if not isinstance(brut, dict) or doc_id not in brut:
        raise RefusDeTourner(f"{manifest_path} : aucune entrée {doc_id!r}")
    # Un gate **périmé au sens du schéma** n'empêche pas d'en écrire un neuf (revue Codex 1.10
    # tour 2). Quand `Gate` gagne un champ obligatoire — `cases` au tour 1, `countersigned` au tour 2
    # —, tous les gates déjà écrits deviennent invalides et doivent être refaits, un document après
    # l'autre. Refuser d'écrire parce qu'un gate n'est pas encore refait est un blocage circulaire :
    #   - pour le document visé, son gate est **remplacé** par celui du run ; sa valeur d'avant n'a
    #     donc aucune importance, et l'entrée est validée sans lui ;
    #   - pour les autres, l'entrée est recopiée **telle quelle**, gate compris — rien n'est réécrit,
    #     et leur document reste en quarantaine tant que leur propre `--gate` n'a pas tourné.
    # Tout le reste de chaque entrée doit être valide : un manifest réellement abîmé arrête toujours
    # l'écriture, sans rien modifier sur disque.
    def _valide_hors_gate(valeur: Any) -> ManifestEntry:
        if not isinstance(valeur, dict):
            raise ValidationError.from_exception_data("ManifestEntry", [])
        return ManifestEntry.model_validate({**valeur, "gate": None})

    for autre, valeur in brut.items():
        try:
            ManifestEntry.model_validate(valeur)
        except ValidationError as exc:
            try:
                _valide_hors_gate(valeur)
            except ValidationError:
                raise RefusDeTourner(f"{manifest_path} : entrée {autre!r} invalide "
                                     f"({exc.errors()[0].get('msg', '')})") from exc
            if autre != doc_id:
                print(f"avertissement : {manifest_path} : le gate de {autre!r} n'est plus au schéma "
                      "en vigueur, il devra être relancé — entrée recopiée telle quelle",
                      file=sys.stderr)
    try:
        entree = _valide_hors_gate(brut[doc_id]).model_copy(update={"gate": gate})
    except ValidationError as exc:
        raise RefusDeTourner(f"{manifest_path} : entrée {doc_id!r} invalide "
                             f"({exc.errors()[0].get('msg', '') if exc.errors() else 'hors schéma'})"
                             ) from exc
    brut[doc_id] = entree.model_dump(mode="json")
    # Nom temporaire **unique**, dans le répertoire du manifest (revue Codex 1.10, M1) : un
    # `manifest.json.tmp` fixe était partagé par deux `--gate` concurrents, qui se marchaient dessus
    # jusqu'à faire échouer un `replace` sur un fichier déjà déplacé. Le même répertoire garde le
    # `replace` atomique (même système de fichiers). Ce qui reste ouvert — deux écrivains qui relisent
    # le même manifest et perdent l'un des deux gates — demande un verrou sur toute la séquence :
    # entrée différée (`target_story: 4.1`), avec les deux écrivains de `data/` à réunir.
    fd, nom = tempfile.mkstemp(prefix=manifest_path.name + ".", suffix=".tmp",
                               dir=str(manifest_path.parent))
    tmp = Path(nom)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as sortie:
            sortie.write(json.dumps(dict(sorted(brut.items())), indent=2, ensure_ascii=False) + "\n")
        tmp.replace(manifest_path)
    except OSError as exc:
        # Disque plein, `data/` en lecture seule, conteneur sans droit d'écriture : un refus dit,
        # pas une trace de pile. Le manifest est intact — l'écriture est atomique, et ce qui a pu
        # être écrit l'a été dans le fichier temporaire.
        tmp.unlink(missing_ok=True)
        raise RefusDeTourner(f"{manifest_path} : écriture impossible ({type(exc).__name__}) — "
                             "le manifest n'a pas été modifié") from exc


# --- l'assemblage ----------------------------------------------------------------------------------

def cle_absente(settings: Settings) -> bool:
    """AD-14 : « sans `ANTHROPIC_API_KEY`, les évals refusent de tourner ».

    La règle elle-même vit dans `config.cle_absente` depuis la story 2.1 : l'ingestion du
    dictionnaire fait la même promesse sur la même variable, et deux commandes qui promettent la même
    chose ne peuvent pas la tenir par deux codes différents. Ce nom reste le point d'entrée des évals.
    """
    return config_cle_absente(settings)


def _materialiser_dans_ombre(source: Path, cible: Path, racine: Path,
                             ancetres: frozenset[Path] = frozenset()) -> None:
    """Matérialise ``source`` sous ``cible`` sans jamais lire hors de ``racine``.

    Les fichiers sont liés physiquement quand le système de fichiers le permet : le corpus AXA
    n'est donc pas recopié. Une copie est le repli portable si le répertoire temporaire se trouve
    sur un autre volume. Les liens symboliques internes sont résolus vers un fichier ou un dossier
    réel dans l'ombre ; ceux qui sortent du vrai ``data/`` sont ignorés, comme le loader les
    mettrait en quarantaine.
    """
    try:
        resolu = source.resolve(strict=True)
    except OSError:
        return
    if not resolu.is_relative_to(racine):
        return
    if resolu.is_dir():
        if resolu in ancetres:
            return
        cible.mkdir()
        descendants = ancetres | {resolu}
        for enfant in resolu.iterdir():
            _materialiser_dans_ombre(enfant, cible / enfant.name, racine, descendants)
        return
    if not resolu.is_file():
        return
    try:
        os.link(resolu, cible)
    except OSError:
        shutil.copy2(resolu, cible)


def _sans_gate_sur_disque(data_dir: Path, doc_id: str, pile: Any) -> Path:
    """Un `data/` **de lecture** identique, sauf que `manifest[doc_id].gate` y vaut `null`.

    Pourquoi il en faut un : `loader._gate_alerts` met en quarantaine, sans dérogation possible, tout
    document dont le gate porte `evals_ok: false` (AD-8 : « jamais servi »). C'est juste au service —
    et c'est un cul-de-sac pour la mesure : après un run rouge, `--gate {doc_id}` refuserait
    éternellement en code 2 « document non servi », et il n'existerait aucun chemin pour reprendre la
    mesure et écrire un gate vert. Un verdict d'éval ne doit pas rendre l'éval impossible.

    Ce que cette fonction **ne fait pas** : modifier `data/`. Elle construit un dossier temporaire
    composé de vrais répertoires et de fichiers liés physiquement (copiés seulement si nécessaire),
    puis n'y réécrit qu'un `manifest.json`, en mémoire, avec le seul `gate` du document visé mis à
    `null`. Cette vue reste donc confinée sous sa propre racine sans recopier normalement le contrat
    AXA. Tout le reste de la règle d'AD-7 continue de s'appliquer sur cette lecture : hashes, overlay,
    bloquant statique, `status: quarantaine`. L'écriture du gate, elle, se fait toujours sur le
    **vrai** manifest.
    """
    ombre = Path(pile.enter_context(tempfile.TemporaryDirectory(prefix="evals-regate-")))
    racine = data_dir.resolve(strict=True)
    for entree in data_dir.iterdir():
        if entree.name == MANIFEST:
            continue
        _materialiser_dans_ombre(entree, ombre / entree.name, racine)
    brut = json.loads((data_dir / MANIFEST).read_text(encoding="utf-8"))
    if isinstance(brut, dict) and isinstance(brut.get(doc_id), dict):
        brut[doc_id]["gate"] = None
    (ombre / MANIFEST).write_text(json.dumps(brut, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
    return ombre


def construire_contexte(settings: Settings, data_dir: Path, *, regate: str | None = None,
                        pile: Any = None, cache_dir: Path | None = None) -> Contexte:
    """Corpus, index et client — le même assemblage qu'`api/etat.construire_etat` (AD-7, AD-9).

    `allow_ungated=True` **toujours**, et ce n'est pas une dérogation : c'est ce runner qui écrit le
    premier gate d'un document, et charger sous la règle du gate rendrait ce premier gate impossible
    à obtenir (le document ne serait pas servi, donc pas mesurable). Tout le reste de la règle d'AD-7
    s'applique : un document dont les hashes ont bougé, dont le rapport porte un bloquant statique ou
    que le manifest met en quarantaine n'est **pas** chargé, et `--gate` le refusera.

    `regate` va un cran plus loin, et seulement pour le document que `--gate` vise : il fait lire un
    manifest où **son** gate vaut `null`, ce qui permet de reprendre la mesure d'un document dont le
    gate précédent était rouge (`gate_echoue`) **ou que le schéma en vigueur ne sait plus lire**.
    Voir `_sans_gate_sur_disque` : `data/` n'est pas touché, et un document en quarantaine pour une
    autre raison le reste.

    Le second cas est le même cul-de-sac que le premier, par une autre porte (revue Codex 1.10
    tour 2) : quand `Gate` gagne un champ obligatoire — `cases` au tour 1, `countersigned` au tour 2
    —, tous les gates déjà écrits deviennent des entrées de manifest invalides, le loader met leur
    document en quarantaine, et `--gate` refuse en code 2 « document non servi ». Il n'existerait
    alors aucun chemin pour écrire le gate au nouveau schéma, alors que c'est exactement ce que la
    commande demande. Un gate que l'image courante ne sait pas lire n'est pas un gate : pour ce seul
    document, et pour la lecture seulement, il est traité comme absent.
    """
    contexte_gate = GateContext(pipeline_digest=pipeline_digest(), prompts_digest=prompts_digest(),
                                model_ids=dict(TIERS))
    lecture = data_dir
    if regate is not None and pile is not None:
        entree_brute = json.loads((data_dir / MANIFEST).read_text(encoding="utf-8")) \
            if (data_dir / MANIFEST).is_file() else {}
        brut_gate = (entree_brute[regate].get("gate")
                     if isinstance(entree_brute.get(regate), dict) else None)
        gate_a_refaire = False
        if isinstance(brut_gate, dict):
            gate_a_refaire = brut_gate.get("evals_ok") is False
            if not gate_a_refaire:
                try:
                    Gate.model_validate(brut_gate)
                except ValidationError:
                    gate_a_refaire = True
        if gate_a_refaire:
            lecture = _sans_gate_sur_disque(data_dir, regate, pile)
    corpus = load_corpus(lecture, allow_ungated=True, current=contexte_gate,
                         perimetre_max_chars=settings.perimetre_max_chars,
                         raison_max_chars=settings.raison_publiable_max_chars)
    dictionnaires = {
        doc_id: load_dictionary(data_dir, corpus, doc_id)
        for doc_id, document in corpus.documents.items()
        if document.kind == "contrat"
    }
    response_cache = PersistentResponseCache(cache_dir) if cache_dir is not None else None
    return Contexte(settings=settings, index=Index(corpus),
                    client=LlmClient(settings, cache=response_cache),
                    pipeline_digest_hex=contexte_gate.pipeline_digest,
                    prompts_digest_hex=contexte_gate.prompts_digest,
                    # Lu dans `data_dir`, pas dans `lecture` : `_sans_gate_sur_disque` ne recopie que
                    # le manifest, et le dictionnaire n'a rien à voir avec le gate qu'on refait.
                    dictionnaire=load_dictionary(data_dir, corpus, settings.guide_doc_id),
                    dictionnaires=dictionnaires, response_cache=response_cache)


async def _fermer(client: Any) -> None:
    """Ferme le pool de connexions du client, si c'en est un (les tests passent un double)."""
    fermer = getattr(client, "aclose", None)
    if fermer is not None:
        await fermer()


async def _executer_puis_fermer(cas: list[Cas], ctx: Contexte, *, gate: str | None,
                                max_cost_eur: float, sortie: Any,
                                variant: str | None = None) -> list[Resultat]:
    """Le refus, l'exécution **et** la fermeture du client, dans **une seule** boucle asyncio.

    Pourquoi une seule, et pourquoi c'est un défaut de production et pas une élégance : le pool de
    connexions du client est lié à la boucle qui l'a ouvert, et la connexion TLS d'`api.anthropic.com`
    l'est jusqu'à sa fermeture (anyio ferme un `TLSStream` en repassant par le transport). Le fermer
    depuis un **second** `asyncio.run` — ce que faisait `pile.callback(lambda: asyncio.run(...))`, la
    pile se déroulant après le premier — le fait tourner sur une boucle neuve alors que ses sockets
    appartiennent à une boucle close : `RuntimeError: Event loop is closed`, attrapé par le garde-fou
    « incident » de `main()`, code 3 et **aucun gate écrit** — après avoir payé tous les appels.
    Mesuré : un run `--gate` rendait `bonne_reponse` puis sortait en 3, manifest intact.

    Aucun test ne le voyait : les doubles n'ont pas de pool, et une boucle close ne gêne personne
    quand `aclose()` ne fait qu'ajouter à une liste. `test_le_client_se_ferme_sur_la_boucle_qui_la_
    servi` referme cette porte avec un double lié à sa boucle, comme le vrai client l'est.

    Le refus « document non servi » est ici pour la même raison : il doit fermer le client qu'il n'a
    pas utilisé, sans jamais ouvrir une seconde boucle pour cela.
    """
    try:
        if gate and gate not in ctx.index.corpus.documents:
            raison = ctx.index.corpus.quarantine.get(gate, "absent du corpus")
            raise RefusDeTourner(f"--gate {gate} : document non servi ({raison})")
        return await executer(cas, ctx, max_cost_eur=max_cost_eur, sortie=sortie, variant=variant)
    finally:
        await _fermer(ctx.client)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server.evals.run",
        description="Harness reproductible des questions-témoins (AD-14) : cache, budget et rapports.")
    p.add_argument("--suite", choices=sorted(SUITES_LIVREES) + sorted(SUITES_DIFFEREES),
                   help="n'exécuter que cette suite (défaut : toutes les suites livrées)")
    p.add_argument("--case", dest="cas", help="n'exécuter que ce cas (son identifiant)")
    p.add_argument("--profile", choices=PROFILS_LIVRES, default="vertical",
                   help="profil exécuté : vertical, ou full (inclut les cas vertical et full)")
    p.add_argument("--quick", action="store_true",
                   help="sous-ensemble CI stable : premier identifiant de chaque suite sélectionnée")
    p.add_argument("--variant", choices=VARIANTES_LIVREES,
                   help="variante de retrieval (défaut par suite : outils pour guide, déterministe sinon)")
    p.add_argument("--gate", metavar="DOC_ID",
                   help="écrire `manifest.gate` pour ce document depuis la suite qui le sert")
    p.add_argument("--max-cost", type=float, default=None,
                   help="plafond de coût du run en euros (défaut : evals_max_cost_eur de config.py)")
    p.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    p.add_argument("--data-dir", type=Path, default=DATA_DIR)
    p.add_argument("--cache-dir", type=Path,
                   help="cache persistant (défaut : <parent data>/.cache/evals)")
    p.add_argument("--output-json", "--json", dest="output_json", type=Path,
                   help="résultat JSON atomique (défaut : <parent data>/eval-results.json)")
    p.add_argument("--output-markdown", "--markdown", dest="output_markdown", type=Path,
                   help="table Markdown atomique (défaut : <parent data>/eval-results.md)")
    p.add_argument("--dry-run", action="store_true",
                   help="valider et afficher le plan sans clé, client, appel ni écriture")
    return p


def _chevauchent(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def valider_chemins(*, output_json: Path, output_markdown: Path, cases_dir: Path,
                    data_dir: Path, cache_dir: Path) -> None:
    """Refuse avant client tout alias ou chevauchement pouvant écraser entrée ou état."""
    for nom, sortie in (("sortie JSON", output_json), ("sortie Markdown", output_markdown)):
        if sortie.exists() and sortie.is_dir():
            raise RefusDeTourner(
                f"chemins en collision : {nom}={sortie.resolve()} existe comme répertoire")
    chemins = {
        "sortie JSON": output_json.resolve(),
        "sortie Markdown": output_markdown.resolve(),
        "cases_dir": cases_dir.resolve(),
        "data_dir": data_dir.resolve(),
        "cache": cache_dir.resolve(),
        "manifest": (data_dir / MANIFEST).resolve(),
    }
    items = list(chemins.items())
    for index, (nom_a, chemin_a) in enumerate(items):
        for nom_b, chemin_b in items[index + 1:]:
            # Le manifest canonique appartient nécessairement à data_dir ; c'est la seule relation
            # ancêtre-descendant structurelle, et elle ne résulte pas d'un argument utilisateur.
            if {nom_a, nom_b} == {"data_dir", "manifest"}:
                continue
            if _chevauchent(chemin_a, chemin_b):
                raise RefusDeTourner(
                    f"chemins en collision : {nom_a}={chemin_a} et {nom_b}={chemin_b}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sortie = sys.stdout
    cas: list[Cas] = []
    output_json = args.output_json or args.data_dir.parent / "eval-results.json"
    output_markdown = args.output_markdown or args.data_dir.parent / "eval-results.md"
    cache_dir = args.cache_dir or args.data_dir.parent / ".cache" / "evals"
    snapshot: CasesSnapshot | None = None
    run_identity: dict[str, Any] | None = None
    try:
        valider_chemins(output_json=output_json, output_markdown=output_markdown,
                        cases_dir=args.cases_dir, data_dir=args.data_dir, cache_dir=cache_dir)
        settings = Settings()
        max_cost = settings.evals_max_cost_eur if args.max_cost is None else args.max_cost
        if not math.isfinite(max_cost) or max_cost <= 0:
            # `argparse(type=float)` accepte « inf » et « nan », et `nan <= 0` est **faux** : sans
            # `isfinite`, `--max-cost inf` ou `--max-cost nan` neutralisait le plafond et le run
            # partait sans borne, contre AD-9 et contre CLAUDE.md (« la clé **et un plafond** »).
            raise RefusDeTourner(f"plafond de run {max_cost} € : les évals tournent avec un plafond "
                                 "fini et strictement positif (CLAUDE.md, AD-9)")
        # 1. Le profil, avant tout le reste.
        if args.profile not in PROFILS_LIVRES:
            raise RefusDeTourner(f"profil `{args.profile}` inconnu (livré : {', '.join(PROFILS_LIVRES)})")
        # 2. Le doc_id du gate avant toute composition de chemin ou lecture de cas.
        if args.gate:
            _valider_doc_id(args.gate)
        # 3. La clé, **avant tout chargement de corpus** (AD-14).
        if not args.dry_run and cle_absente(settings):
            raise RefusDeTourner("les évals exigent une clé : ANTHROPIC_API_KEY est vide ou absente "
                                 "(AD-14 — les unitaires passent sans clé, les évals non)")
        # 4. Les cas, avant tout appel facturé.
        suites = None
        if args.gate and args.cas:
            # `--gate` écrit un gate **de suite** : `cases` et `cases_hash` désignent ce qui a été
            # exécuté. Filtré par `--case`, il affirmerait `evals_ok: true` pour le document entier
            # sur un cas choisi à la main, et `tests/test_digests.py` le verrait — mais après coup.
            raise RefusDeTourner("--gate et --case sont exclusifs : un gate se réclame de la suite "
                                 "qui sert le document, jamais d'un cas choisi")
        if args.gate and args.quick:
            raise RefusDeTourner("--gate et --quick sont exclusifs : un gate exige la suite complète")
        if args.suite in SUITES_DIFFEREES:
            raise RefusDeTourner(f"suite `{args.suite}` : story {SUITES_DIFFEREES[args.suite]}")
        if args.gate:
            suites = (suite_du_document(settings, args.gate, cases_dir=args.cases_dir),)
        if args.suite:
            if suites is not None and args.suite not in suites:
                raise RefusDeTourner(f"--suite {args.suite} et --gate {args.gate} se contredisent : "
                                     f"la suite qui sert {args.gate} est {suites[0]}")
            suites = (args.suite,)
        cas = charger_cas(args.cases_dir, suites=suites)
        refuser_ce_qui_nest_pas_livre(cas, args.profile)
        cas = selection_profil(cas, args.profile)
        if args.cas:
            cas = [c for c in cas if c.id == args.cas]
            if not cas:
                raise RefusDeTourner(f"aucun cas {args.cas!r} au profil {args.profile}")
        if not cas:
            raise RefusDeTourner(f"aucun cas au profil {args.profile} "
                                 f"dans {args.cases_dir}{' (suites ' + ', '.join(suites) + ')' if suites else ''}")
        # Compatibilité fermée avant contexte/client : un couple impossible ne peut rien facturer.
        for c in cas:
            variante_du_cas(c, args.variant)
        if args.gate and args.variant is not None and any(
                args.variant != variante_du_cas(c, None) for c in cas):
            raise RefusDeTourner(
                f"--gate refuse la variante explicite `{args.variant}` : le gate doit mesurer "
                "la variante servie par défaut")
        if args.quick:
            cas = selection_quick(cas)
        snapshot = snapshot_cas(cas, args.cases_dir)
        if args.dry_run:
            # Story 3.7 : jalon explicite avant toute dépense. Aucune construction de contexte —
            # donc ni client, ni lecture/écriture du manifest — et aucun besoin de clé.
            print("dry-run : aucun appel, aucun client et aucune écriture", file=sortie)
            print(f"profil={args.profile} gate={args.gate or '-'} cas={len(cas)} "
                  f"suites={','.join(suites or sorted({c.suite for c in cas}))} "
                  f"variantes={','.join(sorted({variante_du_cas(c, args.variant) for c in cas}))} "
                  f"ids={','.join(c.id for c in cas)} plafond={max_cost:.4f} EUR", file=sortie)
            return 0
        # 5. Le corpus, puis le document visé par `--gate`.
        with ExitStack() as pile:
            ctx = construire_contexte(settings, args.data_dir, regate=args.gate, pile=pile,
                                      cache_dir=cache_dir)
            run_identity = identite_run(
                cas, ctx, profile=args.profile, quick=args.quick, variant=args.variant)
            # Le client (donc un pool de connexions TLS) est construit par `construire_contexte`.
            # Le refus « document non servi », l'exécution et la fermeture tiennent dans **un seul**
            # `asyncio.run` : une fermeture sur une autre boucle que celle qui a servi le client
            # lève `Event loop is closed` et fait sortir en code 3 un run par ailleurs vert, sans
            # écrire le gate. Voir `_executer_puis_fermer`.
            resultats = asyncio.run(_executer_puis_fermer(
                cas, ctx, gate=args.gate, max_cost_eur=max_cost, sortie=sortie,
                variant=args.variant))
    except RefusDeTourner as exc:
        print(f"refus : {exc}", file=sys.stderr)
        return 2
    except IncidentTechnique as exc:
        # AD-16 : l'échec est terminal et **dit**. Le manifest n'a pas été touché.
        if exc.arret_budget and cas:
            try:
                if snapshot is None:
                    raise IncidentTechnique("snapshot des cas absent au moment du rapport partiel")
                rapport = construire_rapport(
                    exc.resultats, cas, cases_dir=args.cases_dir, profile=args.profile,
                    max_cost_eur=max_cost, complete=False, stop_reason=str(exc),
                    non_executes=exc.non_executes, cost_eur_engaged=exc.cost_eur_engaged,
                    snapshot=snapshot, run_identity=run_identity)
                ecrire_rapports(rapport, output_json, output_markdown)
                print(f"rapports partiels écrits : {output_json} ; {output_markdown}", file=sortie)
            except Exception as rapport_exc:  # noqa: BLE001 — frontière d'incident du writer
                print(f"incident de rapport partiel : {type(rapport_exc).__name__}: {rapport_exc} "
                      "— manifest non modifié", file=sys.stderr)
                return 3
        print(f"incident : {exc} — manifest non modifié (un incident n'est pas un verdict)",
              file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 — voir ci-dessous
        # Tout le reste (`TypeError` d'une signature qui a bougé, `KeyError`, `OSError`) sortait en
        # code 1 — celui que ce runner réserve à « un cas a rendu un mauvais label ». Un appelant,
        # ou la CI, aurait lu un bug du runner comme un verdict d'éval. C'est un incident : code 3,
        # manifest intact, et la trace part sur stderr pour être diagnosticable.
        traceback.print_exc()
        print(f"incident : {type(exc).__name__}: {exc} — manifest non modifié", file=sys.stderr)
        return 3
    try:
        resume(resultats, max_cost_eur=max_cost, sortie=sortie)
        if snapshot is None:
            raise IncidentTechnique("snapshot des cas absent au moment du rapport")
        rapport = construire_rapport(resultats, cas, cases_dir=args.cases_dir, profile=args.profile,
                                     max_cost_eur=max_cost, complete=True, snapshot=snapshot,
                                     run_identity=run_identity)
        ecrire_rapports(rapport, output_json, output_markdown)
        print(f"rapports écrits : {output_json} ; {output_markdown}", file=sortie)
    except Exception as exc:  # noqa: BLE001 — construction et écriture sont des incidents techniques
        print(f"incident de rapport : {type(exc).__name__}: {exc} — manifest non modifié",
              file=sys.stderr)
        return 3
    tous_ok = all(r.ok for r in resultats)
    if args.gate:
        try:
            entry = ctx.index.corpus.manifest[args.gate]
            gate = construire_gate(entry, ctx, profil=args.profile, cas=cas,
                                   cases_dir=args.cases_dir, evals_ok=tous_ok, snapshot=snapshot)
            ecrire_gate(args.data_dir / MANIFEST, args.gate, gate)
        except RefusDeTourner as exc:
            print(f"refus : {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 — un gate cassé n'est jamais un verdict d'éval
            print(f"incident de gate : {type(exc).__name__}: {exc} — manifest non modifié",
                  file=sys.stderr)
            return 3
        print(f"gate écrit sur {args.gate} : profile={gate.profile} evals_ok={gate.evals_ok} "
              f"cases={gate.cases} countersigned={gate.countersigned} "
              f"cases_hash={gate.cases_hash[:12]}… date={gate.date}", file=sortie)
        if not gate.countersigned:
            # AD-14 définit `vertical` par une relecture humaine. Écrire un gate dont les cas ne
            # sont pas contresignés est permis — sans quoi aucun gate ne pourrait exister avant la
            # contresignature — mais jamais muet : l'accueil dira la réserve, et ceci la dit à qui
            # lance le run (revue Codex 1.10 tour 2, B2).
            manquants = ", ".join(c.id for c in cas if c.truth.countersigned_by is None)
            print(f"ATTENTION : gate `{gate.profile}` écrit sur des cas non contresignés "
                  f"({manquants}) — l'accueil n'écrira pas « relus à la main » tant que "
                  "`truth.countersigned_by` n'est pas rempli, et le gate devra être relancé",
                  file=sys.stderr)
    return 0 if tous_ok else 1


if __name__ == "__main__":  # pragma: no cover — point d'entrée
    raise SystemExit(main())
