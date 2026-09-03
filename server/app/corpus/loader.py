"""AD-7 — Chargement en lecture seule de `data/` : manifest, hashes recalculés, quarantaine par document.

`corpus` n'importe que `domain` et la stdlib ; `allow_ungated` est passé par l'appelant (depuis `config.py`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from typing import get_args

from server.app.domain import BlockKind, Document, GateContext, Manifest, ManifestEntry, Report

from .racine import CapaciteRegate, Lecture, relire
from .text import normalize

SOURCE_FILES = ("source.js", "source.pdf")  # la première présente est comparée à `manifest.source_hash`
REPORT_FILE = "report.json"  # AD-8 : checks statiques d'ingestion ; seul le niveau `bloquant` décide ici
OVERLAY_FILE = "typing.manual.json"  # typage manuel (FR20) fusionné avant validation ; `document.json` intact
# Proposition de structure de la story 4.2c, couverte par le manifest depuis 4.5 (`structure_hash`),
# exactement comme l'overlay l'est. Elle n'est pas fusionnée au chargement — l'arbre est déjà dans
# `document.json` — mais un artefact qui a bougé sans réingestion doit se voir.
STRUCTURE_FILE = "structure.json"
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
LOG = logging.getLogger("foyer.corpus.loader")


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

    @property
    def served(self) -> list[str]:
        return sorted(self.documents)


def _sha256(octets: bytes) -> str:
    """L'empreinte des **octets qu'on a lus**, jamais d'une seconde ouverture du même chemin.

    Story 4.5, tour de la racine vraiment unique (N1). Cette fonction prenait un `Path` et rouvrait
    le fichier : `document.json` était donc **haché** par un `open()` puis **parsé** par un autre,
    et `typing.manual.json` de même. Sous une racine de publication, une bascule tombant entre les
    deux fait porter le contrôle d'empreinte sur des octets que personne n'utilise — le contrôle qui
    existe *pour* interdire le mélange ne le voyait pas. Les octets hachés sont désormais, par
    construction, les octets parsés : une seule lecture, un seul tampon.
    """
    return hashlib.sha256(octets).hexdigest()


def _first_error(exc: ValueError) -> str:
    """Premier message d'une `ValidationError` (qui hérite de ValueError) sans importer pydantic dans `corpus`."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        items = errors()
        if items:
            return str(items[0].get("msg", ""))
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _read_error(exc: OSError | UnicodeDecodeError | ValueError) -> str:
    """Diagnostic de lecture sans publier le chemin attaché à un ``OSError``.

    Les raisons de quarantaine remontent jusqu'aux surfaces HTTP d'audit. Un ``OSError`` inclut
    souvent le chemin absolu ouvert ; son type suffit à distinguer l'échec, tandis que les erreurs
    de schéma/JSON gardent leur premier message utile.
    """
    return type(exc).__name__ if isinstance(exc, OSError) else _first_error(exc)


def _seuils_du_gate_projetes(du_gate: dict[str, int | float | str | bool],
                             courants: dict[str, int | float | str | bool],
                             ) -> dict[str, int | float | str | bool]:
    """Les seuils du gate lus **sur les clés du contexte courant**, et sur elles seules.

    Le contexte courant ne porte que le sous-ensemble « pipeline » de `thresholds()`
    (`Settings.gate_thresholds`, story 5.6 T20) : les interrupteurs d'exploitation en sont
    absents, parce qu'ils diffèrent par construction entre le poste qui mesure un gate et l'image
    de production, et qu'ils ne changent ni une claim, ni un verdict, ni une citation.

    La projection porte des deux côtés, et c'est ce qui garde **les gates déjà écrits** frais : un
    gate mesuré avant ce correctif porte les 180 seuils publiés, dont les trois interrupteurs qui
    ont fait refuser trois déploiements. Comparer les dictionnaires entiers l'aurait périmé pour
    des clés que le contexte courant ne prétend plus juger — une relance de campagne facturée pour
    rien. Une clé **du sous-ensemble** absente du gate, elle, vaut `None` et périme : c'est un gate
    qui ne dit pas sous quel seuil il a mesuré, pas un gate qui en dit trop.
    """
    return {nom: du_gate.get(nom) for nom in courants}


