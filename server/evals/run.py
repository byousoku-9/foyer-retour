"""AD-14 / AD-7 — Le runner minimal des questions-témoins, et l'écriture du gate de manifest.

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
4. **écrit `manifest.gate`** quand `--gate {doc_id}` est demandé — l'unique endroit du projet qui le
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

Ce qui n'est **pas** ici et qui est refusé plutôt que simulé : le profil `full` (story 4.1), la suite
`parsing` (story 4.2), le cache de réponses, les baselines, le holdout, `docs/evals/latest.md` et
`GET /api/v1/evals/latest` (4.1/4.5).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import time
import traceback
from contextlib import ExitStack
from dataclasses import dataclass, field
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
from server.app.digests import cases_hash, pipeline_digest, prompts_digest
from server.app.domain.answer import Answer
from server.app.domain.errors import ErrorCode, PipelineError
from server.app.domain.ingest import Gate, GateContext, ManifestEntry
from server.app.domain.profil import Profil
from server.app.domain.question import Faits, Turn
from server.app.domain.trace import Trace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines import sinistre as pipeline_sinistre
from server.app.pipelines.guide import repondre_guide

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
PROFILS_LIVRES: tuple[str, ...] = ("vertical",)
PROFILS_DIFFERES: dict[str, str] = {"full": "4.1"}

TruthSource = Literal["lecture_humaine", "codex", "claude"]

# AD-16 : les échecs terminaux des étapes. Ce sont des **incidents**, pas des verdicts (D4).
CODES_INCIDENT: frozenset[ErrorCode] = frozenset({
    ErrorCode.timeout, ErrorCode.llm_unavailable, ErrorCode.llm_parse, ErrorCode.budget_exceeded,
    ErrorCode.corpus_unavailable, ErrorCode.internal,
})


class RefusDeTourner(Exception):
    """Le runner refuse de démarrer, ou de continuer, sur une faute d'appel — code 2, manifest intact."""


class IncidentTechnique(Exception):
    """Un incident pendant un cas — code 3, manifest intact : un incident n'est pas un verdict (D4)."""


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
        dossier = cases_dir.joinpath(*morceaux)
        if not dossier.is_dir():
            continue
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
    """Le profil et les suites non livrés sont refusés **explicitement**, jamais approximés.

    Le contrôle porte sur le profil **d'appel** et sur celui de **chaque cas**. Sans le second, un cas
    `profile: full` déposé dans `cases/guide/` était retiré sans un mot par le filtre de `main()` :
    le gate s'écrivait alors avec un `cases` et un `cases_hash` amputés, c'est-à-dire un golden set
    dont une partie ne tourne pas sans que personne le sache. C'est exactement l'amputation muette
    que les garde-fous « `.yml` » et « suite différée » existent pour empêcher, par une autre porte.
    """
    if profil in PROFILS_DIFFERES:
        raise RefusDeTourner(f"profil `{profil}` : story {PROFILS_DIFFERES[profil]}")
    if profil not in PROFILS_LIVRES:
        raise RefusDeTourner(f"profil `{profil}` inconnu (livré : {', '.join(PROFILS_LIVRES)})")
    for c in cas:
        if c.suite in SUITES_DIFFEREES:
            raise RefusDeTourner(f"suite `{c.suite}` : story {SUITES_DIFFEREES[c.suite]} (cas {c.id})")
        if c.profile in PROFILS_DIFFERES:
            raise RefusDeTourner(f"profil `{c.profile}` : story {PROFILS_DIFFERES[c.profile]} "
                                 f"(cas {c.id})")


# --- le jugement d'un cas (AD-14, D2) ------------------------------------------------------------

@dataclass
class Resultat:
    id: str
    suite: str
    label: str
    ecarts: list[str] = field(default_factory=list)
    cost_eur: float = 0.0
    ms: int = 0
    found: bool = False
    verdict: str | None = None

    @property
    def ok(self) -> bool:
        """Le gate exige `ok`, pas seulement le label (D2) : une attente inassouvie est un échec."""
        return not self.ecarts


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


def suite_du_document(settings: Settings, doc_id: str, *, cases_dir: Path = CASES_DIR) -> str:
    """La suite dont le pipeline sert ce document (D5).

    Le schéma de cas d'AD-14 ne porte **pas** de `doc_id`, et lui en ajouter un amenderait une
    définition partagée pour un besoin que cette story n'a pas : `--gate lux-guide` exécute donc la
    suite `guide`, `--gate axa-lu-optihome-2017` la suite `sinistre`. Le jour où un second contrat
    arrive (story 3.6), c'est cette fonction qui ne suffira plus — entrée différée.
    """
    if doc_id == settings.guide_doc_id:
        return "guide"
    if doc_id == settings.sinistre_doc_id:
        return "sinistre"
    suite_documentaire = cases_dir / "sinistre" / doc_id
    if suite_documentaire.is_dir():
        return f"sinistre/{doc_id}"
    raise RefusDeTourner(
        f"aucune suite ne sert le document {doc_id!r} (suite attendue : "
        f"{suite_documentaire.relative_to(cases_dir)}/)")


