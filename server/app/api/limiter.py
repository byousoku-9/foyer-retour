"""AD-13 — Le garde-fou de coût : identité client réelle, quotas par instance, 429 avec `Retry-After`.

**Ce que ce limiteur promet, et ce qu'il ne promet pas.** AD-13 le dit sans détour et le code le
répète ici parce qu'un lecteur pressé lirait « rate limiter » et entendrait « protection » : il est
**best-effort par instance**. Ses compteurs vivent en mémoire de process — un redémarrage les remet à
zéro, et deux instances servent deux fois le quota. Il protège le budget d'une démonstration servie
par une seule instance (`--max-instances=1`), avec le plafond par requête d'AD-9 et le plafond
fournisseur derrière lui. Ce n'est ni un quota global, ni une protection contre un attaquant : une
adresse forgée par requête le contourne, et l'éviction de la table (bornée par
`rate_limit_max_clients`) rend même ce contournement bon marché. La même phrase est dans le
`README.md`, pour que personne n'ait à lire ce fichier pour le savoir.

**L'identité.** Derrière Cloud Run, `request.client.host` est l'adresse du proxy : la même pour tout
le monde. AD-13 fixe donc l'identité au **premier élément de `X-Forwarded-For`**, et ce module lit
l'en-tête **directement**, sans passer par `scope["client"]`. C'est volontaire, et c'est aussi ce qui
rend `--proxy-headers` sans effet **ici** : le middleware d'uvicorn ne réécrit que `scope["client"]`
et `scope["scheme"]`, jamais les en-têtes, qui arrivent tels quels dans tous les cas. Le `Dockerfile`
garde ces options pour le reste (schéma `https` correct dans les redirections de `StaticFiles`), pas
pour le limiteur. Une IPv4 est prise entière ; une IPv6 est tronquée au **/64**, parce qu'un
fournisseur d'accès délègue couramment un /64 entier à un abonné et que compter par adresse
laisserait le même abonné se donner 2⁶⁴ identités.

**Sans l'en-tête, hors `ENV=dev`, on refuse (400).** Le limiteur ne « démarre pas » aveugle : servir
sans identité reviendrait à n'avoir aucun quota du tout, ce qu'AD-13 refuse explicitement. En dev,
où rien n'est devant le serveur, l'identité est la constante `local`.
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections import OrderedDict

from starlette.requests import Request

from server.app.config import Settings
from server.app.domain.errors import ErrorCode, InvalidRequest, PipelineError

ENTETE = "X-Forwarded-For"
IDENTITE_DEV = "local"
FENETRE_MINUTE_S = 60.0
FENETRE_JOUR_S = 86400.0


def identite_client(request: Request, *, env: str) -> str:
    """Identité d'AD-13 : premier élément de `X-Forwarded-For`, IPv6 tronquée au /64.

    Lève `InvalidRequest` (400) si l'en-tête manque hors `ENV=dev`, ou s'il ne porte pas une adresse
    IP : une chaîne quelconque ferait une identité par valeur reçue, c'est-à-dire aucun quota.
    """
    brut = request.headers.get(ENTETE, "")
    premier = brut.split(",")[0].strip()
    if not premier:
        if env == "dev":
            return IDENTITE_DEV
        raise InvalidRequest(
            f"en-tête {ENTETE} absent : le serveur est censé tourner derrière un proxy qui le pose")
    # Une IPv6 peut arriver entre crochets, avec ou sans port (« [2001:db8::1]:41234 ») : c'est la
    # forme que pose un proxy qui suit RFC 7239. Les crochets ne font pas partie de l'adresse.
    if premier.startswith("[") and "]" in premier:
        premier = premier[1:premier.index("]")]
    # Un port peut suivre une IPv4 (« 203.0.113.7:41234 ») ; l'adresse s'arrête au deux-points, sauf
    # en IPv6 où les deux-points sont la syntaxe même de l'adresse.
    elif premier.count(":") == 1:
        premier = premier.split(":")[0]
    try:
        adresse = ipaddress.ip_address(premier)
    except ValueError:
        raise InvalidRequest(f"en-tête {ENTETE} : adresse IP attendue en premier élément") from None
    if adresse.version == 4:
        return str(adresse)
    return str(ipaddress.ip_network(f"{adresse}/64", strict=False))


class _Fenetre:
    """Compteur de fenêtre fixe : combien d'appels depuis le début de la fenêtre courante."""

    __slots__ = ("debut", "compte")

    def __init__(self, maintenant: float) -> None:
        self.debut = maintenant
        self.compte = 0

    def compter(self, maintenant: float, duree: float) -> int:
        if maintenant - self.debut >= duree:
            self.debut = maintenant
            self.compte = 0
        self.compte += 1
        return self.compte

    def reste(self, maintenant: float, duree: float) -> float:
        return max(0.0, self.debut + duree - maintenant)


