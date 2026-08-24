"""AD-11 — Le contrat HTTP, typé. Aucune route n'improvise sa forme d'entrée ni de sortie.

Entrée (`ChatRequest`) : les bornes d'AD-11 sont portées par le schéma, donc refusées par pydantic
avant d'atteindre la route — `question ≤ 1 000`, `Turn.texte ≤ 2 000` (déjà dans le domaine),
`variant` dans la liste des variantes existantes. Le nombre de tours d'historique, lui, est un seuil
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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.app.config import Settings
from server.app.domain.answer import Answer, AnswerSegment
from server.app.domain.errors import InvalidRequest
from server.app.domain.profil import Profil
from server.app.domain.question import Turn
from server.app.domain.trace import Trace
from server.app.pipelines.guide import VARIANT

VIA = "api/v1"


class ChatRequest(BaseModel):
    """`POST /api/v1/chat` (et son alias `/chat`)."""

    model_config = ConfigDict(extra="ignore")  # `contexte` de l'ancien contrat : reçu, jamais lu

    question: str = Field(min_length=1, max_length=1000)
    profil: Profil = Field(default_factory=Profil)
    historique: list[Turn] = Field(default_factory=list)
    lang: str | None = None
    # AD-1 : « un `pipeline.variant` inconnu ⇒ 400 ». La seule variante existante en J+1 est celle du
    # pipeline du guide ; le littéral est importé de lui pour qu'ajouter une variante (baseline « tout
    # en contexte ») se fasse à un seul endroit.
    variant: str | None = Field(default=None, pattern=f"^{VARIANT}$")

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
    """AD-5 : tant que `validated=false`, le court-circuit « zéro hit » reste désactivé."""

    validated: bool = False


class SanteResponse(BaseModel):
    """`GET /api/v1/sante` (et son alias `/sante`) — sondé par `testerApi()` à chaque page."""

    ok: bool
    version: str
    documents_servis: list[str] = Field(default_factory=list)
    # `null` dès qu'un document servi n'a pas de gate : publier un profil alors qu'un document n'est
    # pas validé serait la bascule silencieuse qu'AD-11 interdit.
    gate_profile: str | None = None
    dictionary: EtatDictionnaire = Field(default_factory=EtatDictionnaire)
    alerts: list[Alerte] = Field(default_factory=list)
    thresholds: dict[str, float | int] = Field(default_factory=dict)
