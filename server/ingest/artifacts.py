"""Artefacts d'ingestion partagés par les ingestions (AD-7) : `document.json`, écriture atomique, manifest.

Paramétré par `doc_id` : `kb_to_blocks` (guide) et `pdf_to_blocks` (contrat) écrivent les mêmes fichiers avec les
mêmes règles. `SCHEMA_VERSION` entre dans chaque `ingest_fingerprint` : tout changement de sérialisation l'incrémente.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from server.app.domain import Document, ManifestEntry

# "2" (story 1.2) : `exclude_defaults` — les valeurs par défaut sont rétablies par le modèle au chargement (reprise 1.1).
# "3" (story 2.3) : `Document.parcours` — les conditions de profil de la source, sérialisées avec le document.
# AD-7 : l'empreinte entre dans les **deux** `ingest_fingerprint`, guide et contrat, même si seul le
# guide en porte : le champ appartient au schéma, et deux documents au même schéma le disent pareil.
SCHEMA_VERSION = "3"


def document_json(doc: Document) -> str:
    """`text_norm` n'est jamais écrit (recalculé au chargement) ; les valeurs par défaut non plus."""
    data = doc.model_dump(exclude_defaults=True, exclude={"blocks": {"__all__": {"text_norm"}}})
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


OVERLAY_FILE = "typing.manual.json"
STRUCTURE_FILE = "structure.json"


def overlay_hash(doc_dir: Path) -> str | None:
    """sha256 de `typing.manual.json` s'il existe (écrit dans le manifest, vérifié par le loader), sinon None."""
    path = doc_dir / OVERLAY_FILE
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def structure_hash(doc_dir: Path) -> str | None:
    """sha256 de `structure.json` s'il existe, sinon `None` — le patron exact d'`overlay_hash`.

    Story 4.5. La proposition de structure de 4.2c était le seul artefact d'ingestion qu'aucune
    empreinte du manifest ne couvrait, alors qu'elle décide de l'arbre que le rappel parcourt : une
    main sur le fichier ne se voyait nulle part. Le loader contrôle désormais « déclaré ⟺ présent »
    puis la valeur, et il ne peut le faire que si **les écrivains d'ingestion renseignent le champ**.

    Sans cette fonction, déposer un `structure.json` mettait le document en quarantaine avec « relancer
    l'ingestion » — et la réingestion réécrivait l'entrée sans le champ, donc ne corrigeait rien : un
    cul-de-sac où la dette 4.2c sortait le document du service et `structure_prouvee_rate` ne pouvait
    jamais devenir vert.
    """
    path = doc_dir / STRUCTURE_FILE
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def load_previous(path: Path) -> Document | None:
    """`document.json` précédent, pour `ids_disparus` ; illisible ou invalide ⇒ comme absent."""
    if not path.is_file():
        return None
    try:
        return Document.model_validate_json(path.read_bytes())
    except (ValidationError, ValueError, OSError):
        return None


