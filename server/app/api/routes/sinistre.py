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

from server.app.api.presenter import clauses_de
from server.app.api.routes.chat import verifier_quota
from server.app.api.schemas import VIA, SinistreRequest, SinistreResponse
from server.app.domain.errors import InvalidRequest, PipelineError

# AD-13 : « le limiteur est une dépendance de routeur sur les routes qui appellent un modèle ».
router = APIRouter(dependencies=[Depends(verifier_quota)])

KIND_ATTENDU = "contrat"


@router.post("/sinistre", response_model=SinistreResponse, response_model_exclude_none=False)
async def sinistre(request: Request, demande: SinistreRequest) -> SinistreResponse:
    etat = request.app.state.foyer  # le limiteur a déjà tranché (dépendance `verifier_quota`)

    document = etat.corpus.documents.get(demande.doc_id)
    if document is None:
        # AD-15 : le `doc_id` reçu n'est pas recopié dans le message — c'est une chaîne de l'appelant.
        raise InvalidRequest("document inconnu : ce service ne sert pas ce contrat (la liste des "
                             "documents servis est publiée par GET /api/v1/documents)")
    if document.kind != KIND_ATTENDU:
        raise InvalidRequest("ce document n'est pas un contrat : un sinistre se confronte à des "
                             "conditions générales, et le guide n'en porte aucune (D3 de la story)")

    budget = etat.client.new_budget()  # AD-9 : deadline monotone armée ici, à l'entrée de la route
    answer, trace = await etat.pipeline_sinistre(
        demande.doc_id, demande.question, demande.faits,
        corpus=etat.corpus, index=etat.index, client=etat.client, settings=etat.settings,
        request_id=request.state.request_id, lang=demande.lang, budget=budget,
        # `dossier` n'est **pas** passé (D1) : la route ne l'expose pas, donc tout le paquet
        # contractuel est réputé inconnu, annoncé par `missing` et réclamé par `ask_client`. C'est
        # ce que « au regard des conditions générales seules » veut dire, et c'est écrit.
        pipeline_digest_hex=etat.pipeline_digest_hex, prompts_digest_hex=etat.prompts_digest_hex,
        **({"variant": demande.variant} if demande.variant is not None else {}))

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
        reason_kind=answer.reason.kind if answer.reason is not None else None,
        variants_count=answer.reason.variants_count if answer.reason is not None else None,
        blocks_scanned=answer.reason.blocks_scanned if answer.reason is not None else None,
        cost_eur=trace.total_cost_eur)
    return SinistreResponse(answer=answer, sources=sources, via=VIA, trace=trace)
