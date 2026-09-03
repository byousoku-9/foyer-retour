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
from fastapi.responses import JSONResponse, StreamingResponse

from server.app.api.presenter import champs_de_journal, fiches_de, sources_de
from server.app.api.progression import Progression, diffuser
from server.app.api.schemas import VIA, VIA_CACHE, ChatRequest, ChatResponse
from server.app.cache import EntreeDeCache, composantes_de_cle
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
    return await executer_chat(request, demande)


@router.post("/chat/progression")
async def chat_progression(request: Request, demande: ChatRequest) -> StreamingResponse:
    """Story 5.6 (L1) — la **même** réponse que `/chat`, dite pendant qu'elle se calcule (SSE).

    Rien n'est dupliqué : `executer_chat` est la fonction que la route classique appelle, et
    l'événement `resultat` porte le JSON de la `ChatResponse` qu'elle rend. Ce qui s'ajoute sont les
    deux rappels d'avancement, que le pipeline reçoit à `None` partout ailleurs. Le limiteur, les
    bornes du schéma et `borner()` s'appliquent **avant** le flux : une requête refusée l'est en
    vraie erreur HTTP, avec l'enveloppe d'AD-16, exactement comme sur la route classique.
    """
    etat = request.app.state.foyer  # le limiteur a déjà tranché (dépendance de routeur)
    demande.borner(etat.settings)
    progression = Progression("guide")

    async def produire() -> JSONResponse:
        reponse = await executer_chat(request, demande, progression=progression)
        return JSONResponse(content=reponse.model_dump(mode="json"))

    return diffuser(request, pipeline="guide", settings=etat.settings,
                    progression=progression, produire=produire)


async def executer_chat(request: Request, demande: ChatRequest, *,
                        progression: Progression | None = None) -> ChatResponse:
    """Le corps de `POST /chat`, partagé mot pour mot avec la route de progression."""
    etat = request.app.state.foyer  # le limiteur a déjà tranché (dépendance `verifier_quota`)
    demande.borner(etat.settings)

    variant = demande.variant or etat.settings.retrieval_variant
    # Story 5.6 (T5) — le cache de réponses, **avant** le budget et avant tout appel. La clé est
    # exacte : la question normalisée sur sa seule forme (espaces, casse, ponctuation finale) et
    # tout le reste mot pour mot. L'historique et le profil entrent dans la clé comme les faits du
    # sinistre : ce sont des données de la requête, et deux dialogues différents ne donnent pas la
    # même réponse. `composantes_de_cle` rend `None` — donc aucun cache, ni lu ni écrit — dès que le
    # document servi est en quarantaine ou porte une alerte de gate.
    cache = etat.cache_reponses
    cle = None
    if cache is not None:
        composantes = composantes_de_cle(
            etat=etat, route="chat", doc_id=etat.settings.guide_doc_id,
            question=demande.question, lang=demande.lang, variant=variant,
            entree={"historique": [t.model_dump(mode="json") for t in demande.historique],
                    "profil": demande.profil.model_dump(mode="json")
                    if demande.profil is not None else None},
            dictionnaire=etat.dictionnaire)
        if composantes is not None:
            cle = cache.cle(composantes)
            if (entree := cache.lire(cle)) is not None:
                return _projeter(request, demande, etat, entree.answer, entree.trace,
                                 via=VIA_CACHE, cout_de_la_requete=0.0)

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
        dictionnaire=etat.dictionnaire,
        variant=variant,
        # Story 5.6 (L1) : `None` sur la route classique, donc rien ne change pour elle.
        on_etape=progression.on_etape if progression is not None else None,
        on_tour=progression.on_tour if progression is not None else None)

    reponse = _projeter(request, demande, etat, answer, trace, via=VIA,
                        cout_de_la_requete=trace.total_cost_eur)
    # Écrit **après** la projection, donc après que `sources_de` a confirmé chaque citation dans le
    # corpus servi : on ne conserve jamais une réponse qui n'aurait pas pu être servie.
    if cache is not None and cle is not None:
        cache.ecrire(cle, EntreeDeCache(answer=answer, trace=trace,
                                        decision_claims=list(answer._decision_claims)))
    return reponse


def _projeter(request: Request, demande: ChatRequest, etat, answer, trace, *,  # type: ignore[no-untyped-def]
              via: str, cout_de_la_requete: float) -> ChatResponse:
    """La projection d'AD-11 et la ligne de log d'AD-10, partagées par le chemin payé et le cache.

    `via` et `cout_de_la_requete` sont les deux **seules** choses qui distinguent les deux chemins :
    la trace publiée est celle de la requête qui a payé — son `request_id`, son `total_cost_eur` —,
    tandis que la ligne de journal de *cette* requête-ci porte ce qu'elle a réellement coûté, c'est-
    à-dire zéro sur un hit. Sans cette distinction, une réponse re-servie aurait recompté sa facture
    à chaque fois, et l'analytique d'AD-10 aurait mesuré une dépense qui n'a pas eu lieu.
    """
    try:
        sources = sources_de(answer, etat.index, etat.corpus)
    except PipelineError as exc:
        # AD-3/AD-16 (revue Codex 1.6, B1 tour 2) : une citation que le corpus servi ne confirme pas
        # est un échec terminal, pas une réponse amputée de ses sources. La trace de la requête suit
        # l'erreur — AD-10 : le coût déjà engagé reste mesurable même quand rien n'est servi.
        # Le cache passe par ici aussi : une réponse conservée dont une citation n'est plus dans le
        # corpus servi est un échec terminal, jamais une réponse ancienne servie en silence.
        exc.trace = trace
        raise
    # AD-10 : la ligne de log ne porte que des champs — jamais la question, jamais `terms_searched`,
    # jamais le texte d'un bloc. Le filtre final est dans `request_id.py`.
    request.state.log_fields.update(
        intent=trace.intent, found=answer.found,
        verdict=answer.verdict.value if answer.verdict is not None else None,
        # Story 4.2f : la cause de l'issue, nommée par `presenter.champs_de_journal` — une seule
        # règle pour les deux routes, qui écrivent le même champ.
        **champs_de_journal(answer),
        # AD-10 : la liste `CHAMPS_DE_LOG` est **close** — `via` n'y entre pas, et un hit se lit
        # dans `cost_eur=0.0` ici et dans les compteurs de `/sante`, pas dans un champ de plus.
        cost_eur=cout_de_la_requete)
    return ChatResponse(texte=answer.texte, segments=answer.segments, sources=sources,
                        fiches=fiches_de(sources), unknown=answer.unknown,
                        comparateur=comparateur_de(demande.question), answer=answer, via=via,
                        trace=trace)
