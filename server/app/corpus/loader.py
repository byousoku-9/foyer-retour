"""AD-7 — Chargement en lecture seule de `data/` : manifest, hashes recalculés, quarantaine par document.

`corpus` n'importe que `domain` et la stdlib ; `allow_ungated` est passé par l'appelant (depuis `config.py`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from typing import get_args

from server.app.domain import BlockKind, Document, GateContext, Manifest, ManifestEntry

from .text import normalize

SOURCE_FILES = ("source.js", "source.pdf")  # la première présente est comparée à `manifest.source_hash`
REPORT_FILE = "report.json"  # AD-8 : checks statiques d'ingestion ; seul le niveau `bloquant` décide ici
OVERLAY_FILE = "typing.manual.json"  # typage manuel (FR20) fusionné avant validation ; `document.json` intact
OVERLAY_SCHEMA_VERSION = "1"
OVERLAY_FIELDS = ("kind", "defines", "scope_node_id", "scope_node_ids", "kind_source")
OVERLAY_TOP_LEVEL = ("schema_version", "doc_id", "note", "blocks")
OVERLAY_KINDS = frozenset(get_args(BlockKind))
# Champs obligatoires par kind typé à la main (revue Codex 1.2, I3) : une définition sans terme défini ne sert à rien
# à `definitions()` (1.4) ; une clause décisionnelle sans portée ne peut pas être jugée applicable (AD-2, 1.8).
OVERLAY_REQUIRED: dict[str, tuple[str, ...]] = {
    "definition": ("defines",), "garantie": ("scope_node_id",), "exclusion": ("scope_node_id",),
    "condition": ("scope_node_id",), "franchise": ("scope_node_id",),
}
# Défaut de `Settings.perimetre_max_chars` (story 2.1). Il est recopié ici — et testé égal — parce
# que `corpus` n'importe pas `config` (table des couches du spine) : l'appelant passe la valeur
# réglée, ce littéral ne sert qu'à ce que `load_corpus` reste appelable seul (tests, `evals`).
PERIMETRE_MAX_CHARS = 4000


@dataclass(frozen=True)
class VerifiedSource:
    """Chemin choisi par le loader, inséparable du hash vérifié à cet instant."""

    path: Path
    sha256: str


@dataclass
class Corpus:
    documents: dict[str, Document] = field(default_factory=dict)
    manifest: Manifest = field(default_factory=dict)
    summaries: dict[str, str] = field(default_factory=dict)
    quarantine: dict[str, str] = field(default_factory=dict)  # doc_id → raison
    alerts: dict[str, list[str]] = field(default_factory=dict)  # doc_id → alertes (document servi)
    # doc_id → périmètre du document, projection des titres de son arbre (story 2.1). C'est ce que
    # *comprendre* annonce au modèle pour classer l'`intent` : la liste écrite à la main dans
    # `prompts/comprendre.md` ne nommait pas l'identité numérique, et « Comment obtenir LuxTrust au
    # meilleur prix ? » ressortait `hors_perimetre` alors que le guide a une fiche entière dessus.
    # Calculé **une fois** au chargement : le préfixe de *comprendre* reste déterministe et
    # cacheable (AD-9), mais il devient vrai — une fiche ajoutée entre dans le périmètre sans qu'on
    # réécrive une phrase.
    perimetres: dict[str, str] = field(default_factory=dict)
    # Source effectivement vérifiée contre `manifest.source_hash` par `_load_one`. Conserver ce
    # chemin permet aux consommateurs autorisés (le lecteur de pages) de réutiliser cette décision
    # sans reconstruire un chemin depuis une valeur venue de la requête.
    source_paths: dict[str, VerifiedSource] = field(default_factory=dict)

    @property
    def served(self) -> list[str]:
        return sorted(self.documents)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _first_error(exc: ValueError) -> str:
    """Premier message d'une `ValidationError` (qui hérite de ValueError) sans importer pydantic dans `corpus`."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        items = errors()
        if items:
            return str(items[0].get("msg", ""))
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _gate_alerts(entry: ManifestEntry, current: GateContext | None, *, allow_ungated: bool) -> tuple[str, list[str]]:
    """Règle du gate (AD-7) : (raison de quarantaine, alertes).

    - pas de gate, ou gate dont `source_hash`/`ingest_fingerprint`/`overlay_hash` ≠ l'entrée ⇒ gate invalide :
      `sans_gate`
      (quarantaine, sauf `allow_ungated` ⇒ alerte) ;
    - `evals_ok=False` ⇒ `gate_echoue`, jamais servi ;
    - `pipeline_digest`/`prompts_digest`/`model_ids` ≠ `current` (si fourni) ⇒ servi avec l'alerte `gate_perime`.
    """
    gate = entry.gate
    if gate is not None and (gate.source_hash, gate.ingest_fingerprint, gate.overlay_hash) != (
            entry.source_hash, entry.ingest_fingerprint, entry.overlay_hash):
        gate = None  # le typage manuel (overlay) fait partie de ce que les témoins ont validé
    if gate is None:
        return ("", ["sans_gate"]) if allow_ungated else ("sans_gate", [])
    if not gate.evals_ok:
        return "gate_echoue", []
    if current is not None and (gate.pipeline_digest, gate.prompts_digest, gate.model_ids) != (
            current.pipeline_digest, current.prompts_digest, current.model_ids):
        return "", ["gate_perime"]
    return "", []