def document_de_la_suite(settings: Settings, suite: str) -> str:
    morceaux = Path(suite).parts
    if len(morceaux) == 2 and morceaux[0] == "sinistre":
        return morceaux[1]
    return settings.guide_doc_id if suite == "guide" else settings.sinistre_doc_id


async def executer_cas(cas: Cas, ctx: Contexte, *, doc_id: str,
                       budget_restant_eur: float) -> tuple[Answer, Trace, float]:
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
                                                 dictionnaire=ctx.dictionnaire, **commun)
        else:
            assert cas.faits is not None  # garanti par `Cas._coherence`
            answer, trace = await pipeline_sinistre.run(
                doc_id, cas.question, cas.faits,
                dictionnaire=ctx.dictionnaires.get(doc_id), **commun)
    except PipelineError as exc:
        # Une erreur terminale peut survenir après un ou plusieurs appels facturés. Le pipeline
        # attache sa trace partielle à l'erreur ; le budget reste l'autorité de coût même si cette
        # trace n'a pas encore été assemblée. Le runner conserve les deux faits sur l'exception afin
        # que l'incident ne retombe jamais artificiellement à 0,0000 €.
        exc.eval_cost_eur = round(budget.cost_eur, 4)
        if exc.trace is not None:
            exc.trace.total_cost_eur = round(budget.cost_eur, 4)
        raise
    except Exception as exc:
        # Une faute interne n'a pas la couture ``PipelineError.trace`` mais elle peut survenir après
        # un appel facturé. Ne pas la laisser sortir brute (ni remettre son coût à zéro) : le runner
        # la convertira en incident, sans inventer de verdict.
        raise _ErreurInterneFacturee(type(exc).__name__, round(budget.cost_eur, 4)) from exc
    return answer, trace, round(budget.cost_eur, 4)


async def executer(cas: list[Cas], ctx: Contexte, *, max_cost_eur: float,
                   sortie: Any = sys.stdout) -> list[Resultat]:
    """Exécute les cas dans l'ordre, en s'arrêtant **avant** le cas qui dépasserait le plafond de run."""
    resultats: list[Resultat] = []
    cumul = 0.0
    for c in cas:
        restant = round(max_cost_eur - cumul, 4)
        if restant <= 0:
            raise IncidentTechnique(
                f"plafond de run atteint ({cumul:.4f} € sur {max_cost_eur:.4f} €) avant le cas "
                f"{c.id} : {len(cas) - len(resultats)} cas non exécutés")
        doc_id = c.doc_id or document_de_la_suite(ctx.settings, c.suite)
        depart = time.monotonic()
        try:
            answer, _trace, cout = await executer_cas(c, ctx, doc_id=doc_id, budget_restant_eur=restant)
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
                    f"{max_cost_eur:.4f} €) : {exc.message}") from exc
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
        resultat = Resultat(id=c.id, suite=c.suite, label=label, ecarts=ecarts, cost_eur=cout,
                            ms=int((time.monotonic() - depart) * 1000), found=answer.found,
                            verdict=answer.verdict.value if answer.verdict is not None else None)
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


# --- le gate (AD-7) --------------------------------------------------------------------------------

def construire_gate(entry: ManifestEntry, ctx: Contexte, *, profil: str, cas: list[Cas],
                    cases_dir: Path, evals_ok: bool) -> Gate:
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
    fichiers = [c.case_path or (cases_dir / c.suite / f"{c.id}.yaml") for c in cas]
    return Gate(
        profile=profil, source_hash=entry.source_hash,
        ingest_fingerprint=entry.ingest_fingerprint, overlay_hash=entry.overlay_hash,
        cases_hash=cases_hash(fichiers, cases_dir), cases=len(cas),
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
                        pile: Any = None) -> Contexte:
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
    return Contexte(settings=settings, index=Index(corpus), client=LlmClient(settings),
                    pipeline_digest_hex=contexte_gate.pipeline_digest,
                    prompts_digest_hex=contexte_gate.prompts_digest,
                    # Lu dans `data_dir`, pas dans `lecture` : `_sans_gate_sur_disque` ne recopie que
                    # le manifest, et le dictionnaire n'a rien à voir avec le gate qu'on refait.
                    dictionnaire=load_dictionary(data_dir, corpus, settings.guide_doc_id),
                    dictionnaires=dictionnaires)


async def _fermer(client: Any) -> None:
    """Ferme le pool de connexions du client, si c'en est un (les tests passent un double)."""
    fermer = getattr(client, "aclose", None)
    if fermer is not None:
        await fermer()


