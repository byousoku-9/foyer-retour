"""AD-14 / AD-7 — Harness reproductible des questions-témoins et écrivain unique du gate.

Ce que ce module fait, et rien de plus :

1. **lit et valide strictement** les cas YAML de `server/evals/cases/{guide,sinistre,parsing}/` au schéma
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
| 4 | refus de budget **avant le premier appel** (story 4.2b) : majorant estimé d'une campagne
payante > budget effectif (`min(--max-cost, LIVE_BUDGET_EUR)`) — rouge chiffré, jamais une
question | **non modifié** |

La ligne de partage entre 1 et 3 est celle d'AD-8 : un mauvais label est un **résultat** — le gate le
consigne et le document part en `gate_echoue` au prochain démarrage, ce que le gate est fait pour
faire. Un incident réseau ne dit rien du système mesuré, et le laisser retirer un document du service
serait une panne inventée.

Ce qui n'est **pas** ici et qui est refusé plutôt que simulé : le holdout, les baselines,
la génération de résultats live courants et `GET /api/v1/evals/latest` (stories 4.3 à 4.5).
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
from typing import Any, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

from server.app.config import REPO_ROOT, Settings
from server.app.config import cle_absente as config_cle_absente
from server.app.corpus.dictionary import Dictionnaire, load_dictionary
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize, normalize_version
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.answer import Answer
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE
from server.app.domain.errors import HTTP_STATUS, ErrorCode, PipelineError
from server.app.domain.ingest import Gate, GateContext, GateDecision, ManifestEntry
from server.app.domain.profil import Profil
from server.app.domain.question import Faits, Turn
from server.app.domain.trace import Trace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.llm.pricing import estimate_run_majorant
from server.app.pipelines import sinistre as pipeline_sinistre
from server.app.pipelines.guide import VARIANTS as VARIANTES_GUIDE
from server.app.pipelines.guide import repondre_guide
from server.evals.cache import PersistentResponseCache, empreinte_canonique, json_canonique
from server.evals.plancher import ChargePlancher, PlancherInvalide, charger_plancher

CASES_DIR = Path(__file__).resolve().parent / "cases"
REFERENCE_DIR = Path(__file__).resolve().parent / "reference"
DATA_DIR = REPO_ROOT / "data"
MANIFEST = "manifest.json"
# Story 4.2b : les artefacts de **protocole** rangés sous `reference/` — le plancher pré-enregistré
# (chargé et figé par `server/evals/plancher.py`, digest dans l'identité de run) et l'allowlist
# juridique de la garde anti-rustine (`tests/test_anti_rustine.py`). Ce ne sont pas des compagnons
# de cas : `charger_references` les ignore, leurs autorités et leurs digests sont ailleurs.
PROTOCOLE_FILES = frozenset({"plancher.yaml", "allowlist-juridique.yaml"})

# AD-14, mot pour mot : « labels fixes ». Un vocabulaire qu'une story élargit n'est plus fixe — ce
# qu'ils ne couvrent pas s'appelle `ecarts` et ne porte pas de nom de label (D2).
Label = Literal["bonne_reponse", "mauvais_doc", "doc_manque", "claim_non_soutenu", "faux_refus",
                "citation_introuvable", "parsing"]
LABELS: tuple[str, ...] = ("bonne_reponse", "mauvais_doc", "doc_manque", "claim_non_soutenu",
                           "faux_refus", "citation_introuvable", "parsing")

Suite = Literal["guide", "sinistre", "parsing"]

GateProfile = Literal["vertical", "full"]
PROFILS_LIVRES: tuple[str, ...] = ("vertical", "full")

FAMILLES_FULL: dict[str, frozenset[str]] = {
    "guide": frozenset({
        "parcours", "meteo", "suivi", "multilingue", "trois_fiches", "hors_guide",
    }),
    "sinistre": frozenset({
        "absurde", "multiple", "hors_habitation", "vide", "contradictoire",
        "clairement_couvert", "sejour_temporaire",
        "telephone_vacances", "exclusion_animale",
        "perte_exploitation", "voies_garantie", "acte_volontaire",
    }),
    "parsing": frozenset({
        "acceptation", "definition", "exclusion", "garantie", "obligations", "portee",
        "responsabilite",
    }),
}

VARIANTES_PAR_SUITE: dict[str, tuple[str, ...]] = {
    "guide": tuple(sorted(VARIANTES_GUIDE)),
    "sinistre": tuple(sorted(pipeline_sinistre.VARIANTES)),
    "parsing": ("local",),
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
                 cost_eur_engaged: float | None = None, http: int | None = None,
                 latency_ms_engaged: int | None = None) -> None:
        super().__init__(message)
        self.resultats = list(resultats or [])
        self.non_executes = list(non_executes or [])
        self.arret_budget = arret_budget
        self.cost_eur_engaged = cost_eur_engaged
        # Statut AD-16 de l'exécution qui a interrompu le run (503/500) — publié `stop_http` dans
        # le rapport partiel (revue 4.2b, LOW 15). `None` quand l'incident n'est pas une exécution.
        self.http = http
        self.latency_ms_engaged = latency_ms_engaged


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
    # Une clarification est un non-résultat qui demande une précision, pas un refus documenté.
    clarification: bool | None = None
    # Suite `parsing` seulement (AD-14 : « compare le texte de blocs clés à une lecture visuelle »).
    text_norm: str | None = None

    @model_validator(mode="after")
    def _identifiants_uniques(self) -> Attendu:
        for champ, valeurs in (("block_ids", self.block_ids), ("fiche_ids", self.fiche_ids)):
            if any(valeur != valeur.strip() or not valeur.strip() for valeur in valeurs):
                raise ValueError(f"expected.{champ} exige des identifiants non blancs et trimés")
            if len(valeurs) != len(set(valeurs)):
                raise ValueError(f"expected.{champ} ne peut pas contenir deux fois le même identifiant")
        if self.refusal is True and self.clarification is True:
            raise ValueError("expected.refusal et expected.clarification ne peuvent pas être vrais ensemble")
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
    famille: str = ""
    expected: Attendu
    truth: Verite
    mode_attendu: Label
    _doc_id: str | None = PrivateAttr(default=None)
    _case_path: Path | None = PrivateAttr(default=None)
    _case_bytes: bytes | None = PrivateAttr(default=None)
    _case_resolved_path: Path | None = PrivateAttr(default=None)
    _case_directory_entries: tuple[str, ...] | None = PrivateAttr(default=None)

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
        if self.question != self.question.strip():
            raise ValueError("question doit être non vide et sans espaces de bord")
        if self.lang is not None and (not self.lang.strip() or self.lang != self.lang.strip()):
            raise ValueError("lang doit être non vide et trimée")
        if self.profile == "full" and self.truth.source not in {"lecture_humaine", "claude"}:
            raise ValueError("un cas full de la story 4.2 exige truth.source=lecture_humaine ou claude")
        if self.profile == "full" and self.suite in {"guide", "sinistre"}:
            if not self.scenario.strip() or self.scenario != self.scenario.strip():
                raise ValueError("un cas full guide/sinistre exige un scenario non vide et trimé")
            if self.famille != self.famille.strip() or self.famille not in FAMILLES_FULL[self.suite]:
                attendues = ", ".join(sorted(FAMILLES_FULL[self.suite]))
                raise ValueError(f"famille full {self.suite} inconnue ou non trimée (attendu : {attendues})")
        if self.suite == "guide" and self.faits is not None:
            raise ValueError("un cas de la suite guide ne porte pas de `faits` (le guide n'a pas de sinistre)")
        if self.suite == "sinistre":
            if self.faits is None:
                raise ValueError("un cas de la suite sinistre exige des `faits`")
            if self.profil is not None or self.historique:
                raise ValueError("le pipeline sinistre ne reçoit ni `profil` ni `historique` : "
                                 "les déclarer ferait croire qu'ils sont pris en compte")
        if self.suite == "parsing":
            if self.profile != "full":
                raise ValueError("un cas parsing appartient au profile `full`")
            if self.famille != self.famille.strip() or self.famille not in FAMILLES_FULL["parsing"]:
                attendues = ", ".join(sorted(FAMILLES_FULL["parsing"]))
                raise ValueError(
                    f"famille parsing inconnue ou non trimée (attendu : {attendues})")
            if self.profil is not None or self.faits is not None or self.historique:
                raise ValueError("le parsing local ne reçoit ni profil, ni faits, ni historique")
            if self.expected.text_norm is None or not self.expected.text_norm.strip():
                raise ValueError("un cas parsing exige expected.text_norm")
            if normalize(self.expected.text_norm) != self.expected.text_norm:
                raise ValueError("expected.text_norm doit déjà être normalisé par normalize()")
            if len(self.expected.block_ids) != 1:
                raise ValueError("un cas parsing référence exactement un expected.block_ids")
            if not self.expected.found or self.expected.fiche_ids or self.expected.verdict \
                    or self.expected.refusal is not None or self.expected.clarification is not None \
                    or self.expected.complete is not None:
                raise ValueError("un cas parsing ne porte que found=true, un block_id et text_norm")
            if self.mode_attendu != "bonne_reponse":
                raise ValueError("la transcription de référence attend mode_attendu=bonne_reponse")
        elif self.text_norm_declare:
            # Toutes les autres attentes produisent un écart quand elles ne sont pas tenues ; celle-ci
            # n'est lue que par le chemin local de la suite `parsing`.
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


class ReferenceUtilite(BaseModel):
    """Grille manuelle stable des trois critères d'utilité d'un cas guide."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    ordre_juste: list[str] = Field(min_length=1)
    documents_cites: list[str] = Field(min_length=1)
    interlocuteur: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    countersigned_by: str | None = None

    @field_validator("case_id", "interlocuteur", "provenance", "countersigned_by")
    @classmethod
    def _chaine_non_blanche(cls, valeur: str | None) -> str | None:
        if valeur is not None and not valeur.strip():
            raise ValueError("la chaîne ne peut pas être blanche")
        return valeur

    @field_validator("ordre_juste", "documents_cites")
    @classmethod
    def _items_non_blancs(cls, valeurs: list[str]) -> list[str]:
        if any(not valeur.strip() for valeur in valeurs):
            raise ValueError("aucun item ne peut être blanc")
        return valeurs


