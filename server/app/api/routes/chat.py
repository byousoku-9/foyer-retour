"""AD-11 — `POST /api/v1/chat` (et l'alias historique `/chat`) : la seule route qui appelle un modèle.

Ce que la route fait, dans cet ordre, et rien d'autre :

1. **le limiteur** (AD-13) — identité `X-Forwarded-For`, quotas par instance. Il est une dépendance
   du **routeur**, et non la première ligne de la fonction : FastAPI résout les dépendances avant de
   valider le corps, si bien qu'un corps invalide est compté lui aussi. Placé dans la fonction, il
   n'aurait jamais vu les requêtes que pydantic refuse — c'est-à-dire un flot de 400 hors quota,
   alors que le limiteur est précisément le garde-fou du volume ;
2. **les bornes du contrat** qui vivent dans `config.py` (`historique_max_turns`) — celles qui vivent
   dans le schéma ont déjà été appliquées par pydantic ;
3. **le budget** (AD-9/NFR3) — un `RequestBudget` par requête, créé ici, deadline monotone armée à
   l'entrée, propagé au pipeline ;
4. **le pipeline**, avec les digests calculés **au démarrage** (reprise 1.6) ;
5. **la projection d'AD-11** — `texte`, `segments`, `sources`, `fiches`, `unknown`, `comparateur`,
   `answer`, `via`, `trace` ;
6. **la ligne de log** (AD-10) — des champs, jamais du texte.

**Toute** sortie du pipeline est un 200 : refus, clarification, claims toutes rejetées. Ce ne sont
pas des erreurs, ce sont des réponses — AD-11 le dit, et c'est la différence entre « le guide ne
traite pas ce sujet » et « le serveur est en panne ». Les échecs terminaux, eux, remontent en
`PipelineError` jusqu'à l'enveloppe d'AD-16, avec leur `Trace` partielle.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request

from server.app.api.presenter import fiches_de, sources_de
from server.app.api.schemas import VIA, ChatRequest, ChatResponse
from server.app.corpus.text import normalize
from server.app.domain.errors import PipelineError


async def verifier_quota(request: Request) -> None:
    """AD-13 — le limiteur, en dépendance de routeur : compté **avant** la lecture du corps.

    Lève `InvalidRequest` (400, en-tête d'identité absent hors dev) ou `PipelineError(rate_limited)`
    (429) ; dans les deux cas, rien n'a encore été lu ni dépensé.
    """
    request.app.state.foyer.limiter.check(request)


router = APIRouter(dependencies=[Depends(verifier_quota)])

# Transposition **littérale** de la règle `toucheContrats` du site (`web/app/chat.js`, l. 208) :
#     /(assur|contrat|couvert|couverture|sinistre|franchise|indemnis|vole?\b|degat)/
# appliquée à la question normalisée. AD-11 conserve `comparateur` « pour l'UI existante » sans dire
# qui le calcule ; le laisser toujours `false` retirerait en silence une fonction du site (l'onglet
# Comparateur ne s'ouvrirait plus jamais depuis une réponse). Ce n'est pas un seuil numérique :
# `config.py` ne le porte pas, il vit avec la route qui le rend. Le test côté site exigeait en plus
# la présence de `window.CONTRATS_KB` — côté serveur, la donnée du comparateur est celle du site,
# toujours servie sous `/guide/`, donc la condition est vraie par construction.
TOUCHE_CONTRATS = re.compile(r"(assur|contrat|couvert|couverture|sinistre|franchise|indemnis|vole?\b|degat)")


def comparateur_de(question: str) -> bool:
    """La question touche-t-elle aux contrats d'assurance (règle du site) ?"""
    return bool(TOUCHE_CONTRATS.search(normalize(question)))


@router.post("/chat", response_model=ChatResponse, response_model_exclude_none=False)
async def chat(request: Request, demande: ChatRequest) -> ChatResponse:
    etat = request.app.state.foyer  # le limiteur a déjà tranché (dépendance `verifier_quota`)
    demande.borner(etat.settings)

    budget = etat.client.new_budget()  # AD-9 : deadline monotone armée ici, à l'entrée de la route
    answer, trace = await etat.pipeline(
        demande.question, demande.historique, demande.profil,
        corpus=etat.corpus, index=etat.index, client=etat.client, settings=etat.settings,
        request_id=request.state.request_id, lang=demande.lang, budget=budget,
        pipeline_digest_hex=etat.pipeline_digest_hex, prompts_digest_hex=etat.prompts_digest_hex,
        # AD-5 / AD-7 (story 2.1) : le dictionnaire est chargé **une fois** au démarrage, en lecture
        # seule, et voyage comme `corpus`, `index` et `client` — le pipeline ne voit pas `corpus`
        # (table des couches) et ne peut donc pas le charger lui-même. Toujours un objet, jamais
        # `None` : un dictionnaire absent est un `Dictionnaire` inerte, qui n'arme rien.
        dictionnaire=etat.dictionnaire, variant=demande.variant)

    try:
        sources = sources_de(answer, etat.index, etat.corpus)
    except PipelineError as exc:
        # AD-3/AD-16 (revue Codex 1.6, B1 tour 2) : une citation que le corpus servi ne confirme pas
        # est un échec terminal, pas une réponse amputée de ses sources. La trace de la requête suit
        # l'erreur — AD-10 : le coût déjà engagé reste mesurable même quand rien n'est servi.
        exc.trace = trace
        raise
    # AD-10 : la ligne de log ne porte que des champs — jamais la question, jamais `terms_searched`,
    # jamais le texte d'un bloc. Le filtre final est dans `request_id.py`.
    request.state.log_fields.update(
        intent=trace.intent, found=answer.found,
        verdict=answer.verdict.value if answer.verdict is not None else None,
        reason_kind=answer.reason.kind if answer.reason is not None else None,
        variants_count=answer.reason.variants_count if answer.reason is not None else None,
        blocks_scanned=answer.reason.blocks_scanned if answer.reason is not None else None,
        cost_eur=trace.total_cost_eur)
    return ChatResponse(texte=answer.texte, segments=answer.segments, sources=sources,
                        fiches=fiches_de(sources), unknown=answer.unknown,
                        comparateur=comparateur_de(demande.question), answer=answer, via=VIA,
                        trace=trace)
