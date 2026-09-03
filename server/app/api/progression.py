"""Story 5.6 (L1) — `POST …/progression` : la même réponse, dite pendant qu'elle se calcule.

Une question au contrat tient entre une et trois minutes. Le front n'a rien à montrer pendant ce
temps : ni ce qui se passe, ni combien il en reste. Un compteur qui tourne ment ; ce qu'il faut est
**ce que le serveur fait**, au moment où il le fait.

Ces routes ne sont pas un second pipeline. Elles appellent **exactement** la fonction que la route
classique appelle, avec le même corps de requête, le même limiteur, le même budget, la même deadline
et le même plafond de coût, et l'événement `resultat` porte **exactement** le JSON que la route
classique aurait rendu — le témoin hermétique de chaque route compare les deux sur la même fixture.
Ce qui s'ajoute est un canal d'observation : deux rappels que le pipeline reçoit à `None` par défaut
(`on_etape`, `on_tour`), qui ne décident de rien et ne peuvent rien annuler.

**Ce qui ne passe jamais dans un événement** (AD-10, AD-15) : ni la question, ni les faits déclarés,
ni un texte de bloc, ni un morceau de prompt, ni une clé. Un événement d'étape porte un nom pris dans
une table close, un rang, un total, un temps écoulé et un libellé écrit ici — rien qui vienne de la
requête ou du modèle. Le `resultat`, lui, est la réponse elle-même, celle que la route classique
publie déjà.

**Les erreurs.** Le limiteur (AD-13), les bornes du schéma et les deux refus 400 du sinistre
s'appliquent **avant** que le flux ne commence : ils sortent en vraie erreur HTTP, avec l'enveloppe
d'AD-16, comme sur la route classique. Une fois le flux ouvert, le statut est parti — un échec
terminal du pipeline devient donc un événement `erreur` dont le corps est celui qu'`errors.py`
aurait sérialisé, accompagné du statut sémantique qu'il aurait rendu. Le corps et le statut sont
littéralement lus sur la réponse que le gestionnaire d'AD-16 construit : il n'y a pas deux
enveloppes d'erreur dans ce service.

**Le keep-alive.** Un flux SSE muet est fermé par l'infrastructure ; la navigation peut tenir plus
d'une minute sur un seul tour d'outils. Un commentaire SSE (`:` seul, ignoré par tout client) part
donc toutes les `sse_keepalive_s` secondes tant qu'aucun événement n'arrive.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from server.app.api.errors import gestionnaire_pipeline, reponse_interne
from server.app.config import Settings
from server.app.domain.errors import PipelineError

MEDIA_TYPE = "text/event-stream"

# Les cinq étapes de la chaîne, dans l'ordre, telles que le front les nomme. La clé est le nom que
# les pipelines emploient (`StepTrace.name`), la valeur le nom **public** : *retrouver* s'appelle
# désormais *naviguer* pour qui regarde, parce que c'est ce que l'étape fait depuis l'amendement
# AD-1 du 03/09/2026 — elle ne retrouve plus, le modèle navigue. Un nom hors table n'émet rien : le
# court-circuit « zéro hit » du guide passe par le même contrôle de deadline sans être une étape.
ETAPES_PUBLIQUES: dict[str, str] = {
    "comprendre": "comprendre",
    "retrouver": "naviguer",
    "rediger": "rediger",
    "verifier": "verifier",
    "restituer": "restituer",
}
ORDRE: tuple[str, ...] = ("comprendre", "naviguer", "rediger", "verifier", "restituer")

# Ce que chaque étape dit à la personne qui attend, à la première personne et au présent. Le seul
# libellé qui diffère d'un pipeline à l'autre est celui de la navigation : on ne lit pas un contrat
# comme on lit des fiches, et le dire faux serait pire que ne rien dire.
LIBELLES: dict[str, dict[str, str]] = {
    "sinistre": {
        "comprendre": "Je lis votre description",
        "naviguer": "Je lis le contrat",
        "rediger": "J'écris la réponse",
        "verifier": "Je vérifie chaque citation",
        "restituer": "Je compose la réponse",
    },
    "guide": {
        "comprendre": "Je lis votre question",
        "naviguer": "Je lis les fiches du guide",
        "rediger": "J'écris la réponse",
        "verifier": "Je vérifie chaque citation",
        "restituer": "Je compose la réponse",
    },
}

# Les deux événements qui ferment le flux. Il n'en existe pas de troisième : un flux qui s'arrête
# sans l'un des deux est une connexion coupée, et le client doit pouvoir les distinguer.
TERMINAUX = frozenset({"resultat", "erreur"})


def _sse(nom: str, donnees: Any) -> bytes:
    """Un événement SSE : `event:` puis `data:` sur une seule ligne JSON, puis la ligne vide."""
    charge = json.dumps(donnees, ensure_ascii=False)
    return f"event: {nom}\ndata: {charge}\n\n".encode()


class Progression:
    """La file d'événements d'une requête, et les deux rappels que le pipeline reçoit.

    Les rappels sont appelés depuis la coroutine du pipeline, sur la **même** boucle que le
    générateur qui vide la file : `put_nowait` sur une file non bornée ne peut ni bloquer ni lever,
    et c'est ce qui garantit qu'observer ne ralentit ni ne casse jamais le calcul observé.
    """

    def __init__(self, pipeline: str) -> None:
        self.pipeline = pipeline
        self._debut = time.monotonic()
        self.evenements: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    def _ms(self) -> int:
        return int((time.monotonic() - self._debut) * 1000)

    def on_etape(self, nom: str) -> None:
        public = ETAPES_PUBLIQUES.get(nom)
        if public is None:
            return
        self.evenements.put_nowait(("etape", {
            "nom": public, "rang": ORDRE.index(public) + 1, "total": len(ORDRE),
            "ms_ecoule": self._ms(), "libelle": LIBELLES[self.pipeline][public]}))

    def on_tour(self, tour: int) -> None:
        """Le tour d'outils qui commence, pendant *naviguer* — un entier, jamais ce qui est lu."""
        self.evenements.put_nowait(("tour", {"tour": tour, "ms_ecoule": self._ms()}))

    def poser(self, nom: str, donnees: dict[str, Any]) -> None:
        self.evenements.put_nowait((nom, donnees))


