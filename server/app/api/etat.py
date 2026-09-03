"""Ce que l'application charge **une fois au démarrage**, et que chaque requête relit sans le refaire.

AD-7 : « `corpus` charge au démarrage en lecture seule ». AD-9 : le client Claude est async et
construit une fois. Et la reprise différée de la story 1.5 ajoute les deux digests : `pipeline_digest()`
et `prompts_digest()` relisent toute l'arborescence du code — les calculer par requête coûterait des
dizaines de lectures de fichiers, et les laisser au repli mémoïsé du pipeline ferait servir des
empreintes périmées par une image dont le code aurait changé à chaud, **sans que rien ne le dise**.

Rien ici n'est muté durablement par une requête (convention « État & transversal » : aucune mutation
d'état hors mémoire de process). Les deux objets vivants sont le limiteur et, depuis la story 3.4,
le cache LRU/sémaphore borné du renderer de pages ; tous deux existent précisément pour partager
une borne entre requêtes d'un même process.
"""

from __future__ import annotations

import ipaddress
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from server.app.api.limiter import RateLimiter
from server.app.api.schemas import Alerte, DOC_ID_MAX, DOC_ID_PATTERN
from server.app.config import RAISON_PUBLIABLE_MAX_DEFAULT, REPO_ROOT, Settings, cle_absente
from server.app.corpus.dictionary import Dictionnaire, load_dictionary
from server.app.domain.dictionary import DICTIONARY_FILE
from server.app.corpus.index import Index
from server.app.corpus.loader import SOURCE_FILES, Corpus, load_corpus
from server.app.corpus.racine import exiger_racine_installee, Lecture, relire
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.evals import EtatPublication, PublicationEvals
from server.app.domain.ingest import Check, GateContext, Report
from server.app.api.page_renderer import PageRenderer, VerifiedSource
from server.app.cache import (
    CacheDeReponses,
    MaintienDesPrefixes,
    RegistreDesPrefixes,
)
from server.app.llm.audit import AuditSink, JsonlAuditSink, ProjectionAuditSink
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines.guide import repondre_guide
from server.app.pipelines.sinistre import run as executer_sinistre

# Logger de module, comme `api/main`, `api/request_id` et `api/errors` (`foyer.*`) : un
# `getLogger("foyer")` posé en ligne d'appel n'aurait pas de nom propre, donc pas de filtre possible,
# et un test ne saurait pas quoi capturer.
LOG = logging.getLogger("foyer.etat")

DATA_DIR = REPO_ROOT / "data"
# Le magasin du cache de réponses (story 5.6, T5). Sous `.cache/`, comme le cache d'évals, et ignoré
# par git pour la même raison : ce sont des octets produits par une exécution, jamais du contenu servi.
CACHE_REPONSES_DIR = REPO_ROOT / ".cache" / "reponses"


def _conversation_secret(settings: Settings) -> bytes:
    """Clé inter-instance sans nouveau secret d'infrastructure.

    En production, toutes les instances reçoivent déjà la même clé Anthropic depuis Secret Manager.
    Une dérivation HMAC à domaine séparé évite de l'employer directement et permet à un suivi de
    changer d'instance. Hors ligne, où cette clé est volontairement vide, une clé de processus suffit.
    """
    provider_key = settings.anthropic_api_key.strip()
    if not provider_key:
        return secrets.token_bytes(32)
    return hmac.new(provider_key.encode("utf-8"), b"foyer-retour/conversation/v1",
                    hashlib.sha256).digest()
RAPPORT = "report.json"
SOURCE_URL = "source.url"
# Une URL de source tient largement là-dedans (celle du contrat AXA fait 170 caractères) ; au-delà,
# ce n'est plus une URL, c'est un fichier qu'on recopierait dans une réponse publique.
SOURCE_URL_MAX = 2048


def _chemin_relatif_probable(match: re.Match[str]) -> str:
    """Masque un chemin relatif plausible sans prendre une date ou un type MIME pour un secret."""
    valeur = match.group(0)
    # Les expressions régulières documentées entre accents graves ne sont pas des chemins, même
    # lorsqu'elles contiennent plusieurs ``/``.
    avant, apres = match.string[:match.start()], match.string[match.end():]
    if avant.count("`") % 2 and apres.count("`") % 2:
        debut = avant.rfind("`") + 1
        fin_relative = apres.find("`")
        contenu = match.string[debut:match.end() + fin_relative]
        # Seul un littéral de regex complet est conservé. Les accents graves ne rendent pas un
        # emplacement publiable : ``secrets/contrats/run.json`` reste un chemin privé.
        if (contenu.startswith("/") and contenu.endswith("/")
                and any(meta in contenu for meta in "^$[]{}+*?")):
            return valeur
    morceaux = valeur.split("/")
    if all(m.isdigit() for m in morceaux):  # date, fraction ou version numérique
        return valeur
    if morceaux[0].lower() in {
        "application", "audio", "font", "image", "message", "model", "multipart", "text", "video",
    }:
        return valeur
    # Deux mots séparés par une barre (``et/ou``) ne suffisent pas à prouver un emplacement. Une
    # extension de fichier ou au moins trois composantes apporte le signal manquant.
    repertoires = {"cache", "config", "data", "home", "private", "secret", "secrets", "srv", "tmp", "users"}
    unicode_present = any(not caractere.isascii() for caractere in valeur)
    if (len(morceaux) < 3 and "." not in morceaux[-1]
            and morceaux[0].lower() not in repertoires and not unicode_present):
        return valeur
    return "[emplacement masqué]"


def _chemin_cite_probable(match: re.Match[str]) -> str:
    """Masque aussi un emplacement cité contenant des espaces, sans masquer ``'et/ou'``."""
    quote, valeur = match.group("quote"), match.group("valeur")
    if (valeur.startswith("/") and valeur.endswith("/")
            and any(metacaractere in valeur for metacaractere in "^$[]{}+*?")):
        return match.group(0)
    if re.match(r"(?i)^[a-z][a-z0-9+.-]*:", valeur):
        return f"{quote}[emplacement masqué]{quote}"
    normalise = valeur.replace("\\", "/")
    morceaux = normalise.split("/")
    repertoires = {"cache", "config", "data", "home", "private", "secret", "secrets", "srv", "tmp", "users"}
    unicode_present = any(not caractere.isascii() for caractere in normalise)
    if (normalise.startswith(("/", "./", "../", "//"))
            or len(morceaux) >= 3 or (len(morceaux) >= 2 and (
                "." in morceaux[-1] or morceaux[0].lower() in repertoires or unicode_present))):
        return f"{quote}[emplacement masqué]{quote}"
    return match.group(0)


def doc_id_auditable(doc_id: str) -> bool:
    """Le contrat public du segment ``doc_id``, appliqué avant toute composition de chemin."""
    return len(doc_id) <= DOC_ID_MAX and re.fullmatch(DOC_ID_PATTERN, doc_id) is not None