class ControleRetraduction(BaseModel):
    """Contrôle versionné d'une réponse multilingue déjà mesurée, sans nouveau rejeu."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    langue: Literal["en", "de", "pt"]
    fixture: str = Field(min_length=1)
    test_id: str = Field(min_length=1)
    journal: str = Field(min_length=1)
    journal_section: str = Field(min_length=1)
    resultat: Literal["fidele", "ecart"]
    ecarts: list[str] = Field(default_factory=list)
    reserve_signature: str = Field(min_length=1)
    countersigned_by: str | None = None

    @field_validator("case_id", "fixture", "test_id", "journal", "journal_section", "reserve_signature",
                     "countersigned_by")
    @classmethod
    def _chaine_non_blanche(cls, valeur: str | None) -> str | None:
        if valeur is not None and not valeur.strip():
            raise ValueError("la chaîne ne peut pas être blanche")
        return valeur

    @field_validator("ecarts")
    @classmethod
    def _ecarts_non_blancs(cls, valeurs: list[str]) -> list[str]:
        if any(not valeur.strip() for valeur in valeurs):
            raise ValueError("un écart déclaré ne peut pas être blanc")
        return valeurs

    @model_validator(mode="after")
    def _resultat_coherent(self) -> ControleRetraduction:
        if self.resultat == "fidele" and self.ecarts:
            raise ValueError("resultat=fidele exige une liste ecarts vide")
        if self.resultat == "ecart" and not self.ecarts:
            raise ValueError("resultat=ecart exige au moins un écart")
        return self


class FichierUtilite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["utilite_guide"]
    version: Literal[1]
    references: list[ReferenceUtilite] = Field(min_length=1)


class FichierRetraduction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["retraductions"]
    version: Literal[1]
    references: list[ControleRetraduction] = Field(min_length=1)


@dataclass(frozen=True)
class ReferencesSnapshot:
    digest: str
    root: Path | None = None
    contents: dict[Path, bytes] = field(default_factory=dict)
    entries: tuple[str, ...] = ()

    @property
    def files(self) -> tuple[Path, ...]:
        return tuple(sorted(self.contents))


def charger_references(cas: list[Cas], reference_dir: Path) -> ReferencesSnapshot:
    """Valide tous les compagnons et les relie aux cas, sans tolérer une référence orpheline."""
    guides_a_documenter = [c for c in cas if c.suite == "guide" and c.scenario.strip()]
    if not reference_dir.exists():
        if guides_a_documenter:
            raise RefusDeTourner(f"{reference_dir} : dossier de références absent")
        return ReferencesSnapshot(empreinte_canonique([]))
    if not reference_dir.is_dir():
        raise RefusDeTourner(f"{reference_dir} : un dossier de références est attendu")
    try:
        root = reference_dir.resolve(strict=True)
        entries = tuple(sorted(p.name for p in reference_dir.iterdir() if not p.name.startswith(".")))
    except OSError as exc:
        raise RefusDeTourner(f"{reference_dir} : dossier de références illisible") from exc
    etrangers = sorted(p.name for p in reference_dir.iterdir()
                       if not p.name.startswith(".") and p.suffix != ".yaml"
                       and p.name not in PROTOCOLE_FILES)
    if etrangers:
        raise RefusDeTourner(
            f"{reference_dir} : entrées hors schéma de nommage : {', '.join(etrangers)}")
    ids_cas = {c.id: c for c in cas}
    utilites: dict[str, ReferenceUtilite] = {}
    retraductions: dict[str, ControleRetraduction] = {}
    fichiers = tuple(sorted(p for p in reference_dir.iterdir()
                            if not p.name.startswith(".") and p.suffix == ".yaml"
                            and p.name not in PROTOCOLE_FILES))
    contenus: dict[Path, bytes] = {}
    for fichier in fichiers:
        try:
            resolu = fichier.resolve(strict=True)
            resolu.relative_to(root)
            contenu = resolu.read_bytes()
            # Le chemin lexical reste dans le snapshot : si un symlink interne est remplacé après
            # la validation, la revérification relit sa nouvelle cible et détecte le changement.
            contenus[root / fichier.name] = contenu
            brut = yaml.safe_load(contenu.decode("utf-8"))
            if not isinstance(brut, dict):
                raise ValueError("un objet YAML est attendu")
            kind = brut.get("kind")
            if kind == "utilite_guide":
                charge = FichierUtilite.model_validate(brut)
                cible = utilites
            elif kind == "retraductions":
                charge = FichierRetraduction.model_validate(brut)
                cible = retraductions
            else:
                raise ValueError("kind doit valoir utilite_guide ou retraductions")
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
            raise RefusDeTourner(f"{fichier} : compagnon invalide ({exc})") from exc
        for reference in charge.references:
            if reference.case_id in cible:
                raise RefusDeTourner(
                    f"{fichier} : référence dupliquée pour {reference.case_id!r}")
            cible[reference.case_id] = reference
    for case_id in sorted(set(utilites) | set(retraductions)):
        if case_id not in ids_cas:
            raise RefusDeTourner(f"référence orpheline : aucun cas {case_id!r}")
    for case_id in sorted(utilites):
        if ids_cas[case_id].suite != "guide":
            raise RefusDeTourner(f"référence d'utilité {case_id!r} : la cible doit être guide")
    for case_id in sorted(retraductions):
        cible = ids_cas[case_id]
        if not (cible.suite == "guide" and cible.profile == "full" and cible.famille == "multilingue"):
            raise RefusDeTourner(
                f"contrôle de retraduction {case_id!r} : la cible doit être guide/full/multilingue")
    manquantes = sorted(c.id for c in guides_a_documenter if c.id not in utilites)
    if manquantes:
        raise RefusDeTourner("grille d'utilité absente pour : " + ", ".join(manquantes))
    multilingues = [c for c in cas
                    if c.suite == "guide" and c.profile == "full" and c.famille == "multilingue"]
    manquantes = sorted(c.id for c in multilingues if c.id not in retraductions)
    if manquantes:
        raise RefusDeTourner("contrôle de retraduction absent pour : " + ", ".join(manquantes))
    for c in multilingues:
        controle = retraductions[c.id]
        if controle.langue != c.lang:
            raise RefusDeTourner(
                f"cas {c.id} : langue du contrôle {controle.langue!r} "
                f"différente du cas {c.lang!r}")
        for champ, relatif in (("fixture", controle.fixture), ("journal", controle.journal)):
            try:
                cible = (REPO_ROOT / relatif).resolve(strict=True)
                cible.relative_to(REPO_ROOT.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise RefusDeTourner(
                    f"cas {c.id} : {champ} de retraduction absent ou hors dépôt ({relatif})") from exc
            if not cible.is_file():
                raise RefusDeTourner(f"cas {c.id} : {champ} n'est pas un fichier ({relatif})")
        test_file, _, test_name = controle.test_id.partition("::")
        if not test_name or test_file != "tests/test_langues_live.py":
            raise RefusDeTourner(f"cas {c.id} : test_id de retraduction non rejouable")
        try:
            test_source = (REPO_ROOT / test_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RefusDeTourner(
                f"cas {c.id} : test de retraduction illisible ({test_file})") from exc
        if f"def {test_name}(" not in test_source and f"async def {test_name}(" not in test_source:
            raise RefusDeTourner(f"cas {c.id} : test_id absent de {test_file}")
        try:
            journal = (REPO_ROOT / controle.journal).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RefusDeTourner(
                f"cas {c.id} : journal de retraduction illisible ({controle.journal})") from exc
        journal_case_id = c.id.removeprefix("g-lang-")
        if controle.journal_section not in journal or f"`{journal_case_id}`" not in journal:
            raise RefusDeTourner(
                f"cas {c.id} : section/résultat absent du journal de retraduction")
    h = hashlib.sha256()
    for fichier, contenu in sorted(contenus.items()):
        h.update(fichier.name.encode("utf-8") + b"\0" + contenu + b"\0")
    return ReferencesSnapshot(h.hexdigest(), root, contenus, entries)


def charger_cas(cases_dir: Path, *, suites: tuple[str, ...] | None = None) -> list[Cas]:
    """Tous les cas des suites demandées, validés strictement — sinon `RefusDeTourner`.

    Le nom du fichier fait foi : `id` doit être le radical du fichier. C'est ce qui rend un cas
    dupliqué impossible à cacher (deux fichiers ne peuvent pas porter le même radical dans le même
    dossier), et ce qui fait que `cases_hash` — un hash de chemins et de contenus — désigne sans
    ambiguïté les cas dont un gate se réclame.
    """
    balayage_complet = suites is None
    if suites is None:
        # **Tous** les dossiers présents, pas seulement une liste recopiée : une suite déposée dans
        # l'arborescence doit être vue et validée, jamais ignorée en silence.
        suites = tuple(sorted(p.name for p in cases_dir.iterdir() if p.is_dir())) \
            if cases_dir.is_dir() else ()
    cas: list[Cas] = []
    for suite_locator in sorted(suites):
        morceaux = Path(suite_locator).parts
        suite = morceaux[0] if morceaux else ""
        if suite not in {"guide", "sinistre", "parsing"} or len(morceaux) > 2:
            raise RefusDeTourner(f"suite documentaire invalide : {suite_locator!r}")
        doc_id = morceaux[1] if len(morceaux) == 2 else None
        if doc_id is not None and suite not in {"sinistre", "parsing"}:
            raise RefusDeTourner(
                "seules les suites sinistre et parsing acceptent un sous-dossier documentaire "
                f"({suite_locator})")
        if suite == "parsing" and doc_id is None:
            # La racine est un simple rangement : chaque observation doit nommer son document par
            # le sous-dossier, afin qu'aucun repli sur un autre corpus ne soit possible.
            pass
        if doc_id is not None:
            _valider_doc_id(doc_id)
        dossier = cases_dir.joinpath(*morceaux)
        if not dossier.is_dir():
            continue
        _dans_cases(cases_dir, dossier, objet="suite documentaire")
        entrees_dossier = tuple(sorted(f.name for f in dossier.iterdir()
                                       if not f.name.startswith(".")))
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
                and not (suite in {"sinistre", "parsing"} and doc_id is None and f.is_dir())
            )
        )
        if etrangers:
            raise RefusDeTourner(f"{dossier} : entrées hors schéma de nommage : "
                                 f"{', '.join(etrangers)} (les cas sont des `*.yaml` à plat ; "
                                 "seuls les sous-dossiers documentaires de `sinistre/` et "
                                 "`parsing/` sont admis)")
        if suite == "parsing" and doc_id is None and any(dossier.glob("*.yaml")):
            raise RefusDeTourner(
                f"{dossier} : un cas parsing doit vivre sous parsing/<doc_id>/")
        for fichier in sorted(dossier.glob("*.yaml")):
            resolu = _dans_cases(cases_dir, fichier, objet="cas YAML")
            lu = _lire_cas(resolu, suite)
            lu._doc_id = doc_id
            lu._case_path = fichier.absolute()
            lu._case_resolved_path = resolu
            lu._case_directory_entries = entrees_dossier
            cas.append(lu)
        if (suite == "parsing" or (balayage_complet and suite == "sinistre")) and doc_id is None:
            for sous_dossier in sorted(p for p in dossier.iterdir() if p.is_dir() and not p.name.startswith(".")):
                cas.extend(charger_cas(cases_dir, suites=(f"{suite}/{sous_dossier.name}",)))
    vus: set[str] = set()
    for c in cas:
        if c.id in vus:
            raise RefusDeTourner(f"deux cas portent l'identifiant {c.id!r}")
        vus.add(c.id)
    return cas


def _lire_cas(fichier: Path, suite_du_dossier: str) -> Cas:
    try:
        contenu = fichier.read_bytes()
        brut = yaml.safe_load(contenu.decode("utf-8"))
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
    if cas.id != fichier.stem:
        raise RefusDeTourner(f"{fichier} : champ id — {cas.id!r} devrait être {fichier.stem!r} "
                             "(le nom du fichier fait foi : c'est lui qu'agrège `cases_hash`)")
    if cas.suite != suite_du_dossier:
        raise RefusDeTourner(f"{fichier} : champ suite — {cas.suite!r} dans le dossier "
                             f"{suite_du_dossier!r}")
    cas._case_bytes = contenu
    return cas


def refuser_ce_qui_nest_pas_livre(cas: list[Cas], profil: str) -> None:
    """Les profils inconnus sont refusés, jamais approximés.

    Le profil ``full`` inclut explicitement les cas ``vertical`` et ``full`` ; le profil
    ``vertical`` ne garde que les premiers. Le filtre est appliqué dans ``main`` après que tous les
    cas ont été strictement chargés, donc aucun fichier inconnu ne disparaît.
    """
    if profil not in PROFILS_LIVRES:
        raise RefusDeTourner(f"profil `{profil}` inconnu (livré : {', '.join(PROFILS_LIVRES)})")


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
    # Story 4.2b — la publication complète par répétition. `repetition` est 1-indexée ;
    # `doc_id` situe le résultat ; `complete` et `reason_kind` viennent de l'`Answer` ;
    # `opened_block_ids` sont les blocs effectivement passés au modèle (`StepTrace.opened_block_ids`,
    # jusqu'ici jetés avec la trace) ; `steps` porte tier/coût/latence par étape ; `proofs` est la
    # projection normalisée des claims affichées — `{doc_id, block_id, kind, quote_hash}` — qui fonde
    # l'agrégat de stabilité.
    repetition: int = 1
    doc_id: str = ""
    complete: bool | None = None
    reason_kind: str | None = None
    opened_block_ids: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    proofs: list[dict[str, Any]] = field(default_factory=list)
    # AD-11 : toute réponse du pipeline (refus compris) est un 200 ; une exécution interrompue
    # n'a pas de `Resultat` et son statut AD-16 part dans `stop_http`. `None` pour la suite
    # `parsing`, locale — aucune sémantique HTTP (revue 4.2b, LOW 15).
    http: int | None = 200

    @property
    def ok(self) -> bool:
        """Le gate exige `ok`, pas seulement le label (D2) : une attente inassouvie est un échec."""
        return not self.ecarts

    @property
    def execution_id(self) -> str:
        return f"{self.id}#r{self.repetition}"


@dataclass(frozen=True)
class CasesSnapshot:
    cases_hash: str
    files: dict[Path, str] = field(default_factory=dict)
    directories: dict[Path, tuple[str, ...]] = field(default_factory=dict)


def snapshot_cas(cas: list[Cas], cases_dir: Path,
                 references: ReferencesSnapshot | None = None) -> CasesSnapshot:
    """Fige les cas et compagnons validés avant le premier appel fournisseur ou calcul local."""
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
        if c._case_resolved_path is not None and path != c._case_resolved_path:
            raise RefusDeTourner(
                f"cas {c.id!r} : cible modifiée entre validation et snapshot")
        contenu = c._case_bytes
        if contenu is None:
            contenu = path.read_bytes()
        assert root is not None
        lexical = candidat.absolute()
        # Même clé que `app.digests.cases_hash` : le chemin résolu relatif à la racine résolue.
        # Les octets, eux, restent ceux capturés et parsés par `_lire_cas`.
        relatif = path.relative_to(root).as_posix()
        h.update(relatif.encode("utf-8") + b"\0")
        h.update(contenu + b"\0")
        fichiers[lexical] = hashlib.sha256(contenu).hexdigest()
    # Les compagnons ont leur autorité dédiée (`references_digest`). Ils restent figés et
    # revérifiés avec les cas, mais ne polluent pas `cases_hash`, que `app/digests.py` recalcule
    # exclusivement depuis les YAML de cas lors du gate.
    for path, contenu in sorted((references.contents if references else {}).items()):
        fichiers[path] = hashlib.sha256(contenu).hexdigest()
    dossiers: dict[Path, tuple[str, ...]] = {}
    for c, path in ((c, c.case_path) for c in cas if c.case_path is not None):
        assert path is not None
        if c._case_directory_entries is not None:
            dossiers[path.parent] = c._case_directory_entries
    for path in fichiers:
        if path.is_relative_to(root) if root is not None else False:
            parent = path.parent
            dossiers.setdefault(parent, tuple(sorted(p.name for p in parent.iterdir()
                                                   if not p.name.startswith("."))))
    if references is not None and references.root is not None:
        dossiers[references.root] = references.entries
    return CasesSnapshot(h.hexdigest(), fichiers, dossiers)


def verifier_snapshot_cas(snapshot: CasesSnapshot) -> None:
    def etiquette(path: Path) -> str:
        try:
            return path.resolve(strict=False).relative_to(REPO_ROOT.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    modifies = []
    for path, attendu in snapshot.files.items():
        try:
            courant = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            courant = "absent"
        if courant != attendu:
            modifies.append(etiquette(path))
    if modifies:
        raise IncidentTechnique(
            "cas modifiés pendant le run : " + ", ".join(sorted(modifies))
            + " — rapports et gate non certifiés")
    modifies_dossiers = []
    for path, attendu in snapshot.directories.items():
        try:
            courant = tuple(sorted(p.name for p in path.iterdir() if not p.name.startswith(".")))
        except OSError:
            courant = ("<absent>",)
        if courant != attendu:
            modifies_dossiers.append(etiquette(path))
    if modifies_dossiers:
        raise IncidentTechnique(
            "contenu de dossiers modifié pendant le run : "
            + ", ".join(sorted(modifies_dossiers))
            + " — rapports et gate non certifiés")


def _bloc(index: Index, block_id: str) -> Any | None:
    """Le bloc du corpus servi, ou `None` s'il n'y est pas (jamais une exception)."""
    try:
        doc_id = index.doc_of(block_id)
        return index.corpus.documents[doc_id].block(block_id)
    except KeyError:
        return None


def quote_hash(segment_norm: str) -> str:
    """SHA-256 du segment relu (`Block.text_norm[start:end]`) préfixé de `normalize_version`.

    Stable entre répétitions à corpus égal, sans nouveau champ de domaine (Design Notes 4.2b) :
    deux répétitions qui citent le même passage produisent le même hash, quel que soit le
    `claim_id` — refait à neuf par chaque appel de *rédiger* — ou la chaîne rendue par le modèle.
    """
    return hashlib.sha256(f"{normalize_version}:{segment_norm}".encode()).hexdigest()


def _preuves(answer: Answer, index: Index) -> list[dict[str, Any]]:
    """Projection normalisée des claims affichées : `{doc_id, block_id, kind, quote_hash}` (4.2b).

    C'est l'identité de « même claim » de l'agrégat de stabilité : la preuve, jamais le libellé.
    Un bloc que le corpus servi ne connaît pas rend une preuve sans hash (`quote_hash: None`) —
    l'écart est publié, pas masqué.
    """
    preuves: list[dict[str, Any]] = []
    for claim in answer.claims:
        for q in claim.quotes:
            bloc = _bloc(index, q.block_id)
            start = getattr(q, "start", None)
            end = getattr(q, "end", None)
            if bloc is None or start is None or end is None:
                preuves.append({"doc_id": None, "block_id": q.block_id, "kind": None,
                                "quote_hash": None})
                continue
            preuves.append({
                "doc_id": index.doc_of(q.block_id),
                "block_id": q.block_id,
                "kind": bloc.kind,
                "quote_hash": quote_hash(bloc.text_norm[start:end]),
            })
    return sorted(preuves, key=lambda p: (p["doc_id"] or "", p["block_id"], p["quote_hash"] or ""))


def _etapes(trace: Trace) -> list[dict[str, Any]]:
    """Tier, coût et latence par étape — publiés par répétition au lieu d'être jetés avec la trace."""
    return [{
        "name": s.name,
        "tier": s.tier,
        "cost_eur": round(sum(call.usage.cost_eur for call in s.calls), 4),
        "ms": s.ms,
        "calls": len(s.calls),
    } for s in trace.steps]


