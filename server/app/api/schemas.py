"""AD-11 — Le contrat HTTP, typé. Aucune route n'improvise sa forme d'entrée ni de sortie.

Entrée (`ChatRequest`) : les bornes d'AD-11 sont portées par le schéma, donc refusées par pydantic
avant d'atteindre la route — `question ≤ 1 000`, `Turn.texte ≤ 2 000` (déjà dans le domaine),
`variant` dans la liste des variantes existantes (`null` garde le défaut retrieval versionné). Le nombre de
tours d'historique, lui, est un seuil
de `config.py` : il ne peut pas vivre dans une annotation de classe, et `borner()` l'applique à
l'entrée de la route — **jamais** en tronquant (AD-11 : « 400 au-delà, jamais tronqué côté serveur »
— tronquer perdrait précisément le tour qui porte l'anaphore).

Le champ `contexte` de l'ancien contrat est **ignoré** : `extra="ignore"` le laisse passer sans le
lire, sans que le serveur ne fasse mine de s'en servir. Que ce soit clair : cela ne rend pas le
`chat.js` livré compatible pour autant — il envoie `profil` en **chaîne** (`decrireProfil()`) et
relit `j.reponse`, deux choses que ce contrat ne connaît pas. Le raccord du front est la story 1.7 ;
`extra="ignore"` ne fait qu'éviter qu'un champ en trop soit une erreur.

Sortie (`ChatResponse`) : la forme exacte d'AD-11, `answer` et `trace` compris — l'`Answer` intégral
est publié parce que le front en lit `unknown`, `rejected_claims` et `reason` (FR3), et la `Trace`
parce qu'AD-10 veut qu'une réponse soit explicable par celui qui la reçoit.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from server.app.config import Settings
from server.app.domain.answer import Answer, AnswerSegment
from server.app.domain.conversation import (
    FACT_KEY_MAX,
    ConversationAction,
    FactConflict,
    FactEvent,
    TargetQuestion,
    VerdictTurn,
)
from server.app.domain.document import Bbox, DOC_ID_MAX, DOC_ID_PATTERN
from server.app.domain.errors import InvalidRequest
from server.app.domain.langue import normaliser_langue_forcee
from server.app.domain.profil import Profil
from server.app.domain.question import Faits, Turn
from server.app.domain.ingest import Gate
from server.app.domain.trace import Trace
from server.app.pipelines.guide import VARIANTS

VIA = "api/v1"
# Story 5.6 (T5) : la même réponse, servie du cache interne au lieu d'être recalculée. Le champ
# `via` existait pour dire **par où** une réponse arrive ; il le dit désormais aussi quand elle
# n'arrive de nulle part. C'est le seul signal de premier niveau du cache : la trace publiée reste
# celle de la requête qui a payé, avec son `request_id` et son `total_cost_eur` d'origine, et le
# journal de cette requête-ci porte `cost_eur=0.0`.
VIA_CACHE = "cache"

# Convention Nommage du spine : « `doc_id` slug (`axa-lu-optihome-2017`, `lux-guide`) ». Le motif et
# la borne viennent du domaine `Document`, puis sont réexportés ici pour les contrats HTTP : un
# document que le domaine accepte doit rester adressable par l'API.


class RequeteAvecLangue(BaseModel):
    """Partie commune des corps HTTP qui peuvent forcer une langue de réponse."""

    lang: str | None = None

    @field_validator("lang")
    @classmethod
    def _langue_servie(cls, v: str | None) -> str | None:
        # La `ValueError` est volontaire : pydantic conserve ainsi le chemin exact `body.lang`.
        return normaliser_langue_forcee(v)


class ChatRequest(RequeteAvecLangue):
    """`POST /api/v1/chat` (et son alias `/chat`)."""

    model_config = ConfigDict(extra="ignore")  # `contexte` de l'ancien contrat : reçu, jamais lu

    question: str = Field(min_length=1, max_length=1000)
    profil: Profil = Field(default_factory=Profil)
    historique: list[Turn] = Field(default_factory=list)
    # AD-1 : « un `pipeline.variant` inconnu ⇒ 400 ». La liste est importée du pipeline pour que les
    # implémentations de *retrouver* et le contrat HTTP ne puissent pas diverger.
    variant: str | None = Field(default=None, pattern=f"^({'|'.join(sorted(VARIANTS))})$")

    @field_validator("variant")
    @classmethod
    def _variante_par_defaut(cls, v: str | None) -> str | None:
        """Le client historique envoyait `null` : la route le résout sur le triplet runtime."""
        return v

    @field_validator("question")
    @classmethod
    def _question_non_blanche(cls, v: str) -> str:
        """Une question faite d'espaces passe `min_length=1` — et coûterait un appel modèle pour rien."""
        if not v.strip():
            raise ValueError("question vide")
        return v

    def borner(self, settings: Settings) -> None:
        """Borne d'AD-11 qui vit dans `config.py` : le nombre de tours d'historique."""
        if len(self.historique) > settings.historique_max_turns:
            raise InvalidRequest(f"historique de {len(self.historique)} tours : la limite est "
                                 f"{settings.historique_max_turns} (jamais tronqué côté serveur)")


