"""AD-10 — La trace est un objet, émise à chaque réponse.

**Ce que la story 2.5 ajoute — et ce qu'elle n'ajoute pas.** `BlocTrace`, `GateTrace` et
`DictionnaireTrace` ne font que **résoudre** ce que la trace nommait déjà : un `block_id` reçoit le
nœud et le titre de fiche auxquels il appartient, le `doc_id` interrogé reçoit le profil du gate qui
l'a validé, et le dictionnaire — dont dépend l'un des deux court-circuits d'AD-5 — dit s'il arme ou
non le refus « zéro hit ». Aucun de ces champs n'est une mesure nouvelle : ils rendent lisible ce que
le serveur savait déjà et que rien ne publiait.

Ils ne portent, littéralement, que des **identifiants**, des **titres de nœuds du corpus** (écrits par
l'ingestion, jamais par un modèle), des **comptes** et des **booléens** — jamais le texte d'un bloc,
jamais une citation, jamais une donnée personnelle au-delà du profil déclaré (AD-10, AD-15). Un titre
de fiche est le seul « texte » qui entre ici, et c'est le même que `sources[].titre` publie déjà au
premier niveau du contrat (AD-11).

Ils vivent dans `Trace`, et non dans `ChatResponse`/`SinistreResponse` : la trace est publiée entière
par les deux routes, l'ajout y est additif, et le contrat de premier niveau n'acquiert aucun champ.
"""

from __future__ import annotations

import math

from pydantic import Field, PrivateAttr, field_validator, model_validator

from .document import DomainModel
from .retrieval import BudgetSnapshot


# Les étapes qui n'appellent **jamais** un modèle. C'est le même fait que `llm.models.STEP_TIERS`
# — leur tier y vaut `None` — énoncé dans la couche que `pipelines` a le droit de lire : la table
# des couches interdit à un pipeline de voir `llm`, et ce fait-là n'appartient pas au fournisseur,
# il appartient à la chaîne. `tests/test_tables_partagees.py` interdit aux deux de diverger.
#
# Ce que la distinction sert (correctif du tour 4) : la deadline protège le budget d'appels. Une
# étape qui ne dépense rien est une **remise**, pas une dépense ; son dépassement se dit, il ne se
# paie pas d'un 503 sur un travail déjà payé.
ETAPES_SANS_APPEL: frozenset[str] = frozenset({"restituer"})


class Usage(DomainModel):
    """AD-9 : usage réel renvoyé par l'API, coût en euros calculé depuis cet usage."""

    input: int = 0
    cached: int = 0
    output: int = 0
    cost_eur: float = 0.0
    cached_response: bool = False
    cost_eur_original: float = 0.0


class CheckResult(DomainModel):
    name: str
    ok: bool = True
    detail: str = ""


class LLMCall(DomainModel):
    """Un appel modèle **réellement émis**, avec le modèle servi et le tier qui l'a choisi.

    Story 4.2e : `StepTrace.tier` est le tier **demandé de l'étape** — celui que la configuration a
    fixé avant qu'elle ne commence. Il ne dit pas ce qu'un appel donné a employé : une étape peut en
    faire plusieurs (les tours d'outils de *retrouver*), et un appelant peut passer un tier qui n'est
    pas celui de l'étape. `model` ne le dit pas non plus — il n'existe aucune table inverse
    modèle → tier, et deux tiers peuvent parfaitement pointer le même modèle dans une matrice de
    réglages. Le tier employé est donc publié ici, à l'endroit qui le connaît.

    `None` n'est pas un défaut mais une absence de mesure (AD-16) : un `LLMCall` construit hors du
    client ne prétend pas connaître son tier.
    """

    model: str
    tier: str | None = None
    ms: int = 0
    usage: Usage = Field(default_factory=Usage)
    cache_read: int = 0
    cache_write: int = 0
    tools: list[str] = Field(default_factory=list)
    call_uid: str = ""
    run_uid: str = ""
    artifact_uid: str = ""
    trusted_line_uids: list[str] = Field(default_factory=list)
    input_sha256: str = ""
    input_bytes: int = 0
    response_sha256: str = ""
    response_bytes: int = 0
    audit_persisted: bool = False


