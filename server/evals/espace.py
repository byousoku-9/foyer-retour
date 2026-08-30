"""L'espace de publication : un bundle immuable, un pointeur unique, une seule bascule.

Story 4.5, B7. Ce module remplace la file de `os.replace` que `run._basculer` exécutait cible par
cible, et avec elle la restauration inverse et l'état mêlé que `BasculePartielle` se contentait de
**nommer**. L'invariant tenu ici est celui de l'AC, littéralement :

    après toute exception, à n'importe quel rang, `BaseException` et interruption comprises,
    **zéro cible du lot** n'est modifiée ni visible dans le nouvel état.

## Pourquoi cette forme, et pourquoi elle est la seule

L'invariant interdit toute réparation après coup (« un protocole qui *défait* ce qu'il a déjà fait
n'est pas tout-ou-rien »). Or CPython peut lever `KeyboardInterrupt` à n'importe quelle frontière
d'instruction. Si aucune réparation n'est admise, alors l'état observable du lot doit **déjà** être
l'ancien à chaque frontière : il existe donc exactement **un** pas qui porte `ancien → nouveau`, et
ce pas doit être atomique vis-à-vis d'une exception Python. C'est un seul appel système.

Un seul appel système ne change qu'une entrée de répertoire. Pour qu'une entrée change l'état résolu
de *toutes* les cibles à la fois, il faut qu'elle soit un **composant traversé par la résolution de
chacune**. D'où la disposition : chaque cible du lot est un chemin dont la résolution passe par
`data/.publie/courant`, et publier, c'est faire pointer `courant` sur l'autre génération — un unique
`os.replace` d'un lien symbolique, dans son propre répertoire, sur une entrée qui existe déjà.

C'est la forme que la spec nomme (« publier un bundle immuable puis basculer un unique pointeur
atomique ») ; le reste de ce module n'est que la mécanique obligatoire de cette forme.

## Ce que la bascule ne fait jamais

Elle ne **crée**, ne **migre** et ne **change le type** d'aucune cible. La disposition est statique :
elle est committée dans le dépôt et posée hors de toute transaction par `installer()`, qui n'est
appelée par aucun chemin de bascule. `basculer()` commence par vérifier que chaque cible du lot est
déjà résolue dans l'espace, et **refuse sans rien toucher** sinon. C'est la distinction que
l'interdiction 7 de l'AC trace : ce qu'elle proscrit est la pose séquentielle **à l'exécution**, où
une exception en cours de pose laisse des cibles modifiées ; ici `lstat` rend le même type avant et
après une bascule, et Git aussi.

## Ce qu'elle garantit, et ce qu'elle ne garantit pas

Garanti : toute exception — `OSError`, `RuntimeError`, `KeyboardInterrupt`, `BaseException` — levée
à n'importe quel rang de la préparation laisse les cibles exactement dans leur état d'avant, et ne
laisse aucun temporaire. Il n'y a rien à défaire, donc rien qui puisse échouer en défaisant.

Garanti aussi, et c'est la **frontière post-commit** (tour correctif 3/3) : aucune exception n'est
propagée une fois le pointeur effectivement remplacé. Le remplacement est le point de commit ;
au-delà, l'opération a réussi, et remonter une exception rendrait « le lot est publié » et « ça a
échoué » vrais en même temps — exactement l'état mêlé que l'AC interdit, une frontière plus loin.
Deux conséquences, toutes deux mécaniques :

- **l'état réel du pointeur sur disque est la seule autorité** du chemin de nettoyage. Un drapeau
  Python posé après le `os.replace` peut être coupé par une interruption à cette frontière
  d'instruction précise ; le gestionnaire relit donc `courant` au lieu de se croire ;
- **rien de ce qui suit le commit ne peut lever** : le `fsync` du répertoire de l'espace, le
  retrait du lien temporaire **et la sortie de la section critique** — `flock(LOCK_UN)`, `close`,
  et tout ce que l'appelant exécute encore dans le `with` — sont exécutés en absorbant toute
  exception, `BaseException` comprise. Une interruption arrivée là est **absorbée**, parce que la
  transaction est acquise et que l'annuler est impossible ; c'est dit ici plutôt que tu.

La frontière post-commit ne s'arrête donc pas au `return` de `publier` : elle court jusqu'à la
sortie de la section critique (tour de racine unique, fait 3). Le verrou est pris et rendu **à la
main**, sans `with`, précisément pour que sa libération tombe du bon côté de cette frontière ; un
`with _verrou(...)` interne rendait le `__exit__` inaccessible au gestionnaire et laissait
`OSError` ou `KeyboardInterrupt` remonter avec le lot déjà publié. Symétriquement, une exception
de verrou **avant** le commit continue de remonter et laisse zéro cible modifiée.

## La transaction : lire, fusionner, publier sous le même verrou

`transaction()` est la seule API d'écriture de l'espace, et elle tient **toute** la séquence sous
un unique `flock` : la lecture d'une cible du lot, la fusion que l'appelant en tire, et la
publication. C'est ce qui ferme la **mise à jour perdue** (tour de racine unique, fait 2) : un
écrivain qui lisait le manifest avant de prendre le verrou fusionnait un état périmé et écrasait
la publication d'un concurrent, quand bien même chaque commit était sérialisé. Sérialiser les
commits ne suffit jamais si la lecture est dehors.

Il n'existe **aucune voie d'écriture sans verrou** : pas de paramètre, pas de défaut permissif,
pas de constructeur privé. `basculer` n'est qu'une transaction d'un seul appel.

Non garanti, et écrit plutôt que tu : un `SIGKILL` ou une coupure matérielle **pendant**
l'unique `rename(2)` relève du système de fichiers, pas de l'espace utilisateur. `rename(2)` est
atomique pour un observateur, mais sa durabilité après coupure dépend du `fsync` du répertoire, que
ce module fait — sans pouvoir promettre plus que ce que le matériel tient ; un `fsync` qui échoue
après le commit ne défait pas la publication, il la rend seulement moins durable, et c'est ce que
l'absorption dit. Le **biais de lecteur** — un lecteur qui résout deux cibles de part et d'autre
d'une bascule — n'est plus dans cette liste : `server/app/corpus/racine.py` en est le pendant
lecture, et toute opération de lecture de production pince une génération unique (tour de la racine
vraiment unique, N1). Ce qui reste non couvert et se dit : un lecteur qui n'emploierait **pas** ce
repère — un outil externe, un script ad hoc — voit toujours un mélange. Non garanti enfin :
l'abandon du **brouillon**
(la génération inactive) est un `rmtree`, qu'une interruption peut couper en deux. Il ne touche
aucune cible — et jamais, en aucun cas, la génération que le pointeur publie — mais il peut laisser
une génération inactive à moitié effacée. C'est un résidu **visible** (`residus()`), pas un état
mêlé : la bascule suivante la reconstruit de zéro.

## Le brouillon est toujours détectable, **dans les deux sens**

Un brouillon incomplet ne doit **jamais** être indiscernable d'un bundle complet — ni pour
`residus()`, ni pour `_reconstruire`, ni pour un opérateur (tour de racine unique, fait 4). Une
**marque** en `.tmp` est donc posée dans l'espace avant la première écriture du brouillon ; tant
qu'elle est là, la génération qu'elle nomme est en cours de construction.

Tour de la racine vraiment unique (N2). Cette marque n'est plus « un signal, pas une garantie ».
Trois changements, et les trois sont la même exigence :

- **à la pose, elle ne peut pas échouer en silence.** Si la marque ne peut pas être créée, la
  préparation refuse **avant toute mutation de la génération inactive** : sans elle, une exception
  au rang N laisserait un `a`/`b` partiel que `residus()` ne verrait pas ;
- **elle est conservée jusqu'au commit établi**, et n'est retirée que dans la frontière
  post-commit. Elle couvre donc *toute* la fenêtre de mutation, et l'abandon prudent — quand le
  pointeur est indécidable et qu'on ne détruit rien — n'a plus rien à **reposer** : elle est déjà
  là. Une repose best-effort partageait exactement le mode de défaillance qu'on ferme ;
- **une marque périmée est assainie strictement avant le commit.** Une marque laissée par un
  processus disparu ne peut donc pas survivre à une bascule saine. En revanche, si son retrait
  post-commit échoue, `residus()` la compte même lorsqu'elle nomme la génération publiée : c'est la
  preuve durable du nettoyage incomplet, et non une exception qui masquerait un commit réussi.

Ce que le nommage par pid ne peut pas garantir seul s'écrit plutôt que tu : deux processus qui
préparent la même génération inactive sont impossibles (le `flock` les sérialise), mais une marque
dont le processus a disparu **entre** deux bascules reste jusqu'à la reconstruction suivante de sa
génération — c'est-à-dire jusqu'au prochain écrivain, pas jusqu'à un moissonneur qui n'existe pas.

`_reconstruire`, lui, n'a jamais à se fier à la marque : il efface et rebâtit la génération
inactive de zéro.

## Spine

AD-14 (la publication des questions-témoins) et AD-8 (« seul `gate.evals_ok` décide de ce qui est
servi ») sont les deux décisions servies. AD-8 est la raison pour laquelle `data/manifest.json`
appartient au **même** lot que les surfaces publiées : promouvoir et publier ne peuvent pas devenir
visibles séparément, sans quoi une surface affirmerait un verdict que le manifest ne porte pas, ou
l'inverse. La publication reste inconditionnelle (FR41) : un lot rouge bascule comme un lot vert.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE

# **La disposition n'a qu'une autorité** (tour de la racine vraiment unique, N1). Les constantes,
# les refus nommés et la moitié *lecture* de la racine vivent dans `server/app/corpus/racine.py`,
# parce que la table des couches du spine interdit à `corpus` comme à `api` d'importer
# `server/evals` (`tests/test_layers.py`) : un lecteur servi ne peut donc pas atteindre ce module.
# Les recopier ici aurait fait deux littéraux `.publie` qu'un caractère de différence désaccorderait.
# Ce module garde ce qui **écrit** : l'installation, la transaction, la bascule et la sonde.
from server.app.corpus.racine import (ARTEFACTS_DOCUMENT as ARTEFACTS_DE_DOCUMENT,
                                      GENERATIONS, POINTEUR, REPERTOIRE_ESPACE,  # noqa: F401
                                      SOURCES_DOCUMENT as SOURCES_PUBLIEES,
                                      SURFACES_DATA as SURFACES_DE_DATA, VERROU,
                                      EspaceIllisible, EspaceNonInstalle,
                                      LotHorsEspace, RacinePubliee, _repertoire_espace_ordinaire,
                                      lire_pointeur, racine_couvrant)

_lire_pointeur = lire_pointeur

# --- la disposition du dépôt : ce que la racine couvre ----------------------------------------------
#
# Tour de racine unique. Une racine n'a d'autorité que sur les cibles qu'elle couvre : un artefact
# publié hors du pointeur est écrit à son propre rang, donc l'opération qui l'écrit avec d'autres
# n'est pas tout-ou-rien. La règle est donc **tout ce qu'un écrivain de production publie**, et rien
# d'autre. Les sources et leur référence effectivement consommées (`source.js`, `source.pdf`,
# `source.url`, `source.sha256`) en sont : les sélectionner ou les lire hors du repère épinglé
# recomposerait un document depuis deux générations. `README.md`, qui n'est pas servi, reste hors
# du bundle.
#
# La liste est **nominale par artefact, jamais par document** : les répertoires de documents sont
# découverts en listant le `data/` **du run**, de sorte qu'aucun `doc_id` n'apparaisse ici.
#
# Les noms sont donnés dans le repère qui leur convient, et pas dans un préfixe codé en dur : ceux du
# `data/` sont relatifs à `data_dir`, ceux de `docs/` à la racine. Écrire `data/manifest.json` en dur
# faisait poser, sous `--data-dir <autre>`, un `<racine>/data/manifest.json` qui n'a rien à voir avec
# le run, et n'installait jamais `<autre>/manifest.json` — donc l'ingestion suivante refusait en lot
# mixte, en désignant une cible que personne n'écrit.

# Les surfaces écrites hors du `data/`, relatives à la racine : le rendu lisible et ses archives.
SURFACES_HORS_DATA = ("docs/evals/latest.md", "docs/evals/campagnes")
# Les artefacts qu'une ingestion publie dans le répertoire d'un document. `kb_to_blocks` et
# `pdf_to_blocks` écrivent les trois premiers **au même lot** que le manifest ; `type_clauses` écrit
# document et rapport et **retire** l'overlay dans ce même lot ; `structure.py` écrit la proposition
# de structure, dont l'empreinte entre au manifest ; `enrich_dictionary` écrit le dictionnaire d'un
# contrat. Une cible absente est un lien pendant, c'est-à-dire une absence (fermeture B6).
#
# `typing.manual.json` est le cas à part, et il est nommé plutôt que caché : c'est une **entrée**
# écrite à la main, dont seule la **suppression** appartient au pipeline (`type_clauses` la retire
# dans le lot qu'il publie). Elle est donc couverte parce que cette suppression doit être membre du
# lot — pas parce qu'un écrivain la publie. La conséquence pour un opérateur est réelle et se dit :
# poser un overlay se fait *par la racine*, et la procédure est écrite dans `docs/evals/harness.md`.
# Toutes les entrées servent à reconnaître un répertoire de document, sans nommer aucun `doc_id`.
# Le sous-ensemble `SOURCES_PUBLIEES` est couvert et pincé, référence de téléchargement comprise.
SOURCES_DE_DOCUMENT = ("source.js", "source.pdf", "source.url", "source.sha256")


def _valider_doc_id_depot(doc_id: str, *, origine: Path) -> None:
    if len(doc_id) > DOC_ID_MAX or DOC_ID_RE.fullmatch(doc_id) is None:
        raise EspaceIllisible(
            f"{origine} : identifiant de document impropre {doc_id!r} "
            f"(attendu : {DOC_ID_RE.pattern}, {DOC_ID_MAX} caractères maximum)")


def cibles_du_depot(racine: Path, data_dir: Path | None = None) -> list[Path]:
    """Toutes les cibles que la racine doit couvrir, relatives à `racine`.

    L'énumération réunit les identifiants valides du manifest et la découverte structurelle des
    répertoires qui portent déjà une source ou un artefact d'ingestion. Elle couvre donc aussi bien
    un document déclaré dont les slots sont encore absents qu'un document neuf pas encore déclaré,
    tout en excluant l'espace lui-même et les caches. Poser un document neuf, c'est reposer la
    disposition — un geste d'opérateur, idempotent, jamais atteint depuis une bascule.

    Les deux familles de surfaces vivent dans leur propre repère : celles du `data/` sont relatives à
    `data_dir`, celles de `docs/` à la racine. Un `data_dir` qui n'est pas sous la racine n'a pas de
    disposition possible — aucun pointeur ne peut couvrir les deux —, et c'est dit (`LotHorsEspace`)
    plutôt que résolu par un préfixe deviné.
    """
    racine = Path(racine)
    data = Path(data_dir) if data_dir is not None else racine / "data"
    try:
        relatif_data = Path(os.path.relpath(os.path.abspath(data), os.path.abspath(racine)))
        if relatif_data.is_absolute() or relatif_data.parts[:1] == ("..",):
            raise ValueError(relatif_data)
    except ValueError as exc:
        raise LotHorsEspace(
            f"{data} : le répertoire de données est hors de la racine {racine} — aucun pointeur ne "
            "peut couvrir à la fois ses surfaces et celles de `docs/`") from exc
    cibles = [relatif_data / nom for nom in SURFACES_DE_DATA]
    cibles += [Path(relatif) for relatif in SURFACES_HORS_DATA]
    documents: set[str] = set()
    manifest = data / "manifest.json"
    try:
        brut_manifest = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(brut_manifest, dict):
            for doc_id in brut_manifest:
                if not isinstance(doc_id, str):
                    raise EspaceIllisible(
                        f"{manifest} : identifiant de document non textuel {doc_id!r}")
                _valider_doc_id_depot(doc_id, origine=manifest)
                documents.add(doc_id)
    except (OSError, UnicodeDecodeError, ValueError):
        # Une pose de réparation doit encore pouvoir couvrir le manifest illisible ; la découverte
        # structurelle ci-dessous conserve alors les documents déjà présents sur disque.
        pass
    if data.is_dir():
        for entree in sorted(data.iterdir()):
            if not entree.is_dir() or entree.name.startswith("."):
                continue
            noms = {chemin.name for chemin in entree.iterdir()}
            if not (noms & set(ARTEFACTS_DE_DOCUMENT)) and not (noms & set(SOURCES_DE_DOCUMENT)):
                continue
            _valider_doc_id_depot(entree.name, origine=entree)
            documents.add(entree.name)
    for doc_id in sorted(documents):
        cibles += [relatif_data / doc_id / nom
                    for nom in (*ARTEFACTS_DE_DOCUMENT, *SOURCES_PUBLIEES)]
    return cibles


class EspacePublie(RacinePubliee):
    """L'espace de publication d'une racine : son bundle, son pointeur, sa bascule.

    `racine` est la racine dont les cibles sont relatives — la racine du dépôt en production,
    `tmp_path` dans les tests. `data_dir` en dérive comme le reste du runner (`run.main` construit
    déjà `output_json`, `output_markdown` et le cache depuis `args.data_dir`), pour qu'un run pointé
    ailleurs n'écrive jamais dans le `data/` du dépôt.

    La moitié **lecture** — la disposition, le slot d'une cible, la génération courante, le repère
    pincé — vient de `RacinePubliee` (`server/app/corpus/racine.py`), que les lecteurs servis
    importent sans jamais atteindre cette classe-ci. Ce qui est ajouté ici **écrit**.
    """

    def verifier_lot(self, cibles: Iterable[Path]) -> None:
        """Refuse **avant toute écriture** si une cible n'est pas couverte par le pointeur."""
        for cible in cibles:
            if not self.resolue_dans_lespace(cible):
                raise EspaceNonInstalle(
                    f"{cible} : cette cible n'est pas résolue par {self.pointeur} — la bascule "
                    "n'installe jamais de lien (elle ne modifierait plus le lot d'un seul geste). "
                    f"Poser la disposition : `python -m server.evals.espace --racine {self.racine} "
                    f"--data-dir {self.data_dir}`")

    # --- installation (hors de toute transaction) ---------------------------------------------------

    def installer(self, cibles: Sequence[Path], *, migrer: bool = False) -> None:
        """Pose la disposition statique. **Aucun chemin de bascule n'appelle cette fonction.**

        Idempotente. Une cible absente devient un lien pendant — ce qui est la même chose qu'absente
        pour tout lecteur. Une cible qui existe déjà en fichier ordinaire n'est **jamais** migrée en
        silence : sans `migrer=True`, l'installation refuse et le dit. `migrer=True` est un geste
        d'opérateur, exécuté une fois et committé ; il n'est jamais atteint depuis un run.
        """
        try:
            _repertoire_espace_ordinaire(self.chemin)
        except EspaceNonInstalle:
            # Une absence réelle est le seul état que l'installation puisse créer. Une entrée
            # existante d'un autre type — en particulier un lien — est refusée avant mutation.
            try:
                os.lstat(self.chemin)
            except FileNotFoundError:
                self.chemin.mkdir(parents=True, exist_ok=False)
            else:
                raise
        _repertoire_espace_ordinaire(self.chemin)
        for generation in GENERATIONS:
            (self.chemin / generation).mkdir(exist_ok=True)
        if not self.installe():
            lien = self.chemin / f".{POINTEUR}.installation.tmp"
            lien.unlink(missing_ok=True)
            os.symlink(GENERATIONS[0], lien)
            os.replace(lien, self.pointeur)
        generation = self.generation()
        for cible in cibles:
            self._installer_une(Path(cible), generation, migrer=migrer)

    def _installer_une(self, brute: Path, generation: str, *, migrer: bool) -> None:
        if self.resolue_dans_lespace(brute):
            return
        cible = self.absolu(brute)
        interne = self.chemin_dans(brute, generation)
        if cible.is_symlink() or cible.exists():
            if not migrer:
                # **Le refus vient avant toute création** (revue N1–N3, constat 17) : poser le
                # répertoire du slot d'abord laissait, sur une installation refusée, des
                # répertoires vides dans le bundle — une trace d'un geste qui n'a pas eu lieu.
                raise EspaceNonInstalle(
                    f"{cible} existe déjà hors de l'espace : l'installation ne migre pas une cible "
                    "sans qu'on le lui demande (`--migrer`), parce qu'une migration silencieuse "
                    "est précisément ce qu'une bascule n'a pas le droit de faire")
            interne.parent.mkdir(parents=True, exist_ok=True)
            if cible.is_dir() and not cible.is_symlink():
                shutil.rmtree(interne, ignore_errors=True)
                shutil.move(str(cible), str(interne))
            else:
                shutil.move(str(cible), str(interne))
        # Le **répertoire** du slot est posé une fois le refus passé : la disposition est alors
        # complète, et un lien pendant reste une absence sans être un cul-de-sac. Sans lui, écrire à
        # travers un lien jamais publié échouerait sur un parent absent, alors que la publication,
        # elle, crée ce répertoire (`_ecrire_dans_bundle`).
        interne.parent.mkdir(parents=True, exist_ok=True)
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.unlink(missing_ok=True)
        # **Le lien passe par le pointeur, jamais par une génération.** Pointer directement sur `a`
        # ferait une cible que la bascule ne toucherait plus jamais : l'atome ne déplace que
        # `courant`, donc tout ce qui ne le traverse pas est hors de la transaction.
        par_le_pointeur = self.chemin / POINTEUR / self.slot(brute)
        os.symlink(os.path.relpath(par_le_pointeur, cible.parent), cible)

    # --- la transaction et la bascule ---------------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """Ouvre la section critique de l'espace : lire, fusionner, publier sous **un seul** `flock`.

        C'est l'unique API d'écriture. Elle existe parce que sérialiser les commits ne suffit pas :
        un écrivain qui lit le manifest **avant** le verrou fusionne un état périmé et écrase la
        publication d'un concurrent — la mise à jour perdue que le tour de racine unique ferme
        (fait 2). La lecture (`Transaction.lire`), la fusion faite par l'appelant et la publication
        (`Transaction.publier`) vivent donc toutes trois dans le même `with`.

        Le verrou est pris et rendu **à la main**, sans `with` interne, pour que sa libération
        tombe du bon côté de la frontière post-commit (fait 3) : une fois le pointeur remplacé,
        `flock(LOCK_UN)` et `close` ne peuvent plus faire remonter quoi que ce soit. Avant le
        commit, au contraire, une exception de verrou remonte et laisse zéro cible modifiée.

        L'autorité du chemin de sortie est toujours le **pointeur sur disque**, jamais un drapeau
        Python qu'une interruption pourrait couper à la frontière d'instruction qui suit
        `os.replace`.

        La génération courante elle-même est lue **sous le verrou**, et c'est la même règle que
        celle qui vaut pour le manifest : une lecture faite avant le verrou est périmée dès qu'un
        concurrent bascule. Ici la mise à jour perdue serait pire qu'une entrée écrasée — un
        écrivain parti d'une génération périmée prendrait pour « inactive » la génération que le
        pointeur publie, la reconstruirait de zéro et la republierait, c'est-à-dire muterait la
        génération active tout en effaçant la publication du concurrent.
        """
        # Avant toute chose et **hors du verrou** : un espace non installé refuse sans rien créer.
        # `_verrou` crée son répertoire parent ; le laisser faire poserait un `.publie/` là où il
        # n'y en a pas, sur le seul fait qu'un appelant s'est trompé de racine. Ce n'est qu'un
        # préflight : la lecture dont la transaction part est refaite ci-dessous, sous le verrou.
        _lire_pointeur(self.chemin)
        verrou = _verrou(self.chemin)
        verrou.__enter__()
        transaction: Transaction | None = None
        try:
            try:
                transaction = Transaction(self, _lire_pointeur(self.chemin))
                yield transaction
            except BaseException:
                if transaction is not None and self._generation_publiee() == transaction.suivante:
                    # Le commit est acquis : le lot entier est publié, l'opération a réussi.
                    # Propager rendrait « publié » et « échoué » vrais en même temps.
                    self._apres_le_commit(transaction, verrou)
                    return
                try:
                    if transaction is not None:
                        self._abandonner(transaction)
                finally:
                    # La cause d'origine prime : une `OSError` de déverrouillage ne doit pas la
                    # masquer. C'est le seul cas où une exception de verrou d'avant-commit ne
                    # remonte pas — elle remonterait *à la place* de ce qui a réellement fait
                    # échouer la transaction.
                    with contextlib.suppress(BaseException):
                        verrou.__exit__()
                raise
            if self._generation_publiee() == transaction.suivante:
                self._apres_le_commit(transaction, verrou)
                return
            # Rien n'a été publié : le brouillon est jeté, et une exception de verrou remonte telle
            # quelle — il n'y a pas de lot basculé qu'elle pourrait contredire.
            self._abandonner(transaction)
            verrou.__exit__()
        finally:
            # **La section critique est rendue quoi qu'il arrive.** Un `KeyboardInterrupt` levé
            # pendant l'abandon du brouillon est délibérément laissé remonter (l'appelant doit voir
            # l'interruption) ; sans ce filet, il sortirait en laissant le `flock` tenu, et tout
            # écrivain ultérieur du processus — y compris la sonde qui vérifie l'état — bloquerait
            # pour toujours. Fermer le descripteur suffit à rendre le verrou.
            verrou.fermer()

    def basculer(self, lot: Sequence[tuple[Path, str | bytes | None]]) -> None:
        """Publie **tout** le lot, ou rien du tout, par un unique `os.replace`.

        Une transaction d'un seul appel — la forme courte de `transaction()`, pour un écrivain qui
        n'a rien à relire avant de publier. `lot` est une suite de `(cible, contenu)` remise **au
        même appel** : le lot est l'ensemble complet des cibles qu'implique l'opération,
        `data/manifest.json` compris. Aucune cible n'en est retirée, et il n'existe pas de variante
        « une seule cible » qui supprimerait le rang où échouer.

        Un `contenu` à `None` **supprime** la cible : son slot est absent de la génération publiée,
        donc son lien devient pendant, donc elle est absente pour tout lecteur (fermeture B6). Une
        suppression est ainsi membre du lot au même titre qu'une écriture — c'est ce qui permet à
        l'opération de typage de retirer son overlay dans le même geste qu'elle publie le reste.
        """
        with self.transaction() as transaction:
            transaction.publier(lot)

    # --- la frontière post-commit ------------------------------------------------------------------

    def _generation_publiee(self) -> str | None:
        """La génération que `courant` désigne **sur disque**, ou `None` si on ne peut pas conclure.

        Volontairement muette : elle est appelée depuis un gestionnaire d'exception, où lever
        masquerait la cause d'origine. `None` signifie « je ne sais pas », et tout appelant en tire
        la conclusion prudente : ne rien détruire.
        """
        try:
            cible = os.readlink(self.pointeur)
        except OSError:
            return None
        return cible if cible in GENERATIONS else None

    def _apres_le_commit(self, transaction: Transaction, verrou: _verrou) -> None:
        """Tout ce qui suit le point de commit, et qui ne peut donc **jamais** lever.

        Le lot est publié : une exception propagée ici serait une exception avec le lot déjà
        basculé. Le `fsync` du répertoire rend l'entrée durable — son échec (`EIO`) coûte de la
        durabilité, jamais l'atomicité —, le retrait du lien temporaire n'a plus d'objet (le
        `rename` l'a consommé), et la **sortie de la section critique** — `flock(LOCK_UN)` puis
        `close` — n'a plus rien à protéger. Les trois sont donc absorbés, `BaseException` comprise :
        une interruption arrivée après le commit ne peut plus annuler quoi que ce soit.

        Le déverrouillage est ici, et pas dans un `with`, parce que c'est précisément là qu'il
        appartient : `_verrou.__exit__` pouvait faire remonter `OSError` ou `KeyboardInterrupt`
        avec le lot déjà publié (tour de racine unique, fait 3). Fermer le descripteur suffit de
        toute façon à rendre le `flock` ; ce qui reste au-delà est du nettoyage, pas de la
        correction.

        **Absorber n'est pas taire** (patch croisé 1/3, `N2-NETTOYAGE-MUET`). Le retrait de la marque
        était absorbé *et* invisible. La marque qui survit est désormais elle-même conservée dans
        `residus()`, y compris si elle nomme la génération publiée ; une trace auxiliaire et un mot
        sur `stderr` complètent ce signal durable sans jamais faire remonter une exception après le
        commit.
        """
        with contextlib.suppress(BaseException):
            transaction.lien_tmp.unlink()
        try:
            transaction.marque.unlink()
        except BaseException as exc:  # noqa: BLE001 — après commit, rien ne remonte : on le **dit**
            self._dire_le_nettoyage_impossible(transaction, exc)
        with contextlib.suppress(BaseException):
            _fsync_repertoire(self.chemin)
        with contextlib.suppress(BaseException):
            verrou.__exit__()

    def _dire_le_nettoyage_impossible(self, transaction: Transaction,
                                      cause: BaseException) -> None:
        """Rendre durablement visible une marque qu'on n'a pas pu retirer après le commit.

        Deux canaux, parce qu'aucun des deux n'est garanti seul : une trace sur disque que
        `residus()` voit si elle peut être écrite, et un mot sur `stderr` pour l'opérateur qui
        regarde. Si ces deux canaux échouent aussi, la marque de brouillon subsistante reste le
        signal durable : on ne lève pas, jamais, le lot étant déjà basculé.
        """
        trace = self.chemin / f".{transaction.suivante}.nettoyage-impossible.{os.getpid()}.tmp"
        with contextlib.suppress(BaseException):
            trace.write_text(
                f"{transaction.marque.name} n'a pas pu être retirée après le commit : "
                f"{type(cause).__name__}: {cause}\n", encoding="utf-8")
        with contextlib.suppress(BaseException):
            print(f"nettoyage impossible après le commit : {transaction.marque} subsiste "
                  f"({type(cause).__name__}) — le lot est publié, rien n'est annulé ; "
                  f"trace : {trace.name}", file=sys.stderr)

    def _abandonner(self, transaction: Transaction) -> None:
        """Jette le brouillon — et **jamais** ce que le pointeur publie.

        Le lien temporaire part d'abord : c'est un `unlink` unique, qui ne peut rien laisser à
        moitié. Le brouillon part ensuite, mais **seulement** si le pointeur relu sur disque ne le
        désigne pas : dans le doute (pointeur illisible), on ne détruit rien, parce qu'un brouillon
        laissé n'est pas une cible alors qu'une génération publiée détruite rend toutes les cibles
        pendantes.

        Il part en **deux temps**, et l'ordre importe : un `rename` unique le sort d'abord de son
        emplacement de génération — c'est atomique, donc le slot est vide ou plein, jamais à moitié
        —, puis son contenu est effacé sous un nom en `.tmp`. Un `rmtree` fait directement sur la
        génération pouvait être coupé en deux par une interruption et laisser une génération
        inactive à moitié effacée, **indiscernable** d'un bundle précédent complet : c'est le
        résidu que le tour correctif 3/3 exige de rendre visible. Sous un nom en `.tmp`, il l'est
        par la même sonde que tous les autres temporaires.

        **Quand le pointeur est indécidable, la marque reste** (tour de racine unique, fait 4). Le
        renoncement prudent est le bon comportement — on ne détruit rien qu'on ne sait pas
        inactif —, mais il laissait la génération sous son nom `a`/`b`, potentiellement à moitié
        écrite, et `residus()` ne voyait que les noms en `.tmp` : un brouillon devenait
        indiscernable d'un bundle complet.

        Tour de la racine vraiment unique (N2) : il n'y a plus rien à **reposer** ici. La marque
        est posée avant la première mutation et **conservée jusqu'au commit établi**, donc elle est
        déjà là quand on arrive dans cette fonction, quelle que soit la cause. La repose
        best-effort d'avant appelait la même fonction que la pose et partageait donc exactement son
        mode de défaillance silencieux : une panne de repose rendait de nouveau une génération
        partielle invisible. Ce chemin ne fait plus que décider s'il peut la **retirer**.

        `ignore_errors` couvre les échecs d'`OSError`, pas l'interruption : un `KeyboardInterrupt`
        pendant l'effacement remonte, et c'est voulu — l'appelant doit voir l'interruption.
        """
        with contextlib.suppress(BaseException):
            transaction.lien_tmp.unlink()
        if not transaction.prepare:
            # Rien n'a été écrit dans la génération inactive : elle porte encore le bundle
            # précédent complet, qui est la matière du prochain miroir. L'effacer serait une perte
            # nette pour un refus qui n'a rien touché. La marque, elle, n'a pas non plus été
            # posée — ou sa pose est précisément ce qui a échoué —, donc il n'y a rien à retirer.
            with contextlib.suppress(BaseException):
                transaction.marque.unlink()
            return
        publiee = self._generation_publiee()
        if publiee is None:
            # Pointeur indécidable : on ne détruit rien, et la marque **déjà posée** reste. C'est
            # elle que `residus()` voit, et c'est tout ce que le fait 4 demandait.
            return
        if publiee == transaction.suivante:
            return
        poubelle: Path | None = None
        sorti = False
        try:
            poubelle = Path(tempfile.mkdtemp(prefix=f".{transaction.suivante}.abandonne.",
                                             suffix=".tmp", dir=self.chemin))
            os.rename(self.chemin / transaction.suivante, poubelle / transaction.suivante)
            sorti = True
        except BaseException:  # nettoyage secondaire : la cause pré-commit doit rester l'autorité
            pass
        # **La marque ne tombe que si le brouillon est réellement sorti de son emplacement.** La
        # retirer inconditionnellement était un aveuglement de la même famille que celui du fait 4 :
        # quand `mkdtemp` ou le `rename` échouent, la génération inactive reste **mêlée** sous son
        # nom `a`/`b` — mi-ancien miroir, mi-nouveau lot — et sans marque plus rien ne la distingue
        # d'un bundle complet. Tant qu'elle est là, elle est vue par `residus()`.
        if sorti:
            try:
                transaction.marque.unlink()
            except BaseException as exc:  # noqa: BLE001 — ne masque jamais la cause pré-commit
                # **Avant commit, une impossibilité de nettoyage se dit** (patch croisé 2/3,
                # `N2-NETTOYAGE-MUET`). L'abandon a réussi — le brouillon est sorti, il est visible
                # sous son nom `.tmp` — donc rien n'est mêlé ; mais la marque qui reste désigne une
                # génération qui n'est plus en construction. Ne pas propager ici est délibéré : on
                # est dans un gestionnaire d'exception, et masquer la cause d'origine serait pire.
                # On le **dit**, et la marque reste un résidu que `residus()` voit.
                # Le canal de signalement fait partie de la panne possible : un stderr fermé ne
                # doit jamais remplacer la cause pré-commit que `transaction()` relancera. La
                # marque et le brouillon sorti restent les traces durables que `residus()` voit.
                with contextlib.suppress(BaseException):
                    print(f"nettoyage impossible pendant l'abandon : "
                          f"{transaction.marque} subsiste ({type(exc).__name__}) — rien n'est "
                          "publié, le brouillon est abandonné", file=sys.stderr)
        if poubelle is not None:
            # `ignore_errors` absorbe les erreurs de nettoyage ordinaires. Une interruption reste
            # toutefois observable sur ce chemin **pré-commit** : la contre-sonde historique
            # l'exige, et le brouillon est déjà sous un nom `.tmp` durablement visible.
            shutil.rmtree(poubelle, ignore_errors=True)

    # --- la sonde de résidus ------------------------------------------------------------------------

    def residus(self) -> list[str]:
        """Tout ce que l'espace laisse traîner, **répertoires compris**.

        Tour correctif 3/3. Les sondes des tours précédents ne comptaient que les *fichiers* dont le
        nom finit par `.tmp` : un brouillon à moitié effacé par un nettoyage interrompu n'était vu
        par aucune d'elles. Depuis qu'`_abandonner` sort le brouillon de son emplacement de
        génération avant de l'effacer, ce reste porte un nom en `.tmp` — et cette sonde le voit,
        parce qu'elle ne filtre plus sur le type d'entrée.

        Tour de racine unique, fait 4. Le suffixe `.tmp` ne suffisait pas comme unique signal :
        quand le pointeur est **indécidable** au moment de l'abandon, `_abandonner` renonce à
        détruire — à raison — et laisse la génération inactive sous son nom `a`/`b`, peut-être à
        moitié écrite, que rien ne distinguait d'un bundle complet. La **marque** de brouillon
        posée avant la première écriture, et conservée par l'abandon prudent, est ce que cette sonde
        voit alors : une génération inactive partielle n'est plus jamais silencieuse.

        N2 exige aussi que l'échec du retrait post-commit reste durablement observable. La sonde ne
        filtre donc aucune marque selon la génération que le pointeur publie. Le faux positif
        historique est fermé à sa cause par l'assainissement pré-commit strict : une marque périmée
        ne survit plus à une bascule saine ; une marque qui subsiste est bien un résidu.

        Ce qu'elle ne compte **pas** non plus, et c'est délibéré : la génération inactive complète
        laissée par une bascule réussie. C'est le bundle précédent, suivi par git, matière du
        prochain miroir — pas un résidu.
        """
        if not self.chemin.is_dir():
            return []
        # **La marque d'une génération publiée n'est plus filtrée** (patch croisé 2/3,
        # `N2-NETTOYAGE-MUET`). Ce filtre existait pour un faux positif réel : une marque laissée
        # par un processus disparu survivait à la reconstruction de sa génération et désignait à
        # jamais la publication en cours comme un brouillon. Mais depuis que l'assainissement
        # **pré-commit** est strict — il refuse plutôt que d'absorber —, aucune marque périmée ne
        # peut plus survivre à une bascule saine : le faux positif est fermé à sa cause. Le filtre
        # ne protégeait donc plus rien, et il ôtait la seule observabilité durable qui restait
        # quand le retrait post-commit échoue. Une marque qui subsiste est désormais **toujours**
        # un résidu.
        marque_du_publie = None
        restes: set[str] = set()
        for chemin in self.chemin.rglob("*"):
            relatif = chemin.relative_to(self.chemin)
            if not any(part.endswith(".tmp") for part in relatif.parts):
                continue
            if (marque_du_publie is not None and len(relatif.parts) == 1
                    and relatif.name.startswith(marque_du_publie)):
                continue
            restes.add(str(relatif))
        return sorted(restes)


class Transaction:
    """Une section critique ouverte sur l'espace : ce qu'on peut y lire, et l'unique publication.

    Jamais construite par un appelant : `EspacePublie.transaction()` en est la seule origine, et
    elle ne la rend qu'à l'intérieur du `flock`. Il n'y a donc pas d'objet de transaction qui
    survive au verrou, ni de voie qui publierait sans lui.
    """

    def __init__(self, espace: EspacePublie, courante: str) -> None:
        self.espace = espace
        self.courante = courante
        self.suivante = GENERATIONS[1] if courante == GENERATIONS[0] else GENERATIONS[0]
        self.lien_tmp = espace.chemin / f".{POINTEUR}.{os.getpid()}.tmp"
        # La marque du brouillon : posée avant la première écriture, retirée quand il est complet.
        self.marque = espace.chemin / f".{self.suivante}.brouillon.{os.getpid()}.tmp"
        # Non pas une autorité — le pointeur sur disque l'est —, mais la seule chose qui distingue
        # « le brouillon n'a jamais été touché » de « il a commencé à l'être ». Posé **avant** la
        # première écriture, donc jamais faux dans le sens dangereux : au pire il fait jeter une
        # génération inactive qui était intacte, ce qui ne coûte qu'un miroir à refaire.
        self.prepare = False
        # Une transaction publie **une fois**. `courante` et `suivante` sont figées à la
        # construction : après un commit acquis, le pointeur désigne `suivante`, et un second
        # `publier` reconstruirait cette même `suivante` — c'est-à-dire `rmtree` sur la génération
        # que le pointeur publie, toutes les cibles pendantes le temps du miroir, puis un retour à
        # l'état d'avant le premier commit, sans la moindre erreur. `publier` refuse déjà l'abus
        # moindre (deux cibles au même slot) ; il refuse celui-ci de la même façon, et avant de
        # toucher quoi que ce soit.
        self.publiee = False

    def chemin_publie(self, cible: Path) -> Path:
        """Le slot **publié** d'une cible du lot, dans le repère de la transaction.

        Un appelant qui doit lire autre chose que des octets — l'horodatage d'un rendu à archiver,
        par exemple — a besoin du chemin, pas du contenu. Le lui donner ici, sous le verrou, est ce
        qui empêche qu'il aille l'observer à travers le lien, où une bascule concurrente peut
        déplacer le sol entre sa lecture et son commit.
        """
        self.espace.verifier_lot([cible])
        return self.espace.chemin / self.courante / self.espace.slot(cible)

    def chemin_lu(self, cible: Path) -> Path:
        """Le chemin **effectivement lu sous le verrou** — le slot publié quand la racine le couvre.

        Story 4.5, N3. Ce que lit un écrivain pour *décider* du contenu qu'il publie (l'empreinte de
        l'overlay, celle de la structure, le document précédent) doit venir de la génération que la
        transaction a pincée, jamais du lien vivant. Une cible que la racine ne couvre pas n'a pas
        de génération : il n'y a rien à pincer et rien à mêler, et son chemin est rendu tel quel.
        C'est la même dissymétrie structurelle que celle de `publier_artefacts`, jamais un paramètre.

        À la différence de `chemin_publie`, cette fonction ne **refuse** pas une cible non couverte :
        elle sert la lecture d'un artefact qui peut légitimement ne pas appartenir au lot (un
        overlay jamais posé dans un arbre partiellement installé), et refuser reviendrait à exiger
        du lot ce que le lot ne contient pas.
        """
        try:
            if not self.espace.resolue_dans_lespace(cible):
                return Path(cible)
            return self.espace.chemin / self.courante / self.espace.slot(cible)
        except LotHorsEspace:
            return Path(cible)

    def lire(self, cible: Path) -> str | None:
        """Le contenu **publié** d'une cible du lot, lu sous le verrou. `None` si elle est absente.

        La lecture passe par le slot de la génération courante plutôt que par le lien de la cible :
        c'est le même octet, mais dit dans le repère de la transaction. C'est cette lecture-là —
        et non celle que l'appelant aurait faite avant d'entrer — qui rend la fusion sûre.
        """
        try:
            return self.chemin_publie(cible).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def marquer(self) -> None:
        """Pose la marque du brouillon. **Elle lève** : sans marque, il n'y a pas de préparation.

        Tour de la racine vraiment unique, N2. Cette fonction était intégralement enveloppée dans
        `contextlib.suppress(OSError)` et ne rendait rien : `publier` enchaînait sur
        `_reconstruire` sans jamais savoir si la marque existait. La séquence mesurée était donc
        réelle — `ENOSPC`, `EDQUOT` ou un `EACCES` ponctuel avalé, puis un `rmtree`/`mkdir` de la
        génération inactive, puis une exception au rang N : `data/.publie/b` restait **partielle
        sous son nom canonique**, sans marque, sans nom en `.tmp`, et `residus()` rendait `[]`.

        L'ordre est donc renversé : la marque d'abord, la première mutation ensuite. Un espace où
        l'on ne peut pas poser un fichier vide est un espace où l'on ne doit rien reconstruire.
        """
        os.close(os.open(self.marque, os.O_CREAT | os.O_WRONLY, 0o644))

    def _assainir_les_marques_perimees(self) -> None:
        """Retire les marques d'**autres** processus nommant la génération qu'on vient de rebâtir.

        Le défaut symétrique de N2, et il est le plus insidieux : la marque est nommée par pid et
        n'était retirée que par la transaction qui l'avait posée. Un processus tué en cours de
        brouillon en laissait donc une que **rien** ne moissonnait — sondé, après deux bascules
        parfaitement saines qui `rmtree`ent et republient la génération qu'elle nomme, `residus()`
        la rendait encore. La marque avait survécu à la génération qu'elle désigne, et signalait
        comme « brouillon en cours » un bundle complet.

        L'assainissement se fait **après** la reconstruction, jamais avant : purger d'abord aurait
        rendu invisible une génération réellement partielle si la reconstruction échouait ensuite —
        c'est-à-dire aurait rouvert le faux négatif pour fermer le faux positif. Après
        `_reconstruire`, la génération est un miroir neuf et complet : ce que d'anciennes marques
        disaient d'elle n'a plus d'objet, et la marque de *cette* transaction la couvre désormais.
        """
        prefixe = f".{self.suivante}.brouillon."
        # **Le parcours est matérialisé avant d'agir** (revue N1–N3, constat 16). `iterdir()` lit le
        # répertoire au fur et à mesure : une `OSError` en cours d'itération sortait du `suppress`
        # englobant et laissait les marques **suivantes** non moissonnées — donc un bundle publié
        # signalé comme brouillon, à jamais. Chaque suppression est isolée à son tour.
        # **Avant le commit, une impossibilité d'assainir est un refus, pas un silence** (patch
        # croisé 1/3, `N2-NETTOYAGE-MUET`). L'itération et chaque `unlink` étaient absorbés, puis la
        # transaction continuait : une marque périmée qu'on n'a pas pu moissonner survivait alors à
        # la reconstruction de sa génération et, une fois celle-ci publiée, `residus()` la filtrait —
        # ni dite, ni observable. Ici rien n'est encore basculé : lever laisse **zéro cible
        # modifiée**, ce qui est exactement ce que l'AC demande d'une exception d'avant commit.
        try:
            entrees = list(self.espace.chemin.iterdir())
        except OSError as exc:
            raise EspaceIllisible(
                f"{self.espace.chemin} : impossible d'énumérer l'espace pour moissonner les marques "
                f"périmées ({type(exc).__name__}) — refus avant toute mutation, plutôt qu'un "
                "brouillon qu'on ne saura plus distinguer d'un bundle complet") from exc
        for entree in entrees:
            if (entree.name.startswith(prefixe) and entree.name.endswith(".tmp")
                    and entree != self.marque):
                try:
                    entree.unlink()
                except FileNotFoundError:
                    continue  # une autre transaction l'a moissonnée : c'est le résultat voulu
                except OSError as exc:
                    raise EspaceIllisible(
                        f"{entree} : marque périmée impossible à retirer ({type(exc).__name__}) — "
                        "refus avant toute mutation ; la laisser survivre à la reconstruction de sa "
                        "génération la rendrait indiscernable d'un bundle publié") from exc

    def publier(self, lot: Sequence[tuple[Path, str | bytes | None]]) -> None:
        """Écrit la génération inactive puis **bascule le pointeur** — l'unique point de commit.

        Déroulé, et pourquoi chaque étape est là :

        1. **Vérifier** que chaque cible est résolue par le pointeur. Rien n'est touché ; un refus
           ici est un refus avant écriture.
        2. **Marquer** le brouillon, puis **construire** la génération inactive : elle est d'abord
           un miroir en liens **durs** de la génération courante — les surfaces que ce lot ne
           réécrit pas gardent leur contenu — puis les slots du lot y sont écrits ou retirés,
           `fsync` compris. Rien de tout cela n'est une cible : `courant` pointe toujours sur
           l'ancienne génération, donc l'état observable du lot est inchangé, à chaque frontière
           d'instruction. La marque tombe quand le brouillon est complet.
        3. **Basculer** : un `os.replace` d'un lien symbolique sur `courant`. C'est l'atome.

        Toute exception avant 3 laisse le brouillon à l'abandon et **aucune cible modifiée**. Toute
        exception pendant ou après 3 : le `rename` a eu lieu ou n'a pas eu lieu, et c'est le
        **pointeur sur disque** qui le dit — le gestionnaire de `transaction()` en tire la suite.

        **Une fois par transaction**, et le refus vient avant toute écriture : republier dans la
        même transaction reconstruirait la génération que le pointeur vient de publier et défairait
        le commit précédent en silence.
        """
        if self.publiee:
            raise LotHorsEspace(
                "cette transaction a déjà publié : une transaction a un seul point de commit, et "
                "republier reconstruirait la génération que le pointeur publie — ouvrir une "
                "nouvelle transaction, ou remettre tout le lot au même appel")
        self.publiee = True
        cibles = [cible for cible, _contenu in lot]
        self.espace.verifier_lot(cibles)
        # Deux cibles qui partagent un slot rendraient la bascule ambiguë — laquelle gagne ? Le
        # refus est dit plutôt que résolu par l'ordre d'itération.
        slots = [str(self.espace.slot(cible)) for cible in cibles]
        if len(set(slots)) != len(slots):
            raise LotHorsEspace(
                f"deux cibles du lot partagent un slot : {sorted(slots)} — un lot ne publie pas "
                "deux contenus au même chemin")
        racine_suivante = self.espace.chemin / self.suivante
        # **La marque d'abord, la première mutation ensuite** (N2). `prepare` n'est posé qu'après :
        # une marque qui ne peut pas être créée refuse la préparation *avant* que la génération
        # inactive ait bougé, donc il n'y a rien à abandonner et rien à rendre visible.
        self.marquer()
        self.prepare = True
        _reconstruire(self.espace.chemin / self.courante, racine_suivante)
        # La génération est un miroir neuf : ce que d'anciennes marques disaient d'elle n'a plus
        # d'objet, et laisser vivre une marque d'un processus disparu ferait signaler comme
        # brouillon, à jamais, un bundle complet.
        self._assainir_les_marques_perimees()
        for cible, contenu in lot:
            chemin = racine_suivante / self.espace.slot(cible)
            if contenu is None:
                _retirer_du_bundle(chemin)
            else:
                _ecrire_dans_bundle(chemin, contenu)
        _fsync_repertoire(racine_suivante)
        # **La marque reste jusqu'au commit établi** (N2). Elle tombait ici, entre le `fsync` du
        # brouillon et l'atome : une exception dans cette fenêtre — préparation du lien temporaire,
        # `symlink`, interruption — laissait la génération inactive écrite, complète pour un lot
        # mais périmée pour le pointeur, et **sans marque**. La frontière post-commit
        # (`_apres_le_commit`) est le seul endroit d'où la retirer, parce que c'est le seul endroit
        # où la génération qu'elle nomme est devenue celle que le pointeur publie.
        self.lien_tmp.unlink(missing_ok=True)
        os.symlink(self.suivante, self.lien_tmp)
        # --- L'ATOME. Un seul `rename(2)`, dans un seul répertoire, sur une entrée qui existe
        # déjà. Avant lui, les cibles se résolvent toutes dans `courante` ; après, toutes dans
        # `suivante`. Il n'y a pas de frontière entre les deux.
        os.replace(self.lien_tmp, self.espace.pointeur)


class _verrou:  # noqa: N801 — gestionnaire de contexte, employé comme une fonction
    """Un `flock` exclusif sur l'espace, en gestionnaire de contexte."""

    def __init__(self, espace: Path) -> None:
        self.chemin = espace / VERROU
        self.fd: int | None = None

    def __enter__(self) -> _verrou:
        """Prend le verrou, ou **ne laisse rien derrière** : un `flock` qui échoue rend son descripteur.

        La prise du verrou précède le `try/finally` de `transaction()` — c'est ce qui garantit
        qu'un espace non installé refuse sans rien créer —, donc c'est ici, et nulle part ailleurs,
        que le descripteur d'un verrou non acquis doit être refermé.
        """
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.chemin, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            with contextlib.suppress(BaseException):
                os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *_exc: object) -> None:
        """Rend le verrou, et **peut lever** : `flock(LOCK_UN)` comme `close` sont des appels système.

        C'est précisément pourquoi la sortie de la section critique appartient à la frontière
        post-commit (tour de racine unique, fait 3) : appelée après un pointeur effectivement
        remplacé, elle est absorbée par `_apres_le_commit` ; appelée avant, elle remonte.
        """
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def fermer(self) -> None:
        """Le filet : rend la section critique sans jamais lever, même si `__exit__` a échoué.

        Fermer le descripteur rend le `flock` — c'est la garantie du système, pas une politesse.
        Sans ce filet, une exception levée pendant l'abandon d'un brouillon sortirait en laissant
        le verrou tenu, et tout écrivain ultérieur du processus attendrait indéfiniment.
        """
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        with contextlib.suppress(BaseException):
            os.close(fd)