def _gate_alerts(entry: ManifestEntry, current: GateContext | None, *, allow_ungated: bool) -> tuple[str, list[str]]:
    """Règle du gate (AD-7) : (raison de quarantaine, alertes).

    - pas de gate, ou gate dont `source_hash`/`ingest_fingerprint`/`overlay_hash`/`structure_hash` ≠
      l'entrée ⇒ gate invalide : `sans_gate`
      (quarantaine, sauf `allow_ungated` ⇒ alerte) ;
    - `evals_ok=False` ⇒ `gate_echoue`, jamais servi ;
    - `pipeline_digest`/`prompts_digest`/`model_ids` ≠ `current` (si fourni) ⇒ servi avec l'alerte `gate_perime` ;
    - seuils : seuls ceux que `current` porte sont comparés — le sous-ensemble « pipeline »
      (`Settings.gate_thresholds`), projeté des deux côtés (`_seuils_du_gate_projetes`).
    """
    gate = entry.gate
    if gate is not None and (gate.source_hash, gate.ingest_fingerprint, gate.overlay_hash,
                             gate.structure_hash) != (
            entry.source_hash, entry.ingest_fingerprint, entry.overlay_hash,
            entry.structure_hash):
        # Le typage manuel (overlay) **et** la proposition de structure (story 4.5) font partie de
        # ce que les témoins ont validé. Écrire `structure_hash` dans le gate sans jamais le
        # recouper avec l'entrée aurait laissé servir, sans une alerte, un document réingéré avec
        # une autre structure sous un gate qui certifie l'ancienne.
        gate = None
    if gate is None:
        return ("", ["sans_gate"]) if allow_ungated else ("sans_gate", [])
    if not gate.evals_ok:
        return "gate_echoue", []
    if gate.profile == "full" and (not gate.decisions or not gate.run_digest
                                   or not gate.plancher_digest or not gate.candidate_revision):
        # Story 4.5 : la sévérité `full` couvre aussi les champs neufs. Un gate `full` sans
        # `plancher_digest` ne dit pas contre quel protocole il a été vert ; sans
        # `candidate_revision`, il ne dit pas quel commit il a mesuré. Servir sous le label de la
        # politique complète un gate qui ne sait ni l'un ni l'autre serait affirmer une mesure dont
        # on a perdu la référence — le même défaut que `gate_preprotocole` nomme depuis 4.2b.
        return "gate_preprotocole", []
    if (gate.profile == "full" and current is not None
            and not _meme_revision(gate.candidate_revision, current.candidate_revision,
                                   env=current.env)):
        # Story 4.5 (revue B2) : un gate `full` **nomme** la révision qu'il a mesurée. Servir sous
        # ce label un code dont la révision diffère, c'est affirmer une mesure qui n'a pas porté sur
        # lui — et `pipeline_digest` ne le rattrape pas : il ne couvre que cinq couches, pas le
        # dépôt. La quarantaine est la même que pour des digests non concordants, et pour la même
        # raison : sous `full`, la politique complète promet que le servi *est* le mesuré.
        return "gate_perime", []
    if current is not None and (
            gate.pipeline_digest, gate.prompts_digest, gate.model_ids,
            _seuils_du_gate_projetes(gate.pipeline_settings, current.pipeline_settings)) != (
            current.pipeline_digest, current.prompts_digest, current.model_ids,
            current.pipeline_settings):
        # Story 4.2b : sous un gate `full`, des digests non concordants ne sont plus une simple
        # alerte — la politique complète promet que le document servi est exactement l'image que la
        # campagne a mesurée, et servir autre chose sous ce label serait la bascule silencieuse
        # qu'AD-11/AD-16 interdisent. Quarantaine. Sous `vertical`, l'alerte `gate_perime` reste :
        # le profil n'affirme que deux cas relus, pas la politique complète.
        if gate.profile == "full":
            return "gate_perime", []
        return "", ["gate_perime"]
    return "", []


# Une révision **identifie un commit**, ou elle n'est pas une révision : quarante hexadécimaux,
# jamais un préfixe. C'est la même exigence que `plancher.verifier_liaison_preuve` oppose déjà à une
# preuve trusted ; les deux moitiés du même invariant doivent demander la même chose.
REVISION_LONGUEUR = 40