async def _terminal(request: Request, produire: Callable[[], Awaitable[Response]]) -> tuple[str, Any]:
    """Le résultat de la route classique, ou son erreur — dans les deux cas, son JSON exact.

    `produire` est la fonction que la route classique appelle, sans rien de plus : le JSON de
    `resultat` est celui de sa réponse, relu depuis les octets qu'elle aurait mis sur le réseau. Une
    `PipelineError` repasse par le gestionnaire d'AD-16 — le même, pas une copie — pour que
    l'enveloppe, le message tu d'un `internal` et la ligne de journal soient les mêmes ici et là.
    """
    try:
        reponse = await produire()
    except PipelineError as exc:
        enveloppe = await gestionnaire_pipeline(request, exc)
        return "erreur", {"status": enveloppe.status_code, "corps": _corps(enveloppe)}
    except Exception:  # noqa: BLE001 — même filet que le gestionnaire global d'AD-16
        enveloppe = reponse_interne(request)
        return "erreur", {"status": enveloppe.status_code, "corps": _corps(enveloppe)}
    return "resultat", _corps(reponse)


def _corps(reponse: Response) -> Any:
    return json.loads(bytes(reponse.body).decode("utf-8"))


async def _remplir(progression: Progression, request: Request,
                   produire: Callable[[], Awaitable[Response]]) -> None:
    nom, donnees = await _terminal(request, produire)
    progression.poser(nom, donnees)


async def _flux(progression: Progression, request: Request,
                produire: Callable[[], Awaitable[Response]], *,
                keepalive_s: float) -> AsyncIterator[bytes]:
    tache = asyncio.create_task(_remplir(progression, request, produire))
    try:
        while True:
            try:
                nom, donnees = await asyncio.wait_for(progression.evenements.get(), keepalive_s)
            except TimeoutError:
                # Commentaire SSE : deux octets utiles, ignorés par tout client conforme, et la
                # seule chose qui dise à l'infrastructure que la connexion vit.
                yield b": keep-alive\n\n"
                continue
            yield _sse(nom, donnees)
            if nom in TERMINAUX:
                return
    finally:
        # Le client est parti (ou le flux s'est refermé) : le calcul n'a plus de destinataire. La
        # tâche est annulée pour ne pas laisser une requête payante tourner dans le vide, et
        # attendue pour que son annulation soit effective avant que la réponse ne se termine.
        if not tache.done():
            tache.cancel()
            try:
                await tache
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def diffuser(request: Request, *, pipeline: str, settings: Settings,
             progression: Progression,
             produire: Callable[[], Awaitable[Response]]) -> StreamingResponse:
    """La réponse `text/event-stream` d'une route de progression.

    `X-Accel-Buffering: no` et `Cache-Control: no-cache` disent aux intermédiaires de ne rien
    tamponner : un flux d'avancement mis en cache ou accumulé n'avance plus.
    """
    return StreamingResponse(
        _flux(progression, request, produire, keepalive_s=settings.sse_keepalive_s),
        media_type=MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