def _reconstruire(courante: Path, suivante: Path) -> None:
    """La génération inactive devient un miroir en liens durs de la génération courante.

    En liens **durs**, pas en copies : les archives de campagne s'accumulent et rien ne justifie de
    recopier leurs octets à chaque publication. Un lien dur porte le même contenu et le même type
    d'entrée qu'un fichier ordinaire ; l'écriture d'un slot dans la génération inactive passe par un
    temporaire puis un `rename`, donc elle **remplace** l'entrée au lieu d'écrire à travers le lien :
    la génération courante n'est jamais modifiée.
    """
    shutil.rmtree(suivante, ignore_errors=True)
    suivante.mkdir(parents=True)
    if not courante.is_dir():
        return
    for source in sorted(courante.rglob("*")):
        relatif = source.relative_to(courante)
        destination = suivante / relatif
        if source.is_dir() and not source.is_symlink():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, destination)


def _ecrire_dans_bundle(chemin: Path, contenu: str | bytes) -> None:
    """Écrit un slot de la génération inactive : temporaire, `fsync`, puis `rename` **dans le bundle**.

    Le `rename` remplace l'entrée plutôt que d'écrire à travers elle : c'est ce qui empêche
    l'écriture d'un slot de traverser le lien dur posé par `_reconstruire` et d'atteindre la
    génération encore publiée.
    """
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd, temporaire = tempfile.mkstemp(prefix=f".{chemin.name}.", suffix=".tmp", dir=chemin.parent)
    try:
        mode = "wb" if isinstance(contenu, bytes) else "w"
        kwargs = {} if isinstance(contenu, bytes) else {"encoding": "utf-8"}
        with os.fdopen(fd, mode, **kwargs) as flux:
            flux.write(contenu)
            flux.flush()
            os.fsync(flux.fileno())
        os.replace(temporaire, chemin)
    except BaseException:
        try:
            os.unlink(temporaire)
        except OSError:
            pass
        raise


