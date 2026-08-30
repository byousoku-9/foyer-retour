"""La racine de publication, **côté lecture** : pincer une génération, tout lire à travers elle.

Story 4.5, B7, tour de la racine vraiment unique (N1). L'espace de publication
(`server/evals/espace.py`) rendait déjà une opération d'**écriture** tout-ou-rien : un bundle
immuable, un pointeur unique, un seul `os.replace`. Mais il n'existait **aucune API de lecture**.
Le seul objet qui figeait une génération était `Transaction` — une API d'écriture, qui exige le
`flock` exclusif et meurt à la sortie du `with` : aucun lecteur de production ne pouvait s'en
servir sans sérialiser toute la production derrière lui. Toute autre voie relisait le pointeur **à
chaque appel système**, un `readlink` par cible.

La conséquence est mesurable : le démarrage du service résout une quarantaine de cibles couvertes
(manifest, puis `document.json`, overlay, structure, sommaire, rapport, dictionnaires, publication
d'évals). Une bascule tombant entre deux de ces résolutions rend un état **composé de deux
générations** — et le contrôle d'empreinte qui existe précisément pour interdire le mélange ne le
voit pas, puisqu'il portait sur des octets qu'un second `open()` avait pu remplacer.

## Ce que ce module ajoute, et où il vit

Une **lecture pincée** : `courant` est résolu **une seule fois**, la génération obtenue devient le
repère immuable de toute l'opération, et chaque cible couverte est lue à travers ce repère plutôt
qu'à travers son lien. Aucun paramètre ne permet de retomber sur une résolution vivante : le choix
est **structurel** — il n'y a pas d'espace installé, donc pas de pointeur, donc rien à pincer et
rien à mêler ; ou il y en a un, et tout passe par lui.

Il vit dans `corpus` et non dans `server/evals/` pour une raison de spine, pas de goût : la table
des couches interdit à `corpus` comme à `api` d'importer quoi que ce soit hors de `domain` et de la
stdlib (`tests/test_layers.py`). Un lecteur servi ne peut donc pas importer l'espace. Ce module est
la moitié **lecture** de la disposition, écrite en stdlib pure ; `server/evals/espace.py` en importe
les constantes et la classe de base, de sorte que la disposition n'ait **qu'une** autorité — pas
deux littéraux `.publie` qu'un caractère de différence désaccorderait.

## Ce qu'un repère garantit, et ce qu'il ne garantit pas

Garanti : **une opération de lecture ne mêle jamais deux générations**. Toutes ses cibles couvertes
viennent du même snapshot, et les octets *hachés* sont les octets *parsés* — une seule lecture, un
seul tampon (`Lecture.octets`).

Garanti aussi : le repère **survit à une bascule concurrente**. Il y a deux générations qui
alternent ; celle qu'un lecteur a pincée devient inactive lors de la bascule suivante, mais elle
n'est reconstruite qu'à la **seconde** — un lecteur pincé traverse donc entièrement une bascule.

Non garanti, et écrit ici plutôt que tu : **deux** bascules pendant une même passe de lecture
finissent par reconstruire la génération pincée. Ce cas ne rend jamais un état mêlé — il est
**détecté** (`Lecture.perimee`, sur l'identité de l'inode que le repère tient ouvert) et la passe
est rejouée par `relire`. Après un nombre borné de tentatives, le refus est dit plutôt que résolu
par un état partiel. Non garanti non plus : ce module ne prend aucun verrou, délibérément — un
lecteur qui sérialiserait la production pour être cohérent coûterait plus cher que le défaut qu'il
ferme.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

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

# Combien de fois une passe de lecture est rejouée quand la génération qu'elle avait pincée a été
# reconstruite sous elle. Il en faut **deux** bascules pour que cela arrive ; trois tentatives
# laissent donc une marge large, et la borne existe pour que l'échec se dise au lieu de tourner.
ESSAIS_DE_LECTURE = 3


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


class LectureHorsGeneration(Exception):
    """La racine couvre ce chemin, mais la cible ne passe plus par le pointeur.

    Revue du tour N1–N3, constat 2. Un lien couvert remplacé par un fichier ordinaire, ou une
    disposition reposée pendant une passe, faisaient rendre le **chemin brut** — c'est-à-dire une
    lecture à travers le lien vivant, hors de la génération pincée. Un lecteur qui ne peut pas
    conclure refuse ; il ne devine pas.
    """


class LecturePerimee(Exception):
    """La génération qu'un repère avait pincée a été reconstruite sous lui.

    Ce n'est **pas** un état mêlé — c'est exactement le contraire : le repère refuse de rendre des
    octets dont il ne peut plus affirmer qu'ils viennent de la génération qu'il a pincée. La passe
    est rejouée (`relire`) ; après épuisement des tentatives, le refus est dit.
    """


def lire_pointeur(espace: Path) -> str:
    """La génération que `courant` désigne, ou un refus nommé — jamais une supposition."""
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
    # **Une génération nommée mais absente est illisible, pas vide** (revue N1–N3, constat 6). Sans
    # ce contrôle, chaque slot était vu absent et le corpus, le smoke et le typage rendaient un état
    # **vide sans refuser** : un espace qu'on ne sait pas lire doit se dire.
    if not (espace / cible).is_dir():
        raise EspaceIllisible(
            f"{espace / cible} : `courant` désigne {cible!r}, dont le répertoire est absent ou "
            "illisible — l'espace de publication ne peut pas être lu")
    return cible


class RacinePubliee:
    """La disposition d'une racine : où vit le bundle, quel est son pointeur, quel slot a une cible.

    Lecture seule. `server/evals/espace.py::EspacePublie` en hérite et y ajoute l'installation, la
    transaction et la bascule — de sorte qu'un lecteur ne puisse pas, même par accident, atteindre
    une API d'écriture.
    """

    def __init__(self, racine: Path, data_dir: Path | None = None) -> None:
        self.racine = Path(racine)
        self.data_dir = Path(data_dir) if data_dir is not None else self.racine / "data"

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
        return lire_pointeur(self.chemin)

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

    def couverte(self, cible: Path) -> bool:
        """`cible` passe-t-elle par le pointeur de cette racine ? — **sans jamais résoudre `courant`**.

        C'est la question du **lecteur**, et elle diffère de celle de l'écrivain
        (`resolue_dans_lespace`, qui exige la génération *courante*). Un lecteur qui a pincé `a`
        pendant qu'une bascule publiait `b` doit continuer de reconnaître ses cibles comme
        couvertes : les opposer au pointeur vivant les ferait retomber sur une lecture à travers le
        lien, c'est-à-dire sur le mélange même que le repère existe pour interdire.

        **La réponse est de l'arithmétique de chemins, jamais une résolution** (revue du tour N1–N3,
        constat 2). La version d'avant faisait un `os.path.realpath` par cible et par génération :
        chacun retraversait `courant`, si bien qu'un repère « qui résout `courant` une seule fois »
        le résolvait en réalité une fois **par lecture**. Pire, quand la résolution échouait un
        instant — repose de disposition concurrente, lien couvert remplacé par un fichier ordinaire
        —, la cible était déclarée non couverte et lue **à travers son lien vivant**, exactement ce
        que le repère existe pour interdire.

        Ici on lit la **cible du lien** (`readlink`, un seul appel, aucune traversée) et on la
        compare, par arithmétique de chemins, à `<espace>/<pointeur>/<slot>` — la forme même que
        pose `installer()`. Un ancêtre lié compte : l'archive de campagne vit sous un répertoire
        lié, et c'est ce répertoire qui porte la couverture.

        Un lien **pendant** est reconnu comme couvert : c'est exactement ce qu'il faut, puisqu'un
        lien pendant *est* l'absence d'un slot, et l'absence se lit dans la génération pincée comme
        le reste.
        """
        return self.lien_couvrant(cible) is not None

    def lien_couvrant(self, cible: Path) -> Path | None:
        """Le premier ancêtre (ou la cible elle-même) qui est un lien **passant par le pointeur**.

        `None` si aucun : la cible ne relève alors pas de cette racine, ou sa disposition est
        cassée — c'est `Lecture.reel` qui distingue les deux, et qui refuse plutôt que de deviner.
        """
        try:
            slot = self.slot(cible)
        except LotHorsEspace:
            return None
        chemin = Path(os.path.abspath(self.absolu(cible)))
        reste = slot
        while True:
            if os.path.islink(chemin):
                try:
                    lien = os.readlink(chemin)
                except OSError:
                    return None
                vise = Path(os.path.normpath(os.path.join(str(chemin.parent), lien)))
                attendu = Path(os.path.abspath(self.chemin / POINTEUR / reste))
                return chemin if vise == attendu else None
            if not reste.parts or str(reste) == ".":
                return None
            chemin, reste = chemin.parent, reste.parent

    def resolue_dans_lespace(self, cible: Path) -> bool:
        """`cible` se résout-elle dans la génération **courante** ?

        On compare des chemins **résolus**, pas des types d'entrée : une cible directement liée et
        une cible vivant dans un répertoire lié (les archives de campagne) passent toutes deux par
        le pointeur, donc l'atome les couvre toutes deux. Un lien pendant se résout aussi — c'est
        exactement ce qu'il faut, puisqu'un lien pendant *est* l'absence (`FileNotFoundError` à la
        lecture, fermeture B6 conservée).
        """
        attendu = self.chemin_dans(cible, self.generation())
        return Path(os.path.realpath(self.absolu(cible))) == Path(os.path.realpath(attendu))

    def lecture(self) -> Lecture:
        """Pince la génération courante et rend le repère de lecture immuable.

        Une seule résolution de `courant`, une seule fois : c'est toute la propriété N1. Si l'espace
        n'est pas installé, il n'y a pas de pointeur, donc rien à pincer et rien à mêler — le repère
        rendu lit les chemins tels quels.

        Un espace **non installé** n'a rien à pincer : le repère lit les chemins tels quels, ce qui
        est le comportement d'un arbre sans racine. Un espace **illisible**, lui, ne se lit pas comme
        un espace vide (revue N1–N3, constat 6) : `EspaceIllisible` remonte, parce qu'un lecteur qui
        ne peut pas conclure refuse au lieu de rendre un corpus vide.
        """
        try:
            generation = lire_pointeur(self.chemin)
        except EspaceNonInstalle:
            return Lecture(self, None)
        return Lecture(self, generation)


class Lecture:
    """Un repère de lecture **immuable** : une génération, résolue une fois, pour toute une passe.

    Jamais construit directement par un lecteur de production : `RacinePubliee.lecture()` et
    `lecture_de()` en sont les origines, et elles décident structurellement s'il y a une génération
    à pincer. `generation is None` signifie « aucun espace installé » : les chemins sont alors lus
    tels quels, ce qui est le comportement d'avant ce tour pour un arbre sans racine.
    """

    def __init__(self, racine: RacinePubliee | None, generation: str | None) -> None:
        self.racine = racine
        self.generation = generation
        self._fd: int | None = None
        self._ino: int | None = None
        if racine is not None and generation is not None:
            # Le repère **tient ouvert** le répertoire de la génération pincée. Deux raisons, et les
            # deux comptent : l'inode ne peut pas être recyclé tant qu'il est ouvert, donc
            # `perimee()` ne peut pas confondre une génération reconstruite avec la même ; et
            # `fstat` est ce qui rend la question décidable sans relire le pointeur.
            #
            # L'échec est **absorbé ici mais pas oublié** : `_ino` reste `None`, et `perimee()` le
            # lit comme « indécidable », donc comme périmé. Un repère qui a pincé une génération
            # sans pouvoir tenir son inode ne peut pas affirmer qu'elle n'a pas été reconstruite
            # sous lui — et un garde-fou qui ne peut pas conclure refuse au lieu de se taire. Le
            # taire aurait été exactement le best-effort que N2 ferme par ailleurs.
            with contextlib.suppress(OSError):
                self._fd = os.open(racine.chemin / generation, os.O_RDONLY)
                self._ino = os.fstat(self._fd).st_ino

    # --- le repère ---------------------------------------------------------------------------------

    def reel(self, cible: Path) -> Path:
        """Le chemin **effectivement lu** pour `cible` : son slot dans la génération pincée.

        Une cible que la racine ne couvre pas — une source, un cache, un artefact hors bundle —
        garde son chemin : il n'y a rien à pincer, donc rien à mêler. Il n'existe pas de paramètre
        pour forcer l'un ou l'autre ; la question est structurelle.

        **Trois cas, et aucun repli silencieux** (revue du tour N1–N3, constats 1 et 2) :

        1. la génération pincée a été **reconstruite** sous le repère : les octets qu'on rendrait ne
           viennent plus de ce qu'on a pincé. `LecturePerimee` — le contrôle ne peut pas rester à la
           charge d'un appelant, puisque aucun ne le faisait ;
        2. un ancêtre (ou la cible) est lié **par le pointeur** : c'est le slot de la génération
           pincée qui est rendu, absence comprise ;
        3. rien ne la lie par le pointeur. Si la racine ne connaît pas ce chemin — aucun slot chez
           elle —, il est rendu tel quel. Si elle le connaît (le slot existe dans la génération
           pincée) mais que la cible ne passe plus par le pointeur, la disposition est cassée : le
           refus est **nommé** (`LectureHorsGeneration`), jamais un repli sur le lien vivant.
        """
        chemin = Path(cible)
        if self.racine is None or self.generation is None:
            return chemin
        if self.perimee():
            raise LecturePerimee(
                f"{cible} : la génération {self.generation!r} pincée par ce repère a été "
                "reconstruite sous lui — les octets rendus ne viendraient plus d'une seule "
                "génération")
        try:
            slot = self.racine.chemin_dans(chemin, self.generation)
        except LotHorsEspace:
            return chemin
        if self.racine.lien_couvrant(chemin) is not None:
            return slot
        if os.path.lexists(slot):
            raise LectureHorsGeneration(
                f"{cible} : la racine couvre ce chemin (slot {slot}) mais la cible ne passe plus "
                "par le pointeur — un lecteur ne devine pas une disposition cassée, il la dit")
        return chemin

    def perimee(self) -> bool:
        """La génération pincée a-t-elle été **reconstruite** sous ce repère ?

        Il faut deux bascules pour cela : la première rend la génération pincée inactive sans y
        toucher, la seconde la reconstruit. La question se pose sur l'identité de l'inode que le
        repère tient ouvert — jamais sur le pointeur, qui a le droit d'avoir bougé.

        Un repère **sans espace** n'est jamais périmé : il n'a rien pincé, donc rien ne peut avoir
        bougé sous lui. Un repère qui a pincé une génération mais n'a **pas** pu tenir son inode
        l'est toujours : la question est alors indécidable, et rendre `False` reviendrait à
        promettre une cohérence qu'on ne peut pas contrôler.
        """
        if self.racine is None or self.generation is None:
            return False
        if self._ino is None:
            return True
        try:
            return os.stat(self.racine.chemin / self.generation).st_ino != self._ino
        except OSError:
            return True

    def fermer(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            with contextlib.suppress(OSError):
                os.close(fd)

    def __enter__(self) -> Lecture:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.fermer()

    # --- les lectures ------------------------------------------------------------------------------

    def octets(self, cible: Path) -> bytes | None:
        """Les octets publiés de `cible`, ou `None` si elle est absente.

        **Une seule lecture, un seul tampon** : c'est ce qui rend vrai « les octets hachés sont les
        octets parsés ». Hacher un fichier puis le rouvrir pour le parser laissait deux `open()`
        distincts de part et d'autre d'une bascule possible, donc un contrôle d'empreinte qui
        validait des octets que personne n'utilisait.
        """
        try:
            return self.reel(cible).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            # Une **absence** reste une absence : un slot jamais publié, ou dont un composant du
            # chemin n'existe pas, n'est pas un incident (revue N1–N3, constat 15). Toute autre
            # `OSError` — droits, répertoire, tube — remonte : ce qu'on ne peut pas lire se dit.
            return None

    def texte(self, cible: Path, encodage: str = "utf-8") -> str | None:
        octets = self.octets(cible)
        return None if octets is None else octets.decode(encodage)

    def fichier(self, cible: Path) -> bool:
        """`cible` est-elle un fichier régulier lisible dans la génération pincée ?"""
        return self.reel(cible).is_file()

    def verifier(self) -> None:
        """Refuse si la génération pincée a été reconstruite — le contrôle **de fin de passe**.

        `reel` refuse déjà à chaque lecture ; celle-ci existe pour les passes qui composent autre
        chose que des octets (une décision de gate, un attendu de promotion) et qui doivent pouvoir
        dire, avant de conclure, que rien n'a bougé sous elles.
        """
        if self.perimee():
            raise LecturePerimee(
                f"{self.racine.chemin if self.racine else '?'} : la génération "
                f"{self.generation!r} pincée par ce repère a été reconstruite sous lui")


def racine_couvrant(cible: Path) -> RacinePubliee | None:
    """La racine dont le pointeur couvre `cible`, ou `None` — **reconnaissance structurelle**.

    Le chemin résolu d'une cible couverte est `<data_dir>/.publie/<génération>/<slot>` : le slot s'y
    lit directement, `data_dir` est le parent du répertoire d'espace, et la racine son parent.
    Aucun nom de document, aucun préfixe codé.

    **La racine est rendue dans le repère de l'appelant, pas dans celui du chemin résolu** (défaut
    trouvé à la vérification indépendante du tour 3/3). `slot()` compare par `os.path.abspath`, qui
    ne résout pas les liens : dériver la racine de `os.path.realpath` mêlerait deux repères, et
    toute cible atteinte par un préfixe lié — `/tmp` en est un sur macOS — ferait lever
    `LotHorsEspace` là où un repli est annoncé. Le slot est donc **retiré du chemin donné** pour
    obtenir la racine telle que l'appelant l'exprime.

    Cette fonction vit ici, et non dans `server/evals/espace.py`, parce que la question « cette
    cible est-elle couverte ? » est celle d'un **lecteur** autant que d'un écrivain, et que la table
    des couches interdit à `corpus` d'importer les évals. L'espace en dérive sa propre version, qui
    rend un objet d'écriture.
    """
    chemin = Path(cible)
    if not chemin.is_symlink():
        return None
    resolu = Path(os.path.realpath(chemin))
    for parent in resolu.parents:
        if parent.name != REPERTOIRE_ESPACE:
            continue
        relatif = resolu.relative_to(parent)
        if not relatif.parts or relatif.parts[0] not in GENERATIONS:
            return None
        slot = relatif.relative_to(relatif.parts[0])
        donne = Path(os.path.abspath(chemin))
        # Le chemin donné doit se terminer par le slot : sinon les deux repères ne décrivent pas la
        # même cible, et rien ne permet d'en déduire une racine.
        if not slot.parts or donne.parts[-len(slot.parts):] != slot.parts:
            return None
        racine = Path(*donne.parts[:-len(slot.parts)])
        # Le `data/` **relatif à la racine**, jamais deviné à un niveau fixe : un espace peut vivre
        # sous `<racine>/data/.publie` (la production) comme sous `<racine>/.publie` (un `data-dir`
        # qui est sa propre racine). Prendre systématiquement le parent du parent inventait un
        # niveau et rendait une racine décalée d'un cran.
        try:
            sous_racine = parent.parent.relative_to(Path(os.path.realpath(racine)))
        except ValueError:
            return None
        return RacinePubliee(racine, racine / sous_racine)
    return None


def lecture_de(data_dir: Path | str, racine: Path | None = None) -> Lecture:
    """Le repère de lecture d'un `data/` — pincé s'il a un espace installé, direct sinon.

    C'est l'entrée que les lecteurs de production emploient. La racine se déduit du `data/` comme
    partout ailleurs (`espace_du_data_dir` en est le pendant écriture) : un `data/` est le
    répertoire de données d'une racine qui est son parent, sauf mention explicite.
    """
    data = Path(data_dir)
    return RacinePubliee(data.parent if racine is None else Path(racine), data).lecture()


T = TypeVar("T")


def relire(data_dir: Path | str, passe: Callable[[Lecture], T],
           *, essais: int = ESSAIS_DE_LECTURE) -> T:
    """Exécute `passe` sur une génération pincée, en la **rejouant** si le repère a été périmé.

    Deux bascules pendant une même passe finissent par reconstruire la génération pincée. Le repère
    le voit (`perimee`) ; la seule réponse honnête est de recommencer la passe sur un repère neuf,
    parce qu'un résultat composé de deux générations est précisément ce que l'AC interdit.

    Après épuisement des tentatives, le refus est **dit** (`LecturePerimee`) : sous une production
    qui bascule assez vite pour périmer trois passes de suite, rendre un état est un mensonge.
    """
    derniere: Lecture | None = None
    for _ in range(max(1, essais)):
        with lecture_de(data_dir) as lecture:
            derniere = lecture
            try:
                resultat = passe(lecture)
            except LecturePerimee:
                # Le repère a refusé **pendant** la passe : c'est le même événement que celui que
                # `perimee()` constate après coup, vu plus tôt. La réponse est la même — rejouer.
                continue
            if not lecture.perimee():
                return resultat
    raise LecturePerimee(
        f"{data_dir} : la génération pincée a été reconstruite pendant chacune des {essais} "
        f"tentatives de lecture (dernière : {derniere.generation if derniere else None}) — aucun "
        "état issu d'une seule génération n'a pu être rendu")


@contextlib.contextmanager
def lecture_pincee(data_dir: Path | str) -> Iterator[Lecture]:
    """Un repère pincé pour la durée d'un `with`, sans rejeu — pour les appelants qui en composent un.

    `relire` est la forme sûre : elle rejoue. Celle-ci sert quand l'appelant compose lui-même
    plusieurs passes et veut que **toutes** partagent le même repère ; il lui appartient alors de
    consulter `perimee()`.
    """
    lecture = lecture_de(data_dir)
    try:
        yield lecture
    finally:
        lecture.fermer()
