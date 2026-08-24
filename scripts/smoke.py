"""AD-12 / FR44 c — Les smoke tests joués sur la révision candidate, **avant** promotion du trafic.

Ce que ce module vérifie, sur le service réellement déployé et non sur le dépôt :

1. `GET /api/v1/sante` — les documents attendus sont **servis** (ni moins, ni d'autres), au
   `gate_profile` que le manifest de ce commit annonce, la `version` publiée est le `sha7` du commit
   qui a déclenché le déploiement, les seuils sont publiés, et **aucune** alerte ne pèse sur le
   service. C'est là que la reprise différée de la story 1.10 se ferme : `ALLOW_UNGATED=true` posé à
   la main sur la configuration du service produit l'alerte `ungated_refuse_en_production`, qu'aucun
   test hors ligne ne pouvait voir — ici elle est rouge.
2. Les **trois surfaces** d'AD-12 (`/`, `/guide/`, `/sinistre/`) répondent 200 — l'origine unique est
   la promesse de l'AD, et une image dont le montage statique casse passerait sinon au vert avec une
   API parfaite et trois pages mortes.
3. `POST /api/v1/chat` — le cas témoin du guide (`server/evals/cases/guide/`), rejoué par HTTP.
4. `POST /api/v1/sinistre` — le cas témoin du sinistre, rejoué de même.

**L'ordre n'est pas un détail : 1 et 2 ne coûtent rien, 3 et 4 sont facturés.** Un écart sur les deux
premières arrête le programme — le déploiement est déjà refusé, et payer deux appels de pipeline pour
le confirmer serait une dépense sans information.

**Pourquoi les deux cas du gate, et pas deux questions écrites ici.** Ce sont les seules attentes du
dépôt qui soient écrites, relues et amarrées à un gate (`cases_hash`, AD-14). Un smoke avec sa propre
question aurait créé un troisième jeu d'attentes que personne ne relit, et qu'aucun `--gate` ne
mesure. Le smoke est plus **large** que le gate sur ce qu'il regarde (le service, son profil, ses
alertes) et plus **étroit** sur l'attente : il exige ce que la matrice de la story 1.11 énumère —
`via`, `found`, au moins une claim `retrouvee`, le `source_hash` du manifest, et pour le sinistre un
verdict parmi les valeurs admissibles du cas. Il ne rejoue pas `expected.fiche_ids` ni
`expected.block_ids` : ce jugement-là appartient au runner d'évals, qui l'a rendu à l'écriture du
gate ; le refaire ici en ferait un second juge, avec sa propre idée de ce qu'est un bon résultat.

**Pourquoi c'est un module Python et non une suite de `curl | jq`.** Les décisions (`verifier_sante`,
`verifier_surfaces`, `verifier_chat`, `verifier_sinistre`) sont **pures** : elles reçoivent un corps
déjà décodé et rendent la liste des écarts. Le transport (`urllib`) est ailleurs. `tests/test_smoke.py`
exerce donc chaque règle sur des charges utiles enregistrées, sans réseau — un smoke écrit en shell
n'est vérifiable que là où il tourne, et le jour où il se trompe, il se trompe en production.

**Ce que le module refuse plutôt que de le deviner.** Tout ce qu'il lit dans le dépôt — manifest, cas
témoins — est lu **strictement** : un YAML dont la racine n'est pas une table, un `expected.verdict`
écrit en scalaire au lieu d'une liste, un `found` absent, un `doc_id` que le manifest ne sert pas sont
des refus **nommés**, jamais une valeur devinée. Un `verdict: sous_conditions` scalaire donnerait
`tuple("sous_conditions")`, c'est-à-dire un tuple de caractères : tout verdict réel en serait absent
et tout déploiement sain repartirait rouge, en accusant le système.

**AD-16** : un écart fait sortir en 1, le workflow échoue, et le trafic ne bouge pas. Aucune valeur
de repli, aucune promotion « quand même ».

Usage :

    uv run python scripts/smoke.py --base-url https://candidat---foyer-retour-xxxx-ew.a.run.app \\
                                   --version 1a2b3c4
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# `python scripts/smoke.py` met `scripts/` en tête de `sys.path`, pas la racine du dépôt : sans cette
# ligne, l'import de `server.app.config` échoue alors que `python -m scripts.smoke` marcherait. Le
# workflow et l'AC écrivent la première forme ; le script s'y plie plutôt que d'imposer la seconde.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app.config import REPO_ROOT, Settings  # noqa: E402 — après la ligne de `sys.path`

VIA_ATTENDU = "api/v1"

# AD-12 : « FastAPI sert `/`, `/guide/`, `/sinistre/`, `/api/v1/*` » — une seule origine. Les trois
# pages sont des fichiers copiés dans l'image (`Dockerfile` : `COPY web`, `COPY tools`) et montés par
# le serveur ; rien dans les trois vérifications d'API ne les touche.
SURFACES = ("/", "/guide/", "/sinistre/")

# Les trois valeurs qui suivent sont la **patience d'un client de CI**, et non des seuils du système
# (Convention Seuils) : elles décrivent combien de temps ce programme accepte d'attendre un serveur,
# pas comment le serveur se règle. Les bornes du système, elles, sont lues sur `/sante`
# (`_timeout_pipeline`).
TIMEOUT_SANTE_S = 30.0
# La révision candidate part en `--min-instances=0` : la toute première requête paie le démarrage à
# froid (uvicorn, PyMuPDF, le chargement des deux corpus). Un démarrage lent n'est pas un déploiement
# malade, et le faire échouer serait un faux négatif coûteux — d'où une reprise **bornée**, et sur la
# seule sonde de santé : les appels de pipeline sont facturés, ils n'ont droit qu'à un essai.
ESSAIS_SANTE = 3
DELAI_REPRISE_S = 5.0


class ErreurTransport(RuntimeError):
    """Le service n'a pas répondu, ou le dépôt est illisible : un échec de smoke, jamais un repli."""


