"""Story 4.2c — le modèle propose une hiérarchie sur des lignes source, le code la prouve.

    ANTHROPIC_API_KEY= uv run python -m server.ingest.structure --dry-run
    uv run python -m server.ingest.structure <doc_id> [--data data] [--max-cost 5]

La proposition ne porte que des `line_uid` du registre de lignes figé par `pdf_to_blocks` et des
liens parent/enfant. Elle ne porte **aucun texte** : les titres servis sont relus dans le registre à
partir du `line_uid` désigné, et les `node_id` sont un chemin positionnel calculé par le code. Le
schéma fournisseur énumère les `uid` autorisés et `parse_proposition` les revérifie « même si le
schéma l'impose » (idiome `type_clauses.parse_reading`).

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
from server.app.llm.models import EFFORT, TIERS
from server.app.llm.audit import append_ingest_audit
from server.app.llm.pricing import cost_from_usage, estimate_cost
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
          "titre_duplique", "titre_ambigu", "cycle", "profondeur_excessive", "largeur_excessive",
          "ordre_impossible", "intervalles_croises", "parent_non_contenant", "ligne_omise",
          "affectation_non_prouvee", "noeud_non_construit")
# `line-v1:` + SHA-256 hexadécimal. La borne empêche toujours un artefact de concentrer son poids
# dans un seul identifiant, mais elle décrit désormais la même identité que `Document.Line`.
LINE_UID_MAX = len("line-v1:") + 64

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


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RelationProposee(StrictModel):
    kind: NodeRelationKind
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
    """Forme **filaire** exacte de la réponse : un objet `{noeuds: [...]}`, et rien d'autre.

    Le modèle n'écrit ni `schema_version`, ni `doc_id` : les deux sont posés par le code. Ce modèle
    dédié est la validation locale qu'impose la convention LLM du spine — celle que `output_format`
    ferait faire au SDK, au prix d'`usage`, de `stop_reason` et du texte reçu.
    """

    noeuds: list[NoeudPropose]


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
                                  unite=unite, source_uids=(uid,))
    return out


def demande(registre: dict[str, Entree], settings: Settings) -> str:
    """Charge utile : uid, page, colonne, ordre, bbox et texte. Rien d'autre, aucune clé de sortie.

    Le `texte` publié est celui de la ligne **portée** — c'est-à-dire ce qu'un lecteur voit à cette
    position, numéro d'article fusionné compris. Les deux uid d'une ligne fusionnée montrent donc la
    même chaîne : ils désignent la même ligne visuelle, et désigner l'un ou l'autre comme titre rend
    le même intitulé.
    """
    lignes = [{"uid": entree.uid, "page": entree.page, "colonne": entree.colonne,
               "ordre": entree.ordre, "bbox": list(entree.bbox), "texte": entree.titre}
              for entree in sorted(registre.values(), key=lambda e: e.ordre)]
    payload = json.dumps({"lignes": lignes}, ensure_ascii=False, separators=(",", ":"))
    if len(payload) > settings.structure_max_input_chars:
        raise ValueError(
            f"charge utile de {len(payload)} caractères > borne "
            f"STRUCTURE_MAX_INPUT_CHARS={settings.structure_max_input_chars}; aucun appel soumis"
        )
    return payload


def _prompt() -> str:
    return (PROMPTS_DIR / "structure.md").read_text("utf-8")


def _schema(uids: tuple[str, ...], settings: Settings) -> dict[str, Any]:
    """Le schéma fournisseur énumère les `uid` autorisés : rien d'autre ne peut être écrit.

    `maxItems` y borne la largeur dès la surface d'écriture. Le nombre d'enfants d'un parent n'est
    pas exprimable en JSON Schema ; `maxItems` le borne transitivement — une fratrie ne compte jamais
    plus de nœuds que l'arbre entier —, et `parse_proposition` le revérifie exactement.
    """
    uid_schema = {"type": "string", "enum": list(uids)}
    noeud = {
        "type": "object",
        "properties": {
            "titre_line_uid": uid_schema,
            "premiere_line_uid": uid_schema,
            "derniere_line_uid": uid_schema,
            "parent_line_uid": {"anyOf": [uid_schema, {"type": "null"}]},
            "title_line_uids": {"type": "array", "items": uid_schema, "minItems": 1},
            "article_uid": {"anyOf": [{"type": "string", "maxLength": 256}, {"type": "null"}]},
            "surface_class": {"type": "string", "enum": [
                "preliminaire", "table_des_matieres", "substantiel", "inconnu"]},
            "continuation_line_uids": {"type": "array", "items": uid_schema},
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": [
                            "same_clause_continuation", "explicit_dependency",
                            "definition_override"]},
                        "target_line_uid": uid_schema,
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
    schema = {
        "type": "object",
        "properties": {"noeuds": {"type": "array", "items": noeud,
                                  "maxItems": settings.structure_max_nodes}},
        "required": ["noeuds"],
        "additionalProperties": False,
    }
    return {"type": "json_schema", "schema": schema}


def requete(registre: dict[str, Entree], doc_id: str, settings: Settings) -> dict[str, Any]:
    """Paramètres de l'unique appel structuré du tier `ingest`. Aucun client n'est construit ici."""
    return {
        "model": MODEL,
        "max_tokens": settings.structure_max_output_tokens,
        "system": [{"type": "text", "text": _prompt()}],
        "messages": [{"role": "user", "content": demande(registre, settings)}],
        "output_config": {"format": _schema(tuple(sorted(registre, key=lambda u: registre[u].ordre)),
                                            settings),
                          "effort": EFFORT[TIER]},
    }


def majorant_eur(params: dict[str, Any], settings: Settings) -> float:
    """Majorant Messages standard de l'unique appel, sans remise Batch."""
    return round(estimate_cost(params["model"], params["system"], params["messages"],
                               params["max_tokens"], settings,
                               output_schema=params["output_config"]["format"]), 4)


