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
l'absorption dit. Non garanti non plus : l'absence de **biais de lecteur**, un lecteur qui résout
deux cibles de part et d'autre d'une bascule voit un mélange. L'AC ne le demande pas ; il est dit
ici pour ne pas laisser croire qu'il est couvert. Non garanti enfin : l'abandon du **brouillon**
(la génération inactive) est un `rmtree`, qu'une interruption peut couper en deux. Il ne touche
aucune cible — et jamais, en aucun cas, la génération que le pointeur publie — mais il peut laisser
une génération inactive à moitié effacée. C'est un résidu **visible** (`residus()`), pas un état
mêlé : la bascule suivante la reconstruit de zéro.

## Le brouillon est toujours détectable

Un brouillon incomplet ne doit **jamais** être indiscernable d'un bundle complet — ni pour
`residus()`, ni pour `_reconstruire`, ni pour un opérateur (tour de racine unique, fait 4). Une
**marque** en `.tmp` est donc posée dans l'espace avant la première écriture du brouillon et
retirée quand il est complet et `fsync`é ; tant qu'elle est là, la génération qu'elle nomme est en
cours de construction. Elle survit à l'abandon prudent : quand le pointeur est **indécidable**,
`_abandonner` ne détruit rien (une génération publiée effacée rendrait toutes les cibles
pendantes) mais repose la marque, de sorte que le brouillon laissé sous son nom `a`/`b` soit vu.
`_reconstruire`, lui, n'a jamais à s'y fier : il efface et rebâtit la génération inactive de zéro.

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
import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

# Le bundle vit sous `data/` parce que c'est ce que l'image copie (`Dockerfile` : `COPY server data
# web tools`). Un espace posé sous `docs/` serait absent de l'image, et la route servie rendrait
# `publie: false` en production — exactement là où FR41 la demande.
REPERTOIRE_ESPACE = ".publie"
POINTEUR = "courant"
VERROU = ".verrou"
# Deux générations qui alternent. Le contenu publié est petit (un manifest, un artefact servi, deux
# rendus Markdown, les archives de campagne) : les suivre toutes les deux dans git coûte quelques
# kilo-octets et évite une génération par run, qui ferait croître le dépôt sans borne.
GENERATIONS = ("a", "b")

# --- la disposition du dépôt : ce que la racine couvre ----------------------------------------------
#
# Tour de racine unique. Une racine n'a d'autorité que sur les cibles qu'elle couvre : un artefact
# publié hors du pointeur est écrit à son propre rang, donc l'opération qui l'écrit avec d'autres
# n'est pas tout-ou-rien. La règle est donc **tout ce qu'un écrivain de production publie**, et rien
# d'autre : les *entrées* (`source.js`, `source.pdf`, `source.url`, `source.sha256`, `README.md`)
# n'en sont pas — personne ne les écrit, et les mettre dans le bundle en ferait un dépôt de sources.
#
# La liste est **nominale par artefact, jamais par document** : les répertoires de documents sont
# découverts en listant `data/`, de sorte qu'aucun `doc_id` n'apparaisse ici.

# Les surfaces de racine, écrites par le runner d'évals et l'enrichissement du dictionnaire.
SURFACES_DE_RACINE = ("data/manifest.json", "data/dictionary.json", "data/evals-latest.json",
                      "docs/evals/latest.md", "docs/evals/campagnes")
# Les artefacts qu'une ingestion publie dans le répertoire d'un document. `kb_to_blocks` et
# `pdf_to_blocks` écrivent les trois premiers **au même lot** que le manifest ; `type_clauses` écrit
# document et rapport et **retire** l'overlay dans ce même lot ; `structure.py` écrit la proposition
# de structure, dont l'empreinte entre au manifest ; `enrich_dictionary` écrit le dictionnaire d'un
# contrat. Une cible absente est un lien pendant, c'est-à-dire une absence (fermeture B6).
ARTEFACTS_DE_DOCUMENT = ("document.json", "summary.md", "report.json", "structure.json",
                         "typing.manual.json", "dictionary.json")