def _blocs_ouverts(trace: Trace) -> list[str]:
    vus: dict[str, None] = {}
    for s in trace.steps:
        for block_id in s.opened_block_ids:
            vus.setdefault(block_id, None)
    return list(vus)


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
        refus = (answer.found is False and answer.reason is not None
                 and answer.reason.kind != "clarification_requise")
        if refus is not cas.expected.refusal:
            ecarts.append(f"refus justifié={refus} (attendu {cas.expected.refusal})")
    if cas.expected.clarification is not None:
        clarification = (answer.found is False and answer.reason is not None
                          and answer.reason.kind == "clarification_requise"
                          and answer.clarification is not None)
        if clarification is not cas.expected.clarification:
            ecarts.append(
                f"clarification explicite={clarification} (attendu {cas.expected.clarification})")
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
    client: Any | None
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
    if suite == "guide":
        return settings.guide_doc_id
    if suite == "sinistre":
        return settings.sinistre_doc_id
    raise RefusDeTourner(f"suite `{suite}` sans sous-dossier documentaire")


def variante_du_cas(cas: Cas, variante_demandee: str | None) -> str:
    """Résout la variante avant tout appel, en refusant les couples suite/variante impossibles."""
    suite = cas.suite.split("/", 1)[0]
    connues = VARIANTES_PAR_SUITE.get(suite, ())
    variante = variante_demandee or (
        "outils" if suite == "guide" else "local" if suite == "parsing" else "deterministe")
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
                     else ctx.dictionnaires.get(doc_id) if cas.suite == "sinistre" else None)
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
        # Le parsing mesure les blocs ingérés ; publier le hash d'un `Dictionnaire()` vide ferait
        # croire à une autorité lexicale qui n'a participé ni au chargement ni au jugement local.
        "dictionary_fingerprint": (
            None if cas.suite == "parsing" else _empreinte_dictionnaire(dictionnaire)),
        "normalize_version": normalize_version,
        "input": entree,
        "input_digest": empreinte_canonique(entree),
    }


