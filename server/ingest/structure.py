"""Story 4.2c — le modèle propose une hiérarchie sur des lignes source, le code la prouve.

    ANTHROPIC_API_KEY= uv run python -m server.ingest.structure --dry-run
    uv run python -m server.ingest.structure <doc_id> [--data data] [--max-cost 5]

La proposition ne désigne que des lignes du registre figé par `pdf_to_blocks` et des liens
parent/enfant. Elle ne porte **aucun texte** : les titres servis sont relus dans le registre à
partir de la ligne désignée, et les `node_id` sont un chemin positionnel calculé par le code.

Le modèle référence une ligne par son **index entier** dans le segment reçu — `0..N-1` pour les
lignes à couvrir, `N..N+k-1` pour les ancres de frontière —, jamais par son empreinte. Une grammaire
contrainte ne peut pas énumérer des empreintes de 72 caractères : l'API refuse le schéma (400
« Schema is too complex », mesuré dès 70 uid énumérés, et « Enum must be a non-empty array » pour un
segment sans ancre). `rapprocher_indices` traduit chaque index en `line_uid` avant tout usage et
refuse par un motif nommé un index hors plage, hors domaine ou répété ; `parse_proposition`
revérifie ensuite les `uid` obtenus « même si le schéma l'impose » (idiome
`type_clauses.parse_reading`). Le contrat de sortie change, le contrat d'artefact non :
`structure.json`, la couture et le vérificateur restent en `line_uid`.

L'appel suit la convention LLM du spine : `messages.parse(..., output_config={"format": …})`
**sans** `output_format`, puis validation locale par `TypeAdapter`. Avec `output_format`, le SDK
1.0.0 valide le texte avant de rendre la réponse et lève `ValidationError` : `usage`, `stop_reason`
et le texte reçu seraient perdus — donc le coût réel et le motif du refus. Le corps envoyé est
identique sur le fil.

`verifier()` est en code pur, hors réseau et **fail-closed** : un refus est nommé, il n'est jamais
rattrapé par un repli silencieux vers l'heuristique numérique (AD-16). Sans `structure.json`,
l'heuristique reste le chemin nominal ; **avec** un `structure.json` refusé — ou seulement présent
et illisible, un répertoire, un lien pendant —, le document part en quarantaine.

La proposition est un **artefact**, pas un appel à chaud : AD-2 exige que `source_hash` +
`ingest_fingerprint` égaux rendent les mêmes identifiants, et rejouer le modèle à chaque ingestion
rendrait l'arbre instable. Ce module écrit `data/{doc_id}/structure.json` une fois, hors ligne ;
`pdf_to_blocks.run()` le relit et le revérifie à chaque ingestion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import (BaseModel, ConfigDict, Field, TypeAdapter, ValidationError,
                      field_validator, model_validator)

from server.app.config import REPO_ROOT, Settings, cle_absente, get_settings
from server.app.corpus.racine import racine_couvrant
from server.app.domain.artifact import document_artifact_uid
from server.app.domain.document import (DOC_ID_MAX, DOC_ID_RE, NodeRelation,
                                        NodeRelationKind, SurfaceClass)
from server.app.domain.trace import Usage
from server.app.llm.models import EFFORT, MODEL_CAPS, TIERS
from server.app.llm.audit import append_ingest_audit
from server.app.llm.pricing import cost_from_usage, estimate_cost_from_token_bounds
from server.ingest.artifacts import exiger_espace_installe, write_atomic
from server.ingest.line_identity import line_uid

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
TIER = "ingest"
MODEL = TIERS[TIER]
# `-4` : l'unité de portage est devenue une donnée du registre, et le vérificateur refuse lui-même
# une proposition qui la scinde ou qui en fait deux titres distincts — ce que seul `build_document`
# refusait. `-3` : la largeur de l'arbre est bornée à son tour (`structure_max_nodes`,
# `structure_max_children`). `-2` : la couverture est devenue totale et les lignes d'une table sont
# entrées au registre. Une proposition acceptée sous les règles précédentes ne l'est plus forcément,
# et le registre qu'elle adresse a changé : l'empreinte doit le dire (AD-2, lu par le loader).
STRUCTURE_RULES_VERSION = "5.0-v4-semantic-oracle"
NORMAL_STOPS = frozenset({"end_turn", "stop_sequence", "tool_use"})
# Vocabulaire fermé des refus : un motif qui n'est pas là ne peut pas sortir du vérificateur.
MOTIFS = ("proposition_illisible", "proposition_vide", "document_different", "ligne_inconnue",
          "index_invalide", "titre_duplique", "titre_ambigu", "cycle", "profondeur_excessive",
          "largeur_excessive", "ordre_impossible", "intervalles_croises", "parent_non_contenant",
          "ligne_omise", "affectation_non_prouvee", "noeud_non_construit")
# `line-v1:` + SHA-256 hexadécimal. La borne empêche toujours un artefact de concentrer son poids
# dans un seul identifiant, mais elle décrit désormais la même identité que `Document.Line`.
LINE_UID_MAX = len("line-v1:") + 64
# Les ancres sont un voisinage de frontière, jamais une copie du registre. La moitié regarde en
# arrière (parents/continuations), l'autre en avant (relations). Cette constante structurelle borne
# à la fois payload et enum sans ouvrir un levier corpus dans la configuration produit.
MAX_BOUNDARY_ANCHORS = 32

_ARTICLE_TITLE_RE = re.compile(
    r"^\s*(?:article|art\.?|artikel|articulo|artigo|articolo)\s+"
    r"(?P<number>(?:\d+|[ivxlcdm]+)(?:[.\-][0-9a-z]+)*)\b",
    re.IGNORECASE,
)
_TECHNICAL_SURFACES: tuple[tuple[re.Pattern[str], SurfaceClass], ...] = (
    (re.compile(
        r"^(?:table\s+des\s+matieres|sommaire|table\s+of\s+contents|contents|"
        r"inhaltsverzeichnis|indice(?:\s+general)?|sumario|sommario|inhoudsopgave)\b"),
     "table_des_matieres"),
    (re.compile(
        r"^(?:preambule|preamble|praambule|preambolo|vorwort|foreword|avant-propos|"
        r"introduction|dispositions?\s+preliminaires?|preliminary\s+provisions?)\b"),
     "preliminaire"),
)


def _oracle_text(value: str) -> str:
    return " ".join(
        "".join(char for char in unicodedata.normalize("NFKD", value)
                if not unicodedata.combining(char)).casefold().split()
    )


def oracle_surface_class(title: str, supporting_texts: Sequence[str] = ()) -> SurfaceClass | None:
    """Classe de surface prouvée localement, indépendante de la proposition Opus.

    Les classes techniques sont reconnues dans plusieurs langues. Une surface substantielle doit
    porter soit une identité d'article lisible, soit du texte de corps observable ; un intitulé
    isolé sans indice reste inconnu et ferme le gate.
    """
    normalized = _oracle_text(title)
    technical = next((surface for pattern, surface in _TECHNICAL_SURFACES
                      if pattern.search(normalized)), None)
    if technical is not None:
        return technical
    if _ARTICLE_TITLE_RE.search(normalized):
        return "substantiel"
    evidence = [text.strip() for text in supporting_texts if text.strip()]
    if evidence or title.rstrip().endswith((".", ";", ":", "?", "!")):
        return "substantiel"
    return None


def oracle_article_uid(title: str) -> str | None:
    """Identité canonique uniquement lorsqu'un numéro d'article est réellement lisible."""
    match = _ARTICLE_TITLE_RE.search(_oracle_text(title))
    return f"article:{match.group('number')}" if match else None


def _lignes_de_lintervalle(noeud: NoeudPropose,
                           registre: dict[str, Entree]) -> list[Entree]:
    """Lignes du registre couvertes par l'intervalle du nœud, dans l'ordre de lecture."""
    first = registre[noeud.premiere_line_uid].ordre
    last = registre[noeud.derniere_line_uid].ordre
    return sorted((entry for entry in registre.values() if first <= entry.ordre <= last),
                  key=lambda entry: entry.ordre)


def surface_de_provenance(noeud: NoeudPropose, registre: dict[str, Entree]) -> SurfaceClass | None:
    """Classe technique prouvée par la **provenance** de toutes les lignes de l'intervalle.

    Toutes, sans exception : une seule ligne de corps dans l'intervalle et le nœud n'est plus une
    surface technique. C'est la même règle que celle qui rendra ces lignes non citables — appeler
    `preliminaire` un intervalle que la porte de lecture publiera citable serait la contradiction
    que ce chemin corrige.
    """
    lues = {entry.surface_provenance for entry in _lignes_de_lintervalle(noeud, registre)}
    return lues.pop() if len(lues) == 1 else None


def _lignes_lues(noeud: NoeudPropose, registre: dict[str, Entree], limite: int = 6) -> str:
    """Ce que l'oracle a **lu**, rendu au lecteur : index dans le registre, page, texte tronqué.

    Un refus de surface qui ne dit que la classe attendue et la classe reçue laisse chercher dans
    un contrat de plusieurs milliers de lignes lesquelles il a jugées.
    """
    rangs = {entry.uid: index for index, entry
             in enumerate(sorted(registre.values(), key=lambda item: item.ordre))}
    lignes = _lignes_de_lintervalle(noeud, registre)
    rendu = [
        f"[{rangs[entry.uid]}] p.{entry.page} "
        f"{entry.surface_provenance or 'corps'} « {entry.titre.strip()[:60]} »"
        for entry in lignes[:limite]
    ]
    if len(lignes) > limite:
        rendu.append(f"… {len(lignes) - limite} ligne(s) de plus")
    return " ; ".join(rendu)


def _semantique_locale(noeud: NoeudPropose, registre: dict[str, Entree]) -> tuple[str, SurfaceClass | None]:
    """Titre relu et classe prouvée sur l'intervalle, communs aux artefacts v1 et v2.

    **Une seule source pour la surface technique.** La provenance décide la première : elle vient
    de la porte de lecture, qui est aussi ce qui rendra ces lignes citables ou non. Ce n'est
    qu'ensuite, sur du corps, que les mots du titre sont lus. Dans l'autre ordre, une couverture
    (« HOME / Conditions générales / CG-… / Particuliers ») ne portait aucun mot technique, l'oracle
    la déclarait `substantiel` faute de mieux — et refusait la seule bonne réponse.
    """
    title_uids = noeud.title_line_uids or [noeud.titre_line_uid]
    title = " ".join(registre[uid].titre.strip() for uid in title_uids).strip()
    provenance = surface_de_provenance(noeud, registre)
    if provenance is not None:
        return title, provenance
    first = registre[noeud.premiere_line_uid].ordre
    last = registre[noeud.derniere_line_uid].ordre
    title_set = set(title_uids)
    supporting_texts = [
        entry.titre for uid, entry in registre.items()
        if first <= entry.ordre <= last and uid not in title_set
    ]
    return title, oracle_surface_class(title, supporting_texts)


def _candidates_ancres(registre: dict[str, Entree]) -> tuple[AncreStructure, ...]:
    """Catalogue générique interne des lignes pouvant raisonnablement intituler un nœud.

    Le catalogue est dérivé avant API, uniquement de la forme locale d'une ligne : article ou
    surface technique reconnue, ou intitulé bref qui ne ressemble pas à une phrase de corps
    terminée. Une erreur par excès ne publie rien d'elle-même : la cible devra encore être un vrai
    ``titre_line_uid`` de la proposition globale. Une erreur par défaut interdit seulement l'arête
    transfrontalière et reste donc fail-closed.
    """
    anchors: list[AncreStructure] = []
    for entry in sorted(registre.values(), key=lambda item: item.ordre):
        title = entry.titre.strip()
        normalized = _oracle_text(title)
        technical = any(pattern.search(normalized) for pattern, _surface in _TECHNICAL_SURFACES)
        looks_like_heading = (
            bool(title)
            and len(title) <= 160
            and not title.endswith((".", ";", "?", "!"))
        )
        if oracle_article_uid(title) is not None or technical or looks_like_heading:
            anchors.append(AncreStructure(line_uid=entry.uid, ordre=entry.ordre, titre=entry.titre))
    return tuple(anchors)


def _ancres_frontiere(registre: dict[str, Entree], line_uids: Sequence[str], *,
                      candidates: Sequence[AncreStructure] | None = None,
                      ) -> tuple[AncreStructure, ...]:
    """Voisinage externe borné d'un segment, sans dupliquer ses lignes locales.

    Les candidates sont dérivées du registre entier mais seules les plus proches de chaque côté de
    la frontière sont sérialisées. La taille reste donc O(1) par segment, même si chaque ligne d'un
    document dense ressemble à un titre.
    """
    owned = set(line_uids)
    orders = [registre[uid].ordre for uid in line_uids]
    first, last = min(orders), max(orders)
    candidates = _candidates_ancres(registre) if candidates is None else candidates
    half = MAX_BOUNDARY_ANCHORS // 2
    before = [anchor for anchor in candidates if anchor.ordre < first and anchor.line_uid not in owned]
    after = [anchor for anchor in candidates if anchor.ordre > last and anchor.line_uid not in owned]
    return tuple((*before[-half:], *after[:MAX_BOUNDARY_ANCHORS - half]))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RelationProposee(StrictModel):
    kind: NodeRelationKind
    target_line_uid: str = Field(max_length=LINE_UID_MAX)


class ContinuationFrontiereProposee(StrictModel):
    """Préfixe local qui prolonge un titre d'un segment antérieur sans en inventer un nouveau."""

    premiere_line_uid: str = Field(max_length=LINE_UID_MAX)
    derniere_line_uid: str = Field(max_length=LINE_UID_MAX)
    target_line_uid: str = Field(max_length=LINE_UID_MAX)


