"""Le maintien au chaud des préfixes déjà servis (story 5.6, T5).

**Le chiffre qui décide.** Le préfixe cacheable d'une requête — le prompt système, le sommaire
complet du document, les définitions d'outils et le schéma de sortie — est mis en cache une heure
chez le fournisseur (`MODEL_CAPS[...]["cache_ttl"] == "1h"`, AD-9). Tant qu'il est chaud, il se
relit à ≈ 0,015 € ; froid, la requête suivante paie son **écriture**, ≈ 0,28 €. Sur un service qui
reçoit quelques requêtes par jour — c'est-à-dire exactement une démonstration —, presque **toutes**
les requêtes sont la première après expiration.

**Ce que fait le maintien.** Une tâche de fond réveillée toutes les `prefix_keepalive_s` secondes
(défaut 3 000 s = 50 min, soit 600 s de marge sous l'heure) renvoie, pour chaque préfixe **déjà servi
depuis le démarrage**, le plus petit appel qui existe : le même modèle, le même bloc système avec son
`cache_control`, les mêmes outils, le même schéma de sortie, un message d'un caractère et
`max_tokens = 1`. Il ne demande rien au modèle ; il lui fait relire le préfixe, ce qui en repousse
l'expiration.

**Les trois bornes, et pourquoi elles sont là.**

- *Déjà servi.* Le registre ne contient que des préfixes qu'une vraie requête a fait écrire chez le
  fournisseur (le client ne note un préfixe qu'après une réponse qui l'a réellement caché). Rien
  n'est jamais chauffé « au cas où » : ce serait payer pour un chemin que personne n'emprunte.
- *Un plafond de coût par jour.* Un préfixe maintenu en continu coûte ≈ 0,015 € × 86 400 / 3 000
  ≈ 0,43 € par jour. Le service en sert plusieurs (comprendre, navigation, rédiger, vérifier, par
  document) : sans plafond, le maintien coûterait plus cher que ce qu'il évite. Le plafond atteint,
  le maintien s'arrête pour la journée et `/sante` le dit.
- *Seulement sous `--min-instances=1`.* Hors de là, aucune instance ne tourne entre deux requêtes :
  la tâche ne s'exécuterait pas, ou pire, s'exécuterait à des instants imprévisibles quand Cloud Run
  rend le CPU à une instance gelée — on paierait des maintiens sans jamais tenir le préfixe chaud.
  Le drapeau reste décidé par `deploy.yml`, seule autorité sur le dimensionnement ; le processus, lui,
  reçoit `PREFIX_KEEPALIVE_ENABLED` du même fichier, et `tests/test_workflows.py` refuse que l'un
  soit posé sans l'autre.

**Jamais en test.** Le maintien exige la clé fournisseur, dont l'absence est la règle de la suite
hermétique, **et** un réglage dont le défaut est faux. Un client factice n'en déclenche aucun.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger("foyer.cache")

# La forme de l'appel minimal — `max_tokens = 1`, un message d'un caractère — vit dans
# `llm/client.MAINTIEN_MAX_TOKENS`/`MAINTIEN_MESSAGE`, avec le code qui l'émet : la table des couches
# interdit à `cache` d'importer `llm`, et ce module n'a de toute façon rien à en décider. Il décide
# **quand** maintenir et **jusqu'où**, ce qui est ci-dessous.
# Un jour, en secondes, pour le plafond de coût quotidien. Le compteur bascule sur le quantième UTC :
# un plafond « par jour » a besoin d'une frontière, et une frontière fixe se lit dans un journal.
SECONDES_PAR_JOUR = 86_400.0


@dataclass(frozen=True)
class PrefixeServi:
    """De quoi rejouer exactement le préfixe facturable d'un appel déjà servi.

    Les quatre champs sont ceux dont `llm/client._cache_key` tire `prefix_digest` : le modèle, le
    bloc système (avec son `cache_control`), les outils et le schéma de sortie. Rejouer autre chose
    écrirait un **second** préfixe au lieu de relire le premier.
    """

    digest: str
    model: str
    system: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    output_config: dict[str, Any] | None
    # La durée de vie du préfixe **de ce modèle**, en secondes (`llm/models.CACHE_TTL_S`). Tous ne la
    # déclarent pas égale : « 1h » pour `reason`, « 5m » pour `ingest` et `micro`. Un préfixe dont le
    # TTL est plus court que l'intervalle de maintien n'est pas maintenable — le relire toutes les
    # 50 minutes paierait une **écriture** à chaque tour, exactement ce qu'on cherche à éviter.
    cache_ttl_s: float = 3600.0


@dataclass
class EtatMaintien:
    """Ce que `/sante` publie du maintien : des comptes et un coût, jamais un prompt."""

    actif: bool = False
    prefixes: int = 0
    maintiens: int = 0
    # Préfixes servis mais **non maintenables** : leur modèle déclare un TTL plus court que
    # l'intervalle. Publiés, pas tus — sinon `prefixes` annoncerait une couverture qui n'existe pas.
    ignores: int = 0
    echecs: int = 0
    cout_cumule_eur: float = 0.0
    cout_du_jour_eur: float = 0.0
    plafond_du_jour_atteint: bool = False


class RegistreDesPrefixes:
    """Les préfixes que le fournisseur a réellement cachés depuis le démarrage, bornés en nombre.

    Borné parce que le registre est alimenté par le trafic : un service qui sert plusieurs documents,
    plusieurs langues et quatre étapes en produit vite plus qu'il n'est raisonnable de maintenir. Le
    dépassement **refuse le nouveau venu** plutôt que d'évincer un ancien : les préfixes déjà tenus
    au chaud sont ceux dont l'économie est prouvée, et faire tourner la liste ferait payer une
    écriture à chaque rotation.
    """

    def __init__(self, max_prefixes: int) -> None:
        self.max_prefixes = int(max_prefixes)
        self._entrees: dict[str, PrefixeServi] = {}
        self.refuses = 0

    def __len__(self) -> int:
        return len(self._entrees)

    def entrees(self) -> list[PrefixeServi]:
        return list(self._entrees.values())

    def noter(self, digest: str, *, model: str, system: Any, tools: Any,
              output_config: Any = None, cache_ttl_s: float = 3600.0) -> None:
        if digest in self._entrees:
            return
        if len(self._entrees) >= self.max_prefixes:
            self.refuses += 1
            return
        self._entrees[digest] = PrefixeServi(
            digest=digest, model=model,
            system=list(system) if system is not None else [],
            tools=list(tools) if tools is not None else None,
            output_config=dict(output_config) if output_config is not None else None,
            cache_ttl_s=float(cache_ttl_s))


class MaintienDesPrefixes:
    """La tâche de fond. Elle n'échoue jamais vers l'appelant : elle compte, journalise et continue."""

    def __init__(self, *, client: Any, registre: RegistreDesPrefixes, intervalle_s: float,
                 max_cout_eur_par_jour: float, timeout_s: float) -> None:
        self._client = client
        self._registre = registre
        self._intervalle_s = float(intervalle_s)
        self._max_cout_eur_par_jour = float(max_cout_eur_par_jour)
        self._timeout_s = float(timeout_s)
        self._tache: asyncio.Task[None] | None = None
        self._jour = self._quantieme()
        self.etat = EtatMaintien()

    # --- cycle de vie ------------------------------------------------------

    def demarrer(self) -> None:
        if self._tache is not None:
            return
        self.etat.actif = True
        self._tache = asyncio.create_task(self._boucle(), name="foyer.maintien-prefixes")

    async def fermer(self) -> None:
        self.etat.actif = False
        tache, self._tache = self._tache, None
        if tache is None:
            return
        tache.cancel()
        try:
            await tache
        except asyncio.CancelledError:
            pass

    # --- boucle ------------------------------------------------------------

    async def _boucle(self) -> None:
        while True:
            await asyncio.sleep(self._intervalle_s)
            try:
                await self.un_tour()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — une tâche de fond ne tue jamais le service
                self.etat.echecs += 1
                LOG.warning("maintien de préfixes : tour abandonné (%s)", type(exc).__name__)

    async def un_tour(self) -> None:
        """Un passage sur tous les préfixes servis. Public, pour qu'un test le joue sans horloge."""
        self._basculer_le_jour()
        entrees = self._registre.entrees()
        self.etat.prefixes = len(entrees)
        self.etat.ignores = sum(1 for e in entrees if e.cache_ttl_s <= self._intervalle_s)
        for entree in entrees:
            if entree.cache_ttl_s <= self._intervalle_s:
                # Le préfixe a déjà refroidi quand le tour arrive : le relire écrirait un nouveau
                # préfixe au tarif d'écriture, à chaque tour, pour rien. On ne le maintient pas, et
                # `/sante` le compte au lieu de laisser croire qu'il est chaud.
                continue
            if self.etat.cout_du_jour_eur >= self._max_cout_eur_par_jour:
                # Le plafond est une décision, pas un incident : il se lit dans `/sante` et le
                # maintien reprend au quantième suivant.
                self.etat.plafond_du_jour_atteint = True
                return
            try:
                cout = await self._client.maintenir_prefixe(entree, timeout_s=self._timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — mapping déjà fait côté client
                self.etat.echecs += 1
                LOG.warning("maintien du préfixe %s abandonné (%s)",
                            entree.digest[:12], type(exc).__name__)
                continue
            self.etat.maintiens += 1
            self.etat.cout_cumule_eur += cout
            self.etat.cout_du_jour_eur += cout

    def _basculer_le_jour(self) -> None:
        jour = self._quantieme()
        if jour == self._jour:
            return
        self._jour = jour
        self.etat.cout_du_jour_eur = 0.0
        self.etat.plafond_du_jour_atteint = False

    @staticmethod
    def _quantieme() -> int:
        return int(time.time() // SECONDES_PAR_JOUR)