def parse_proposition(raw: str, registre: dict[str, Entree], doc_id: str, *,
                      settings: Settings | None = None) -> StructureProposee:
    """Défense locale, **même si le schéma l'impose** : tout `uid` étranger est refusé avant usage.

    Deux temps, dans cet ordre. D'abord la **forme filaire**, validée localement par
    `TypeAdapter(PropositionFilaire).validate_json` — la convention LLM du spine, celle que
    `client.py` applique déjà. Puis les contrôles d'`uid` : appartenance au registre et unicité des
    titres. La validation locale ne les remplace pas — aucun schéma pydantic ne connaît le registre
    de ce document — elle les précède.
    """
    try:
        noeuds = _FILAIRE.validate_json(raw).noeuds
    except (ValidationError, ValueError, TypeError) as exc:
        raise ValueError(f"proposition hors schéma strict ({type(exc).__name__})") from exc
    # La largeur d'abord : elle borne le travail de tout ce qui suit, ici comme dans `verifier`.
    detail = largeur_hors_borne(noeuds, settings or get_settings())
    if detail is not None:
        raise ValueError(f"largeur_excessive : {detail}")
    vus: set[str] = set()
    for noeud in noeuds:
        for champ, uid in (("titre_line_uid", noeud.titre_line_uid),
                           ("premiere_line_uid", noeud.premiere_line_uid),
                           ("derniere_line_uid", noeud.derniere_line_uid),
                           ("parent_line_uid", noeud.parent_line_uid)):
            if uid is not None and uid not in registre:
                raise ValueError(f"{champ}: line_uid inconnu du registre {uid!r}")
        if noeud.titre_line_uid in vus:
            raise ValueError(f"titre_line_uid dupliqué {noeud.titre_line_uid!r}")
        vus.add(noeud.titre_line_uid)
    # Compatibilité des doubles historiques : le schéma filaire courant impose v2, mais les tests
    # ou reprises v1 sont migrés sans inventer de relation. L'identité d'article reste explicitement
    # nulle et la classe conservatrice ``inconnu`` interdit la citation jusqu'à re-proposition.
    migrated = []
    for node in noeuds:
        if not node.title_line_uids:
            node = node.model_copy(update={
                "title_line_uids": [node.titre_line_uid],
            # Migration de lecture uniquement : avant V2, seules les lignes préliminaires/TDM
            # étaient exclues par leur provenance. Une proposition V1 portant le corps est donc
            # substantielle ; les nouvelles propositions doivent déclarer la classe explicitement.
            "surface_class": "substantiel",
            })
        migrated.append(node)
    version = "2" if all({"title_line_uids", "article_uid", "surface_class",
                           "continuation_line_uids", "relations"} <= node.model_fields_set
                         for node in noeuds) else "1"
    return StructureProposee(schema_version=version, doc_id=doc_id, noeuds=migrated)


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
            first = registre[noeud.premiere_line_uid].ordre
            last = registre[noeud.derniere_line_uid].ordre
            title_set = set(noeud.title_line_uids)
            supporting_texts = [
                entry.titre for uid, entry in registre.items()
                if first <= entry.ordre <= last and uid not in title_set
            ]
            technical_surface = oracle_surface_class(title, supporting_texts)
            if technical_surface is None:
                return _refus(
                    "affectation_non_prouvee",
                    f"surface sans preuve locale indépendante sous {noeud.titre_line_uid!r}",
                )
            if noeud.surface_class != technical_surface:
                return _refus(
                    "affectation_non_prouvee",
                    f"surface {technical_surface!r} lisible mais classée {noeud.surface_class!r}",
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
            if len(child_path) > 1 and not (
                    len(child_path) > len(parent_path)
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
                surface_class=(noeud.surface_class or
                               ("substantiel" if proposition.schema_version == "1" else "inconnu")),
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
        return StructureProposee.model_validate(charge), brut
    except (ValidationError, ValueError, TypeError) as exc:
        raise StructureRefusee("proposition_illisible",
                               f"{path.name} illisible ou hors schéma : {type(exc).__name__}") from exc


def _texte(message: Any, settings: Settings) -> tuple[str, float]:
    usage = getattr(message, "usage", None)
    if not usage:  # absente **ou vide** : sans usage, aucun coût réel ne peut être certifié
        raise ValueError("réponse sans usage facturable; aucun artefact écrit")
    cost = cost_from_usage(MODEL, usage, settings.usd_eur, batch=False).cost_eur
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason is not None and stop_reason not in NORMAL_STOPS:
        raise ValueError(f"réponse interrompue (stop_reason={stop_reason!r}, coût réel {cost:.4f} €)")
    text = "".join(str(getattr(part, "text", "")) for part in (getattr(message, "content", None) or [])
                   if getattr(part, "type", None) == "text")
    return text, cost


def proposer(client: Any, registre: dict[str, Entree], *, doc_id: str,
             settings: Settings, source_hash: str | None = None) -> tuple[StructureProposee, float]:
    """Un seul appel structuré du tier `ingest`, client **injecté** : ce module n'en construit pas.

    Convention LLM du spine : `messages.parse(..., output_config={"format": …})` **sans**
    `output_format`, puis validation locale par `TypeAdapter` (`parse_proposition`). Avec
    `output_format`, le SDK 1.0.0 valide le texte avant de rendre la réponse et lève
    `ValidationError` : `usage`, `stop_reason` et le texte reçu seraient perdus — donc le coût réel
    d'un appel pourtant facturé, et le motif exact du refus. Ils sont ici tous portés par le refus.
    """
    params = requete(registre, doc_id, settings)
    artifact_uid = document_artifact_uid(document_uid=doc_id, source_hash=source_hash)
    try:
        message = client.messages.parse(**params)
    except Exception as exc:  # noqa: BLE001 - toute panne fournisseur est un refus, pas une trace
        # Réseau coupé, 429, 5xx, authentification : l'appelant reçoit un refus contrôlé et la
        # promesse « rien n'a été écrit », jamais un traceback brut sorti du SDK.
        append_ingest_audit(
            settings.llm_audit_path, run_uid=f"structure:{doc_id}", step="structure",
            model=MODEL, request=params, response=None,
            trusted_line_uids=tuple(sorted(registre)), artifact_uid=artifact_uid,
            error_class=type(exc).__name__, max_bytes=settings.llm_audit_max_bytes,
            retention_files=settings.llm_audit_retention_files)
        raise ValueError(f"appel refusé ({type(exc).__name__}); rien n'a été écrit") from exc
    append_ingest_audit(
        settings.llm_audit_path, run_uid=f"structure:{doc_id}", step="structure",
        model=MODEL, request=params, response=message,
        trusted_line_uids=tuple(sorted(registre)), artifact_uid=artifact_uid,
        max_bytes=settings.llm_audit_max_bytes,
        retention_files=settings.llm_audit_retention_files)
    raw, cost = _texte(message, settings)
    try:
        return parse_proposition(raw, registre, doc_id, settings=settings), cost
    except ValueError as exc:
        stop_reason = getattr(message, "stop_reason", None)
        raise ValueError(f"{exc} (coût réel {cost:.4f} €, stop_reason={stop_reason!r}, "
                         f"{len(raw)} caractère(s) reçus); rien n'a été écrit") from exc


def _bornes_publiees(settings: Settings) -> dict[str, Any]:
    return {name: value for name, value in settings.thresholds().items()
            if name.startswith(("structure_", "column_"))}


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
    # avant l'extraction et avant le seul appel payant de ce module, jamais après. Le lot n'a qu'une
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
        params = requete(registre, args.doc_id, settings)
        estimate = majorant_eur(params, settings)
        print(f"{len(registre)} ligne(s) source, {len(params['messages'][0]['content'])} caractère(s) ; "
              f"majorant Messages standard {estimate:.4f} € / plafond {ceiling:.4f} €", file=output)
        if estimate > ceiling:
            raise ValueError(f"majorant {estimate:.4f} € > plafond {ceiling:.4f} €; aucun appel soumis")
        if args.dry_run:
            print("dry-run : aucune soumission, aucune écriture", file=output)
            return 0
        if client is None:
            if cle_absente(settings):
                print("ANTHROPIC_API_KEY absente; aucun appel soumis", file=sys.stderr)
                return 2
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0)
        source_hash = hashlib.sha256((doc_dir / "source.pdf").read_bytes()).hexdigest()
        proposition, cost = proposer(
            client, registre, doc_id=args.doc_id, settings=settings, source_hash=source_hash)
        verdict = verifier(proposition, registre, doc_id=args.doc_id, settings=settings)
        print(f"coût réel {cost:.4f} € ; verdict {'accepté' if verdict.accepte else verdict.motif} : "
              f"{verdict.detail}", file=output)
        if not verdict.accepte:
            return 4
        write_atomic(doc_dir / "structure.json",
                     json.dumps(proposition.model_dump(), indent=2, ensure_ascii=False) + "\n")
    except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        print(f"proposition refusée, rien n'a été écrit : {exc}", file=sys.stderr)
        return 3
    print(f"{doc_dir / 'structure.json'} écrit", file=output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
