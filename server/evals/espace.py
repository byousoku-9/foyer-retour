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
- **rien de ce qui suit le commit ne peut lever** : le `fsync` du répertoire de l'espace et le
  retrait du lien temporaire sont exécutés en absorbant toute exception, `BaseException` comprise.
  Une interruption arrivée là est **absorbée**, parce que la transaction est acquise et que
  l'annuler est impossible ; c'est dit ici plutôt que tu.

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
from collections.abc import Iterable, Sequence
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

    # --- la bascule -------------------------------------------------------------------------------

    def basculer(self, lot: Sequence[tuple[Path, str]]) -> None:
        """Publie **tout** le lot, ou rien du tout, par un unique `os.replace`.

        `lot` est une suite de `(cible, contenu)` remise **au même appel** : le lot est l'ensemble
        complet des cibles qu'implique l'opération, `data/manifest.json` compris. Aucune cible n'en
        est retirée, et il n'existe pas de variante « une seule cible » qui supprimerait le rang où
        échouer.

        Déroulé, et pourquoi chaque étape est là :

        1. **Vérifier** que chaque cible est résolue par le pointeur. Rien n'est touché ; un refus
           ici est un refus avant écriture.
        2. **Verrouiller** l'espace (`flock`). Le ping-pong à deux générations n'est sûr que sous
           verrou : deux runs concurrents choisiraient sinon la même génération inactive et le
           pointeur publierait un mélange. C'est la dette `target_story: 4.1` (« deux écrivains de
           `data/` à réunir »), payée ici parce que la forme l'exige.
        3. **Construire** la génération inactive : elle est d'abord un miroir en liens **durs** de
           la génération courante — les surfaces que ce lot ne réécrit pas gardent leur contenu —
           puis les slots du lot y sont écrits, `fsync` compris. Rien de tout cela n'est une cible :
           `courant` pointe toujours sur l'ancienne génération, donc l'état observable du lot est
           inchangé, à chaque frontière d'instruction.
        4. **Basculer** : un `os.replace` d'un lien symbolique sur `courant`. C'est l'atome.

        Toute exception avant 4 laisse la génération inactive à l'abandon (elle est jetée par
        `_abandonner`) et **aucune cible modifiée**. Toute exception pendant ou après 4 : le
        `rename` a eu lieu ou n'a pas eu lieu, et c'est le **pointeur sur disque** qui le dit, pas
        un drapeau Python. S'il a eu lieu, l'opération a réussi et rien n'est propagé (frontière
        post-commit) ; sinon, rien n'est publié et la cause remonte.
        """
        cibles = [cible for cible, _contenu in lot]
        self.verifier_lot(cibles)
        # Deux cibles qui partagent un slot rendraient la bascule ambiguë — laquelle gagne ? Le
        # refus est dit plutôt que résolu par l'ordre d'itération.
        slots = [str(self.slot(cible)) for cible in cibles]
        if len(set(slots)) != len(slots):
            raise LotHorsEspace(
                f"deux cibles du lot partagent un slot : {sorted(slots)} — un lot ne publie pas "
                "deux contenus au même chemin")
        with _verrou(self.chemin):
            courante = self.generation()
            suivante = GENERATIONS[1] if courante == GENERATIONS[0] else GENERATIONS[0]
            racine_suivante = self.chemin / suivante
            lien_tmp = self.chemin / f".{POINTEUR}.{os.getpid()}.tmp"
            try:
                _reconstruire(self.chemin / courante, racine_suivante)
                for cible, contenu in lot:
                    _ecrire_dans_bundle(racine_suivante / self.slot(cible), contenu)
                _fsync_repertoire(racine_suivante)
                lien_tmp.unlink(missing_ok=True)
                os.symlink(suivante, lien_tmp)
                # --- L'ATOME. Un seul `rename(2)`, dans un seul répertoire, sur une entrée qui
                # existe déjà. Avant lui, les cibles se résolvent toutes dans `courante` ; après,
                # toutes dans `suivante`. Il n'y a pas de frontière entre les deux.
                os.replace(lien_tmp, self.pointeur)
            except BaseException:
                # **Le pointeur sur disque est la seule autorité** (tour correctif 3/3). Noter le
                # succès dans une variable Python ne marche pas : `KeyboardInterrupt` peut tomber
                # entre le `os.replace` réussi et l'affectation qui suit, et le gestionnaire
                # détruirait alors la génération **devenue active** — trois cibles publiées puis
                # rendues pendantes, une destruction et non un état mêlé.
                if self._generation_publiee() == suivante:
                    # Le commit est acquis : le lot entier est publié, l'opération a réussi.
                    # Propager ici rendrait une exception avec tout le lot déjà basculé.
                    self._apres_le_commit(lien_tmp)
                    return
                self._abandonner(suivante, lien_tmp)
                raise
            self._apres_le_commit(lien_tmp)

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

    def _apres_le_commit(self, lien_tmp: Path) -> None:
        """Tout ce qui suit le point de commit, et qui ne peut donc **jamais** lever.

        Le lot est publié : une exception propagée ici serait une exception avec le lot déjà
        basculé. Le `fsync` du répertoire rend l'entrée durable — son échec (`EIO`) coûte de la
        durabilité, jamais l'atomicité — et le retrait du lien temporaire n'a plus d'objet, le
        `rename` l'ayant consommé. Les deux sont donc absorbés, `BaseException` comprise : une
        interruption arrivée après le commit ne peut plus annuler quoi que ce soit.
        """
        with contextlib.suppress(BaseException):
            lien_tmp.unlink()
        with contextlib.suppress(BaseException):
            _fsync_repertoire(self.chemin)

    def _abandonner(self, generation: str, lien_tmp: Path) -> None:
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

        `ignore_errors` couvre les échecs d'`OSError`, pas l'interruption : un `KeyboardInterrupt`
        pendant l'effacement remonte, et c'est voulu — l'appelant doit voir l'interruption.
        """
        try:
            lien_tmp.unlink()
        except OSError:
            pass
        if self._generation_publiee() in (None, generation):
            return
        poubelle: Path | None = None
        try:
            poubelle = Path(tempfile.mkdtemp(prefix=f".{generation}.abandonne.", suffix=".tmp",
                                             dir=self.chemin))
            os.rename(self.chemin / generation, poubelle / generation)
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

        Ce qu'elle ne compte **pas**, et c'est délibéré : la génération inactive complète laissée
        par une bascule réussie. C'est le bundle précédent, suivi par git, matière du prochain
        miroir — pas un résidu.
        """
        if not self.chemin.is_dir():
            return []
        return sorted({str(p.relative_to(self.chemin)) for p in self.chemin.rglob("*")
                       if any(part.endswith(".tmp")
                              for part in p.relative_to(self.chemin).parts)})


