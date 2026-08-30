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
| 1 | le run ne rend pas un vert : mauvais label, attente inassouvie, décision de plancher rouge,
publication FR41 en échec, ou rapport non publiable (`RapportInexploitable`) | gate écrit tel qu'il a
été mesuré (`evals_ok: false`), **sauf** si la publication a échoué : le gate appartient alors au
même lot atomique que les surfaces, donc rien n'est publié **ni** écrit (story 4.5, B7) — c'est aussi
le cas d'un manifest qu'on ne peut pas écrire, qui était un refus 2 tant qu'il avait son écriture à
lui |
| 2 | refus de tourner : pas de clé, profil ou suite non livrés, cas invalide, document non servi | **non modifié** |
| 3 | incident technique : `Timeout`, `LlmUnavailable`, `BudgetExceeded`, plafond de run atteint | **non modifié** |
| 4 | refus de budget **avant le premier appel** (story 4.2b) : majorant estimé d'une campagne
payante > budget effectif (`min(--max-cost, LIVE_BUDGET_EUR)`) — rouge chiffré, jamais une
question | **non modifié** |

La ligne de partage entre 1 et 3 est celle d'AD-8 : un mauvais label est un **résultat** — le gate le
consigne et le document part en `gate_echoue` au prochain démarrage, ce que le gate est fait pour
faire. Un incident réseau ne dit rien du système mesuré, et le laisser retirer un document du service
serait une panne inventée. **Un rapport que la validation canonique refuse tombe du côté 1**, pas du
côté 3 (revue R1) : c'est un défaut de données, pas une panne, et l'étiqueter « incident technique »
enverrait chercher un bug de réseau là où une clé manque.

**Ce que 1 dit d'un gate `full` dont la publication a échoué** (story 4.5, revue A) : la publication
et le gate sont préparés ensemble puis basculés ensemble, si bien qu'un échec de préparation ne laisse
**ni** publication **ni** gate — le manifest est byte-identique et aucune surface n'a bougé. Le code
reste 1 : le verdict *a* été mesuré, et le run n'a pas tenu sa promesse de le publier. Un run muet qui
sortirait 0 rendrait « FR41 n'a pas publié » indiscernable de « aucun run n'a tourné ».

Ce qui n'est **pas** ici et qui est refusé plutôt que simulé : le holdout et les baselines
(stories 4.3 et 4.4). La publication des résultats et `GET /api/v1/evals/latest` sont livrés par la
story 4.5 : l'artefact vient du rapport du run, jamais d'une valeur fabriquée, et son absence se dit
`publie: false`.
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
from pydantic import (BaseModel, ConfigDict, Field, PrivateAttr, StrictBool, ValidationError,
                      field_validator, model_validator)

from server.app.config import EVALS_PUBLICATION_FILE, REPO_ROOT, Settings
from server.app.config import cle_absente as config_cle_absente
from server.app.corpus.dictionary import Dictionnaire, load_dictionary
from server.app.corpus.index import Index
from server.app.corpus.loader import SOURCE_FILES, load_corpus
from server.app.corpus.text import normalize, normalize_version
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.answer import Answer
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE
from server.app.domain.evals import (LABELS, PROFILS_LIVRES, GateProfile, Label, PublicationEvals,
                                     ReservesPubliees, SecondeLecturePubliee, Suite)
from server.app.domain.errors import HTTP_STATUS, ErrorCode, PipelineError, TruncatedRead
from server.app.domain.ingest import (STRUCTURE_CHECK, TREE_CHECK, Gate, GateContext,
                                      GateDecision, ManifestEntry, Report,
                                      lire_attestation_arbre, lire_attestation_structure)
from server.app.domain.profil import Profil
from server.app.domain.question import Faits, Turn
from server.app.domain.trace import Trace
from server.app.domain.verdict import KINDS_FONDATEURS
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.llm.pricing import estimate_run_majorant
from server.app.pipelines import sinistre as pipeline_sinistre
from server.app.pipelines.guide import VARIANT as VARIANT_GUIDE
from server.app.pipelines.guide import VARIANTS as VARIANTES_GUIDE
from server.app.pipelines.guide import repondre_guide
from server.evals.cache import PersistentResponseCache, empreinte_canonique, json_canonique
from server.evals.campaign import CampaignLedger, CampaignLedgerError
from server.evals.espace import REPERTOIRE_ESPACE as _REPERTOIRE_ESPACE
from server.app.corpus.racine import Lecture, lecture_pincee
from server.evals.espace import (EspaceIllisible, EspaceNonInstalle, EspacePublie,
                                 LotHorsEspace)
from server.evals.plancher import (ChargePlancher, PlancherInvalide, PreuveExterneVerifiee,
                                   charger_plancher, verifier_liaison_preuve)
from server.evals.publication import (DOCS_LATEST, ArchivePrecedenteIllisible,
                                      RapportInexploitable, construire_publication, digest_contenu,
                                      preparer_publication, rendre_publication_markdown,
                                      valider_rapport_publiable)
from server.evals.publication import nombre as nombre_publie
from server.evals.relecture import (RelectureInvalide, blocs_cles_du_rapport, charger_images,
                                    plan_de_relecture, statut_du_verdict, valider_verdict)
from server.evals.revision import ARBRE_NON_VERIFIABLE as _ARBRE_NON_VERIFIABLE
from server.evals.revision import SORTIES_DU_RUN as _SORTIES_DU_RUN
from server.evals.revision import est_revision, revision_executee, sorties_du_run

CASES_DIR = Path(__file__).resolve().parent / "cases"
REFERENCE_DIR = Path(__file__).resolve().parent / "reference"
DATA_DIR = REPO_ROOT / "data"
MANIFEST = "manifest.json"
# Story 4.2b : les artefacts de **protocole** rangés sous `reference/` — le plancher pré-enregistré
# (chargé et figé par `server/evals/plancher.py`, digest dans l'identité de run) et l'allowlist
# juridique de la garde anti-rustine (`tests/test_anti_rustine.py`). Ce ne sont pas des compagnons
# de cas : `charger_references` les ignore, leurs autorités et leurs digests sont ailleurs.
PROTOCOLE_FILES = frozenset({
    "plancher.yaml", "allowlist-juridique.yaml", "floor-4.2a.yaml",
    "trusted-automation-plancher.yaml",
})

# AD-14, mot pour mot : « labels fixes ». La définition vit dans `server/app/domain/evals.py` depuis
# la revue R1 : la validation canonique de la publication doit exiger **les sept**, et elle ne peut
# pas importer ce module (c'est l'inverse qui a lieu). Les noms d'usage restent `run.LABELS` et
# `run.Label` — une seule définition, deux endroits d'où la lire. `Suite` l'a rejointe au tour
# correctif 1/3, pour la même raison : la validation canonique doit exiger le vocabulaire littéral
# des suites, et elle ne peut pas lire ce module.