class SourceItem(BaseModel):
    """Une citation **relue du corpus** (AD-3), telle que le front l'affiche sous la phrase."""

    block_id: str
    fiche_id: str | None = None  # `montrerFiche(id)` de `ui.js` ; null pour une FAQ
    titre: str = ""
    url: str | None = None  # lien officiel de la fiche (`Node.sources`), quand il y en a un
    quote: str
    status: str  # "verifiee", ou le `rejection_kind` de la claim écartée


class ChatResponse(BaseModel):
    """La forme d'AD-11, pour **toute** sortie du pipeline : réponse, refus, clarification."""

    texte: str
    segments: list[AnswerSegment] = Field(default_factory=list)
    sources: list[SourceItem] = Field(default_factory=list)
    fiches: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    comparateur: bool = False
    answer: Answer
    via: str = VIA
    trace: Trace


class Alerte(BaseModel):
    """Une alerte portée par un document (AD-7) : `sans_gate`, `gate_perime`, `source_absente`…"""

    doc_id: str
    alerte: str
    detail: str = ""


class EtatDictionnaire(BaseModel):
    """AD-5 : les deux verrous du dictionnaire, et la règle qu'ils décident — publiés, jamais recalculés.

    `validated` (une main a signé) et `corpus_ok` (les empreintes décrivent le corpus servi) sont deux
    faits ; `refus_zero_hit_actif` est la **règle** qui les combine (`validated ∧ corpus_ok`). Les
    trois sont publiés parce que la règle n'a qu'une autorité — le serveur : la page d'accueil lit
    `refus_zero_hit_actif` au lieu de refaire la conjonction, sans quoi une évolution de la règle
    devrait être répercutée dans chaque lecteur, et un lecteur oublié afficherait un refus armé qui
    ne l'est pas (AD-16 : aucun dégradé silencieux, et rien d'affirmé qui ne soit vérifié).
    """

    validated: bool = False
    corpus_ok: bool = False
    refus_zero_hit_actif: bool = False


class EtatCacheReponses(BaseModel):
    """Story 5.6 (T5) — ce que le cache de réponses a fait depuis le démarrage.

    Des **comptes**, jamais une question ni un texte (AD-10, AD-15). `actif=false` dit qu'aucun cache
    n'est armé — sans clé fournisseur, il n'y a rien à économiser — et distingue ce cas de celui d'un
    cache armé qui n'a encore rien servi.
    """

    actif: bool = False
    hits: int = 0
    misses: int = 0
    ecritures: int = 0
    evictions: int = 0
    # Entrées écartées parce qu'illisibles, d'une autre version de schéma ou d'une autre clé. Elles
    # comptent aussi dans `misses` : une entrée douteuse n'est jamais servie « au mieux ».
    invalides: int = 0
    entrees: int = 0


class EtatMaintienPrefixes(BaseModel):
    """Story 5.6 (T5) — ce que le maintien au chaud des préfixes a coûté, publié parce qu'il coûte.

    Un cache qui dépense en silence est un trou de facture : c'est précisément ce que cette story
    ferme. Le coût cumulé est celui du processus courant ; celui du jour se remet à zéro au quantième
    UTC, et `plafond_du_jour_atteint` dit que le maintien s'est **arrêté**, pas qu'il a échoué.
    """

    actif: bool = False
    prefixes: int = 0
    maintiens: int = 0
    # Préfixes servis dont le modèle déclare un TTL plus court que l'intervalle de maintien : ils ne
    # sont pas maintenables, et le dire vaut mieux que de les compter comme chauds.
    ignores: int = 0
    echecs: int = 0
    cout_cumule_eur: float = 0.0
    cout_du_jour_eur: float = 0.0
    plafond_du_jour_atteint: bool = False