def raison_publiable(raison: str | None, *, max_chars: int = RAISON_PUBLIABLE_MAX_DEFAULT) -> str | None:
    """Diagnostic public borné, dont seuls les véritables emplacements sont masqués.

    Cette décision est commune à ``/sante`` et ``/documents``. Une URI avec ``://`` (ou une URI
    ``data:``), un chemin POSIX absolu/relatif, un chemin Windows avec lecteur ou un partage UNC est
    privé ; un deux-points de diagnostic ou une barre oblique au milieu d'un motif de validation ne
    l'est pas.
    """
    if raison is None:
        return None
    propre = re.sub(
        r"(?P<quote>['\"])(?P<valeur>[^'\"\n]*[/\\][^'\"\n]*)(?P=quote)",
        _chemin_cite_probable,
        raison,
    )
    propre = re.sub(
        r"(?i)\b[a-z][a-z0-9+.-]*://[^\n,;)'\"<>]*",
        "[emplacement masqué]",
        propre,
    )
    # ``data:`` est un vrai schéma sans ``//``. Le nommer explicitement évite de réintroduire le
    # filtre trop large qui prenait ``Invalid JSON:`` ou ``manifest invalide :`` pour des URI.
    propre = re.sub(
        r"(?i)\bdata:[^\s)]*",
        "[emplacement masqué]",
        propre,
    )
    propre = re.sub(
        r"(?<![\w])(?:[a-zA-Z]:[\\/]|\\\\)[^\n,;)'\"<>]*",
        "[emplacement masqué]",
        propre,
    )
    propre = re.sub(
        r"(?<![\w.])(?:\.\.?/|/)(?=[\w.~\-])[^\n,;)'\"<>]*",
        "[emplacement masqué]",
        propre,
    )
    # Chemin relatif plausible sans préfixe `./` : au moins deux composantes séparées. Les regex
    # usuelles (`^[a-z0-9-]+$`, `/^[a-z]+/`) ne satisfont pas cette forme et restent donc intactes.
    propre = re.sub(
        r"(?<![\w./])(?:[\w.-]+/)+[\w.-]+(?=$|[\s,;)'\"<>`])",
        _chemin_relatif_probable,
        propre,
    )
    return propre if len(propre) <= max_chars else propre[:max_chars - 1] + "…"


def url_publiable(brut: str | None) -> str | None:
    """L'URL publique d'un document, ou `None` — l'**unique** décision de ce qui sort (AD-7).

    Deux appelants la partagent : `_sources()`, qui lit `data/{doc_id}/source.url`, et
    `routes/documents.py`, qui publie d'abord `Document.source_url` écrit par l'ingestion. Les
    laisser décider chacun de leur côté avait déjà produit un trou (revue 1.9) : le fichier était
    filtré, le champ du document ne l'était pas, et un `gs://` écrit par une ingestion future serait
    ressorti tel quel dans une réponse publique.

    Ce qui est refusé, et pourquoi : un schéma qui n'est pas `http(s)` (le bucket privé de secours
    d'AD-7 n'est ni atteignable ni instructif pour un lecteur) ; toute valeur contenant un blanc (ce
    n'est plus une URL) ; et toute valeur au-delà de `SOURCE_URL_MAX`.

    **La ligne retenue est la première dont le schéma est publiable, pas la première tout court**
    (revue 1.9, tour 2). `source.url` peut porter deux lignes — l'URL publique et la copie privée —
    et rien dans l'ingestion ne garantit leur ordre. Ne regarder que la première rendait le filtre
    dépendant de cet ordre : un fichier qui écrit `gs://…` d'abord ne publiait plus **aucune**
    source, en silence, et la page perdait le lien « voir le contrat à sa source publique » — le
    seul qui rende « édition juin 2017 » vérifiable par celui à qui on l'annonce (AD-7). Balayer les
    lignes ne relâche rien : un `gs://` n'est jamais publié, quelle que soit sa position.

    La comparaison de schéma est **insensible à la casse** : `HTTPS://` est une URL valide (RFC 3986
    : le schéma est insensible à la casse), et un `startswith` strict la rejetait comme un `gs://`.
    L'URL rendue, elle, garde sa casse d'origine — on filtre, on ne réécrit pas.
    """
    if not brut:
        return None
    for ligne in brut.splitlines():
        url = ligne.strip()
        if not url or len(url) > SOURCE_URL_MAX or any(c.isspace() for c in url):
            continue
        try:
            parsed = urlsplit(url)
            port = parsed.port  # force la validation de la borne et de la syntaxe du port
        except ValueError:
            continue
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            continue
        if parsed.username is not None or parsed.password is not None or "\\" in url:
            continue
        hostname = parsed.hostname.rstrip(".").lower()
        if (hostname == "localhost"
                or hostname.endswith((".localhost", ".local", ".internal", ".home", ".lan"))):
            continue
        adresse_historique = _ipv4_historique(hostname)
        try:
            adresse = adresse_historique or ipaddress.ip_address(hostname)
        except ValueError:
            # Un nom public doit être un nom DNS, pas un alias local à une seule composante. IDNA
            # valide aussi les hôtes Unicode sans les réécrire dans la valeur publiée.
            try:
                ascii_host = hostname.encode("idna").decode("ascii")
            except UnicodeError:
                continue
            if "." not in ascii_host or any(
                    not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9-]+", label)
                    or label.startswith("-") or label.endswith("-")
                    for label in ascii_host.split(".")):
                continue
        else:
            if (not adresse.is_global or adresse.is_loopback or adresse.is_private
                    or adresse.is_link_local or adresse.is_unspecified):
                continue
        if port is not None and not 1 <= port <= 65535:  # ``urlsplit`` couvre déjà, garde explicite
            continue
        return url
    return None


def _ipv4_historique(hostname: str) -> ipaddress.IPv4Address | None:
    """Notation IPv4 historique que les navigateurs WHATWG normalisent avant navigation.

    ``127.1``, ``127.0.1``, les composantes hexadécimales et octales désignent bien le loopback.
    Les traiter comme des noms DNS laisserait le serveur publier une URL que le navigateur
    réinterprète ensuite comme locale.
    """
    morceaux = hostname.split(".")
    if morceaux and morceaux[-1] == "":
        morceaux.pop()
    if not morceaux or len(morceaux) > 4:
        return None
    nombres: list[int] = []
    for morceau in morceaux:
        base = 10
        chiffres = morceau
        if morceau.lower().startswith("0x"):
            base, chiffres = 16, morceau[2:]
        elif len(morceau) > 1 and morceau.startswith("0"):
            base, chiffres = 8, morceau[1:]
        if not chiffres:
            nombres.append(0)
            continue
        try:
            nombres.append(int(chiffres, base))
        except ValueError:
            return None
    if any(nombre > 255 for nombre in nombres[:-1]):
        return None
    dernier_max = 256 ** (5 - len(nombres))
    if nombres[-1] >= dernier_max:
        return None
    valeur = sum(nombre * (256 ** (3 - index)) for index, nombre in enumerate(nombres[:-1]))
    valeur += nombres[-1]
    return ipaddress.IPv4Address(valeur)


