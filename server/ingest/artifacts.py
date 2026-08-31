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
from collections.abc import Callable, Sequence
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
TYPING_REUSED_IDS_STAT = "ids_typage_reutilises"


# `overlay_hash`, `structure_hash` et `load_previous` ont été **supprimés** (story 4.5, revue du tour
# N1–N3, constat 14). C'étaient trois lectures d'artefacts couverts faites hors du verrou : elles
# décidaient du contenu publié depuis un état qu'une opération concurrente pouvait avoir remplacé.
# Leurs remplaçantes — `LectureDuLot.empreinte` et `LectureDuLot.document_precedent` — ne sont
# atteignables que **dans** la transaction qui publie. Les laisser exportées aurait laissé croire
# qu'il reste une voie légitime de lire ces artefacts hors verrou : il n'y en a pas.


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


def write_atomic_bytes(path: Path, content: bytes) -> None:
    """Variante binaire de :func:`write_atomic`, soumise au même point de commit."""
    publier_artefacts([(path, content)])


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
        racine = next(iter(couverts.values()))
        raise EspaceNonInstalle(
            f"lot mixte : {sorted(couverts)} sont couvertes par une racine de publication, "
            f"{hors} ne le sont pas — un lot moitié couvert n'a pas de geste unique qui le publie. "
            "Poser la disposition complète du dépôt (c'est la commande que la documentation "
            "opérateur donne, et elle est idempotente) : `python -m server.evals.espace --racine "
            f"{racine.racine} --data-dir {racine.data_dir} --depot`")
    racines = {str(espace.chemin) for espace in couverts.values()}
    if len(racines) != 1:
        raise LotHorsEspace(
            f"les cibles du lot relèvent de racines différentes ({sorted(racines)}) : aucun "
            "pointeur ne les bascule ensemble")
    return next(iter(couverts.values()))


def exiger_espace_installe(cibles: Sequence[Path]) -> None:
    """Le préflight d'un **entrypoint de production** : une racine installée, ou un refus avant tout.

    Story 4.5, tour de la racine vraiment unique (N3). `verifier_couverture_du_lot` ne lève que sur
    un lot **mixte** ou deux racines : quand `espace_couvrant` rend `None` pour *toutes* les cibles,
    `_espace_du_lot` rend `None` sans lever, et `publier_artefacts` prend silencieusement le repli
    rootless. Sept entrypoints de production atteignaient ce repli — dont trois **après** avoir payé
    des appels de modèle, et quatre avec un lot d'une seule cible, pour lequel le refus « lot mixte »
    est structurellement inatteignable. Une cible couverte dont le lien avait été cassé était alors
    réécrite en fichier ordinaire, silencieusement, et ne se signalait qu'à l'opération multi-cibles
    suivante.

    La garde ne dépend donc plus d'un lot mixte : **aucune** cible couverte, c'est un refus, quel que
    soit le nombre de cibles. Elle n'interdit pas les `data-dir` custom — un custom **installé** a sa
    propre racine et passe sans traitement particulier — elle interdit les `data-dir` **non
    installés**. Le repli rootless reste ce qu'il est, une primitive interne qu'aucun entrypoint de
    production n'atteint (la contre-sonde historique du typage l'exerce directement, et le doit).
    """
    from server.evals.espace import EspaceNonInstalle

    if _espace_du_lot(cibles) is None:
        chemins = sorted(str(cible) for cible in cibles)
        raise EspaceNonInstalle(
            f"aucune racine de publication ne couvre {chemins} : écrire sans racine n'est pas une "
            "opération tout-ou-rien, et un entrypoint de production ne le fait pas. Poser la "
            "disposition (idempotente) : `python -m server.evals.espace --racine <racine> "
            "--data-dir <data> --depot`")


def verifier_couverture_du_lot(cibles: Sequence[Path]) -> None:
    """Préflight : ce lot est-il publiable d'un seul geste ? — **sans rien écrire, ni lire**.

    Revue du tour de racine unique, constat 6. Le refus « lot mixte » ne tombait qu'au **dernier**
    geste d'une opération d'ingestion : un typage LLM entièrement payé était jeté pour une
    disposition qu'on pouvait vérifier avant la première soumission, et l'appelant n'en voyait
    qu'une trace Python là où il convertit tous les autres refus en check bloquant.

    Cette fonction est exactement la question posée par `_espace_du_lot`, posée **tôt** : elle lève
    les mêmes exceptions, avec le même message, et ne fait rien d'autre. Elle ne remplace pas le
    contrôle final — la disposition peut changer entre les deux — elle le déplace là où il coûte le
    moins cher.
    """
    _espace_du_lot(cibles)