class EtatCaches(BaseModel):
    """Les deux caches de la story 5.6, côte à côte : celui d'ici et celui du fournisseur."""

    reponses: EtatCacheReponses = Field(default_factory=EtatCacheReponses)
    prefixes: EtatMaintienPrefixes = Field(default_factory=EtatMaintienPrefixes)


class SanteResponse(BaseModel):
    """`GET /api/v1/sante` (et son alias `/sante`) — sondé par `testerApi()` à chaque page."""

    ok: bool
    version: str
    documents_servis: list[str] = Field(default_factory=list)
    # `null` dès qu'un document servi n'a pas de gate : publier un profil alors qu'un document n'est
    # pas validé serait la bascule silencieuse qu'AD-11 interdit.
    gate_profile: str | None = None
    # Le nombre de cas relus à la main qui fondent ce profil, somme des `Gate.cases` des documents
    # servis — `null` exactement quand `gate_profile` l'est. L'accueil affiche « vertical — 2 cas
    # relus à la main » et n'écrit « 2 » nulle part : le compte vient du serveur, qui le tient du run
    # qui l'a constaté (AD-7 : `manifest.gate` n'est écrit que par `evals run --gate`).
    gate_cases: int | None = None
    # La relecture de ces cas est-elle contresignée par un humain ? — `null` exactement quand
    # `gate_profile` l'est (amendement AD-7 / AD-14, revue Codex 1.10 tour 2). C'est ce champ, et non
    # le nom du profil, qui autorise l'accueil à écrire « relus à la main » : `vertical` dit quelle
    # politique de mesure a tourné, celui-ci dit si la relecture qu'AD-14 met dans sa définition a
    # bien été faite par la personne à qui `epics.md` l'attribue.
    gate_countersigned: bool | None = None
    # Story 4.5 / AC 3 — la **troisième** réserve, à côté de `gate_countersigned` et de
    # `dictionary.validated`. AD-14 la fixe : « les verdicts ne sont pas validés par un expert
    # assurance », et `Verite.validated_by_expert` est contraint à `False` dans le schéma des cas.
    # Elle est publiée quand même, et publiée **ici**, parce qu'une promesse qu'on ne tient pas doit
    # être lisible à l'endroit où l'on dit ce qu'on tient — pas seulement dans un document annexe.
    # `null` exactement quand `gate_profile` l'est : sans gate, il n'y a pas de verdict à qualifier.
    gate_validated_by_expert: bool | None = None
    dictionary: EtatDictionnaire = Field(default_factory=EtatDictionnaire)
    alerts: list[Alerte] = Field(default_factory=list)
    thresholds: dict[str, float | int] = Field(default_factory=dict)
    # Story 5.6 (T5) : les deux caches décidés le 03/09. Ce sont les seuls champs de `/sante` qui ne
    # soient pas calculés au démarrage — mais ils ne coûtent rien pour autant : ce sont des
    # compteurs de processus, lus tels quels. Ils sont ici parce que c'est ici qu'on dit ce que le
    # service fait de l'argent, à côté des seuils qui le bornent.
    caches: EtatCaches = Field(default_factory=EtatCaches)


# --- Story 1.9 : l'outil sinistre (AD-11) ---------------------------------

class DocumentItem(BaseModel):
    """Une entrée de `GET /api/v1/documents` : l'audit d'un document connu du loader.

    `source_url` s'ajoute aux cinq champs du contrat pour une raison qu'AD-7 impose : le PDF d'un
    assureur n'est **pas** redistribué par ce service, et la seule chose qu'on puisse offrir au
    lecteur est le lien vers sa source publique. Sans lui, « édition juin 2017 — actualité non
    vérifiée » serait invérifiable par celui à qui on le dit.

    Un document en quarantaine reste absent de ``Corpus.documents`` : cette projection ne le rend
    ni chargeable ni soumettable. Elle expose seulement les faits déjà validés au démarrage. Quand
    ``document.json`` n'a pas été retenu par le loader, le titre de repli est le ``doc_id`` et le
    ``kind`` reste indisponible plutôt que d'être relu ou inventé.
    """

    doc_id: str
    title: str
    edition: str | None = None
    kind: str | None = None  # `guide` ou `contrat` seulement quand le document a été validé
    status: Literal["servi", "quarantaine"]
    selectionnable: bool = False
    raison: str | None = None
    source_url: str | None = None
    source_hash: str | None = None
    ingest_fingerprint: str | None = None
    document_hash: str | None = None
    overlay_hash: str | None = None
    gate: Gate | None = None
    # `disponible`, `absent`, `illisible` ou `etranger`. Un rapport absent n'est donc jamais
    # confondu avec un rapport valide dont la liste de checks est vide.
    report_status: Literal["disponible", "absent", "illisible", "etranger"] = "absent"