# `GateProfile` et `PROFILS_LIVRES` vivent dans `server/app/domain/evals.py` depuis le tour
# correctif 2/3 (revue B5), pour la même raison que `LABELS` et `SUITES` : la validation canonique de
# la publication doit fermer `profile` sur ce vocabulaire, et elle ne peut pas importer ce module.
# Les noms d'usage restent `run.GateProfile` et `run.PROFILS_LIVRES`.

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
# La variante qu'une suite tourne quand `--variant` n'est pas donné. Elle est **lue sur le pipeline**
# (`VARIANT`), jamais recopiée : deux runs ne sont comparables qu'à variante égale, et le harness doit
# mesurer ce que la production sert. Le parsing n'emprunte aucun pipeline — son unique variante est
# le chemin local.
DEFAUT_PAR_SUITE: dict[str, str] = {
    "guide": VARIANT_GUIDE,
    "sinistre": pipeline_sinistre.VARIANT,
    "parsing": "local",
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
    # Prédicat générique du témoin sinistre : au moins une claim affichée cite une garantie ou une
    # exclusion dont le typage est confirmé, et son applicabilité a été calculée. Aucun `block_id`
    # particulier n'est nécessaire pour exiger une preuve décisionnelle.
    decision_claim: StrictBool | None = None
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
        elif self.expected.decision_claim is not None:
            raise ValueError("expected.decision_claim n'a de sens que dans la suite `sinistre`")
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
                    or self.expected.complete is not None or self.expected.decision_claim is not None:
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
    # Sans défaut, délibérément (revue 4.2d) : un résultat dit quelle variante l'a produit, et aucune
    # valeur n'est plus « celle qu'on peut supposer ». Les trois sites de construction la passent
    # déjà — `variant="local"` pour le parsing, `trace.variant` pour les deux pipelines.
    variant: str
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
    # Story 4.5 : le prédicat décisionnel de `juger` (4.2a), **conservé** au lieu d'être jeté après
    # l'écart. C'est ce qui rend mesurable la stabilité de la claim décisionnelle entre répétitions
    # (`stabilite_claim_decisionnelle`) : sans lui, la seule chose qu'un run savait dire était
    # « le cas attendait un prédicat et ne l'a pas eu », jamais « les trois répétitions ne sont pas
    # d'accord entre elles ». `False` pour la suite `parsing` et pour une lecture tronquée : aucune
    # claim n'y survit.
    decision_claim: bool = False
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

    Story 4.5 : chaque preuve porte en plus `kind_confirmed` — le typage du bloc cité a-t-il été posé
    à la main ou vérifié par le modèle (AD-6, AD-8), ou n'est-ce qu'une heuristique d'ingestion ? Le
    champ **n'entre pas** dans la signature de stabilité, dont le numérateur est figé au plancher
    importé (`{doc_id, block_id, kind, quote_hash}`) : `_signature_stabilite` continue de ne lire que
    ces quatre-là. Il alimente un témoin **distinct**, `typage_confirme_rate` — c'est la seule façon
    d'ajouter une exigence sans modifier le sens d'une mesure déjà pré-enregistrée.
    """
    preuves: list[dict[str, Any]] = []
    for claim in answer.claims:
        for q in claim.quotes:
            bloc = _bloc(index, q.block_id)
            start = getattr(q, "start", None)
            end = getattr(q, "end", None)
            if bloc is None or start is None or end is None:
                preuves.append({"doc_id": None, "block_id": q.block_id, "kind": None,
                                "quote_hash": None, "kind_confirmed": False})
                continue
            preuves.append({
                "doc_id": index.doc_of(q.block_id),
                "block_id": q.block_id,
                "kind": bloc.kind,
                "quote_hash": quote_hash(bloc.text_norm[start:end]),
                "kind_confirmed": bool(bloc.kind_confirmed),
            })
    return sorted(preuves, key=lambda p: (p["doc_id"] or "", p["block_id"], p["quote_hash"] or ""))


def predicat_decisionnel(answer: Answer, index: Index) -> bool:
    """Au moins une claim affichée est-elle **décisionnelle** ? (prédicat 4.2a)

    « Décisionnelle » : la claim est retrouvée, pertinente, son applicabilité a été calculée (`oui`,
    `non` ou `humain`), et elle cite au moins un bloc d'un kind fondateur dont le typage est
    **confirmé**. C'est le prédicat que le témoin `decision_claim_rate` chiffre.

    Il vivait à l'intérieur de `juger`, calculé puis jeté une fois l'écart écrit. L'extraire ne change
    rien à ce que `juger` décide — la fonction l'appelle ici — mais le rend disponible au `Resultat`,
    donc à l'agrégat de stabilité du prédicat que l'AC 5 exige (`stabilite_claim_decisionnelle`). Une
    valeur qu'on recalculerait dans deux endroits finirait par diverger dans un seul.
    """
    return any(
        claim.status.retrouvee is True
        and claim.status.pertinente is True
        and claim.status.applicable in {"oui", "non", "humain"}
        and any(
            (bloc := _bloc(index, quote.block_id)) is not None
            and bloc.kind in KINDS_FONDATEURS
            and bloc.kind_confirmed
            for quote in claim.quotes
        )
        for claim in answer.claims
    )


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
    decisionnelle = predicat_decisionnel(answer, index)
    if cas.expected.decision_claim is not None and decisionnelle is not cas.expected.decision_claim:
        ecarts.append(
            "claim décisionnelle confirmée avec applicabilité calculée="
            f"{decisionnelle} (attendu {cas.expected.decision_claim})")

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
    elif answer.lecture_partielle is not None:
        # Story 4.2f : une lecture bornée sans claim survivante est désormais un **200 typé**, là où
        # elle levait `TruncatedRead`. Le harness gardait déjà ce cas en `claim_non_soutenu` sur la
        # branche d'exception ; il le garde ici, à la même précédence — avant `faux_refus`, qu'un
        # `found=False` déclencherait sinon. Un 200 partiel n'est jamais `bonne_reponse` par défaut,
        # et deux campagnes ne restent comparables que si le vocabulaire du harness ne change pas
        # avec le code HTTP.
        lp = answer.lecture_partielle
        ecarts.append(f"lecture partielle : {lp.nodes_read} nœud(s) lu(s), "
                      f"{lp.blocks_read} bloc(s) transmis, aucune affirmation retenue")
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


# --- la révision réellement exécutée (story 4.5, revue B1) -----------------------------------------
#
# La définition vit dans `server/evals/revision.py` depuis le tour correctif 2/3 : le **classement**
# du plancher doit opposer la révision candidate au checkout réellement exécuté, et `plancher.py` ne
# peut pas lire ce module — c'est ce module qui l'importe. Une seconde recette côté plancher aurait
# fait deux définitions de « la révision qu'on exécute », donc aucune. Les noms d'usage
# (`run.revision_executee`, `run.SORTIES_DU_RUN`, `run.sorties_du_run`) sont conservés ici.
_est_revision = est_revision
ARBRE_NON_VERIFIABLE = _ARBRE_NON_VERIFIABLE
SORTIES_DU_RUN = _SORTIES_DU_RUN


def _source_du_document(data_dir: Path, doc_id: str) -> str | None:
    """Le nom de la source qui fait foi pour ce document, ou `None` si aucune n'est présente.

    La règle est celle du loader, rejouée à l'identique : la **première** source présente dans
    `SOURCE_FILES` fait foi (`api/etat._pdf_sources` la rejoue déjà de la même façon). C'est
    l'**unique** endroit où le runner décide ce qu'un document est — les deux témoins de structure
    s'y branchent, et aucun ne connaît de `doc_id`.
    """
    for nom in SOURCE_FILES:
        chemin = data_dir / doc_id / nom
        try:
            if chemin.is_file():
                return nom
        except OSError as exc:
            # `Path.is_file()` avale l'`OSError` et rend `False` : un document dont la source
            # **existe** mais est inatteignable (droits, montage) disparaissait alors des deux
            # dénominateurs de structure, sans un mot. Ce n'est pas « pas de source », c'est « je ne
            # sais pas » — et un témoin qui ne sait pas ne retire personne de son dénominateur
            # (revue B3/B6, second chemin frère).
            raise RefusDeTourner(
                f"source de {doc_id!r} inatteignable ({nom} : {type(exc).__name__}) — impossible "
                "de dire ce que ce document est, donc impossible de le compter ou non") from exc
    return None


def image_du_run(plancher_digest: str) -> dict[str, Any]:
    """L'identité d'image du run courant — **les cinq champs** qu'`identite_run` publie.

    Une seule construction, partagée par l'identité de run et par la confrontation des preuves
    externes : deux listes séparées auraient divergé, et c'est exactement ce qui s'est produit —
    l'appelant en opposait trois, le rapport en portait cinq, et les deux manquants n'étaient
    comparés nulle part.
    """
    return {
        "pipeline_digest": pipeline_digest(),
        "prompts_digest": prompts_digest(),
        "model_ids": dict(TIERS),
        "normalize_version": normalize_version,
        "plancher_digest": plancher_digest,
    }


def document_parse_depuis_un_pdf(data_dir: Path, doc_id: str) -> bool:
    """Ce document est-il **réellement parsé**, c'est-à-dire ingéré depuis un PDF ?

    Un `source.js` — la copie du site — n'a rien à prouver côté extraction : son texte n'est pas
    extrait d'une image de page, il est déjà du texte.

    Aucune branche par document, aucun slug : c'est la présence d'un artefact qui décide.
    """
    return _source_du_document(data_dir, doc_id) == "source.pdf"


def suites_du_gate(settings: Settings, doc_id: str, profil: str, *,
                   cases_dir: Path = CASES_DIR) -> tuple[str, ...]:
    """Les suites qu'un `--gate {doc_id} --profile {profil}` exécute (story 4.5).

    Sous `vertical`, c'est la seule suite qui sert le document (D5) — le contrat historique n'a pas
    changé. Sous `full`, la **suite `parsing` du même document** s'y ajoute quand elle existe : c'est
    ce que l'AC 1 demande, et c'est la condition pour que `parsing_ok_rate` puisse rougir. Un profil
    qui promet la politique complète en excluant la seule preuve d'extraction du document mesurerait
    le raisonnement sur un texte dont personne n'a vérifié qu'il est celui du contrat.

    Conséquence assumée et écrite dans la passation : le `cases_hash` d'un gate `full` n'est plus
    celui d'un gate `vertical` du même document. Deux profils, deux périmètres, deux hashes — c'est
    exactement ce que `cases_hash` existe pour dire.

    Cette fonction est l'**autorité commune** de `main()` et du miroir de `tests/test_digests.py` :
    deux calculs séparés du périmètre d'un gate divergeraient au premier profil ajouté.
    """
    suites = [suite_du_document(settings, doc_id, cases_dir=cases_dir)]
    if profil == "full":
        dossier_parsing = cases_dir / "parsing" / doc_id
        if dossier_parsing.is_dir():
            _dans_cases(cases_dir, dossier_parsing, objet="suite parsing du document")
            suites.append(f"parsing/{doc_id}")
    return tuple(suites)


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
    variante = variante_demandee or DEFAUT_PAR_SUITE.get(suite, "")
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
                 plancher_digest: str | None = None, repeat: int = 1,
                 campaign_id: str | None = None, series_kind: str | None = None,
                 series_id: str | None = None,
                 candidate_revision: str | None = None) -> dict[str, Any]:
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
        # Story 4.5 (revue B1) : la **révision produit mesurée** fait partie de l'identité du run,
        # donc de `run_digest`. Sans elle, deux runs de deux commits différents pouvaient porter le
        # même digest — et une preuve trusted pouvait s'y raccrocher indifféremment.
        "candidate_revision": candidate_revision,
        # Les cinq champs de `image_du_run`, écrits ici avec les digests **du contexte** (les
        # doubles de test en posent d'autres). Story 4.2b : le protocole fait partie de l'identité —
        # deux runs ne se comparent qu'à plancher égal, comme à corpus égal.
        "image": {
            "pipeline_digest": ctx.pipeline_digest_hex,
            "prompts_digest": ctx.prompts_digest_hex,
            "model_ids": dict(TIERS),
            "normalize_version": normalize_version,
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
            "campaign_id": campaign_id,
            "series_kind": series_kind,
            "series_id": series_id,
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
                                                 variant=variant or DEFAUT_PAR_SUITE["guide"],
                                                 **commun)
        else:
            assert cas.faits is not None  # garanti par `Cas._coherence`
            answer, trace = await pipeline_sinistre.run(
                doc_id, cas.question, cas.faits,
                dictionnaire=ctx.dictionnaires.get(doc_id),
                variant=variant or DEFAUT_PAR_SUITE["sinistre"], **commun)
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
    # La même source que partout ailleurs (`DEFAUT_PAR_SUITE`, via `variante_du_cas`) : une table
    # recopiée ici vieillirait seule, et cette garde-là refuse **après** les appels facturés — elle
    # jetterait alors la namespace de cache d'une trace pourtant conforme. `variante_du_cas` sait
    # aussi qu'une suite peut porter un sous-dossier documentaire (`sinistre/axa-…`).
    variante_attendue = variant or variante_du_cas(cas, None)
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
                cout_total = cumul + cout_engage
                if isinstance(exc, TruncatedRead):
                    trace = exc.trace
                    resultat = Resultat(
                        id=c.id, suite=c.suite, label="claim_non_soutenu",
                        variant=(trace.variant if trace is not None else variante),
                        ecarts=[f"lecture tronquée : {exc.message}"], cost_eur=cout_engage,
                        cost_eur_original=(_cout_original(trace) if trace is not None else 0.0),
                        ms=int((time.monotonic() - depart) * 1000), found=False,
                        expected_found=c.expected.found,
                        expected_block_ids=list(c.expected.block_ids),
                        expected_fiche_ids=list(c.expected.fiche_ids), repetition=repetition,
                        doc_id=doc_id, complete=False,
                        reason_kind="lecture_tronquee",
                        opened_block_ids=(_blocs_ouverts(trace) if trace is not None else []),
                        steps=(_etapes(trace) if trace is not None else []),
                        http=HTTP_STATUS[exc.code])
                    resultats.append(resultat)
                    cumul = cout_total
                    _ligne(resultat, sortie, repeat=repeat)
                    continue
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
                            # Story 4.2f : le même vocabulaire des deux côtés de la bascule. La
                            # branche `TruncatedRead` ci-dessous pose `lecture_tronquee` sur un 503 ;
                            # le 200 typé qui la remplace doit poser le même mot, sans quoi une
                            # campagne d'avant et une campagne d'après cesseraient d'être comparables
                            # sur le seul motif que le code HTTP a changé.
                            reason_kind=("lecture_tronquee" if answer.lecture_partielle is not None
                                         else (answer.reason.kind if answer.reason is not None
                                               else None)),
                            opened_block_ids=_blocs_ouverts(trace),
                            steps=_etapes(trace),
                            proofs=_preuves(answer, ctx.index),
                            decision_claim=predicat_decisionnel(answer, ctx.index))
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
        numeros = {r.repetition for r in reps}
        attendus = set(range(1, repeat + 1))
        if numeros != attendus:
            raisons.append(
                f"numéros de répétition invalides : {sorted(numeros)}, attendus {sorted(attendus)}")
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


def agreger_stabilite_claim(resultats: list[Resultat], cas: list[Cas], *, repeat: int,
                            non_executes: list[str] | None = None) -> tuple[float, int] | None:
    """La stabilité du **prédicat décisionnel** sur les cas sinistre, ou `None` s'il n'y en a pas.

    Pourquoi un agrégat **à côté** de `_signature_stabilite`, et pas un champ de plus dedans : le
    numérateur de `stabilite_sinistre` est écrit dans le plancher pré-enregistré — « la meme preuve
    {doc_id, block_id, kind, quote_hash} et le meme verdict admissible ». Y glisser le prédicat
    changerait le sens d'un témoin importé sans diminution, c'est-à-dire un changement de protocole
    déguisé en amélioration : deux campagnes qui portent le même `plancher_digest` cesseraient de
    mesurer la même chose. L'exigence nouvelle est donc un témoin nouveau, dont le rouge se lit
    séparément (`stabilite_claim_decisionnelle`).

    Un cas est stable ssi ses `repeat` répétitions sont **toutes** là et rendent le même prédicat.
    Une répétition manquante est une interruption : rouge, jamais retirée du dénominateur.
    """
    manquantes: dict[str, int] = {}
    for execution_id in non_executes or []:
        base = _cas_de_lexecution(execution_id)
        manquantes[base] = manquantes.get(base, 0) + 1
    ids = sorted({c.id for c in cas if c.suite.startswith("sinistre")}
                 | {r.id for r in resultats if r.suite.startswith("sinistre")})
    if not ids:
        return None
    stables = 0
    for case_id in ids:
        reps = [r for r in resultats if r.id == case_id]
        if len(reps) < repeat or {r.repetition for r in reps} != set(range(1, repeat + 1)):
            continue
        if manquantes.get(case_id):
            continue
        if len({r.decision_claim for r in reps}) == 1:
            stables += 1
    return round(stables / len(ids), 4), len(ids)


def _temoin_applicable(temoin: Any, cas: list[Cas], *, exigences_full: bool = False) -> bool:
    """Le témoin porte-t-il sur ce lot, même si sa mesure appartient à l'orchestrateur ?

    `exigences_full` dit si **ce run** est le gate qui revendique la politique complète
    (`--gate {doc_id} --profile full`). Les témoins que le plancher marque `arme_par: gate_full` n'y
    sont applicables qu'alors : un diagnostic `--profile full` sans gate — ce que la CI lance à
    chaque PR — et un gate `vertical` ne promettent pas cette politique, et les leur opposer ferait
    rougir une mesure qui ne la revendique pas.
    """
    if getattr(temoin, "arme_par", "toujours") == "gate_full" and not exigences_full:
        return False
    if temoin.scope in ("repo", "run"):
        return True
    if temoin.scope == "suite":
        return any(c.suite.split("/", 1)[0] == temoin.pipeline for c in cas)
    if temoin.scope == "case":
        return temoin.case_id is not None and any(c.id == temoin.case_id for c in cas)
    if temoin.scope == "live_http":
        return any(c.suite.split("/", 1)[0] == temoin.pipeline for c in cas)
    return False


def charger_decisions_orchestrateur(path: Path, *, plancher: ChargePlancher,
                                    candidate_revision: str,
                                    report_path: Path,
                                    image_courante: dict[str, Any] | None = None,
                                    ) -> tuple[list[GateDecision], PreuveExterneVerifiee]:
    """Charge une preuve externe mesurée par l'orchestrateur, sans lui faire déclarer son statut.

    Le fichier ne fournit que les mesures : seuil, producteur et statut sont recalculés depuis le
    plancher versionné. Ainsi A16, `decision_claim` (le prédicat est fusionné dans `juger`, mais la
    décision chiffrée reste celle de la série orchestrateur), les tests hors ligne et les deux gardes
    de la story 4.5 (anti-rustine, métamorphique) peuvent rejoindre le gate sans être simulés par le
    runner ni auto-certifiés par le JSON d'entrée.

    Story 4.5 (M2), durcie par la revue B1 : avant de lire la moindre mesure, la preuve est **liée**
    à ce candidat par `plancher.verifier_liaison_preuve`, qui recoupe sept choses — les cinq clés
    racine exactes, le protocole (`plancher_digest`), la révision candidate, les octets du rapport
    (`report_digest`), l'égalité du `run_digest` racine avec celui du rapport référencé **et** de
    chaque mesure, le **recalcul** de ce `run_digest` depuis l'identité du rapport (un digest cru sur
    parole n'est qu'une chaîne), et enfin l'**image** mesurée — c'est à quoi sert `image_courante` :
    `pipeline_digest`, `prompts_digest` et `model_ids` du run courant, deux runs ne se comparant qu'à
    code, prompts et modèles égaux.

    Une preuve d'une autre révision, d'une autre image, ou dont le rapport a bougé, est refusée
    **avant toute décision** (code 2), et aucun gate n'est écrit. C'est la différence entre « une
    preuve existe » et « cette preuve mesure ceci ».

    Rend `(décisions, preuve vérifiée)` (revue B5, tour correctif 2/3). La `PreuveExterneVerifiee`
    n'est pas un ornement : c'est le **seul** ancrage qui autorisera ensuite
    `valider_rapport_publiable` à publier une décision portant l'empreinte d'un autre run. Sans
    elle, la validation devait croire ce que le rapport déclarait sur lui-même. La faire suivre
    d'ici jusqu'aux quatre surfaces est précisément le travail que le tour précédent avait écarté.
    """
    try:
        brut = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RefusDeTourner(
            f"preuve orchestrateur {path} illisible ({type(exc).__name__})") from exc
    try:
        octets_rapport: bytes | None = report_path.read_bytes()
    except OSError:
        octets_rapport = None
    try:
        preuve = verifier_liaison_preuve(brut, plancher_digest=plancher.digest,
                                         candidate_revision=candidate_revision,
                                         report_bytes=octets_rapport,
                                         image_courante=image_courante)
    except PlancherInvalide as exc:
        raise RefusDeTourner(str(exc)) from exc
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
    return decisions, preuve


def _blocs_attendus_ouverts(r: Resultat) -> bool:
    """« Blocs attendus ∈ blocs lus » — la suite `parsing` est locale : aucun bloc n'y est *lu*."""
    return r.suite == "parsing" or set(r.expected_block_ids) <= set(r.opened_block_ids)


def _typage_confirme(r: Resultat) -> bool:
    """Chaque preuve citant une **clause fondatrice** porte-t-elle un typage confirmé (AD-6, AD-8) ?

    L'univers est `KINDS_FONDATEURS` — `garantie`, `exclusion` —, exactement celui du prédicat
    décisionnel de `juger`, et c'est le typage qu'AD-6 exige : celui dont dépend un verdict.

    Le témoin regardait **toutes** les preuves, y compris les paragraphes du guide. Or aucun bloc du
    corpus servi n'est `kind_confirmed` hors clauses contractuelles (sonde : 0 sur 506) : un gate
    `full` du guide était donc **structurellement rouge**, quelle que soit la qualité du candidat —
    un témoin qu'aucun travail ne peut verdir n'est pas une exigence, c'est un mur. Le guide n'a
    aucune clause juridique à typer ; ce qu'il doit prouver, il le prouve par
    `citations_retrouvees_rate` et `blocs_attendus_ouverts_rate`.

    Une exécution sans preuve fondatrice est vraie par vacuité, et c'est le bon résultat : elle
    n'affirme rien qu'un typage juridique devrait soutenir.
    """
    return all(bool(p.get("kind_confirmed")) for p in r.proofs
               if p.get("kind") in KINDS_FONDATEURS)