@dataclass
class EtatApp:
    """L'état de démarrage, posé sur `app.state.foyer`."""

    settings: Settings
    corpus: Corpus
    index: Index
    client: LlmClient
    limiter: RateLimiter
    followup_limiter: RateLimiter
    pipeline_digest_hex: str
    prompts_digest_hex: str
    # Story 3.7 : clé éphémère du jeton de continuation. Elle n'identifie aucun utilisateur et ne
    # conserve aucun dossier ; son renouvellement au redémarrage invalide les pages anciennes.
    conversation_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)
    # AD-5 / AD-7 (story 2.1) : l'objet chargé une fois au démarrage, en lecture seule. Le champ
    # porte l'**objet** et non plus un booléen, parce que trois lecteurs en ont besoin — `/sante`
    # (trois booléens), le pipeline du guide (le court-circuit et le compte de variantes) et
    # *retrouver* (l'élargissement) — et qu'un booléen ne dit ni pourquoi, ni sur quel corpus.
    dictionnaire: Dictionnaire = field(default_factory=Dictionnaire)
    # Les dictionnaires contractuels sont propriétaires de leur document. Le guide garde le champ
    # historique ci-dessus pour `/sante`; le pipeline sinistre ne reçoit que l'objet de son contrat.
    dictionnaires: dict[str, Dictionnaire] = field(default_factory=dict)
    # Le pipeline est un attribut, et non un import direct dans la route : c'est ce qui rend
    # explicite que l'API n'appelle **qu'un** pipeline (AD-1 : jamais de dispatch), et ce que les
    # tests remplacent par un double pour couvrir la matrice d'E/S sans réseau.
    pipeline: Any = repondre_guide
    # Le pipeline sinistre est un second attribut, pour la même raison que le premier : la route
    # n'appelle **qu'un** pipeline et ne dispatche jamais (AD-1). Deux routes, deux pipelines, aucun
    # aiguillage par variante — `POST /api/v1/sinistre` appelle celui-ci et rien d'autre.
    pipeline_sinistre: Any = executer_sinistre
    # AD-7/AD-8 : `report.json` est écrit par l'ingestion, lu **une fois** au démarrage, et exposé
    # tel quel par `GET /api/v1/documents/{doc_id}/report`. Aucune lecture de `data/` par requête.
    reports: dict[str, Report] = field(default_factory=dict)
    # État de lecture des rapports qui ne peuvent pas être publiés. La valeur est stable pour toute
    # la vie du process et distingue un artefact absent, illisible ou étranger d'un rapport valide
    # dont ``checks`` serait réellement vide.
    report_errors: dict[str, str] = field(default_factory=dict)
    # `doc_id` → URL publique de la source (AD-7, `data/{doc_id}/source.url`). Lue au démarrage
    # comme tout le reste : `GET /api/v1/documents` ne touche pas `data/`.
    source_urls: dict[str, str] = field(default_factory=dict)
    # Chemins déjà choisis et vérifiés par le loader. Le renderer ne les reconstruit jamais depuis
    # un `doc_id` reçu sur HTTP, et n'ouvre aucun PDF tant qu'une page n'est pas demandée.
    pdf_sources: dict[str, VerifiedSource] = field(default_factory=dict)
    page_renderer: PageRenderer | None = None
    alerts: list[Alerte] = field(default_factory=list)
    # FR41 — le dernier run d'évals publié, lu une fois au démarrage. `publie: false` est un état
    # normal (aucun run publié dans cette image), jamais une erreur : la route le rend tel quel.
    publication_evals: EtatPublication = field(
        default_factory=lambda: EtatPublication(publie=False, raison="absent"))
    # Story 5.6 (T5) — les deux caches de la facture, `None` quand ils ne sont pas armés (pas de clé
    # fournisseur, ou réglage éteint). `None` et non un objet inerte : ici, « il n'y a pas de cache »
    # est une propriété que `/sante` publie, et un objet inerte l'aurait rendue indistinguable d'un
    # cache qui ne sert jamais.
    cache_reponses: CacheDeReponses | None = None
    maintien_prefixes: MaintienDesPrefixes | None = None

    @property
    def documents_servis(self) -> list[str]:
        return self.corpus.served

    @property
    def gate_profile(self) -> str | None:
        """Profil commun aux documents servis, ou `null` dès que l'un d'eux n'a pas de gate.

        AD-11 interdit la bascule silencieuse : annoncer `vertical` alors qu'un document servi n'est
        validé par rien ferait passer pour éprouvé ce qui ne l'est pas. Deux profils différents ne
        se résument pas non plus — c'est `null`, et `/sante` publie les alertes à côté.
        """
        profils = set()
        for doc_id in self.corpus.served:
            entree = self.corpus.manifest.get(doc_id)
            gate = entree.gate if entree is not None else None
            # Le loader neutralise **localement** un gate dont `source_hash`/`ingest_fingerprint`/
            # `overlay_hash` ne correspondent plus à l'entrée (`corpus/loader._gate_alerts`) : il
            # sert alors le document avec l'alerte `sans_gate`, mais l'entrée du manifest, elle,
            # garde son `gate` renseigné. Relire le manifest seul publierait donc `vertical` à côté
            # d'une alerte `sans_gate`, dans la même réponse — exactement la bascule silencieuse que
            # cette propriété dit interdire. La décision du loader fait foi.
            if gate is None or "sans_gate" in self.corpus.alerts.get(doc_id, ()):
                return None
            profils.add(gate.profile)
        return profils.pop() if len(profils) == 1 else None

    @property
    def gate_cases(self) -> int | None:
        """Le nombre total de cas relus qui fondent le profil publié, ou `null`.

        Strictement adossé à `gate_profile` : `null` dès que celui-ci est `null`. L'accueil affiche
        « niveau de validation : vertical — N cas relus à la main » ; publier un compte sans profil
        laisserait écrire « 2 cas » sous un système dont un document n'est validé par rien, ce
        qu'AD-11 nomme la bascule silencieuse. Le compte est la **somme** des `Gate.cases` des
        documents servis : c'est ce que dit l'AC (« `gate_cases == 2 » pour deux documents gatés à un
        cas chacun), et chaque terme est écrit par le run qui l'a constaté (AD-7 : jamais à la main).
        """
        if self.gate_profile is None:
            return None
        total = 0
        for doc_id in self.corpus.served:
            entree = self.corpus.manifest.get(doc_id)
            if entree is None or entree.gate is None:  # impossible si `gate_profile` n'est pas nul
                return None
            total += entree.gate.cases
        return total

    @property
    def gate_countersigned(self) -> bool | None:
        """Les cas qui fondent le profil publié sont-ils tous contresignés par un humain, ou `null`.

        Amendement AD-7 / AD-14 (revue Codex 1.10 tour 2, B2). AD-14 définit `vertical` comme « un
        cas guide et un cas sinistre **relus à la main** », « affiché comme tel » : c'est donc la
        page d'accueil qui affirme publiquement la relecture. Tant que la contresignature de la
        personne à qui `epics.md` l'attribue est due, la relecture est celle de la boucle autonome,
        et l'affirmer humaine serait la sorte d'invention qu'AD-16 interdit.

        Comme `gate_cases` : strictement adossé à `gate_profile` (`null` dès qu'il l'est) et
        **conjonction** sur les documents servis — un seul document dont les cas ne sont pas
        contresignés retire la qualification à la phrase entière, qui les additionne.
        """
        if self.gate_profile is None:
            return None
        for doc_id in self.corpus.served:
            entree = self.corpus.manifest.get(doc_id)
            if entree is None or entree.gate is None:  # impossible si `gate_profile` n'est pas nul
                return None
            if not entree.gate.countersigned:
                return False
        return True

    @property
    def gate_validated_by_expert(self) -> bool | None:
        """Un expert assurance a-t-il validé les verdicts qui fondent ce profil ? — `null` ou `False`.

        Jamais `True`, et ce n'est pas une omission : AD-14 pose que « les verdicts ne sont pas
        validés par un expert assurance » et le schéma des cas contraint
        `truth.validated_by_expert` à `False`. Rien dans ce dépôt ne peut donc établir le contraire,
        et un champ qui pourrait valoir `True` laisserait croire qu'un chemin existe.

        Il est publié malgré cela — c'est le point de l'AC 3 : la réserve doit être **lisible** là où
        le service dit son niveau de validation, et pas seulement dans un document que personne
        n'ouvre. Comme `gate_cases` et `gate_countersigned`, il est strictement adossé à
        `gate_profile` : sans gate publié, il n'y a aucun verdict à qualifier.
        """
        return None if self.gate_profile is None else False