# --- les attendus, tirés du dépôt (jamais écrits ici) --------------------------------------------

@dataclass(frozen=True)
class CasTemoin:
    """Un cas du golden set, réduit à ce qu'un appel HTTP en consomme."""

    id: str
    doc_id: str
    question: str
    lang: str | None = None
    profil: dict[str, Any] = field(default_factory=dict)
    # AD-14 admet un `historique` sur un cas du guide, et `evals/run.py` le transmet au pipeline.
    # L'omettre ferait rejouer « le cas du gate » sans ce qui le définit (revue 1.11).
    historique: tuple[dict[str, Any], ...] = ()
    faits: dict[str, Any] = field(default_factory=dict)
    found_attendu: bool = True
    verdicts_admissibles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Attendus:
    """Ce que le dépôt dit du service qu'il vient de construire."""

    documents: tuple[str, ...]
    gate_profile: str
    # Somme des `gate.cases` des documents servis — c'est ce que `/api/v1/sante` publie en
    # `gate_cases`. Sans ce compte, un gate réécrit sur moins de cas que le manifest committé passait
    # le smoke sans un mot : le profil restait `vertical`, seul le nombre avait fondu (revue 1.11).
    gate_cases: int
    source_hash: dict[str, str]
    cas_guide: CasTemoin
    cas_sinistre: CasTemoin