def identite_run(cas: list[Cas], ctx: Contexte, *, profile: str, quick: bool,
                 variant: str | None, references_digest: str | None = None,
                 plancher_digest: str | None = None, repeat: int = 1) -> dict[str, Any]:
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
        document = {
            champ: ns[champ] for champ in (
                "source_hash", "ingest_fingerprint", "overlay_hash",
                "dictionary_fingerprint",
            )
        }
        precedent = documents.get(doc_id)
        if precedent is not None and document["dictionary_fingerprint"] is None:
            document["dictionary_fingerprint"] = precedent["dictionary_fingerprint"]
        documents[doc_id] = document
    identite = {
        "image": {
            "pipeline_digest": ctx.pipeline_digest_hex,
            "prompts_digest": ctx.prompts_digest_hex,
            "model_ids": dict(TIERS),
            "normalize_version": normalize_version,
            # Story 4.2b : le protocole (plancher pré-enregistré) fait partie de l'identité — deux
            # runs ne se comparent qu'à plancher égal, comme à corpus égal.
            "plancher_digest": plancher_digest,
        },
        "scope": {
            "profile": profile,
            "quick": quick,
            "repeat": repeat,
            "case_ids": [c.id for c in cas],
            "suites": sorted({c.suite for c in cas}),
            "variants": variantes,
            "references_digest": references_digest,
        },
        "documents": dict(sorted(documents.items())),
        "cache_namespace_digests": namespaces,
    }
    # `run_digest` = empreinte canonique de l'identité (Design Notes 4.2b). Calculé sur l'identité
    # **sans** lui-même, puis publié dedans : c'est lui que les décisions du gate référencent.
    identite["run_digest"] = empreinte_canonique(identite)
    return identite


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
    if cas.suite == "parsing":
        raise RefusDeTourner("le parsing emprunte exclusivement le chemin local")
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


def _ecart_texte(attendu: str, observe: str) -> str:
    """Situe le premier écart normalisé sans recopier tout le contrat dans le rapport."""
    position = next((i for i, (a, b) in enumerate(zip(attendu, observe)) if a != b),
                    min(len(attendu), len(observe)))
    debut = max(0, position - 30)
    fin = position + 30
    return (f"texte normalisé différent à l'index {position} : "
            f"attendu={attendu[debut:fin]!r}, observé={observe[debut:fin]!r}")


def executer_parsing(cas: Cas, ctx: Contexte, *, doc_id: str) -> Resultat:
    """Compare un unique bloc servi à sa transcription visuelle, sans client ni fournisseur."""
    block_id = cas.expected.block_ids[0]
    bloc = _bloc(ctx.index, block_id)
    found = False
    ecarts: list[str] = []
    if bloc is None:
        label = "citation_introuvable"
        ecarts.append(f"bloc attendu absent du document {doc_id} : {block_id}")
    else:
        # L'identifiant encode son document, mais l'index reste l'autorité : aucune observation ne
        # peut réussir en tombant sur un bloc homonyme d'un autre contrat.
        try:
            document_observe = ctx.index.doc_of(block_id)
        except KeyError:
            document_observe = ""
        if document_observe != doc_id:
            label = "citation_introuvable"
            ecarts.append(
                f"bloc {block_id} servi par {document_observe!r}, attendu dans {doc_id!r}")
        else:
            found = True
            observe = normalize(bloc.text)
            attendu = cas.expected.text_norm or ""
            if observe == attendu:
                label = "bonne_reponse"
            else:
                label = "parsing"
                ecarts.append(_ecart_texte(attendu, observe))
    if label != cas.mode_attendu:
        ecarts.append(f"label {label} (mode_attendu {cas.mode_attendu})")
    return Resultat(
        id=cas.id, suite=cas.suite, label=label, variant="local", ecarts=ecarts,
        cost_eur=0.0, cost_eur_original=0.0,
        # `ms` mesure la latence fournisseur dans les rapports comparatifs : il n'y en a aucune.
        ms=0, found=found,
        expected_found=True, expected_block_ids=[block_id],
        http=None,  # comparaison locale : aucune sémantique HTTP (LOW 15)
    )


async def executer(cas: list[Cas], ctx: Contexte, *, max_cost_eur: float,
                   sortie: Any = sys.stdout, variant: str | None = None,
                   repeat: int = 1) -> list[Resultat]:
    """Exécute les cas dans l'ordre, en s'arrêtant **avant** l'exécution qui dépasserait le plafond.

    Story 4.2b : `repeat` répète chaque cas `repeat` fois — trois exécutions **payantes** quand le
    cache est désarmé (le runner le désarme sous `--repeat`), chacune publiée en entier. Tout
    incident — budget ou non — emporte les acquis (`resultats`) et le plan restant
    (`non_executes`) : le rapport partiel est toujours écrit, et les exécutions manquantes restent
    rouges au dénominateur.
    """
    if repeat < 1:
        raise RefusDeTourner(f"--repeat {repeat} : au moins une exécution par cas")
    if repeat > 1 and ctx.response_cache is not None:
        raise RefusDeTourner("--repeat exige un cache désarmé : une répétition servie du cache "
                             "ne mesure pas la stabilité (story 4.2b)")
    def _restants(index_cas: int, repetition_courante: int) -> list[str]:
        restants: list[str] = []
        for suivant_index in range(index_cas, len(cas)):
            debut = repetition_courante if suivant_index == index_cas else 1
            for suivante_repetition in range(debut, repeat + 1):
                identifiant = cas[suivant_index].id
                restants.append(identifiant if repeat == 1 else
                                 f"{identifiant}#r{suivante_repetition}")
        return restants

    resultats: list[Resultat] = []
    cumul = 0.0
    for index_cas, c in enumerate(cas):
        for repetition in range(1, repeat + 1):
            doc_id = c.doc_id or document_de_la_suite(ctx.settings, c.suite)
            if c.suite == "parsing":
                resultat = executer_parsing(c, ctx, doc_id=doc_id)
                resultat.repetition = repetition
                resultats.append(resultat)
                _ligne(resultat, sortie, repeat=repeat)
                continue
            restant = round(max_cost_eur - cumul, 4)
            if restant <= 0 and ctx.response_cache is None:
                non_executes = _restants(index_cas, repetition)
                raise IncidentTechnique(
                    f"plafond de run atteint ({cumul:.4f} € sur {max_cost_eur:.4f} €) avant "
                    f"l'exécution {c.id} (répétition {repetition}) : "
                    f"{len(non_executes)} exécutions non exécutées",
                    resultats=resultats, non_executes=non_executes,
                    arret_budget=True, cost_eur_engaged=cumul)
            variante = variante_du_cas(c, variant)
            if ctx.response_cache is not None:
                ctx.response_cache.set_namespace(namespace_cache(
                    c, ctx, doc_id=doc_id, variant=variante))
            depart = time.monotonic()
            try:
                answer, trace, cout = await executer_cas(
                    c, ctx, doc_id=doc_id, budget_restant_eur=restant, variant=variante)
            except _ErreurInterneFacturee as exc:
                cout_total = round(cumul + exc.cout_eur, 4)
                # Story 4.2b : l'incident n'efface plus les acquis — les résultats terminés et le
                # plan restant partent avec l'exception, et le rapport partiel est écrit.
                raise IncidentTechnique(
                    f"cas {c.id} (répétition {repetition}) : internal ({exc.type_erreur}) — "
                    f"coût engagé {cout_total:.4f} €",
                    resultats=resultats, non_executes=_restants(index_cas, repetition),
                    cost_eur_engaged=cout_total,
                    http=HTTP_STATUS[ErrorCode.internal],
                    latency_ms_engaged=int((time.monotonic() - depart) * 1000)) from exc
            except PipelineError as exc:
                cout_engage = exc.eval_cost_eur
                cout_total = round(cumul + cout_engage, 4)
                if exc.code is ErrorCode.budget_exceeded:
                    # Le plafond atteint pendant un cas est le même rouge que l'arrêt avant le
                    # suivant, vu une étape plus tôt.
                    raise IncidentTechnique(
                        f"plafond de run atteint pendant le cas {c.id} (répétition {repetition}, "
                        f"{cout_total:.4f} € engagés sur {max_cost_eur:.4f} €) : {exc.message}",
                        resultats=resultats,
                        non_executes=_restants(index_cas, repetition), arret_budget=True,
                        cost_eur_engaged=cout_total,
                        http=HTTP_STATUS[ErrorCode.budget_exceeded],
                        latency_ms_engaged=int((time.monotonic() - depart) * 1000)) from exc
                if exc.code in CODES_INCIDENT:
                    # D4 : un incident ne dit rien du système mesuré. Le manifest n'est pas touché,
                    # mais les acquis sont conservés et le rapport partiel écrit.
                    raise IncidentTechnique(
                        f"cas {c.id} (répétition {repetition}) : {exc.code.value} — {exc.message} — "
                        f"coût engagé {cout_total:.4f} €",
                        resultats=resultats, non_executes=_restants(index_cas, repetition),
                        cost_eur_engaged=cout_total, http=HTTP_STATUS[exc.code],
                        latency_ms_engaged=int((time.monotonic() - depart) * 1000)) from exc
                # `invalid_request` : le cas est hors des bornes du pipeline. C'est une faute
                # d'écriture du cas, pas une mesure ; refus, et rien n'est écrit non plus.
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
                            }),
                            repetition=repetition, doc_id=doc_id,
                            complete=answer.complete,
                            reason_kind=answer.reason.kind if answer.reason is not None else None,
                            opened_block_ids=_blocs_ouverts(trace),
                            steps=_etapes(trace),
                            proofs=_preuves(answer, ctx.index))
            resultats.append(resultat)
            _ligne(resultat, sortie, repeat=repeat)
    return resultats


