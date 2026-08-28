"""Story 4.2c — le modèle propose une hiérarchie sur des lignes source, le code la prouve.

    ANTHROPIC_API_KEY= uv run python -m server.ingest.structure --dry-run
    uv run python -m server.ingest.structure <doc_id> [--data data] [--max-cost 5]

La proposition ne porte que des `line_uid` du registre de lignes figé par `pdf_to_blocks` et des
liens parent/enfant. Elle ne porte **aucun texte** : les titres servis sont relus dans le registre à
partir du `line_uid` désigné, et les `node_id` sont un chemin positionnel calculé par le code. Le
schéma fournisseur énumère les `uid` autorisés et `parse_proposition` les revérifie « même si le
schéma l'impose » (idiome `type_clauses.parse_reading`).

`verifier()` est en code pur, hors réseau et **fail-closed** : un refus est nommé, il n'est jamais
rattrapé par un repli silencieux vers l'heuristique numérique (AD-16). Sans `structure.json`,
l'heuristique reste le chemin nominal ; **avec** un `structure.json` refusé, le document part en
quarantaine.

La proposition est un **artefact**, pas un appel à chaud : AD-2 exige que `source_hash` +
`ingest_fingerprint` égaux rendent les mêmes identifiants, et rejouer le modèle à chaque ingestion
rendrait l'arbre instable. Ce module écrit `data/{doc_id}/structure.json` une fois, hors ligne ;
`pdf_to_blocks.run()` le relit et le revérifie à chaque ingestion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from server.app.config import REPO_ROOT, Settings, cle_absente, get_settings
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE
from server.app.llm.models import EFFORT, TIERS
from server.app.llm.pricing import cost_from_usage, estimate_cost
from server.ingest.artifacts import write_atomic

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
TIER = "ingest"
MODEL = TIERS[TIER]
STRUCTURE_RULES_VERSION = "4.2c-1"
NORMAL_STOPS = frozenset({"end_turn", "stop_sequence", "tool_use"})
# Vocabulaire fermé des refus : un motif qui n'est pas là ne peut pas sortir du vérificateur.
MOTIFS = ("proposition_illisible", "proposition_vide", "document_different", "ligne_inconnue",
          "titre_duplique", "titre_ambigu", "cycle", "profondeur_excessive", "ordre_impossible",
          "intervalles_croises", "parent_non_contenant", "couverture_insuffisante",
          "noeud_non_construit")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoeudPropose(StrictModel):
    """Un nœud proposé : quatre ancres de lignes, et rien d'autre — ni titre, ni kind, ni portée."""

    titre_line_uid: str
    premiere_line_uid: str
    derniere_line_uid: str
    parent_line_uid: str | None = None


class StructureProposee(StrictModel):
    schema_version: Literal["1"] = "1"
    doc_id: str = Field(max_length=DOC_ID_MAX)
    noeuds: list[NoeudPropose] = Field(default_factory=list)


@dataclass(frozen=True)
class Entree:
    """Ce que le modèle voit d'une ligne : sa position et son texte, en lecture seule.

    `texte` est le texte **du registre**, immuable. `texte_porte` est celui de la ligne de travail
    qui porte cet uid : les deux ne diffèrent que lorsque `_merge_number_lines` a réuni un numéro et
    son intitulé, et c'est alors `texte_porte` qui dit ce qu'un lecteur voit — donc le titre servi.
    """

    uid: str
    page: int
    colonne: int
    ordre: int  # rang dans l'ordre de lecture du document entier, 1-indexé
    bbox: tuple[float, float, float, float]
    texte: str
    texte_porte: str = ""

    @property
    def titre(self) -> str:
        return self.texte_porte or self.texte


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


def registre_lignes(pages: list[Any]) -> dict[str, Entree]:
    """Registre adressable : les lignes source qu'un bloc peut porter, dans l'ordre de lecture.

    Les lignes écartées par un motif explicite (bande récurrente, ligne de table) n'y figurent pas :
    elles ne sont l'ancre de rien, et une proposition qui les désignerait serait refusée.
    L'ordre de lecture doit avoir été arrêté (`pdf_to_blocks.ordonner_pages`) avant l'appel.
    """
    out: dict[str, Entree] = {}
    rang = 0
    for page in pages:
        par_uid = {source.uid: source for source in page.source.lines}
        for line in page.lines:
            for uid in line.source_uids:
                source = par_uid.get(uid)
                if source is None or uid in out:
                    continue
                rang += 1
                out[uid] = Entree(uid=uid, page=source.page, colonne=line.colonne, ordre=rang,
                                  bbox=source.bbox, texte=source.text, texte_porte=line.text)
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


