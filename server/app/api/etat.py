"""Ce que l'application charge **une fois au démarrage**, et que chaque requête relit sans le refaire.

AD-7 : « `corpus` charge au démarrage en lecture seule ». AD-9 : le client Claude est async et
construit une fois. Et la reprise différée de la story 1.5 ajoute les deux digests : `pipeline_digest()`
et `prompts_digest()` relisent toute l'arborescence du code — les calculer par requête coûterait des
dizaines de lectures de fichiers, et les laisser au repli mémoïsé du pipeline ferait servir des
empreintes périmées par une image dont le code aurait changé à chaud, **sans que rien ne le dise**.

Rien ici n'est muté par une requête (convention « État & transversal » : aucune mutation d'état hors
mémoire de process). Le seul objet vivant est le limiteur, dont c'est la raison d'être.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.app.api.limiter import RateLimiter
from server.app.api.schemas import Alerte
from server.app.config import REPO_ROOT, Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.ingest import GateContext, Report
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines.guide import repondre_guide
from server.app.pipelines.sinistre import run as executer_sinistre

# Logger de module, comme `api/main`, `api/request_id` et `api/errors` (`foyer.*`) : un
# `getLogger("foyer")` posé en ligne d'appel n'aurait pas de nom propre, donc pas de filtre possible,
# et un test ne saurait pas quoi capturer.
LOG = logging.getLogger("foyer.etat")

DATA_DIR = REPO_ROOT / "data"
DICTIONARY = "dictionary.json"
RAPPORT = "report.json"
SOURCE_URL = "source.url"
# AD-7 : `source.url` peut porter l'URL publique **ou** l'URL `gs://` de la copie privée de
# secours. Seule la première se publie : l'autre ne mène nulle part pour un lecteur, et annoncer un
# bucket privé sur une page publique n'apprendrait rien à personne d'utile.
SCHEMAS_PUBLIABLES = ("https://", "http://")
# Une URL de source tient largement là-dedans (celle du contrat AXA fait 170 caractères) ; au-delà,
# ce n'est plus une URL, c'est un fichier qu'on recopierait dans une réponse publique.
SOURCE_URL_MAX = 2048


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
        if url.lower().startswith(SCHEMAS_PUBLIABLES):
            return url
    return None


@dataclass
class EtatApp:
    """L'état de démarrage, posé sur `app.state.foyer`."""

    settings: Settings
    corpus: Corpus
    index: Index
    client: LlmClient
    limiter: RateLimiter
    pipeline_digest_hex: str
    prompts_digest_hex: str
    dictionary_validated: bool = False
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
    # `doc_id` → URL publique de la source (AD-7, `data/{doc_id}/source.url`). Lue au démarrage
    # comme tout le reste : `GET /api/v1/documents` ne touche pas `data/`.
    source_urls: dict[str, str] = field(default_factory=dict)
    alerts: list[Alerte] = field(default_factory=list)

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


def _alertes(corpus: Corpus) -> list[Alerte]:
    """Les alertes des documents servis (AD-7), et les documents que le chargement a écartés."""
    alertes = [Alerte(doc_id=doc_id, alerte=a)
               for doc_id in sorted(corpus.alerts) for a in corpus.alerts[doc_id]]
    alertes += [Alerte(doc_id=doc_id, alerte="quarantaine", detail=raison)
                for doc_id, raison in sorted(corpus.quarantine.items())]
    return alertes


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


def _dictionnaire_valide(data_dir: Path) -> bool:
    """AD-5 : tant qu'aucun humain n'a validé le dictionnaire, le court-circuit « zéro hit » dort.

    Absent (c'est le cas jusqu'à la story 2.1) ou illisible ⇒ `false`. Jamais une exception au
    démarrage : un dictionnaire manquant désactive une optimisation, il n'empêche pas de servir.

    Le `is True` n'est pas de la coquetterie (revue Codex 1.6, M1) : `bool("false")` vaut `True`, et
    `validated` est écrit par un générateur (`enrich_dictionary`, story 2.1) puis relu ici. Un champ
    rendu en chaîne au lieu d'un booléen aurait fait annoncer à `/sante` un dictionnaire validé, et
    ré-armé le court-circuit « zéro hit » qu'AD-5 tient désactivé tant qu'un humain n'a pas signé.
    Le seul `true` JSON strict compte.
    """
    chemin = data_dir / DICTIONARY
    if not chemin.is_file():
        return False
    try:
        return json.loads(chemin.read_bytes()).get("validated", False) is True
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        return False