def _ligne(r: Resultat, sortie: Any, *, repeat: int = 1) -> None:
    etiquette = r.id if repeat == 1 else r.execution_id
    print(f"{etiquette:<28} {r.label:<21} {'ok' if r.ok else 'ECART':<6} "
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


def _recall(resultats: list[Resultat], manquants: list[Cas] | None = None) -> float:
    """Recall du run. Story 4.2b : les exécutions **manquantes** (interruption) restent au
    dénominateur — chacune apporte ses attentes avec zéro trouvé, jamais retirée du calcul."""
    trouves = 0
    attendus = 0
    for r in resultats:
        if r.suite == "parsing":
            attendus += 1
            trouves += int(r.ok)
            continue
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
    for c in manquants or []:
        if c.suite == "parsing":
            attendus += 1
        elif c.expected.block_ids or c.expected.fiche_ids:
            attendus += len(c.expected.block_ids) + len(c.expected.fiche_ids)
        elif c.expected.found:
            attendus += 1
    return round(trouves / attendus, 4) if attendus else 0.0


def _cas_de_lexecution(execution_id: str) -> str:
    """`g-cas#r2` → `g-cas` ; un id sans répétition reste lui-même."""
    return execution_id.split("#r", 1)[0]


def _signature_stabilite(r: Resultat) -> dict[str, Any]:
    """L'identité comparée entre répétitions (story 4.2b).

    Sinistre : « même claim » ⇔ même preuve `{doc_id, block_id, kind, quote_hash}`, et même
    verdict. Guide : même statut (`reason.kind`), `found`/`complete`, label, ensemble de fiches.
    """
    if r.suite.startswith("sinistre"):
        return {
            "verdict": r.verdict,
            "proofs": [f"{p['doc_id']}:{p['block_id']}:{p['kind']}:{p['quote_hash']}"
                       for p in r.proofs],
        }
    return {"found": r.found, "complete": r.complete, "label": r.label,
            "reason_kind": r.reason_kind, "fiches": list(r.cited_fiche_ids)}


def agreger_stabilite(resultats: list[Resultat], cas: list[Cas], *, repeat: int,
                      non_executes: list[str] | None = None) -> dict[str, Any]:
    """L'agrégat de stabilité par cas : preuves, verdicts, dispersion de coût et de latence.

    Un cas est **stable** ssi ses `repeat` répétitions sont toutes là (une répétition manquante
    est une interruption : rouge) et rendent la même signature — et, pour un sinistre dont le cas
    borne les verdicts admissibles, un verdict admissible. La dispersion est publiée, jamais
    masquée.

    Les cas `parsing` sont **exclus des métriques** `stabilite_*` — et l'exclusion est dite, pas
    tue (revue 4.2b, LOW 14) : la suite est locale et déterministe, sa « stabilité » ne mesure
    rien du modèle. Leur détail reste publié avec `comptabilise: false`.
    """
    par_cas = {c.id: c for c in cas}
    manquantes: dict[str, int] = {}
    for execution_id in non_executes or []:
        base = _cas_de_lexecution(execution_id)
        manquantes[base] = manquantes.get(base, 0) + 1
    detail: dict[str, Any] = {}
    for case_id in sorted({r.id for r in resultats} | set(manquantes)):
        reps = sorted((r for r in resultats if r.id == case_id), key=lambda r: r.repetition)
        signatures = [_signature_stabilite(r) for r in reps]
        raisons: list[str] = []
        if len(reps) < repeat:
            raisons.append(f"répétitions manquantes : {repeat - len(reps)} sur {repeat}")
        if signatures and any(s != signatures[0] for s in signatures[1:]):
            raisons.append("signatures divergentes entre répétitions")
        c = par_cas.get(case_id)
        if (c is not None and reps and reps[0].suite.startswith("sinistre") and c.expected.verdict
                and any(r.verdict not in c.expected.verdict for r in reps)):
            raisons.append("verdict hors des valeurs admissibles sur au moins une répétition")
        suite = reps[0].suite if reps else (c.suite if c is not None else "")
        detail[case_id] = {
            "suite": suite,
            "stable": not raisons,
            "raisons": raisons,
            "comptabilise": suite != "parsing",
            "repetitions_completed": len(reps),
            "repetitions_planned": repeat,
            "signatures": signatures,
            "cost_eur": [r.cost_eur for r in reps],
            "latency_ms": [r.ms for r in reps],
        }
    agregat: dict[str, Any] = {"n": repeat, "cases": detail}
    if any(not v["comptabilise"] for v in detail.values()):
        agregat["exclusions"] = {
            "parsing": "suite locale déterministe : hors métriques stabilite_* (détail publié)"}
    return agregat


def _taux_stabilite(stabilite: dict[str, Any], prefixe_suite: str) -> tuple[float, int] | None:
    cases = [v for v in stabilite["cases"].values() if str(v["suite"]).startswith(prefixe_suite)]
    if not cases:
        return None
    stables = sum(1 for v in cases if v["stable"])
    return round(stables / len(cases), 4), len(cases)


def _temoin_applicable(temoin: Any, cas: list[Cas]) -> bool:
    """Le témoin porte-t-il sur ce lot, même si sa mesure appartient à l'orchestrateur ?"""
    if temoin.scope in ("repo", "run"):
        return True
    if temoin.scope == "suite":
        return any(c.suite.split("/", 1)[0] == temoin.pipeline for c in cas)
    if temoin.scope == "case":
        return temoin.case_id is not None and any(c.id == temoin.case_id for c in cas)
    if temoin.scope == "live_http":
        return any(c.suite.split("/", 1)[0] == temoin.pipeline for c in cas)
    return False


def charger_decisions_orchestrateur(path: Path, *, plancher: ChargePlancher) -> list[GateDecision]:
    """Charge une preuve externe mesurée par l'orchestrateur, sans lui faire déclarer son statut.

    Le fichier ne fournit que les mesures : seuil, producteur et statut sont recalculés depuis le
    plancher versionné. Ainsi A16, `decision_claim` et les tests hors ligne peuvent rejoindre le
    gate sans être simulés par le runner ni auto-certifiés par le JSON d'entrée.
    """
    try:
        brut = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RefusDeTourner(
            f"preuve orchestrateur {path} illisible ({type(exc).__name__})") from exc
    if not isinstance(brut, dict) or brut.get("plancher_digest") != plancher.digest:
        raise RefusDeTourner("preuve orchestrateur : plancher_digest divergent")
    mesures = brut.get("decisions")
    if not isinstance(mesures, list):
        raise RefusDeTourner("preuve orchestrateur : decisions doit être une liste")
    decisions: list[GateDecision] = []
    deja: set[str] = set()
    for mesure in mesures:
        if not isinstance(mesure, dict) or set(mesure) != {"metric", "n", "value", "run_digest"}:
            raise RefusDeTourner(
                "preuve orchestrateur : chaque mesure porte metric, n, value et run_digest")
        metric = mesure["metric"]
        temoin = plancher.plancher.temoin(metric) if isinstance(metric, str) else None
        if temoin is None or temoin.mesure_par != "orchestrator":
            raise RefusDeTourner(f"preuve orchestrateur : métrique externe inconnue {metric!r}")
        if metric in deja:
            raise RefusDeTourner(f"preuve orchestrateur : métrique dupliquée {metric!r}")
        deja.add(metric)
        n, value, run_digest = mesure["n"], mesure["value"], mesure["run_digest"]
        if (isinstance(n, bool) or not isinstance(n, int) or n < 0
                or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or not 0 <= float(value) <= 1
                or not isinstance(run_digest, str) or len(run_digest) != 64
                or any(c not in "0123456789abcdef" for c in run_digest)):
            raise RefusDeTourner(f"preuve orchestrateur : mesure invalide pour {metric!r}")
        status = "green" if n >= temoin.n and float(value) >= temoin.plancher else "red"
        raison = None if status == "green" else (
            f"sous-échantillonné n={n} < N={temoin.n}" if n < temoin.n
            else f"valeur {float(value):.4f} < plancher {temoin.plancher:.4f}")
        scope = (f"case:{temoin.case_id}" if temoin.scope == "case" and temoin.case_id
                 else temoin.scope)
        decisions.append(GateDecision(
            metric=metric, producer="orchestrator", threshold=temoin.plancher, scope=scope,
            n=n, run_digest=run_digest, value=round(float(value), 4), status=status,
            reason=raison))
    return decisions


def construire_decisions(resultats: list[Resultat], cas: list[Cas], *, plancher: ChargePlancher,
                         repeat: int, run_digest: str, producer: str,
                         non_executes: list[str] | None = None,
                         decisions_orchestrateur: list[GateDecision] | None = None,
                         ) -> list[GateDecision]:
    """Les décisions chiffrées du run contre le plancher pré-enregistré (story 4.2b).

    Seules les métriques que **ce runner** mesure produisent une décision (`mesure_par:
    eval_runner`) : les témoins de l'orchestrateur (tests hors ligne, A16 HTTP, prédicat
    `decision_claim` de la branche 4.2a) restent les siens — les dupliquer ici serait une seconde
    autorité sur la même mesure. Trois règles ferment le vert par vacuité (revue 4.2b, HIGH 1) :

    - une décision dont `n` est sous le `N` du témoin pré-enregistré est **rouge**
      (`sous-échantillonné`) — un `--gate` sans `--repeat 3` ne prouve rien ;
    - tout témoin **bloquant** du runner dont le scope couvre ce lot produit une décision, même
      quand le run ne l'a pas mesuré : absente = non prouvée = rouge explicite ;
    - toute interruption reste au dénominateur (`executions_completes`, témoin du plancher).
    """
    decisions: list[GateDecision] = []
    planifiees = len(cas) * repeat
    manquantes = len(non_executes or [])

    def _decision(metric: str, *, value: float, n: int, scope: str) -> None:
        temoin = plancher.plancher.temoin(metric)
        if temoin is None:
            return
        raison: str | None = None
        status = "green"
        if producer != plancher.plancher.producer_de_preuve:
            status = "red"
            raison = (f"producteur non probant {producer!r} ; attendu "
                      f"{plancher.plancher.producer_de_preuve!r}")
        elif n < temoin.n:
            status = "red"
            raison = f"sous-échantillonné n={n} < N={temoin.n}"
        elif value < temoin.plancher:
            status = "red"
        decisions.append(GateDecision(
            metric=metric, producer=producer, threshold=temoin.plancher, scope=scope,
            n=n, run_digest=run_digest, value=round(value, 4), status=status, reason=raison))

    ok = sum(1 for r in resultats if r.ok)
    _decision("cases_ok_rate", value=(ok / planifiees) if planifiees else 0.0,
              n=planifiees, scope="run")
    # LOW 13 : le seuil d'`executions_completes` vit dans le plancher, jamais en dur ici.
    _decision("executions_completes",
              value=round((planifiees - manquantes) / planifiees, 4) if planifiees else 0.0,
              n=planifiees, scope="run")
    stabilite = agreger_stabilite(resultats, cas, repeat=repeat, non_executes=non_executes)
    for metric, prefixe in (("stabilite_sinistre", "sinistre"), ("stabilite_guide", "guide")):
        taux = _taux_stabilite(stabilite, prefixe)
        if taux is not None:
            # Sous `repeat=1` le taux est trivialement 1.0 : la décision existe quand même, et le
            # sous-échantillonnage la rend rouge — jamais un vert par absence de mesure.
            _decision(metric, value=taux[0], n=repeat, scope=f"suite:{prefixe}")
    for temoin in plancher.plancher.temoins:
        if temoin.mesure_par != "eval_runner" or temoin.case_id is None:
            continue
        if not any(c.id == temoin.case_id for c in cas):
            continue
        reps_ok = sum(1 for r in resultats if r.id == temoin.case_id and r.ok)
        _decision(temoin.metric, value=reps_ok / repeat, n=repeat,
                  scope=f"case:{temoin.case_id}")
    for decision in decisions_orchestrateur or []:
        temoin = plancher.plancher.temoin(decision.metric)
        if temoin is not None and _temoin_applicable(temoin, cas):
            decisions.append(decision)
    emises = {d.metric for d in decisions}
    for temoin in plancher.plancher.temoins:
        if (temoin.criticite != "bloquant" or temoin.metric in emises
                or not _temoin_applicable(temoin, cas)):
            continue
        decisions.append(GateDecision(
            metric=temoin.metric, producer=producer, threshold=temoin.plancher,
            scope=temoin.scope, n=0, run_digest=run_digest, value=0.0, status="red",
            reason=("témoin orchestrateur applicable absent de ce run"
                    if temoin.mesure_par == "orchestrator"
                    else "témoin bloquant applicable non prouvé par ce run")))
    return decisions


def construire_rapport(resultats: list[Resultat], cas: list[Cas], *, cases_dir: Path,
                       profile: str, max_cost_eur: float, complete: bool,
                       stop_reason: str | None = None,
                       non_executes: list[str] | None = None,
                       cost_eur_engaged: float | None = None,
                       snapshot: CasesSnapshot | None = None,
                       run_identity: dict[str, Any] | None = None,
                       repeat: int = 1,
                       plancher: ChargePlancher | None = None,
                       producer: str = "builder",
                       preflight: dict[str, Any] | None = None,
                       stop_http: int | None = None,
                       stop_latency_ms: int | None = None,
                       decisions_orchestrateur: list[GateDecision] | None = None) -> dict[str, Any]:
    snapshot = snapshot or snapshot_cas(cas, cases_dir)
    verifier_snapshot_cas(snapshot)
    labels = {label: sum(1 for r in resultats if r.label == label) for label in LABELS}
    variants = {variant: sum(1 for r in resultats if r.variant == variant)
                for variant in sorted({r.variant for r in resultats})}
    sinistres = [r for r in resultats if r.suite.startswith("sinistre")]
    cout_cas_termines = round(sum(r.cost_eur for r in resultats), 4)
    cout_total = round(cost_eur_engaged, 4) if cost_eur_engaged is not None else cout_cas_termines
    cout_original = round(sum(r.cost_eur_original for r in resultats), 4)
    par_cas = {c.id: c for c in cas}
    # Story 4.2b : les dénominateurs incluent les interruptions. Une exécution manquante reste
    # planifiée — la retirer du calcul rendrait un run interrompu plus flatteur qu'un run complet.
    executions_planifiees = len(cas) * repeat
    manquantes_ids = list(non_executes or [])
    manquants_cas = [par_cas[_cas_de_lexecution(execution_id)] for execution_id in manquantes_ids
                     if _cas_de_lexecution(execution_id) in par_cas]
    sinistres_planifies = len(sinistres) + sum(
        1 for c in manquants_cas if c.suite.startswith("sinistre"))
    cout_interrompu = max(0.0, round(cout_total - cout_cas_termines, 4))
    couts = sorted([r.cost_eur for r in resultats]
                   + ([cout_interrompu] if cout_interrompu > 0 else []))
    latences = sorted([r.ms for r in resultats]
                      + ([stop_latency_ms] if stop_latency_ms is not None else []))

    def _p95(valeurs: list[Any]) -> Any:
        return valeurs[max(0, math.ceil(0.95 * len(valeurs)) - 1)] if valeurs else 0

    rapport: dict[str, Any] = {
        "schema_version": 3,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "profile": profile,
        "identity": run_identity or {
            "scope": {"profile": profile, "case_ids": [c.id for c in cas]},
        },
        "complete": complete,
        "stop_reason": stop_reason,
        # Statut AD-16 de l'exécution qui a interrompu le run (`None` sur un run complet) : les
        # exécutions jamais démarrées n'ont pas de statut, elles sont dans `unexecuted_cases`.
        "stop_http": stop_http,
        "unexecuted_cases": manquantes_ids,
        "cases_hash": snapshot.cases_hash,
        "cases_planned": len(cas),
        "cases_completed": len({r.id for r in resultats}),
        "repeat": repeat,
        "executions_planned": executions_planifiees,
        "executions_completed": len(resultats),
        "executions_interrupted": len(manquantes_ids),
        "max_cost_eur": max_cost_eur,
        "cost_eur": cout_total,
        "cost_eur_original": cout_original,
        "metrics": {
            "labels": labels,
            "variants": variants,
            "recall": _recall(resultats, manquants_cas),
            "average_cost_eur": round(statistics.mean(couts), 4) if couts else 0.0,
            "latency_p50_ms": int(statistics.median(r.ms for r in resultats)) if resultats else 0,
            # Story 4.2b : la comparaison majorant estimé vs coût réel exige la queue de la
            # distribution, pas seulement la médiane (reprises différées 1.8/1.9).
            "cost_p50_eur": round(statistics.median(couts), 4) if couts else 0.0,
            "cost_p95_eur": _p95(couts),
            "cost_max_eur": couts[-1] if couts else 0.0,
            "latency_p95_ms": _p95(latences),
            "latency_max_ms": latences[-1] if latences else 0,
            "ne_tranche_pas_rate": (
                round(sum(1 for r in sinistres if r.verdict == "ne_tranche_pas")
                      / sinistres_planifies, 4)
                if sinistres_planifies else 0.0
            ),
        },
        "results": [
            {
                "id": r.id,
                "repetition": r.repetition,
                "suite": r.suite,
                "doc_id": r.doc_id,
                "label": r.label,
                "variant": r.variant,
                "ok": r.ok,
                "ecarts": r.ecarts,
                "cost_eur": r.cost_eur,
                "cost_eur_original": r.cost_eur_original,
                "latency_ms": r.ms,
                "http": r.http,
                "found": r.found,
                "complete": r.complete,
                "reason_kind": r.reason_kind,
                "verdict": r.verdict,
                "claims": r.claims,
                "proofs": r.proofs,
                "opened_block_ids": r.opened_block_ids,
                # « Blocs attendus ∈ blocs lus » : la preuve que le rappel a réellement présenté
                # les blocs attendus au modèle, publiée par exécution (spec 4.2b, Code Map). La
                # suite `parsing` est locale : aucun bloc n'est « lu » par un modèle.
                "expected_blocks_not_opened": ([] if r.suite == "parsing" else sorted(
                    set(r.expected_block_ids) - set(r.opened_block_ids))),
                "steps": r.steps,
            }
            for r in resultats
        ],
    }
    if preflight is not None:
        rapport["preflight"] = preflight
    if repeat > 1:
        rapport["stability"] = agreger_stabilite(resultats, cas, repeat=repeat,
                                                 non_executes=manquantes_ids)
    if plancher is not None:
        run_digest = str((run_identity or {}).get("run_digest", ""))
        decisions = construire_decisions(resultats, cas, plancher=plancher, repeat=repeat,
                                         run_digest=run_digest, producer=producer,
                                         non_executes=manquantes_ids,
                                         decisions_orchestrateur=decisions_orchestrateur)
        rapport["plancher_digest"] = plancher.digest
        rapport["decisions"] = [d.model_dump(mode="json") for d in decisions]
    return rapport


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
                    snapshot: CasesSnapshot | None = None,
                    decisions: list[GateDecision] | None = None,
                    run_digest: str | None = None) -> Gate:
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
        decisions=list(decisions or []), run_digest=run_digest,
        date=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"))