class RateLimiter:
    """Quotas `rate_limit_per_minute` / `rate_limit_per_day` par identité, table bornée."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: OrderedDict[str, tuple[_Fenetre, _Fenetre]] = OrderedDict()

    def reset(self) -> None:
        self._clients.clear()

    def __len__(self) -> int:
        return len(self._clients)

    def check(self, request: Request, *, maintenant: float | None = None) -> str:
        """Compte cette requête pour son identité ; rend l'identité, ou lève.

        `InvalidRequest` (400) quand l'identité manque hors dev, `PipelineError(rate_limited)` (429)
        quand un quota est dépassé. Dans les deux cas, l'appelant n'a encore rien dépensé : le
        limiteur est appelé **avant** le budget et avant le pipeline.
        """
        s = self._settings
        identite = identite_client(request, env=s.env)
        maintenant = time.monotonic() if maintenant is None else maintenant
        fenetres = self._clients.get(identite)
        if fenetres is None:
            fenetres = (_Fenetre(maintenant), _Fenetre(maintenant))
            self._clients[identite] = fenetres
            while len(self._clients) > s.rate_limit_max_clients:
                # Table bornée (AD-13, limiteur en mémoire de process) : la plus anciennement vue
                # part. Elle repart de zéro si elle revient — c'est le « best-effort », écrit.
                self._clients.popitem(last=False)
        self._clients.move_to_end(identite)
        minute, jour = fenetres
        # Les deux fenêtres sont incrémentées ensemble : un appel refusé par la minute compte quand
        # même dans la journée. Sans cela, marteler le serveur une fois la minute pleine coûterait
        # zéro quota journalier — l'exact contraire de ce qu'un limiteur doit faire.
        compte_minute = minute.compter(maintenant, FENETRE_MINUTE_S)
        compte_jour = jour.compter(maintenant, FENETRE_JOUR_S)
        if compte_minute > s.rate_limit_per_minute:
            raise self._trop(f"{s.rate_limit_per_minute} requêtes par minute",
                             minute.reste(maintenant, FENETRE_MINUTE_S))
        if compte_jour > s.rate_limit_per_day:
            raise self._trop(f"{s.rate_limit_per_day} requêtes par jour",
                             jour.reste(maintenant, FENETRE_JOUR_S))
        return identite

    def _trop(self, quota: str, reste_s: float) -> PipelineError:
        # `Retry-After` = le temps restant de la fenêtre dépassée, borné en haut par `retry_after_s`
        # (une fenêtre journalière peut rester fermée des heures : l'annoncer n'aiderait personne, et
        # `Retry-After` est une indication — le client qui revient trop tôt reçoit un nouveau 429).
        retry = max(1, min(self._settings.retry_after_s, math.ceil(reste_s)))
        erreur = PipelineError(ErrorCode.rate_limited, f"quota dépassé ({quota}) : réessayez dans {retry} s")
        erreur.retry_after_s = retry  # lu par `api/errors.gestionnaire_pipeline` pour l'en-tête
        return erreur