def revision_comparable(valeur: str | None) -> str | None:
    """La révision utilisable pour une comparaison, ou `None` si elle n'en est pas une.

    `dev` (le défaut hors conteneur), une chaîne vide, ou une forme **tronquée** : ce sont trois
    façons de ne pas savoir exactement quelle révision tourne, et aucune n'est une révision.

    Story 4.5, revue B2 : la version précédente acceptait un préfixe de sept caractères et comparait
    par préfixe commun. C'est la forme même de la production — le gate porte 40 hexadécimaux, le
    service en portait 7 — et la comparaison ne discriminait donc plus que 16⁷ classes : un gate
    `full` d'un **autre** commit partageant le sha7 était servi sans alerte. On ne relâche pas la
    comparaison pour absorber l'ambiguïté ; on lève l'ambiguïté (`GIT_SHA` porte la révision
    complète, `Settings.version_publiee` en projette la forme courte pour l'affichage).
    """
    texte = (valeur or "").strip().lower()
    if len(texte) != REVISION_LONGUEUR or any(c not in "0123456789abcdef" for c in texte):
        return None
    return texte


def _meme_revision(gate_revision: str | None, courante: str, *, env: str = "dev") -> bool:
    """Le gate décrit-il la révision qui tourne ? — **identité stricte**, jamais un préfixe.

    **Quand la révision est inconnue, la réponse dépend de l'environnement**, et c'est le patron
    qu'AD-7 applique déjà à `allow_ungated` :

    - hors production, ne rien pouvoir conclure n'est pas une raison de refuser de servir : un poste
      de développement ne se nomme pas, et mettre son corpus en quarantaine n'apprendrait rien à
      personne ;
    - en **production**, sous un gate `full`, une révision inconnue, tronquée ou ambiguë est une
      **preuve manquante**. Le profil promet que le servi est exactement le mesuré ; ne pas pouvoir
      l'établir, c'est ne pas pouvoir tenir la promesse. Quarantaine.
    """
    gate_lue = revision_comparable(gate_revision)
    courante_lue = revision_comparable(courante)
    if gate_lue is None or courante_lue is None:
        return env != "prod"
    return gate_lue == courante_lue


def _gate_full_preprotocole(brut: object) -> bool:
    """Le gate brut est-il un `full` **antérieur au protocole** de la story 4.5 ?

    C'est-à-dire : il se déclare `full`, et il ne porte pas — ou porte mal formé — le protocole
    (`plancher_digest`) ou la révision (`candidate_revision`) que ce profil exige désormais. La
    lecture est faite sur le **brut**, sans passer par le modèle, précisément parce que le modèle
    vient de le refuser.
    """
    if not isinstance(brut, dict):
        return False
    gate = brut.get("gate")
    if not isinstance(gate, dict) or gate.get("profile") != "full":
        return False
    return not all(isinstance(gate.get(champ), str) and gate.get(champ)
                   for champ in ("plancher_digest", "candidate_revision"))


def _raison_entree_invalide(brut: object, exc: ValueError) -> str:
    """La raison de quarantaine d'une entrée refusée — **nommée** quand c'est le gate qui pèche.

    Sans ce détour, un gate `full` écrit avant la story 4.5 rendait toute son entrée invalide au
    schéma, et le document partait en quarantaine « entrée de manifest invalide : … » — un message
    qui parle du manifest alors que le manifest va bien, et qui masque exactement le diagnostic que
    `gate_preprotocole` existe pour donner (« ce gate a été obtenu avant le protocole en vigueur, il
    doit être refait »). Le symptôme est le même que celui qu'`ecrire_gate` a rencontré au tour 2 de
    la story 1.10, et le remède est le sien : **revalider sans le gate**.

    Le détour ne vaut que si l'entrée est **par ailleurs valide** — un `document_hash` manquant reste
    une entrée invalide — et que si le gate est un `full` pré-protocole. Toute autre invalidité du
    gate (un `cases: 0`, un `evals_ok` incohérent) garde le message générique : elle n'a pas de nom.
    """
    if _gate_full_preprotocole(brut) and isinstance(brut, dict):
        try:
            ManifestEntry.model_validate({**brut, "gate": None})
        except ValueError:
            pass  # l'entrée pèche ailleurs qu'au gate : le message générique est le bon
        else:
            return "gate_preprotocole"
    return f"entrée de manifest invalide : {_first_error(exc)}"