def _alertes(corpus: Corpus, *, raison_max_chars: int = RAISON_PUBLIABLE_MAX_DEFAULT) -> list[Alerte]:
    """Les alertes des documents servis (AD-7), et les documents que le chargement a écartés."""
    alertes = [Alerte(doc_id=doc_id, alerte=a)
               for doc_id in sorted(corpus.alerts) for a in corpus.alerts[doc_id]]
    # Une clé non publiable ne devient jamais un segment d'URL ni un identifiant réfléchi. Sa
    # quarantaine reste néanmoins visible comme propriété du service : la taire ferait précisément
    # disparaître le défaut de manifest que cette alerte doit signaler.
    alertes += [Alerte(doc_id=doc_id if doc_id_auditable(doc_id) else "*",
                       alerte="quarantaine",
                       detail=raison_publiable(raison, max_chars=raison_max_chars)
                       or "raison indisponible")
                for doc_id, raison in sorted(corpus.quarantine.items())]
    return alertes


def _alerte_fournisseur(settings: Settings) -> list[Alerte]:
    """Sans clé fournisseur, le service ne peut répondre à **aucune** question — et il le dit.

    `/sante` ne consultait que le corpus : clé absente, révoquée ou hors crédit, il répondait
    `ok: true` pendant que chaque question rendait 503 ou 500. Le front ne lit que `ok` ; la panne
    aurait donc duré jusqu'à ce qu'un humain la voie.

    Ce que cette alerte couvre est ce qui se vérifie **sans appeler personne** : la clé est-elle
    configurée. Une clé présente mais révoquée, ou sans crédit, ne se distingue d'une clé valide
    que par un appel facturé — le dire ici demanderait une sonde périodique vers le fournisseur,
    qui est une décision de déploiement et non de code (reprise différée
    `sonde-fournisseur-post-promotion-epic5-D3`). Ne pas pouvoir tout dire n'autorise pas à taire
    ce qu'on sait.

    `doc_id="*"` : l'alerte porte sur le service, pas sur un document. C'est la même convention que
    la quarantaine d'un identifiant non publiable.
    """
    if settings.anthropic_api_key.strip():
        return []
    return [Alerte(doc_id="*", alerte="cle_fournisseur_absente",
                   detail="aucune clé fournisseur configurée : toute question sera refusée")]


def _alerte_ungated(settings: Settings) -> list[Alerte]:
    """`ENV=prod` + `ALLOW_UNGATED=true` : la dérogation est **refusée**, et le dire est le reste (D7).

    AD-7 cadre `ALLOW_UNGATED` — « dev / J+1 avant le premier gate » — et l'AC de la story 1.10 la
    ferme : « désactivé en production à la fin de cette story ». Retirer la ligne du `Dockerfile` ne
    la fermait pas : la surface réelle est la configuration du service (`--set-env-vars
    ALLOW_UNGATED=true`), qu'aucun test hors ligne ne voit. C'est `config.Settings` qui force
    `allow_ungated=False` en `prod` (revue Codex 1.10, B3) ; ici on publie ce refus.

    Refuser en silence serait le défaut symétrique : celui qui a posé la variable croirait servir des
    documents sans gate alors qu'ils sont en quarantaine. L'alerte le dit là où l'état du système se
    lit, et la page d'accueil l'affiche avec les autres. Le `doc_id` est `*` — c'est une propriété du
    **service**, pas d'un document, et `Alerte` n'a pas d'autre place pour le dire.
    """
    if settings.env != "prod" or not settings.ungated_demande_en_prod:
        return []
    return [Alerte(doc_id="*", alerte="ungated_refuse_en_production",
                   detail="ALLOW_UNGATED=true posé avec ENV=prod : la dérogation est refusée "
                          "(AC 1.10) — allow_ungated vaut false, un document sans gate valide reste "
                          "en quarantaine. Retirer la variable de la configuration du service.")]