def write_atomic(path: Path, text: str) -> None:
    """Écrit `path` atomiquement — **par le protocole de publication** quand la cible en relève.

    Story 4.5, B7. `data/manifest.json` est une entrée de l'espace de publication : un lien statique
    vers `data/.publie/courant/data/manifest.json`, committé une fois, que l'unique `os.replace` du
    pointeur fait basculer avec les autres surfaces. Un `tmp.replace(path)` nu détruirait ce lien et
    le remplacerait par un fichier ordinaire — silencieusement, et une seule fois suffirait : la
    bascule suivante déplacerait le pointeur sans que `data/manifest.json` en voie rien, et le run
    publierait quatre surfaces sur cinq en annonçant un succès.

    Tour correctif 3/3. Écrire **à travers** le lien évitait cette destruction mais en ouvrait une
    autre, et c'est celle que le recheck a nommée : le `rename` avait lieu **dans la génération
    active**, hors du verrou de l'espace. Le bundle n'était donc pas immuable — la génération que le
    pointeur publie changeait sous les pieds de ses lecteurs — et une ingestion concurrente courait
    avec la reconstruction et la bascule d'un run, deux écrivains sur les deux mêmes générations.

    Toute écriture d'une cible couverte par un pointeur passe donc désormais par
    `EspacePublie.basculer` : même verrou (`flock`), même génération inactive, même unique
    `os.replace`. La génération active n'est jamais mutée, et le lot d'une ingestion — une seule
    cible — est aussi tout-ou-rien que celui d'un run.

    `ecrire_gate` reste l'unique écrivain du champ `gate` (AD-7) : l'ingestion écrit l'entrée, jamais
    son gate, et `merged_manifest` ne fait que **préserver** celui qui était là. Ce qui change ici
    est le chemin d'écriture, pas qui écrit quoi.

    Une cible ordinaire — un fichier hors de toute racine de publication — n'est couverte par aucun
    pointeur : elle garde l'écriture atomique d'avant, temporaire puis `rename`, et un lien qui ne
    relève d'aucun espace continue d'être écrit **à travers** plutôt que remplacé.

    C'est la forme courte de `publier_artefacts` pour une opération dont le lot n'a qu'une cible.
    Une opération qui en écrit plusieurs ne doit **jamais** l'appeler en boucle : ce serait autant
    de points de commit que de cibles, donc l'état mêlé entre eux (tour de racine unique).
    """
    publier_artefacts([(path, text)])


def _espace_du_lot(cibles: Sequence[Path]) -> Any:
    """L'espace qui couvre **tout** le lot, `None` s'il n'en couvre aucune cible — sinon un refus.

    Un lot moitié couvert, moitié ordinaire n'a aucun geste unique qui le publie : le pointeur ne
    déplacerait que sa moitié, et l'autre serait écrite à un autre rang. C'est exactement l'état
    mêlé que l'AC interdit, et la seule réponse honnête est de **refuser avant de toucher quoi que
    ce soit**, en disant quelle cible manque à la disposition. Deux racines distinctes dans un même
    lot sont refusées pour la même raison : il n'y a pas de pointeur commun.
    """
    # Import différé : `server/evals/` importe `server/ingest/` (le runner lit les rapports
    # d'ingestion), et un import de module à module dans les deux sens serait un cycle. Le module
    # importé ici n'a lui-même aucune dépendance hors stdlib.
    from server.evals.espace import EspaceNonInstalle, LotHorsEspace, espace_couvrant

    espaces = {str(path): espace_couvrant(path) for path in cibles}
    couverts = {chemin: espace for chemin, espace in espaces.items() if espace is not None}
    if not couverts:
        return None
    hors = sorted(chemin for chemin, espace in espaces.items() if espace is None)
    if hors:
        raise EspaceNonInstalle(
            f"lot mixte : {sorted(couverts)} sont couvertes par une racine de publication, "
            f"{hors} ne le sont pas — un lot moitié couvert n'a pas de geste unique qui le publie. "
            "Poser la disposition des cibles manquantes : "
            "`python -m server.evals.espace --racine . --data-dir data --cible <chemin>`")
    racines = {str(espace.chemin) for espace in couverts.values()}
    if len(racines) != 1:
        raise LotHorsEspace(
            f"les cibles du lot relèvent de racines différentes ({sorted(racines)}) : aucun "
            "pointeur ne les bascule ensemble")
    return next(iter(couverts.values()))


def publier_artefacts(lot: Sequence[tuple[Path, str | None]]) -> None:
    """Publie le lot **complet** d'une opération d'ingestion — d'un seul geste quand une racine le couvre.

    Tour de racine unique. `write_atomic` appelé cible par cible faisait autant de points de commit
    que de cibles : une ingestion qui écrivait `document.json`, `summary.md`, `report.json` puis le
    manifest laissait, si le dernier échouait, trois artefacts neufs devant un manifest périmé.
    L'opération n'était pas tout-ou-rien, quand bien même chacune de ses écritures l'était.

    `contenu` à `None` **supprime** la cible — la suppression de l'overlay par le typage est membre
    du lot comme le reste, et non un `unlink` à un rang où une exception laisserait l'un fait et
    l'autre non.

    Quand aucune cible n'est couverte par une racine (un `data/` de test, une arborescence jetable),
    il n'y a **pas de pointeur à déplacer**, donc pas d'atome fort — mais la propriété observable
    que l'AC exige reste la même et reste tenue : **après toute exception propagée, zéro cible du
    lot n'est modifiée**. Elle s'obtient par `_publier_sans_racine`, et ce qu'elle ne garantit pas
    est écrit là plutôt que tu.

    La différence entre les deux chemins est **structurelle**, jamais un choix d'appelant : elle ne
    se pilote par aucun paramètre, et il n'existe pas de voie qui désarme le verrou d'une racine
    installée.
    """
    espace = _espace_du_lot([cible for cible, _contenu in lot])
    if espace is not None:
        espace.basculer(lot)
        return
    _publier_sans_racine(lot)