class _verrou:  # noqa: N801 — gestionnaire de contexte, employé comme une fonction
    """Un `flock` exclusif sur l'espace, en gestionnaire de contexte."""

    def __init__(self, espace: Path) -> None:
        self.chemin = espace / VERROU
        self.fd: int | None = None

    def __enter__(self) -> _verrou:
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.chemin, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


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
    est `<data_dir>/.publie/<génération>/<slot>`, donc `data_dir` est le parent du répertoire
    d'espace, et la racine son parent — exactement la dérivation unique de `espace_du_data_dir`.
    """
    chemin = Path(cible)
    if not chemin.is_symlink():
        return None
    resolu = Path(os.path.realpath(chemin))
    for parent in resolu.parents:
        if parent.name != REPERTOIRE_ESPACE:
            continue
        data_dir = parent.parent
        espace = EspacePublie(data_dir.parent, data_dir)
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
    p.add_argument("--migrer", action="store_true",
                   help="déplace le contenu d'une cible déjà existante dans le bundle")
    args = p.parse_args(argv)
    espace = EspacePublie(args.racine, args.data_dir)
    try:
        espace.installer(args.cible, migrer=args.migrer)
    except (EspaceNonInstalle, LotHorsEspace, EspaceIllisible, OSError) as exc:
        print(f"refus : {exc}")
        return 2
    print(f"espace posé : {espace.pointeur} -> {espace.generation()}")
    return 0


if __name__ == "__main__":  # pragma: no cover — point d'entrée
    raise SystemExit(_main())