class SinistreRequest(RequeteAvecLangue):
    """`POST /api/v1/sinistre` — les quatre champs d'AD-11, et pas un de plus.

    **Ce que le corps ne porte pas, et pourquoi (D1).** `pipelines.sinistre.run(..., dossier)` dit ce
    que l'appelant détient déjà (conditions particulières, options, avenants, date d'effet) et décide
    de ce que `missing` annonce et de ce qu'`ask_client` réclame. La route ne l'expose **pas** :
    AD-11 énumère les champs du corps et « une route qui en ajoute un » est une route inventée par
    story ; sur un démonstrateur public non authentifié, un booléen « j'ai les conditions
    particulières » est une auto-déclaration invérifiable qui referme la seule question que l'outil
    sait poser ; et la promesse affichée est « au regard des conditions générales seules ». Le
    paramètre reste au pipeline, pour les évals et l'usage programmatique.

    `faits` est le `Faits` du domaine : sa borne (`description ≤ 2 000`) est donc appliquée par
    pydantic **avant** la route, et un dépassement ressort en 400 par le gestionnaire de validation
    d'AD-16 — jamais tronqué (la fin d'une description de sinistre porte souvent la cause).
    """

    # `extra="forbid"`, et non `extra="ignore"` comme `ChatRequest` (revue Codex 1.9, tour 1, I3).
    # Le contrat de `POST /api/v1/chat` porte une clause d'AD-11 que celui du sinistre n'a pas — le
    # `contexte` de l'ancien site, « reçu, jamais lu » —, et rien de tel n'existe ici : AD-11 énumère
    # `doc_id, question, faits, lang?`, un point. Ignorer un champ en trop laissait le serveur
    # acquiescer en silence à un corps qu'il n'honore pas — un `dossier: true` posté par un appelant
    # qui a lu D1 de travers repartait avec un verdict rendu « conditions générales seules » sans que
    # rien ne le démente. Un champ hors contrat est donc un 400, comme une borne franchie : AD-11 veut
    # « le rejet plutôt que la troncature », et ignorer est une troncature du corps.
    model_config = ConfigDict(extra="forbid")

    # Obligatoire : le corpus servira plusieurs contrats (AD-14, « ≥ 2 contrats »), et laisser la
    # route retomber sur `settings.sinistre_doc_id` ferait répondre sur un document que l'appelant
    # n'a pas demandé. La forme est celle d'un slug — convention Nommage du spine, `[a-z0-9-]+` —
    # et elle est **appliquée**, pas seulement décrite (revue 1.9) : la borne de longueur seule
    # laissait passer `../../etc/passwd`, inoffensif ici (c'est une clé de dictionnaire) mais
    # démenti par le commentaire, et un jour recopié dans un chemin par une route qui viendra.
    doc_id: str = Field(min_length=1, max_length=DOC_ID_MAX, pattern=DOC_ID_PATTERN)
    question: str = Field(min_length=1, max_length=1000)
    faits: Faits
    # **Pas de `variant`** (revue Codex 1.9, tour 1, I3). Le champ existait pour AD-1 (« un
    # `pipeline.variant` inconnu ⇒ 400 »), mais AD-11 ne l'énumère pas dans le corps de cette route,
    # et la story a refusé `dossier` **en invoquant cette énumération** : le garder revenait à
    # opposer AD-11 à un champ et à l'ignorer pour un autre. Depuis la story 4.2d le pipeline en
    # connaît deux (`outils` par défaut, `deterministe` en baseline) : ce corps ne les expose
    # toujours pas, et c'est ce qui fait du **défaut du pipeline** la variante servie en HTTP —
    # une baseline se compare en éval, pas par une requête publique. AD-1 reste
    # servi, et mieux : `extra="forbid"` rend `variant="agentique"` — comme toute variante, connue
    # ou non — en 400 `invalid_request`, avant le premier appel facturé. La sélection de variante
    # reste au pipeline, pour les évals et l'usage programmatique (`pipelines.sinistre.run(variant=…)`),
    # qui sont les seuls appelants qu'AD-1 vise : une baseline se compare en éval, pas par HTTP.

    @field_validator("question")
    @classmethod
    def _question_non_blanche(cls, v: str) -> str:
        """Une question faite d'espaces passe `min_length=1` — et coûterait quatre appels pour rien."""
        if not v.strip():
            raise ValueError("question vide")
        return v

    @field_validator("faits")
    @classmethod
    def _description_non_blanche(cls, v: Faits) -> Faits:
        """Un sinistre sans description n'est pas un sinistre (revue 1.9).

        `Faits.description` n'a qu'un `max_length` : une chaîne vide ou faite d'espaces traversait
        la validation et partait en quatre appels payants pour un `ne_tranche_pas` acquis d'avance —
        exactement le motif qui fait refuser `question` blanche deux lignes plus haut, et exactement
        ce que la page refuse déjà de poster. Le contrôle est ici, dans le **contrat HTTP**, et non
        dans le domaine : les évals et les tests construisent des `Faits` partiels de plein droit,
        et le pipeline n'a pas à leur imposer la borne d'une route.
        """
        if not v.description.strip():
            raise ValueError("description du sinistre vide")
        return v


