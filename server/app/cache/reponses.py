"""Le cache interne de réponses : clé exacte, jamais sémantique (story 5.6, T5).

**Ce qu'il fait.** Une question déjà posée, mot pour mot, sur le même document, la même image
d'ingestion, le même code, les mêmes prompts et les mêmes seuils, rend la réponse déjà payée sans
appeler personne. La réponse servie est **la même**, avec sa trace d'origine et son coût d'origine ;
la requête qui la reçoit, elle, coûte zéro.

**Ce qu'il ne fait pas, et c'est le point.** Il ne rapproche rien. Deux questions qui « veulent dire
la même chose » sont deux questions ; une faute de frappe est une requête payée. Un cache sémantique
choisirait, à la place du modèle, quelle question l'utilisateur a posée — exactement la classe de
décision que l'amendement AD-1 du 03/09/2026 retire du code. La seule liberté que la clé s'accorde
est la **forme** de la saisie : les espaces, la casse et la ponctuation finale, qui ne sont pas des
mots. Rien d'autre : pas d'accents repliés (« ou » et « où » ne sont pas la même question), pas de
ponctuation interne retirée, pas de radicalisation.

**Ce qui invalide une entrée.** Toutes les empreintes qui décident de la réponse entrent dans la
clé — `source_hash` et `ingest_fingerprint` du document, empreinte du dictionnaire,
`pipeline_digest`, `prompts_digest`, seuils publiés, variante — plus l'entrée humaine complète :
question normalisée, langue, historique, profil, faits, mot pour mot. Une seule de ces composantes
qui bouge, et la clé change : il n'existe aucun chemin qui serve une réponse produite par une autre
image. C'est le même patron que la namespace du cache d'évals (`server/evals/cache.py`), qu'on ne
peut pas importer d'ici — la table des couches interdit à `server/app` de dépendre des évals — et
dont on reprend donc la projection canonique, testée des deux côtés.

**Ce qui n'est jamais servi du cache.** Un document en quarantaine ou dont le gate est rouge
(`sans_gate`, `gate_perime`, `source_absente`, `bloquant_statique`) : servir une réponse ancienne
sous un document que le service ne valide plus serait précisément la bascule silencieuse qu'AD-11
interdit — et l'entrée n'est pas non plus **écrite**, pour que la levée de la quarantaine ne
ressuscite pas un stock constitué pendant qu'elle durait.

**Ce que le cache ne dispense pas de refaire.** La projection des citations (`presenter.sources_de`,
`presenter.clauses_de`) relit chaque `block_id` **dans le corpus servi**, sur un hit comme sur un
miss. Une réponse cachée dont une citation n'est plus confirmée est donc un échec terminal (AD-3),
jamais une réponse amputée servie en silence.

**Le stockage.** Un fichier JSON par entrée sous `.cache/reponses/` (ignoré par git), écrit
atomiquement (`os.replace`), sous verrou exclusif pour la fenêtre écriture + éviction. La lecture
n'a pas besoin du verrou : elle voit un fichier entier ou pas de fichier. Une entrée mal formée,
d'une autre version de schéma ou d'une autre clé n'est jamais lue « au mieux » : elle est un
**miss**, et elle est retirée.
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pydantic

from server.app.domain.answer import Answer
from server.app.domain.trace import Trace
from server.app.domain.verdict import ClaimJugee

LOG = logging.getLogger("foyer.cache")

# Version du **contrat de clé et d'entrée** de ce cache. Elle entre dans la clé : changer la
# composition de la clé ou la forme de la valeur périme mécaniquement tout ce qui est sur le disque,
# au lieu de relire d'anciens octets avec de nouvelles règles.
CACHE_SCHEMA_VERSION = 1

# Les alertes de `corpus.alerts` qui disent qu'un document servi n'est validé par rien de courant.
# Ce sont exactement celles que `EtatApp.gate_profile` refuse de résumer et que `/sante` publie.
ALERTES_DE_GATE: frozenset[str] = frozenset(
    {"sans_gate", "gate_perime", "source_absente", "bloquant_statique"})

# Ponctuation **finale** retirée de la question avant la clé. Elle est de la forme, pas du mot :
# « la foudre est-elle couverte ? » et « la foudre est-elle couverte » sont la même question posée
# deux fois. La liste est fermée et ne contient aucun caractère qui puisse porter du sens en
# position interne — un tiret ou une apostrophe n'y sont pas.
PONCTUATION_FINALE = " \t\n\r.?!…,;:"

# Marge de forme : au-delà, ce n'est plus une question, c'est un corps de requête. Le schéma HTTP
# borne déjà la question ; cette borne-ci protège la **clé** d'un stockage sans rapport avec ce que
# le cache sert. Elle vit avec le code qui l'emploie, comme `DIAGNOSTIC_LOG_MAX_CHARS` dans `llm`.
QUESTION_MAX_CHARS_CLE = 4096


class CacheCorrompu(ValueError):
    """Une entrée existe sur le disque mais ne satisfait pas le contrat persistant."""


def json_canonique(value: Any) -> str:
    """Projection JSON byte-stable, identique à celle du cache d'évals (clés triées, sans NaN)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def empreinte_canonique(value: Any) -> str:
    return hashlib.sha256(json_canonique(value).encode("utf-8")).hexdigest()