def _sans_5xx_technique(r: Resultat) -> bool:
    """AD-11 : toute réponse du pipeline est un 200. Un `http >= 500` est un incident, pas un verdict.

    Il en existait deux sortes indiscernables dans les agrégats : le refus de budget (rouge chiffré,
    voulu) et la panne technique — dont la branche `TruncatedRead`, qui produit un `Resultat` avec un
    503 tout en comptant comme une exécution terminée. Le témoin les sépare : ce qui porte un 5xx
    n'est pas une mesure du système, et un gate qui l'ignore compte une panne comme une réponse.
    """
    return r.http is None or r.http < 500


def construire_decisions(resultats: list[Resultat], cas: list[Cas], *, plancher: ChargePlancher,
                         repeat: int, run_digest: str, producer: str,
                         non_executes: list[str] | None = None,
                         decisions_orchestrateur: list[GateDecision] | None = None,
                         exigences_full: bool = False,
                         structure: tuple[int, int] | None = None,
                         arbre: tuple[int, int] | None = None,
                         ) -> list[GateDecision]:
    """Les décisions chiffrées du run contre le plancher pré-enregistré (story 4.2b).

    Seules les métriques que **ce runner** mesure produisent une décision (`mesure_par:
    eval_runner`) : les témoins de l'orchestrateur (tests hors ligne, A16 HTTP, la série chiffrée
    `decision_claim` — le prédicat lui-même est fusionné dans `juger`, qui le juge à chaque
    répétition d'un cas `expected.decision_claim`) restent les siens — les dupliquer ici serait une
    seconde autorité sur la même mesure. Trois règles ferment le vert par vacuité (revue 4.2b, HIGH 1) :

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
              n=repeat, scope="run")
    # LOW 13 : le seuil d'`executions_completes` vit dans le plancher, jamais en dur ici.
    _decision("executions_completes",
              value=round((planifiees - manquantes) / planifiees, 4) if planifiees else 0.0,
              n=repeat, scope="run")
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

    # --- story 4.5 : les huit mesures du gate `full` ----------------------------------------------
    #
    # Elles ne sont calculées que lorsque les exigences de `full` sont armées — c'est-à-dire sous
    # `--gate {doc_id} --profile full`. Ailleurs, `_temoin_applicable` les écarte de toute façon ;
    # ne pas les calculer évite en plus de publier des chiffres qu'aucune décision ne lit.
    #
    # **Toutes** ont pour dénominateur les exécutions *planifiées* : une exécution manquante n'est
    # jamais retirée du calcul, sans quoi un run interrompu serait plus flatteur qu'un run complet
    # (règle d'incident du plancher).
    if exigences_full:
        parsing_planifiees = sum(1 for c in cas if c.suite == "parsing") * repeat
        if parsing_planifiees:
            parsing_ok = sum(1 for r in resultats if r.suite == "parsing" and r.ok)
            _decision("parsing_ok_rate", value=parsing_ok / parsing_planifiees, n=repeat,
                      scope="suite:parsing")
        if planifiees:
            for metric, predicat in (
                ("blocs_attendus_ouverts_rate", _blocs_attendus_ouverts),
                ("citations_retrouvees_rate", lambda r: r.label != "citation_introuvable"),
                ("zero_5xx_technique_rate", _sans_5xx_technique),
            ):
                tenues = sum(1 for r in resultats if predicat(r))
                _decision(metric, value=tenues / planifiees, n=repeat, scope="run")
        # Le typage juridique se mesure sur la **suite sinistre** : c'est la seule qui porte des
        # clauses fondatrices, et le dénominateur suit le numérateur (revue I1).
        sinistres_planifiees = sum(1 for c in cas if c.suite.startswith("sinistre")) * repeat
        if sinistres_planifiees:
            tenues = sum(1 for r in resultats
                         if r.suite.startswith("sinistre") and _typage_confirme(r))
            _decision("typage_confirme_rate", value=tenues / sinistres_planifiees, n=repeat,
                      scope="suite:sinistre")
        if structure is not None and structure[1]:
            _decision("structure_prouvee_rate", value=structure[0] / structure[1], n=repeat,
                      scope="suite:parsing")
        # Le pendant de `structure_prouvee_rate` pour les documents qui ne viennent pas d'un PDF
        # (revue B4, volet guide). Le partage est celui de `SOURCE_FILES`, pas un `doc_id` : les deux
        # dénominateurs sont complémentaires, et aucun périmètre ne se retrouve sans exigence de
        # structure. Un dénominateur nul n'émet rien — et la fermeture par vacuité rend alors le
        # témoin rouge, jamais neutre.
        if arbre is not None and arbre[1]:
            _decision("arbre_prouve_rate", value=arbre[0] / arbre[1], n=repeat,
                      scope="suite:guide")
        taux_claim = agreger_stabilite_claim(resultats, cas, repeat=repeat,
                                             non_executes=non_executes)
        if taux_claim is not None:
            _decision("stabilite_claim_decisionnelle", value=taux_claim[0], n=repeat,
                      scope="suite:sinistre")

    for decision in decisions_orchestrateur or []:
        temoin = plancher.plancher.temoin(decision.metric)
        if temoin is not None and _temoin_applicable(temoin, cas, exigences_full=exigences_full):
            decisions.append(decision)
    emises = {d.metric for d in decisions}
    for temoin in plancher.plancher.temoins:
        if (temoin.criticite != "bloquant" or temoin.metric in emises
                or not _temoin_applicable(temoin, cas, exigences_full=exigences_full)):
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
                       decisions_orchestrateur: list[GateDecision] | None = None,
                       exigences_full: bool = False,
                       structure: tuple[int, int] | None = None,
                       arbre: tuple[int, int] | None = None,
                       dictionary_validated: bool | None = None) -> dict[str, Any]:
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
    couts_observes = ([r.cost_eur for r in resultats]
                      + ([cout_interrompu] if cout_interrompu > 0 else []))
    couts = sorted(couts_observes + [0.0] * max(0, executions_planifiees - len(couts_observes)))
    latences_observees = ([r.ms for r in resultats]
                          + ([stop_latency_ms] if stop_latency_ms is not None else []))
    latences = sorted(latences_observees
                      + [0] * max(0, executions_planifiees - len(latences_observees)))
    repetitions_par_cas = {
        c.id: {r.repetition for r in resultats if r.id == c.id}
        for c in cas
    }

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
        "cases_completed": sum(
            1 for c in cas if repetitions_par_cas[c.id] == set(range(1, repeat + 1))),
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
            "average_cost_eur": round(cout_total / executions_planifiees, 4)
            if executions_planifiees else 0.0,
            "latency_p50_ms": int(statistics.median(latences)) if latences else 0,
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
    # Story 4.5 (P6) : les trois réserves entrent dans le **rapport**, donc dans le résumé que la CI
    # concatène — pas seulement dans la publication, que la CI ne produit pas (elle tourne sans
    # `--gate`). Les deux premières se dérivent des cas exécutés, exactement comme `construire_gate`
    # dérive `countersigned` ; la troisième vient du dictionnaire chargé et n'est renseignée que
    # lorsque l'appelant l'a construit — un refus de budget survient avant tout contexte.
    if dictionary_validated is not None:
        rapport["reserves"] = reserves_du_lot(cas, dictionary_validated=dictionary_validated
                                              ).model_dump(mode="json")
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
                                         decisions_orchestrateur=decisions_orchestrateur,
                                         exigences_full=exigences_full, structure=structure,
                                         arbre=arbre)
        rapport["plancher_digest"] = plancher.digest
        rapport["decisions"] = [d.model_dump(mode="json") for d in decisions]
        # **Les empreintes de run étrangères sont déclarées**, jamais implicites (revue B5, tour
        # correctif 1/3). Une décision venue d'une preuve trusted porte le `run_digest` du run que
        # cette preuve mesure, et non celui de ce run-ci. Sans cette liste, la validation canonique
        # n'avait aucun moyen de distinguer cette empreinte légitime d'une empreinte arbitraire.
        #
        # Ce que la liste établit et ce qu'elle n'établit pas (revue P5) : elle rend le rapport
        # **cohérent avec lui-même** — chaque empreinte étrangère est portée par au moins une
        # décision `producer="orchestrator"`, et réciproquement. La liaison cryptographique de la
        # preuve à ce candidat, elle, a déjà eu lieu **en amont**, dans
        # `charger_decisions_orchestrateur` → `verifier_liaison_preuve`, avant qu'aucune de ces
        # décisions n'existe : un lecteur de rapport ne peut ni la voir ni la refaire. Elle n'est
        # écrite que lorsqu'il y en a — un rapport sans preuve externe ne déclare pas une liste vide
        # qu'il faudrait ensuite interpréter.
        etrangeres = sorted({d.run_digest for d in decisions if d.run_digest != run_digest})
        if etrangeres:
            rapport["external_run_digests"] = etrangeres
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


def rendre_markdown(rapport: dict[str, Any],
                    publication: PublicationEvals | None = None, *,
                    preuve_externe: PreuveExterneVerifiee | None) -> str:
    """La table Markdown du run — **le fichier que la CI concatène dans `$GITHUB_STEP_SUMMARY`**.

    Story 4.5, correctif P6 du tour de revue précédent : **un seul renderer**. Le détail par cas
    reste ici — c'est le journal du run, pas l'artefact publié — mais tout ce que l'AC 4 compare
    d'une surface à l'autre (identité,
    chiffres, stabilité N/N, décisions, réserves, limites) est rendu par
    `publication.rendre_publication_markdown`, la **même** fonction qui écrit `docs/evals/latest.md`
    et `data/evals-latest.json`. Deux renderers auraient divergé au premier champ ajouté, et les
    tests n'auraient comparé que deux rendus synthétiques.

    `publication` est fournie par le gate quand il y en a un (elle porte alors `evals_ok`, la
    révision candidate et le protocole) ; sinon elle est **construite depuis ce rapport**, sans
    gate — ce que la CI produit à chaque PR. Les champs qu'un diagnostic n'établit pas restent
    absents, jamais fabriqués.
    """
    # **La même validation canonique que la publication**, appelée avant la première ligne rendue
    # (revue B5, cycle de récupération). Ce journal est l'une des quatre surfaces — c'est lui que la
    # CI concatène dans `$GITHUB_STEP_SUMMARY` —, et il lisait le rapport par une seconde lecture,
    # plus permissive : un `metrics.recall` nul y serait sorti en `TypeError` nue, ou pire, en
    # chiffre. Un seul contrôle, quatre surfaces, un seul refus dit.
    valider_rapport_publiable(rapport, preuve_externe=preuve_externe)
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
        f"| {_markdown_code(rapport['cases_hash'])} | {nombre_publie(m['recall'])} | "
        f"{nombre_publie(m['average_cost_eur'])} | "
        f"{m['latency_p50_ms']} | {nombre_publie(m['ne_tranche_pas_rate'])} |",
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
    journal = "\n".join(lignes).rstrip() + "\n"
    # L'artefact publié, rendu par **sa** fonction, appendu au journal du run : c'est ce que la CI
    # concatène, et c'est littéralement la même chaîne que `docs/evals/latest.md`.
    pub = publication if publication is not None else construire_publication(
        rapport, preuve_externe=preuve_externe)
    return journal + "\n---\n\n" + rendre_publication_markdown(
        pub, valeur=_markdown_value, code=_markdown_code)


def cibles_publiees_du_run(data_dir: Path, output_json: Path, output_markdown: Path, *,
                           gate: bool) -> list[Path]:
    """Le lot que ce run publiera — la question que le préflight de racine pose (story 4.5, N3).

    Hors gate, l'opération de production est le couple `(rapport JSON, table Markdown)`. Sous
    `--gate`, elle y ajoute le manifest, l'artefact servi et le rendu lisible : c'est le lot complet
    de l'AC, `data/manifest.json` compris, et il est connu **avant** la première mesure.
    L'archive de campagne, elle, vit **sous** `docs/evals/campagnes`, dont la couverture se déduit
    de celle de ce répertoire ; l'inclure nommément exigerait un horodatage qui n'existe pas encore.
    """
    cibles = [output_json, output_markdown]
    if gate:
        cibles += [data_dir / MANIFEST, data_dir / EVALS_PUBLICATION_FILE,
                   data_dir.parent.joinpath(*DOCS_LATEST)]
    return cibles


def espace_du_data_dir(data_dir: Path) -> EspacePublie:
    """L'espace de publication d'un `data/` — **l'autorité unique** du couple (racine, bundle).

    La racine est `data_dir.parent`, exactement comme `output_json`, `output_markdown` et le cache
    en dérivent déjà dans `main()`. Deux dérivations auraient fini par diverger, et un run pointé
    ailleurs aurait basculé le `data/` du dépôt.
    """
    return EspacePublie(data_dir.parent, data_dir)


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


def preparer_les_rapports(rapport: dict[str, Any], json_path: Path, markdown_path: Path, *,
                          preuve_externe: PreuveExterneVerifiee | None,
                          contenu_json: str | None = None) -> list[tuple[Path, str]]:
    """Le couple `(rapport JSON, table Markdown)` en **lot**, sans écrire un seul octet.

    Tour correctif 3/3. Le couple n'est plus basculé par lui-même sur le chemin du gate : il entre
    dans le **même** lot que la publication et le manifest, pour qu'il n'y ait qu'un seul point de
    commit dans toute l'opération de production. Le séparer en atome propre était exactement la
    frontière que le recheck a nommée — chaque atome était tout-ou-rien, l'opération ne l'était pas.

    `contenu_json` permet à l'appelant de fournir les octets **déjà sérialisés**, ceux-là mêmes dont
    il a tiré `gate.report_digest` : deux sérialisations du même rapport seraient identiques, mais
    « seraient » n'est pas « sont », et l'empreinte publiée doit désigner les octets publiés.
    """
    return [
        (json_path, contenu_json if contenu_json is not None else json_canonique(rapport) + "\n"),
        (markdown_path, rendre_markdown(rapport, preuve_externe=preuve_externe)),
    ]


def ecrire_rapports(rapport: dict[str, Any], json_path: Path, markdown_path: Path, *,
                    preuve_externe: PreuveExterneVerifiee | None, espace: EspacePublie) -> None:
    """Le rapport JSON **et** sa table Markdown, ou aucun des deux (revue B3, chemin frère).

    Deux `_ecrire_atomique` enchaînés laissaient, si le second échouait, un `eval-results.json` neuf
    à côté d'un `eval-results.md` périmé — et ce n'est pas un couple anodin : c'est l'empreinte du
    rapport qui alimente `gate.report_digest`, donc celle par laquelle un gate se réclame de son
    rapport. Un lot mêlé ici se propage jusque dans le gate.

    **Ce couple est un lot comme un autre** (story 4.5, B7) : il passe par le même unique pointeur
    atomique que la publication. Les deux rendus sont construits d'abord — c'est là que
    `RapportInexploitable` tombe, avant qu'aucune surface ne bouge — puis remis ensemble à
    `EspacePublie.basculer`, qui les publie en un seul `os.replace` ou pas du tout.

    C'est **l'opération de production entière** des chemins qui n'écrivent pas de gate : un run de
    diagnostic, un refus de budget, un rapport partiel d'incident. Un run de gate, lui, ne passe
    plus par ici : son opération comprend aussi la publication et le manifest, et elle n'a qu'un
    commit (tour correctif 3/3).
    """
    espace.basculer(preparer_les_rapports(rapport, json_path, markdown_path,
                                          preuve_externe=preuve_externe))


# --- les preuves de structure d'ingestion (lues par le gate `full`) --------------------------------
#
# Il y en a **deux**, parce que les deux chemins d'ingestion prouvent deux choses différentes, et
# elles se répartissent par la règle générique `SOURCE_FILES` du loader — jamais par un `doc_id` :
#
# - un document issu d'un PDF prouve sa **proposition de structure** (story 4.2c) : `structure.json`
#   déclaré, présent, concordant, et attesté par le rapport d'ingestion (`preuve_de_structure`) ;
# - un document qui n'en est pas issu — une copie de site — n'a pas de proposition de structure, mais
#   son ingestion émet le même contrôle déterministe d'arbre que l'autre (`invariants_arbre`). Ce
#   qu'il prouve, il le prouve par l'attestation de ce contrôle (`preuve_darbre`).
#
# Chacune alimente son propre témoin bloquant du plancher. Restreindre la première aux PDF sans
# donner la seconde au guide aurait laissé un périmètre entier **sans aucune** exigence de structure
# — un abaissement du plancher par omission, et le plancher ne s'abaisse jamais.

STRUCTURE_FILE = "structure.json"


def _rapport_dingestion(data_dir: Path, doc_id: str, lecture: Lecture) -> Report | None:
    """Le `report.json` de ce document, **rattaché à lui**, ou `None` — absent, illisible, étranger.

    Les trois cas se valent pour une preuve : rien n'atteste, donc rien n'est prouvé. Un rapport
    dont le `doc_id` diffère décrit un autre document, et ses checks ne disent rien de celui-ci.

    La lecture passe par le **repère pincé** de l'opération de gate (story 4.5, N1) : le manifest
    d'où viennent les empreintes attendues, la proposition de structure et ce rapport doivent venir
    de la même génération — une décision de gate composée de trois générations ne décrit aucun état.
    """
    try:
        lu = Report.model_validate_json(lecture.reel(data_dir / doc_id / "report.json").read_bytes())
    except (OSError, ValueError):
        return None
    return lu if lu.doc_id == doc_id else None


def _atteste(lu: Report, nom: str, lire: Any, attendu: tuple[str, ...]) -> bool:
    """Le rapport porte-t-il, pour le check `nom`, une attestation affirmative valant `attendu` ?

    Deux conditions, et la première est celle que la revue D a rétablie : **aucun check `nom` de
    niveau `bloquant`**. Le vérificateur qui refuse et l'ingestion qui atteste écrivent le même nom
    de check ; n'exiger qu'une attestation non bloquante laissait un rapport portant les deux — un
    refus **et** une acceptation — compter comme prouvé. Un chemin de production n'en émet qu'un à
    la fois, mais une preuve ne se lit pas en pariant sur ce qui a bien voulu être écrit.
    """
    if any(c.name == nom and c.level == "bloquant" for c in lu.checks):
        return False
    attestations = [lire(c.detail) for c in lu.checks if c.name == nom and c.level != "bloquant"]
    return attendu in [a for a in attestations if a is not None]


def preuve_darbre(data_dir: Path, ctx: Contexte, doc_ids: list[str], *,
                  lecture: Lecture | None = None) -> tuple[int, int]:
    """(documents non-PDF dont l'arbre est prouvé, documents non-PDF du lot) — jamais une valeur neutre.

    Story 4.5, revue B4 — **volet guide**. `structure_prouvee_rate` a été restreint aux documents
    issus d'un PDF, ce qui est juste : la story 4.2c ne s'applique qu'à eux, et l'exiger d'une copie
    de site rendait tout gate `full` du guide définitivement rouge. Mais restreindre sans remplacer
    laissait le guide **sans aucune** exigence de structure — un plancher abaissé par omission. Ce
    témoin est la preuve déterministe qui lui est applicable.

    Le dénominateur est l'exact **complément** de celui de `preuve_de_structure` : la règle
    `SOURCE_FILES` du loader (première source présente) partage le lot en deux, sans une branche par
    document. Un document dont aucune source n'est présente n'entre dans aucun des deux — il n'y a
    rien à lire pour décider ce qu'il est, et inventer un périmètre serait pire que de n'en compter
    aucun. Le loader, lui, **sert** ce document (alerte `source_absente`, jamais un refus) : le trou
    est donc réel, et c'est `main()` qui le ferme, en refusant un gate `full` sur un document sans
    source **avant tout appel** (revue C).

    « Prouvé » veut dire **trois** choses ensemble, et l'absence d'une seule suffit à ne pas compter :

    1. `report.json` est présent, **lisible** et **rattaché à ce document** (`doc_id` égal) ;
    2. il porte un check `invariants_arbre` **affirmatif** et **aucun** check `invariants_arbre`
       bloquant — un arbre refusé n'est pas un arbre prouvé, même si une acceptation traîne à côté ;
    3. son attestation nomme le `document_hash` **et** l'`ingest_fingerprint` de l'entrée du
       manifest — les octets exacts de l'arbre servi, et le code qui les a produits.

    La troisième condition est celle qui fait la différence entre une preuve et une déclaration : le
    corpus servi porte aujourd'hui `invariants_arbre: ok`, sans empreinte. Un `report.json` fabriqué
    à la main ne verdit donc rien, et une réingestion — ou un `document.json` qui bouge — détache
    l'attestation sans qu'aucune ligne de code n'ait à s'en apercevoir.
    """
    if lecture is None:
        with lecture_pincee(data_dir) as pincee:
            return preuve_darbre(data_dir, ctx, doc_ids, lecture=pincee)
    uniques = [doc_id for doc_id in sorted(set(doc_ids))
               if _source_du_document(data_dir, doc_id) not in (None, "source.pdf")]
    prouves = 0
    for doc_id in uniques:
        entry = ctx.index.corpus.manifest.get(doc_id)
        attendu = ((getattr(entry, "document_hash", "") or ""),
                   (getattr(entry, "ingest_fingerprint", "") or ""))
        if not all(attendu):
            continue
        lu = _rapport_dingestion(data_dir, doc_id, lecture)
        if lu is None:
            continue
        if _atteste(lu, TREE_CHECK, lire_attestation_arbre, attendu):
            prouves += 1
    return prouves, len(uniques)


def preuve_de_structure(data_dir: Path, ctx: Contexte, doc_ids: list[str], *,
                        lecture: Lecture | None = None) -> tuple[int, int]:
    """(documents PDF dont la structure est prouvée, documents PDF du lot) — jamais une valeur neutre.

    **Le dénominateur ne retient que les documents issus d'un PDF** (règle `SOURCE_FILES` du loader,
    par `document_parse_depuis_un_pdf`). Un document non-PDF — la copie de site — n'a aucune
    structure d'ingestion à prouver : la story 4.2c ne s'applique pas à lui, aucun chemin de
    production ne lui écrit de `structure.json`, et l'exiger rendait tout gate `full` du guide
    définitivement rouge. L'exclusion est documentée au plancher et dans `docs/evals/harness.md`, et
    elle ne laisse **aucun** périmètre sans exigence : ce que le témoin n'oppose plus au guide,
    `preuve_darbre` le lui oppose sous une autre forme — celle que son ingestion peut réellement
    produire. Les deux pendants fail-open sont fermés dans `main()`, avant tout appel : un document
    PDF sans cas `parsing` ne rend le témoin applicable à rien, et un document **sans source** ne
    tombe sous aucun des deux périmètres. Les deux font refuser le gate `full`.

    « Prouvée » veut dire **quatre** choses ensemble, et l'absence de l'une suffit à ne pas compter :

    1. le manifest **déclare** un `structure_hash` ;
    2. l'artefact `data/{doc_id}/structure.json` est présent et concorde avec lui ;
    3. `report.json` est présent, **lisible** et **rattaché à ce document** (`doc_id` égal) ;
    4. il porte un check `structure_proposee` **affirmatif** — et **aucun** bloquant du même nom —
       dont l'attestation nomme le `document_hash` et le `structure_hash` observés.

    La quatrième condition est celle qui manquait : `if rapport.is_file()` rendait le rapport
    facultatif, si bien qu'un `structure.json` au contenu arbitraire, son hash recopié au manifest et
    **aucun** `report.json` suffisaient à faire verdir le témoin. Une preuve fabriquée verdissait.

    Sur le corpus servi aujourd'hui, aucune `structure.json` n'existe et aucun `report.json` ne porte
    d'attestation : la story 4.2c n'a jamais été exercée sur ce corpus, et la réingestion réelle est
    une dette de l'orchestrateur. « Structure insuffisamment prouvée » est donc l'état **réel** ; ce
    témoin le dit en rouge chiffré au lieu de le contourner.
    """
    if lecture is None:
        with lecture_pincee(data_dir) as pincee:
            return preuve_de_structure(data_dir, ctx, doc_ids, lecture=pincee)
    uniques = [doc_id for doc_id in sorted(set(doc_ids))
               if document_parse_depuis_un_pdf(data_dir, doc_id)]
    prouves = 0
    for doc_id in uniques:
        entry = ctx.index.corpus.manifest.get(doc_id)
        attendu = getattr(entry, "structure_hash", None) if entry is not None else None
        if not attendu:
            continue
        chemin = lecture.reel(data_dir / doc_id / STRUCTURE_FILE)
        try:
            observe = hashlib.sha256(chemin.read_bytes()).hexdigest()
        except OSError:
            continue
        if observe != attendu:
            continue
        lu = _rapport_dingestion(data_dir, doc_id, lecture)
        if lu is None:
            continue  # rapport absent, illisible ou étranger : rien n'atteste, rien n'est prouvé
        if not _atteste(lu, STRUCTURE_CHECK, lire_attestation_structure,
                        (getattr(entry, "document_hash", "") or "", observe)):
            continue
        prouves += 1
    return prouves, len(uniques)


# --- le gate (AD-7) --------------------------------------------------------------------------------

def construire_gate(entry: ManifestEntry, ctx: Contexte, *, profil: str, cas: list[Cas],
                    cases_dir: Path, evals_ok: bool,
                    snapshot: CasesSnapshot | None = None,
                    decisions: list[GateDecision] | None = None,
                    run_digest: str | None = None,
                    plancher_digest: str | None = None,
                    candidate_revision: str | None = None,
                    report_digest: str | None = None) -> Gate:
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
        model_ids=dict(TIERS), pipeline_settings=ctx.settings.thresholds(), evals_ok=evals_ok,
        decisions=list(decisions or []), run_digest=run_digest,
        # Story 4.5 : le protocole, la révision et le rapport — recopiés, jamais recalculés ici.
        # `structure_hash` vient de **l'entrée du manifest**, comme `overlay_hash` : le recalculer
        # donnerait un gate qui se valide lui-même (le loader compare le gate à l'entrée, et deux
        # valeurs issues du même calcul seraient toujours d'accord).
        plancher_digest=plancher_digest, candidate_revision=candidate_revision,
        report_digest=report_digest, structure_hash=getattr(entry, "structure_hash", None),
        date=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"))


def preparer_gate(manifest_path: Path, doc_id: str, gate: Gate, *,
                  lire: Any = None) -> tuple[Path, str] | None:
    """Sérialise `manifest[doc_id].gate` et rend `(manifest, contenu)` — **sans rien écrire**.

    Rend `None` — sans rien préparer — quand le gate **ne doit pas** être écrit : c'est la
    non-mutation du dernier vert, et elle se décide ici, avant toute écriture.

    **Story 4.5, B7 : plus aucun temporaire n'est créé ici.** La fonction ne fait que lire, valider
    et sérialiser ; l'écriture appartient entièrement à `EspacePublie.basculer`, qui écrit tous les
    slots du lot dans une génération inactive puis publie le tout par un unique `os.replace`. Le
    temporaire de manifest qui échappait à son nettoyage n'existe plus : il n'y a plus de
    temporaire de manifest.

    Le manifest entier est relu et **validé** avant la moindre écriture : un fichier illisible ou une
    entrée hors schéma arrête tout sans rien modifier sur disque. Le contenu final est sérialisé **en
    mémoire**, à la forme des autres écrivains de `data/` — clés triées, `indent=2`,
    `ensure_ascii=False`, saut de ligne final —, pour qu'un `git diff` de gate ne soit qu'un gate.

    **Pourquoi une préparation séparée** (revue A). Publier puis écrire le gate déplaçait la fenêtre
    de la revue B3 au lieu de la fermer : `ecrire_gate` peut encore refuser (manifest illisible,
    entrée absente ou hors schéma), et les trois surfaces auraient alors publié `evals_ok: true` sans
    qu'aucun gate n'existe. Tout ce qui peut échouer — lecture, validation, sérialisation — se
    produit donc **avant** la bascule ; ne reste ensuite que l'unique `os.replace` du pointeur, qui
    publie le lot entier ou rien.

    **Non-mutation du dernier vert (story 4.2b).** Un gate candidat **rouge** ne remplace jamais un
    gate `evals_ok: true` : le verdict candidat est publié dans le rapport (décisions chiffrées,
    raison, limites), et le mécanisme de promotion reste le seul chemin de bascule.

    **`lire` est la lecture de la transaction, et c'est elle qui compte** (revue du tour de racine
    unique, constat 1). Le gate est le quatrième écrivain de `data/manifest.json`, et il lisait le
    manifest entier **hors du verrou** avant d'en remettre la sérialisation à la bascule : une
    ingestion publiée entre-temps était donc écrasée par une fusion périmée — le fait 2 exactement,
    sur le writer que le tour n'avait pas fermé. La lecture, la décision de non-mutation du dernier
    vert et la publication vivent maintenant dans la même `EspacePublie.transaction()`. Sans `lire`
    (arbre qu'aucune racine ne couvre), le chemin est lu tel quel, comme avant.
    """
    try:
        brut_texte = lire(manifest_path) if lire is not None else manifest_path.read_text(
            encoding="utf-8")
        if brut_texte is None:
            raise FileNotFoundError(str(manifest_path))
        brut = json.loads(brut_texte)
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
        return None
    if (gate.evals_ok and not gate.decisions and isinstance(gate_existant, dict)
            and gate_existant.get("evals_ok") is True and gate_existant.get("decisions")):
        # HIGH 1 : un vert **sans décisions** — écrit à la main ou par un chemin antérieur au
        # protocole — ne remplace pas un vert qui porte sa preuve chiffrée. Ce serait troquer une
        # preuve contre un booléen.
        print(f"gate candidat sans décisions sur {doc_id!r} : le gate vert existant, qui porte "
              "ses décisions chiffrées, n'est pas modifié", file=sys.stderr)
        return None
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
    # La forme des autres écrivains de `data/` — clés triées, `indent=2`, `ensure_ascii=False`, saut
    # de ligne final — pour qu'un `git diff` de gate ne montre qu'un gate.
    #
    # La course de perte de gate — deux écrivains qui relisent le même manifest et perdent l'une des
    # deux entrées — demandait un verrou sur **toute la séquence**, entrée différée
    # (`target_story: 4.1`). Elle est payée, et pour tous les écrivains : le ping-pong à deux
    # générations impose un `flock` exclusif, et lecture, fusion et publication vivent dans la même
    # `EspacePublie.transaction()` — ici par `lire`, dans l'ingestion par
    # `server/ingest/artifacts.py::fusionner_et_publier`. Il ne reste aucun écrivain de
    # `data/manifest.json` qui lise hors du verrou.
    return manifest_path, json.dumps(dict(sorted(brut.items())), indent=2,
                                     ensure_ascii=False) + "\n"


def ecrire_gate(manifest_path: Path, doc_id: str, gate: Gate) -> bool:
    """Sérialise puis bascule, en une fois — l'unique écriture de `data/` de tout ce module (AD-7).

    C'est `preparer_gate` suivi de sa bascule : la sémantique est mot pour mot celle d'avant la revue
    A (rend `True` quand le gate a été écrit, `False` quand la non-mutation du dernier vert
    s'applique, lève `RefusDeTourner` sur un manifest qu'on ne peut ni lire ni écrire). Le gate
    `full` de `main()`, lui, n'appelle pas cette fonction : il remet le manifest **avec** ses
    publications au même appel de bascule, pour qu'aucune surface ne puisse affirmer un verdict que
    le manifest ne porte pas.

    **Le contrat de l'espace couvre ce chemin** (story 4.5, B7). Il n'y a plus de temporaire préparé
    avant l'appel, donc plus de temporaire à oublier : la sérialisation est en mémoire, l'écriture
    et la publication appartiennent à `EspacePublie.transaction()`. L'espace est dérivé du manifest
    par l'autorité unique `espace_du_data_dir`, de sorte qu'un appelant ne puisse pas en désigner un
    autre que celui où vit le manifest qu'il écrit.

    **La lecture est dans la transaction** (revue du tour de racine unique) : relire, décider la
    non-mutation du dernier vert et publier sous le même `flock`, sans quoi une entrée publiée
    entre la lecture et la bascule disparaîtrait du manifest que ce gate republie.
    """
    espace = espace_du_data_dir(manifest_path.parent)
    if not espace.installe():
        # Aucun pointeur : il n'y a pas de section critique à ouvrir — personne d'autre ne peut
        # publier ce manifest — et le refus, si un gate doit réellement être écrit, reste celui
        # d'avant, dit par la bascule et nommant la commande qui pose la disposition.
        prepare = preparer_gate(manifest_path, doc_id, gate)
        if prepare is None:
            return False
        espace.basculer([prepare])
        return True
    with espace.transaction() as transaction:
        prepare = preparer_gate(manifest_path, doc_id, gate, lire=transaction.lire)
        if prepare is None:
            return False
        transaction.publier([prepare])
    return True


# --- la publication (FR41, FR42) --------------------------------------------------------------------

def reserves_du_lot(cas: list[Cas], *, dictionary_validated: bool) -> ReservesPubliees:
    """Les trois réserves de l'AC 3, dérivées du lot — **une seule autorité**, jamais recomposées.

    - `countersigned` : la conjonction des `truth.countersigned_by` des cas exécutés — exactement la
      règle que `construire_gate` applique pour écrire `Gate.countersigned` ;
    - `validated_by_expert` : AD-14 la fixe à `false` pour tout ce que ce projet produit, et le
      schéma de cas (`Verite.validated_by_expert: Literal[False]`) l'empêche d'être vraie ;
    - `dictionary_validated` : une main a-t-elle signé `data/dictionary.json` (AD-5) ?

    Aucune décision de gate n'en dépend, et c'est délibéré : ce sont des dettes **connues et dites**,
    pas des défauts du candidat. Les rendre bloquantes ferait refuser de servir pour une signature
    manquante ; les taire ferait affirmer une relecture qui n'a pas eu lieu.

    La fonction prend le **lot** et non le gate : le rapport de CI la publie aussi, et la CI tourne
    sans `--gate` (revue 4.5, P6). Deux dérivations auraient pu diverger.
    """
    return ReservesPubliees(
        countersigned=all(c.truth.countersigned_by is not None for c in cas),
        # Dérivé des cas, jamais écrit en dur : le jour où AD-14 changerait d'avis, c'est le schéma
        # de cas qui bougerait, et cette ligne suivrait sans qu'on ait à s'en souvenir.
        validated_by_expert=any(bool(c.truth.validated_by_expert) for c in cas),
        dictionary_validated=dictionary_validated)


def etat_seconde_lecture(ctx: Contexte, rapport: dict[str, Any], *,
                         candidate_revision: str,
                         verdict_path: Path | None = None,
                         images_dir: Path | None = None) -> SecondeLecturePubliee:
    """Où en est la seconde lecture (FR47) — le plan est calculable ici, le verdict est **ingéré**.

    Le builder produit le **plan** déterministe, dérivé du rapport par `blocs_cles_du_rapport` — la
    même fonction que la CLI `python -m server.evals.relecture`, pour que le plan qu'un orchestrateur
    reçoit soit exactement celui que la publication compte.

    Le verdict `concordant|divergent`, lui, vient de l'orchestrateur : seul lui regarde les images de
    pages. Tant qu'il n'est pas déposé, le statut est `planifiee` — **jamais** « concordante par
    défaut » : affirmer une relecture humaine qui n'a pas eu lieu est précisément ce qu'AD-16
    interdit et ce que la story combat.

    Un verdict déposé qui ne se recoupe pas avec le plan — ou dont les `image_sha256` ne
    correspondent pas aux **octets réellement regardés** (`--relecture-images`) — lève
    `RelectureInvalide` : c'est un échec de publication (le run ne peut pas être vert), jamais une
    seconde lecture publiée au rabais.
    """
    # **La révision est exigée par la signature**, plus par un invariant distant (revue B1, frère).
    # Elle l'était en fait déjà — l'appel n'a lieu que sous `exigences_full` —, mais le type disait
    # le contraire et le corps publiait alors `absente 0/0` : tout appelant futur hors gate `full`
    # aurait publié une seconde lecture « absente » sans réserve, là où la vraie réponse est « je ne
    # sais pas de quel candidat on parle ».
    if not candidate_revision:
        raise RelectureInvalide(
            "seconde lecture : la révision candidate est obligatoire — sans elle, le plan ne se "
            "rattache à aucun candidat et « absente » serait une affirmation sans objet")
    plan = plan_de_relecture(ctx.index, blocs_cles_du_rapport(rapport),
                             candidate_revision=candidate_revision)
    attendues = len(plan.cles_attendues)
    if plan.non_projetables:
        # **Rouge, jamais concordante** (revue B5) : des clés attendues n'ont pas pu être projetées.
        # Le statut le dit, la limite dérivée le publie, et `valider_verdict` refuserait de toute
        # façon un verdict déposé sur un tel plan. « Aucun bloc clé » et « tous improjetables »
        # cessent d'être indiscernables.
        return SecondeLecturePubliee(statut="impossible", blocs_planifies=attendues,
                                     blocs_verifies=0,
                                     blocs_non_projetables=len(plan.non_projetables))
    if verdict_path is None:
        statut = "planifiee" if plan.blocs else "absente"
        return SecondeLecturePubliee(statut=statut, blocs_planifies=attendues, blocs_verifies=0)
    try:
        brut = json.loads(verdict_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RelectureInvalide(
            f"verdict de seconde lecture {verdict_path} illisible ({type(exc).__name__})") from exc
    if images_dir is None:
        raise RelectureInvalide(
            "--relecture-verdict exige --relecture-images : un verdict se valide sur les octets "
            "réellement regardés, jamais sur les seules empreintes qu'il annonce")
    # Les octets sont relus **ici**, depuis le dossier que l'orchestrateur a rempli avec la route de
    # la story 3.4 : le validateur recalcule l'empreinte, il ne croit pas celle du verdict (revue B5).
    verdict = valider_verdict(brut, plan, candidate_revision=candidate_revision,
                              images=charger_images(images_dir, plan))
    return SecondeLecturePubliee(statut=statut_du_verdict(verdict),
                                 blocs_planifies=attendues,
                                 blocs_verifies=len(verdict.verdicts))


def preparer_la_publication(rapport: dict[str, Any], gate: Gate, ctx: Contexte, cas: list[Cas], *,
                            data_dir: Path, output_markdown: Path, report_digest: str,
                            candidate_revision: str | None,
                            relecture_verdict: Path | None = None,
                            relecture_images: Path | None = None,
                            repo_root: Path | None = None,
                            resoudre: Any = None,
                            preuve_externe: PreuveExterneVerifiee | None,
                            ) -> list[tuple[Path, str]]:
    """Construit l'artefact unique et **prépare** ses écritures — sans rien publier encore.

    Rend la liste `[(cible, contenu)]` à basculer. Les quatre surfaces de l'AC 4 tiennent en
    **trois écritures** — `data/evals-latest.json` (que `GET /api/v1/evals/latest` sert et que `/`
    compose), `docs/evals/latest.md`, et le **même** rendu dans `eval-results.md`, le fichier que la
    CI concatène dans `$GITHUB_STEP_SUMMARY` —, plus une quatrième cible quand le
    `docs/evals/latest.md` précédent doit être archivé avant d'être remplacé. « Le même artefact » ne
    peut pas être deux rendus divergents : c'est littéralement la même chaîne, rendue une fois.

    Le journal du run (`eval-results.md`) est rendu **ici**, publication comprise : c'est l'autorité
    unique de cette surface. Le rapport JSON, le manifest et ce lot sont ensuite remis au **même**
    appel de bascule — un seul point de commit pour toute l'opération de production (tour correctif
    3/3) —, de sorte qu'aucune surface ne puisse affirmer un verdict que le manifest ne porte pas,
    ni l'inverse, ni qu'un journal de run survive à un gate qui n'a pas été écrit.

    Rien n'est visible à la sortie de cette fonction (revue B3) : rien n'est écrit du tout.

    `repo_root` est **dérivé de `data_dir`** par l'appelant, exactement comme `output_json` et le
    cache le sont déjà. Le figer sur `REPO_ROOT` ferait écrire le `docs/evals/latest.md` du dépôt
    depuis n'importe quel run pointé ailleurs.

    `resoudre` est le repère dans lequel lire le rendu précédent à archiver — `tx.chemin_publie`
    sous une transaction. `docs/evals/latest.md` est une cible **couverte** : décider son archivage
    sur une lecture faite hors du verrou, c'est risquer d'écraser sans l'archiver un rendu qu'une
    bascule concurrente a publié entre-temps (revue du tour de racine unique, constat 1).
    """
    repo_root = repo_root if repo_root is not None else REPO_ROOT
    publication = construire_publication(
        rapport, gate,
        reserves=reserves_du_lot(
            cas, dictionary_validated=bool(getattr(ctx.dictionnaire, "validated", False))),
        relecture=etat_seconde_lecture(ctx, rapport, candidate_revision=candidate_revision,
                                       verdict_path=relecture_verdict,
                                       images_dir=relecture_images),
        report_digest=report_digest, candidate_revision=candidate_revision,
        preuve_externe=preuve_externe)
    prepares = preparer_publication(
        publication, data_dir=data_dir, repo_root=repo_root, resoudre=resoudre,
        nom=ctx.settings.evals_publication_file,
        markdown_run=rendre_markdown(rapport, publication, preuve_externe=preuve_externe),
        chemin_run=output_markdown,
        valeur=_markdown_value, code=_markdown_code)
    return prepares


# --- l'assemblage ----------------------------------------------------------------------------------

def cle_absente(settings: Settings) -> bool:
    """AD-14 : « sans `ANTHROPIC_API_KEY`, les évals refusent de tourner ».

    La règle elle-même vit dans `config.cle_absente` depuis la story 2.1 : l'ingestion du
    dictionnaire fait la même promesse sur la même variable, et deux commandes qui promettent la même
    chose ne peuvent pas la tenir par deux codes différents. Ce nom reste le point d'entrée des évals.
    """
    return config_cle_absente(settings)


def _materialiser_dans_ombre(source: Path, cible: Path, racine: Path,
                             lecture: Lecture | None = None,
                             ancetres: frozenset[Path] = frozenset()) -> None:
    """Matérialise ``source`` sous ``cible`` sans jamais lire hors de ``racine``.

    Les fichiers sont liés physiquement quand le système de fichiers le permet : le corpus AXA
    n'est donc pas recopié. Une copie est le repli portable si le répertoire temporaire se trouve
    sur un autre volume. Les liens symboliques internes sont résolus vers un fichier ou un dossier
    réel dans l'ombre ; ceux qui sortent du vrai ``data/`` sont ignorés, comme le loader les
    mettrait en quarantaine.
    """
    try:
        # **Depuis le repère pincé** (story 4.5, N1) : l'ombre est une photographie du `data/` servi,
        # et une photographie composée de deux générations ne décrit rien. `reel` rend le slot de la
        # génération que l'opération a pincée ; hors espace, il rend le chemin tel quel.
        resolu = (source if lecture is None else lecture.reel(source)).resolve(strict=True)
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
            _materialiser_dans_ombre(enfant, cible / enfant.name, racine, lecture, descendants)
        return
    if not resolu.is_file():
        return
    try:
        os.link(resolu, cible)
    except OSError:
        shutil.copy2(resolu, cible)


def _sans_gate_sur_disque(data_dir: Path, doc_id: str, pile: Any,
                          lecture: Lecture | None = None) -> Path:
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
    if lecture is None:
        lecture = pile.enter_context(lecture_pincee(data_dir))
    ombre = Path(pile.enter_context(tempfile.TemporaryDirectory(prefix="evals-regate-")))
    racine = data_dir.resolve(strict=True)
    for entree in data_dir.iterdir():
        if entree.name == MANIFEST:
            continue
        _materialiser_dans_ombre(entree, ombre / entree.name, racine, lecture)
    brut = json.loads(lecture.reel(data_dir / MANIFEST).read_text(encoding="utf-8"))
    if isinstance(brut, dict) and isinstance(brut.get(doc_id), dict):
        brut[doc_id]["gate"] = None
    (ombre / MANIFEST).write_text(json.dumps(brut, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
    return ombre


def construire_contexte(settings: Settings, data_dir: Path, *, regate: str | None = None,
                        pile: Any = None, cache_dir: Path | None = None,
                        campaign_budget_eur: float | None = None,
                        campaign_accrued_eur: float = 0.0,
                        campaign_cost_recorder: Any = None,
                        lecture: Lecture | None = None) -> Contexte:
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

    `lecture` est le **repère pincé** de l'opération de gate (story 4.5, N1) : le corpus qui sert de
    base à la mesure, les dictionnaires et les preuves de structure lues juste après doivent venir
    d'**une seule** génération. Sans lui, un repère est pincé pour la durée de cet appel seul.
    """
    if lecture is None:
        with lecture_pincee(data_dir) as pincee:
            return construire_contexte(
                settings, data_dir, regate=regate, pile=pile, cache_dir=cache_dir,
                campaign_budget_eur=campaign_budget_eur,
                campaign_accrued_eur=campaign_accrued_eur,
                campaign_cost_recorder=campaign_cost_recorder, lecture=pincee)
    contexte_gate = GateContext(pipeline_digest=pipeline_digest(), prompts_digest=prompts_digest(),
                                model_ids=dict(TIERS), pipeline_settings=settings.thresholds(),
                                # Story 4.5 (revue B2) : le loader compare la révision qu'un gate
                                # `full` **nomme** à celle qui tourne. Le runner charge exactement
                                # comme `api/etat.py` : le contexte doit donc la porter ici aussi.
                                candidate_revision=settings.git_sha, env=settings.env)
    source = data_dir
    lecture_du_corpus = lecture
    if regate is not None and pile is not None:
        entree_brute = json.loads(lecture.reel(data_dir / MANIFEST).read_text(encoding="utf-8")) \
            if lecture.fichier(data_dir / MANIFEST) else {}
        brut_gate = (entree_brute[regate].get("gate")
                     if isinstance(entree_brute.get(regate), dict) else None)
        gate_a_refaire = False
        if isinstance(brut_gate, dict):
            gate_a_refaire = brut_gate.get("evals_ok") is False
            if (not gate_a_refaire and brut_gate.get("profile") == "full"
                    and (brut_gate.get("pipeline_digest"), brut_gate.get("prompts_digest"),
                         brut_gate.get("model_ids"), brut_gate.get("pipeline_settings", {})) != (
                         contexte_gate.pipeline_digest, contexte_gate.prompts_digest,
                         contexte_gate.model_ids, contexte_gate.pipeline_settings)):
                gate_a_refaire = True
            if not gate_a_refaire:
                try:
                    Gate.model_validate(brut_gate)
                except ValidationError:
                    gate_a_refaire = True
        if gate_a_refaire:
            source = _sans_gate_sur_disque(data_dir, regate, pile, lecture)
            # L'ombre est une photographie **déjà pincée** : elle n'a pas d'espace, donc son repère
            # ne résout rien et ne peut rien mêler. Le repère du `data/` réel reste celui de tout le
            # reste de la passe.
            lecture_du_corpus = pile.enter_context(lecture_pincee(source))
    corpus = load_corpus(source, allow_ungated=True, current=contexte_gate,
                         perimetre_max_chars=settings.perimetre_max_chars,
                         raison_max_chars=settings.raison_publiable_max_chars,
                         lecture=lecture_du_corpus)
    dictionnaires = {
        doc_id: load_dictionary(data_dir, corpus, doc_id, lecture=lecture)
        for doc_id, document in corpus.documents.items()
        if document.kind == "contrat"
    }
    response_cache = PersistentResponseCache(cache_dir) if cache_dir is not None else None
    return Contexte(settings=settings, index=Index(corpus),
                    client=LlmClient(settings, cache=response_cache,
                                     campaign_budget_eur=campaign_budget_eur,
                                     campaign_accrued_eur=campaign_accrued_eur,
                                     campaign_cost_recorder=campaign_cost_recorder),
                    pipeline_digest_hex=contexte_gate.pipeline_digest,
                    prompts_digest_hex=contexte_gate.prompts_digest,
                    # Lu dans `data_dir`, pas dans `source` : `_sans_gate_sur_disque` ne recopie
                    # que le manifest, et le dictionnaire n'a rien à voir avec le gate qu'on refait.
                    dictionnaire=load_dictionary(data_dir, corpus, settings.guide_doc_id,
                                                 lecture=lecture),
                    dictionnaires=dictionnaires, response_cache=response_cache)