def _schema(uids: tuple[str, ...]) -> dict[str, Any]:
    """Le schéma fournisseur énumère les `uid` autorisés : rien d'autre ne peut être écrit."""
    uid_schema = {"type": "string", "enum": list(uids)}
    noeud = {
        "type": "object",
        "properties": {
            "titre_line_uid": uid_schema,
            "premiere_line_uid": uid_schema,
            "derniere_line_uid": uid_schema,
            "parent_line_uid": {"anyOf": [uid_schema, {"type": "null"}]},
        },
        "required": ["titre_line_uid", "premiere_line_uid", "derniere_line_uid", "parent_line_uid"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {"noeuds": {"type": "array", "items": noeud}},
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
        "output_config": {"format": _schema(tuple(sorted(registre, key=lambda u: registre[u].ordre))),
                          "effort": EFFORT[TIER]},
    }


def majorant_eur(params: dict[str, Any], settings: Settings) -> float:
    """Majorant Messages standard de l'unique appel, sans remise Batch."""
    return round(estimate_cost(params["model"], params["system"], params["messages"],
                               params["max_tokens"], settings,
                               output_schema=params["output_config"]["format"]), 4)


def parse_proposition(raw: str, registre: dict[str, Entree], doc_id: str) -> StructureProposee:
    """Défense locale, **même si le schéma l'impose** : tout `uid` étranger est refusé avant usage."""
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"noeuds"}:
            raise ValueError("objet {noeuds: [...]} attendu")
        noeuds = [NoeudPropose.model_validate(item) for item in value["noeuds"]]
    except (ValidationError, ValueError, TypeError) as exc:
        raise ValueError(f"proposition hors schéma strict ({type(exc).__name__})") from exc
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
    return StructureProposee(schema_version="1", doc_id=doc_id, noeuds=noeuds)


def _refus(motif: str, detail: str) -> Verdict:
    return Verdict(accepte=False, motif=motif, detail=detail[:1000])


def verifier(proposition: StructureProposee, registre: dict[str, Entree], *, doc_id: str,
             settings: Settings) -> Verdict:
    """Prouve les invariants d'une proposition, en code pur et hors réseau. Un refus est nommé.

    L'ordre des familles est fixe : le premier invariant enfreint donne le motif, si bien qu'une même
    proposition rend toujours le même refus.
    """
    if proposition.doc_id != doc_id:
        return _refus("document_different", f"proposition pour {proposition.doc_id!r}, document {doc_id!r}")
    if not proposition.noeuds:
        # Une proposition sans nœud passerait la couverture dès que `structure_min_coverage` vaut 0,
        # servirait un arbre plat et désarmerait l'heuristique en annonçant « proposition vérifiée ».
        return _refus("proposition_vide", "aucun nœud proposé : rien n'est structuré ni prouvé")
    noeuds = proposition.noeuds
    titres: dict[str, NoeudPropose] = {}
    for noeud in noeuds:
        for champ, uid in (("titre_line_uid", noeud.titre_line_uid),
                           ("premiere_line_uid", noeud.premiere_line_uid),
                           ("derniere_line_uid", noeud.derniere_line_uid)):
            if uid not in registre:
                return _refus("ligne_inconnue", f"{champ}={uid!r}")
        if noeud.titre_line_uid in titres:
            return _refus("titre_duplique", f"titre_line_uid={noeud.titre_line_uid!r}")
        titres[noeud.titre_line_uid] = noeud
    for noeud in noeuds:
        parent = noeud.parent_line_uid
        if parent is not None and parent not in titres:
            # Un `uid` qui existe mais n'intitule aucun nœud ne peut pas être un parent : la ligne
            # est inconnue **en tant que nœud**, et l'arbre serait suspendu à rien.
            return _refus("ligne_inconnue", f"parent_line_uid={parent!r} n'intitule aucun nœud")

    # (page, bbox) est l'identité visuelle d'un titre : deux titres au même endroit rendraient
    # l'arbre inspectable à deux réponses différentes pour la même page surlignée.
    positions: dict[tuple[int, tuple[float, float, float, float]], str] = {}
    for uid in titres:
        entree = registre[uid]
        cle = (entree.page, entree.bbox)
        if cle in positions:
            return _refus("titre_ambigu", f"{positions[cle]!r} et {uid!r} partagent (page, bbox) {cle}")
        positions[cle] = uid

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
    for uid, noeud in titres.items():
        premiere = registre[noeud.premiere_line_uid].ordre
        derniere = registre[noeud.derniere_line_uid].ordre
        titre = registre[uid].ordre
        if premiere > derniere:
            return _refus("ordre_impossible", f"{uid!r} : première ligne après la dernière")
        if not premiere <= titre <= derniere:
            return _refus("ordre_impossible", f"{uid!r} : titre hors de son propre intervalle")
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

    couvertes = sum(1 for entree in registre.values()
                    if any(premiere <= entree.ordre <= derniere for premiere, derniere in bornes.values()))
    couverture = couvertes / len(registre) if registre else 0.0
    if couverture < settings.structure_min_coverage:
        return _refus("couverture_insuffisante",
                      f"{couvertes}/{len(registre)} ligne(s) couverte(s), soit {couverture:.1%} "
                      f"< {settings.structure_min_coverage:.0%}")
    return Verdict(accepte=True, detail=f"{len(noeuds)} nœud(s), couverture {couverture:.1%}")