def _alertes_dictionnaire(
        dictionnaire: Dictionnaire, *,
        raison_max_chars: int = RAISON_PUBLIABLE_MAX_DEFAULT) -> list[Alerte]:
    """AD-5 / AD-16 : un dictionnaire inutilisable est **dit**, jamais tu.

    Deux alertes, parce que deux causes et deux correctifs (`doc_id="*"` : ce sont des propriétés du
    **service**, comme `ungated_refuse_en_production`, et `Alerte` n'a pas d'autre place pour le dire) :

    - `dictionnaire_non_valide` — aucune main n'a signé (ou le fichier est absent, illisible, non
      conforme : dans tous ces cas `validated` est faux). Le refus « zéro hit » d'AD-5 dort ; le
      correctif est `--valider "Nom"`, ou une réingestion si le fichier ne se lit pas.
    - `dictionnaire_corpus_perime` — le fichier se lit, mais ses `corpus_source_hashes` décrivent un
      autre corpus. Ni variantes, ni court-circuit ; le correctif est de relancer l'enrichissement.

    Les deux peuvent tomber ensemble : un dictionnaire d'un autre corpus n'est pas validé pour
    celui-ci, et taire l'une des deux raisons laisserait chercher la mauvaise.

    **Chaque alerte dit donc sa propre raison** (revue coordonnée 2.1). `Dictionnaire.raison` est,
    pour un fichier **chargé**, renseignée par le seul échec de `_corpus_ok` : la composer dans le
    détail de la première faisait dire à `dictionnaire_non_valide`, sur un fichier lisible, périmé et
    non signé, « aucune validation humaine … — source_hash différent du manifest » — la raison de
    l'autre alerte —, et le correctif `--valider "Nom"` disparaissait au profit d'un diagnostic qui
    n'était pas le sien. La `raison` n'entre donc dans la première que lorsqu'elle **est** la sienne :
    quand le fichier n'a pas pu être chargé du tout.
    """
    alertes: list[Alerte] = []
    if not dictionnaire.validated:
        detail = ("aucune validation humaine : le refus « zéro hit » d'AD-5 est désactivé "
                  "(la recherche se poursuit vers *retrouver*)")
        if dictionnaire.charge:
            # Le fichier est là et se lit : ce qui manque est une signature, et rien d'autre.
            # **Ce détail est lu par un utilisateur**, sur la page d'accueil et dans « Pourquoi
            # cette réponse » : il dit donc ce qui manque et à qui cela incombe, jamais une commande
            # de terminal — un message d'utilisateur ne donne pas d'ordre de shell. La commande
            # exacte, elle, appartient au journal de démarrage (`_journaliser_dictionnaire`) et à
            # `docs/`, que lit celui qui peut l'exécuter.
            detail += " — cette validation reste à faire par l'équipe"
        else:
            # Absent, illisible ou non conforme : `raison` décrit **cet** échec-là, c'est bien le sien.
            raison = dictionnaire.raison or "dictionnaire non chargé"
            detail += f" — {raison_publiable(raison, max_chars=raison_max_chars)}"
        alertes.append(Alerte(doc_id="*", alerte="dictionnaire_non_valide", detail=detail))
    if dictionnaire.charge and not dictionnaire.corpus_ok:
        raison = dictionnaire.raison or "empreintes différentes du manifest"
        alertes.append(Alerte(
            doc_id="*", alerte="dictionnaire_corpus_perime",
            detail=f"{DICTIONARY_FILE} décrit un autre corpus que celui qui est servi : ni variantes, ni "
                   f"court-circuit — {raison_publiable(raison, max_chars=raison_max_chars)}. "
                   "L'enrichissement du dictionnaire doit être relancé par l'équipe."))
    return alertes


def _journaliser_dictionnaire(dictionnaire: Dictionnaire) -> None:
    """Journalise une fois l'état interne complet ; la projection d'alertes reste une fonction pure."""
    if not dictionnaire.validated:
        # La **commande** vit ici, pas dans l'alerte : le journal de démarrage est lu par celui qui
        # peut l'exécuter, l'alerte est lue par l'utilisateur du produit.
        LOG.warning(
            "dictionnaire_non_valide : le refus « zéro hit » d'AD-5 est désactivé — %s "
            "— correctif : python -m server.ingest.enrich_dictionary --valider \"Nom\"",
            dictionnaire.raison or "aucune validation humaine")
    if dictionnaire.charge and not dictionnaire.corpus_ok:
        LOG.warning(
            "dictionnaire_corpus_perime : %s "
            "— correctif : python -m server.ingest.enrich_dictionary",
            dictionnaire.raison or "empreintes différentes du manifest")


def _rapport_publiable(rapport: Report, *, raison_max_chars: int) -> Report:
    """Projection publique d'un rapport, sans emplacement privé dans ses champs textuels."""
    checks = [Check(
        name=raison_publiable(check.name, max_chars=raison_max_chars) or "",
        level=check.level,
        detail=raison_publiable(check.detail, max_chars=raison_max_chars) or "",
    ) for check in rapport.checks]
    stats = {}
    for cle, valeur in rapport.stats.items():
        cle_publique = raison_publiable(cle, max_chars=raison_max_chars) or ""
        if isinstance(valeur, str):
            valeur_publique: int | float | str | dict[str, int] = (
                raison_publiable(valeur, max_chars=raison_max_chars) or "")
        elif isinstance(valeur, dict):
            valeur_publique = {
                raison_publiable(sous_cle, max_chars=raison_max_chars) or "": sous_valeur
                for sous_cle, sous_valeur in valeur.items()
            }
        else:
            valeur_publique = valeur
        stats[cle_publique] = valeur_publique
    return Report(doc_id=rapport.doc_id, checks=checks, stats=stats)


def _artefact_audit(data_dir: Path, doc_id: str, nom: str,
                    lecture: Lecture | None = None) -> Path | None:
    """Résout un artefact sans jamais suivre un lien hors de ``data_dir``.

    La résolution part du **repère pincé** (N1, story 4.5) : sous une racine de publication, le
    chemin réellement lu est le slot de la génération que la passe a pincée, jamais celui que le
    pointeur désigne au moment de cet appel. Le garde-fou est inchangé — ce qui sort de ``data_dir``
    n'est pas publié —, et l'espace vit sous ``data_dir``, donc un slot y reste.
    """
    racine = data_dir.resolve()
    brut = data_dir / doc_id / nom
    chemin = brut if lecture is None else lecture.reel(brut)
    try:
        resolu = chemin.resolve(strict=True)
    except OSError:
        return None
    return resolu if resolu.is_relative_to(racine) and resolu.is_file() else None