class StepTrace(DomainModel):
    name: str
    tier: str | None = None
    # `None` pour une étape sans préfixe fournisseur ; vrai/faux pour le profil effectivement
    # appliqué au retrieval. Ce fait ne se déduit ni du tier ni de l'usage (petit préfixe non caché).
    prompt_cache: bool | None = None
    mechanism_order: list[str] = Field(default_factory=list)
    ms: int = 0
    usage: Usage = Field(default_factory=Usage)
    opened_block_ids: list[str] = Field(default_factory=list)
    discarded_block_ids: list[str] = Field(default_factory=list)
    # Correctif du tour 2 : ce que l'étape a réellement **consommé** de son budget de lecture.
    # `retrieval_max_tokens` était publié dans `Trace.thresholds`, jamais ce qui en restait — trois
    # runs A16 frôlaient la saturation (99,8 %) et rien dans la trace ne le disait, si bien qu'une
    # enquête a dû le recalculer à la main. Le type existait déjà, il n'était simplement pas remonté.
    # `None` pour toute étape qui ne lit pas le corpus.
    budget_lecture: BudgetSnapshot | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    calls: list[LLMCall] = Field(default_factory=list)
    # Story 4.5, revue croisée B1 : une sortie modèle peut déclarer à la fois une intention refusée
    # et une clarification. Le fait public vit dans ``checks`` ; son texte non fiable ne doit jamais
    # entrer dans la trace sérialisée (AD-10), mais le pipeline guide doit pouvoir le servir si son
    # périmètre tronqué lui interdit de croire ``hors_perimetre``. Un attribut Pydantic privé garde
    # cette donnée strictement entre *comprendre* et son appelant, sans changer le contrat de Trace.
    _clarification_neutralisee: str | None = PrivateAttr(default=None)

    @property
    def clarification_neutralisee(self) -> str | None:
        return self._clarification_neutralisee

    def neutraliser_clarification(self, clarification: str) -> None:
        self._clarification_neutralisee = clarification


class BlocTrace(DomainModel):
    """Un `block_id` de la trace, résolu jusqu'à la fiche qui le porte (story 2.5).

    `StepTrace.opened_block_ids` / `discarded_block_ids` sont des identifiants opaques : à l'écran,
    « lux-guide:farrivee:2 » ne dit pas à l'utilisateur ce qui a été lu. Le nœud parent et son titre
    le disent, et ce sont exactement ceux que `api/presenter._source_item` publie déjà pour les
    sources — même règle de `fiche_id` (`{doc_id}:f…` privé de son préfixe, `None` sinon), pour que
    les deux surfaces ne puissent pas nommer la même fiche de deux façons.

    Jamais le texte du bloc : le titre est celui du **nœud**, écrit par l'ingestion (AD-10).
    """

    block_id: str
    doc_id: str
    node_id: str
    # `None` quand le nœud parent n'est pas une fiche (une FAQ `{doc}:q…`, une page `{doc}:p…`) :
    # il y a un titre à afficher, mais aucune fiche à ouvrir.
    fiche_id: str | None = None
    titre: str = ""


class GateTrace(DomainModel):
    """AD-7 / AD-14 — Ce qui valide le document interrogé, au moment où la réponse est servie.

    `EtatApp.gate_profile` publie déjà un profil sur `/api/v1/sante`, mais il **résume** les documents
    servis : il vaut `null` dès que deux d'entre eux divergent, et il ne dit rien du document qui a
    répondu à *cette* question. La trace, elle, ne parle que d'un document, et c'est ce que le panneau
    « Pourquoi cette réponse » affiche.

    `alerts` voyage **dans le même objet** que `profile`, et ce n'est pas un ornement : c'est ce qui
    interdit de lire le profil seul. Un gate que le loader a neutralisé localement (`sans_gate` :
    empreintes du gate différentes de l'entrée) laisse son `profile` dans le manifest ; le publier
    sans son alerte serait la « bascule silencieuse » d'AD-11, le publier avec elle est un fait
    complet. Les trois champs sont `None` quand l'entrée de manifest n'a pas de gate : absence de
    mesure, jamais une mesure par défaut (AD-16).
    """

    profile: str | None = None
    cases: int | None = None
    countersigned: bool | None = None
    alerts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherence(self) -> GateTrace:
        mesures = (self.profile, self.cases, self.countersigned)
        absentes = tuple(v is None for v in mesures)
        if not (all(absentes) or not any(absentes)):
            raise ValueError(
                "profile, cases et countersigned sont soit tous absents, soit tous présents")
        if self.cases is not None and self.cases < 1:
            raise ValueError("un gate présent exige cases >= 1")
        return self