def _etat_avant(chemin: Path) -> tuple[str, object]:
    """L'état observable d'une cible, dans la forme exacte qui permet de le **rétablir**.

    Les trois formes sont celles que l'AC compare : une absence, un lien (et sa cible), un fichier
    (et ses octets). Rétablir un lien par un fichier de mêmes octets ne serait pas un rétablissement
    — c'est la septième substitution interdite du tour 2/3, un changement de type d'entrée visible
    par `lstat` comme par git.

    Capturer **lève** plutôt que de deviner : une cible illisible arrête l'opération avant qu'aucune
    autre n'ait bougé, ce qui est précisément la garantie recherchée.
    """
    if chemin.is_symlink():
        return ("lien", os.readlink(chemin))
    try:
        return ("fichier", chemin.read_bytes())
    except FileNotFoundError:
        return ("absent", None)


def _retablir(chemin: Path, etat: tuple[str, object], temporaires: list[Path]) -> None:
    """Remet une cible dans son état d'avant, **sans jamais lever**.

    Chaque rétablissement est isolé : une erreur ici ne doit pas remplacer, dans la pile de
    l'appelant, la cause qui a réellement fait échouer l'opération (tour correctif 1/3, P2). Le
    fichier passe par un temporaire enregistré, de sorte qu'un rétablissement interrompu ne laisse
    ni cible à moitié écrite, ni temporaire derrière lui.
    """
    forme, valeur = etat
    with contextlib.suppress(BaseException):
        if forme == "absent":
            if chemin.is_symlink() or chemin.exists():
                chemin.unlink()
            return
        if forme == "lien":
            if chemin.is_symlink() or chemin.exists():
                chemin.unlink()
            os.symlink(str(valeur), chemin)
            return
        tmp = chemin.with_name(chemin.name + ".restaure.tmp")
        temporaires.append(tmp)
        tmp.write_bytes(valeur)  # type: ignore[arg-type]
        os.replace(tmp, chemin)