def _rapports(data_dir: Path, doc_ids: list[str], lecture: Lecture | None = None, *,
              raison_max_chars: int = RAISON_PUBLIABLE_MAX_DEFAULT) -> tuple[dict[str, Report], list[Alerte]]:
    """Les rapports d'ingestion des documents connus, lus au démarrage (AD-7/AD-8).

    Comme `dictionary.json`, un rapport **absent** ne fait pas tomber le démarrage : AD-8 fait du
    rapport un artefact d'ingestion, et un document peut être servi avant qu'on l'ait écrit (le
    guide l'a été en 1.1). Ce qui change ici, c'est un fichier **présent et invalide** : il produit
    l'alerte `rapport_illisible` sur `/api/v1/sante` (AD-7 : une incohérence est visible, jamais
    muette) et, sur la route, un 400 — la même réponse qu'un document inconnu, puisqu'il n'y a rien
    d'honnête à publier.

    Lire le rapport d'une quarantaine ne charge ni son ``document.json`` ni son sommaire et ne
    l'ajoute jamais au working set. C'est un artefact d'audit distinct dont la page affiche à côté
    le statut effectif du loader.
    """
    rapports: dict[str, Report] = {}
    alertes: list[Alerte] = []
    for doc_id in doc_ids:
        chemin = _artefact_audit(data_dir, doc_id, RAPPORT, lecture)
        if chemin is None:
            continue
        try:
            rapport = Report.model_validate_json(chemin.read_bytes())
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            # Le détail (`exc`) resterait un diagnostic interne s'il partait dans l'enveloppe
            # (AD-16) ; ici il est dans une alerte de `/sante`, qui n'est lue que par nous et par la
            # page d'accueil. Le message dit **quoi**, pas le contenu du fichier.
            alertes.append(Alerte(doc_id=doc_id, alerte="rapport_illisible",
                                  detail=f"{RAPPORT} présent mais non conforme au schéma "
                                         f"({type(exc).__name__})"))
            continue
        if rapport.doc_id != doc_id:
            # Un rapport conforme au schéma peut parler d'un **autre** document (copie de dossier,
            # ingestion relancée ailleurs, `doc_id` renommé sans réingestion). Le publier tel quel
            # sous cette clé ferait lire à un humain les checks et les statistiques d'un document
            # qu'il n'a pas demandé — sur la route qui sert précisément à juger si un contrat est
            # lisible (AD-8). L'alerte porte un nom distinct de `rapport_illisible` : le fichier
            # n'est pas illisible, il est **étranger**, et ce n'est pas le même correctif.
            alertes.append(Alerte(doc_id=doc_id, alerte="rapport_etranger",
                                  detail=f"{RAPPORT} décrit un autre document que le dossier qui "
                                         f"le porte : il n'est pas publié"))
            continue
        rapports[doc_id] = _rapport_publiable(rapport, raison_max_chars=raison_max_chars)
    return rapports, alertes


def _erreurs_rapports(doc_ids: list[str], rapports: dict[str, Report],
                      alertes: list[Alerte]) -> dict[str, str]:
    """Résume l'issue de lecture, sans détail interne destiné aux journaux ou à ``/sante``."""
    erreurs = {doc_id: "absent" for doc_id in doc_ids if doc_id not in rapports}
    for alerte in alertes:
        if alerte.alerte == "rapport_illisible":
            erreurs[alerte.doc_id] = "illisible"
        elif alerte.alerte == "rapport_etranger":
            erreurs[alerte.doc_id] = "etranger"
    return erreurs


def _doc_ids_audit(corpus: Corpus) -> list[str]:
    """Clés adressables par l'API, avant toute composition de chemin sous ``data_dir``.

    Le manifest reste une entrée non fiable. Une clé absolue ou avec traversée peut être conservée
    comme quarantaine interne par le loader, mais elle ne doit jamais atteindre ``_rapports`` ou
    ``_sources`` : ``Path(data_dir) / doc_id`` sortirait alors potentiellement de ``data_dir``.
    """
    connus = set(corpus.served) | set(corpus.quarantine)
    return sorted(doc_id for doc_id in connus if doc_id_auditable(doc_id))


def _sources(data_dir: Path, doc_ids: list[str], lecture: Lecture | None = None) -> dict[str, str]:
    """L'URL publique de chaque document auditable, lue **au démarrage** (AD-7).

    Pourquoi ici et pas dans `Document.source_url` : AD-7 fait de `data/{doc_id}/source.url` le
    fichier canonique (« `data/{doc_id}/source.url` + `source_hash` »), et l'ingestion PDF, elle,
    laisse `Document.source_url` à `None` — le PDF d'un assureur n'est pas committé, il est
    téléchargé au build depuis ce fichier. Le contrat AXA n'aurait donc aucune source affichable
    alors que le repo la connaît, et l'AC de la story demande précisément qu'elle soit publiée.
    Le champ du document, quand il est renseigné (le guide), reste prioritaire : c'est celui que
    l'ingestion a validé.

    Absent ou illisible ⇒ pas d'URL, pas d'alerte : une source non publiée n'empêche rien de servir
    et ne cache aucune incohérence (le `source_hash`, lui, est vérifié par le loader).
    """
    urls: dict[str, str] = {}
    for doc_id in doc_ids:
        chemin = _artefact_audit(data_dir, doc_id, SOURCE_URL, lecture)
        if chemin is None:
            continue
        try:
            brut = chemin.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        url = url_publiable(brut)
        if url is not None:
            urls[doc_id] = url
    return urls


def _publication_evals(data_dir: Path, nom: str,
                       lecture: Lecture | None = None) -> EtatPublication:
    """L'artefact des résultats d'évals, lu **une fois au démarrage** — ou un état typé « aucun ».

    Trois façons de ne pas avoir de run publié, et les trois se disent (AD-16) :

    - **absent** — aucun fichier ; c'est l'état normal d'une image où aucun gate `full` n'a tourné ;
    - **illisible** — les octets ne se lisent pas, ou ne sont pas du JSON (permissions, fichier
      tronqué en cours d'écriture, encodage) ;
    - **hors schéma** — c'est du JSON, mais pas cet objet-là (version antérieure, champ manquant,
      valeur hors bornes).

    Les deux dernières sont **réellement** distinguées, par un `json.loads` avant la validation :
    `model_validate_json` les confondait, si bien qu'un JSON cassé — la cause que le libellé nomme —
    ressortait `hors_schema` et qu'`illisible` n'était atteignable que par une erreur d'entrée-sortie.
    Un état publié qui ne peut pas décrire sa propre cause ne vaut guère mieux qu'un silence.

    Aucune ne rend 5xx, aucune n'invente un chiffre, et aucune n'est confondue avec « un run vert
    sans limites ». Comme `report.json` et `dictionary.json` : lu au démarrage, jamais par requête
    (AD-7).
    """
    import json

    chemin = data_dir / nom
    if lecture is not None:
        chemin = lecture.reel(chemin)
    if not chemin.is_file():
        return EtatPublication(publie=False, raison="absent")
    try:
        brut = json.loads(chemin.read_bytes())
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        LOG.warning("publication d'évals illisible : %s", type(exc).__name__)
        return EtatPublication(publie=False, raison="illisible")
    try:
        publication = PublicationEvals.model_validate(brut)
    except ValueError as exc:
        LOG.warning("publication d'évals hors schéma : %s", _first_error_public(exc))
        return EtatPublication(publie=False, raison="hors_schema")
    return EtatPublication(publie=True, publication=publication)