def normaliser_question(question: str) -> str:
    """Espaces, casse, ponctuation finale. **Rien d'autre.**

    Les trois seules libertés que la clé s'accorde, dans cet ordre : les suites d'espaces (y compris
    les retours à la ligne d'un copier-coller) deviennent un espace simple, la ponctuation de fin est
    retirée, et la casse est repliée. Aucune n'ajoute ni ne retire un mot ; toutes trois décrivent la
    même saisie. Tout le reste — accents, apostrophes, ponctuation interne, ordre — distingue.
    """
    compacte = " ".join(question.split())
    sans_finale = compacte.rstrip(PONCTUATION_FINALE)
    return sans_finale.casefold()


def document_cachable(etat: Any, doc_id: str) -> bool:
    """Ce document peut-il donner et recevoir une réponse cachée ?

    Trois conditions, et les trois sont celles que le service publie déjà : le document est
    **servi**, il n'est pas en **quarantaine**, et aucune alerte de gate ne pèse sur lui. Un
    document qu'on ne valide plus ne re-sert pas une réponse d'avant.
    """
    corpus = etat.corpus
    if doc_id not in getattr(corpus, "served", ()):
        return False
    if doc_id in getattr(corpus, "quarantine", {}):
        return False
    return not (ALERTES_DE_GATE & set(corpus.alerts.get(doc_id, ())))


def _empreinte_dictionnaire(dictionnaire: Any) -> str:
    """Empreinte du dictionnaire **effectivement chargé** pour ce document.

    Le dictionnaire arme l'un des deux court-circuits d'AD-5 et élargit les termes cherchés : deux
    réponses produites sous deux dictionnaires différents ne sont pas la même réponse, même à
    question et image identiques. L'empreinte est celle de l'objet entier, projetée exactement comme
    `server/evals/run.py` le fait (`asdict`, puis empreinte canonique) — y compris les tables
    privées, qui sont précisément ce qui élargit. Un dictionnaire absent est distingué d'un
    dictionnaire inerte : ce ne sont pas les mêmes réponses qu'ils autorisent.
    """
    if dictionnaire is None:
        return empreinte_canonique(None)
    if not dataclasses.is_dataclass(dictionnaire) or isinstance(dictionnaire, type):
        # La table des couches interdit à `cache` d'importer `corpus` : le type exact ne peut pas
        # être exigé ici. Un objet qui n'est pas la structure attendue n'est pas approximé — il rend
        # une empreinte qui lui est propre, donc une clé qui ne collisionne avec aucune autre.
        return empreinte_canonique({"type": type(dictionnaire).__name__})
    return empreinte_canonique(dataclasses.asdict(dictionnaire))