class DictionnaireTrace(DomainModel):
    """AD-5 — L'état du dictionnaire pour le document interrogé, donc l'état du refus « zéro hit ».

    Les trois premiers booléens décrivent l'artefact : chargé, signé et conforme au corpus. Le
    quatrième décrit le **pré-contrôle** de cette requête : leur conjonction est nécessaire, puis le
    document interrogé et la politique `perimetre_tronque` peuvent encore le désarmer. Faux signifie
    donc que la requête poursuit vers *retrouver* ; cette recherche peut légitimement conclure ensuite
    à une absence `zero_hit`.
    """

    charge: bool = False
    validated: bool = False
    corpus_ok: bool = False
    court_circuit_actif: bool = False

    @model_validator(mode="after")
    def _coherence(self) -> DictionnaireTrace:
        if not self.charge and (self.validated or self.corpus_ok):
            raise ValueError(
                "un dictionnaire non chargé ne peut être ni validé ni conforme au corpus")
        attendu = self.charge and self.validated and self.corpus_ok
        if self.court_circuit_actif and not attendu:
            raise ValueError(
                "court_circuit_actif implique charge ∧ validated ∧ corpus_ok")
        return self


class Trace(DomainModel):
    request_id: str
    pipeline: str
    variant: str = "deterministe"
    # AD-10 : « les logs portent, par requête, `intent`, `found`, `verdict.value`, `reason.kind`,
    # `variants_count`, `blocks_scanned` et `cost_eur` ». Tous ces champs se lisent sur la réponse,
    # sauf `intent` : il est produit par *comprendre*, à l'intérieur du pipeline, et n'apparaît nulle
    # part dans `Answer`. Seule la trace peut le remonter jusqu'à l'API sans lui faire rouvrir une
    # étape. `None` quand *comprendre* n'a pas abouti (échec avant, ou pendant, son appel) : le log
    # dira alors qu'il n'y a pas eu d'intention comprise, plutôt que d'en inventer une.
    intent: str | None = None
    steps: list[StepTrace] = Field(default_factory=list)
    total_cost_eur: float = 0.0
    source_hash: dict[str, str] = Field(default_factory=dict)  # par doc_id
    ingest_fingerprint: dict[str, str] = Field(default_factory=dict)
    pipeline_digest: str = ""
    prompts_digest: str = ""
    thresholds: dict[str, float | int] = Field(default_factory=dict)  # rempli par Settings.thresholds()
    retries: int = 0
    truncations: int = 0
    deadline_remaining_s: float | None = None
    # Story 2.5 — les trois résolutions décrites en tête de module. Les défauts sont ceux d'une trace
    # qui n'a rien à en dire (aucun bloc touché, document hors manifest, pipeline sans dictionnaire) :
    # une liste vide et deux `None`, que le front fait disparaître au lieu de les remplir.
    blocs: list[BlocTrace] = Field(default_factory=list)
    gate: GateTrace | None = None
    dictionnaire: DictionnaireTrace | None = None
    # Correctif du tour 2 : **ce que *comprendre* a décidé, et dont tout le reste dépend.** Les
    # termes de recherche et le découpage en sous-questions sont produits librement par le modèle à
    # chaque appel ; ils déterminent le classement, donc les blocs lus, donc la réponse. Rien ne les
    # publiait — `faits_compris` ne porte ni l'un ni l'autre —, si bien que le rejeu d'un incident
    # était impossible **même avec l'audit** : trois réponses différentes à la même question, sans
    # aucun moyen de dire ce qui avait été cherché. Ce sont les mêmes libellés que ceux déjà servis
    # dans `AbsenceProof.terms_searched` (AD-4) : rien de neuf n'est exposé, c'est la même donnée,
    # publiée sur le chemin nominal et pas seulement sur un refus.
    termes: list[str] = Field(default_factory=list)
    facettes: list[str] = Field(default_factory=list)

    @field_validator("total_cost_eur", mode="before")
    @classmethod
    def _cout_reel(cls, valeur: object) -> object:
        if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
            raise ValueError("total_cost_eur doit être un nombre")
        if not math.isfinite(valeur) or valeur < 0:
            raise ValueError("total_cost_eur doit être fini et positif ou nul")
        return valeur

    @field_validator("thresholds", mode="before")
    @classmethod
    def _seuils_numeriques(cls, valeur: object) -> object:
        if not isinstance(valeur, dict):
            return valeur
        invalides = sorted(
            str(nom) for nom, seuil in valeur.items()
            if isinstance(seuil, bool) or not isinstance(seuil, (int, float))
            or not math.isfinite(seuil)
        )
        if invalides:
            raise ValueError(f"thresholds doit porter des nombres finis : {invalides}")
        return valeur