def _publier_sans_racine(lot: Sequence[tuple[Path, str | None]]) -> None:
    """Le lot d'un arbre qu'**aucune racine ne couvre** — même propriété observable, autre mécanique.

    Reprise du tour de racine unique. Une version antérieure de ce chemin préparait ses temporaires
    puis enchaînait les `rename` sans rien défaire, au motif qu'« un arbre sans racine n'a pas la
    propriété ». C'était un affaiblissement : un `rename` réussi au rang 1 suivi d'un échec au
    rang 2 laissait la première cible **publiée**, exactement l'état mêlé que l'AC interdit, et la
    contre-sonde historique du typage
    (`tests/test_type_clauses.py::test_un_echec_de_remplacement_restaure_tous_les_artefacts`) le
    dit depuis toujours.

    Ce que ce chemin peut tenir, et qu'il tient donc :

    1. l'état d'avant des cibles est capturé **avant** la moindre écriture — une cible illisible
       arrête l'opération sans que rien n'ait bougé ;
    2. tous les temporaires sont préparés ensuite : c'est là que tombent les erreurs d'écriture, de
       place disque et de permission, toujours sans qu'aucune cible ait bougé ;
    3. les `rename` puis les suppressions s'enchaînent, chaque rang effectué étant noté ;
    4. sur **toute** exception, `BaseException` et interruption comprises, les rangs effectués sont
       rétablis en ordre inverse, chacun isolé, puis la cause d'origine est propagée ;
    5. aucun temporaire ne subsiste, quel que soit le chemin de sortie.

    Ce qu'il **ne** garantit **pas**, et qui est écrit ici plutôt que tu : POSIX n'offre aucun
    renommage multiple atomique, donc une coupure **hors exception Python** — `SIGKILL`, panne
    matérielle — entre deux appels système peut laisser un lot à moitié publié. C'est la raison
    d'être de la racine de publication, qui elle n'a rien à défaire : là où un pointeur couvre le
    lot, c'est ce chemin-là qui est pris, et cette limite disparaît. Une limite non couvrable ne
    justifie jamais une cible modifiée après une exception propagée.
    """
    # La cible réellement opérée : à travers un lien qui ne relève d'aucun espace pour une écriture
    # (ne pas le remplacer par un fichier ordinaire), le lien lui-même pour une suppression.
    operations: list[tuple[Path, str | None]] = [
        (cible if contenu is None else (Path(os.path.realpath(cible)) if cible.is_symlink() else cible), contenu)
        for cible, contenu in lot]
    # Deux entrées visant le même chemin rendraient le rétablissement ambigu — laquelle est
    # « l'état d'avant » ? Le refus est dit, avant toute écriture, plutôt que résolu par l'ordre.
    chemins = [str(chemin) for chemin, _contenu in operations]
    if len(set(chemins)) != len(chemins):
        raise ValueError(f"deux entrées du lot visent le même chemin : {sorted(chemins)}")
    avant = {str(chemin): _etat_avant(chemin) for chemin, _contenu in operations}
    temporaires: list[Path] = []
    faites: list[Path] = []
    try:
        prets: list[tuple[Path, Path]] = []
        for chemin, contenu in operations:
            if contenu is None:
                continue
            chemin.parent.mkdir(parents=True, exist_ok=True)
            tmp = chemin.with_name(chemin.name + ".tmp")
            temporaires.append(tmp)
            tmp.write_text(contenu, "utf-8")
            prets.append((tmp, chemin))
        for tmp, chemin in prets:
            tmp.replace(chemin)
            faites.append(chemin)
        for chemin, contenu in operations:
            if contenu is None:
                chemin.unlink(missing_ok=True)
                faites.append(chemin)
    except BaseException:
        for chemin in reversed(faites):
            _retablir(chemin, avant[str(chemin)], temporaires)
        raise
    finally:
        for tmp in temporaires:
            with contextlib.suppress(OSError):
                tmp.unlink()


def read_manifest(manifest_path: Path) -> dict[str, Any]:
    """Lu et validé avant toute écriture : un manifest illisible bloque sans rien modifier sur disque.

    C'est un **préflight**, pas la lecture dont la fusion part : celle-là est refaite sous le verrou
    par `fusionner_et_publier`. Lire ici sert à refuser tôt, avec un rapport bloquant plutôt qu'une
    trace Python ; s'y fier pour fusionner serait la mise à jour perdue que le tour de racine unique
    ferme.
    """
    raw: dict[str, Any] = json.loads(manifest_path.read_text("utf-8")) if manifest_path.is_file() else {}
    if not isinstance(raw, dict):
        raise ValueError(f"{manifest_path} : un objet JSON {{doc_id: entrée}} est attendu")
    return raw