def composantes_de_cle(*, etat: Any, route: str, doc_id: str, question: str,
                       lang: str | None, variant: str | None,
                       entree: dict[str, Any],
                       dictionnaire: Any = None) -> dict[str, Any] | None:
    """Toutes les composantes normatives de la clé, ou `None` si ce document n'est pas cachable.

    `entree` porte ce que l'appelant a envoyé **au-delà** de la question : l'historique et le profil
    pour le guide, les faits pour le sinistre. Ils sont pris **mot pour mot**, sans normalisation :
    ce sont des données, pas une saisie de champ de recherche, et deux historiques différents ne
    donnent pas la même réponse. Le cahier de la story ne nomme que les faits du sinistre ; y
    ajouter l'historique et le profil ne relâche rien — la clé n'en devient que plus stricte, et
    l'omission aurait servi la réponse d'un autre dialogue.
    """
    if not document_cachable(etat, doc_id):
        return None
    if len(question) > QUESTION_MAX_CHARS_CLE:
        return None
    entry = etat.corpus.manifest.get(doc_id)
    if entry is None:
        return None
    return {
        "schema": CACHE_SCHEMA_VERSION,
        "route": route,
        "doc_id": doc_id,
        "lang": lang,
        "variant": variant,
        "question": normaliser_question(question),
        "entree": entree,
        "source_hash": entry.source_hash,
        "ingest_fingerprint": entry.ingest_fingerprint,
        "overlay_hash": entry.overlay_hash,
        "dictionary_fingerprint": _empreinte_dictionnaire(dictionnaire),
        "pipeline_digest": etat.pipeline_digest_hex,
        "prompts_digest": etat.prompts_digest_hex,
        "thresholds": etat.settings.thresholds(),
    }


@dataclass(frozen=True)
class EntreeDeCache:
    """Ce qu'une requête payée a produit, tel qu'il sera re-servi."""

    answer: Answer
    trace: Trace
    decision_claims: list[ClaimJugee] = field(default_factory=list)


@dataclass
class Compteurs:
    """Ce que `/sante` publie du cache : des comptes, jamais une question ni un texte."""

    hits: int = 0
    misses: int = 0
    ecritures: int = 0
    evictions: int = 0
    invalides: int = 0