def arbre(proposition: StructureProposee, registre: dict[str, Entree], doc_id: str,
          ) -> tuple[dict[str, NoeudVerifie], dict[str, str]]:
    """Nœuds dérivés d'une proposition **déjà vérifiée**, et la ligne → nœud le plus profond.

    Le `node_id` est un chemin positionnel (`{doc_id}:s1.2`) calculé par le code, et le titre est
    relu dans le registre : le modèle n'écrit ni l'un ni l'autre.
    """
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
            plan[node_id] = NoeudVerifie(
                # Le titre est celui de la ligne **portée** : un intitulé que l'extracteur scinde en
                # « 1 » puis « Objet … » est servi entier, comme sur le chemin heuristique.
                node_id=node_id, level=len(position), title=registre[uid].titre,
                premiere=registre[noeud.premiere_line_uid].ordre,
                derniere=registre[noeud.derniere_line_uid].ordre,
                parent_id=None if parent_uid is None else node_ids[parent_uid],
            )
            parcourir(uid, position)

    parcourir(None, ())
    profondeur = {node_id: spec.level for node_id, spec in plan.items()}
    par_uid: dict[str, str] = {}
    for entree in registre.values():
        candidats = [node_id for node_id, spec in plan.items()
                     if spec.premiere <= entree.ordre <= spec.derniere]
        if candidats:
            par_uid[entree.uid] = max(candidats, key=lambda node_id: (profondeur[node_id], node_id))
    return plan, par_uid


def charger(path: Path) -> StructureProposee:
    """Lit `structure.json`. Un fichier illisible ou hors schéma est un refus **nommé**.

    `OSError` compte au même titre que le hors-schéma : un fichier aux droits refusés ou remplacé par
    un répertoire donnait sinon une quarantaine sous motif générique, alors que le check promet un
    motif du vocabulaire fermé.
    """
    try:
        return StructureProposee.model_validate_json(path.read_bytes())
    except (ValidationError, ValueError, OSError, UnicodeDecodeError) as exc:
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
             settings: Settings) -> tuple[StructureProposee, float]:
    """Un seul appel structuré du tier `ingest`, client **injecté** : ce module n'en construit pas."""
    params = requete(registre, doc_id, settings)
    try:
        message = client.messages.create(**params)
    except Exception as exc:  # noqa: BLE001 - toute panne fournisseur est un refus, pas une trace
        # Réseau coupé, 429, 5xx, authentification : l'appelant reçoit un refus contrôlé et la
        # promesse « rien n'a été écrit », jamais un traceback brut sorti du SDK.
        raise ValueError(f"appel refusé ({type(exc).__name__}); rien n'a été écrit") from exc
    raw, cost = _texte(message, settings)
    return parse_proposition(raw, registre, doc_id), cost


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
    try:
        # Import tardif : `pdf_to_blocks` dépend de ce module pour vérifier, la CLI ne peut donc
        # pas l'importer au chargement sans fermer le cycle.
        from server.ingest.pdf_to_blocks import extract_pages, ordonner_pages
        pages, _toc = extract_pages(doc_dir / "source.pdf")
        ordonner_pages(pages)
        registre = registre_lignes(pages)
        if not registre:
            raise ValueError("registre vide : aucune ligne source à proposer")
        params = requete(registre, args.doc_id, settings)
        estimate = majorant_eur(params, settings)
        ceiling = settings.structure_max_cost_eur if args.max_cost is None else args.max_cost
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
        proposition, cost = proposer(client, registre, doc_id=args.doc_id, settings=settings)
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