def _first_error_public(exc: ValueError) -> str:
    """Premier message de validation, sans jamais publier un chemin de `data/`."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        items = errors()
        if items:
            return str(items[0].get("msg", ""))
    return type(exc).__name__


def _pdf_sources(data_dir: Path, corpus: Corpus, lecture: Lecture) -> dict[str, VerifiedSource]:
    """Projette les PDF déjà validés par le loader, sans toucher au cœur du corpus.

    Un document servi a déjà passé le hash de la première source présente dans
    ``SOURCE_FILES``. Rejouer exactement cette sélection permet au lecteur de
    mémoriser le chemin choisi et ses octets dans le même repère. Le renderer sert ensuite ce
    tampon immuable et ne relit jamais le chemin vivant par requête.
    """
    sources: dict[str, VerifiedSource] = {}
    for doc_id in corpus.served:
        entry = corpus.manifest.get(doc_id)
        if entry is None:
            continue
        for source_name in SOURCE_FILES:
            path = data_dir / doc_id / source_name
            payload = lecture.octets(path)
            if payload is None:
                continue
            if source_name == "source.pdf":
                # Le renderer sert ce tampon immuable : aucune seconde traversée de `courant`, ni
                # aucune reconstruction ultérieure de la génération pincée, ne peut changer les
                # octets rendus entre la sélection et la requête HTTP.
                sources[doc_id] = VerifiedSource(
                    path=lecture.reel(path), sha256=entry.source_hash, payload=payload)
            break
    return sources


@dataclass(frozen=True)
class SurfacesLues:
    """Tout ce que le démarrage lit dans `data/` — **d'une seule génération** (story 4.5, N1).

    Le regroupement n'est pas cosmétique : c'est ce qui rend la passe rejouable telle quelle sur un
    repère neuf quand la génération pincée a été reconstruite sous elle. Rien de coûteux et de non
    lu n'y entre (ni client, ni renderer, ni index) — les rejouer serait payer deux fois ce qui ne
    dépend pas du disque.
    """

    corpus: Corpus
    dictionnaire: Dictionnaire
    dictionnaires: dict[str, Dictionnaire]
    doc_ids_audit: list[str]
    rapports: dict[str, Report]
    alertes_rapports: list[Alerte]
    sources: dict[str, str]
    pdf_sources: dict[str, VerifiedSource]
    publication: EtatPublication


def _lire_les_surfaces(data_dir: Path, settings: Settings, contexte: GateContext,
                       lecture: Lecture) -> SurfacesLues:
    """La passe de lecture du démarrage, jouée **entièrement** à travers un repère unique.

    Dette D1 refermée (story 4.5) : la disjonction d'AD-7 a **trois** termes, et c'est
    `Settings.deroger_au_gate` qui les combine — `ALLOW_UNGATED` **ou** `ENV=dev`. En `prod`, les
    deux sont faux et la fermeture de l'AC 1.10 est intacte.
    """
    corpus = load_corpus(data_dir, allow_ungated=settings.deroger_au_gate, current=contexte,
                         perimetre_max_chars=settings.perimetre_max_chars,
                         raison_max_chars=settings.raison_publiable_max_chars, lecture=lecture)
    # Le `doc_id` que le pipeline du guide lui appliquera (revue Codex 2.1, B3) : le verrou
    # `corpus_ok` exige l'empreinte de **ce** document, pas celle d'un document quelconque du corpus.
    dictionnaire = load_dictionary(data_dir, corpus, settings.guide_doc_id, lecture=lecture)
    dictionnaires = {
        doc_id: load_dictionary(data_dir, corpus, doc_id, lecture=lecture)
        for doc_id, document in corpus.documents.items()
        if document.kind == "contrat"
    }
    # Les quarantaines sont connues du loader mais ne sont jamais dans ``documents``. Leurs seuls
    # artefacts d'audit (rapport et URL publique filtrée) peuvent néanmoins être lus ici, une fois.
    doc_ids_audit = _doc_ids_audit(corpus)
    rapports, alertes_rapports = _rapports(
        data_dir, doc_ids_audit, lecture,
        raison_max_chars=settings.raison_publiable_max_chars)
    return SurfacesLues(
        corpus=corpus, dictionnaire=dictionnaire, dictionnaires=dictionnaires,
        doc_ids_audit=doc_ids_audit, rapports=rapports, alertes_rapports=alertes_rapports,
        sources=_sources(data_dir, doc_ids_audit, lecture),
        pdf_sources=_pdf_sources(data_dir, corpus, lecture),
        publication=_publication_evals(data_dir, settings.evals_publication_file, lecture))


def _audit_sink(settings: Settings) -> AuditSink:
    """L'audit exact hors production, la projection sinon (correctif du tour 2, défaut 9).

    L'enveloppe exacte porte question, historique et blocs : elle est **conservée** sur la machine,
    jamais publiée — la distinction est celle d'AD-15, et le fichier reste en 0600, borné en taille
    et en rétention (`llm_audit_max_bytes`, `llm_audit_retention_files`). Un chemin d'audit qu'on ne
    peut pas ouvrir n'est pas une raison d'arrêter de servir : le service repart sur la projection,
    et le dit dans le journal.
    """
    if not settings.audit_exact_actif:
        return ProjectionAuditSink()
    try:
        return JsonlAuditSink(settings.llm_audit_path,
                              max_bytes=settings.llm_audit_max_bytes,
                              retention_files=settings.llm_audit_retention_files)
    except (OSError, ValueError):
        logging.getLogger("foyer.audit").warning(
            "audit exact indisponible : projection seule")
        return ProjectionAuditSink()


def _caches(settings: Settings, client: LlmClient
             ) -> tuple[CacheDeReponses | None, MaintienDesPrefixes | None]:
    """Arme les deux caches de la story 5.6, ou aucun. La clé fournisseur est la condition commune.

    **Sans clé, rien n'est armé** — ni le cache de réponses, ni le maintien. C'est la règle que
    `/sante` applique déjà à `ok` : un service qui ne peut appeler personne ne peut rien économiser.
    Elle a un second effet, voulu : toute exécution hors ligne — la suite hermétique, un `uvicorn` de
    lecture, un script de démonstration sans clé — ne laisse aucun état sur disque qu'elle n'a pas
    demandé, et deux requêtes identiques y restent deux requêtes.

    Le maintien exige en plus son propre réglage, faux par défaut : il n'a de sens que sous
    `--min-instances=1`, drapeau que `deploy.yml` décide seul et à côté duquel il pose la variable.
    """
    if cle_absente(settings) or not settings.anthropic_api_key.strip():
        return None, None
    cache = (CacheDeReponses(CACHE_REPONSES_DIR,
                             ttl_s=settings.response_cache_ttl_s,
                             max_entries=settings.response_cache_max_entries,
                             max_bytes=settings.response_cache_max_bytes)
             if settings.response_cache_enabled else None)
    if not settings.prefix_keepalive_enabled:
        return cache, None
    registre = RegistreDesPrefixes(settings.prefix_keepalive_max_prefixes)
    client.prefix_registry = registre
    maintien = MaintienDesPrefixes(
        client=client, registre=registre,
        intervalle_s=settings.prefix_keepalive_s,
        max_cout_eur_par_jour=settings.prefix_keepalive_max_cost_eur_per_day,
        # Un maintien est un appel comme un autre du point de vue du fournisseur : il emprunte le
        # même délai d'appel qu'AD-16 borne, jamais un délai qui lui serait propre.
        timeout_s=settings.llm_timeout_s)
    return cache, maintien


def construire_etat(settings: Settings, *, data_dir: Path | None = None) -> EtatApp:
    """Charge tout ce qui est constant pour la vie du process (AD-7, AD-9, reprise 1.6)."""
    data_dir = DATA_DIR if data_dir is None else data_dir
    # **Le démarrage du service est un entrypoint de lecture de production** (patch croisé 2/3,
    # `N3-LECTURE-ROOTLESS`) : il exige une racine installée avant de lire quoi que ce soit. Sans
    # cela, un `data-dir` jamais installé se lisait rootless et rendait un corpus **vide sans
    # refuser** — un service qui démarre en affirmant ne rien avoir à servir, au lieu de dire qu'il
    # ne sait pas lire son espace.
    exiger_racine_installee(data_dir)
    digest_pipeline = pipeline_digest()
    digest_prompts = prompts_digest()
    # `GateContext` décrit l'image en cours : sans lui, le loader ne peut pas voir qu'un gate a été
    # obtenu avec un autre code ou d'autres modèles (`gate_perime`, AD-7).
    # `gate_thresholds()` et non `thresholds()` : le contexte de gate ne porte que les seuils qui
    # peuvent changer une claim, un verdict ou une citation (story 5.6, T20). `/api/v1/sante`
    # continue de publier `thresholds()` en entier — la transparence est ailleurs que dans la
    # fraîcheur, et les trois interrupteurs d'exploitation qui diffèrent entre le poste de mesure et
    # l'image de production périmaient sinon *tous* les gates.
    contexte = GateContext(pipeline_digest=digest_pipeline, prompts_digest=digest_prompts,
                           model_ids=dict(TIERS), pipeline_settings=settings.gate_thresholds(),
                           # Story 4.5 : la révision qui tourne, telle que le service la connaît.
                           # Un gate `full` d'un autre commit part en quarantaine (`gate_perime`).
                           candidate_revision=settings.git_sha, env=settings.env)
    # Dette D1 refermée (story 4.5) : la disjonction d'AD-7 a **trois** termes, et c'est
    # `Settings.deroger_au_gate` qui les combine — `ALLOW_UNGATED` **ou** `ENV=dev`. En `prod`, les
    # deux sont faux et la fermeture de l'AC 1.10 est intacte.
    # **Une seule génération pour toute la passe de démarrage** (story 4.5, N1). Le service résolvait
    # une quarantaine de cibles couvertes — manifest, puis document, overlay, structure, sommaire,
    # rapport, dictionnaires, publication d'évals — et relisait le pointeur à *chaque* appel
    # système : une bascule tombant entre deux de ces résolutions rendait un état composé de deux
    # générations, que le contrôle d'empreinte ne voyait pas puisqu'il portait sur des octets qu'un
    # second `open()` avait pu remplacer. `relire` pince une génération, joue la passe entière à
    # travers elle, et la rejoue si le repère a été périmé sous elle (deux bascules).
    surfaces = relire(data_dir, lambda lecture: _lire_les_surfaces(
        data_dir, settings, contexte, lecture))
    corpus = surfaces.corpus
    dictionnaire = surfaces.dictionnaire
    dictionnaires = surfaces.dictionnaires
    doc_ids_audit = surfaces.doc_ids_audit
    rapports, alertes_rapports = surfaces.rapports, surfaces.alertes_rapports
    erreurs_rapports = _erreurs_rapports(doc_ids_audit, rapports, alertes_rapports)
    sources = surfaces.sources
    pdf_sources = surfaces.pdf_sources
    page_renderer = PageRenderer(
        max_lines=settings.pdf_highlight_max_lines,
        max_blocks=settings.pdf_highlight_max_blocks,
        concurrency=settings.pdf_render_concurrency,
        cache_pages=settings.pdf_render_cache_pages,
        dpi=settings.pdf_render_dpi,
        max_pixels=settings.pdf_render_max_pixels,
        queue_timeout_s=settings.pdf_render_queue_timeout_s)
    alertes_ungated = _alerte_ungated(settings)
    if alertes_ungated:
        # AD-7 : une incohérence est visible, jamais muette. L'alerte de `/sante` est lue par la page
        # d'accueil ; ce `warning` est lu par celui qui regarde le journal de démarrage du conteneur —
        # c'est-à-dire par celui qui vient de déployer, au moment où il peut encore le défaire.
        LOG.warning(
            "ungated_refuse_en_production : ALLOW_UNGATED=true posé avec ENV=prod — la dérogation "
            "est refusée (AC 1.10) ; un document sans gate valide reste en quarantaine")
    # Même raison que l'avertissement d'`ungated` : celui qui regarde le journal de démarrage est
    # celui qui vient de déployer. Une seule couture journalise l'état complet ; composer les
    # alertes HTTP reste pur et ne duplique plus la ligne.
    _journaliser_dictionnaire(dictionnaire)
    client = LlmClient(settings, audit_sink=_audit_sink(settings))
    cache_reponses, maintien = _caches(settings, client)
    return EtatApp(
        settings=settings, corpus=corpus,
        # Convention Seuils : les trois budgets de projection de l'index viennent de la
        # configuration publiée, jamais d'un nombre recopié dans la couche `corpus`.
        index=Index(corpus, excerpt_max_chars=settings.excerpt_max_chars),
        # AD-10/AD-15 : le sink projeté ne conserve jamais question, historique ni blocs —
        # l'enveloppe exacte est détruite dès que hashes, tailles et IDs sont projetés. C'est le
        # défaut en production, et c'était le seul comportement possible jusqu'au correctif du
        # tour 2 : le témoin qui porte le plancher était alors le seul chemin **sans** audit exact,
        # et trois enquêtes ont dû déduire ce qu'un fichier aurait dit. Hors production, l'audit
        # exact est écrit sur disque, en 0600, taille et rétention bornées — conservé, jamais
        # publié, la distinction même qu'AD-15 fait.
        client=client,
        cache_reponses=cache_reponses, maintien_prefixes=maintien,
        limiter=RateLimiter(settings),
        followup_limiter=RateLimiter(
            settings, per_minute="conversation_rate_limit_per_minute",
            per_day="conversation_rate_limit_per_day"),
        pipeline_digest_hex=digest_pipeline,
        prompts_digest_hex=digest_prompts, conversation_secret=_conversation_secret(settings),
        dictionnaire=dictionnaire,
        dictionnaires=dictionnaires,
        reports=rapports, report_errors=erreurs_rapports, source_urls=sources,
        pdf_sources=pdf_sources, page_renderer=page_renderer,
        publication_evals=surfaces.publication,
        alerts=_alerte_fournisseur(settings)
        + _alertes(corpus, raison_max_chars=settings.raison_publiable_max_chars)
        + alertes_rapports + alertes_ungated
        + _alertes_dictionnaire(
            dictionnaire, raison_max_chars=settings.raison_publiable_max_chars))