# Les **entrées** d'un document : elles ne sont jamais publiées, et servent seulement à reconnaître
# un répertoire de document d'un cache ou de l'espace lui-même, sans nommer aucun `doc_id`.
SOURCES_DE_DOCUMENT = ("source.js", "source.pdf", "source.url", "source.sha256")


def cibles_du_depot(racine: Path, data_dir: Path | None = None) -> list[Path]:
    """Toutes les cibles que la racine doit couvrir, relatives à `racine`.

    L'énumération est **structurelle** : un répertoire de `data/` est celui d'un document s'il porte
    au moins une source ou un artefact d'ingestion — ce qui exclut d'office l'espace lui-même et les
    caches, sans avoir à les nommer. Poser un document neuf, c'est donc reposer la disposition — un
    geste d'opérateur, idempotent, jamais atteint depuis une bascule.
    """
    racine = Path(racine)
    data = Path(data_dir) if data_dir is not None else racine / "data"
    cibles = [Path(relatif) for relatif in SURFACES_DE_RACINE]
    relatif_data = data.relative_to(racine) if data.is_absolute() else Path(data)
    if data.is_dir():
        for entree in sorted(data.iterdir()):
            if not entree.is_dir() or entree.name.startswith("."):
                continue
            noms = {chemin.name for chemin in entree.iterdir()}
            if not (noms & set(ARTEFACTS_DE_DOCUMENT)) and not (noms & set(SOURCES_DE_DOCUMENT)):
                continue
            cibles += [relatif_data / entree.name / nom for nom in ARTEFACTS_DE_DOCUMENT]
    return cibles


class EspaceNonInstalle(Exception):
    """Une cible du lot n'est pas résolue dans l'espace : on refuse **avant** de toucher quoi que ce soit.

    Ce refus est la contrepartie de l'interdiction 7 : puisque la bascule ne pose jamais de lien,
    une disposition absente doit se dire au lieu de s'installer en douce. Le message porte la
    commande exacte qui l'installe.
    """


class LotHorsEspace(Exception):
    """Une cible du lot n'est pas sous la racine de l'espace — il n'y a pas de pointeur commun."""


class EspaceIllisible(Exception):
    """L'espace existe mais n'a pas pu être lu : un garde-fou qui ne peut pas conclure refuse."""


def _lire_pointeur(espace: Path) -> str:
    try:
        cible = os.readlink(espace / POINTEUR)
    except FileNotFoundError as exc:
        raise EspaceNonInstalle(
            f"{espace / POINTEUR} : l'espace de publication n'est pas installé") from exc
    except OSError as exc:
        raise EspaceIllisible(
            f"{espace / POINTEUR} : pointeur illisible ({type(exc).__name__})") from exc
    if cible not in GENERATIONS:
        raise EspaceIllisible(
            f"{espace / POINTEUR} : génération {cible!r} hors de {GENERATIONS}")
    return cible