def deposer_par_la_racine(lot: Sequence[tuple[Path, str | bytes | None]]) -> None:
    """La voie **opérateur** de dépôt d'un artefact écrit à la main — overlay de typage compris.

    Patch croisé 1/3, `N3-OVERLAY-BYPASS`. La procédure documentée appelait directement
    `publier_artefacts`, qui sur une cible custom **non installée** fait rendre `None` à
    `_espace_du_lot` et prend le repli rootless : le septième entrypoint cartographié atteignait donc
    publiquement, et officiellement, la primitive que N3 déclare inaccessible en production. Le repli
    ne peut pas être à la fois « interne » et prescrit par la documentation.

    Cette fonction est la procédure : elle exige une racine installée sur le lot **avant** de
    publier, refuse en nommant `--depot` sinon, et délègue ensuite à `publier_artefacts` inchangée.
    Un `data-dir` custom **installé** passe sans traitement particulier ; c'est le non installé qui
    ferme.
    """
    exiger_espace_installe([cible for cible, _ in lot])
    publier_artefacts(lot)


def publier_artefacts(lot: Sequence[tuple[Path, str | bytes | None]]) -> None:
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


def _publier_sans_racine(lot: Sequence[tuple[Path, str | bytes | None]]) -> None:
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
    operations: list[tuple[Path, str | bytes | None]] = [
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
            if isinstance(contenu, bytes):
                tmp.write_bytes(contenu)
            else:
                tmp.write_text(contenu, "utf-8")
            prets.append((tmp, chemin))
        for tmp, chemin in prets:
            # **Le rang se note avant l'appel système, jamais après.** `KeyboardInterrupt` peut
            # tomber à la frontière d'instruction qui suit un `rename` réussi : noter ensuite
            # laisserait la cible publiée et hors du rétablissement, c'est-à-dire une cible modifiée
            # après une exception propagée — ce que l'AC interdit littéralement, `BaseException`
            # comprise. L'asymétrie décide : rétablir une cible qui n'a pas bougé est inoffensif
            # (on y réécrit ses propres octets), l'inverse ne l'est pas.
            faites.append(chemin)
            tmp.replace(chemin)
        for chemin, contenu in operations:
            if contenu is None:
                faites.append(chemin)
                chemin.unlink(missing_ok=True)
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


class LectureDuLot:
    """Ce qu'un écrivain a le droit de lire pour **décider** du contenu qu'il publie.

    Story 4.5, tour de la racine vraiment unique (N3, second volet). Le tour précédent n'avait fermé
    la mise à jour perdue que sur les **octets du manifest** : `overlay_hash()`, `structure_hash()`,
    `load_previous()` et les champs repris de l'entrée antérieure restaient évalués **comme
    arguments d'appel**, donc avant l'ouverture de la transaction — parfois des minutes avant, dans
    le cas du typage. Ce qui décide du contenu publié doit se lire sous le verrou, depuis le repère
    que la transaction a pincé : sinon l'entrée publiée décrit un état que la publication contredit.

    Sous une racine, la lecture passe par `Transaction.chemin_publie`, c'est-à-dire par le slot de
    la génération courante, dans le repère de la transaction. Sans racine — un arbre de test — il
    n'y a ni pointeur ni verrou à protéger, et le chemin est lu tel quel : c'est la même
    dissymétrie structurelle que celle de `publier_artefacts`, jamais un paramètre.
    """

    def __init__(self, transaction: Any = None) -> None:
        self._transaction = transaction

    def chemin(self, cible: Path) -> Path:
        return Path(cible) if self._transaction is None else self._transaction.chemin_lu(cible)

    def octets(self, cible: Path) -> bytes | None:
        try:
            return self.chemin(cible).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            # Même règle que côté lecteur (revue N1–N3, constat 15) : l'absence est une absence,
            # tout le reste remonte plutôt que d'être lu comme un artefact vide.
            return None

    def empreinte(self, cible: Path) -> str | None:
        """sha256 des octets **publiés** de `cible`, ou `None` si elle est absente.

        La règle est exactement celle d'`overlay_hash`/`structure_hash`, dite sous le verrou : ce
        qui n'est pas un fichier régulier est une absence, et l'empreinte porte sur les octets que
        la génération courante publie, pas sur ceux qu'un lien pouvait désigner minutes plus tôt.
        """
        chemin = self.chemin(cible)
        return hashlib.sha256(chemin.read_bytes()).hexdigest() if chemin.is_file() else None

    def document_precedent(self, cible: Path) -> Document | None:
        """Le `document.json` publié, pour `ids_disparus` ; illisible ou invalide ⇒ comme absent."""
        chemin = self.chemin(cible)
        if not chemin.is_file():
            return None
        try:
            return Document.model_validate_json(chemin.read_bytes())
        except (ValidationError, ValueError, OSError):
            return None


def republier(cibles: Sequence[Path],
              fabrique: Callable[[LectureDuLot], Sequence[tuple[Path, str | None]]]) -> None:
    """Relit et republie un lot **sous le même verrou** — le read-modify-write d'une cible couverte.

    Story 4.5, tour de la racine vraiment unique (N3). `enrich_dictionary --valider` lisait le
    dictionnaire, le signait, puis l'écrivait par `write_atomic` : trois gestes à cheval sur le
    verrou, donc un enrichissement concurrent publié entre la lecture et l'écriture était écrasé par
    une signature portant l'ancien contenu. C'est le même défaut que celui du manifest, sur une
    autre surface, et il se ferme de la même façon : la lecture, la transformation et la publication
    vivent dans la même section critique.

    Sans racine — un `data/` de test —, il n'y a pas de pointeur à protéger, et le chemin d'avant
    reste le bon. La différence est structurelle, jamais un paramètre.
    """
    espace = _espace_du_lot(cibles)
    if espace is None:
        publier_artefacts(list(fabrique(LectureDuLot(None))))
        return
    with espace.transaction() as transaction:
        transaction.publier(list(fabrique(LectureDuLot(transaction))))


def fusionner_et_publier(manifest_path: Path, doc_id: str,
                         entree: ManifestEntry | Callable[
                             [LectureDuLot], tuple[ManifestEntry, Sequence[tuple[Path, str | None]]]],
                         *, artefacts: Sequence[tuple[Path, str | None]] = (),
                         cibles: Sequence[Path] = ()) -> ManifestEntry:
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

    Tour de la racine vraiment unique (N3). `entree` peut désormais être une **fabrique** —
    `(LectureDuLot) -> (entrée, artefacts)` — plutôt qu'une entrée déjà construite. C'est ce qui
    permet à `overlay_hash`, `structure_hash`, au document précédent et aux champs repris de
    l'entrée antérieure d'être lus **dans** la transaction, depuis le repère qu'elle a pincé, au
    lieu d'être évalués comme arguments d'appel avant elle. `cibles` déclare, pour le préflight de
    couverture, les chemins que la fabrique publiera : le lot doit être connu avant d'ouvrir la
    section critique, puisque c'est lui qui désigne la racine.
    """
    lot_declare = [*[cible for cible, _c in artefacts], *cibles, manifest_path]
    espace = _espace_du_lot(lot_declare)

    def _composer(lecture: LectureDuLot) -> tuple[ManifestEntry, list[tuple[Path, str | None]]]:
        if callable(entree):
            entry, arts = entree(lecture)
            return entry, [*artefacts, *arts]
        return entree, [*artefacts]

    if espace is None:
        entry, arts = _composer(LectureDuLot(None))
        raw = read_manifest(manifest_path)
        text, merged = merged_manifest(raw, doc_id, entry)
        publier_artefacts([*arts, (manifest_path, text)])
        return merged
    with espace.transaction() as transaction:
        entry, arts = _composer(LectureDuLot(transaction))
        raw = _manifest_depuis(transaction.lire(manifest_path), manifest_path)
        text, merged = merged_manifest(raw, doc_id, entry)
        transaction.publier([*arts, (manifest_path, text)])
    return merged


def merge_manifest(manifest_path: Path, doc_id: str,
                   entree: ManifestEntry | Callable[
                       [LectureDuLot], tuple[ManifestEntry, Sequence[tuple[Path, str | None]]]],
                   *, artefacts: Sequence[tuple[Path, str | None]] = (),
                   cibles: Sequence[Path] = ()) -> ManifestEntry:
    """Fusionne l'entrée `doc_id` ; les autres documents sont conservés tels quels, même invalides.

    Le `raw` que cette fonction prenait en paramètre a disparu **à dessein** : le garder aurait
    laissé croire qu'une lecture faite hors du verrou peut servir de base à une fusion. Elle ne le
    peut pas, et c'était le défaut. La lecture qui compte est refaite dans la transaction.
    """
    return fusionner_et_publier(manifest_path, doc_id, entree, artefacts=artefacts, cibles=cibles)