def ecrire_gate(manifest_path: Path, doc_id: str, gate: Gate) -> bool:
    """Écrit `manifest[doc_id].gate` — l'unique écriture de `data/` de tout ce module (AD-7).

    Le manifest entier est relu et **validé** avant la moindre écriture : un fichier illisible ou une
    entrée hors schéma arrête tout sans rien modifier sur disque. L'écriture est atomique (fichier
    temporaire puis `replace`) et garde la forme des autres écrivains de `data/` — clés triées,
    `indent=2`, `ensure_ascii=False`, saut de ligne final —, pour qu'un `git diff` de gate ne soit
    qu'un gate.

    **Non-mutation du dernier vert (story 4.2b).** Un gate candidat **rouge** ne remplace jamais un
    gate `evals_ok: true` : le verdict candidat est publié dans le rapport (décisions chiffrées,
    raison, limites), le mécanisme de promotion reste le seul chemin de bascule, et cette fonction
    rend `False` sans toucher le disque. Elle rend `True` quand le gate a été écrit.
    """
    try:
        brut = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RefusDeTourner(f"{manifest_path} illisible : {type(exc).__name__}") from exc
    if not isinstance(brut, dict) or doc_id not in brut:
        raise RefusDeTourner(f"{manifest_path} : aucune entrée {doc_id!r}")
    gate_existant = brut[doc_id].get("gate") if isinstance(brut[doc_id], dict) else None
    if (not gate.evals_ok and isinstance(gate_existant, dict)
            and gate_existant.get("evals_ok") is True):
        # Le rouge est un **résultat**, publié dans le rapport ; le dernier vert — gate, révision,
        # trafic — reste intact. Écraser le vert ferait d'un candidat raté une régression de
        # production (Intent 4.2b : « un rouge peut détruire le dernier vert »).
        print(f"gate candidat rouge sur {doc_id!r} : le gate vert existant n'est pas modifié "
              "(le verdict candidat, ses raisons et ses limites sont publiés dans le rapport)",
              file=sys.stderr)
        return False
    if (gate.evals_ok and not gate.decisions and isinstance(gate_existant, dict)
            and gate_existant.get("evals_ok") is True and gate_existant.get("decisions")):
        # HIGH 1 : un vert **sans décisions** — écrit à la main ou par un chemin antérieur au
        # protocole — ne remplace pas un vert qui porte sa preuve chiffrée. Ce serait troquer une
        # preuve contre un booléen.
        print(f"gate candidat sans décisions sur {doc_id!r} : le gate vert existant, qui porte "
              "ses décisions chiffrées, n'est pas modifié", file=sys.stderr)
        return False
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
    return True


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
                        pile: Any = None, cache_dir: Path | None = None,
                        campaign_budget_eur: float | None = None) -> Contexte:
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
                    client=LlmClient(settings, cache=response_cache,
                                     campaign_budget_eur=campaign_budget_eur),
                    pipeline_digest_hex=contexte_gate.pipeline_digest,
                    prompts_digest_hex=contexte_gate.prompts_digest,
                    # Lu dans `data_dir`, pas dans `lecture` : `_sans_gate_sur_disque` ne recopie que
                    # le manifest, et le dictionnaire n'a rien à voir avec le gate qu'on refait.
                    dictionnaire=load_dictionary(data_dir, corpus, settings.guide_doc_id),
                    dictionnaires=dictionnaires, response_cache=response_cache)