def _bloquant_statique(doc_dir: Path, lecture: Lecture) -> str:
    """Le défaut statique de `report.json`, ou "" lorsqu'il est absent ou valide sans bloquant.

    AD-8 énonce la règle du **service** : « un document est `servi` ssi aucun bloquant statique **et**
    `gate.evals_ok` ». La seconde moitié est ici depuis la story 1.1 (`_gate_alerts`) ; la première ne
    tenait que **transitivement**, par le `status` que l'ingestion écrit dans le manifest quand elle
    trouve un bloquant (`ingest/kb_to_blocks.py`, `ingest/pdf_to_blocks.py`). Une main sur
    `manifest.json` — ou une réingestion partielle — remettait donc `status: "servi"` sur un document
    dont le rapport porte « page décisionnelle corrompue », sans que rien ne le voie. Le loader relit
    donc le rapport lui-même : c'est une propriété de ce qui est **chargé**, pas de ce qui a été écrit.

    L'absence historique reste tolérée : le guide a précédé cet artefact. En revanche, dès qu'un
    fichier est présent, le loader échoue fermé : une forme JSON ou un schéma invalide ne prouve pas
    l'absence de bloquant et met donc ce document en quarantaine. `api/etat` conserve en parallèle
    l'erreur publique précise (`rapport_illisible`) ; les deux lectures ont des usages disjoints.

    Un rapport valide mais étranger reste distinct : ses checks ne décrivent pas ce document et ne
    peuvent donc lui être appliqués. La couche API le refuse et publie `rapport_etranger`.
    """
    chemin = doc_dir / REPORT_FILE
    if not lecture.fichier(chemin):
        return ""
    try:
        rapport = Report.model_validate_json(lecture.reel(chemin).read_bytes())
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"rapport_statique_illisible : {_read_error(exc)}"
    if rapport.doc_id != doc_dir.name:
        # Un rapport **étranger** (copie de dossier, `doc_id` renommé sans réingestion) parle d'un
        # autre document : ses bloquants ne disent rien de celui-ci, et les lui appliquer retirerait
        # du service un document sain sur la foi d'un fichier qui ne le décrit pas. `api/etat` porte
        # déjà l'alerte `rapport_etranger` (revue 1.9) : l'incohérence est dite, elle n'est pas muette.
        return ""
    noms = [check.name for check in rapport.blocking]
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
              current: GateContext | None, lecture: Lecture,
              raison_max_chars: int = 500) -> tuple[Document | None, str, list[str]]:
    """Renvoie (document | None, raison de quarantaine, alertes). Aucune exception ne sort : tout devient une raison.

    Toutes les lectures passent par `lecture`, le **repère pincé** de la passe (N1) : le manifest
    dont vient `entry` et les artefacts qu'on lui oppose viennent de la même génération, et chaque
    artefact n'est ouvert **qu'une fois** — l'empreinte porte sur les octets qui sont ensuite parsés.
    """
    if entry.status == "quarantaine":
        return None, "quarantaine (manifest)", []
    if doc_dir.name != doc_id:
        return None, f"dossier {doc_dir.name!r} différent du doc_id", []
    doc_path = doc_dir / "document.json"
    if not lecture.fichier(doc_path):
        return None, "document.json absent", []
    try:
        doc_octets = lecture.reel(doc_path).read_bytes()
        if _sha256(doc_octets) != entry.document_hash:
            return None, "document_hash différent du manifest", []
        source_found = False
        for name in SOURCE_FILES:
            src = doc_dir / name
            if lecture.fichier(src):
                source_found = True
                if _sha256(lecture.reel(src).read_bytes()) != entry.source_hash:
                    return None, f"source_hash différent du manifest ({name})", []
                break
        raw_doc = json.loads(doc_octets)
        overlay_path = doc_dir / OVERLAY_FILE
        overlay_present = lecture.fichier(overlay_path)
        # L'overlay est couvert par le manifest (`overlay_hash`) comme `document.json` l'est par `document_hash`.
        if overlay_present != (entry.overlay_hash is not None):
            return None, ("overlay : typing.manual.json présent mais non déclaré dans le manifest (relancer l'ingestion)"
                          if overlay_present else "overlay : déclaré dans le manifest mais absent"), []
        if overlay_present:
            try:
                overlay_octets = lecture.reel(overlay_path).read_bytes()
            except (OSError, UnicodeDecodeError) as exc:
                return None, f"overlay illisible : {_read_error(exc)}"[:raison_max_chars], []
            if _sha256(overlay_octets) != entry.overlay_hash:
                return None, "overlay_hash différent du manifest (relancer l'ingestion)", []
            try:
                overlay = json.loads(overlay_octets)
            except (UnicodeDecodeError, ValueError) as exc:
                return None, f"overlay illisible : {_read_error(exc)}"[:raison_max_chars], []
            reason = _apply_overlay(raw_doc, overlay) if isinstance(raw_doc, dict) else ""
            if reason:
                return None, reason, []
        # `structure.json` sur le patron exact d'`overlay_hash` (story 4.5) : déclaré ⟺ présent,
        # puis la valeur. La proposition de structure décide de l'arbre que le rappel parcourt ; un
        # fichier remplacé sans réingestion changerait ce que les questions-témoins ont validé, sans
        # qu'une seule empreinte du manifest ne bouge.
        structure_path = doc_dir / STRUCTURE_FILE
        structure_presente = lecture.fichier(structure_path)
        if structure_presente != (entry.structure_hash is not None):
            return None, ("structure : structure.json présent mais non déclaré dans le manifest "
                          "(relancer l'ingestion)"
                          if structure_presente
                          else "structure : déclarée dans le manifest mais absente"), []
        if structure_presente and _sha256(
                lecture.reel(structure_path).read_bytes()) != entry.structure_hash:
            return None, "structure_hash différent du manifest (relancer l'ingestion)", []
        doc = Document.model_validate(raw_doc)
    except ValueError as exc:  # ValidationError et JSONDecodeError en héritent
        return None, f"document.json invalide : {_first_error(exc)}", []
    except (OSError, UnicodeDecodeError) as exc:
        # La raison devient publique dans l'audit (story 3.5). Le type suffit à distinguer l'échec ;
        # ``str(OSError)`` peut contenir le chemin absolu de ``data/`` et ne doit jamais sortir.
        # ``doc_id`` vient de la clé brute du manifest. ``%r`` conserve le détail interne tout en
        # échappant les retours à la ligne, donc une clé hostile ne forge pas une ligne de log.
        LOG.warning("document.json illisible pour %r : %r", doc_id, exc)
        return None, f"document.json illisible : {type(exc).__name__}", []
    if doc.doc_id != doc_id:
        return None, f"doc_id {doc.doc_id!r} différent de la clé du manifest", []
    if doc.source_hash != entry.source_hash:
        return None, "source_hash du document différent du manifest", []
    if doc.ingest_fingerprint != entry.ingest_fingerprint:
        return None, "ingest_fingerprint du document différent du manifest", []
    if doc.edition != entry.edition:
        return None, f"edition {doc.edition!r} différente du manifest ({entry.edition!r})", []
    if not lecture.fichier(doc_dir / "summary.md"):
        return None, "sommaire_absent", []
    # AD-8, avant le gate et **avant** toute dérogation : `ALLOW_UNGATED` déroge à l'absence de
    # questions-témoins (AD-7 la nomme « dev / J+1 avant le premier gate »), jamais à un contrat
    # illisible. Un bloquant statique met ce seul document en quarantaine, quel que soit l'environnement.
    bloquants = _bloquant_statique(doc_dir, lecture)
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
                perimetre_max_chars: int = PERIMETRE_MAX_CHARS,
                raison_max_chars: int = 500, lecture: Lecture | None = None,
                regate: str | None = None,
                capacite_regate: CapaciteRegate | None = None,
                neutraliser_regate: bool = False) -> Corpus:
    """Charge chaque document du manifest ; une incohérence met ce seul document en quarantaine (AD-7).

    `current` décrit l'image en cours (digests, modèles) ; sans lui, la péremption du gate n'est pas évaluée.
    `perimetre_max_chars` borne la projection des titres rendue à *comprendre* (story 2.1) ; son
    défaut est celui de `config.Settings`, que `corpus` ne peut pas importer.

    `lecture` est le **repère pincé** de l'opération de lecture qui englobe ce chargement (N1, story
    4.5) : une passe qui charge le corpus *puis* les dictionnaires, les rapports et la publication
    d'évals doit lire toutes ses surfaces couvertes dans **une seule** génération. Sans lui, le
    chargement pince la sienne, le temps de sa propre passe. Il n'existe pas de paramètre qui
    rétablisse une résolution vivante : ou il y a un espace installé, et tout passe par lui, ou il
    n'y en a pas, et il n'y a rien à mêler.

    `regate` et `capacite_regate` sont indissociables : la cible ne peut être relue qu'avec la
    capacité issue de `lecture_pincee_regate`, le même objet `lecture`, sa génération non nulle et
    le même `data_dir`. `neutraliser_regate` est **fail-closed** par défaut (`False`) : même avec ce
    trio qualifié, le gate ciblé reste présent. Seul le runner, après avoir établi que ce gate est
    rouge, périmé, préprotocole ou hors schéma, passe explicitement `True`. La neutralisation ne
    modifie que la copie mémoire du manifest ; elle n'élargit jamais `allow_ungated`, ne touche
    aucune autre entrée et n'autorise aucun autre repère.
    """
    data_dir = Path(data_dir)
    if (regate is None) != (capacite_regate is None):
        raise ValueError(
            "une neutralisation de gate exige ensemble une cible et sa capacité de regate")
    if capacite_regate is not None and (lecture is None or not allow_ungated):
        raise ValueError(
            "une neutralisation de gate exige un repère explicite et allow_ungated=True")
    if lecture is None:
        # **`relire`, jamais un simple pincement** (revue du tour N1–N3, constat 1). Pincer sans
        # jamais consulter la péremption laissait la passe rendre un état composé de deux
        # générations quand la génération pincée était reconstruite sous elle — précisément ce que
        # N1 déclare impossible. `relire` rejoue la passe sur un repère neuf, et dit le refus après
        # un nombre borné de tentatives.
        return relire(data_dir, lambda pincee: load_corpus(
            data_dir, allow_ungated=allow_ungated, current=current,
            perimetre_max_chars=perimetre_max_chars, raison_max_chars=raison_max_chars,
            lecture=pincee))
    manifest_path = data_dir / "manifest.json"
    if capacite_regate is None and not lecture.fichier(manifest_path):
        return Corpus()
    if capacite_regate is not None and regate is not None:
        # Une erreur d'opposition est une erreur de contrat, pas un manifest métier à mettre en
        # quarantaine : elle doit refuser immédiatement et rester visible de l'appelant.
        raw = capacite_regate.manifest_regate(
            lecture=lecture, data_dir=data_dir, cible=regate,
            neutraliser=neutraliser_regate)
    else:
        try:
            raw = json.loads(lecture.reel(manifest_path).read_bytes())
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            if isinstance(exc, OSError):
                LOG.warning("manifest illisible %s : %s", manifest_path, exc)
            return Corpus(
                quarantine={"*": f"manifest invalide : {_read_error(exc)}"[:raison_max_chars]})
    try:
        if not isinstance(raw, dict):
            raise ValueError("un objet JSON {doc_id: entrée} est attendu")
    except ValueError as exc:
        return Corpus(quarantine={"*": f"manifest invalide : {_read_error(exc)}"[:raison_max_chars]})
    corpus = Corpus()
    racine_resolue = data_dir.resolve()
    for doc_id in sorted(raw):
        try:
            entry = ManifestEntry.model_validate(raw[doc_id])
        except ValueError as exc:
            corpus.quarantine[doc_id] = _raison_entree_invalide(raw[doc_id], exc)
            continue
        corpus.manifest[doc_id] = entry
        doc_dir = data_dir / doc_id
        if not doc_dir.resolve().is_relative_to(racine_resolue):
            # La clé non publiable est annoncée anonymement par l'API ; la raison ne doit donc pas
            # révéler si elle était absolue, traversante ou un lien symbolique.
            corpus.quarantine[doc_id] = "quarantaine (manifest)"
            continue
        doc, reason, alerts = _load_one(
            doc_dir, doc_id, entry, allow_ungated=allow_ungated, current=current,
            lecture=lecture, raison_max_chars=raison_max_chars)
        if doc is None:
            corpus.quarantine[doc_id] = reason
            continue
        try:
            summary = lecture.reel(data_dir / doc_id / "summary.md").read_text("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            LOG.warning("summary.md illisible pour %r : %r", doc_id, exc)
            corpus.quarantine[doc_id] = f"summary.md illisible : {type(exc).__name__}"
            continue
        corpus.documents[doc_id] = doc
        corpus.summaries[doc_id] = summary
        corpus.perimetres[doc_id], tronque = _perimetre(doc, perimetre_max_chars)
        if tronque:
            # AD-16 / revue Codex 2.1 (I2) : le prompt de *comprendre* dit de cette liste qu'elle
            # « fait foi » ; amputée d'une catégorie entière, elle fait refuser des questions que le
            # document traite. Un réglage qui en arrive là est un écart, et il se voit.
            alerts.append("perimetre_tronque")
        corpus.alerts[doc_id] = alerts
    return corpus