class CacheDeReponses:
    """Le magasin sur disque, borné en nombre d'entrées et en octets, expirant par TTL."""

    def __init__(self, root: Path, *, ttl_s: float, max_entries: int, max_bytes: int) -> None:
        self.root = Path(root)
        self.ttl_s = float(ttl_s)
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        self.compteurs = Compteurs()

    # --- clé ---------------------------------------------------------------

    @staticmethod
    def cle(composantes: dict[str, Any]) -> str:
        return empreinte_canonique(composantes)

    def _chemin(self, cle: str) -> Path:
        if len(cle) != 64 or any(c not in "0123456789abcdef" for c in cle):
            raise ValueError("clé de cache de réponses invalide")
        return self.root / cle[:2] / f"{cle}.json"

    # --- lecture -----------------------------------------------------------

    def lire(self, cle: str) -> EntreeDeCache | None:
        """L'entrée valide et non expirée, ou `None`. Une entrée douteuse est retirée, jamais servie."""
        chemin = self._chemin(cle)
        try:
            brut = chemin.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.compteurs.misses += 1
            return None
        except OSError as exc:
            LOG.warning("cache de réponses illisible (%s) : miss", type(exc).__name__)
            self.compteurs.misses += 1
            return None
        try:
            entree = self._relire(cle, brut)
        except (CacheCorrompu, pydantic.ValidationError, ValueError) as exc:
            # Fail-closed : ce n'est pas une réponse, donc ce n'est pas un hit. L'entrée part, pour
            # que le prochain passage soit un miss reproductible plutôt qu'une erreur récurrente.
            LOG.warning("entrée de cache de réponses écartée (%s)", type(exc).__name__)
            self.compteurs.invalides += 1
            self.compteurs.misses += 1
            self._supprimer(chemin)
            return None
        if entree is None:  # expirée
            self.compteurs.misses += 1
            self._supprimer(chemin)
            return None
        self.compteurs.hits += 1
        return entree

    def _relire(self, cle: str, brut: str) -> EntreeDeCache | None:
        doc = json.loads(brut)
        if not isinstance(doc, dict):
            raise CacheCorrompu("objet JSON attendu")
        if set(doc) != {"schema_version", "cle", "cree_le", "valeur"}:
            raise CacheCorrompu("champs inattendus ou manquants")
        if type(doc["schema_version"]) is not int or doc["schema_version"] != CACHE_SCHEMA_VERSION:
            raise CacheCorrompu("version de schéma inconnue")
        if doc["cle"] != cle:
            raise CacheCorrompu("clé incohérente avec le chemin")
        cree_le = doc["cree_le"]
        if isinstance(cree_le, bool) or not isinstance(cree_le, (int, float)) \
                or not math.isfinite(cree_le):
            raise CacheCorrompu("date de création non finie")
        if time.time() - float(cree_le) > self.ttl_s:
            return None
        valeur = doc["valeur"]
        if not isinstance(valeur, dict) or set(valeur) != {"answer", "trace", "decision_claims"}:
            raise CacheCorrompu("réponse, trace ou claims de décision absents")
        answer = Answer.model_validate(valeur["answer"])
        trace = Trace.model_validate(valeur["trace"])
        claims = [ClaimJugee.model_validate(c) for c in valeur["decision_claims"]]
        # État interne d'AD-6, exclu de toute projection HTTP : il est reposé sur l'objet, comme
        # *vérifier* le pose, pour que la conversation 3.7 scelle le même état sur un hit que sur un
        # miss. Sans lui, un fil ouvert sur une réponse re-servie n'aurait aucune claim à continuer.
        answer._decision_claims = claims
        return EntreeDeCache(answer=answer, trace=trace, decision_claims=claims)

    # --- écriture ----------------------------------------------------------

    def ecrire(self, cle: str, entree: EntreeDeCache) -> None:
        """Écrit l'entrée, puis ramène le magasin sous ses deux bornes. Jamais bloquant pour la requête."""
        chemin = self._chemin(cle)
        payload = json_canonique({
            "schema_version": CACHE_SCHEMA_VERSION,
            "cle": cle,
            "cree_le": time.time(),
            "valeur": {
                "answer": entree.answer.model_dump(mode="json"),
                "trace": entree.trace.model_dump(mode="json"),
                "decision_claims": [c.model_dump(mode="json") for c in entree.decision_claims],
            },
        }) + "\n"
        octets = payload.encode("utf-8")
        if len(octets) > self.max_bytes:
            # Une seule réponse ne peut pas occuper tout le magasin : la garder évincerait toutes les
            # autres à chaque écriture, et le cache cesserait de servir ce pour quoi il existe.
            LOG.warning("réponse trop grande pour le cache (%d octets > %d) : non conservée",
                        len(octets), self.max_bytes)
            return
        try:
            with self._verrou():
                self._ecrire_atomique(chemin, octets)
                self.compteurs.ecritures += 1
                self._borner()
        except OSError as exc:
            # Le cache est un service, pas une dépendance : un disque plein ou en lecture seule fait
            # perdre l'économie, jamais la réponse déjà produite.
            LOG.warning("écriture du cache de réponses abandonnée (%s)", type(exc).__name__)

    def _ecrire_atomique(self, chemin: Path, octets: bytes) -> None:
        chemin.parent.mkdir(parents=True, exist_ok=True)
        fd, temporaire = tempfile.mkstemp(prefix=f".{chemin.name}.", suffix=".tmp",
                                          dir=chemin.parent)
        try:
            with os.fdopen(fd, "wb") as flux:
                flux.write(octets)
                flux.flush()
                os.fsync(flux.fileno())
            os.replace(temporaire, chemin)
        except BaseException:
            try:
                os.unlink(temporaire)
            except FileNotFoundError:
                pass
            raise

    # --- bornes ------------------------------------------------------------

    def _entrees(self) -> list[tuple[float, int, Path]]:
        """`(date de création, taille, chemin)` de chaque entrée, la plus ancienne d'abord.

        La date est celle **écrite dans l'entrée**, pas le `mtime` : un checkout, une copie d'image
        ou un `rsync` réécrivent les `mtime` et feraient évincer au hasard.
        """
        trouvees: list[tuple[float, int, Path]] = []
        for chemin in self.root.rglob("*.json"):
            try:
                doc = json.loads(chemin.read_text(encoding="utf-8"))
                cree_le = float(doc["cree_le"])
                taille = chemin.stat().st_size
            except (OSError, ValueError, KeyError, TypeError):
                # Illisible : elle n'a pas d'âge, donc elle part en premier.
                trouvees.append((float("-inf"), 0, chemin))
                continue
            trouvees.append((cree_le, taille, chemin))
        trouvees.sort(key=lambda t: (t[0], t[2].name))
        return trouvees

    def _borner(self) -> None:
        """Retire les expirées, puis les plus anciennes jusqu'à respecter les deux bornes."""
        entrees = self._entrees()
        maintenant = time.time()
        restantes: list[tuple[float, int, Path]] = []
        for cree_le, taille, chemin in entrees:
            if maintenant - cree_le > self.ttl_s:
                self._supprimer(chemin)
                self.compteurs.evictions += 1
                continue
            restantes.append((cree_le, taille, chemin))
        total = sum(taille for _, taille, _ in restantes)
        index = 0
        while (len(restantes) - index > self.max_entries or total > self.max_bytes) \
                and index < len(restantes):
            _, taille, chemin = restantes[index]
            self._supprimer(chemin)
            self.compteurs.evictions += 1
            total -= taille
            index += 1

    @staticmethod
    def _supprimer(chemin: Path) -> None:
        try:
            chemin.unlink()
        except (FileNotFoundError, OSError):
            pass

    def _verrou(self) -> Any:
        """Verrou exclusif inter-processus sur la fenêtre écriture + éviction.

        Deux instances Cloud Run ne partagent pas ce disque, mais un même conteneur sert
        `--concurrency=2` et le poste de développement fait tourner plusieurs processus sur le même
        dépôt. Sans verrou, deux évictions concurrentes lisent le même état et suppriment deux fois
        plus que la borne ne le demande. L'inode du verrou n'est **jamais** supprimé : le retirer
        pendant qu'un concurrent attend dessus lui donnerait un verrou sur un fichier fantôme.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        return _VerrouFichier(self.root / ".verrou")

    # --- publication -------------------------------------------------------

    def compte_des_entrees(self) -> int:
        try:
            return sum(1 for _ in self.root.rglob("*.json"))
        except OSError:
            return 0


class _VerrouFichier:
    """`flock` exclusif, dégradé en absence de `fcntl` (Windows) : le cache reste correct, seul le
    partage inter-processus n'est plus garanti — et aucun environnement servi n'est dans ce cas."""

    def __init__(self, chemin: Path) -> None:
        self._chemin = chemin
        self._fd: int | None = None

    def __enter__(self) -> _VerrouFichier:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - aucune plateforme servie n'y tombe
            return self
        try:
            self._fd = os.open(self._chemin, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError as exc:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            if exc.errno not in (errno.EACCES, errno.EROFS, errno.ENOSYS):
                raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        finally:
            os.close(self._fd)
            self._fd = None