class EspacePublie:
    """L'espace de publication d'une racine : son bundle, son pointeur, sa bascule.

    `racine` est la racine dont les cibles sont relatives — la racine du dépôt en production,
    `tmp_path` dans les tests. `data_dir` en dérive comme le reste du runner (`run.main` construit
    déjà `output_json`, `output_markdown` et le cache depuis `args.data_dir`), pour qu'un run pointé
    ailleurs n'écrive jamais dans le `data/` du dépôt.
    """

    def __init__(self, racine: Path, data_dir: Path | None = None) -> None:
        self.racine = Path(racine)
        self.data_dir = Path(data_dir) if data_dir is not None else self.racine / "data"

    # --- lecture ----------------------------------------------------------------------------------

    @property
    def chemin(self) -> Path:
        return self.data_dir / REPERTOIRE_ESPACE

    @property
    def pointeur(self) -> Path:
        return self.chemin / POINTEUR

    def installe(self) -> bool:
        return self.pointeur.is_symlink()

    def generation(self) -> str:
        """La génération sur laquelle `courant` pointe."""
        return _lire_pointeur(self.chemin)

    def absolu(self, cible: Path) -> Path:
        """La cible en chemin absolu sous la racine — un chemin relatif s'entend depuis la racine.

        Sans cela, un appelant passant `data/manifest.json` sonderait le répertoire courant du
        processus au lieu de l'espace, et l'installation refuserait (ou pire, migrerait) le mauvais
        fichier.
        """
        chemin = Path(cible)
        return chemin if chemin.is_absolute() else (self.racine / chemin)

    def slot(self, cible: Path) -> Path:
        """Le chemin de `cible` **dans** une génération — son chemin relatif à la racine.

        Le bundle est un miroir de l'arborescence publiée : `data/manifest.json` vit à
        `<gen>/data/manifest.json`. Aucun nom aplati, aucune table de correspondance à tenir à jour :
        le chemin *est* la clé, donc deux cibles distinctes ne peuvent pas se partager un slot.
        """
        try:
            # `os.path.abspath` et non `resolve()` : résoudre suivrait le lien de la cible et
            # rendrait le chemin **dans** le bundle, donc un slot imbriqué à chaque appel.
            return Path(os.path.abspath(self.absolu(cible))).relative_to(
                Path(os.path.abspath(self.racine)))
        except ValueError as exc:
            raise LotHorsEspace(
                f"{cible} : hors de la racine {self.racine} — aucun pointeur ne peut la couvrir"
            ) from exc

    def chemin_dans(self, cible: Path, generation: str) -> Path:
        return self.chemin / generation / self.slot(cible)

    def resolue_dans_lespace(self, cible: Path) -> bool:
        """`cible` se résout-elle dans la génération courante ?

        On compare des chemins **résolus**, pas des types d'entrée : une cible directement liée et
        une cible vivant dans un répertoire lié (les archives de campagne) passent toutes deux par
        le pointeur, donc l'atome les couvre toutes deux. Un lien pendant se résout aussi — c'est
        exactement ce qu'il faut, puisqu'un lien pendant *est* l'absence (`FileNotFoundError` à la
        lecture, fermeture B6 conservée).
        """
        attendu = self.chemin_dans(cible, self.generation())
        return Path(os.path.realpath(self.absolu(cible))) == Path(os.path.realpath(attendu))

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
        self.chemin.mkdir(parents=True, exist_ok=True)
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

    def basculer(self, lot: Sequence[tuple[Path, str | None]]) -> None:
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
        """
        with contextlib.suppress(BaseException):
            transaction.lien_tmp.unlink()
        with contextlib.suppress(BaseException):
            transaction.marque.unlink()
        with contextlib.suppress(BaseException):
            _fsync_repertoire(self.chemin)
        with contextlib.suppress(BaseException):
            verrou.__exit__()

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
        indiscernable d'un bundle complet. La marque posée avant la première écriture n'est donc
        retirée que lorsqu'on a pu conclure ; sinon elle est **reposée**, et la sonde la voit.

        `ignore_errors` couvre les échecs d'`OSError`, pas l'interruption : un `KeyboardInterrupt`
        pendant l'effacement remonte, et c'est voulu — l'appelant doit voir l'interruption.
        """
        try:
            transaction.lien_tmp.unlink()
        except OSError:
            pass
        if not transaction.prepare:
            # Rien n'a été écrit dans la génération inactive : elle porte encore le bundle
            # précédent complet, qui est la matière du prochain miroir. L'effacer serait une perte
            # nette pour un refus qui n'a rien touché.
            return
        publiee = self._generation_publiee()
        if publiee is None:
            transaction.marquer()
            return
        if publiee == transaction.suivante:
            return
        poubelle: Path | None = None
        try:
            poubelle = Path(tempfile.mkdtemp(prefix=f".{transaction.suivante}.abandonne.",
                                             suffix=".tmp", dir=self.chemin))
            os.rename(self.chemin / transaction.suivante, poubelle / transaction.suivante)
        except OSError:
            pass
        try:
            transaction.marque.unlink()
        except OSError:
            pass
        if poubelle is not None:
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
        posée avant la première écriture, et reposée par l'abandon prudent, est ce que cette sonde
        voit alors : une génération inactive partielle n'est plus jamais silencieuse.

        Ce qu'elle ne compte **pas**, et c'est délibéré : la génération inactive complète laissée
        par une bascule réussie. C'est le bundle précédent, suivi par git, matière du prochain
        miroir — pas un résidu.
        """
        if not self.chemin.is_dir():
            return []
        return sorted({str(p.relative_to(self.chemin)) for p in self.chemin.rglob("*")
                       if any(part.endswith(".tmp")
                              for part in p.relative_to(self.chemin).parts)})


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

    def lire(self, cible: Path) -> str | None:
        """Le contenu **publié** d'une cible du lot, lu sous le verrou. `None` si elle est absente.

        La lecture passe par le slot de la génération courante plutôt que par le lien de la cible :
        c'est le même octet, mais dit dans le repère de la transaction. C'est cette lecture-là —
        et non celle que l'appelant aurait faite avant d'entrer — qui rend la fusion sûre.
        """
        self.espace.verifier_lot([cible])
        chemin = self.espace.chemin / self.courante / self.espace.slot(cible)
        try:
            return chemin.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def marquer(self) -> None:
        """Pose la marque du brouillon, sans jamais lever — elle est un signal, pas une garantie."""
        with contextlib.suppress(OSError):
            os.close(os.open(self.marque, os.O_CREAT | os.O_WRONLY, 0o644))

    def publier(self, lot: Sequence[tuple[Path, str | None]]) -> None:
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
        """
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
        self.prepare = True
        self.marquer()
        _reconstruire(self.espace.chemin / self.courante, racine_suivante)
        for cible, contenu in lot:
            chemin = racine_suivante / self.espace.slot(cible)
            if contenu is None:
                _retirer_du_bundle(chemin)
            else:
                _ecrire_dans_bundle(chemin, contenu)
        _fsync_repertoire(racine_suivante)
        # Le brouillon est complet et durable : plus rien ne le distingue d'un bundle publiable.
        with contextlib.suppress(OSError):
            self.marque.unlink()
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