def _rapports(data_dir: Path, doc_ids: list[str]) -> tuple[dict[str, Report], list[Alerte]]:
    """Les rapports d'ingestion des documents **servis**, lus au démarrage (AD-7/AD-8, D9).

    Comme `dictionary.json`, un rapport **absent** ne fait pas tomber le démarrage : AD-8 fait du
    rapport un artefact d'ingestion, et un document peut être servi avant qu'on l'ait écrit (le
    guide l'a été en 1.1). Ce qui change ici, c'est un fichier **présent et invalide** : il produit
    l'alerte `rapport_illisible` sur `/api/v1/sante` (AD-7 : une incohérence est visible, jamais
    muette) et, sur la route, un 400 — la même réponse qu'un document inconnu, puisqu'il n'y a rien
    d'honnête à publier.

    Seuls les documents servis sont lus : un document en quarantaine n'est pas chargé (AD-7), et
    publier son rapport laisserait croire qu'il l'est.
    """
    rapports: dict[str, Report] = {}
    alertes: list[Alerte] = []
    for doc_id in doc_ids:
        chemin = data_dir / doc_id / RAPPORT
        if not chemin.is_file():
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
        rapports[doc_id] = rapport
    return rapports, alertes


def _sources(data_dir: Path, doc_ids: list[str]) -> dict[str, str]:
    """L'URL publique de chaque document servi, lue **au démarrage** (AD-7).

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
        chemin = data_dir / doc_id / SOURCE_URL
        if not chemin.is_file():
            continue
        try:
            brut = chemin.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        url = url_publiable(brut)
        if url is not None:
            urls[doc_id] = url
    return urls


def construire_etat(settings: Settings, *, data_dir: Path | None = None) -> EtatApp:
    """Charge tout ce qui est constant pour la vie du process (AD-7, AD-9, reprise 1.6)."""
    data_dir = DATA_DIR if data_dir is None else data_dir
    digest_pipeline = pipeline_digest()
    digest_prompts = prompts_digest()
    # `GateContext` décrit l'image en cours : sans lui, le loader ne peut pas voir qu'un gate a été
    # obtenu avec un autre code ou d'autres modèles (`gate_perime`, AD-7).
    contexte = GateContext(pipeline_digest=digest_pipeline, prompts_digest=digest_prompts,
                           model_ids=dict(TIERS))
    corpus = load_corpus(data_dir, allow_ungated=bool(settings.allow_ungated), current=contexte)
    rapports, alertes_rapports = _rapports(data_dir, corpus.served)
    sources = _sources(data_dir, corpus.served)
    alertes_ungated = _alerte_ungated(settings)
    if alertes_ungated:
        # AD-7 : une incohérence est visible, jamais muette. L'alerte de `/sante` est lue par la page
        # d'accueil ; ce `warning` est lu par celui qui regarde le journal de démarrage du conteneur —
        # c'est-à-dire par celui qui vient de déployer, au moment où il peut encore le défaire.
        LOG.warning(
            "ungated_refuse_en_production : ALLOW_UNGATED=true posé avec ENV=prod — la dérogation "
            "est refusée (AC 1.10) ; un document sans gate valide reste en quarantaine")
    return EtatApp(
        settings=settings, corpus=corpus, index=Index(corpus), client=LlmClient(settings),
        limiter=RateLimiter(settings), pipeline_digest_hex=digest_pipeline,
        prompts_digest_hex=digest_prompts, dictionary_validated=_dictionnaire_valide(data_dir),
        reports=rapports, source_urls=sources,
        alerts=_alertes(corpus) + alertes_rapports + alertes_ungated)