def _retirer_du_bundle(chemin: Path) -> None:
    """Retire un slot de la génération inactive : la cible sera **absente** après la bascule.

    Une suppression est membre du lot comme une écriture. Sans elle, une opération qui retire un
    artefact — le typage et son overlay — devrait le faire par un `unlink` hors de l'atome, donc
    à un rang où une exception laisserait l'artefact retiré et le reste non publié.
    """
    try:
        chemin.unlink()
    except FileNotFoundError:
        pass
    except IsADirectoryError:
        shutil.rmtree(chemin)


def _fsync_repertoire(chemin: Path) -> None:
    """`fsync` d'un répertoire : ce qui rend durable une entrée créée ou renommée.

    Sans lui, l'atome serait atomique pour un observateur mais pas durable après coupure. Un échec
    n'est pas fatal — il n'a laissé aucune cible dans un état mêlé — mais il n'est pas tu non plus :
    seule l'impossibilité d'ouvrir le répertoire est ignorée, le reste remonte.
    """
    try:
        fd = os.open(chemin, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def espace_couvrant(cible: Path) -> EspacePublie | None:
    """L'espace dont le pointeur couvre `cible`, ou `None` si elle n'est couverte par aucun.

    Tour correctif 3/3, frontière « immutabilité et concurrence ». Un écrivain **hors** de ce module
    — l'ingestion et son `write_atomic` — écrivait `data/manifest.json` en résolvant son lien puis en
    renommant **dans la génération active**, hors du verrou. Le bundle n'était donc pas immuable : la
    génération que le pointeur publie changeait sous les pieds des lecteurs, et une ingestion
    concurrente courait avec la reconstruction et la bascule d'un run.

    Cette fonction est le pont : elle reconnaît, à partir du chemin **résolu** de la cible, l'espace
    qui la couvre, pour que tout écrivain d'une cible du lot passe par le même protocole (même
    verrou, même génération inactive, même unique `os.replace`). Elle ne construit rien et n'écrit
    rien ; une cible ordinaire — `document.json`, `structure.json`, un fichier hors bundle — rend
    `None`, et son écrivain garde son chemin d'avant.

    La reconnaissance est **structurelle**, jamais nominale : le chemin résolu d'une cible couverte
    est `<data_dir>/.publie/<génération>/<slot>`, donc le **slot** s'y lit directement, `data_dir`
    est le parent du répertoire d'espace, et la racine son parent — exactement la dérivation unique
    de `espace_du_data_dir`.

    **L'espace est rendu dans le repère de l'appelant, pas dans celui du chemin résolu** (défaut
    trouvé à la vérification indépendante du tour 3/3). Dériver la racine de `os.path.realpath` puis
    interroger `resolue_dans_lespace` avec le chemin **non résolu** mêlait deux repères : `slot()`
    compare par `os.path.abspath`, qui ne résout pas les liens, donc toute cible atteinte par un
    préfixe lié — `/tmp` en est un sur macOS — faisait lever `LotHorsEspace` là où la docstring
    annonçait un repli, et `basculer` aurait levé de la même façon.

    La correction dérive le slot du chemin résolu, puis **retire ce slot du chemin donné** pour
    obtenir la racine telle que l'appelant l'exprime. Les deux moitiés de la comparaison vivent alors
    dans le même repère, et `slot()` reste inchangée : son `os.path.abspath` sur la cible est
    délibéré (résoudre la cible elle-même rendrait un slot imbriqué dans le bundle, et l'archive de
    campagne, qui vit sous un répertoire lié, en serait la première victime).

    Attraper `LotHorsEspace` pour rendre `None` aurait été le faux correctif : une cible réellement
    **dans** l'espace, atteinte par un préfixe lié, serait retombée sur l'écriture à travers le
    lien — c'est-à-dire la réouverture, sous un autre chemin, du défaut que ce tour ferme.

    La reconnaissance elle-même vit dans `server/app/corpus/racine.py` (tour de la racine vraiment
    unique, N1) : c'est la question d'un **lecteur** autant que d'un écrivain, et la table des
    couches interdit à `corpus` d'importer les évals. Ici on ne fait qu'en tirer l'objet
    d'**écriture** — et revérifier que la cible est bien résolue dans la génération courante.
    """
    lue = racine_couvrant(Path(cible))
    if lue is None:
        return None
    espace = EspacePublie(lue.racine, lue.data_dir)
    return espace if espace.resolue_dans_lespace(Path(cible)) else None


# --- la pose de la disposition, en ligne de commande -----------------------------------------------
#
# Séparée du runner **à dessein** : elle est un geste d'opérateur, exécuté une fois et committé, et
# aucun chemin de bascule ne l'atteint. C'est ce qui rend vraie la phrase « la bascule ne crée, ne
# migre et ne change le type d'aucune cible ».

def _main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover — point d'entrée
    import argparse

    p = argparse.ArgumentParser(description="Pose l'espace de publication (bundle + pointeur).")
    p.add_argument("--racine", type=Path, default=Path.cwd())
    p.add_argument("--data-dir", type=Path, default=None)
    p.add_argument("--cible", type=Path, action="append", default=[],
                   help="chemin relatif à la racine ; répétable")
    p.add_argument("--depot", action="store_true",
                   help="pose toute la disposition du dépôt (surfaces de racine + artefacts de "
                        "chaque document de data/), en plus des --cible donnés")
    p.add_argument("--migrer", action="store_true",
                   help="déplace le contenu d'une cible déjà existante dans le bundle")
    args = p.parse_args(argv)
    try:
        espace = EspacePublie(args.racine, args.data_dir)
        cibles = list(args.cible)
        if args.depot:
            cibles += [c for c in cibles_du_depot(espace.racine, espace.data_dir)
                        if c not in cibles]
        espace.installer(cibles, migrer=args.migrer)
    except (EspaceNonInstalle, LotHorsEspace, EspaceIllisible, OSError) as exc:
        print(f"refus : {exc}")
        return 2
    print(f"espace posé : {espace.pointeur} -> {espace.generation()}")
    restes = espace.residus()
    if restes:
        # Le seul reste qu'un refus puisse laisser : un brouillon abandonné dont l'effacement a été
        # interrompu. Il ne touche aucune cible et la bascule suivante le remplace, mais il se dit —
        # une sonde qui ne peut pas le voir ne prouve rien sur ce chemin.
        print(f"résidus (brouillons abandonnés, sans effet sur les cibles) : {', '.join(restes)}")
    return 0


if __name__ == "__main__":  # pragma: no cover — point d'entrée
    raise SystemExit(_main())