def _bloquant_statique(doc_dir: Path) -> str:
    """Les noms des checks `level: bloquant` de `report.json`, ou "" (D6 de la story 1.10).

    AD-8 énonce la règle du **service** : « un document est `servi` ssi aucun bloquant statique **et**
    `gate.evals_ok` ». La seconde moitié est ici depuis la story 1.1 (`_gate_alerts`) ; la première ne
    tenait que **transitivement**, par le `status` que l'ingestion écrit dans le manifest quand elle
    trouve un bloquant (`ingest/kb_to_blocks.py`, `ingest/pdf_to_blocks.py`). Une main sur
    `manifest.json` — ou une réingestion partielle — remettait donc `status: "servi"` sur un document
    dont le rapport porte « page décisionnelle corrompue », sans que rien ne le voie. Le loader relit
    donc le rapport lui-même : c'est une propriété de ce qui est **chargé**, pas de ce qui a été écrit.

    Ce qu'il ne fait **pas** : traiter un rapport absent, illisible ou étranger comme un bloquant.
    AD-8 fait du rapport un artefact d'ingestion, et un document peut être servi avant qu'on l'ait
    écrit (le guide l'a été en 1.1) ; l'illisibilité, elle, est déjà dite par les alertes
    `rapport_illisible` / `rapport_etranger` d'`api/etat` (D9 de la story 1.9), qui sont des alertes
    de la couche `api` et n'ont rien à faire dans `corpus` (table des couches du spine). Le rapport
    est donc lu deux fois au démarrage, pour deux usages disjoints — dix lignes contre une refonte de
    `Corpus` qui ferait remonter des alertes typées `api` dans `corpus`.
    """
    chemin = doc_dir / REPORT_FILE
    if not chemin.is_file():
        return ""
    try:
        rapport = json.loads(chemin.read_bytes())
    except (OSError, UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(rapport, dict) or not isinstance(rapport.get("checks"), list):
        return ""
    if rapport.get("doc_id") != doc_dir.name:
        # Un rapport **étranger** (copie de dossier, `doc_id` renommé sans réingestion) parle d'un
        # autre document : ses bloquants ne disent rien de celui-ci, et les lui appliquer retirerait
        # du service un document sain sur la foi d'un fichier qui ne le décrit pas. `api/etat` porte
        # déjà l'alerte `rapport_etranger` (revue 1.9) : l'incohérence est dite, elle n'est pas muette.
        return ""
    noms = [str(c.get("name", "?")) for c in rapport["checks"]
            if isinstance(c, dict) and c.get("level") == "bloquant"]
    return ", ".join(noms) if noms else ""


def _apply_overlay(raw_doc: dict, overlay: object) -> str:
    """Fusionne `typing.manual.json` sur le dict brut de `document.json` ; renvoie une raison de quarantaine ou "".

    Strict (revue Codex 1.2) : `schema_version` et `doc_id` obligatoires, au moins un bloc, `kind` connu et obligatoire
    avec `kind_source="manual"`, aucun champ hors `OVERLAY_FIELDS`, nœuds de portée connus, champs obligatoires par
    kind (`OVERLAY_REQUIRED`). Le loader reste générique : quels blocs un contrat donné doit typer relève de ses tests
    d'artefact (`tests/test_parsing_axa.py`) et du gate (AD-8), pas du chargement (AD-7).
    """
    if not isinstance(overlay, dict) or not isinstance(overlay.get("blocks"), dict):
        return "overlay : objet {schema_version, doc_id, blocks: {block_id: {kind, …}}} attendu"
    if overlay.get("schema_version") != OVERLAY_SCHEMA_VERSION:
        return f"overlay : schema_version {overlay.get('schema_version')!r} ≠ {OVERLAY_SCHEMA_VERSION!r}"
    if overlay.get("doc_id") != raw_doc.get("doc_id"):
        return f"overlay : doc_id {overlay.get('doc_id')!r} différent du document"
    unknown_top = sorted(set(overlay) - set(OVERLAY_TOP_LEVEL))
    if unknown_top:
        return f"overlay : champs inattendus : {unknown_top}"
    if not overlay["blocks"]:
        return "overlay : aucun bloc typé"
    blocks = {b.get("block_id"): b for b in raw_doc.get("blocks", []) if isinstance(b, dict)}
    node_ids = {n.get("node_id") for n in raw_doc.get("nodes", []) if isinstance(n, dict)}
    for block_id, entry in overlay["blocks"].items():
        if block_id not in blocks:
            return f"overlay : bloc inconnu {block_id}"
        if not isinstance(entry, dict) or entry.get("kind_source") != "manual":
            return f"overlay : kind_source ≠ manual pour {block_id}"
        if not isinstance(entry.get("kind"), str):
            return f"overlay : kind obligatoire pour {block_id}"
        if entry["kind"] not in OVERLAY_KINDS:
            return f"overlay : kind inconnu {entry['kind']!r} pour {block_id}"
        unknown = sorted(set(entry) - set(OVERLAY_FIELDS))
        if unknown:
            return f"overlay : champs inattendus pour {block_id} : {unknown}"
        scope = entry.get("scope_node_id")
        if scope is not None and scope not in node_ids:
            return f"overlay : nœud inconnu {scope} pour {block_id}"
        scopes = entry.get("scope_node_ids", [])
        if not isinstance(scopes, list) or any(n not in node_ids for n in scopes):
            return f"overlay : scope_node_ids doit lister des nœuds connus pour {block_id}"
        missing = [f for f in OVERLAY_REQUIRED.get(entry["kind"], ()) if not (isinstance(entry.get(f), str) and entry[f])]
        if missing:
            return f"overlay : {', '.join(missing)} obligatoire pour un bloc {entry['kind']} ({block_id})"
        blocks[block_id].update({k: v for k, v in entry.items()})
    return ""


def _perimetre(doc: Document, max_chars: int) -> tuple[str, bool]:
    """`(projection, catégorie(s) perdue(s) ?)` — voir `perimetre()`.

    **Trois paliers, du plus informatif au plus sûr** (revue Codex 2.1, I2). Le prompt de *comprendre*
    affirme de cette liste qu'« elle fait foi, aucune autre » : une catégorie qui en disparaît fait
    refuser `hors_perimetre` une question que le guide traite — le faux refus même que la story 2.1
    corrige. La borne ne peut donc pas commencer par retirer des catégories :

    1. catégories **et** fiches, ce que le corpus livré rend aujourd'hui (3 004 caractères sur 4 000) ;
    2. si c'est trop long, les **catégories seules** (« - Logement ») : la liste reste *exhaustive*,
       elle perd son détail. C'est le degré qui préserve exactement ce dont le prompt fait autorité ;
    3. si même cela dépasse — un `perimetre_max_chars` absurdement bas —, les dernières catégories
       tombent, la première jamais (une liste vide ferait de *tout* un hors-périmètre), et le second
       membre du couple vaut `True` : `load_corpus` en fait l'alerte `perimetre_tronque`, que
       `/api/v1/sante` publie et que la page d'accueil écrit. Dit, jamais tu (AD-16).
    """
    par_id = {n.node_id: n for n in doc.nodes}
    titres: list[tuple[str, list[str]]] = []
    for node in doc.nodes:
        if node.level != 1 or not node.title.strip():
            continue
        enfants = [par_id[c].title.strip() for c in node.children
                   if c in par_id and par_id[c].title.strip()]
        titres.append((node.title.strip(), enfants))

    detaillees = [f"- {t}" + (" : " + ", ".join(e) if e else "") for t, e in titres]
    if len("\n".join(detaillees)) <= max_chars:
        return "\n".join(detaillees), False
    lignes = [f"- {t}" for t, _ in titres]  # exhaustive, sans le détail des fiches
    perdues = False
    while len(lignes) > 1 and len("\n".join(lignes)) > max_chars:
        lignes.pop()
        perdues = True
    return "\n".join(lignes), perdues


def perimetre(doc: Document, max_chars: int = PERIMETRE_MAX_CHARS) -> str:
    """Périmètre d'un document : ses nœuds de **niveau 1** et leurs enfants directs, une ligne chacun.

    Le niveau 1 est la catégorie (« Logement »), ses enfants directs sont les fiches (« Signer un
    bail », « Assurer son logement ») : c'est exactement la granularité qu'il faut pour dire « ce
    sujet est dans le guide » sans recopier le sommaire — les titres sont écrits par l'ingestion, pas
    par un modèle, et aucun texte de bloc n'y entre (AD-10).

    **Borné en retirant des lignes entières, jamais en coupant une ligne** : une catégorie tronquée
    au milieu d'un titre de fiche ferait croire au modèle que la fiche s'appelle autrement. Le détail
    des fiches tombe **avant** toute catégorie (voir `_perimetre`), et la première ligne ne tombe
    jamais.
    """
    return _perimetre(doc, max_chars)[0]


def _load_one(doc_dir: Path, doc_id: str, entry: ManifestEntry, *, allow_ungated: bool,
              current: GateContext | None) -> tuple[Document | None, str, list[str]]:
    """Renvoie (document | None, raison de quarantaine, alertes). Aucune exception ne sort : tout devient une raison."""
    if entry.status == "quarantaine":
        return None, "quarantaine (manifest)", []
    if doc_dir.name != doc_id:
        return None, f"dossier {doc_dir.name!r} différent du doc_id", []
    doc_path = doc_dir / "document.json"
    if not doc_path.is_file():
        return None, "document.json absent", []
    try:
        if _sha256(doc_path) != entry.document_hash:
            return None, "document_hash différent du manifest", []
        source_found = False
        for name in SOURCE_FILES:
            src = doc_dir / name
            if src.is_file():
                source_found = True
                if _sha256(src) != entry.source_hash:
                    return None, f"source_hash différent du manifest ({name})", []
                break
        raw_doc = json.loads(doc_path.read_bytes())
        overlay_path = doc_dir / OVERLAY_FILE
        # L'overlay est couvert par le manifest (`overlay_hash`) comme `document.json` l'est par `document_hash`.
        if overlay_path.is_file() != (entry.overlay_hash is not None):
            return None, ("overlay : typing.manual.json présent mais non déclaré dans le manifest (relancer l'ingestion)"
                          if overlay_path.is_file() else "overlay : déclaré dans le manifest mais absent"), []
        if overlay_path.is_file():
            if _sha256(overlay_path) != entry.overlay_hash:
                return None, "overlay_hash différent du manifest (relancer l'ingestion)", []
            try:
                overlay = json.loads(overlay_path.read_bytes())
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                return None, f"overlay illisible : {_first_error(exc)}"[:500], []
            reason = _apply_overlay(raw_doc, overlay) if isinstance(raw_doc, dict) else ""
            if reason:
                return None, reason, []
        doc = Document.model_validate(raw_doc)
    except ValueError as exc:  # ValidationError et JSONDecodeError en héritent
        return None, f"document.json invalide : {_first_error(exc)}", []
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"document.json illisible : {type(exc).__name__}: {exc}"[:500], []
    if doc.doc_id != doc_id:
        return None, f"doc_id {doc.doc_id!r} différent de la clé du manifest", []
    if doc.source_hash != entry.source_hash:
        return None, "source_hash du document différent du manifest", []
    if doc.ingest_fingerprint != entry.ingest_fingerprint:
        return None, "ingest_fingerprint du document différent du manifest", []
    if doc.edition != entry.edition:
        return None, f"edition {doc.edition!r} différente du manifest ({entry.edition!r})", []
    if not (doc_dir / "summary.md").is_file():
        return None, "sommaire_absent", []
    # AD-8, avant le gate et **avant** toute dérogation : `ALLOW_UNGATED` déroge à l'absence de
    # questions-témoins (AD-7 la nomme « dev / J+1 avant le premier gate »), jamais à un contrat
    # illisible. Un bloquant statique met ce seul document en quarantaine, quel que soit l'environnement.
    bloquants = _bloquant_statique(doc_dir)
    if bloquants:
        return None, f"bloquant_statique : {bloquants}", []
    reason, alerts = _gate_alerts(entry, current, allow_ungated=allow_ungated)
    if reason:
        return None, reason, []
    if not source_found:
        alerts.append("source_absente")
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    return doc, "", alerts


def load_corpus(data_dir: Path | str, *, allow_ungated: bool, current: GateContext | None = None,
                perimetre_max_chars: int = PERIMETRE_MAX_CHARS) -> Corpus:
    """Charge chaque document du manifest ; une incohérence met ce seul document en quarantaine (AD-7).

    `current` décrit l'image en cours (digests, modèles) ; sans lui, la péremption du gate n'est pas évaluée.
    `perimetre_max_chars` borne la projection des titres rendue à *comprendre* (story 2.1) ; son
    défaut est celui de `config.Settings`, que `corpus` ne peut pas importer.
    """
    data_dir = Path(data_dir)
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        return Corpus()
    try:
        raw = json.loads(manifest_path.read_bytes())
        if not isinstance(raw, dict):
            raise ValueError("un objet JSON {doc_id: entrée} est attendu")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return Corpus(quarantine={"*": f"manifest invalide : {_first_error(exc)}"[:500]})
    corpus = Corpus()
    for doc_id in sorted(raw):
        try:
            entry = ManifestEntry.model_validate(raw[doc_id])
        except ValueError as exc:
            corpus.quarantine[doc_id] = f"entrée de manifest invalide : {_first_error(exc)}"
            continue
        corpus.manifest[doc_id] = entry
        doc, reason, alerts = _load_one(data_dir / doc_id, doc_id, entry, allow_ungated=allow_ungated, current=current)
        if doc is None:
            corpus.quarantine[doc_id] = reason
            continue
        try:
            summary = (data_dir / doc_id / "summary.md").read_text("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            corpus.quarantine[doc_id] = f"summary.md illisible : {type(exc).__name__}: {exc}"[:500]
            continue
        corpus.documents[doc_id] = doc
        # `_load_one` vérifie la première source présente de `SOURCE_FILES`. On ne retient donc que
        # cette source-là : un éventuel `source.pdf` placé après un `source.js` n'a pas été vérifié
        # et ne doit jamais devenir lisible par la route documentaire.
        for source_name in SOURCE_FILES:
            source_path = data_dir / doc_id / source_name
            if source_path.is_file():
                corpus.source_paths[doc_id] = VerifiedSource(path=source_path, sha256=entry.source_hash)
                break
        corpus.summaries[doc_id] = summary
        corpus.perimetres[doc_id], tronque = _perimetre(doc, perimetre_max_chars)
        if tronque:
            # AD-16 / revue Codex 2.1 (I2) : le prompt de *comprendre* dit de cette liste qu'elle
            # « fait foi » ; amputée d'une catégorie entière, elle fait refuser des questions que le
            # document traite. Un réglage qui en arrive là est un écart, et il se voit.
            alerts.append("perimetre_tronque")
        corpus.alerts[doc_id] = alerts
    return corpus