def _manifest_depuis(brut: str | None, manifest_path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(brut) if brut else {}
    if not isinstance(raw, dict):
        raise ValueError(f"{manifest_path} : un objet JSON {{doc_id: entrée}} est attendu")
    return raw


def merged_manifest(raw: dict[str, Any], doc_id: str, entry: ManifestEntry) -> tuple[str, ManifestEntry]:
    """Prépare le manifest fusionné sans écrire.

    Le gate ne couvre pas seulement la source : il certifie le `document.json`, l'overlay **et**, depuis
    la story 4.5, la proposition de structure qui ont été relus. Il n'est donc conservé que si leurs
    trois empreintes sont byte-identiques. Le schéma autoritaire du gate reste inchangé ; cette
    comparaison appartient à l'ingestion.

    `structure_hash` entre dans la comparaison pour la même raison qu'`overlay_hash` y était déjà :
    le loader compare le gate à l'entrée, et conserver un gate qui certifie une autre structure
    ferait servir un document sous une validation qui ne le décrit plus.
    """
    adapter = TypeAdapter(ManifestEntry)
    previous = raw.get(doc_id)
    gate = None
    if previous:
        try:
            previous_entry = adapter.validate_python(previous)
            if (previous_entry.document_hash, previous_entry.overlay_hash,
                    previous_entry.structure_hash) == (
                    entry.document_hash, entry.overlay_hash, entry.structure_hash):
                gate = previous_entry.gate  # jamais écrit par l'ingestion (AD-7), seulement préservé
        except ValidationError as exc:
            print(f"avertissement : entrée {doc_id} précédente invalide, gate ignoré ({exc.errors()[0].get('msg', '')})",
                  file=sys.stderr)
    for other, value in raw.items():
        if other == doc_id:
            continue
        try:
            adapter.validate_python(value)
        except ValidationError:
            print(f"avertissement : entrée {other!r} du manifest invalide, conservée telle quelle", file=sys.stderr)
    entry = entry.model_copy(update={"gate": gate})
    raw[doc_id] = entry.model_dump()
    return json.dumps(dict(sorted(raw.items())), indent=2, ensure_ascii=False) + "\n", entry


def fusionner_et_publier(manifest_path: Path, doc_id: str, entry: ManifestEntry, *,
                         artefacts: Sequence[tuple[Path, str | None]] = ()) -> ManifestEntry:
    """Relit, fusionne et publie **sous le même verrou** — la fin des mises à jour perdues.

    Tour de racine unique, fait 2. `merge_manifest` fusionnait un `raw` que l'appelant avait lu
    **avant** son traitement, parfois des minutes plus tôt, et ne prenait le verrou qu'à l'écriture
    finale. Deux ingestions, ou une ingestion et un run, pouvaient donc chacune publier un manifest
    fusionné depuis un état périmé : la seconde écrasait l'entrée de la première sans jamais la
    voir. Sérialiser les commits n'y changeait rien — c'est la **lecture** qui était dehors.

    La séquence entière vit maintenant dans la section critique de la racine : `tx.lire` rend les
    octets réellement publiés, la fusion en part, et `tx.publier` bascule le manifest **avec** les
    autres artefacts de l'opération en un seul geste. Il n'y a pas de variante sans verrou : quand
    aucune racine ne couvre le manifest (un `data/` de test), il n'y a pas de pointeur à protéger,
    et le chemin d'avant reste le bon.

    `ecrire_gate` demeure l'unique écrivain du champ `gate` (AD-7) : `merged_manifest` ne fait que
    **préserver** celui qui était là, exactement comme avant.
    """
    espace = _espace_du_lot([*[cible for cible, _c in artefacts], manifest_path])
    if espace is None:
        raw = read_manifest(manifest_path)
        text, merged = merged_manifest(raw, doc_id, entry)
        publier_artefacts([*artefacts, (manifest_path, text)])
        return merged
    with espace.transaction() as transaction:
        raw = _manifest_depuis(transaction.lire(manifest_path), manifest_path)
        text, merged = merged_manifest(raw, doc_id, entry)
        transaction.publier([*artefacts, (manifest_path, text)])
    return merged


def merge_manifest(manifest_path: Path, doc_id: str, entry: ManifestEntry, *,
                   artefacts: Sequence[tuple[Path, str | None]] = ()) -> ManifestEntry:
    """Fusionne l'entrée `doc_id` ; les autres documents sont conservés tels quels, même invalides.

    Le `raw` que cette fonction prenait en paramètre a disparu **à dessein** : le garder aurait
    laissé croire qu'une lecture faite hors du verrou peut servir de base à une fusion. Elle ne le
    peut pas, et c'était le défaut. La lecture qui compte est refaite dans la transaction.
    """
    return fusionner_et_publier(manifest_path, doc_id, entry, artefacts=artefacts)
