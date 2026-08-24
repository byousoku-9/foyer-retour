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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.app.api.limiter import RateLimiter
from server.app.api.schemas import Alerte
from server.app.config import REPO_ROOT, Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.ingest import GateContext
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines.guide import repondre_guide

DATA_DIR = REPO_ROOT / "data"
DICTIONARY = "dictionary.json"


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


def _alertes(corpus: Corpus) -> list[Alerte]:
    """Les alertes des documents servis (AD-7), et les documents que le chargement a écartés."""
    alertes = [Alerte(doc_id=doc_id, alerte=a)
               for doc_id in sorted(corpus.alerts) for a in corpus.alerts[doc_id]]
    alertes += [Alerte(doc_id=doc_id, alerte="quarantaine", detail=raison)
                for doc_id, raison in sorted(corpus.quarantine.items())]
    return alertes


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
    return EtatApp(
        settings=settings, corpus=corpus, index=Index(corpus), client=LlmClient(settings),
        limiter=RateLimiter(settings), pipeline_digest_hex=digest_pipeline,
        prompts_digest_hex=digest_prompts, dictionary_validated=_dictionnaire_valide(data_dir),
        alerts=_alertes(corpus))
