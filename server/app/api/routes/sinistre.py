"""AD-11 / AD-16 — `POST /api/v1/sinistre` : la seconde route qui appelle un modèle (FR15/FR23).

Transposition littérale du patron de `routes/chat.py`, dans le même ordre et pour les mêmes raisons :

1. **le limiteur** (AD-13) — dépendance de **routeur**, donc comptée avant que pydantic ne lise le
   corps. La dépendance est celle de `chat.py`, importée telle quelle : deux copies de « l'identité
   client est le premier `X-Forwarded-For` » auraient divergé au premier amendement d'AD-13 ;
2. **les bornes d'entrée** qui ne vivent pas dans le schéma — ici : le document existe-t-il, et
   est-ce un contrat ? Les deux sont levées **avant** le premier appel facturé ;
3. **le budget** (AD-9) — un `RequestBudget` par requête, deadline monotone armée à l'entrée ;
4. **le pipeline**, via `etat.pipeline_sinistre` : `pipelines.sinistre.run` et rien d'autre, aucun
   dispatch par variante (AD-1) ;
5. **la projection d'AD-11** — `answer`, `sources`, `via`, `trace`, avec les citations **relues du
   corpus** au moment de la projection (`api/presenter.clauses_de`) ;
6. **la ligne de log** (AD-10) — des champs, jamais du texte, et surtout jamais la description du
   sinistre.

**Toute** sortie du pipeline est un 200 : un refus, un `ne_tranche_pas`, des clauses toutes
rejetées. Ce sont des résultats, pas des pannes — et `ne_tranche_pas` est le résultat d'une table
qui n'a rien eu à trancher, dit comme tel. Les échecs terminaux remontent en `PipelineError`
jusqu'à l'enveloppe d'AD-16 avec leur `Trace` partielle. **Aucun repli**, ni ici ni sur la page.

**Deux refus en 400, avant tout appel facturé.** Le premier est celui de l'AC (« `doc_id` non servi
⇒ 400 ») : le pipeline lèverait bien `CorpusUnavailable`, mais en 503 — ce qu'AD-16 réserve à une
indisponibilité passagère, alors que le corpus est chargé une fois au démarrage et ne bouge plus.
Un document que ce service ne sert pas est une faute d'appel, et la route la nomme comme telle.
Le second est D3 : un `doc_id` **servi** qui n'est pas un contrat. Soumettre un sinistre au guide
dépenserait quatre appels payants pour un `ne_tranche_pas` acquis d'avance — aucun bloc du guide
n'est typé `garantie|exclusion` —, et AD-11 veut « le rejet plutôt que la troncature » aux bornes
d'entrée. Le message dit pourquoi.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from server.app.api.conversation_token import signer, verifier as verifier_token
from server.app.api.presenter import champs_de_journal, clauses_de
from server.app.api.routes.chat import verifier_quota
from server.app.api.schemas import (
    VIA,
    ConversationView,
    SinistreConversationResponse,
    SinistreFollowupRequest,
    SinistreRequest,
    SinistreResponse,
)
from server.app.domain.conversation import ContinuationState, initialiser
from server.app.domain.errors import InvalidRequest, PipelineError
from server.app.pipelines.sinistre import run_followup

router = APIRouter()

KIND_ATTENDU = "contrat"


@router.post("/sinistre", response_model=SinistreResponse, response_model_exclude_none=False,
             dependencies=[Depends(verifier_quota)])
async def sinistre(request: Request, demande: SinistreRequest) -> SinistreResponse | JSONResponse:
    etat = request.app.state.foyer  # le limiteur a déjà tranché (dépendance `verifier_quota`)

    document = etat.corpus.documents.get(demande.doc_id)
    if document is None:
        # AD-15 : le `doc_id` reçu n'est pas recopié dans le message — c'est une chaîne de l'appelant.
        raise InvalidRequest("document inconnu : ce service ne sert pas ce contrat (la liste des "
                             "documents servis est publiée par GET /api/v1/documents)")
    if document.kind != KIND_ATTENDU:
        # Le message est lu par un appelant, pas par nous : il ne cite ni une décision de spec ni un
        # numéro de story, qui ne veulent rien dire hors de ce dépôt (AD-16 : le message d'un code
        # que l'appelant peut corriger lui est dû, et il doit lui servir).
        raise InvalidRequest("ce document n'est pas un contrat : un sinistre se confronte à des "
                             "conditions générales, et ce document n'en porte aucune (voir "
                             "GET /api/v1/documents, champ `kind`)")

    budget = etat.client.new_budget()  # AD-9 : deadline monotone armée ici, à l'entrée de la route
    answer, trace = await etat.pipeline_sinistre(
        demande.doc_id, demande.question, demande.faits,
        corpus=etat.corpus, index=etat.index, client=etat.client, settings=etat.settings,
        request_id=request.state.request_id, lang=demande.lang, budget=budget,
        dictionnaire=etat.dictionnaires.get(demande.doc_id),
        # `dossier` n'est **pas** passé (D1) : la route ne l'expose pas, donc tout le paquet
        # contractuel est réputé inconnu, annoncé par `missing` et réclamé par `ask_client`. C'est
        # ce que « au regard des conditions générales seules » veut dire, et c'est écrit.
        # `variant` n'est pas transmis non plus (revue Codex 1.9, tour 1) : le corps ne le porte
        # plus et AD-11 ne l'énumère pas. Le pipeline en connaît deux depuis la story 4.2d, et c'est
        # précisément parce que la route n'en choisit aucune que son **défaut** — la navigation par
        # outils, mode par défaut d'AD-1 — est la variante servie ici ; `deterministe` reste la
        # baseline de comparaison des évals, jamais un chemin sélectionnable par une requête.
        # AD-1 (« un `pipeline.variant` inconnu ⇒ 400 ») est servi par `extra="forbid"` du schéma,
        # avant le premier appel facturé.
        pipeline_digest_hex=etat.pipeline_digest_hex, prompts_digest_hex=etat.prompts_digest_hex)

    try:
        sources = clauses_de(answer, etat.index, etat.corpus)
    except PipelineError as exc:
        # AD-3/AD-16 : une clause que le corpus servi ne confirme pas est un échec terminal, pas un
        # verdict amputé de ses clauses — c'est sous un verdict que « phrase sans source » coûte le
        # plus cher. La trace suit l'erreur (AD-10 : le coût engagé reste mesurable).
        exc.trace = trace
        raise
    # AD-10 : des champs, jamais du texte — ni la question, ni la description du sinistre, ni les
    # termes cherchés. Le filtre final est dans `request_id.py`.
    request.state.log_fields.update(
        intent=trace.intent, found=answer.found,
        verdict=answer.verdict.value if answer.verdict is not None else None,
        # Story 4.2f : la cause de l'issue, nommée par `presenter.champs_de_journal` — une seule
        # règle pour les deux routes, qui écrivent le même champ.
        **champs_de_journal(answer),
        cost_eur=trace.total_cost_eur)
    response = SinistreResponse(answer=answer, sources=sources, via=VIA, trace=trace)
    # Compatibilité explicite : les appelants one-shot conservent leur contrat exact. La page 3.7
    # demande le fil par un en-tête ; une Response directe contourne seulement la projection FastAPI
    # qui retirerait le champ ajouté, pas la validation pydantic ci-dessous.
    if request.headers.get("X-Sinistre-Conversation") != "1":
        return response
    entry = etat.corpus.manifest.get(demande.doc_id)
    if entry is None:
        raise InvalidRequest("empreintes du document indisponibles : conversation impossible")
    state = initialiser(
        doc_id=demande.doc_id, source_hash=entry.source_hash,
        ingest_fingerprint=entry.ingest_fingerprint,
        pipeline_digest=etat.pipeline_digest_hex, prompts_digest=etat.prompts_digest_hex,
        request_id=request.state.request_id, faits=demande.faits, answer=answer,
        decision_claims=list(answer._decision_claims),
        active_questions_max=etat.settings.conversation_active_questions_max)
    conversation = _view(state, signer(state, etat.conversation_secret))
    payload = SinistreConversationResponse(
        answer=answer, sources=sources, via=VIA, trace=trace, conversation=conversation)
    return JSONResponse(content=payload.model_dump(mode="json"))


def _view(state: ContinuationState, token: str) -> ConversationView:
    return ConversationView(
        token=token, turn=state.turn, facts=state.facts, conflicts=state.conflicts,
        questions=state.questions, history=state.history)


async def verifier_quota_suivi(request: Request) -> None:
    """Quota coût-zéro distinct, avec la même identité et les mêmes réponses AD-13."""
    request.app.state.foyer.followup_limiter.check(request)


@router.post("/sinistre/suivi", response_model=SinistreConversationResponse,
             response_model_exclude_none=False,
             dependencies=[Depends(verifier_quota_suivi)])
async def suivi(request: Request, demande: SinistreFollowupRequest) -> SinistreConversationResponse:
    """Valide entièrement l'état transporté avant tout budget, retrieval ou modèle."""
    etat = request.app.state.foyer
    state = verifier_token(demande.token, etat.conversation_secret)
    if state.doc_id != demande.doc_id:
        raise InvalidRequest("l'état de continuation appartient à un autre document")
    entry = etat.corpus.manifest.get(demande.doc_id)
    document = etat.corpus.documents.get(demande.doc_id)
    if document is None or document.kind != KIND_ATTENDU or entry is None:
        raise InvalidRequest("le document de l'état n'est plus un contrat servi")
    if (state.source_hash != entry.source_hash
            or state.ingest_fingerprint != entry.ingest_fingerprint):
        raise InvalidRequest("l'état de continuation est périmé pour ce document")
    if (state.pipeline_digest != etat.pipeline_digest_hex
            or state.prompts_digest != etat.prompts_digest_hex):
        raise InvalidRequest("l'état de continuation est périmé pour cette version du pipeline")

    answer, trace, updated = run_followup(
        state, demande.domain_action(), settings=etat.settings,
        request_id=request.state.request_id)
    try:
        sources = clauses_de(answer, etat.index, etat.corpus)
    except PipelineError as exc:
        exc.trace = trace
        raise
    token = signer(updated, etat.conversation_secret)
    request.state.log_fields.update(
        intent="suivi", found=answer.found,
        verdict=answer.verdict.value if answer.verdict is not None else None,
        cost_eur=0.0, conversation_turn=updated.turn)
    return SinistreConversationResponse(
        answer=answer, sources=sources, via=VIA, trace=trace,
        conversation=_view(updated, token))