class NoeudPropose(StrictModel):
    """Nœud proposé ; les champs v2 portent la vérité sémantique à projeter."""

    titre_line_uid: str = Field(max_length=LINE_UID_MAX)
    premiere_line_uid: str = Field(max_length=LINE_UID_MAX)
    derniere_line_uid: str = Field(max_length=LINE_UID_MAX)
    parent_line_uid: str | None = Field(default=None, max_length=LINE_UID_MAX)
    title_line_uids: list[str] = Field(default_factory=list)
    article_uid: str | None = Field(default=None, max_length=256)
    surface_class: SurfaceClass | None = None
    continuation_line_uids: list[str] = Field(default_factory=list)
    relations: list[RelationProposee] = Field(default_factory=list)

    @model_validator(mode="after")
    def _listes_uniques(self) -> NoeudPropose:
        for name, values in (
            ("title_line_uids", self.title_line_uids),
            ("continuation_line_uids", self.continuation_line_uids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} contient un doublon")
        relation_keys = [(relation.kind, relation.target_line_uid) for relation in self.relations]
        if len(relation_keys) != len(set(relation_keys)):
            raise ValueError("relations contient un doublon")
        return self


def _trop_de_noeuds(total: int, settings: Settings) -> str | None:
    """Le compte total, jugé en O(1) — avant même de parcourir les nœuds une première fois."""
    if total > settings.structure_max_nodes:
        return (f"{total} nœud(s) proposé(s) > borne STRUCTURE_MAX_NODES="
                f"{settings.structure_max_nodes}")
    return None


def _fratrie_trop_large(fratries: Counter[Any], settings: Settings) -> str | None:
    """La plus large fratrie, racines comprises (clé `None`).

    Une proposition « plate » de N nœuds sans parent est une largeur de N : compter par parent
    déclaré **sans** les racines laisserait passer exactement le cas le plus simple à fabriquer.
    """
    if not fratries:
        return None
    parent, largeur = fratries.most_common(1)[0]
    if largeur > settings.structure_max_children:
        return (f"{largeur} enfant(s) sous {'la racine' if parent is None else repr(parent)} > "
                f"borne STRUCTURE_MAX_CHILDREN={settings.structure_max_children}")
    return None


def largeur_hors_borne(noeuds: Sequence[NoeudPropose], settings: Settings) -> str | None:
    """Détail du dépassement de largeur, ou `None` si la proposition tient dans ses deux bornes.

    Une seule règle pour les cinq points où elle s'applique — schéma fournisseur, modèle Pydantic,
    parse local, chargement depuis le disque, vérificateur —, si bien qu'aucun d'eux ne peut dériver
    d'un autre. Le compte total est jugé le premier : il refuse sans rien parcourir.
    """
    return (_trop_de_noeuds(len(noeuds), settings)
            or _fratrie_trop_large(Counter(noeud.parent_line_uid for noeud in noeuds), settings))


def _largeur_brute(charge: Any, settings: Settings) -> str | None:
    """La même largeur, lue sur la charge **brute** d'un `structure.json`, avant tout modèle bâti.

    Compter avant de construire est ce qui permet au motif dédié de sortir : bâtir d'abord ferait
    rendre `proposition_illisible` par le modèle, un motif qui ne dit pas ce qui est en cause. Une
    forme aberrante n'est pas jugée ici — c'est l'affaire du modèle —, seule la largeur l'est.
    """
    noeuds = charge.get("noeuds") if isinstance(charge, dict) else None
    if not isinstance(noeuds, list):
        return None
    trop = _trop_de_noeuds(len(noeuds), settings)
    if trop is not None:
        return trop  # le compte suffit : la liste n'est même pas parcourue
    fratries: Counter[Any] = Counter()
    for noeud in noeuds:
        parent = noeud.get("parent_line_uid") if isinstance(noeud, dict) else None
        # Un parent qui n'est ni une chaîne ni `null` est hors schéma : il est compté à part, sous une
        # clé qui ne peut pas être un `uid`, et le modèle le refusera juste après.
        fratries[parent if parent is None or isinstance(parent, str) else "?"] += 1
    return _fratrie_trop_large(fratries, settings)


class StructureProposee(StrictModel):
    schema_version: Literal["1", "2"] = "2"
    doc_id: str = Field(max_length=DOC_ID_MAX)
    noeuds: list[NoeudPropose] = Field(default_factory=list)
    # Transport segmentaire uniquement. La couture doit les consommer toutes ; elles ne sont jamais
    # sérialisées dans l'artefact public final.
    continuations_frontiere: list[ContinuationFrontiereProposee] = Field(
        default_factory=list, exclude=True)

    @field_validator("noeuds")
    @classmethod
    def _borner_la_largeur(cls, noeuds: list[NoeudPropose]) -> list[NoeudPropose]:
        """La forme reste indépendante du singleton global ; chaque frontière applique ses Settings.

        ``parse_proposition``, ``charger_octets``, ``verifier`` et ``arbre`` portent tous la borne
        effective. La figer ici via ``get_settings()`` faisait refuser une proposition valide à un
        appelant qui avait explicitement fourni une borne plus large.
        """
        return noeuds

    @model_validator(mode="after")
    def _v2_complete(self) -> StructureProposee:
        if self.schema_version != "2":
            return self
        for node in self.noeuds:
            required = {
                "title_line_uids", "article_uid", "surface_class",
                "continuation_line_uids", "relations",
            }
            missing = required - node.model_fields_set
            if missing:
                raise ValueError(f"nœud v2 incomplet : champs absents {sorted(missing)}")
            if not node.title_line_uids or node.titre_line_uid not in node.title_line_uids:
                raise ValueError("title_line_uids doit contenir titre_line_uid")
            if node.surface_class is None:
                raise ValueError("surface_class obligatoire en v2")
        return self


class PropositionFilaire(StrictModel):
    """Forme filaire exacte d'une réponse segmentaire, nœuds et continuations seulement.

    Le modèle n'écrit ni `schema_version`, ni `doc_id` : les deux sont posés par le code. Ce modèle
    dédié est la validation locale qu'impose la convention LLM du spine — celle que `output_format`
    ferait faire au SDK, au prix d'`usage`, de `stop_reason` et du texte reçu.
    """

    noeuds: list[NoeudPropose]
    continuations_frontiere: list[ContinuationFrontiereProposee] = Field(
        default_factory=list, max_length=1)


_FILAIRE: TypeAdapter[PropositionFilaire] = TypeAdapter(PropositionFilaire)


@dataclass(frozen=True)
class Entree:
    """Ce que le modèle voit d'une ligne : sa position et son texte, en lecture seule.

    `texte` est le texte **du registre**, immuable. `texte_porte` est celui de la ligne de travail
    qui porte cet uid : les deux ne diffèrent que lorsque `_merge_number_lines` a réuni un numéro et
    son intitulé, et c'est alors `texte_porte` qui dit ce qu'un lecteur voit — donc le titre servi.

    `source_uids` garde les ancres d'extraction internes qui composent la ligne portée. Pour le
    contrat v2, `uid` est l'identité `line-v1` de cette ligne et `unite` reste vide : exactement la
    valeur qui sera persistée. Le lecteur v1 historique conserve ses entrées positionnelles et
    utilise `unite` pour grouper les fragments que le même bloc porte nécessairement ensemble.
    """

    uid: str
    page: int
    colonne: int
    ordre: int  # rang dans l'ordre de lecture du document entier, 1-indexé
    bbox: tuple[float, float, float, float]
    texte: str
    texte_porte: str = ""
    unite: str = ""
    source_uids: tuple[str, ...] = ()
    # Classe technique décidée par la **porte de lecture** (`pdf_to_blocks.surfaces_de_provenance`),
    # ou `None` pour une ligne de corps. C'est la même règle qui rendra ces lignes non citables dans
    # le document publié : la preuve locale d'un nœud `preliminaire` ou `table_des_matieres` s'y
    # appuie plutôt que de redevinner la notion à partir de mots de titre.
    surface_provenance: SurfaceClass | None = None

    @property
    def titre(self) -> str:
        return self.texte_porte or self.texte

    @property
    def portage(self) -> str:
        """Identifiant de l'unité indivisible dont cette entrée fait partie — elle-même par défaut."""
        return self.unite or self.uid


@dataclass(frozen=True)
class Verdict:
    accepte: bool
    motif: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class AncreStructure:
    """Titre source candidat exposé uniquement pour une couture parent/relation."""

    line_uid: str
    ordre: int
    titre: str


@dataclass(frozen=True)
class SegmentStructure:
    """Un appel du plan, figé avant la première soumission fournisseur."""

    index: int
    segment_uid: str
    line_uids: tuple[str, ...]
    anchors: tuple[AncreStructure, ...]
    request: dict[str, Any]
    request_chars: int
    input_tokens_majorant: int
    majorant_eur_brut: float
    majorant_eur: float


@dataclass(frozen=True)
class PlanStructure:
    """Partition bijective du registre et enveloppes exactes déjà chiffrées."""

    plan_uid: str
    registry_hash: str
    segments: tuple[SegmentStructure, ...]
    majorant_eur: float


@dataclass(frozen=True)
class ExecutionStructure:
    """Proposition globale et compteurs cumulés de tous les appels du run."""

    proposition: StructureProposee
    plan: PlanStructure
    usage: Usage


@dataclass(frozen=True)
class NoeudVerifie:
    """Nœud dérivé d'une proposition prouvée : son `node_id` et son titre viennent du code."""

    node_id: str
    level: int
    title: str
    premiere: int
    derniere: int
    parent_id: str | None
    article_uid: str | None = None
    surface_class: SurfaceClass = "substantiel"
    continuation_line_uids: tuple[str, ...] = ()
    relations: tuple[NodeRelation, ...] = ()


class StructureRefusee(ValueError):
    """Refus nommé d'une proposition. Bloquant côté rapport, jamais un repli (AD-16)."""

    def __init__(self, motif: str, detail: str) -> None:
        super().__init__(f"{motif} : {detail}")
        self.motif = motif
        self.detail = detail


def empreinte_proposition(proposition: StructureProposee | None) -> str:
    """Digest de la proposition **effectivement appliquée**, ou le marqueur de son absence.

    AD-2 : « mêmes `source_hash` et `ingest_fingerprint` ⇒ mêmes IDs », et le loader n'a que ces deux
    valeurs. Sans ce digest dans l'empreinte, le même PDF rendait `{doc}:a1…` ou `{doc}:s1…` selon la
    présence d'un `structure.json` sans qu'aucune des deux valeurs ne bouge.
    """
    if proposition is None:
        return "absente"
    encoded = json.dumps(proposition.model_dump(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _porteurs(page: Any) -> list[tuple[list[str], int, str, tuple[float, float, float, float], int]]:
    """Porteurs d'une page, avec les données exactes qui seront persistées dans `Document.Line`.

    Une table sert son contenu dans un bloc `table` : les lignes brutes qu'elle a absorbées sont donc
    portées, pas retirées, et doivent figurer au registre présenté au proposant — **à la position de
    la table dans l'ordre de lecture**, la seule qui rende un intervalle proposable autour d'elle.
    Les lignes gardent leur ordre exact (`pdf_to_blocks._assign_reading_order` l'a arrêté) ; chaque
    table est insérée devant la première ligne que sa clé de lecture précède, la clé même dont
    `_segment_page` se sert pour replacer la table entre les groupes.
    """
    porteurs = [(line.source_uids, line.colonne, line.text, tuple(line.bbox), line.ordre_lecture)
                for line in page.lines]
    cles = [(line.bande, line.colonne, line.bbox[1], line.bbox[0]) for line in page.lines]
    layout = page.layout
    tables = sorted((table for table in page.tables if table.sert_un_bloc),
                    key=lambda table: (layout.bande(table.bbox[1]), layout.colonne(table.bbox),
                                       table.bbox[1], table.bbox[0]))
    for decalage, table in enumerate(tables):
        cle = (layout.bande(table.bbox[1]), layout.colonne(table.bbox), table.bbox[1], table.bbox[0])
        position = sum(1 for autre in cles if autre < cle)
        row_height = max((table.bbox[3] - table.bbox[1]) / max(len(table.rows), 1), 1.0)
        bbox = table.row_bboxes[0] if table.row_bboxes else [
            table.bbox[0], table.bbox[1], table.bbox[2],
            round(min(table.bbox[1] + row_height, table.bbox[3]), 2),
        ]
        text = " | ".join(table.rows[0])
        porteurs.insert(position + decalage, (
            # Le bloc table persiste sa première rangée comme ancre atomique d'ordre 1. Garder le
            # sentinel 0 ici faisait diverger le line_uid Opus de la Document.Line réellement servie.
            table.source_uids, layout.colonne(table.bbox), text, tuple(bbox), 1,
        ))
    return porteurs


def registre_lignes(pages: list[Any], *, document_uid: str | None = None) -> dict[str, Entree]:
    """Registre adressable : les lignes source qu'un bloc porte, dans l'ordre de lecture.

    Les lignes écartées par un motif de retrait explicite (bande récurrente) n'y figurent pas : leur
    contenu n'est servi nulle part, elles ne sont donc l'ancre de rien, et une proposition qui les
    désignerait serait refusée. Les lignes absorbées par une table y figurent au contraire : leur
    texte **est** servi, par le bloc `table`.
    L'ordre de lecture doit avoir été arrêté (`pdf_to_blocks.ordonner_pages`) avant l'appel.
    """
    # Import tardif : `pdf_to_blocks` dépend de ce module pour vérifier une proposition, l'importer
    # au chargement fermerait le cycle. La provenance vient de **là-bas** et de nulle part ailleurs.
    from server.ingest.pdf_to_blocks import surfaces_de_provenance
    provenances = surfaces_de_provenance(pages)

    def _provenance(uids: Sequence[str]) -> SurfaceClass | None:
        """Classe technique d'un porteur : celle de ses lignes, **si elles s'accordent toutes**.

        Un porteur à cheval sur la frontière du dernier groupe technique et du premier article
        n'est prouvé technique par personne ; il retombe sur l'oracle de titre, fail-closed.
        """
        lues = {provenances.get(uid) for uid in uids}
        return lues.pop() if len(lues) == 1 else None

    out: dict[str, Entree] = {}
    rang = 0
    for page in pages:
        par_uid = {source.uid: source for source in page.source.lines}
        for uids, colonne, texte_porte, bbox_porte, ordre_porte in _porteurs(page):
            if document_uid is not None:
                sources = [par_uid[uid] for uid in uids if uid in par_uid]
                if not sources:
                    continue
                rang += 1
                uid = line_uid(document_uid=document_uid, page=page.page, source_uids=uids,
                               bbox=bbox_porte, order=ordre_porte, text=texte_porte)
                if uid in out:
                    raise ValueError(f"collision line_uid dans le registre structure : {uid}")
                out[uid] = Entree(
                    uid=uid, page=page.page, colonne=colonne, ordre=rang, bbox=bbox_porte,
                    texte=texte_porte, texte_porte=texte_porte, source_uids=tuple(uids),
                    surface_provenance=_provenance([source.uid for source in sources]),
                )
                continue
            # Un porteur **est** une unité de portage : ce que ce bloc-là portera d'un seul tenant.
            # L'unité est nommée par son premier uid retenu, donc par sa position de lecture ; les
            # rangs qu'elle reçoit ici sont consécutifs, ce dont `_unite_scindee` tire son span.
            unite = ""
            for uid in uids:
                source = par_uid.get(uid)
                if source is None or uid in out:
                    continue
                rang += 1
                unite = unite or uid
                out[uid] = Entree(uid=uid, page=source.page, colonne=colonne, ordre=rang,
                                  bbox=source.bbox, texte=source.text, texte_porte=texte_porte,
                                  unite=unite, source_uids=(uid,),
                                  surface_provenance=_provenance([uid]))
    return out


def _lignes_payload(registre: dict[str, Entree]) -> list[dict[str, Any]]:
    """Lignes du segment adressées par un index positionnel `0..N-1`, dans l'ordre du registre.

    L'empreinte `line_uid` n'est pas publiée : le modèle ne l'écrit plus, et l'exposer coûterait 72
    caractères par ligne dans une enveloppe déjà bornée par `STRUCTURE_MAX_INPUT_CHARS`.
    """
    return [{"index": index, "page": entree.page, "colonne": entree.colonne,
             "ordre": entree.ordre, "bbox": list(entree.bbox), "texte": entree.titre}
            for index, entree in enumerate(sorted(registre.values(), key=lambda e: e.ordre))]


def table_indices(registre: dict[str, Entree],
                  anchors: Sequence[AncreStructure] = ()) -> tuple[str, ...]:
    """Table `index -> line_uid` du segment, exactement telle que la demande la présente.

    Les lignes locales occupent `0..N-1` dans l'ordre de lecture, les ancres de frontière la plage
    contiguë suivante `N..N+k-1` dans l'ordre où elles sont sérialisées. Une seule fonction la
    produit pour l'encodage et pour le rapprochement : les deux ne peuvent pas diverger.
    """
    locales = tuple(entree.uid for entree in sorted(registre.values(), key=lambda e: e.ordre))
    return (*locales, *(anchor.line_uid for anchor in anchors))


def _encoder_demande(registre: dict[str, Entree], settings: Settings, *,
                     anchors: Sequence[AncreStructure] = ()) -> str:
    duplicated = sorted(set(registre) & {anchor.line_uid for anchor in anchors})
    if duplicated:
        raise ValueError(
            f"ancres_frontiere duplique des lignes locales {duplicated[:5]}; aucun appel soumis")
    payload_value: dict[str, Any] = {"lignes": _lignes_payload(registre)}
    if anchors:
        payload_value["ancres_frontiere"] = [
            {"index": len(registre) + position, "ordre": anchor.ordre, "titre": anchor.titre}
            for position, anchor in enumerate(anchors)
        ]
    payload = json.dumps(payload_value, ensure_ascii=False, separators=(",", ":"))
    if len(payload) > settings.structure_max_input_chars:
        raise ValueError(
            f"charge utile de {len(payload)} caractères > borne "
            f"STRUCTURE_MAX_INPUT_CHARS={settings.structure_max_input_chars}; aucun appel soumis"
        )
    return payload


def demande(registre: dict[str, Entree], settings: Settings) -> str:
    """Charge utile : index, page, colonne, ordre, bbox et texte. Rien d'autre, aucune clé de sortie.

    Le `texte` publié est celui de la ligne **portée** — c'est-à-dire ce qu'un lecteur voit à cette
    position, numéro d'article fusionné compris. Les deux index d'une ligne fusionnée montrent donc
    la même chaîne : ils désignent la même ligne visuelle, et désigner l'un ou l'autre comme titre
    rend le même intitulé.
    """
    return _encoder_demande(registre, settings)


def _prompt() -> str:
    return (PROMPTS_DIR / "structure.md").read_text("utf-8")


def _schema() -> dict[str, Any]:
    """Grammaire de sortie **constante** : sa taille ne dépend plus du registre du segment.

    Les champs de référence sont des `integer` : le modèle désigne une ligne par son index dans la
    demande. Énumérer les `line_uid` autorisés était impossible à toute cardinalité utile — l'API
    répond 400 « Schema is too complex » dès 70 empreintes de 72 caractères, et 400 « Enum must be a
    non-empty array » pour un segment sans ancre de frontière. L'appartenance d'un index à sa plage,
    et le domaine exact de chaque champ, sont donc prouvés par `rapprocher_indices`, en code.

    Il ne porte aucune borne de cardinalité, de longueur ni d'intervalle : la sortie structurée du
    fournisseur refuse `maxItems`, `minItems`, `maxLength`, `minimum` et `maximum` (400
    `invalid_request_error`, mesuré sur le premier segment réel). Les bornes vivent côté code,
    fail-closed : `STRUCTURE_MAX_NODES` et `STRUCTURE_MAX_CHILDREN` dans
    `parse_proposition`/`verifier`, `max_length=1` sur `continuations_frontiere`, `max_length=256`
    sur `article_uid`, et un `title_line_uids` vide est refusé par le validateur du nœud.
    """
    index = {"type": "integer"}
    noeud = {
        "type": "object",
        "properties": {
            "titre_line_uid": index,
            "premiere_line_uid": index,
            "derniere_line_uid": index,
            "parent_line_uid": {"anyOf": [index, {"type": "null"}]},
            "title_line_uids": {"type": "array", "items": index},
            "article_uid": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "surface_class": {"type": "string", "enum": [
                "preliminaire", "table_des_matieres", "substantiel", "inconnu"]},
            "continuation_line_uids": {"type": "array", "items": index},
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": [
                            "same_clause_continuation", "explicit_dependency",
                            "definition_override"]},
                        "target_line_uid": index,
                    },
                    "required": ["kind", "target_line_uid"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["titre_line_uid", "premiere_line_uid", "derniere_line_uid", "parent_line_uid",
                     "title_line_uids", "article_uid", "surface_class",
                     "continuation_line_uids", "relations"],
        "additionalProperties": False,
    }
    continuation = {
        "type": "object",
        "properties": {
            "premiere_line_uid": index,
            "derniere_line_uid": index,
            "target_line_uid": index,
        },
        "required": ["premiere_line_uid", "derniere_line_uid", "target_line_uid"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "noeuds": {"type": "array", "items": noeud},
            "continuations_frontiere": {"type": "array", "items": continuation},
        },
        "required": ["noeuds", "continuations_frontiere"],
        "additionalProperties": False,
    }
    return {"type": "json_schema", "schema": schema}


def sortie_attendue(lignes: int, settings: Settings) -> int:
    """Tokens de sortie qu'un segment de `lignes` lignes demande : réflexion **plus** JSON.

    Le fournisseur compte la réflexion dans `max_tokens` : elle n'est jamais disponible pour le
    JSON, et une capacité qui ne la réserve pas est courte de sa taille. Les deux coefficients sont
    mesurés et vivent dans `Settings` (`structure_thinking_reserve_tokens`,
    `structure_output_tokens_per_line`), avec leur dérivation ; rien n'est en dur ici.
    """
    return (settings.structure_thinking_reserve_tokens
            + math.ceil(lignes * settings.structure_output_tokens_per_line))


def max_tokens_segment(lignes: int, settings: Settings) -> int:
    """`max_tokens` réellement envoyé pour ce segment : sa sortie attendue, sous son plafond.

    C'est cette valeur — et non le plafond — que la fenêtre, le majorant de coût, le dry-run et
    l'audit voient : demander 16 000 tokens pour un segment qui en rend 300 majorait le coût d'un
    facteur cinquante et rétrécissait la fenêtre d'autant.
    """
    return min(settings.structure_max_output_tokens, sortie_attendue(lignes, settings))


def requete(registre: dict[str, Entree], doc_id: str, settings: Settings, *,
            anchors: Sequence[AncreStructure] = ()) -> dict[str, Any]:
    """Paramètres d'un appel segmentaire du tier `ingest`. Aucun client n'est construit ici."""
    return {
        "model": MODEL,
        "max_tokens": max_tokens_segment(len(registre), settings),
        "system": [{"type": "text", "text": _prompt()}],
        "messages": [{"role": "user", "content": _encoder_demande(
            registre, settings, anchors=anchors)}],
        "output_config": {"format": _schema(), "effort": EFFORT[TIER]},
    }


def majorant_eur(params: dict[str, Any], settings: Settings) -> float:
    """Majorant Messages standard d'un appel, arrondi monétairement vers le haut."""
    return _arrondir_eur_superieur(_majorant_eur_brut(params, settings))


def _majorant_eur_brut(params: dict[str, Any], settings: Settings) -> float:
    prefix = _json_canonique({
        "system": params["system"],
        "output_config": params["output_config"],
    })
    messages = _json_canonique(params["messages"])
    return estimate_cost_from_token_bounds(
        params["model"],
        prefix_tokens=len(prefix.encode("utf-8")),
        message_tokens=len(messages.encode("utf-8")),
        max_tokens=params["max_tokens"],
        settings=settings,
    )


def _arrondir_eur_superieur(value: float) -> float:
    return math.ceil(value * 10_000 - 1e-12) / 10_000


def _json_canonique(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)


def _empreinte_registre(registre: dict[str, Entree]) -> str:
    """Empreinte de toutes les données source, texte compris, avant tout appel.

    Le fournisseur ne peut nommer que des ``line_uid``. Cette empreinte prouve en plus que le
    dictionnaire fourni par l'appelant n'a pas été remplacé ou muté entre la planification et la
    couture finale.
    """
    lignes = [
        {
            "uid": entree.uid,
            "page": entree.page,
            "colonne": entree.colonne,
            "ordre": entree.ordre,
            "bbox": list(entree.bbox),
            "texte": entree.texte,
            "texte_porte": entree.texte_porte,
            "unite": entree.unite,
            "source_uids": list(entree.source_uids),
            # La provenance décide du verdict de surface : si elle changeait entre la planification
            # et la couture, la même proposition serait jugée autrement. Elle appartient donc à
            # l'empreinte, au même titre que le texte.
            "surface_provenance": entree.surface_provenance,
        }
        for entree in sorted(registre.values(), key=lambda item: item.ordre)
    ]
    return hashlib.sha256(_json_canonique(lignes).encode("utf-8")).hexdigest()


def _taille_entree_majorante(params: dict[str, Any], settings: Settings) -> tuple[int, int]:
    """Taille caractères et borne prouvable de tokens de l'enveloppe complète.

    Le schéma est constant, mais le prompt système ne l'est pas et la charge utile porte tout le
    texte du segment. Mesurer seulement ``messages[0].content`` reproduirait précisément le faux
    préflight monolithique que cette segmentation corrige. La sérialisation comprend donc système,
    messages et schéma. La
    fenêtre n'utilise pas l'heuristique financière en caractères : le nombre d'octets UTF-8 est une
    borne supérieure du nombre de tokens d'un tokenizer byte-level, y compris sur Unicode dense.
    """
    envelope = _json_canonique({
        "model": params["model"],
        "system": params["system"],
        "messages": params["messages"],
        "output_config": params["output_config"],
    })
    return len(envelope), len(envelope.encode("utf-8"))


def _segment_admissible(params: dict[str, Any], settings: Settings, *,
                        lignes: int) -> tuple[bool, int, int]:
    """Un segment tient s'il tient **des deux côtés** : son entrée, et la sortie qu'il demande.

    Ne le borner que par l'entrée revenait à choisir « le plus long préfixe qui tient en entrée »,
    c'est-à-dire exactement le segment qui maximise le risque de dépasser la sortie. Le premier
    appel réel du chemin a été perdu ainsi (`stop_reason='max_tokens'`, réponse coupée au milieu
    d'un objet, 0,7654 € facturés pour rien).
    """
    request_chars, input_tokens = _taille_entree_majorante(params, settings)
    context_window = int(MODEL_CAPS[params["model"]]["context_window"])
    admissible = (sortie_attendue(lignes, settings) <= settings.structure_max_output_tokens
                  and input_tokens + int(params["max_tokens"]) <= context_window)
    return admissible, request_chars, input_tokens


def _segment_impossible(lignes: int, params: dict[str, Any] | None, settings: Settings) -> str:
    """Dit **lequel des deux côtés** déborde, avec le `max_tokens` dérivé et sa dérivation.

    Nommer `STRUCTURE_MAX_OUTPUT_TOKENS` seul ne disait pas ce qui avait été demandé au segment ni
    d'où venait le chiffre : un opérateur ne pouvait pas savoir s'il fallait resegmenter, recalibrer
    le ratio, ou relever la fenêtre.
    """
    attendue = sortie_attendue(lignes, settings)
    envoye = max_tokens_segment(lignes, settings)
    # `params` est absent quand la charge utile elle-même dépasse `STRUCTURE_MAX_INPUT_CHARS` : la
    # requête n'existe alors pas, et le côté entrée se dit par cette borne-là, pas par la fenêtre.
    entree = (f"{_taille_entree_majorante(params, settings)[1]} token(s) d'entrée majorés dans une "
              f"fenêtre effective de {MODEL_CAPS[MODEL]['context_window']}"
              if params is not None else
              f"une charge utile au-delà de "
              f"STRUCTURE_MAX_INPUT_CHARS={settings.structure_max_input_chars}")
    return (
        f"{lignes} ligne(s) demandent {attendue} token(s) de sortie "
        f"(réserve de réflexion STRUCTURE_THINKING_RESERVE_TOKENS="
        f"{settings.structure_thinking_reserve_tokens} + "
        f"STRUCTURE_OUTPUT_TOKENS_PER_LINE={settings.structure_output_tokens_per_line} par ligne) "
        f"pour un max_tokens dérivé de {envoye} sous "
        f"STRUCTURE_MAX_OUTPUT_TOKENS={settings.structure_max_output_tokens}, et {entree}"
    )


def _groupes_portage(registre: dict[str, Entree]) -> list[tuple[str, ...]]:
    """Unités contiguës indivisibles ; une même unité disjointe rend le registre inplanifiable."""
    ordered = sorted(registre.values(), key=lambda entree: entree.ordre)
    ordres = [entry.ordre for entry in ordered]
    if len(ordres) != len(set(ordres)):
        raise ValueError("registre non ordonnable : ordres de lecture dupliqués")
    groups: list[list[str]] = []
    ports: list[str] = []
    for entry in ordered:
        if not groups or ports[-1] != entry.portage:
            groups.append([])
            ports.append(entry.portage)
        groups[-1].append(entry.uid)
    if len(ports) != len(set(ports)):
        raise ValueError("registre non ordonnable : unité de portage non contiguë")
    return [tuple(group) for group in groups]


def _construire_plan(partitions: Sequence[tuple[str, ...]], registre: dict[str, Entree], *,
                     doc_id: str, settings: Settings) -> PlanStructure:
    """Fige, chiffre et adresse une partition déjà choisie."""
    expected = tuple(
        entry.uid for entry in sorted(registre.values(), key=lambda item: item.ordre))
    flattened = tuple(uid for line_uids in partitions for uid in line_uids)
    if flattened != expected or len(flattened) != len(set(flattened)):
        raise ValueError("plan de segments non bijectif avec le registre; aucun appel soumis")
    owners: dict[str, int] = {}
    for index, line_uids in enumerate(partitions, 1):
        for portage in {registre[uid].portage for uid in line_uids}:
            previous = owners.setdefault(portage, index)
            if previous != index:
                raise ValueError(
                    f"plan scinde l'unité de portage {portage!r}; aucun appel soumis")
    provisional: list[tuple[
        tuple[str, ...], tuple[AncreStructure, ...], dict[str, Any], int, int, float,
    ]] = []
    candidates = _candidates_ancres(registre)
    for line_uids in partitions:
        subset = {uid: registre[uid] for uid in line_uids}
        anchors = _ancres_frontiere(registre, line_uids, candidates=candidates)
        params = requete(subset, doc_id, settings, anchors=anchors)
        admissible, request_chars, input_tokens = _segment_admissible(
            params, settings, lignes=len(line_uids))
        if not admissible:
            raise ValueError(
                f"segment impossible : {_segment_impossible(len(line_uids), params, settings)}; "
                "aucun appel soumis")
        provisional.append((line_uids, anchors, params, request_chars, input_tokens,
                            _majorant_eur_brut(params, settings)))

    registry_hash = _empreinte_registre(registre)
    segment_hashes = [
        hashlib.sha256(_json_canonique({
            "line_uids": line_uids,
            "request": params,
        }).encode("utf-8")).hexdigest()
        for line_uids, _anchors, params, *_ in provisional
    ]
    plan_uid = "structure-plan-v2:" + hashlib.sha256(_json_canonique({
        "doc_id": doc_id,
        "registry_hash": registry_hash,
        "segments": segment_hashes,
    }).encode("utf-8")).hexdigest()
    segments = tuple(
        SegmentStructure(
            index=index,
            segment_uid=f"structure-segment-v2:{segment_hashes[index - 1]}",
            line_uids=line_uids,
            anchors=anchors,
            request=params,
            request_chars=request_chars,
            input_tokens_majorant=input_tokens,
            majorant_eur_brut=raw_estimate,
            majorant_eur=_arrondir_eur_superieur(raw_estimate),
        )
        for index, (line_uids, anchors, params, request_chars, input_tokens, raw_estimate)
        in enumerate(provisional, 1)
    )
    return PlanStructure(
        plan_uid=plan_uid,
        registry_hash=registry_hash,
        segments=segments,
        majorant_eur=_arrondir_eur_superieur(
            sum(segment.majorant_eur_brut for segment in segments)),
    )


def planifier_segments(registre: dict[str, Entree], *, doc_id: str,
                       settings: Settings) -> PlanStructure:
    """Partitionne le registre en enveloppes contiguës toutes admissibles avant API.

    La recherche dichotomique choisit, à chaque frontière, le plus long préfixe qui tient dans la
    fenêtre effective du modèle (entrée sérialisée complète + sortie maximale). Le résultat est
    déterministe pour un registre et des settings donnés. Les segments sont disjoints, contigus et
    leur concaténation est contrôlée contre le registre avant de rendre le plan.
    """
    if not registre:
        raise ValueError("registre vide : aucune ligne source à proposer")
    groups = _groupes_portage(registre)
    candidates = _candidates_ancres(registre)
    partitions: list[tuple[str, ...]] = []
    start = 0
    while start < len(groups):
        low, high = start + 1, len(groups)
        best: int | None = None
        while low <= high:
            end = (low + high) // 2
            line_uids = tuple(uid for group in groups[start:end] for uid in group)
            subset = {uid: registre[uid] for uid in line_uids}
            anchors = _ancres_frontiere(registre, line_uids, candidates=candidates)
            try:
                params = requete(subset, doc_id, settings, anchors=anchors)
            except ValueError:
                admissible, request_chars, input_tokens = False, 0, 0
            else:
                admissible, request_chars, input_tokens = _segment_admissible(
                    params, settings, lignes=len(line_uids))
            if admissible:
                best = end
                low = end + 1
            else:
                high = end - 1
        if best is None:
            group = groups[start]
            subset = {uid: registre[uid] for uid in group}
            try:
                params = requete(
                    subset, doc_id, settings,
                    anchors=_ancres_frontiere(registre, group, candidates=candidates))
            except ValueError:
                params = None
            raise ValueError(
                f"segment impossible : l'unité de portage {registre[group[0]].portage!r} ne tient "
                f"pas — {_segment_impossible(len(group), params, settings)}; aucun appel soumis"
            )
        partitions.append(tuple(uid for group in groups[start:best] for uid in group))
        start = best
    return _construire_plan(partitions, registre, doc_id=doc_id, settings=settings)


def _valider_plan(plan: PlanStructure, registre: dict[str, Entree], *, doc_id: str,
                   settings: Settings) -> None:
    """Rejoue les invariants content-addressés du plan avant la première soumission."""
    if _empreinte_registre(registre) != plan.registry_hash:
        raise ValueError("registre source différent du plan; aucun appel soumis")
    expected_uids = tuple(
        entry.uid for entry in sorted(registre.values(), key=lambda item: item.ordre))
    planned_uids = tuple(uid for segment in plan.segments for uid in segment.line_uids)
    if planned_uids != expected_uids or len(planned_uids) != len(set(planned_uids)):
        raise ValueError("plan de segments non bijectif avec le registre; aucun appel soumis")
    if [segment.index for segment in plan.segments] != list(range(1, len(plan.segments) + 1)):
        raise ValueError("indices de segments incohérents; aucun appel soumis")

    segment_hashes: list[str] = []
    owners: dict[str, int] = {}
    candidates = _candidates_ancres(registre)
    for segment in plan.segments:
        subset = {uid: registre[uid] for uid in segment.line_uids}
        expected_anchors = _ancres_frontiere(
            registre, segment.line_uids, candidates=candidates)
        expected_request = requete(subset, doc_id, settings, anchors=expected_anchors)
        admissible, request_chars, input_tokens = _segment_admissible(
            expected_request, settings, lignes=len(segment.line_uids))
        expected_raw = _majorant_eur_brut(expected_request, settings)
        expected_majorant = majorant_eur(expected_request, settings)
        digest = hashlib.sha256(_json_canonique({
            "line_uids": segment.line_uids,
            "request": expected_request,
        }).encode("utf-8")).hexdigest()
        segment_hashes.append(digest)
        for portage in {registre[uid].portage for uid in segment.line_uids}:
            previous = owners.setdefault(portage, segment.index)
            if previous != segment.index:
                raise ValueError(
                    f"plan scinde l'unité de portage {portage!r}; aucun appel soumis")
        if (not admissible
                or segment.anchors != expected_anchors
                or segment.request != expected_request
                or segment.request_chars != request_chars
                or segment.input_tokens_majorant != input_tokens
                or segment.majorant_eur_brut != expected_raw
                or segment.majorant_eur != expected_majorant
                or segment.segment_uid != f"structure-segment-v2:{digest}"):
            raise ValueError(
                f"segment {segment.index} incohérent avec son enveloppe chiffrée; "
                "aucun appel soumis")
    expected_total = _arrondir_eur_superieur(
        sum(segment.majorant_eur_brut for segment in plan.segments))
    expected_plan_uid = "structure-plan-v2:" + hashlib.sha256(_json_canonique({
        "doc_id": doc_id,
        "registry_hash": plan.registry_hash,
        "segments": segment_hashes,
    }).encode("utf-8")).hexdigest()
    if plan.majorant_eur != expected_total or plan.plan_uid != expected_plan_uid:
        raise ValueError("identité ou majorant cumulé du plan incohérent; aucun appel soumis")


# Champs de référence du contrat de sortie, par domaine. Une ligne locale seule peut intituler ou
# borner un nœud ; seule une ancre de frontière peut être la cible d'une continuation ; parents et
# relations peuvent traverser la frontière. Le schéma fournisseur ne sait plus rien de ces domaines
# — il ne connaît que `integer` — donc c'est ici, et nulle part ailleurs, qu'ils sont prouvés.
_CHAMPS_LOCAUX = ("titre_line_uid", "premiere_line_uid", "derniere_line_uid")
_LISTES_LOCALES = ("title_line_uids", "continuation_line_uids")


def rapprocher_indices(raw: str, registre: dict[str, Entree], *,
                       anchors: Sequence[AncreStructure] = ()) -> str:
    """Traduit les index entiers d'une réponse en `line_uid`, ou refuse par un motif nommé.

    C'est la seule couche qui connaît la convention d'index : en amont le modèle n'écrit que des
    entiers, en aval tout est en `line_uid` — modèles Pydantic, couture, `verifier`, `structure.json`
    et l'audit `trusted_line_uids` sont inchangés. La traduction est **bijective et sans réparation**
    : un index hors de sa plage, un index d'ancre là où une ligne locale est exigée (ou l'inverse),
    un index répété dans une liste, une valeur qui n'est pas un entier — chacun est un refus, jamais
    une correction. C'est ce refus qui porte la garantie que le schéma énumératif portait avant lui :
    rien d'autre qu'une ligne effectivement présentée au modèle ne peut être désignée.
    """
    table = table_indices(registre, anchors)
    locales = len(registre)

    def uid(valeur: Any, champ: str, *, domaine: str) -> str:
        # `bool` est un `int` en Python : sans ce filtre, `true` désignerait la ligne d'index 1.
        if not isinstance(valeur, int) or isinstance(valeur, bool):
            raise StructureRefusee(
                "index_invalide", f"{champ}: index entier attendu, reçu {valeur!r}")
        if not 0 <= valeur < len(table):
            raise StructureRefusee(
                "index_invalide",
                f"{champ}: index {valeur} hors de la plage 0..{len(table) - 1} du segment")
        if domaine == "locale" and valeur >= locales:
            raise StructureRefusee(
                "index_invalide",
                f"{champ}: index {valeur} désigne une ancre de frontière, pas une ligne du segment")
        if domaine == "externe" and valeur < locales:
            raise StructureRefusee(
                "index_invalide",
                f"{champ}: index {valeur} désigne une ligne du segment, pas une ancre de frontière")
        return table[valeur]

    def liste(valeurs: Any, champ: str) -> list[str]:
        if not isinstance(valeurs, list):
            raise StructureRefusee("proposition_illisible", f"{champ}: liste attendue")
        uids: list[str] = []
        for position, valeur in enumerate(valeurs):
            traduit = uid(valeur, f"{champ}[{position}]", domaine="locale")
            if traduit in uids:
                raise StructureRefusee("index_invalide", f"{champ}: index {valeur!r} répété")
            uids.append(traduit)
        return uids

    try:
        charge = json.loads(raw)
    except ValueError as exc:
        raise StructureRefusee(
            "proposition_illisible", f"réponse non JSON ({type(exc).__name__})") from exc
    if not isinstance(charge, dict):
        raise StructureRefusee("proposition_illisible", "réponse JSON qui n'est pas un objet")
    noeuds = charge.get("noeuds", [])
    if not isinstance(noeuds, list):
        raise StructureRefusee("proposition_illisible", "noeuds: liste attendue")
    for rang, noeud in enumerate(noeuds):
        if not isinstance(noeud, dict):
            raise StructureRefusee("proposition_illisible", f"noeuds[{rang}]: objet attendu")
        prefixe = f"noeuds[{rang}]"
        for champ in _CHAMPS_LOCAUX:
            if champ in noeud:
                noeud[champ] = uid(noeud[champ], f"{prefixe}.{champ}", domaine="locale")
        if noeud.get("parent_line_uid") is not None:
            noeud["parent_line_uid"] = uid(
                noeud["parent_line_uid"], f"{prefixe}.parent_line_uid", domaine="quelconque")
        for champ in _LISTES_LOCALES:
            if champ in noeud:
                noeud[champ] = liste(noeud[champ], f"{prefixe}.{champ}")
        relations = noeud.get("relations", [])
        if not isinstance(relations, list):
            raise StructureRefusee("proposition_illisible", f"{prefixe}.relations: liste attendue")
        for position, relation in enumerate(relations):
            if not isinstance(relation, dict):
                raise StructureRefusee(
                    "proposition_illisible", f"{prefixe}.relations[{position}]: objet attendu")
            if "target_line_uid" in relation:
                relation["target_line_uid"] = uid(
                    relation["target_line_uid"],
                    f"{prefixe}.relations[{position}].target_line_uid", domaine="quelconque")
    continuations = charge.get("continuations_frontiere", [])
    if not isinstance(continuations, list):
        raise StructureRefusee(
            "proposition_illisible", "continuations_frontiere: liste attendue")
    for rang, continuation in enumerate(continuations):
        if not isinstance(continuation, dict):
            raise StructureRefusee(
                "proposition_illisible", f"continuations_frontiere[{rang}]: objet attendu")
        prefixe = f"continuations_frontiere[{rang}]"
        for champ, domaine in (("premiere_line_uid", "locale"), ("derniere_line_uid", "locale"),
                               ("target_line_uid", "externe")):
            if champ in continuation:
                continuation[champ] = uid(
                    continuation[champ], f"{prefixe}.{champ}", domaine=domaine)
    return json.dumps(charge, ensure_ascii=False)


def parse_proposition(raw: str, registre: dict[str, Entree], doc_id: str, *,
                      settings: Settings | None = None,
                      reference_uids: Sequence[str] = ()) -> StructureProposee:
    """Défense locale, **même si le schéma l'impose** : tout `uid` étranger est refusé avant usage.

    Elle est lue en `line_uid` : `rapprocher_indices` a déjà traduit les index de la réponse, et un
    `structure.json` relu du disque n'a jamais porté d'index. Le contrôle reste entier — il ne
    suppose ni la traduction en amont, ni le schéma fournisseur.

    Deux temps, dans cet ordre. D'abord la **forme filaire**, validée localement par
    `TypeAdapter(PropositionFilaire).validate_json` — la convention LLM du spine, celle que
    `client.py` applique déjà. Puis les contrôles d'`uid` : appartenance au registre et unicité des
    titres. La validation locale ne les remplace pas — aucun schéma pydantic ne connaît le registre
    de ce document — elle les précède.
    """
    try:
        filaire = _FILAIRE.validate_json(raw)
        noeuds = filaire.noeuds
    except (ValidationError, ValueError, TypeError) as exc:
        raise ValueError(f"proposition hors schéma strict ({type(exc).__name__})") from exc
    # La largeur d'abord : elle borne le travail de tout ce qui suit, ici comme dans `verifier`.
    detail = largeur_hors_borne(noeuds, settings or get_settings())
    if detail is not None:
        raise ValueError(f"largeur_excessive : {detail}")
    vus: set[str] = set()
    references = set(reference_uids)
    for continuation in filaire.continuations_frontiere:
        for champ, uid in (
            ("continuations_frontiere.premiere_line_uid", continuation.premiere_line_uid),
            ("continuations_frontiere.derniere_line_uid", continuation.derniere_line_uid),
        ):
            if uid not in registre:
                raise ValueError(f"{champ}: line_uid inconnu du registre {uid!r}")
        if (continuation.target_line_uid not in references
                or continuation.target_line_uid in registre):
            raise ValueError(
                "continuations_frontiere.target_line_uid doit être une ancre externe "
                f"{continuation.target_line_uid!r}")
    for noeud in noeuds:
        for champ, uid in (("titre_line_uid", noeud.titre_line_uid),
                           ("premiere_line_uid", noeud.premiere_line_uid),
                           ("derniere_line_uid", noeud.derniere_line_uid)):
            if uid not in registre:
                raise ValueError(f"{champ}: line_uid inconnu du registre {uid!r}")
        if (noeud.parent_line_uid is not None and noeud.parent_line_uid not in registre
                and noeud.parent_line_uid not in references):
            raise ValueError(
                f"parent_line_uid: line_uid inconnu du registre et des ancres "
                f"{noeud.parent_line_uid!r}")
        for relation in noeud.relations:
            if (relation.target_line_uid not in registre
                    and relation.target_line_uid not in references):
                raise ValueError(
                    f"relations.target_line_uid: line_uid inconnu du registre et des ancres "
                    f"{relation.target_line_uid!r}")
        if noeud.titre_line_uid in vus:
            raise ValueError(f"titre_line_uid dupliqué {noeud.titre_line_uid!r}")
        vus.add(noeud.titre_line_uid)
    # Compatibilité des doubles historiques : le schéma filaire courant impose v2, mais les tests
    # ou reprises v1 sont migrés sans inventer de relation. La classe n'est jamais promue par défaut :
    # elle est relue dans les lignes de l'intervalle par le même oracle indépendant que le gate.
    migrated = []
    for node in noeuds:
        if not node.title_line_uids:
            _title, surface = _semantique_locale(node, registre)
            node = node.model_copy(update={
                "title_line_uids": [node.titre_line_uid],
                "surface_class": surface,
            })
        migrated.append(node)
    version = "2" if all({"title_line_uids", "article_uid", "surface_class",
                           "continuation_line_uids", "relations"} <= node.model_fields_set
                         for node in noeuds) else "1"
    return StructureProposee(
        schema_version=version,
        doc_id=doc_id,
        noeuds=migrated,
        continuations_frontiere=filaire.continuations_frontiere,
    )


def _refus(motif: str, detail: str) -> Verdict:
    return Verdict(accepte=False, motif=motif, detail=detail[:1000])


def unite_scindee(registre: dict[str, Entree],
                  intervalles: Sequence[tuple[int, int]]) -> str | None:
    """Une frontière d'intervalle proposé tombe-t-elle **à l'intérieur** d'une unité de portage ?

    Une unité est indivisible : ses uid seront portés par un seul et même bloc, quel que soit le
    découpage proposé. Une frontière de nœud posée en son milieu demande donc de servir ce bloc sous
    deux nœuds à la fois — ce que `_noeud_de_groupe` refuse au build, et qu'il faut refuser dès
    l'acceptation, sans quoi la CLI écrit un `structure.json` que l'ingestion ne pourra jamais
    accepter.

    La règle est **générique** : ligne fusionnée ou table, l'unité est la même chose et se juge
    pareil. Aucune règle propre à un assureur, une page, un titre ou une numérotation.

    Le coût est en O(lignes + nœuds) : les frontières sont l'ensemble des ordres où un intervalle
    s'ouvre (`premiere`) ou vient de se fermer (`derniere + 1`), et une unité n'est parcourue que
    lorsqu'elle porte au moins deux uid. Comparer deux à deux les nœuds effectifs de chaque uid
    coûterait O(lignes × nœuds) sur un chemin dont toute la valeur est de refuser vite.
    """
    frontieres: set[int] = set()
    for premiere, derniere in intervalles:
        frontieres.add(premiere)
        frontieres.add(derniere + 1)
    spans: dict[str, list[int]] = {}
    for entree in registre.values():
        spans.setdefault(entree.portage, []).append(entree.ordre)
    for unite, ordres in sorted(spans.items()):
        if len(ordres) < 2:
            continue
        debut, fin = min(ordres), max(ordres)
        coupures = sorted(borne for borne in frontieres if debut < borne <= fin)
        if coupures:
            return (f"unité de portage {unite!r} — {len(ordres)} ligne(s) source d'ordre {debut} à "
                    f"{fin}, qu'un même bloc portera d'un seul tenant — scindée par une frontière de "
                    f"nœud à l'ordre {coupures[0]}")
    return None


def verifier(proposition: StructureProposee, registre: dict[str, Entree], *, doc_id: str,
             settings: Settings) -> Verdict:
    """Prouve les invariants d'une proposition, en code pur et hors réseau. Un refus est nommé.

    L'ordre des familles est fixe : le premier invariant enfreint donne le motif, si bien qu'une même
    proposition rend toujours le même refus.
    """
    if proposition.doc_id != doc_id:
        return _refus("document_different", f"proposition pour {proposition.doc_id!r}, document {doc_id!r}")
    if proposition.continuations_frontiere:
        return _refus(
            "affectation_non_prouvee",
            "continuation de transport non consommée dans la proposition globale",
        )
    # La largeur est jugée **avant toute boucle** : celle des intervalles est en O(n²), et le modèle
    # Pydantic ne suffit pas — `model_construct` et tout appel programmatique direct le contournent.
    # Une proposition trop large est refusée en O(n), sans être parcourue une seule fois de plus.
    detail = largeur_hors_borne(proposition.noeuds, settings)
    if detail is not None:
        return _refus("largeur_excessive", detail)
    if not proposition.noeuds:
        # Refusée pour ce qu'elle est, avant même la couverture : « aucun nœud » est un état du
        # proposant, pas une liste de lignes omises. Le motif dit donc qu'il n'y a rien à prouver,
        # plutôt que d'énumérer tout le registre sous `ligne_omise`.
        return _refus("proposition_vide", "aucun nœud proposé : rien n'est structuré ni prouvé")
    noeuds = proposition.noeuds
    titres: dict[str, NoeudPropose] = {}
    for noeud in noeuds:
        referenced_uids = [
            ("titre_line_uid", noeud.titre_line_uid),
            ("premiere_line_uid", noeud.premiere_line_uid),
            ("derniere_line_uid", noeud.derniere_line_uid),
            *(("title_line_uids", uid) for uid in noeud.title_line_uids),
            *(("continuation_line_uids", uid) for uid in noeud.continuation_line_uids),
            *(("relations.target_line_uid", relation.target_line_uid)
              for relation in noeud.relations),
        ]
        for champ, uid in referenced_uids:
            if uid not in registre:
                return _refus("ligne_inconnue", f"{champ}={uid!r}")
        if noeud.titre_line_uid in titres:
            return _refus("titre_duplique", f"titre_line_uid={noeud.titre_line_uid!r}")
        titres[noeud.titre_line_uid] = noeud
        if proposition.schema_version == "2":
            title_orders = [registre[uid].ordre for uid in noeud.title_line_uids]
            if title_orders != sorted(title_orders):
                return _refus("ordre_impossible", "title_line_uids hors ordre documentaire")
            if noeud.title_line_uids[0] != noeud.titre_line_uid:
                return _refus(
                    "affectation_non_prouvee",
                    "title_line_uids doit commencer par titre_line_uid",
                )
            if title_orders != list(range(title_orders[0], title_orders[0] + len(title_orders))):
                return _refus(
                    "affectation_non_prouvee",
                    "title_line_uids non contigus : une ligne de corps ne peut être annexée au titre",
                )
            title = " ".join(registre[uid].titre.strip() for uid in noeud.title_line_uids).strip()
            if any(len(registre[uid].titre.strip()) > 160
                   or registre[uid].titre.rstrip().endswith((".", ";"))
                   for uid in noeud.title_line_uids[1:]):
                return _refus(
                    "affectation_non_prouvee",
                    "title_line_uids contient une ligne de corps selon l'oracle local",
                )
            _title, technical_surface = _semantique_locale(noeud, registre)
            if technical_surface is None:
                return _refus(
                    "affectation_non_prouvee",
                    f"surface sans preuve locale indépendante sous {noeud.titre_line_uid!r} ; "
                    f"lignes lues : {_lignes_lues(noeud, registre)}",
                )
            if noeud.surface_class != technical_surface:
                return _refus(
                    "affectation_non_prouvee",
                    f"surface {technical_surface!r} lisible mais classée {noeud.surface_class!r} ; "
                    f"lignes lues : {_lignes_lues(noeud, registre)}",
                )
            readable_article = oracle_article_uid(title)
            if noeud.article_uid != readable_article:
                return _refus(
                    "affectation_non_prouvee",
                    f"article_uid {noeud.article_uid!r} diverge du numéro lisible "
                    f"{readable_article!r}",
                )
            if noeud.surface_class == "inconnu":
                return _refus("affectation_non_prouvee", "surface_class=inconnu interdite en publication v2")
    for noeud in noeuds:
        parent = noeud.parent_line_uid
        if parent is not None and parent not in titres:
            # Un `uid` qui existe mais n'intitule aucun nœud ne peut pas être un parent : la ligne
            # est inconnue **en tant que nœud**, et l'arbre serait suspendu à rien.
            return _refus("ligne_inconnue", f"parent_line_uid={parent!r} n'intitule aucun nœud")
        for relation in noeud.relations:
            if relation.target_line_uid not in titres:
                return _refus(
                    "ligne_inconnue",
                    f"relation {relation.kind} vise un titre absent {relation.target_line_uid!r}",
                )
            if relation.target_line_uid == noeud.titre_line_uid:
                return _refus("cycle", f"auto-relation sur {noeud.titre_line_uid!r}")

    # Les relations structurelles sont un graphe orienté distinct de la parenté. Toute boucle rend
    # navigation et contexte singleton non terminants, même si l'arbre parent/enfant est valide.
    relation_graph = {
        node.titre_line_uid: [relation.target_line_uid for relation in node.relations]
        for node in noeuds
    }
    # Parcours itératif borné : une chaîne valide à la profondeur maximale ne dépend jamais de la
    # limite de récursion Python, et chaque arête est examinée au plus une fois.
    colors: dict[str, int] = {uid: 0 for uid in relation_graph}  # 0 blanc, 1 gris, 2 noir
    for origin in relation_graph:
        if colors[origin] != 0:
            continue
        colors[origin] = 1
        stack: list[tuple[str, int]] = [(origin, 0)]
        while stack:
            current, offset = stack[-1]
            targets = relation_graph[current]
            if offset >= len(targets):
                colors[current] = 2
                stack.pop()
                continue
            target = targets[offset]
            stack[-1] = (current, offset + 1)
            if colors[target] == 1:
                return _refus("cycle", "cycle dans les relations structurelles")
            if colors[target] == 0:
                colors[target] = 1
                stack.append((target, 0))

    # (page, bbox) est l'identité visuelle d'un titre : deux titres au même endroit rendraient
    # l'arbre inspectable à deux réponses différentes pour la même page surlignée.
    #
    # L'unité de portage est la **seconde** identité d'un même endroit, et il en faut deux : les bbox
    # source d'un numéro et de son intitulé diffèrent — l'extracteur les a vus séparément —, alors
    # que le titre servi est relu sur la ligne *portée*, la même pour les deux. Deux nœuds intitulés
    # par une même unité afficheraient donc le même intitulé et répondraient deux fois la même chose
    # pour le même endroit de la page : c'est exactement ce que `titre_ambigu` nomme déjà, et lui
    # ajouter un motif dirait le même défaut sous deux mots. Aucune des deux clés ne subsume l'autre :
    # deux lignes source distinctes peuvent partager (page, bbox) sans partager d'unité.
    positions: dict[tuple[int, tuple[float, float, float, float]], str] = {}
    unites: dict[str, str] = {}
    for uid in titres:
        entree = registre[uid]
        cle = (entree.page, entree.bbox)
        if cle in positions:
            return _refus("titre_ambigu", f"{positions[cle]!r} et {uid!r} partagent (page, bbox) {cle}")
        positions[cle] = uid
        if entree.portage in unites:
            return _refus("titre_ambigu",
                          f"{unites[entree.portage]!r} et {uid!r} partagent l'unité de portage "
                          f"{entree.portage!r} : les deux nœuds seraient intitulés par la même ligne "
                          f"portée, {entree.titre!r}")
        unites[entree.portage] = uid

    profondeurs: dict[str, int] = {}
    for uid in titres:
        vus: set[str] = set()
        courant: str | None = uid
        while courant is not None:
            if courant in vus:
                return _refus("cycle", f"chaîne de parents sans racine depuis {uid!r}")
            vus.add(courant)
            courant = titres[courant].parent_line_uid
        profondeurs[uid] = len(vus)
        if profondeurs[uid] > settings.structure_max_depth:
            return _refus("profondeur_excessive",
                          f"{uid!r} à la profondeur {profondeurs[uid]} > {settings.structure_max_depth}")

    bornes: dict[str, tuple[int, int]] = {}
    article_owners: dict[str, str] = {}
    for uid, noeud in titres.items():
        premiere = registre[noeud.premiere_line_uid].ordre
        derniere = registre[noeud.derniere_line_uid].ordre
        titre = registre[uid].ordre
        if premiere > derniere:
            return _refus("ordre_impossible", f"{uid!r} : première ligne après la dernière")
        if not premiere <= titre <= derniere:
            return _refus("ordre_impossible", f"{uid!r} : titre hors de son propre intervalle")
        semantic_uids = [*noeud.title_line_uids, *noeud.continuation_line_uids]
        outside = [candidate for candidate in semantic_uids
                   if not premiere <= registre[candidate].ordre <= derniere]
        if outside:
            return _refus(
                "affectation_non_prouvee",
                f"{uid!r} projette des lignes sémantiques hors de son intervalle : {outside}")
        if set(noeud.title_line_uids) & set(noeud.continuation_line_uids):
            return _refus(
                "affectation_non_prouvee",
                f"{uid!r} classe une même ligne comme titre et continuation")
        if noeud.article_uid is not None:
            if noeud.article_uid != noeud.article_uid.strip() or not noeud.article_uid:
                return _refus("affectation_non_prouvee", f"article_uid invalide sous {uid!r}")
            previous = article_owners.get(noeud.article_uid)
            if previous is not None:
                return _refus(
                    "titre_ambigu",
                    f"article_uid {noeud.article_uid!r} collisionne entre {previous!r} et {uid!r}")
            article_owners[noeud.article_uid] = uid
        bornes[uid] = (premiere, derniere)
    fratries: dict[str | None, list[str]] = {}
    for noeud in noeuds:  # l'ordre de la proposition est celui que le modèle revendique
        fratries.setdefault(noeud.parent_line_uid, []).append(noeud.titre_line_uid)
    for parent, freres in fratries.items():
        debuts = [bornes[uid][0] for uid in freres]
        if any(suivant <= precedent for precedent, suivant in zip(debuts, debuts[1:])):
            return _refus("ordre_impossible",
                          f"frères de {parent!r} rendus dans un ordre décroissant : {freres}")

    def ancetre(descendant: str, candidat: str) -> bool:
        courant = titres[descendant].parent_line_uid
        while courant is not None:
            if courant == candidat:
                return True
            courant = titres[courant].parent_line_uid
        return False

    # Un enfant **doit** tenir dans l'intervalle de son parent déclaré. Le contrôle de croisement
    # ci-dessous ne peut pas y suppléer : il s'arrête au premier intervalle disjoint, si bien que
    # deux nœuds disjoints ne sont jamais confrontés — et qu'une section « 5 à 8 » pouvait se
    # déclarer sous-section d'une section « 1 à 4 » sans qu'aucun invariant ne l'en empêche.
    for uid, noeud in titres.items():
        parent = noeud.parent_line_uid
        if parent is None:
            continue
        a_enfant, b_enfant = bornes[uid]
        a_parent, b_parent = bornes[parent]
        if not (a_parent <= a_enfant and b_enfant <= b_parent) or (a_parent, b_parent) == (a_enfant, b_enfant):
            return _refus("parent_non_contenant",
                          f"{uid!r} [{a_enfant},{b_enfant}] se déclare sous {parent!r} "
                          f"[{a_parent},{b_parent}] sans y être strictement contenu")
        child_article = titres[uid].article_uid
        parent_article = titres[parent].article_uid
        if child_article is not None and parent_article is not None:
            child_path = tuple(child_article.removeprefix("article:").replace("-", ".").split("."))
            parent_path = tuple(parent_article.removeprefix("article:").replace("-", ".").split("."))
            if not (len(child_path) > len(parent_path)
                    and child_path[:len(parent_path)] == parent_path):
                return _refus(
                    "parent_non_contenant",
                    f"parenté sémantique impossible : {child_article!r} sous {parent_article!r}",
                )

    ordonnes = sorted(titres, key=lambda uid: (bornes[uid][0], bornes[uid][1], uid))
    for index, gauche in enumerate(ordonnes):
        a1, b1 = bornes[gauche]
        for droite in ordonnes[index + 1:]:
            a2, b2 = bornes[droite]
            if a2 > b1:
                break  # les intervalles suivants commencent plus loin encore : plus rien à croiser
            inclus = a1 <= a2 and b2 <= b1 and (a1, b1) != (a2, b2)
            if not inclus:
                return _refus("intervalles_croises",
                              f"{gauche!r} [{a1},{b1}] et {droite!r} [{a2},{b2}] "
                              "ne sont ni disjoints ni strictement emboîtés")
            if not ancetre(droite, gauche):
                return _refus("intervalles_croises",
                              f"{droite!r} est emboîté dans {gauche!r} sans en descendre")

    # Couverture **totale**, prouvée uid par uid : « toute ligne inconnue, dupliquée, omise […] met
    # le document en quarantaine ». Une part suffisante ne prouvait rien de la ligne restante : les
    # groupes laissés hors de tout intervalle héritaient du nœud voisin, et l'arbre servi portait
    # alors une affectation que la proposition n'avait ni portée ni prouvée.
    # `structure_min_coverage` reste la borne de couverture nommée et publiée qu'exige l'AC, mais
    # elle ne peut que **durcir** cette règle, jamais la desserrer : l'abaisser ne rouvre aucun trou,
    # chaque uid non couvert reste un refus nommé qui désigne les lignes concernées.
    omises = [entree.uid for entree in sorted(registre.values(), key=lambda e: e.ordre)
              if not any(premiere <= entree.ordre <= derniere
                         for premiere, derniere in bornes.values())]
    if omises:
        return _refus("ligne_omise",
                      f"{len(omises)}/{len(registre)} ligne(s) du registre hors de tout intervalle "
                      f"proposé (borne STRUCTURE_MIN_COVERAGE={settings.structure_min_coverage:.0%}, "
                      f"jamais desserrée ligne à ligne) : {', '.join(omises[:20])}")

    # Dernière famille, parce qu'elle suppose les précédentes : les intervalles sont emboîtés et la
    # couverture est totale, si bien qu'une frontière posée à l'intérieur d'une unité de portage est
    # bien une demande de servir un bloc indivisible sous deux nœuds. Le verdict `accepte=True`
    # promet que l'ingestion honorera la proposition ; il ne peut pas être rendu sur une proposition
    # que `build_document` refusera à coup sûr.
    detail = unite_scindee(registre, list(bornes.values()))
    if detail is not None:
        return _refus("affectation_non_prouvee", detail)
    # Compatibilité v1 : l'absence de classe n'est jamais promue ; ``arbre`` la dérive de la source
    # ou conserve ``inconnu`` (donc non citable). Une classe explicitement transportée, en revanche,
    # doit concorder avec l'oracle. Ce contrôle vient après les familles historiques afin qu'un
    # intervalle invalide garde son motif structurel déterministe.
    if proposition.schema_version == "1":
        for noeud in noeuds:
            if noeud.surface_class is None:
                continue
            _title, technical_surface = _semantique_locale(noeud, registre)
            if technical_surface is None or noeud.surface_class != technical_surface:
                return _refus(
                    "affectation_non_prouvee",
                    f"surface v1 {noeud.surface_class!r} sans preuve locale concordante sous "
                    f"{noeud.titre_line_uid!r}",
                )
    return Verdict(accepte=True,
                   detail=f"{len(noeuds)} nœud(s), couverture totale de {len(registre)} ligne(s)")


def arbre(proposition: StructureProposee, registre: dict[str, Entree], doc_id: str, *,
          settings: Settings | None = None) -> tuple[dict[str, NoeudVerifie], dict[str, str]]:
    """Nœuds dérivés d'une proposition **déjà vérifiée**, et la ligne → nœud le plus profond.

    Le `node_id` est un chemin positionnel (`{doc_id}:s1.2`) calculé par le code, et le titre est
    relu dans le registre : le modèle n'écrit ni l'un ni l'autre.

    « Déjà vérifiée » est ici **imposé**, pas supposé, pour la seule borne dont le coût se paierait
    sur-le-champ : l'affectation ligne → nœud est en O(lignes × nœuds), et un appel programmatique
    direct sur une proposition non bornée referait le travail que `verifier` refuse.
    """
    detail = largeur_hors_borne(proposition.noeuds, settings or get_settings())
    if detail is not None:
        raise StructureRefusee("largeur_excessive", detail)
    titres = {noeud.titre_line_uid: noeud for noeud in proposition.noeuds}
    enfants: dict[str | None, list[str]] = {}
    for noeud in proposition.noeuds:
        enfants.setdefault(noeud.parent_line_uid, []).append(noeud.titre_line_uid)
    plan: dict[str, NoeudVerifie] = {}
    node_ids: dict[str, str] = {}

    def parcourir(parent_uid: str | None, chemin: tuple[int, ...]) -> None:
        for rang, uid in enumerate(enfants.get(parent_uid, []), 1):
            position = (*chemin, rang)
            node_id = f"{doc_id}:s{'.'.join(str(n) for n in position)}"
            node_ids[uid] = node_id
            noeud = titres[uid]
            title_uids = noeud.title_line_uids or [uid]
            full_title = " ".join(dict.fromkeys(
                registre[title_uid].titre.strip() for title_uid in title_uids
                if registre[title_uid].titre.strip()))
            plan[node_id] = NoeudVerifie(
                # Le titre est celui de la ligne **portée** : un intitulé que l'extracteur scinde en
                # « 1 » puis « Objet … » est servi entier, comme sur le chemin heuristique.
                node_id=node_id, level=len(position), title=full_title or registre[uid].titre,
                premiere=registre[noeud.premiere_line_uid].ordre,
                derniere=registre[noeud.derniere_line_uid].ordre,
                parent_id=None if parent_uid is None else node_ids[parent_uid],
                article_uid=noeud.article_uid,
                surface_class=(noeud.surface_class
                               or _semantique_locale(noeud, registre)[1]
                               or "inconnu"),
                continuation_line_uids=tuple(noeud.continuation_line_uids),
            )
            parcourir(uid, position)

    parcourir(None, ())
    # Les cibles de relation deviennent des ``node_id`` seulement après le parcours positionnel.
    for uid, node_id in node_ids.items():
        proposed = titres[uid]
        spec = plan[node_id]
        plan[node_id] = NoeudVerifie(
            node_id=spec.node_id, level=spec.level, title=spec.title,
            premiere=spec.premiere, derniere=spec.derniere, parent_id=spec.parent_id,
            article_uid=spec.article_uid, surface_class=spec.surface_class,
            continuation_line_uids=spec.continuation_line_uids,
            relations=tuple(NodeRelation(
                kind=relation.kind,
                target_node_id=node_ids[relation.target_line_uid],
            ) for relation in proposed.relations),
        )
    # Même exigence, même raison que la borne de largeur ci-dessus : `arbre()` est documenté « sur une
    # proposition déjà vérifiée », et un appel programmatique direct rendrait sinon un `node_of_uid`
    # qui scinde une unité indivisible. Le coût est en O(lignes + nœuds), payé une fois.
    detail = unite_scindee(registre, [(spec.premiere, spec.derniere) for spec in plan.values()])
    if detail is not None:
        raise StructureRefusee("affectation_non_prouvee", detail)
    profondeur = {node_id: spec.level for node_id, spec in plan.items()}
    par_uid: dict[str, str] = {}
    for entree in registre.values():
        candidats = [node_id for node_id, spec in plan.items()
                     if spec.premiere <= entree.ordre <= spec.derniere]
        if candidats:
            par_uid[entree.uid] = max(candidats, key=lambda node_id: (profondeur[node_id], node_id))
    return plan, par_uid


def presente(path: Path) -> bool:
    """`structure.json` **existe-t-il** au sens du système de fichiers ? — pas « est-ce un fichier ».

    Un répertoire, un lien pendant, un tube : autant d'artefacts présents mais illisibles. Les
    confondre avec une absence rendait la main à l'heuristique numérique sans qu'aucun refus soit
    nommé — le repli silencieux qu'AD-16 interdit, sur le chemin même bâti pour l'empêcher. Tout ce
    qui existe passe donc par `charger()`, donc par un refus nommé et la quarantaine.

    `lexists` et non `exists` : un lien pendant n'existe pas au sens de sa cible, mais son entrée de
    répertoire, elle, est bien là — et c'est elle que l'opérateur a déposée.

    **Sauf sous une racine de publication** (story 4.5, tour de la racine vraiment unique). Là, un
    lien pendant n'est pas un artefact déposé : c'est la forme même de l'**absence** — un slot que la
    génération publiée ne porte pas. La disposition en crée un pour tout artefact jamais publié, et
    c'est l'état réel des `structure.json` du dépôt. Les compter comme « présents » aurait fait
    rendre `proposition_illisible` à `charger()`, donc un check bloquant, donc la mise en quarantaine
    des documents servis à la prochaine réingestion — un défaut d'interaction créé par la
    disposition, pas par l'artefact. La reconnaissance est **structurelle** (le chemin résolu passe
    par un pointeur de publication), jamais nominale : hors racine, la règle d'AD-16 est inchangée,
    et un lien pendant ordinaire reste « présent mais illisible ».
    """
    if not os.path.lexists(path):
        return False
    if not path.is_file() and racine_couvrant(path) is not None:
        return False
    return True


def charger(path: Path, *, settings: Settings | None = None) -> StructureProposee:
    """La proposition seule — la forme courte de `charger_octets` quand les octets n'importent pas."""
    return charger_octets(path, settings=settings)[0]


def charger_octets(path: Path, *, settings: Settings | None = None
                   ) -> tuple[StructureProposee, bytes]:
    """La proposition **et les octets dont elle vient** — une seule lecture, un seul tampon.

    Revue du tour N1–N3, constat 7. `pdf_to_blocks` appelait `charger()` puis `structure_hash()` :
    deux lectures du même artefact, donc le défaut N1 (« les octets hachés sont les octets parsés »)
    déplacé dans un écrivain. Un remplacement entre les deux faisait nommer au manifest une empreinte
    que le document publié n'applique pas. L'appelant reçoit donc le tampon qui a servi.

    Lit `structure.json`. Un artefact illisible, hors schéma ou trop large est un refus **nommé**.

    La nature de l'entrée est jugée **avant** toute ouverture : ce qui n'est pas un fichier régulier
    est refusé sans être lu. Un répertoire ou un lien pendant lèveraient bien une `OSError`, mais un
    tube, lui, ferait bloquer l'ingestion jusqu'à ce qu'un écrivain se présente — un refus doit être
    rendu, jamais attendu.

    Sa **taille** est jugée juste après, toujours sans lecture : un artefact ne peut pas peser plus
    que la charge utile qui l'a produit (`structure_max_input_chars`), puisqu'il ne fait que nommer
    des `uid` que cette charge portait déjà, chacun accompagné là-bas de sa page, de sa colonne, de
    son ordre, de sa boîte et de son texte. Sans cette borne d'entrée, un `structure.json` de dix
    millions de nœuds était entièrement désérialisé, puis vérifié, avant d'être rejeté — la borne de
    sortie (`structure_max_output_tokens`) ne borne, elle, que la réponse du réseau.

    La largeur est ensuite comptée sur la charge **brute**, avant que le moindre modèle soit bâti :
    c'est ce qui permet à `largeur_excessive` de sortir plutôt qu'un `proposition_illisible` qui ne
    dirait pas ce qui est en cause.

    `OSError` compte enfin au même titre que le hors-schéma : un fichier aux droits refusés donnait
    sinon une quarantaine sous motif générique, alors que le check promet un motif du vocabulaire
    fermé.
    """
    if not path.is_file():  # suit les liens : seul un fichier régulier atteignable est lisible
        raise StructureRefusee(
            "proposition_illisible",
            f"{path.name} présent mais illisible : ni fichier régulier, ni absent")
    settings = settings or get_settings()
    try:
        taille = path.stat().st_size
    except OSError as exc:
        raise StructureRefusee("proposition_illisible",
                               f"{path.name} illisible : {type(exc).__name__}") from exc
    if taille > settings.structure_max_input_chars:
        raise StructureRefusee(
            "largeur_excessive",
            f"{path.name} pèse {taille} octet(s) > borne STRUCTURE_MAX_INPUT_CHARS="
            f"{settings.structure_max_input_chars} : refusé sans être lu")
    try:
        with path.open("rb") as flux:
            # La taille annoncée n'est pas crue sur parole : la lecture s'arrête d'elle-même un octet
            # au-delà de la borne. Un fichier qui grandit entre l'appel et la lecture, ou dont
            # l'entrée de répertoire a changé de cible, ne fait donc pas entrer plus que la borne.
            brut = flux.read(settings.structure_max_input_chars + 1)
    except (OSError, ValueError) as exc:
        raise StructureRefusee("proposition_illisible",
                               f"{path.name} illisible : {type(exc).__name__}") from exc
    if len(brut) > settings.structure_max_input_chars:
        raise StructureRefusee(
            "largeur_excessive",
            f"{path.name} dépasse la borne STRUCTURE_MAX_INPUT_CHARS="
            f"{settings.structure_max_input_chars} : jamais lu au-delà")
    try:
        # `RecursionError` compte comme le hors-schéma : un artefact profondément imbriqué la lève
        # dans l'analyseur JSON, et elle sortirait sinon du filet — le rapport publierait alors un
        # motif générique là où le check promet un mot du vocabulaire fermé.
        charge = json.loads(brut)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise StructureRefusee("proposition_illisible",
                               f"{path.name} illisible ou hors schéma : {type(exc).__name__}") from exc
    detail = _largeur_brute(charge, settings)
    if detail is not None:
        raise StructureRefusee("largeur_excessive", f"{path.name} : {detail}")
    try:
        proposition = StructureProposee.model_validate(charge)
        if proposition.continuations_frontiere:
            raise ValueError("continuation de transport interdite dans l'artefact final")
        return proposition, brut
    except (ValidationError, ValueError, TypeError) as exc:
        raise StructureRefusee("proposition_illisible",
                               f"{path.name} illisible ou hors schéma : {type(exc).__name__}") from exc


def _texte(message: Any, settings: Settings) -> tuple[str, Usage]:
    usage = getattr(message, "usage", None)
    if not usage:  # absente **ou vide** : sans usage, aucun coût réel ne peut être certifié
        raise ValueError("réponse sans usage facturable; aucun artefact écrit")
    measured = cost_from_usage(MODEL, usage, settings.usd_eur, batch=False)
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason is not None and stop_reason not in NORMAL_STOPS:
        raise ValueError(
            f"réponse interrompue (stop_reason={stop_reason!r}, "
            f"coût réel {measured.cost_eur:.4f} €)")
    text = "".join(str(getattr(part, "text", "")) for part in (getattr(message, "content", None) or [])
                   if getattr(part, "type", None) == "text")
    return text, measured


def _usage_cumule(usages: Sequence[Usage]) -> Usage:
    return Usage(
        input=sum(usage.input for usage in usages),
        cached=sum(usage.cached for usage in usages),
        output=sum(usage.output for usage in usages),
        cost_eur=round(sum(usage.cost_eur for usage in usages), 4),
        cached_response=bool(usages) and all(usage.cached_response for usage in usages),
        cost_eur_original=round(sum(usage.cost_eur_original for usage in usages), 4),
    )


def _proposition_locale(proposition: StructureProposee,
                        line_uids: Sequence[str]) -> StructureProposee:
    """Retire seulement les arêtes transfrontières pour prouver le segment en isolation.

    Les nœuds, bornes, titres, continuations, classes et identités restent byte-for-byte ceux de
    la réponse. Parents et relations locaux restent eux aussi présents. Les arêtes externes seront
    rétablies sans modification par la validation globale, où leur cible devra être un vrai titre.
    """
    local = set(line_uids)
    nodes = [node.model_copy(update={
        "parent_line_uid": (node.parent_line_uid
                            if node.parent_line_uid in local else None),
        "relations": [relation for relation in node.relations
                      if relation.target_line_uid in local],
    }) for node in proposition.noeuds]
    return StructureProposee(
        schema_version=proposition.schema_version, doc_id=proposition.doc_id, noeuds=nodes,
        continuations_frontiere=[],
    )


def _verifier_segment(proposition: StructureProposee, registre: dict[str, Entree], *,
                      doc_id: str, settings: Settings) -> Verdict:
    """Prouve la couverture locale, continuation de préfixe comprise, sans faux nœud.

    Une continuation est au plus unique par schéma. Elle doit couvrir exactement un préfixe local
    contigu ; le reliquat est validé par le vérificateur complet après retrait des seules arêtes
    externes. Un segment composé uniquement de corps continué est donc valide sans titre inventé.
    """
    local = _proposition_locale(proposition, tuple(registre))
    continuations = proposition.continuations_frontiere
    if not continuations:
        return verifier(local, registre, doc_id=doc_id, settings=settings)
    continuation = continuations[0]
    ordered = sorted(registre.values(), key=lambda entry: entry.ordre)
    first = registre[continuation.premiere_line_uid].ordre
    last = registre[continuation.derniere_line_uid].ordre
    if first != ordered[0].ordre or last < first:
        return _refus(
            "ordre_impossible",
            "continuation de frontière non préfixe ou bornes inversées",
        )
    continued = {entry.uid for entry in ordered if first <= entry.ordre <= last}
    expected_prefix = {entry.uid for entry in ordered[:len(continued)]}
    if continued != expected_prefix:
        return _refus("ligne_omise", "continuation de frontière non contiguë")
    for node in local.noeuds:
        node_first = registre[node.premiere_line_uid].ordre
        if node_first <= last:
            return _refus(
                "intervalles_croises",
                f"nœud {node.titre_line_uid!r} chevauche la continuation de frontière",
            )
    remaining = {uid: entry for uid, entry in registre.items() if uid not in continued}
    continued_ports = {registre[uid].portage for uid in continued}
    remaining_ports = {entry.portage for entry in remaining.values()}
    split_ports = sorted(continued_ports & remaining_ports)
    if split_ports:
        return _refus(
            "affectation_non_prouvee",
            f"continuation scinde l'unité de portage {split_ports[0]!r}",
        )
    if not remaining:
        if local.noeuds:
            return _refus("ligne_inconnue", "nœud local sans ligne hors continuation")
        return Verdict(accepte=True, detail=f"continuation de {len(continued)} ligne(s)")
    if not local.noeuds:
        return _refus("ligne_omise", "lignes après continuation sans nœud local")
    return verifier(local, remaining, doc_id=doc_id, settings=settings)


def _recoller(plan: PlanStructure, propositions: Sequence[StructureProposee],
               registre: dict[str, Entree], *, doc_id: str,
               settings: Settings) -> StructureProposee:
    """Recoud les sorties sans créer de ligne et revalide la hiérarchie globale.

    Chaque sortie a déjà été validée contre son sous-registre exact. La couture conserve chaque
    nœud et relation, puis étend uniquement la dernière borne d'un parent explicitement désigné
    lorsque son enfant traverse une frontière. Elle ne crée ni ne réécrit aucune ligne. Toute cible
    absente, parent postérieur, collision, cycle ou croisement est refusé avant publication.
    """
    if len(propositions) != len(plan.segments):
        raise ValueError("couture incomplète : une réponse de segment manque")
    try:
        _valider_plan(plan, registre, doc_id=doc_id, settings=settings)
    except ValueError as exc:
        raise ValueError(f"couture refusée : {exc}") from exc
    ordered_uids = tuple(entry.uid for entry in sorted(registre.values(), key=lambda item: item.ordre))
    planned_uids = tuple(uid for segment in plan.segments for uid in segment.line_uids)
    if planned_uids != ordered_uids or len(planned_uids) != len(set(planned_uids)):
        raise ValueError("couture refusée : couverture des segments non bijective")

    nodes: list[NoeudPropose] = []
    continuations: list[ContinuationFrontiereProposee] = []
    title_owners: dict[str, int] = {}
    for segment, proposition in zip(plan.segments, propositions, strict=True):
        if proposition.doc_id != doc_id:
            raise ValueError(
                f"couture refusée : segment {segment.index} pour {proposition.doc_id!r}")
        allowed = set(segment.line_uids)
        anchor_uids = {anchor.line_uid for anchor in segment.anchors}
        for continuation in proposition.continuations_frontiere:
            bounds = {continuation.premiere_line_uid, continuation.derniere_line_uid}
            if not bounds <= allowed:
                raise ValueError(
                    f"couture refusée : continuation du segment {segment.index} hors frontière")
            if continuation.target_line_uid not in anchor_uids:
                raise ValueError(
                    f"couture refusée : continuation du segment {segment.index} vise une "
                    f"ancre inconnue {continuation.target_line_uid!r}")
            continuations.append(continuation)
        for node in proposition.noeuds:
            owned_references = {
                node.titre_line_uid,
                node.premiere_line_uid,
                node.derniere_line_uid,
                *node.title_line_uids,
                *node.continuation_line_uids,
            }
            foreign = sorted(owned_references - allowed)
            if foreign:
                raise ValueError(
                    f"couture refusée : segment {segment.index} place un titre, une borne ou "
                    f"une continuation hors frontière "
                    f"{foreign[:5]}")
            edge_targets = {
                *(relation.target_line_uid for relation in node.relations),
                *(() if node.parent_line_uid is None else (node.parent_line_uid,)),
            }
            unknown_edges = sorted(edge_targets - allowed - anchor_uids)
            if unknown_edges:
                raise ValueError(
                    f"couture refusée : segment {segment.index} vise des ancres inconnues "
                    f"{unknown_edges[:5]}")
            previous = title_owners.get(node.titre_line_uid)
            if previous is not None:
                raise ValueError(
                    f"couture refusée : titre {node.titre_line_uid!r} dupliqué entre "
                    f"segments {previous} et {segment.index}")
            title_owners[node.titre_line_uid] = segment.index
            nodes.append(node)

    by_title = {node.titre_line_uid: node for node in nodes}
    missing_continuations = sorted({continuation.target_line_uid for continuation in continuations}
                                   - set(by_title))
    if missing_continuations:
        raise ValueError(
            f"couture refusée : continuation vise une ancre non proposée "
            f"{missing_continuations[:5]}")
    for node in nodes:
        targets = [*(relation.target_line_uid for relation in node.relations)]
        if node.parent_line_uid is not None:
            targets.append(node.parent_line_uid)
        missing = sorted(target for target in targets if target not in by_title)
        if missing:
            raise ValueError(
                f"couture refusée : {node.titre_line_uid!r} vise une ancre non proposée "
                f"{missing[:5]}")

    # Calcule la fermeture transitive des bornes parentales. Un parent doit commencer avant son
    # enfant ; seule sa dernière borne peut alors être étendue. Le DFS détecte aussi les cycles
    # avant toute mutation de la proposition globale.
    order_of = {uid: entry.ordre for uid, entry in registre.items()}
    uid_at_order = {entry.ordre: uid for uid, entry in registre.items()}
    children: dict[str, list[str]] = {uid: [] for uid in by_title}
    for node in nodes:
        if node.parent_line_uid is not None:
            children[node.parent_line_uid].append(node.titre_line_uid)
    colors: dict[str, int] = {uid: 0 for uid in by_title}
    final_last: dict[str, int] = {}
    continuation_last: dict[str, int] = {}
    for continuation in continuations:
        target = continuation.target_line_uid
        continuation_last[target] = max(
            continuation_last.get(target, 0),
            order_of[continuation.derniere_line_uid],
        )

    def close_interval(uid: str) -> int:
        if colors[uid] == 1:
            raise ValueError("couture refusée : cycle de parenté transfrontière")
        if colors[uid] == 2:
            return final_last[uid]
        colors[uid] = 1
        node = by_title[uid]
        first = order_of[node.premiere_line_uid]
        last = max(order_of[node.derniere_line_uid], continuation_last.get(uid, 0))
        for child_uid in children[uid]:
            child = by_title[child_uid]
            child_first = order_of[child.premiere_line_uid]
            if child_first <= first:
                raise ValueError(
                    f"couture refusée : parent {uid!r} postérieur ou confondu avec "
                    f"l'enfant {child_uid!r}")
            last = max(last, close_interval(child_uid))
        colors[uid] = 2
        final_last[uid] = last
        return last

    for title_uid in by_title:
        close_interval(title_uid)
    reconciled = [node.model_copy(update={
        "derniere_line_uid": uid_at_order[final_last[node.titre_line_uid]],
    }) for node in nodes]

    schema_version = "2" if all(proposition.schema_version == "2"
                                  for proposition in propositions) else "1"
    stitched = StructureProposee(
        schema_version=schema_version, doc_id=doc_id, noeuds=reconciled)
    verdict = verifier(stitched, registre, doc_id=doc_id, settings=settings)
    if not verdict.accepte:
        raise ValueError(
            f"couture globale refusée ({verdict.motif}) : {verdict.detail}; rien n'a été écrit")
    return stitched


def _usage_audit(usages: Sequence[Usage], *, current_known: bool = True) -> dict[str, Any]:
    value = _usage_cumule(usages).model_dump()
    value["current_known"] = current_known
    return value


def _erreur_contexte_fournisseur(exc: BaseException) -> bool:
    """Reconnaît uniquement un refus de contexte structuré, jamais un fragment de message."""
    if getattr(exc, "status_code", None) != 400:
        return False
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return False
    error = body.get("error", body)
    if not isinstance(error, dict) or error.get("type") != "invalid_request_error":
        return False
    if error.get("code") in {"context_length_exceeded", "request_too_large"}:
        return True
    # Messages standard ne publie pas toujours `code`; dans ce cas seulement, le type 400 fermé
    # ci-dessus autorise le vocabulaire générique du protocole. Aucun document, modèle ou taille
    # observée n'entre dans cette reconnaissance.
    message = str(error.get("message", "")).casefold()
    return any(marker in message for marker in (
        "context window", "context length", "prompt is too long", "request too large",
    ))


def _raffiner_plan(plan: PlanStructure, position: int, registre: dict[str, Entree], *,
                   doc_id: str, settings: Settings) -> PlanStructure | None:
    """Scinde en deux aux frontières de portage le segment refusé, ou rend `None`."""
    segment = plan.segments[position]
    subset = {uid: registre[uid] for uid in segment.line_uids}
    groups = _groupes_portage(subset)
    if len(groups) < 2:
        return None
    middle = len(groups) // 2
    left = tuple(uid for group in groups[:middle] for uid in group)
    right = tuple(uid for group in groups[middle:] for uid in group)
    partitions = [item.line_uids for item in plan.segments]
    partitions[position:position + 1] = [left, right]
    return _construire_plan(partitions, registre, doc_id=doc_id, settings=settings)


def executer_plan(client: Any, plan: PlanStructure, registre: dict[str, Entree], *,
                  doc_id: str, settings: Settings, source_hash: str | None = None,
                  max_cost_eur: float | None = None) -> ExecutionStructure:
    """Soumet, scinde et resoumet ce que le fournisseur n'a pas pu rendre, puis recoud.

    Deux causes de scission, une seule mécanique — `_raffiner_plan`, aux frontières de portage
    existantes, mêmes invariants de contiguïté et de bijection : un refus de contexte typé, et une
    réponse **interrompue** (`stop_reason='max_tokens'`). La seconde était un refus terminal : le
    run jetait le coût déjà acquis alors que la réponse suivante, sur un segment deux fois plus
    court, tenait. Un contrat inconnu portera des zones plus denses que celles mesurées ; l'ingestion
    doit y résister par construction, pas par calibrage.

    Le nombre de scissions d'un run est borné par `STRUCTURE_MAX_REFINEMENTS`, et chaque appel perdu
    est compté au cumul avant toute décision — c'est lui qui fait respecter `--max-cost`.
    """
    _valider_plan(plan, registre, doc_id=doc_id, settings=settings)
    ceiling = settings.structure_max_cost_eur if max_cost_eur is None else max_cost_eur
    if plan.majorant_eur > ceiling:
        raise ValueError(
            f"majorant cumulé {plan.majorant_eur:.4f} € > plafond {ceiling:.4f} €; "
            "aucun appel soumis")
    artifact_uid = document_artifact_uid(document_uid=doc_id, source_hash=source_hash)
    run_uid = f"structure:{doc_id}:{plan.plan_uid}"
    propositions: list[StructureProposee] = []
    usages: list[Usage] = []
    working_plan = plan
    position = 0
    raffinements = 0
    while position < len(working_plan.segments):
        segment = working_plan.segments[position]
        anchor_uids = tuple(anchor.line_uid for anchor in segment.anchors)
        subset = {uid: registre[uid] for uid in segment.line_uids}
        trusted_uids = tuple(sorted({*segment.line_uids, *anchor_uids}))
        try:
            message = client.messages.parse(**segment.request)
        except Exception as exc:  # noqa: BLE001 - panne fournisseur = refus contrôlé
            append_ingest_audit(
                settings.llm_audit_path, run_uid=run_uid, step="structure",
                model=MODEL, request=segment.request, response=None,
                trusted_line_uids=trusted_uids, artifact_uid=artifact_uid,
                error_class=type(exc).__name__, max_bytes=settings.llm_audit_max_bytes,
                retention_files=settings.llm_audit_retention_files,
                usage_cumule=_usage_audit(usages, current_known=False))
            refined = (_raffiner_plan(
                working_plan, position, registre, doc_id=doc_id, settings=settings)
                if _erreur_contexte_fournisseur(exc)
                and raffinements < settings.structure_max_refinements else None)
            if refined is not None:
                remaining_raw = sum(
                    item.majorant_eur_brut for item in refined.segments[position:])
                # Un BadRequest de contexte ne rend généralement aucun usage. L'absence de mesure
                # ne prouve pas l'absence de facturation : avant tout retry, la tentative refusée
                # réserve donc son propre majorant, en plus du plan raffiné restant.
                refused_reserve = segment.majorant_eur_brut
                retry_bound = _arrondir_eur_superieur(
                    sum(usage.cost_eur for usage in usages)
                    + refused_reserve
                    + remaining_raw)
                if retry_bound > ceiling:
                    raise ValueError(
                        f"raffinement du segment {segment.index} refusé avant retry : majorant "
                        f"cumulé {retry_bound:.4f} € > plafond {ceiling:.4f} €; "
                        "rien n'a été écrit") from exc
                working_plan = refined
                raffinements += 1
                continue
            accumulated_cost = _usage_cumule(usages).cost_eur
            raise ValueError(
                f"segment {segment.index}/{len(working_plan.segments)} refusé "
                f"({type(exc).__name__}); coût réel cumulé {accumulated_cost:.4f} €; "
                "rien n'a été écrit") from exc
        raw = ""
        usage: Usage | None = None
        api_usage = getattr(message, "usage", None)
        measured_before_validation = (
            cost_from_usage(MODEL, api_usage, settings.usd_eur, batch=False)
            if api_usage else None
        )
        audited_usages = [*usages]
        if measured_before_validation is not None:
            audited_usages.append(measured_before_validation)
        append_ingest_audit(
            settings.llm_audit_path, run_uid=run_uid, step="structure",
            model=MODEL, request=segment.request, response=message,
            trusted_line_uids=trusted_uids, artifact_uid=artifact_uid,
            max_bytes=settings.llm_audit_max_bytes,
            retention_files=settings.llm_audit_retention_files,
            usage_cumule=_usage_audit(
                audited_usages, current_known=measured_before_validation is not None),
        )
        if getattr(message, "stop_reason", None) == "max_tokens":
            # La réponse est perdue mais **facturée** : son coût entre au cumul avant toute
            # décision, sans quoi le plafond serait jugé sur une dépense sous-déclarée.
            if measured_before_validation is not None:
                usages.append(measured_before_validation)
            acquis = _usage_cumule(usages).cost_eur
            derive = (
                f"segment {segment.index}/{len(working_plan.segments)} interrompu "
                f"(stop_reason='max_tokens') : "
                f"{_segment_impossible(len(segment.line_uids), segment.request, settings)}")
            if raffinements >= settings.structure_max_refinements:
                raise ValueError(
                    f"{derive}; borne de scission "
                    f"STRUCTURE_MAX_REFINEMENTS={settings.structure_max_refinements} atteinte; "
                    f"coût réel cumulé acquis {acquis:.4f} €; rien n'a été écrit")
            refined = _raffiner_plan(
                working_plan, position, registre, doc_id=doc_id, settings=settings)
            if refined is None:
                raise ValueError(
                    f"{derive}; l'unité de portage "
                    f"{registre[segment.line_uids[0]].portage!r} est indivisible et ne peut pas "
                    f"être scindée; coût réel cumulé acquis {acquis:.4f} €; rien n'a été écrit")
            retry_bound = _arrondir_eur_superieur(
                acquis + sum(item.majorant_eur_brut for item in refined.segments[position:]))
            if retry_bound > ceiling:
                raise ValueError(
                    f"{derive}; scission refusée avant retry : majorant cumulé "
                    f"{retry_bound:.4f} € > plafond {ceiling:.4f} €; "
                    f"coût réel cumulé acquis {acquis:.4f} €; rien n'a été écrit")
            working_plan = refined
            raffinements += 1
            continue
        try:
            raw, usage = _texte(message, settings)
            proposition = parse_proposition(
                rapprocher_indices(raw, subset, anchors=segment.anchors),
                subset, doc_id, settings=settings, reference_uids=anchor_uids)
            verdict = _verifier_segment(
                proposition, subset, doc_id=doc_id, settings=settings)
            if not verdict.accepte:
                raise ValueError(f"{verdict.motif} : {verdict.detail}")
        except ValueError as exc:
            stop_reason = getattr(message, "stop_reason", None)
            measured = usage or measured_before_validation
            accumulated = [*usages, *((measured,) if measured is not None else ())]
            accumulated_cost = _usage_cumule(accumulated).cost_eur
            segment_cost = (f"{measured.cost_eur:.4f} €"
                            if measured is not None else "inconnu (usage absent)")
            raise ValueError(
                f"segment {segment.index}/{len(working_plan.segments)} invalide : {exc} "
                f"(coût réel du segment {segment_cost}, "
                f"coût réel cumulé acquis {accumulated_cost:.4f} €, "
                f"stop_reason={stop_reason!r}, "
                f"{len(raw)} caractère(s) reçus); rien n'a été écrit") from exc
        assert usage is not None  # `_texte` ne rend jamais sans usage facturable.
        usages.append(usage)
        propositions.append(proposition)
        position += 1

    cumulative = _usage_cumule(usages)
    try:
        stitched = _recoller(
            working_plan, propositions, registre, doc_id=doc_id, settings=settings)
    except ValueError as exc:
        raise ValueError(
            f"{exc} (usage cumulé acquis input={cumulative.input}, cache={cumulative.cached}, "
            f"output={cumulative.output}; coût réel cumulé {cumulative.cost_eur:.4f} €; "
            "rien n'a été écrit") from exc
    return ExecutionStructure(proposition=stitched, plan=working_plan, usage=cumulative)


def proposer(client: Any, registre: dict[str, Entree], *, doc_id: str,
             settings: Settings, source_hash: str | None = None,
             plan: PlanStructure | None = None) -> tuple[StructureProposee, float]:
    """Appels structurés du tier `ingest`, client **injecté** : aucun n'est construit ici.

    Convention LLM du spine : `messages.parse(..., output_config={"format": …})` **sans**
    `output_format`, puis validation locale par `TypeAdapter` (`parse_proposition`). Avec
    `output_format`, le SDK 1.0.0 valide le texte avant de rendre la réponse et lève
    `ValidationError` : `usage`, `stop_reason` et le texte reçu seraient perdus — donc le coût réel
    d'un appel pourtant facturé, et le motif exact du refus. Ils sont ici tous portés par le refus.
    """
    execution = executer_plan(
        client, plan or planifier_segments(registre, doc_id=doc_id, settings=settings), registre,
        doc_id=doc_id, settings=settings, source_hash=source_hash,
    )
    return execution.proposition, execution.usage.cost_eur


def _bornes_publiees(settings: Settings) -> dict[str, Any]:
    return {name: value for name, value in settings.thresholds().items()
            if name.startswith(("structure_", "column_"))}


def _serialiser_artefact(proposition: StructureProposee, settings: Settings) -> str:
    """Sérialise les octets exacts que `charger_octets` devra pouvoir relire."""
    payload = json.dumps(proposition.model_dump(), indent=2, ensure_ascii=False) + "\n"
    size = len(payload.encode("utf-8"))
    if size > settings.structure_max_input_chars:
        raise ValueError(
            f"artefact final de {size} octets > borne STRUCTURE_MAX_INPUT_CHARS="
            f"{settings.structure_max_input_chars}; rien n'a été écrit")
    return payload


def main(argv: list[str] | None = None, *, client: Any = None, settings: Settings | None = None,
         output: Any = None) -> int:
    # `sys.stdout` est résolu à l'appel, jamais figé à l'import : la CLI suit la sortie courante.
    output = sys.stdout if output is None else output
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("doc_id", nargs="?")
    parser.add_argument("--data", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-cost", type=float)
    args = parser.parse_args(argv)
    settings = settings or get_settings()
    # AD-9 : le plafond est résolu et validé **avant** toute lecture, toute extraction et toute
    # construction de client. `nan` rend `estimate > ceiling` toujours faux et `inf` ne bloque
    # jamais : le plafond annoncé serait neutralisé juste avant l'appel le plus cher du projet.
    # `Settings` borne `structure_max_cost_eur` par `gt=0`, ce qui rejette bien `nan` mais laisse
    # passer `inf` : la garde porte donc sur la valeur **résolue**, quelle que soit sa source.
    # Code 2 (et non 3) : le run n'a rien tenté, comme un `doc_id` mal formé — 3 reste le refus
    # chiffré rendu après l'estimation (idiome `type_clauses.main`).
    ceiling = settings.structure_max_cost_eur if args.max_cost is None else args.max_cost
    if not math.isfinite(ceiling) or ceiling <= 0:
        print(f"plafond {ceiling!r} invalide : un nombre fini strictement positif est exigé "
              "(--max-cost ou STRUCTURE_MAX_COST_EUR); aucune extraction, aucun appel, "
              "rien n'a été écrit", file=sys.stderr)
        return 2
    print(f"bornes: {json.dumps(_bornes_publiees(settings), ensure_ascii=False, sort_keys=True)}",
          file=output)
    print(f"règles: {STRUCTURE_RULES_VERSION}; refus possibles: {', '.join(MOTIFS)}", file=output)
    if args.doc_id is None:
        if not args.dry_run:
            print("doc_id obligatoire hors --dry-run", file=sys.stderr)
            return 2
        print("dry-run sans document : aucune requête, aucun client construit", file=output)
        return 0
    if len(args.doc_id) > DOC_ID_MAX or not DOC_ID_RE.fullmatch(args.doc_id):
        print(f"doc_id invalide (slug [a-z0-9-]+ de {DOC_ID_MAX} caractères maximum attendu) : "
              f"{args.doc_id!r}", file=sys.stderr)
        return 2
    doc_dir = args.data / args.doc_id
    # **Un entrypoint de production exige une racine installée** (story 4.5, N3) : le refus tombe
    # avant l'extraction et avant tout appel payant de ce module, jamais après. Le lot n'a qu'une
    # cible — c'est précisément le cas pour lequel le refus « lot mixte » était inatteignable.
    try:
        exiger_espace_installe([doc_dir / "structure.json"])
    except Exception as exc:  # noqa: BLE001 — une disposition absente n'est pas une trace Python
        print(f"refus, rien n'a été écrit : {exc}", file=sys.stderr)
        return 2
    try:
        # Import tardif : `pdf_to_blocks` dépend de ce module pour vérifier, la CLI ne peut donc
        # pas l'importer au chargement sans fermer le cycle.
        from server.ingest.pdf_to_blocks import extract_pages, ordonner_pages
        pages, _toc = extract_pages(doc_dir / "source.pdf")
        ordonner_pages(pages)
        registre = registre_lignes(pages, document_uid=args.doc_id)
        if not registre:
            raise ValueError("registre vide : aucune ligne source à proposer")
        plan = planifier_segments(registre, doc_id=args.doc_id, settings=settings)
        estimate = plan.majorant_eur
        payload_chars = sum(
            len(segment.request["messages"][0]["content"]) for segment in plan.segments)
        print(
            f"{len(registre)} ligne(s) source, {payload_chars} caractère(s), "
            f"{len(plan.segments)} segment(s) ; majorant Messages standard cumulé "
            f"{estimate:.4f} € / plafond {ceiling:.4f} € ; plan {plan.plan_uid}",
            file=output,
        )
        for segment in plan.segments:
            print(
                f"segment {segment.index}/{len(plan.segments)} {segment.segment_uid} : "
                f"{len(segment.line_uids)} ligne(s), {segment.request_chars} caractère(s) "
                f"d'enveloppe, {segment.input_tokens_majorant} tokens d'entrée majorés, "
                f"max_tokens dérivé {segment.request['max_tokens']} "
                f"(sortie attendue {sortie_attendue(len(segment.line_uids), settings)}), "
                f"{segment.majorant_eur:.4f} €",
                file=output,
            )
        if estimate > ceiling:
            raise ValueError(
                f"majorant cumulé {estimate:.4f} € > plafond {ceiling:.4f} €; "
                "aucun appel soumis")
        if args.dry_run:
            print("dry-run : aucune soumission, aucune écriture", file=output)
            return 0
        if client is None:
            if cle_absente(settings):
                print("ANTHROPIC_API_KEY absente; aucun appel soumis", file=sys.stderr)
                return 2
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0)
        source_hash = hashlib.sha256((doc_dir / "source.pdf").read_bytes()).hexdigest()
        execution = executer_plan(
            client, plan, registre, doc_id=args.doc_id, settings=settings,
            source_hash=source_hash, max_cost_eur=ceiling)
        proposition = execution.proposition
        verdict = verifier(proposition, registre, doc_id=args.doc_id, settings=settings)
        usage = execution.usage
        print(f"usage cumulé input={usage.input}, cache={usage.cached}, output={usage.output}; "
              f"coût réel cumulé {usage.cost_eur:.4f} € ; "
              f"verdict {'accepté' if verdict.accepte else verdict.motif} : "
              f"{verdict.detail}", file=output)
        if not verdict.accepte:
            return 4
        write_atomic(doc_dir / "structure.json", _serialiser_artefact(proposition, settings))
    except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        print(f"proposition refusée, rien n'a été écrit : {exc}", file=sys.stderr)
        return 3
    print(f"{doc_dir / 'structure.json'} écrit", file=output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