async def _executer_puis_fermer(cas: list[Cas], ctx: Contexte, *, gate: str | None,
                                max_cost_eur: float, sortie: Any) -> list[Resultat]:
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
        return await executer(cas, ctx, max_cost_eur=max_cost_eur, sortie=sortie)
    finally:
        await _fermer(ctx.client)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server.evals.run",
        description="Questions-témoins (AD-14), version minimale bornée de la story 1.10.")
    p.add_argument("--suite", choices=sorted(SUITES_LIVREES) + sorted(SUITES_DIFFEREES),
                   help="n'exécuter que cette suite (défaut : toutes les suites livrées)")
    p.add_argument("--case", dest="cas", help="n'exécuter que ce cas (son identifiant)")
    p.add_argument("--profile", default="vertical",
                   help="profil de gate exigé des cas exécutés (livré : vertical)")
    p.add_argument("--gate", metavar="DOC_ID",
                   help="écrire `manifest.gate` pour ce document depuis la suite qui le sert")
    p.add_argument("--max-cost", type=float, default=None,
                   help="plafond de coût du run en euros (défaut : evals_max_cost_eur de config.py)")
    p.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    p.add_argument("--data-dir", type=Path, default=DATA_DIR)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sortie = sys.stdout
    try:
        settings = Settings()
        max_cost = settings.evals_max_cost_eur if args.max_cost is None else args.max_cost
        if not math.isfinite(max_cost) or max_cost <= 0:
            # `argparse(type=float)` accepte « inf » et « nan », et `nan <= 0` est **faux** : sans
            # `isfinite`, `--max-cost inf` ou `--max-cost nan` neutralisait le plafond et le run
            # partait sans borne, contre AD-9 et contre CLAUDE.md (« la clé **et un plafond** »).
            raise RefusDeTourner(f"plafond de run {max_cost} € : les évals tournent avec un plafond "
                                 "fini et strictement positif (CLAUDE.md, AD-9)")
        # 1. Le profil, avant tout le reste : `--profile full` se refuse sans avoir rien lu.
        if args.profile in PROFILS_DIFFERES:
            raise RefusDeTourner(f"profil `{args.profile}` : story {PROFILS_DIFFERES[args.profile]}")
        if args.profile not in PROFILS_LIVRES:
            raise RefusDeTourner(f"profil `{args.profile}` inconnu (livré : {', '.join(PROFILS_LIVRES)})")
        # 2. La clé, **avant tout chargement de corpus** (AD-14).
        if cle_absente(settings):
            raise RefusDeTourner("les évals exigent une clé : ANTHROPIC_API_KEY est vide ou absente "
                                 "(AD-14 — les unitaires passent sans clé, les évals non)")
        # 3. Les cas, avant tout appel facturé.
        suites = None
        if args.gate and args.cas:
            # `--gate` écrit un gate **de suite** : `cases` et `cases_hash` désignent ce qui a été
            # exécuté. Filtré par `--case`, il affirmerait `evals_ok: true` pour le document entier
            # sur un cas choisi à la main, et `tests/test_digests.py` le verrait — mais après coup.
            raise RefusDeTourner("--gate et --case sont exclusifs : un gate se réclame de la suite "
                                 "qui sert le document, jamais d'un cas choisi")
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
        cas = [c for c in cas if c.profile == args.profile]
        if args.cas:
            cas = [c for c in cas if c.id == args.cas]
            if not cas:
                raise RefusDeTourner(f"aucun cas {args.cas!r} au profil {args.profile}")
        if not cas:
            raise RefusDeTourner(f"aucun cas au profil {args.profile} "
                                 f"dans {args.cases_dir}{' (suites ' + ', '.join(suites) + ')' if suites else ''}")
        # 4. Le corpus, puis le document visé par `--gate`.
        with ExitStack() as pile:
            ctx = construire_contexte(settings, args.data_dir, regate=args.gate, pile=pile)
            # Le client (donc un pool de connexions TLS) est construit par `construire_contexte`.
            # Le refus « document non servi », l'exécution et la fermeture tiennent dans **un seul**
            # `asyncio.run` : une fermeture sur une autre boucle que celle qui a servi le client
            # lève `Event loop is closed` et fait sortir en code 3 un run par ailleurs vert, sans
            # écrire le gate. Voir `_executer_puis_fermer`.
            resultats = asyncio.run(_executer_puis_fermer(
                cas, ctx, gate=args.gate, max_cost_eur=max_cost, sortie=sortie))
    except RefusDeTourner as exc:
        print(f"refus : {exc}", file=sys.stderr)
        return 2
    except IncidentTechnique as exc:
        # AD-16 : l'échec est terminal et **dit**. Le manifest n'a pas été touché.
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
    resume(resultats, max_cost_eur=max_cost, sortie=sortie)
    tous_ok = all(r.ok for r in resultats)
    if args.gate:
        try:
            entry = ctx.index.corpus.manifest[args.gate]
            gate = construire_gate(entry, ctx, profil=args.profile, cas=cas,
                                   cases_dir=args.cases_dir, evals_ok=tous_ok)
            ecrire_gate(args.data_dir / MANIFEST, args.gate, gate)
        except RefusDeTourner as exc:
            print(f"refus : {exc}", file=sys.stderr)
            return 2
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