def charger_attendus(*, racine: Path | None = None) -> Attendus:
    """Lit `data/manifest.json` et les deux cas témoins — les seules sources d'attentes du dépôt."""
    racine = racine or REPO_ROOT
    reglages = Settings(_env_file=None)
    fichier = racine / "data" / "manifest.json"
    try:
        manifest = json.loads(fichier.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ErreurTransport(f"{fichier} illisible : {e}") from e
    if not isinstance(manifest, dict):
        raise ErreurTransport(f"{fichier} : la racine doit être une table de documents")

    documents = tuple(sorted(doc_id for doc_id, e in manifest.items()
                             if isinstance(e, dict) and e.get("status") == "servi"))
    if not documents:
        raise ErreurTransport("aucun document `servi` dans data/manifest.json : rien à vérifier")

    # L'AC nomme **littéralement** les deux documents. Dériver l'attendu du seul manifest le rendrait
    # complaisant : le jour où une réingestion cesserait de marquer l'un des deux `servi`, l'attendu
    # descendrait avec lui et le smoke resterait vert sur un service amputé. Les deux `doc_id` de
    # `config.py` — la même autorité que celle qu'utilise le serveur — sont donc exigés ici, à la
    # lecture, et leur absence est un refus, pas un attendu plus petit.
    for role, doc_id in (("guide", reglages.guide_doc_id), ("sinistre", reglages.sinistre_doc_id)):
        if doc_id not in documents:
            raise ErreurTransport(
                f"le document {role} ({doc_id!r}) n'est pas `servi` dans data/manifest.json — "
                f"l'AC de la story exige que la révision serve les deux, et un attendu qui suivrait "
                f"le manifest sans broncher validerait un service amputé")

    profils: set[str | None] = set()
    for doc_id in documents:
        gate = manifest[doc_id].get("gate")
        # Une entrée peut porter explicitement `"gate": null` (document ingéré, jamais mesuré) :
        # `.get("gate", {}).get(...)` levait alors sur `None`. C'est un refus, pas une exception nue.
        profils.add(gate.get("profile") if isinstance(gate, dict) else None)
    if len(profils) != 1 or None in profils:
        # AD-11 : `/sante` publie `gate_profile: null` dès qu'un document servi n'a pas de gate. Le
        # smoke ne saurait pas quoi exiger, et exiger `null` reviendrait à valider un déploiement de
        # documents non mesurés. On refuse au lieu de choisir.
        raise ErreurTransport(
            f"les documents servis n'annoncent pas un seul profil de gate : {sorted(map(str, profils))} "
            f"— relancer `uv run python -m server.evals.run --gate {{doc_id}} --profile vertical`")
    gate_profile = profils.pop()
    assert isinstance(gate_profile, str)

    # `etat.py::gate_cases` somme les `cases` des documents **servis** ; l'attendu se calcule de la
    # même façon, sur le manifest de ce commit. Un `cases` absent ou non entier est un refus : le
    # deviner à 0 ou à 1 reviendrait à inventer l'attente que le smoke est censé lire.
    gate_cases = 0
    for doc_id in documents:
        gate = manifest[doc_id].get("gate")
        cas = gate.get("cases") if isinstance(gate, dict) else None
        if not isinstance(cas, int) or isinstance(cas, bool) or cas < 0:
            raise ErreurTransport(f"data/manifest.json : {doc_id} a un `gate.cases` illisible "
                                  f"({cas!r}) — relancer `--gate {doc_id} --profile {gate_profile}`")
        gate_cases += cas

    hashes: dict[str, str] = {}
    for doc_id in documents:
        valeur = manifest[doc_id].get("source_hash")
        if not isinstance(valeur, str) or not valeur:
            raise ErreurTransport(f"data/manifest.json : {doc_id} n'a pas de `source_hash` lisible")
        hashes[doc_id] = valeur

    cases = racine / "server" / "evals" / "cases"
    return Attendus(
        documents=documents,
        gate_profile=gate_profile,
        gate_cases=gate_cases,
        source_hash=hashes,
        cas_guide=_lire_cas(cases / "guide", reglages.guide_doc_id),
        cas_sinistre=_lire_cas(cases / "sinistre", reglages.sinistre_doc_id))


def _exiger(condition: bool, message: str) -> None:
    if not condition:
        raise ErreurTransport(message)


def _lire_cas(dossier: Path, doc_id: str) -> CasTemoin:
    """Le cas unique de la suite, lu **strictement**.

    Deux cas, ou zéro, sont un refus : le smoke rejoue le cas témoin de la suite, il n'en désigne pas
    un parmi plusieurs. Et chaque champ consommé est contrôlé dans sa forme plutôt que converti : le
    contre-exemple qui a motivé cette dureté est `expected.verdict: sous_conditions` écrit en scalaire
    — `tuple(...)` en aurait fait un tuple de onze caractères, dans lequel aucun verdict réel ne
    figure, et **tout** déploiement sain serait reparti rouge. `server/evals/run.py` valide ces mêmes
    fichiers avec pydantic ; ici on ne peut pas l'importer (couche `evals`, jamais chargée par un
    outil d'exploitation), alors on refuse ce qu'on ne sait pas lire.
    """
    if not dossier.is_dir():
        raise ErreurTransport(f"{dossier} : dossier de cas témoins absent")
    fichiers = sorted(dossier.glob("*.yaml"))
    _exiger(len(fichiers) == 1,
            f"{dossier} contient {len(fichiers)} cas : le smoke rejoue le cas témoin de la suite, "
            f"il n'en désigne pas un parmi plusieurs (élargir le smoke, ou le laisser refuser)")
    fichier = fichiers[0]
    try:
        brut = yaml.safe_load(fichier.read_text("utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ErreurTransport(f"{fichier} illisible : {e}") from e
    _exiger(isinstance(brut, dict), f"{fichier} : la racine du cas doit être une table YAML")

    _exiger(isinstance(brut.get("id"), str) and bool(brut["id"].strip()),
            f"{fichier} : `id` doit être une chaîne non vide")
    _exiger(isinstance(brut.get("question"), str) and bool(brut["question"].strip()),
            f"{fichier} : `question` doit être une chaîne non vide")

    attendu = brut.get("expected")
    _exiger(isinstance(attendu, dict), f"{fichier} : `expected` doit être une table")
    _exiger(isinstance(attendu.get("found"), bool),
            f"{fichier} : `expected.found` doit être un booléen (absent ou `null`, l'attente du cas "
            f"deviendrait celle du smoke)")
    verdicts = attendu.get("verdict", [])
    _exiger(isinstance(verdicts, list) and all(isinstance(v, str) for v in verdicts),
            f"{fichier} : `expected.verdict` doit être une **liste** de valeurs admissibles "
            f"(un scalaire serait lu caractère par caractère)")

    lang = brut.get("lang")
    _exiger(lang is None or isinstance(lang, str), f"{fichier} : `lang` doit être une chaîne ou `null`")
    profil = brut.get("profil") or {}
    _exiger(isinstance(profil, dict), f"{fichier} : `profil` doit être une table")
    faits = brut.get("faits") or {}
    _exiger(isinstance(faits, dict), f"{fichier} : `faits` doit être une table")
    historique = brut.get("historique") or []
    _exiger(isinstance(historique, list) and all(isinstance(t, dict) for t in historique),
            f"{fichier} : `historique` doit être une liste de tours")

    return CasTemoin(
        id=brut["id"],
        doc_id=doc_id,
        question=brut["question"],
        lang=lang,
        profil=profil,
        historique=tuple(historique),
        faits=faits,
        found_attendu=attendu["found"],
        verdicts_admissibles=tuple(verdicts))


# --- les décisions : pures, testées hors ligne ---------------------------------------------------

_ABSENT = object()


def _lire(corps: Any, *chemin: str) -> Any:
    """Descend un chemin de clés, en rendant `_ABSENT` dès qu'un maillon n'est pas un dictionnaire.

    Lecture **stricte** : une clé manquante ne vaut pas `None`, `False` ni `[]`. Un corps amputé doit
    produire un écart nommé, pas une comparaison qui réussit par accident (AD-16).
    """
    courant = corps
    for cle in chemin:
        if not isinstance(courant, dict) or cle not in courant:
            return _ABSENT
        courant = courant[cle]
    return courant


def _manquant(chemin: str) -> str:
    return f"{chemin} absent de la réponse (corps illisible)"


# Les deux seuils que `_timeout_pipeline` consomme. Ils sont **exigés** de `/sante` : sans eux, la
# patience du smoke retomberait sur `TIMEOUT_SANTE_S` (30 s), c'est-à-dire **sous** la deadline du
# serveur (55 s), et le smoke couperait une requête parfaitement saine en accusant le pipeline.
SEUILS_EXIGES = ("deadline_s", "client_abort_margin_s")


def verifier_sante(corps: Any, *, documents: tuple[str, ...], gate_profile: str,
                   gate_cases: int, version: str) -> list[str]:
    """`GET /api/v1/sante` : ce que la révision candidate sert, et à quel niveau de validation."""
    ecarts: list[str] = []

    ok = _lire(corps, "ok")
    if ok is _ABSENT:
        ecarts.append(_manquant("ok"))
    elif ok is not True:
        ecarts.append(f"ok vaut {ok!r} : le document du guide n'est pas servi")

    servis = _lire(corps, "documents_servis")
    if not isinstance(servis, list):
        ecarts.append(_manquant("documents_servis"))
    else:
        absents = [d for d in documents if d not in servis]
        if absents:
            ecarts.append(f"documents_servis={sorted(servis)!r} : il manque {absents!r} "
                          f"(attendus d'après data/manifest.json et config.py)")
        # Le sens inverse compte autant : un document que la révision sert et que le manifest de ce
        # commit ne connaît pas dit que l'image et le dépôt ont divergé — un `data/` resté d'une
        # construction antérieure, ou un manifest édité sans réingestion.
        inattendus = [d for d in servis if d not in documents]
        if inattendus:
            ecarts.append(f"documents_servis porte {inattendus!r}, absent(s) du manifest de ce "
                          f"commit : l'image et le dépôt ont divergé")

    profil = _lire(corps, "gate_profile")
    if profil is _ABSENT:
        ecarts.append(_manquant("gate_profile"))
    elif profil != gate_profile:
        ecarts.append(f"gate_profile={profil!r}, attendu {gate_profile!r} : la révision ne sert pas "
                      f"le corpus mesuré par ce commit")

    # Le profil seul ne dit pas **combien** de cas l'ont établi : un gate réécrit sur un cas de moins
    # garde son nom et perd sa substance, et `/` afficherait « vertical — N cas » avec un N que
    # personne ne relit. `gate_countersigned`, en revanche, n'est **pas** exigé ici : le dépôt sait
    # qu'il est faux (contresignature due à Lancelot), et l'exiger refuserait tout déploiement.
    cases = _lire(corps, "gate_cases")
    if cases is _ABSENT:
        ecarts.append(_manquant("gate_cases"))
    elif cases != gate_cases:
        ecarts.append(f"gate_cases={cases!r}, attendu {gate_cases!r} d'après data/manifest.json : "
                      f"le gate servi ne repose pas sur le même nombre de cas que ce commit")

    # AD-11 : `version` est le sha7 de la révision déployée. Sans ce contrôle, le smoke mesurerait une
    # révision qu'il n'a pas construite — une image plus ancienne restée en service, ou une promotion
    # qui a raté, et les deux passeraient au vert.
    lue = _lire(corps, "version")
    if lue is _ABSENT:
        ecarts.append(_manquant("version"))
    elif lue != version:
        ecarts.append(f"version={lue!r}, attendu {version!r} : la révision sondée n'est pas celle "
                      f"que ce commit a construite")

    # `SanteResponse.thresholds` a un `default_factory=dict` : un corps sans seuils est un corps
    # valide pour pydantic, et c'est justement pourquoi le contrôle doit être ici. La patience des
    # deux appels de pipeline en est tirée (`_timeout_pipeline`) ; les taire, c'est faire couper le
    # smoke avant le serveur.
    seuils = _lire(corps, "thresholds")
    if not isinstance(seuils, dict) or not seuils:
        ecarts.append("thresholds absent ou vide : le smoke y lit la patience qu'il accorde aux "
                      "appels de pipeline, il ne la réinvente pas")
    else:
        for nom in SEUILS_EXIGES:
            if not isinstance(seuils.get(nom), (int, float)) or isinstance(seuils.get(nom), bool):
                ecarts.append(f"thresholds.{nom}={seuils.get(nom)!r} : attendu un nombre — sans lui "
                              f"le smoke couperait la requête avant la deadline du serveur")

    # AD-16 / reprise différée de 1.10 : la dérogation `ALLOW_UNGATED` armée sur la configuration du
    # service, un gate périmé, une source absente — tout cela se lit ici, et rien ne doit être promu
    # avec. Le smoke n'a pas de liste d'alertes « acceptables » : une alerte est un écart.
    alertes = _lire(corps, "alerts")
    if not isinstance(alertes, list):
        ecarts.append(_manquant("alerts"))
    elif alertes:
        for a in alertes:
            doc = a.get("doc_id", "?") if isinstance(a, dict) else "?"
            nom = a.get("alerte", a) if isinstance(a, dict) else a
            ecarts.append(f"alerte sur le service : {doc} → {nom}")

    return ecarts


def verifier_surfaces(statuts: dict[str, int]) -> list[str]:
    """Les trois pages d'AD-12 répondent-elles ? — leur existence, rien de leur contenu.

    Ce que ce contrôle attrape et qu'aucun autre ne voit : un `COPY web` ou `COPY tools` disparu du
    `Dockerfile`, un montage `StaticFiles` cassé, un chemin renommé. L'API répondrait parfaitement et
    les trois smokes seraient verts sur un service dont **toutes** les pages sont mortes. Ce qu'il ne
    contrôle **pas**, et volontairement : ce que les pages affichent. Le rendu est déjà tenu par les
    130 cas de front hors ligne et par le tour navigateur consigné dans `docs/tests-live.md` ; un
    smoke qui lirait du HTML deviendrait un troisième juge de l'affichage.
    """
    ecarts: list[str] = []
    for chemin in SURFACES:
        code = statuts.get(chemin)
        if code is None:
            ecarts.append(f"surface {chemin} non sondée")
        elif code != 200:
            ecarts.append(f"surface {chemin} → HTTP {code} : l'origine unique d'AD-12 sert cette "
                          f"page depuis la même image, elle doit répondre")
    return ecarts


def _ecarts_de_reponse(corps: Any, *, doc_id: str, source_hash: str, found_attendu: bool,
                       etiquette: str) -> list[str]:
    """Ce que les deux pipelines doivent tenir en commun (AD-12 : `via`, claim retrouvée, empreinte)."""
    ecarts: list[str] = []

    via = _lire(corps, "via")
    if via is _ABSENT:
        ecarts.append(_manquant("via"))
    elif via != VIA_ATTENDU:
        ecarts.append(f"{etiquette} : via={via!r}, attendu {VIA_ATTENDU!r}")

    found = _lire(corps, "answer", "found")
    if found is _ABSENT:
        ecarts.append(_manquant("answer.found"))
    elif bool(found) is not found_attendu:
        ecarts.append(f"{etiquette} : answer.found={found!r}, attendu {found_attendu!r} "
                      f"(attente du cas témoin)")

    claims = _lire(corps, "answer", "claims")
    if not isinstance(claims, list):
        ecarts.append(_manquant("answer.claims"))
    else:
        retrouvees = [c for c in claims
                      if isinstance(c, dict) and _lire(c, "status", "retrouvee") is True]
        if not retrouvees:
            ecarts.append(f"{etiquette} : aucune claim `retrouvee` parmi {len(claims)} claim(s) — "
                          f"une réponse sans citation retrouvée n'est pas une réponse sourcée (AD-3)")

    hashes = _lire(corps, "trace", "source_hash")
    if not isinstance(hashes, dict):
        ecarts.append(_manquant("trace.source_hash"))
    elif hashes.get(doc_id) != source_hash:
        ecarts.append(f"{etiquette} : trace.source_hash[{doc_id!r}]={hashes.get(doc_id)!r}, "
                      f"attendu {source_hash!r} — la révision sert une autre source que ce commit")

    return ecarts


def verifier_chat(corps: Any, *, cas: CasTemoin, source_hash: str) -> list[str]:
    """`POST /api/v1/chat` : le cas témoin du guide, rejoué par HTTP."""
    return _ecarts_de_reponse(corps, doc_id=cas.doc_id, source_hash=source_hash,
                              found_attendu=cas.found_attendu, etiquette=f"chat/{cas.id}")


def verifier_sinistre(corps: Any, *, cas: CasTemoin, source_hash: str) -> list[str]:
    """`POST /api/v1/sinistre` : le cas de la bougie, verdict compris."""
    ecarts = _ecarts_de_reponse(corps, doc_id=cas.doc_id, source_hash=source_hash,
                                found_attendu=cas.found_attendu, etiquette=f"sinistre/{cas.id}")
    if cas.verdicts_admissibles:
        # `answer.verdict` est le `Verdict` d'AD-6 — un objet (`value`, `reason`, `missing`,
        # `ask_client`, `escalate`), et non une chaîne. Le comparer entier à une liste de valeurs
        # échouerait toujours, en accusant le système à tort ; c'est `value` que le cas témoin borne,
        # comme `evals/run.py::juger` le fait.
        verdict = _lire(corps, "answer", "verdict", "value")
        if verdict is _ABSENT:
            ecarts.append(_manquant("answer.verdict.value"))
        elif verdict not in cas.verdicts_admissibles:
            ecarts.append(f"sinistre/{cas.id} : verdict={verdict!r}, hors des valeurs admissibles "
                          f"du cas témoin {list(cas.verdicts_admissibles)!r}")
    return ecarts


# --- le transport ---------------------------------------------------------------------------------

def appeler(url: str, *, corps: dict[str, Any] | None = None, timeout: float) -> Any:
    """Un appel, une réponse JSON décodée. Toute autre issue est une `ErreurTransport`."""
    donnees = None if corps is None else json.dumps(corps).encode("utf-8")
    entetes = {"Accept": "application/json"}
    if donnees is not None:
        entetes["Content-Type"] = "application/json"
    requete = urllib.request.Request(url, data=donnees, headers=entetes,
                                     method="GET" if donnees is None else "POST")
    try:
        # L'URL vient de `--base-url`, c'est-à-dire du workflow : ni entrée utilisateur, ni schéma
        # arbitraire. (Pas de `noqa: S310` : `S` n'est pas dans `select`, et une annotation qui ne
        # supprime rien laisse croire à une règle active — revue 1.11.)
        with urllib.request.urlopen(requete, timeout=timeout) as reponse:
            brut = reponse.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # AD-11 : toute sortie de pipeline est un 200. Un 4xx/5xx est donc un échec de service, et son
        # corps est l'enveloppe d'AD-16 — on la recopie, elle porte le `request_id` qui corrèle au log.
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ErreurTransport(f"{url} → HTTP {e.code} : {detail}") from e
    # `ValueError` : `urlopen` le lève sur une URL sans schéma (`--base-url candidat---…`, une faute
    # de frappe plausible dans un workflow) ; `HTTPException` : réponse tronquée ou en-têtes
    # malformés. Les laisser passer donnerait une trace nue là où AD-16 attend un échec **nommé**.
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
        raise ErreurTransport(f"{url} injoignable : {type(e).__name__}: {e}") from e
    try:
        return json.loads(brut)
    except json.JSONDecodeError as e:
        raise ErreurTransport(f"{url} → réponse non-JSON ({brut[:200]!r})") from e


def sonder(url: str, *, timeout: float) -> int:
    """Le code HTTP d'une page, sans lire son contenu.

    Un 404 est ici une **information**, pas une panne de transport : il devient un écart nommé par
    `verifier_surfaces`, avec les autres. Seul un serveur injoignable reste une `ErreurTransport`.
    """
    requete = urllib.request.Request(url, headers={"Accept": "text/html"}, method="GET")
    try:
        with urllib.request.urlopen(requete, timeout=timeout) as reponse:  # URL du workflow
            reponse.read(1)
            return int(reponse.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
        raise ErreurTransport(f"{url} injoignable : {type(e).__name__}: {e}") from e


def _patienter(secondes: float) -> None:  # pragma: no cover — remplacé dans les tests
    time.sleep(secondes)


def appeler_avec_reprise(url: str, *, timeout: float, essais: int = ESSAIS_SANTE,
                         delai_s: float = DELAI_REPRISE_S) -> Any:
    """La sonde de santé, et **elle seule**, a droit à une seconde chance.

    `--min-instances=0` veut dire qu'aucune instance ne tourne quand le smoke arrive : la première
    requête paie le démarrage du conteneur, et un démarrage lent ferait échouer un déploiement
    parfaitement sain. La reprise est bornée — trois essais, quelques secondes — et ne s'applique
    jamais aux appels de pipeline : ceux-là sont facturés, et réessayer un appel modèle qui a échoué
    reviendrait à payer deux fois pour cacher un symptôme (AD-16 : l'échec est terminal).
    """
    derniere: ErreurTransport | None = None
    for essai in range(1, essais + 1):
        try:
            return appeler(url, timeout=timeout)
        except ErreurTransport as e:
            derniere = e
            if essai < essais:
                print(f"      · essai {essai}/{essais} sur {url} : {e} — nouvelle tentative dans "
                      f"{delai_s:g} s (démarrage à froid probable)")
                _patienter(delai_s)
    assert derniere is not None
    raise ErreurTransport(f"après {essais} essais : {derniere}")


def _timeout_pipeline(sante: Any) -> float:
    """La patience du smoke vient du **serveur** : `deadline_s` + la marge que `/sante` publie.

    Le front du guide fait déjà exactement cela (README, « il borne son attente »). Recopier un
    nombre ici en ferait un second texte faisant autorité sur la même durée — ce que la Convention
    Seuils interdit.

    Le repli sur `TIMEOUT_SANTE_S` n'est **pas** un garde-fou : 30 s est *sous* la deadline du serveur,
    et s'y rabattre ferait couper une requête saine. C'est pourquoi `verifier_sante` **exige**
    `thresholds` et ses deux entrées, et pourquoi `main` s'arrête avant tout appel de pipeline dès que
    la santé porte un écart : quand cette fonction est appelée, les seuils ont déjà été constatés
    présents et numériques. Le repli ne subsiste que pour qu'un appel direct de la fonction ne lève
    pas — il est exercé par les tests, jamais par le programme.
    """
    seuils = _lire(sante, "thresholds")
    if not isinstance(seuils, dict):
        return TIMEOUT_SANTE_S
    deadline = seuils.get("deadline_s")
    marge = seuils.get("client_abort_margin_s", 0)
    if not isinstance(deadline, (int, float)) or not isinstance(marge, (int, float)):
        return TIMEOUT_SANTE_S
    return float(deadline) + float(marge)


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(
        prog="scripts/smoke.py",
        description="Smoke tests d'AD-12, joués sur une révision Cloud Run avant promotion du trafic.")
    parseur.add_argument("--base-url", required=True,
                         help="URL de la révision candidate (tag `candidat`), sans barre finale")
    parseur.add_argument("--version", required=True,
                         help="sha7 du commit déployé : `/api/v1/sante` doit publier exactement celui-là")
    parseur.add_argument("--racine", type=Path, default=None,
                         help="racine du dépôt d'où lire manifest et cas témoins (défaut : ce dépôt)")
    args = parseur.parse_args(argv)

    base = args.base_url.rstrip("/")
    try:
        attendus = charger_attendus(racine=args.racine)
    except ErreurTransport as e:
        print(f"ÉCHEC · attendus illisibles dans le dépôt : {e}", file=sys.stderr)
        return 1

    resultats: list[tuple[str, list[str]]] = []
    try:
        sante = appeler_avec_reprise(f"{base}/api/v1/sante", timeout=TIMEOUT_SANTE_S)
        resultats.append(("sante", verifier_sante(
            sante, documents=attendus.documents, gate_profile=attendus.gate_profile,
            gate_cases=attendus.gate_cases, version=args.version)))

        statuts = {chemin: sonder(f"{base}{chemin}", timeout=TIMEOUT_SANTE_S)
                   for chemin in SURFACES}
        resultats.append(("surfaces", verifier_surfaces(statuts)))

        # Les deux vérifications ci-dessus ne coûtent rien ; les deux suivantes appellent un modèle.
        # Un déploiement déjà refusé n'a pas besoin d'être confirmé à 0,08 € et 40 s.
        if any(ecarts for _, ecarts in resultats):
            total = _rapporter(resultats)
            print(f"ÉCHEC · {total} écart(s) avant tout appel facturé : chat et sinistre ne sont pas "
                  f"joués, le trafic ne doit pas être promu (AD-12, AD-16).", file=sys.stderr)
            return 1

        patience = _timeout_pipeline(sante)
        cas = attendus.cas_guide
        chat = appeler(f"{base}/api/v1/chat", timeout=patience, corps=corps_chat(cas))
        resultats.append(("chat", verifier_chat(
            chat, cas=cas, source_hash=attendus.source_hash[cas.doc_id])))

        cas = attendus.cas_sinistre
        sin = appeler(f"{base}/api/v1/sinistre", timeout=patience, corps=corps_sinistre(cas))
        resultats.append(("sinistre", verifier_sinistre(
            sin, cas=cas, source_hash=attendus.source_hash[cas.doc_id])))
    except ErreurTransport as e:
        _rapporter(resultats)
        print(f"ÉCHEC · transport : {e} — le trafic ne doit pas être promu (AD-12, AD-16).",
              file=sys.stderr)
        return 1

    total = _rapporter(resultats)
    if total:
        print(f"ÉCHEC · {total} écart(s) : le trafic ne doit pas être promu (AD-12, AD-16).",
              file=sys.stderr)
        return 1
    print(f"ok    · les quatre vérifications passent sur {base} (version {args.version})")
    return 0


def corps_chat(cas: CasTemoin) -> dict[str, Any]:
    """Le corps de `POST /api/v1/chat` — les champs d'AD-11, tels que le cas témoin les porte."""
    return {"question": cas.question, "profil": cas.profil,
            "historique": [dict(t) for t in cas.historique], "lang": cas.lang}


def corps_sinistre(cas: CasTemoin) -> dict[str, Any]:
    """Le corps de `POST /api/v1/sinistre` — `SinistreRequest` refuse tout champ en trop (AD-11)."""
    return {"doc_id": cas.doc_id, "question": cas.question, "faits": cas.faits, "lang": cas.lang}


def _rapporter(resultats: list[tuple[str, list[str]]]) -> int:
    """Écrit chaque vérification et son détail ; rend le nombre total d'écarts."""
    total = 0
    for nom, ecarts in resultats:
        if not ecarts:
            print(f"ok    · {nom}")
            continue
        total += len(ecarts)
        print(f"ÉCART · {nom}")
        for e in ecarts:
            print(f"        - {e}")
    return total


if __name__ == "__main__":  # pragma: no cover — le point d'entrée du workflow
    sys.exit(main())