def construire_contexte_parsing(settings: Settings, data_dir: Path, *,
                                lecture: Lecture | None = None) -> Contexte:
    """Charge seulement les artefacts servis : aucun client, cache ou dictionnaire n'est construit.

    `lecture` : le repère pincé de l'opération (N1), comme pour `construire_contexte`.
    """
    if lecture is None:
        with lecture_pincee(data_dir) as pincee:
            return construire_contexte_parsing(settings, data_dir, lecture=pincee)
    contexte_gate = GateContext(pipeline_digest=pipeline_digest(), prompts_digest=prompts_digest(),
                                model_ids=dict(TIERS), pipeline_settings=settings.thresholds(),
                                # Story 4.5 (revue B2) : le loader compare la révision qu'un gate
                                # `full` **nomme** à celle qui tourne. Le runner charge exactement
                                # comme `api/etat.py` : le contexte doit donc la porter ici aussi.
                                candidate_revision=settings.git_sha, env=settings.env)
    corpus = load_corpus(data_dir, allow_ungated=True, current=contexte_gate,
                         perimetre_max_chars=settings.perimetre_max_chars,
                         raison_max_chars=settings.raison_publiable_max_chars, lecture=lecture)
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
    # Le défaut annoncé est **dérivé** de `DEFAUT_PAR_SUITE`, jamais réécrit à la main : c'est la
    # seule description que lit un opérateur avant de lancer une campagne, et deux runs ne sont
    # comparables qu'à variante égale.
    p.add_argument("--variant", choices=VARIANTES_LIVREES,
                   help="variante (défaut : " + ", ".join(
                       f"{suite} → {defaut}" for suite, defaut in DEFAUT_PAR_SUITE.items()) + ")")
    p.add_argument("--gate", metavar="DOC_ID",
                   help="écrire `manifest.gate` pour ce document depuis la suite qui le sert")
    p.add_argument("--repeat", type=int, default=1, metavar="N",
                   help="répéter chaque cas N fois, sans cache de réponse : publication complète "
                        "par répétition + agrégat de stabilité (story 4.2b ; N>=3 pour une preuve)")
    p.add_argument("--producer", choices=("builder", "orchestrator"), default="builder",
                   help="qui produit ce run : la règle trusted ne reconnaît que l'orchestrateur "
                        "comme producteur de preuve ; un run de builder est un diagnostic")
    p.add_argument("--series-kind", choices=("baseline", "final"),
                   help="phase de la série orchestrateur ; obligatoire avec --producer orchestrator")
    p.add_argument("--series-id",
                   help="identité stable partagée par les points d'une même série orchestrateur")
    p.add_argument("--orchestrator-evidence", type=Path,
                   help="mesures externes trusted (tests hors ligne, A16, série decision_claim — "
                        "le prédicat est jugé par le runner, la série chiffrée reste orchestrateur) ; "
                        "réservé à --producer orchestrator et --gate")
    p.add_argument("--orchestrator-report", type=Path,
                   help="rapport dont la preuve trusted se réclame ; ses octets sont recoupés avec "
                        "report_digest (obligatoire avec --orchestrator-evidence)")
    p.add_argument("--candidate-revision", metavar="SHA40",
                   help="révision produit mesurée par ce run (40 hexadécimaux) ; obligatoire sous "
                        "--gate {doc_id} --profile full et avec --orchestrator-evidence")
    p.add_argument("--relecture-verdict", type=Path,
                   help="verdict de seconde lecture rempli par l'orchestrateur (FR47) : il est "
                        "recoupé avec le plan dérivé du rapport de ce run avant d'être publié")
    p.add_argument("--relecture-images", type=Path,
                   help="répertoire des images de pages réellement regardées (un fichier "
                        "`{block_id avec _ }.png` par bloc du plan) : leur empreinte est recalculée "
                        "et confrontée aux image_sha256 du verdict ; obligatoire avec "
                        "--relecture-verdict")
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
    # **La résolution s'arrête à l'espace de publication** (story 4.5, B7). `resolve()` suit les
    # liens, et toutes les cibles publiées se résolvent désormais *dans* `data/.publie/<gen>/…` :
    # comparées là, `eval-results.json` et `data_dir` se chevauchent toujours, et tout run serait
    # refusé pour une collision qui n'en est pas une — c'est l'indirection du pointeur unique, pas
    # un alias d'argument. Hors de l'espace, `resolve()` garde son rôle : détecter deux arguments
    # qui désignent le même fichier par des chemins différents.
    espace_chemin = Path(os.path.abspath(data_dir / _REPERTOIRE_ESPACE))

    def _pour_collision(chemin: Path) -> Path:
        resolu = chemin.resolve()
        return Path(os.path.abspath(chemin)) if resolu.is_relative_to(espace_chemin) else resolu

    chemins = {
        "sortie JSON": _pour_collision(output_json),
        "sortie Markdown": _pour_collision(output_markdown),
        "cases_dir": _pour_collision(cases_dir),
        "reference_dir": _pour_collision(reference_dir),
        "data_dir": _pour_collision(data_dir),
        "cache": _pour_collision(cache_dir),
        "manifest": _pour_collision(data_dir / MANIFEST),
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
    """Ferme toujours les ressources persistantes, quel que soit le code de sortie de la CLI."""
    with ExitStack() as lifecycle:
        return _main(argv, lifecycle=lifecycle)


def _main(argv: list[str] | None = None, *, lifecycle: ExitStack) -> int:
    args = _parser().parse_args(argv)
    sortie = sys.stdout
    cas: list[Cas] = []
    output_json = args.output_json or args.data_dir.parent / "eval-results.json"
    output_markdown = args.output_markdown or args.data_dir.parent / "eval-results.md"
    cache_dir = args.cache_dir or args.data_dir.parent / ".cache" / "evals"
    # **L'espace de publication du run** (story 4.5, B7) : bundle immuable et pointeur unique. Il est
    # dérivé de `--data-dir` par l'autorité unique, comme les sorties et le cache le sont déjà, et il
    # n'est **jamais posé ici** : une bascule qui installerait sa propre disposition changerait le
    # type de ses cibles une par une, ce que l'invariant interdit. Un espace absent est un refus dit.
    espace = espace_du_data_dir(args.data_dir)
    snapshot: CasesSnapshot | None = None
    run_identity: dict[str, Any] | None = None
    decisions_orchestrateur: list[GateDecision] = []
    # **L'ancrage des empreintes de run étrangères**, porté d'ici jusqu'aux quatre surfaces (revue
    # B5, tour correctif 2/3). `None` ne veut pas dire « pas de contrainte » mais « aucune preuve
    # externe n'a été vérifiée, donc aucune décision étrangère n'est publiable » — un diagnostic
    # sans preuve externe n'a pas non plus de décision externe à publier.
    preuve_externe: PreuveExterneVerifiee | None = None
    references = ReferencesSnapshot(empreinte_canonique([]))
    reference_dir = args.cases_dir.parent / "reference"
    campaign: CampaignLedger | None = None
    # Armées seulement sous `--gate {doc_id} --profile full`, et connues des trois chemins de
    # rapport (refus de budget, incident, run complet) pour qu'un rapport partiel porte les mêmes
    # décisions qu'un rapport complet aurait portées.
    exigences_full = False
    structure_lot: tuple[int, int] | None = None
    arbre_lot: tuple[int, int] | None = None
    # Troisième réserve de l'AC 3, publiée jusque dans le résumé de CI (revue 4.5, P6). `None` tant
    # qu'aucun contexte n'est construit — un refus de budget survient avant : le rapport ne dit alors
    # rien des réserves plutôt que d'en inventer l'état.
    dictionary_validated: bool | None = None
    # FR41 : une publication qui échoue est un run qui n'a pas tenu sa promesse (revue 4.5, P8).
    publication_ok = True

    def noter_campaign(status: str) -> None:
        if campaign is not None:
            campaign.record_run(
                run_digest=(str(run_identity.get("run_digest")) if run_identity else None),
                status=status)
    try:
        valider_chemins(output_json=output_json, output_markdown=output_markdown,
                        cases_dir=args.cases_dir, reference_dir=reference_dir,
                        data_dir=args.data_dir, cache_dir=cache_dir)
        # **La racine de publication, vérifiée avant toute mesure** (story 4.5, N3). Les seules
        # occurrences d'`espace` étaient une construction pure — qui ne vérifie rien — puis quatre
        # usages **tous postérieurs** à l'exécution : sur un `--data-dir` non installé, la campagne
        # entière était payée avant que le refus ne tombe, et le rapport était perdu.
        # `docs/evals/harness.md` affirmait pourtant, mot pour mot, que « le run refuse avant toute
        # mesure ». Il le fait désormais, et le refus est un code 2 comme les autres refus d'avant
        # appel. Un `--data-dir` custom **installé** passe sans traitement particulier.
        try:
            if not args.dry_run:
                # `--dry-run` n'écrit **rien** : il n'y a ni travail payant, ni cible, donc rien que
                # la racine protège. Ce n'est pas une voie opt-in — aucun argument ne désarme le
                # contrôle sur un chemin qui publie —, c'est l'absence d'objet.
                espace.verifier_lot(cibles_publiees_du_run(
                    args.data_dir, output_json, output_markdown, gate=bool(args.gate)))
        except (EspaceNonInstalle, LotHorsEspace, EspaceIllisible) as exc:
            raise RefusDeTourner(
                f"espace de publication : {exc} — rien n'a été mesuré, aucun appel n'a été "
                "soumis, aucune cible n'a été écrite") from exc
        try:
            settings = Settings()
        except ValidationError as exc:
            premier = exc.errors()[0]
            champ = ".".join(str(p) for p in premier.get("loc", ())) or "configuration"
            raise RefusDeTourner(
                f"configuration invalide ({champ}) : {premier.get('msg', '')}") from exc
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
            charge_plancher = charger_plancher(producer=args.producer)
        except PlancherInvalide as exc:
            raise RefusDeTourner(str(exc)) from exc
        if args.repeat > charge_plancher.plancher.series.max_repeat:
            raise RefusDeTourner(
                f"--repeat {args.repeat} dépasse la borne pré-enregistrée "
                f"{charge_plancher.plancher.series.max_repeat}")
        if args.producer == "orchestrator":
            if args.max_cost is None:
                raise RefusDeTourner(
                    "--producer orchestrator exige --max-cost explicite (borne locale par run)")
            if args.repeat < charge_plancher.plancher.n_minimum:
                raise RefusDeTourner(
                    f"--producer orchestrator exige --repeat >= "
                    f"{charge_plancher.plancher.n_minimum}")
            if not args.dry_run and (args.series_kind is None or not args.series_id):
                raise RefusDeTourner(
                    "--producer orchestrator exige --series-kind et --series-id")
            if not args.dry_run and not settings.live_campaign_id:
                raise RefusDeTourner(
                    "--producer orchestrator exige LIVE_CAMPAIGN_ID : preuve non vérifiable")
        elif args.series_kind is not None or args.series_id is not None:
            raise RefusDeTourner(
                "--series-kind/--series-id sont réservés à --producer orchestrator")
        # --- story 4.5 : les gardes de `full`, **avant tout appel** (code 2) -----------------------
        #
        # Elles s'arment sur **le gate**, pas sur le profil seul. La CI lance `EVALS_PROFILE: full`
        # sans `--gate` ni `--repeat` à chaque PR (`ci.yml`, `tests/test_evals_live.py`) : exiger
        # `--repeat >= 3` sur le profil rendrait cette CI rouge pour une raison qui n'a rien à voir
        # avec le candidat, et l'AC formule d'ailleurs sa condition sur la commande complète —
        # « `--gate {doc_id} --profile full` ». Un `full` sans gate reste ce qu'il est : un
        # diagnostic.
        exigences_full = bool(args.gate) and args.profile == "full"
        if args.candidate_revision is not None and not (
                len(args.candidate_revision) == 40
                and all(c in "0123456789abcdef" for c in args.candidate_revision)):
            raise RefusDeTourner(
                f"--candidate-revision {args.candidate_revision!r} : 40 caractères hexadécimaux "
                "attendus (la révision produit que ce run mesure)")
        if exigences_full:
            if args.repeat < charge_plancher.plancher.n_minimum:
                raise RefusDeTourner(
                    f"--gate {args.gate} --profile full exige --repeat >= "
                    f"{charge_plancher.plancher.n_minimum} (n_minimum du plancher) : une politique "
                    f"complète prouvée sur {args.repeat} exécution(s) ne prouve pas la stabilité")
            if args.candidate_revision is None:
                raise RefusDeTourner(
                    f"--gate {args.gate} --profile full exige --candidate-revision <40 hex> : un "
                    "gate qui ne nomme pas la révision qu'il mesure peut être réutilisé par une "
                    "révision qu'il n'a jamais vue")
            # **La révision annoncée doit être celle qu'on exécute** (revue B1). Sans ce contrôle,
            # `--candidate-revision` n'était comparée qu'à elle-même : le runner la recopiait dans le
            # gate et dans la preuve, puis recoupait la preuve avec ce même argument — trois surfaces
            # d'accord sur une révision que personne n'a exécutée.
            revision, sales = revision_executee(
                REPO_ROOT, sorties=sorties_du_run(settings.evals_publication_file))
            if revision is None:
                raise RefusDeTourner(
                    "--profile full : la révision réellement exécutée n'a pu être établie (ni "
                    "`git rev-parse HEAD`, ni GIT_SHA en 40 hexadécimaux) — une liaison qu'on ne "
                    "peut pas prouver n'est pas une liaison")
            if sales:
                raise RefusDeTourner(
                    "--profile full : l'arbre de travail porte des modifications non commises "
                    f"({', '.join(sales[:5])}{'…' if len(sales) > 5 else ''}) — un gate mesuré sur "
                    "un arbre sale se réclame d'un commit qui ne décrit pas le code exécuté")
            if revision != args.candidate_revision:
                raise RefusDeTourner(
                    f"--candidate-revision {args.candidate_revision} ≠ révision réellement "
                    f"exécutée {revision} : ce gate mesurerait un code qu'il ne nomme pas")
        if args.orchestrator_evidence is not None:
            if args.producer != "orchestrator" or args.gate is None:
                raise RefusDeTourner(
                    "--orchestrator-evidence exige --producer orchestrator et --gate")
            if args.orchestrator_report is None:
                raise RefusDeTourner(
                    "--orchestrator-evidence exige --orchestrator-report : sans les octets du "
                    "rapport, `report_digest` ne peut être recoupé avec rien (M2)")
            if args.candidate_revision is None:
                raise RefusDeTourner(
                    "--orchestrator-evidence exige --candidate-revision : une preuve trusted se "
                    "réclame de la révision qu'elle mesure (M2)")
            decisions_orchestrateur, preuve_externe = charger_decisions_orchestrateur(
                args.orchestrator_evidence, plancher=charge_plancher,
                candidate_revision=args.candidate_revision,
                report_path=args.orchestrator_report,
                # Deux runs ne se comparent qu'à image égale : la preuve doit décrire **ce** code.
                # **Les cinq champs** qu'`identite_run` écrit, sans exception : la comparaison itère
                # sur ce dictionnaire, donc un champ omis ici est un champ jamais opposé. C'était le
                # cas de `normalize_version` — dont dépendent tous les `text_norm`, donc tous les
                # `quote_hash` — et de `plancher_digest` (revue B1).
                image_courante=image_du_run(charge_plancher.digest))
        elif args.orchestrator_report is not None:
            raise RefusDeTourner(
                "--orchestrator-report n'a de sens qu'avec --orchestrator-evidence")
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
            # Sous `full`, la suite `parsing` du document entre dans le lot (AC 1) : sans elle,
            # `parsing_ok_rate` n'aurait rien à mesurer et la politique complète promettrait un
            # raisonnement juste sur un texte que personne n'a comparé au contrat imprimé.
            suites = suites_du_gate(settings, args.gate, args.profile, cases_dir=args.cases_dir)
        if args.suite:
            # La comparaison porte sur la **suite qui sert le document** (`suites[0]`), pas sur le
            # tuple entier : depuis 4.5, celui-ci peut aussi porter la suite `parsing` du document
            # sous `full`, et un `--suite parsing --gate X` sélectionnerait alors le parsing de
            # **tous** les documents — un gate qui se réclamerait d'une suite qu'il n'a pas exécutée.
            if suites is not None and args.suite != suites[0]:
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
        # **Le trou fail-open de la première condition rouge** (revue 4.5, P2), et sa jumelle de
        # structure (revue B4), fermées par la **même** garde de composition du lot.
        #
        # `parsing_ok_rate` et les deux témoins de structure ont un scope `suite` : sur un lot qui ne
        # les arme pas, aucune décision n'est émise, et un gate `full` pouvait devenir vert sans la
        # moindre preuve d'extraction ni de structure — exactement ce que l'AC 1 interdit.
        #
        # La fermeture est un refus **avant tout appel** plutôt qu'une décision rouge, et c'est le
        # choix cohérent avec le mécanisme existant : la vacuité de `construire_decisions` ferme ce
        # qu'un run **a mesuré et n'a pas prouvé**, tandis qu'ici c'est le *lot lui-même* qui est mal
        # composé — une faute d'appel, pas une mesure. La refuser avant le premier appel évite en
        # plus de payer une campagne dont on sait déjà qu'elle ne peut rien conclure.
        #
        # Générique, sans branche par document : c'est la règle `SOURCE_FILES` du loader qui dit ce
        # qu'un document **est**, et donc quel témoin le couvre — `structure_prouvee_rate` et
        # `parsing_ok_rate` pour un document issu d'un PDF, `arbre_prouve_rate` pour une copie de
        # site. La question posée est unique : « les témoins qui couvrent ce document sont-ils armés
        # par ce lot ? ».
        if exigences_full:
            source = _source_du_document(args.data_dir, args.gate)
            if source is None:
                # **Aucune source sur le disque** (revue C) — l'état réel d'un checkout frais : les
                # `source.pdf` ne sont pas committés (`.gitignore`), ils sont téléchargés au build.
                # Sans source, la règle `SOURCE_FILES` ne sait pas ce que ce document est, aucun des
                # deux témoins de structure ne le compte, et le gate `full` verdirait sans qu'aucune
                # preuve de structure ne lui ait été opposée. Le refus nomme la remise en état.
                raise RefusDeTourner(
                    f"--gate {args.gate} --profile full : aucune source n'est présente pour ce "
                    f"document ({', '.join(SOURCE_FILES)} absents de "
                    f"{args.data_dir / args.gate}), donc rien ne dit ce qu'il est ni quelle preuve "
                    f"de structure lui opposer. Un gate `full` sans exigence de structure "
                    f"affirmerait la politique complète sans l'avoir opposée — remettre la source "
                    f"en état (`uv run python -m server.ingest.fetch_source --all --data "
                    f"{args.data_dir}`), ou mesurer en `--profile vertical`")
            depuis_un_pdf = source == "source.pdf"
            couvrants = ["structure_prouvee_rate", "parsing_ok_rate"] if depuis_un_pdf else [
                "arbre_prouve_rate"]
            for metrique in couvrants:
                temoin_couvrant = charge_plancher.plancher.temoin(metrique)
                if temoin_couvrant is None or _temoin_applicable(
                        temoin_couvrant, cas, exigences_full=True):
                    continue
                if depuis_un_pdf:
                    raise RefusDeTourner(
                        f"--gate {args.gate} --profile full : ce document est ingéré depuis un "
                        f"PDF, et aucun cas `parsing` ne l'accompagne dans le lot — le témoin "
                        f"{metrique} ne peut donc rien mesurer. La politique complète promet un "
                        f"raisonnement juste sur un texte dont personne n'aurait vérifié qu'il est "
                        f"celui du contrat — écrire au moins un cas sous parsing/{args.gate}/, ou "
                        f"mesurer en `--profile vertical`")
                raise RefusDeTourner(
                    f"--gate {args.gate} --profile full : ce document n'est pas ingéré depuis un "
                    f"PDF, et le témoin {metrique} — la preuve de structure qui lui est applicable "
                    f"— n'est pas armé par ce lot. Un gate `full` sans aucune exigence de structure "
                    f"affirmerait la politique complète sans l'avoir opposée — mesurer en "
                    f"`--profile vertical`, ou composer le lot avec la suite qui sert ce document")
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
        accrued_cost_eur = 0.0
        if args.producer == "orchestrator" and not args.dry_run:
            try:
                campaign = lifecycle.enter_context(CampaignLedger(
                    cache_dir / "campaigns",
                    campaign_id=str(settings.live_campaign_id),
                    budget_eur=settings.live_budget_eur))
                max_series = (charge_plancher.plancher.series.baseline_per_named_witness
                              if args.series_kind == "baseline"
                              else charge_plancher.plancher.series.final_per_named_witness)
                campaign.register_series(
                    kind=args.series_kind, series_id=str(args.series_id),
                    witnesses=[c.id for c in cas], max_series=max_series)
                accrued_cost_eur = campaign.accrued_cost_eur
            except CampaignLedgerError as exc:
                raise RefusDeTourner(str(exc)) from exc
        budget_restant_global = max(settings.live_budget_eur - accrued_cost_eur, 0.0)
        # Deux bornes distinctes : `--max-cost` limite ce run ; le ledger limite la campagne.
        # Aucun arrondi ne peut transformer un petit budget positif en zéro silencieux.
        budget_effectif = min(max_cost, budget_restant_global)
        # Story 4.2b — préflight : le majorant de la campagne est estimé **avant le premier appel**
        # (agrégat du majorant par requête d'AD-9, `max_cost_eur_per_request`, sur les exécutions
        # payantes). Une campagne payante (`--repeat` ≥ 2 : cache désarmé, chaque exécution est
        # facturée) dont le majorant dépasse le budget effectif est refusée proprement, chiffres
        # publiés, acquis conservés — jamais une question humaine (règle trusted).
        executions_payantes = sum(1 for c in cas if c.suite != "parsing") * args.repeat
        majorant_estime = estimate_run_majorant(executions_payantes, settings)
        preflight = {
            "campaign_id": settings.live_campaign_id,
            "series_kind": args.series_kind,
            "series_id": args.series_id,
            "configured_budget_eur": settings.live_budget_eur,
            "accrued_cost_eur": accrued_cost_eur,
            "live_budget_eur": settings.live_budget_eur,
            "max_cost_eur": max_cost,
            "effective_run_budget_eur": budget_effectif,
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
        if executions_payantes and majorant_estime > budget_effectif:
            non_executes = [
                c.id if args.repeat == 1 else f"{c.id}#r{repetition}"
                for c in cas for repetition in range(1, args.repeat + 1)
            ]
            identite_refus = {
                "candidate_revision": args.candidate_revision,
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
                    "campaign_id": settings.live_campaign_id,
                    "series_kind": args.series_kind,
                    "series_id": args.series_id,
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
                decisions_orchestrateur=decisions_orchestrateur,
                exigences_full=exigences_full)
            ecrire_rapports(rapport_refus, output_json, output_markdown, espace=espace,
                            preuve_externe=preuve_externe)
            print("refus de budget avant le premier appel : "
                  f"configured_budget_eur={settings.live_budget_eur:.4f} "
                  f"accrued_cost_eur={accrued_cost_eur:.4f} "
                  f"refused_cost_eur={majorant_estime:.4f} "
                  f"({executions_payantes} exécutions payantes × majorant par requête) — "
                  "rouge chiffré, rapport écrit, acquis conservés, manifest non modifié",
                  file=sys.stderr)
            if campaign is not None:
                campaign.record_run(
                    run_digest=str(identite_refus["run_digest"]), status="budget_refused")
            return 4
        # 5. Le corpus, puis le document visé par `--gate`.
        with ExitStack() as pile:
            # **Un seul repère de lecture pour toute la décision de gate** (story 4.5, N1). Le
            # corpus qui sert de base à la mesure, les dictionnaires, la preuve de structure et la
            # preuve d'arbre lisaient chacun le pointeur à leur tour : jusqu'à trois générations
            # pouvaient entrer dans une seule décision de gate. Elles viennent désormais toutes de
            # la génération pincée ici, pour la durée de l'opération.
            lecture_run = pile.enter_context(lecture_pincee(args.data_dir))
            if all(c.suite == "parsing" for c in cas):
                ctx = construire_contexte_parsing(settings, args.data_dir, lecture=lecture_run)
            else:
                # `--repeat` désarme le cache : trois répétitions servies du cache ne mesureraient
                # ni la stabilité ni le coût (story 4.2b).
                ctx = construire_contexte(settings, args.data_dir, regate=args.gate, pile=pile,
                                          lecture=lecture_run,
                                          cache_dir=None if args.repeat > 1 else cache_dir,
                                          campaign_budget_eur=(settings.live_budget_eur
                                                               if campaign is not None
                                                               else budget_effectif),
                                          campaign_accrued_eur=accrued_cost_eur,
                                          campaign_cost_recorder=(campaign.record_cost
                                                                  if campaign is not None else None))
            dictionary_validated = bool(getattr(ctx.dictionnaire, "validated", False))
            if exigences_full:
                # Lues **avant** l'exécution, sur le corpus que ce run va mesurer : les preuves de
                # structure sont une propriété des artefacts servis, pas du résultat des appels. Les
                # deux périmètres sont complémentaires (règle `SOURCE_FILES`) et lus sur la **même**
                # liste de documents : aucun document du lot n'échappe aux deux.
                documents_du_lot = [c.doc_id or document_de_la_suite(settings, c.suite)
                                    for c in cas]
                structure_lot = preuve_de_structure(args.data_dir, ctx, documents_du_lot,
                                                    lecture=lecture_run)
                arbre_lot = preuve_darbre(args.data_dir, ctx, documents_du_lot,
                                          lecture=lecture_run)
            try:
                run_identity = identite_run(
                    cas, ctx, profile=args.profile, quick=args.quick, variant=args.variant,
                    references_digest=references_digest,
                    plancher_digest=charge_plancher.digest, repeat=args.repeat,
                    campaign_id=settings.live_campaign_id, series_kind=args.series_kind,
                    series_id=args.series_id, candidate_revision=args.candidate_revision)
            except Exception:
                fermer = getattr(ctx.client, "aclose", None)
                if fermer is not None:
                    asyncio.run(fermer())
                raise
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
        noter_campaign("refused")
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
                    decisions_orchestrateur=decisions_orchestrateur,
                    exigences_full=exigences_full, structure=structure_lot, arbre=arbre_lot,
                    dictionary_validated=dictionary_validated)
                ecrire_rapports(rapport, output_json, output_markdown, espace=espace,
                                preuve_externe=preuve_externe)
                print(f"rapports partiels écrits : {output_json} ; {output_markdown}", file=sortie)
            except Exception as rapport_exc:  # noqa: BLE001 — frontière d'incident du writer
                print(f"incident de rapport partiel : {type(rapport_exc).__name__}: {rapport_exc} "
                      "— manifest non modifié", file=sys.stderr)
                noter_campaign("incident_report")
                return 3
        print(f"incident : {exc} — manifest non modifié (un incident n'est pas un verdict)",
              file=sys.stderr)
        noter_campaign("incident")
        return 3
    except Exception as exc:  # noqa: BLE001 — voir ci-dessous
        # Tout le reste (`TypeError` d'une signature qui a bougé, `KeyError`, `OSError`) sortait en
        # code 1 — celui que ce runner réserve à « un cas a rendu un mauvais label ». Un appelant,
        # ou la CI, aurait lu un bug du runner comme un verdict d'éval. C'est un incident : code 3,
        # manifest intact, et la trace part sur stderr pour être diagnosticable.
        traceback.print_exc()
        print(f"incident : {type(exc).__name__}: {exc} — manifest non modifié", file=sys.stderr)
        noter_campaign("internal")
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
                                     decisions_orchestrateur=decisions_orchestrateur,
                                     exigences_full=exigences_full, structure=structure_lot,
                                     arbre=arbre_lot,
                                     dictionary_validated=dictionary_validated)
        # **Les octets du rapport, une fois pour toute l'opération** (tour correctif 3/3).
        # `gate.report_digest` s'en déduit sans relire le disque, donc sans exiger que le rapport
        # ait déjà été publié : c'est ce qui permet au rapport, à la publication et au manifest de
        # n'avoir qu'un seul point de commit.
        contenu_rapport = json_canonique(rapport) + "\n"
        report_digest = digest_contenu(contenu_rapport)
        if not args.gate:
            # Hors gate, l'opération de production **est** ce couple : un lot, un commit.
            espace.basculer(preparer_les_rapports(rapport, output_json, output_markdown,
                                                  preuve_externe=preuve_externe,
                                                  contenu_json=contenu_rapport))
            print(f"rapports écrits : {output_json} ; {output_markdown}", file=sortie)
    except (EspaceNonInstalle, LotHorsEspace, EspaceIllisible) as exc:
        # **Nommer la cause** (revue B3/B6, forme B7) : ce n'est pas une panne du writer, c'est
        # l'espace de publication qui n'est pas posé ou pas lisible — donc un lot qu'aucun pointeur
        # unique ne couvrirait. Rien n'a été écrit, et le code 3 tient sa promesse.
        print(f"refus d'écrire les rapports : {exc} — aucune bascule n'a eu lieu, manifest non "
              "modifié", file=sys.stderr)
        noter_campaign("incident_report")
        return 3
    except RapportInexploitable as exc:
        # **Un défaut de données n'est pas une panne technique** (revue R1). Le journal du run est
        # l'une des quatre surfaces, et il indexait des clés que rien n'exigeait : un rapport
        # amputé y levait un `KeyError` — qui n'est pas une `ValueError` — et ressortait par le
        # dernier `except Exception` en « incident », code 3. C'est la ligne de partage d'AD-8
        # franchie dans le mauvais sens : le verdict *a* été mesuré, c'est le rapport qui n'est pas
        # publiable. Code 1, refus dit, manifest intact (aucun gate n'est écrit sur ce chemin).
        print(f"refus d'écrire les rapports : {exc} — le rapport du run n'est pas publiable, "
              "aucune bascule n'a eu lieu, manifest non modifié", file=sys.stderr)
        noter_campaign("red")
        return 1
    except Exception as exc:  # noqa: BLE001 — construction et écriture sont des incidents techniques
        print(f"incident de rapport : {type(exc).__name__}: {exc} — manifest non modifié",
              file=sys.stderr)
        noter_campaign("incident_report")
        return 3
    tous_ok = all(r.ok for r in resultats)
    if args.gate:
        prepares: list[tuple[Path, str]] = []
        # `False` tant que le gate n'est pas écrit : depuis la revue B3, un échec de publication
        # sort de la séquence **avant** `ecrire_gate`, et `gate_ok` doit alors se lire faux.
        gate_ecrit = False
        try:
            entry = ctx.index.corpus.manifest[args.gate]
            # Story 4.2b : `evals_ok` est fondé sur les décisions chiffrées — un booléen seul
            # s'écrase, une décision porte son plancher, son n et son run_digest.
            decisions = construire_decisions(
                resultats, cas, plancher=charge_plancher, repeat=args.repeat,
                run_digest=str(run_identity.get("run_digest", "")) if run_identity else "",
                producer=args.producer, decisions_orchestrateur=decisions_orchestrateur,
                exigences_full=exigences_full, structure=structure_lot, arbre=arbre_lot)
            # HIGH 1 : une liste de décisions vide serait un vert par vacuité (`all([])` est vrai).
            evals_ok = tous_ok and bool(decisions) and all(d.status == "green" for d in decisions)
            gate = construire_gate(entry, ctx, profil=args.profile, cas=cas,
                                   cases_dir=args.cases_dir, evals_ok=evals_ok, snapshot=snapshot,
                                   decisions=decisions,
                                   run_digest=(str(run_identity.get("run_digest", ""))
                                               if run_identity else None),
                                   plancher_digest=(charge_plancher.digest if exigences_full
                                                    else None),
                                   candidate_revision=args.candidate_revision,
                                   # L'empreinte des octets **que ce commit publiera**, tirée d'eux
                                   # et non d'un fichier déjà écrit : c'est ce rapport-là qu'on
                                   # pourra rouvrir pour vérifier les chiffres derrière `evals_ok`.
                                   report_digest=report_digest if exigences_full else None)
            # **Une seule opération de production, un seul commit** (tour correctif 3/3).
            #
            # Le rapport et sa table étaient publiés dans un premier atome, puis la publication et
            # le manifest dans un second : chaque atome était tout-ou-rien, l'opération ne l'était
            # pas — si le second échouait, `eval-results.*`, pourtant membres du lot, avaient déjà
            # changé. Tout est donc préparé ici, depuis les mêmes octets en mémoire, et remis en une
            # fois à l'unique pointeur atomique.
            #
            # Ce qui doit rester distinct le reste, et se décide **avant** ce commit : un rapport
            # inexploitable est refusé sans qu'aucune surface bouge, un incident technique sort en
            # code 3 sans gate écrit, et la non-mutation du dernier vert retire le manifest du lot
            # sans retirer la publication (un rouge est un résultat, publié — AC 2).
            publications: list[tuple[Path, str]] = []
            # **Tout ce qui lit une cible couverte lit sous le verrou** (revue du tour de racine
            # unique, constat 1). Deux lectures se faisaient dehors : le manifest entier, relu par
            # `preparer_gate`, et le `docs/evals/latest.md` précédent, relu pour décider son
            # archivage. Les deux remettaient ensuite à la bascule une décision prise sur un état
            # qu'une ingestion concurrente pouvait avoir remplacé — la mise à jour perdue, sur les
            # deux surfaces à la fois. La séquence entière — lire, fusionner, décider, publier —
            # vit donc dans une seule `EspacePublie.transaction()`, qui reste **un seul point de
            # commit** pour le lot complet.
            with espace.transaction() as transaction:
                if exigences_full:
                    publications = preparer_la_publication(
                        rapport, gate, ctx, cas,
                        data_dir=args.data_dir, output_markdown=output_markdown,
                        report_digest=report_digest,
                        candidate_revision=args.candidate_revision,
                        relecture_verdict=args.relecture_verdict,
                        relecture_images=args.relecture_images,
                        repo_root=args.data_dir.parent,
                        resoudre=transaction.chemin_publie,
                        preuve_externe=preuve_externe)
                    # Le journal du run porte la publication : c'est `preparer_la_publication` qui
                    # le rend, et il n'entre donc pas une seconde fois par `preparer_les_rapports`
                    # — deux contenus au même slot sont un refus, et ce serait le bon (une seule
                    # autorité).
                    prepares = [(output_json, contenu_rapport), *publications]
                else:
                    prepares = preparer_les_rapports(rapport, output_json, output_markdown,
                                                     preuve_externe=preuve_externe,
                                                     contenu_json=contenu_rapport)
                cibles = [cible for cible, _contenu in publications]
                # `None` quand la non-mutation du dernier vert s'applique : les publications sont
                # alors basculées quand même — un rouge est un **résultat**, publié —, et le
                # manifest ne bouge pas. C'est exactement ce que l'AC 2 demande, et la décision se
                # prend sur le manifest **réellement publié**, lu dans la transaction.
                prepare_gate = preparer_gate(args.data_dir / MANIFEST, args.gate, gate,
                                             lire=transaction.lire)
                if prepare_gate is not None:
                    prepares.append(prepare_gate)
                transaction.publier(prepares)
            prepares = []
            gate_ecrit = prepare_gate is not None
            print(f"rapports écrits : {output_json} ; {output_markdown}", file=sortie)
            if cibles:
                print("publication écrite : " + ", ".join(str(cible) for cible in cibles),
                      file=sortie)
        except RefusDeTourner as exc:
            print(f"refus : {exc}", file=sys.stderr)
            return 2
        except (RelectureInvalide, RapportInexploitable) as exc:
            # **Une donnée inexploitable est un refus dit, pas un incident technique** (revue B5) :
            # un rapport corrompu tombait dans `except Exception` — « incident de gate », code 3,
            # « manifest non modifié » —, trois affirmations dont la dernière seule était vraie, et
            # qui désignaient une panne là où le défaut est dans les données. Le même handler couvre
            # le verdict de seconde lecture qui ne se recoupe pas : les deux sont des refus de
            # publier, aucun n'a fait bouger le disque.
            publication_ok = False
            print(f"échec de publication : rapport ou seconde lecture inexploitable — {exc} — "
                  "aucune bascule n'a eu lieu, aucun gate n'a été écrit, le manifest est inchangé "
                  "et les rapports du run n'ont pas été écrits (un seul commit pour toute "
                  "l'opération)", file=sys.stderr)
        except (EspaceNonInstalle, LotHorsEspace, EspaceIllisible,
                ArchivePrecedenteIllisible) as exc:
            # **« Je n'ai pas pu lire » n'est pas « il n'y avait rien »** (revue B3/B6). L'échec
            # survient avant le premier `os.replace` : rien n'a bougé, et c'est vrai de le dire.
            # Publier quand même écraserait un état qu'on ne saurait ni archiver ni restaurer.
            publication_ok = False
            print(f"refus de publier : {exc} — aucune bascule n'a eu lieu, aucun gate n'a été "
                  "écrit, le manifest est inchangé et rien n'a été publié, rapports du run "
                  "compris", file=sys.stderr)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            # **FR41 : rien n'est promu si rien ne peut être publié** (revue B3, durcie par A).
            # L'échec survient pendant la **préparation**, donc avant la première bascule : aucun
            # gate n'est écrit, le manifest reste byte-identique, aucune surface n'a bougé, et les
            # temporaires sont effacés.
            #
            # Le code reste dans la ligne de partage d'AD-8/D4 : ni 2 (le run a bien tourné), ni 3
            # (qui promet « manifest non modifié » — vrai ici, mais 3 dit « incident technique »
            # alors que le verdict *a* été mesuré), ni 4. C'est donc **1**, le code des verdicts non
            # tenus.
            publication_ok = False
            print(f"échec de publication : {type(exc).__name__}: {exc} — aucun gate n'a été "
                  "écrit, le manifest est inchangé et rien n'a été publié, rapports du run "
                  "compris", file=sys.stderr)
        # **Il n'y a plus de handler d'« état mêlé », parce qu'il n'y a plus d'état mêlé** (story
        # 4.5, B7). `BasculePartielle` nommait les cibles laissées dans le nouvel état après une
        # restauration incomplète ; l'espace de publication ne restaure rien, parce qu'il ne modifie
        # rien avant l'unique `os.replace` qui publie tout le lot. Un meilleur signalement d'un état
        # mêlé n'était pas l'invariant : le rendre inatteignable l'est.
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
    code = 0 if tous_ok and gate_ok and publication_ok else 1
    noter_campaign("green" if code == 0 else "red")
    return code


if __name__ == "__main__":  # pragma: no cover — point d'entrée
    raise SystemExit(main())