class ClauseSource(BaseModel):
    """Une clause **relue du corpus** (AD-3), telle que la page l'affiche sous le verdict.

    Les cinq champs d'AD-11 (`block_id`, `page`, `bbox`, `quote`, `status`), plus `line_ids` que le
    contrat de la story nomme, plus deux que D5 justifie :

    - `kind` : UX-DR6 exige que chaque clause affiche son **type** (garantie, exclusion, condition,
      franchise…). Le front n'a pas de corpus et ne peut pas l'inventer ; AD-6 fait de `Block.kind`
      la seule source de typage, et le serveur le lit sur le bloc relu, comme `page` et `bbox` ;
    - `kind_confirmed` : un `kind` non confirmé vaut `applicable="humain"` (AD-6). Afficher
      « garantie » sans dire que le typage n'est pas confirmé donnerait au lecteur une certitude que
      le pipeline n'a pas.

    Rien de ce que le modèle a rendu n'entre ici : `quote` est ré-extraite de `Block.text` aux
    offsets prouvés, `page`, `bbox` et `kind` viennent du bloc du corpus.
    """

    block_id: str
    page: int | None = None
    bbox: Bbox | None = None
    line_ids: list[str] = Field(default_factory=list)
    kind: str
    kind_confirmed: bool = False
    quote: str
    status: str  # "verifiee" : `sources[]` ne porte que les citations de la réponse affichée (AD-11)


class SinistreResponse(BaseModel):
    """La forme d'AD-11, pour **toute** sortie du pipeline sinistre : verdict, refus, `ne_tranche_pas`."""

    answer: Answer
    sources: list[ClauseSource] = Field(default_factory=list)
    via: str = VIA
    trace: Trace


class ConversationView(BaseModel):
    """Projection non décisive de l'état signé, consommée par la page."""

    token: str
    turn: int
    facts: list[FactEvent] = Field(default_factory=list)
    conflicts: list[FactConflict] = Field(default_factory=list)
    questions: list[TargetQuestion] = Field(default_factory=list)
    history: list[VerdictTurn] = Field(default_factory=list)


class SinistreConversationResponse(SinistreResponse):
    conversation: ConversationView


class SinistreFollowupRequest(BaseModel):
    """Suivi sans modèle : le jeton fait autorité après vérification HMAC."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(min_length=1, max_length=DOC_ID_MAX, pattern=DOC_ID_PATTERN)
    token: str = Field(min_length=1, max_length=60_000)
    action: Literal["reponse", "correction", "resolution"] = "reponse"
    value: str | None = Field(None, max_length=500)
    question_id: str | None = Field(None, max_length=80)
    fact_key: str | None = Field(None, max_length=FACT_KEY_MAX)
    replaces_event_id: str | None = Field(None, max_length=80)
    conflict_id: str | None = Field(None, max_length=80)
    chosen_event_id: str | None = Field(None, max_length=80)

    @model_validator(mode="after")
    def _action_valide(self) -> SinistreFollowupRequest:
        # Le modèle du domaine est l'autorité de forme ; ce passage conserve aussi les chemins
        # pydantic du contrat HTTP, donc un corps incomplet est un 400 avant la route.
        ConversationAction.model_validate(self.model_dump(exclude={"doc_id", "token"}))
        return self

    def domain_action(self) -> ConversationAction:
        return ConversationAction.model_validate(self.model_dump(exclude={"doc_id", "token"}))