def _ecrire_dans_bundle(chemin: Path, contenu: str) -> None:
    """Écrit un slot de la génération inactive : temporaire, `fsync`, puis `rename` **dans le bundle**.

    Le `rename` remplace l'entrée plutôt que d'écrire à travers elle : c'est ce qui empêche
    l'écriture d'un slot de traverser le lien dur posé par `_reconstruire` et d'atteindre la
    génération encore publiée.
    """
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd, temporaire = tempfile.mkstemp(prefix=f".{chemin.name}.", suffix=".tmp", dir=chemin.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as flux:
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
    """
    chemin = Path(cible)
    if not chemin.is_symlink():
        return None
    resolu = Path(os.path.realpath(chemin))
    for parent in resolu.parents:
        if parent.name != REPERTOIRE_ESPACE:
            continue
        generation = resolu.relative_to(parent).parts[0]
        if generation not in GENERATIONS:
            return None
        slot = resolu.relative_to(parent / generation)
        donne = Path(os.path.abspath(chemin))
        # Le chemin donné doit se terminer par le slot : sinon les deux repères ne décrivent pas la
        # même cible, et rien ne permet d'en déduire une racine.
        if donne.parts[-len(slot.parts):] != slot.parts:
            return None
        racine = Path(*donne.parts[:-len(slot.parts)])
        sous_racine = parent.parent.relative_to(parent.parent.parent)
        espace = EspacePublie(racine, racine / sous_racine)
        return espace if espace.resolue_dans_lespace(chemin) else None
    return None


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
    espace = EspacePublie(args.racine, args.data_dir)
    cibles = list(args.cible)
    if args.depot:
        cibles += [c for c in cibles_du_depot(espace.racine, espace.data_dir) if c not in cibles]
    try:
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