def construire_contexte_parsing(settings: Settings, data_dir: Path) -> Contexte:
    """Charge seulement les artefacts servis : aucun client, cache ou dictionnaire n'est construit."""
    contexte_gate = GateContext(pipeline_digest=pipeline_digest(), prompts_digest=prompts_digest(),
                                model_ids=dict(TIERS))
    corpus = load_corpus(data_dir, allow_ungated=True, current=contexte_gate,
                         perimetre_max_chars=settings.perimetre_max_chars,
                         raison_max_chars=settings.raison_publiable_max_chars)
    return Contexte(
        settings=settings, index=Index(corpus), client=None,
        pipeline_digest_hex=contexte_gate.pipeline_digest,
        prompts_digest_hex=contexte_gate.prompts_digest,
    )


async def _fermer(client: Any) -> None:
    """Ferme le pool de connexions du client, si c'en est un (les tests passent un double)."""
    fermer = getattr(client, "aclose", None)
    if fermer is not None:
        await fermer()


async def _executer_puis_fermer(cas: list[Cas], ctx: Contexte, *, gate: str | None,
                                max_cost_eur: float, sortie: Any,
                                variant: str | None = None, repeat: int = 1) -> list[Resultat]:
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
        return await executer(cas, ctx, max_cost_eur=max_cost_eur, sortie=sortie, variant=variant,
                              repeat=repeat)
    finally:
        await _fermer(ctx.client)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server.evals.run",
        description="Harness reproductible des questions-témoins (AD-14) : cache, budget et rapports.")
    p.add_argument("--suite", choices=sorted(get_args(Suite)),
                   help="n'exécuter que cette suite (défaut : toutes les suites livrées)")
    p.add_argument("--exclude-suite", action="append", default=[], choices=sorted(get_args(Suite)),
                   help="exclure une suite du lot (répétable ; utile aux évals fournisseur)")
    p.add_argument("--case", dest="cas", help="n'exécuter que ce cas (son identifiant)")
    p.add_argument("--profile", choices=PROFILS_LIVRES, default="vertical",
                   help="profil exécuté : vertical, ou full (inclut les cas vertical et full)")
    p.add_argument("--quick", action="store_true",
                   help="sous-ensemble CI stable : premier identifiant de chaque suite sélectionnée")
    p.add_argument("--variant", choices=VARIANTES_LIVREES,
                   help="variante (défaut : outils pour guide, déterministe pour sinistre, local pour parsing)")
    p.add_argument("--gate", metavar="DOC_ID",
                   help="écrire `manifest.gate` pour ce document depuis la suite qui le sert")
    p.add_argument("--repeat", type=int, default=1, metavar="N",
                   help="répéter chaque cas N fois, sans cache de réponse : publication complète "
                        "par répétition + agrégat de stabilité (story 4.2b ; N>=3 pour une preuve)")
    p.add_argument("--producer", choices=("builder", "orchestrator"), default="builder",
                   help="qui produit ce run : la règle trusted ne reconnaît que l'orchestrateur "
                        "comme producteur de preuve ; un run de builder est un diagnostic")
    p.add_argument("--orchestrator-evidence", type=Path,
                   help="mesures externes trusted (tests hors ligne, A16, decision_claim) ; "
                        "réservé à --producer orchestrator et --gate")
    p.add_argument("--max-cost", type=float, default=None,
                   help="plafond de coût du run en euros (défaut : evals_max_cost_eur de config.py ; "
                        "le budget effectif est min(--max-cost, LIVE_BUDGET_EUR))")
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
                    data_dir: Path, cache_dir: Path, reference_dir: Path | None = None) -> None:
    """Refuse avant client tout alias ou chevauchement pouvant écraser entrée ou état."""
    for nom, sortie in (("sortie JSON", output_json), ("sortie Markdown", output_markdown)):
        if sortie.exists() and sortie.is_dir():
            raise RefusDeTourner(
                f"chemins en collision : {nom}={sortie.resolve()} existe comme répertoire")
    reference_dir = reference_dir or cases_dir.parent / "reference"
    chemins = {
        "sortie JSON": output_json.resolve(),
        "sortie Markdown": output_markdown.resolve(),
        "cases_dir": cases_dir.resolve(),
        "reference_dir": reference_dir.resolve(),
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
    decisions_orchestrateur: list[GateDecision] = []
    references = ReferencesSnapshot(empreinte_canonique([]))
    reference_dir = args.cases_dir.parent / "reference"
    try:
        valider_chemins(output_json=output_json, output_markdown=output_markdown,
                        cases_dir=args.cases_dir, reference_dir=reference_dir,
                        data_dir=args.data_dir, cache_dir=cache_dir)
        settings = Settings()
        max_cost = settings.evals_max_cost_eur if args.max_cost is None else args.max_cost
        if not math.isfinite(max_cost) or max_cost <= 0:
            # `argparse(type=float)` accepte « inf » et « nan », et `nan <= 0` est **faux** : sans
            # `isfinite`, `--max-cost inf` ou `--max-cost nan` neutralisait le plafond et le run
            # partait sans borne, contre AD-9 et contre CLAUDE.md (« la clé **et un plafond** »).
            raise RefusDeTourner(f"plafond de run {max_cost} € : les évals tournent avec un plafond "
                                 "fini et strictement positif (CLAUDE.md, AD-9)")
        if args.repeat < 1:
            raise RefusDeTourner(f"--repeat {args.repeat} : au moins une exécution par cas")
        # Story 4.2b : le protocole précède toute mesure. Sans plancher chargé — donc sans digest —
        # aucun run ne part : il n'y aurait rien contre quoi comparer le résultat.
        try:
            charge_plancher = charger_plancher()
        except PlancherInvalide as exc:
            raise RefusDeTourner(str(exc)) from exc
        if args.repeat > charge_plancher.plancher.series.max_repeat:
            raise RefusDeTourner(
                f"--repeat {args.repeat} dépasse la borne pré-enregistrée "
                f"{charge_plancher.plancher.series.max_repeat}")
        if args.orchestrator_evidence is not None:
            if args.producer != "orchestrator" or args.gate is None:
                raise RefusDeTourner(
                    "--orchestrator-evidence exige --producer orchestrator et --gate")
            decisions_orchestrateur = charger_decisions_orchestrateur(
                args.orchestrator_evidence, plancher=charge_plancher)
        # Budget effectif de campagne : min(--max-cost, LIVE_BUDGET_EUR). La règle trusted fait
        # foi — 0,50 € par défaut, surchargé par l'environnement, jamais relevé en silence.
        budget_effectif = round(min(max_cost, settings.live_budget_eur), 4)
        # 1. Le profil, avant tout le reste.
        if args.profile not in PROFILS_LIVRES:
            raise RefusDeTourner(f"profil `{args.profile}` inconnu (livré : {', '.join(PROFILS_LIVRES)})")
        # 2. Le doc_id du gate avant toute composition de chemin ou lecture de cas.
        if args.gate:
            _valider_doc_id(args.gate)
        # 3. Les cas, avant toute décision de clé ou tout appel facturé. Un `--case p-*` peut ainsi
        # résoudre honnêtement vers un lot intégralement local sans exiger de secret fournisseur.
        suites = None
        if args.gate and args.cas:
            # `--gate` écrit un gate **de suite** : `cases` et `cases_hash` désignent ce qui a été
            # exécuté. Filtré par `--case`, il affirmerait `evals_ok: true` pour le document entier
            # sur un cas choisi à la main, et `tests/test_digests.py` le verrait — mais après coup.
            raise RefusDeTourner("--gate et --case sont exclusifs : un gate se réclame de la suite "
                                 "qui sert le document, jamais d'un cas choisi")
        if args.gate and args.quick:
            raise RefusDeTourner("--gate et --quick sont exclusifs : un gate exige la suite complète")
        if args.gate:
            suites = (suite_du_document(settings, args.gate, cases_dir=args.cases_dir),)
        if args.suite:
            if suites is not None and args.suite not in suites:
                raise RefusDeTourner(f"--suite {args.suite} et --gate {args.gate} se contredisent : "
                                     f"la suite qui sert {args.gate} est {suites[0]}")
            suites = (args.suite,)
        if args.suite and args.suite in args.exclude_suite:
            raise RefusDeTourner(
                f"--suite {args.suite} et --exclude-suite {args.suite} se contredisent")
        cas_tous = charger_cas(args.cases_dir)
        references = charger_references(cas_tous, reference_dir)
        if suites is None:
            cas = cas_tous
        else:
            demandes = set(suites)
            cas = [c for c in cas_tous if (
                (c.suite == "parsing" and "parsing" in demandes)
                or (c.suite == "sinistre" and args.suite == "sinistre" and args.gate is None)
                or (c.doc_id is None and c.suite in demandes)
                or (c.doc_id is not None and f"{c.suite}/{c.doc_id}" in demandes)
            )]
        if args.exclude_suite:
            cas = [c for c in cas if c.suite not in set(args.exclude_suite)]
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
        parsing_local_seul = args.gate is None and all(c.suite == "parsing" for c in cas)
        if not args.dry_run and not parsing_local_seul and cle_absente(settings):
            raise RefusDeTourner("les évals exigent une clé : ANTHROPIC_API_KEY est vide ou absente "
                                 "(AD-14 — les unitaires passent sans clé, les évals non)")
        # Les compagnons décrivent exclusivement les cas guide `full`. Les injecter dans un run
        # vertical périmerait les cinq gates historiques malgré leurs YAML byte-identiques.
        references_du_run = references if args.profile == "full" and any(
            c.suite == "guide" for c in cas) else None
        references_digest = references.digest if references_du_run else None
        snapshot = snapshot_cas(cas, args.cases_dir, references_du_run)
        # Story 4.2b — préflight : le majorant de la campagne est estimé **avant le premier appel**
        # (agrégat du majorant par requête d'AD-9, `max_cost_eur_per_request`, sur les exécutions
        # payantes). Une campagne payante (`--repeat` ≥ 2 : cache désarmé, chaque exécution est
        # facturée) dont le majorant dépasse le budget effectif est refusée proprement, chiffres
        # publiés, acquis conservés — jamais une question humaine (règle trusted).
        executions_payantes = sum(1 for c in cas if c.suite != "parsing") * args.repeat
        majorant_estime = estimate_run_majorant(executions_payantes, settings)
        preflight = {
            "configured_budget_eur": budget_effectif,
            "accrued_cost_eur": 0.0,
            "live_budget_eur": settings.live_budget_eur,
            "max_cost_eur": max_cost,
            "majorant_estime_eur": majorant_estime,
            "refused_cost_eur": majorant_estime,
            "executions_payantes": executions_payantes,
        }
        if args.dry_run:
            # Story 3.7 : jalon explicite avant toute dépense. Aucune construction de contexte —
            # donc ni client, ni lecture/écriture du manifest — et aucun besoin de clé.
            print("dry-run : aucun appel, aucun client et aucune écriture", file=sortie)
            print(f"profil={args.profile} gate={args.gate or '-'} cas={len(cas)} "
                  f"suites={','.join(suites or sorted({c.suite for c in cas}))} "
                  f"variantes={','.join(sorted({variante_du_cas(c, args.variant) for c in cas}))} "
                  f"ids={','.join(c.id for c in cas)} plafond={max_cost:.4f} EUR", file=sortie)
            print(f"plancher_digest={charge_plancher.digest} repeat={args.repeat} "
                  f"majorant_estime={majorant_estime:.4f} EUR "
                  f"budget_effectif={budget_effectif:.4f} EUR", file=sortie)
            return 0
        if args.repeat > 1 and majorant_estime > budget_effectif:
            non_executes = [
                c.id if args.repeat == 1 else f"{c.id}#r{repetition}"
                for c in cas for repetition in range(1, args.repeat + 1)
            ]
            identite_refus = {
                "image": {
                    "pipeline_digest": pipeline_digest(),
                    "prompts_digest": prompts_digest(),
                    "model_ids": dict(TIERS),
                    "normalize_version": normalize_version,
                    "plancher_digest": charge_plancher.digest,
                },
                "scope": {
                    "profile": args.profile,
                    "quick": args.quick,
                    "repeat": args.repeat,
                    "case_ids": [c.id for c in cas],
                    "suites": sorted({c.suite for c in cas}),
                    "variants": {c.id: variante_du_cas(c, args.variant) for c in cas},
                    "references_digest": references_digest,
                },
                "documents": {},
                "cache_namespace_digests": {},
                "preflight_refused": True,
            }
            identite_refus["run_digest"] = empreinte_canonique(identite_refus)
            rapport_refus = construire_rapport(
                [], cas, cases_dir=args.cases_dir, profile=args.profile,
                max_cost_eur=budget_effectif, complete=False,
                stop_reason="refus de budget avant le premier appel",
                non_executes=non_executes, cost_eur_engaged=0.0,
                snapshot=snapshot, run_identity=identite_refus, repeat=args.repeat,
                plancher=charge_plancher, producer=args.producer, preflight=preflight,
                decisions_orchestrateur=decisions_orchestrateur)
            ecrire_rapports(rapport_refus, output_json, output_markdown)
            print("refus de budget avant le premier appel : "
                  f"configured_budget_eur={budget_effectif:.4f} "
                  "accrued_cost_eur=0.0000 "
                  f"refused_cost_eur={majorant_estime:.4f} "
                  f"({executions_payantes} exécutions payantes × majorant par requête) — "
                  "rouge chiffré, rapport écrit, acquis conservés, manifest non modifié",
                  file=sys.stderr)
            return 4
        # 5. Le corpus, puis le document visé par `--gate`.
        with ExitStack() as pile:
            if all(c.suite == "parsing" for c in cas):
                ctx = construire_contexte_parsing(settings, args.data_dir)
            else:
                # `--repeat` désarme le cache : trois répétitions servies du cache ne mesureraient
                # ni la stabilité ni le coût (story 4.2b).
                ctx = construire_contexte(settings, args.data_dir, regate=args.gate, pile=pile,
                                          cache_dir=None if args.repeat > 1 else cache_dir,
                                          campaign_budget_eur=budget_effectif)
            run_identity = identite_run(
                cas, ctx, profile=args.profile, quick=args.quick, variant=args.variant,
                references_digest=references_digest,
                plancher_digest=charge_plancher.digest, repeat=args.repeat)
            # Le client (donc un pool de connexions TLS) est construit par `construire_contexte`.
            # Le refus « document non servi », l'exécution et la fermeture tiennent dans **un seul**
            # `asyncio.run` : une fermeture sur une autre boucle que celle qui a servi le client
            # lève `Event loop is closed` et fait sortir en code 3 un run par ailleurs vert, sans
            # écrire le gate. Voir `_executer_puis_fermer`.
            resultats = asyncio.run(_executer_puis_fermer(
                cas, ctx, gate=args.gate, max_cost_eur=budget_effectif, sortie=sortie,
                variant=args.variant, repeat=args.repeat))
    except RefusDeTourner as exc:
        print(f"refus : {exc}", file=sys.stderr)
        return 2
    except IncidentTechnique as exc:
        # AD-16 : l'échec est terminal et **dit**. Le manifest n'a pas été touché.
        # Story 4.2b : le rapport partiel est écrit sur **tout** incident — budget ou non — dès
        # qu'un lot existait ; les exécutions manquantes restent rouges au dénominateur.
        if cas:
            try:
                if snapshot is None:
                    raise IncidentTechnique("snapshot des cas absent au moment du rapport partiel")
                rapport = construire_rapport(
                    exc.resultats, cas, cases_dir=args.cases_dir, profile=args.profile,
                    max_cost_eur=budget_effectif, complete=False, stop_reason=str(exc),
                    non_executes=exc.non_executes, cost_eur_engaged=exc.cost_eur_engaged,
                    snapshot=snapshot, run_identity=run_identity, repeat=args.repeat,
                    plancher=charge_plancher, producer=args.producer, preflight=preflight,
                    stop_http=exc.http, stop_latency_ms=exc.latency_ms_engaged,
                    decisions_orchestrateur=decisions_orchestrateur)
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
        resume(resultats, max_cost_eur=budget_effectif, sortie=sortie)
        if snapshot is None:
            raise IncidentTechnique("snapshot des cas absent au moment du rapport")
        rapport = construire_rapport(resultats, cas, cases_dir=args.cases_dir, profile=args.profile,
                                     max_cost_eur=budget_effectif, complete=True, snapshot=snapshot,
                                     run_identity=run_identity, repeat=args.repeat,
                                     plancher=charge_plancher, producer=args.producer,
                                     preflight=preflight,
                                     decisions_orchestrateur=decisions_orchestrateur)
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
            # Story 4.2b : `evals_ok` est fondé sur les décisions chiffrées — un booléen seul
            # s'écrase, une décision porte son plancher, son n et son run_digest.
            decisions = construire_decisions(
                resultats, cas, plancher=charge_plancher, repeat=args.repeat,
                run_digest=str(run_identity.get("run_digest", "")) if run_identity else "",
                producer=args.producer, decisions_orchestrateur=decisions_orchestrateur)
            # HIGH 1 : une liste de décisions vide serait un vert par vacuité (`all([])` est vrai).
            evals_ok = tous_ok and bool(decisions) and all(d.status == "green" for d in decisions)
            gate = construire_gate(entry, ctx, profil=args.profile, cas=cas,
                                   cases_dir=args.cases_dir, evals_ok=evals_ok, snapshot=snapshot,
                                   decisions=decisions,
                                   run_digest=(str(run_identity.get("run_digest", ""))
                                               if run_identity else None))
            gate_ecrit = ecrire_gate(args.data_dir / MANIFEST, args.gate, gate)
        except RefusDeTourner as exc:
            print(f"refus : {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 — un gate cassé n'est jamais un verdict d'éval
            print(f"incident de gate : {type(exc).__name__}: {exc} — manifest non modifié",
                  file=sys.stderr)
            return 3
        if gate_ecrit:
            print(f"gate écrit sur {args.gate} : profile={gate.profile} evals_ok={gate.evals_ok} "
                  f"cases={gate.cases} countersigned={gate.countersigned} "
                  f"cases_hash={gate.cases_hash[:12]}… date={gate.date}", file=sortie)
        if gate_ecrit and not gate.countersigned:
            # AD-14 définit `vertical` par une relecture humaine. Écrire un gate dont les cas ne
            # sont pas contresignés est permis — sans quoi aucun gate ne pourrait exister avant la
            # contresignature — mais jamais muet : l'accueil dira la réserve, et ceci la dit à qui
            # lance le run (revue Codex 1.10 tour 2, B2).
            manquants = ", ".join(c.id for c in cas if c.truth.countersigned_by is None)
            print(f"ATTENTION : gate `{gate.profile}` écrit sur des cas non contresignés "
                  f"({manquants}) — l'accueil n'écrira pas « relus à la main » tant que "
                  "`truth.countersigned_by` n'est pas rempli, et le gate devra être relancé",
                  file=sys.stderr)
    gate_ok = not args.gate or (gate_ecrit and gate.evals_ok)
    return 0 if tous_ok and gate_ok else 1


if __name__ == "__main__":  # pragma: no cover — point d'entrée
    raise SystemExit(main())
