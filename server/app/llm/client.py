"""AD-9/10/16 — Le client Claude unique : async, structured outputs, cache de préfixe, deadline,
plafonds, coût réel, mapping exhaustif des erreurs fournisseur, trace de chaque appel.

Choix d'implémentation (Design Notes de la spec 1.3, convention LLM du spine amendée en revue 1.3) :
l'appel passe par `client.messages.parse(..., output_config={"format": …})` — schéma produit par
`anthropic.transform_schema` — **sans** `output_format=output_model` : avec `output_format`, le SDK
1.0.0 valide le texte avant de rendre la réponse et lève `ValidationError` — `usage`, `stop_reason`
et le texte reçu seraient perdus pour la trace (AD-10), le coût réel et le retry. La validation est
faite localement par `TypeAdapter(output_model).validate_json`, le code même de `parse_text` du SDK ;
le corps envoyé est identique sur le fil.

Le client Anthropic est construit avec `max_retries=0` : les retries du SDK (429/5xx)
casseraient la deadline et le compteur d'appels (AD-9).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Literal, Protocol, TypeVar

import anthropic
import pydantic
from anthropic import AsyncAnthropic
from pydantic import TypeAdapter

from server.app.config import Settings
from server.app.domain.errors import BudgetExceeded, ErrorCode, LlmParse, LlmUnavailable, PipelineError, Timeout
from server.app.domain.trace import CheckResult, LLMCall, StepTrace, Usage

from .budget import RequestBudget
from .models import EFFORT, MODEL_CAPS, Tier, model_for
from .pricing import cost_from_usage, estimate_cost
from .prompting import untrusted

T = TypeVar("T", bound=pydantic.BaseModel)

# Table exhaustive de mapping des erreurs fournisseur (AD-16), testée classe par classe.
# L'ordre n'importe pas : la résolution parcourt le MRO de l'exception (APITimeoutError avant
# APIConnectionError, sous-classes de statut avant APIStatusError).
PROVIDER_ERRORS: dict[type[Exception], ErrorCode] = {
    anthropic.APITimeoutError: ErrorCode.timeout,
    anthropic.APIConnectionError: ErrorCode.llm_unavailable,  # réseau
    anthropic.RateLimitError: ErrorCode.llm_unavailable,  # 429
    anthropic.OverloadedError: ErrorCode.llm_unavailable,  # 529
    anthropic.InternalServerError: ErrorCode.llm_unavailable,  # 5xx
    anthropic.ServiceUnavailableError: ErrorCode.llm_unavailable,  # 503
    anthropic.DeadlineExceededError: ErrorCode.llm_unavailable,  # 504
    anthropic.AuthenticationError: ErrorCode.llm_unavailable,  # 401
    anthropic.PermissionDeniedError: ErrorCode.llm_unavailable,  # 403
    anthropic.BadRequestError: ErrorCode.internal,  # 400 : notre requête est fausse
    anthropic.NotFoundError: ErrorCode.internal,  # 404
    anthropic.UnprocessableEntityError: ErrorCode.internal,  # 422
    anthropic.RequestTooLargeError: ErrorCode.internal,  # 413
    anthropic.ConflictError: ErrorCode.internal,  # 409
    anthropic.APIStatusError: ErrorCode.internal,  # tout statut restant
    anthropic.APIResponseValidationError: ErrorCode.internal,  # réponse hors schéma SDK
    anthropic.RetryableError: ErrorCode.llm_unavailable,  # le SDK la déclare transitoire
    anthropic.AnthropicError: ErrorCode.internal,  # toute erreur SDK résiduelle — le mapping est total
}


def map_provider_error(exc: Exception) -> PipelineError:
    """Erreur SDK → erreur typée du domaine ; message = classe SDK + request_id, jamais la clé ni le corps."""
    request_id = getattr(exc, "request_id", None)
    message = f"{type(exc).__name__} (request_id={request_id or 'absent'})"
    for cls in type(exc).__mro__:
        code = PROVIDER_ERRORS.get(cls)
        if code is None:
            continue
        if code is ErrorCode.timeout:
            return Timeout(message)
        return LlmUnavailable(message, code=code)
    raise exc  # pas une erreur du SDK Anthropic : on ne l'avale pas


class ResponseCache(Protocol):
    """Cache d'évals (AD-11, implémentation persistante en 4.1) : réponse brute + coût d'origine."""

    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, value: dict[str, Any]) -> None: ...


class MemoryResponseCache:
    """Implémentation mémoire du protocole, suffisante pour tester `cached_response`."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._store.get(key)

    def set(self, key: str, value: dict[str, Any]) -> None:
        self._store[key] = value


@dataclass
class LlmResult(Generic[T]):
    parsed: T
    usage: Usage
    call: LLMCall


@dataclass
class ToolTurnResult:
    """Réponse brute d'un tour d'outils, conservant tous les blocs `tool_use`."""

    message: Any
    usage: Usage
    call: LLMCall


def structured_request_parts(
    *, tier: Tier, system_prefix: str, messages: list[dict[str, Any]],
    output_model: type[T], max_tokens: int,
    tools: list[dict[str, Any]] | None = None,
    effort: Literal["low", "medium", "high", "max"] | None = None,
    prompt_cache: bool = True,
) -> tuple[dict[str, Any], TypeAdapter[T]]:
    """Construit l'enveloppe structurée unique employée au préflight et par le client."""
    model = model_for(tier)
    caps = MODEL_CAPS[model]
    adapter: TypeAdapter[T] = TypeAdapter(output_model)
    cache_control: dict[str, Any] = {"type": "ephemeral"}
    if caps["cache_ttl"] == "1h":
        cache_control["ttl"] = "1h"
    system_block: dict[str, Any] = {"type": "text", "text": system_prefix}
    if prompt_cache:
        system_block["cache_control"] = cache_control
    schema = adapter.json_schema()
    output_config: dict[str, Any] = {
        "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
    }
    if effort is not None and not caps["effort"]:
        raise ValueError(f"le modèle du tier {tier!r} n'accepte pas de paramètre effort")
    if caps["effort"]:
        output_config["effort"] = effort or EFFORT[tier]
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": [system_block],
        "messages": list(messages),
        "output_config": output_config,
        "tools": tools,
        "extra_body": {"temperature": 0} if caps["temperature"] else None,
    }, adapter


def structured_input_envelope(**kwargs: Any) -> str:
    """JSON exact des champs d'entrée facturables d'un appel `parse`, hors sortie réservée."""
    request, _adapter = structured_request_parts(**kwargs)
    wire = {key: request[key] for key in ("system", "messages", "output_config")}
    if request["tools"] is not None:
        wire["tools"] = request["tools"]
    return json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cache_key(body: dict[str, Any]) -> str:
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text_of(message: Any) -> str:
    return "".join(block.text for block in message.content if getattr(block, "type", None) == "text")


def _champs_du_schema(schema: Any) -> frozenset[str]:
    """Noms de champs déclarés par le schéma de sortie (récursif, `$defs` et sous-modèles inclus).

    AD-15 (revue Codex 1.4, B7, tour 2) : le `loc` d'une erreur pydantic n'est du texte de **notre**
    code que tant qu'il nomme un champ du schéma. Avec `extra="forbid"` (tous les modèles du domaine),
    un champ surnuméraire inventé par le modèle devient lui-même le `loc` de l'erreur
    `extra_forbidden` — le nom arbitraire du modèle traverserait alors le motif de relance et
    `StepTrace.checks`. Cette liste est la référence qui permet de le remplacer.
    """
    noms: set[str] = set()
    pile: list[Any] = [schema]
    while pile:
        node = pile.pop()
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                noms.update(k for k in props if isinstance(k, str))
            pile.extend(node.values())
        elif isinstance(node, list):
            pile.extend(node)
    return frozenset(noms)


# Types d'erreur pydantic dont le `msg` recopie une valeur reçue (le tag d'une union discriminée) :
# leur message est remplacé par le seul code. Les autres messages intégrés sont composés à partir du
# schéma (« Field required », « Input should be 'question', 'suivi', … », « Extra inputs are not
# permitted ») ; ceux des validateurs du domaine n'interpolent aucune valeur reçue (règle vérifiée par
# `tests/test_domain.py`).
_MSG_CITE_LA_VALEUR = frozenset({"union_tag_invalid", "union_tag_not_found"})


class LlmClient:
    """Client unique des étapes ; ne connaît ni les étapes ni les pipelines."""

    def __init__(self, settings: Settings, anthropic_client: Any | None = None,
                 cache: ResponseCache | None = None,
                 campaign_budget_eur: float | None = None,
                 campaign_accrued_eur: float = 0.0,
                 campaign_cost_recorder: Callable[[float], None] | None = None) -> None:
        self._settings = settings
        self._cache = cache
        # Story 4.2b — budget de **campagne** (règle trusted `LIVE_BUDGET_EUR`) : cumul de tous les
        # appels facturés à travers ce client, quel que soit le nombre de requêtes. `None` (défaut,
        # serveur HTTP) : aucune limite de campagne — le plafond par requête d'AD-9 reste seul.
        # Le runner d'évals le règle sur `min(--max-cost, live_budget_eur)` : l'appel qui ferait
        # déborder la campagne est refusé **avant** l'envoi, avec les trois chiffres du rapport
        # trusted (configured/accrued/refused), jamais une question humaine.
        self.campaign_budget_eur = campaign_budget_eur
        self.campaign_cost_eur = campaign_accrued_eur
        self._campaign_cost_recorder = campaign_cost_recorder
        if anthropic_client is None:
            anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=0)
        self._anthropic = anthropic_client

    def _refuser_hors_campagne(self, estimate: float) -> None:
        """Refus chiffré avant l'appel qui déborderait le budget de campagne (story 4.2b)."""
        if self.campaign_budget_eur is None:
            return
        if self.campaign_cost_eur + estimate > self.campaign_budget_eur:
            raise BudgetExceeded(
                f"budget de campagne : configured_budget_eur={self.campaign_budget_eur:.4f} "
                f"accrued_cost_eur={self.campaign_cost_eur:.4f} "
                f"refused_cost_eur={estimate:.4f}")

    def _noter_campagne(self, usage: Usage) -> None:
        # Garde la précision fournisseur ; seuls les rendus sont arrondis. Le callback du runner
        # persiste chaque appel sous verrou, même si le processus s'interrompt plus tard.
        self.campaign_cost_eur += usage.cost_eur
        if self._campaign_cost_recorder is not None:
            self._campaign_cost_recorder(usage.cost_eur)

    async def aclose(self) -> None:
        """Ferme le pool de connexions du SDK. Appelé par le `lifespan` de l'API, à l'arrêt.

        Tolérant : un double fourni par les tests n'a pas forcément de `close`, et un client déjà
        fermé ne doit pas faire échouer un arrêt.
        """
        fermer = getattr(self._anthropic, "close", None)
        if fermer is None:
            return
        resultat = fermer()
        if hasattr(resultat, "__await__"):
            await resultat

    def new_budget(self, deadline_s: float | None = None) -> RequestBudget:
        """Budget d'une requête, réglé sur les seuils actifs (AD-9 : deadline, appels, euros).

        Story 1.5 : la table des couches interdit à `pipelines` d'importer `llm` — il ne peut donc
        pas construire lui-même un `RequestBudget`. Le client, qui est le seul à s'en servir, le
        fabrique ; le pipeline le reçoit ou le demande, sans jamais connaître son type.

        Story 1.8 : `deadline_s` **raccourcit** la deadline du réglage, jamais l'inverse. Un appelant
        qui a déjà consommé du temps (un script de démonstration, une éval qui enchaîne des cas) doit
        pouvoir donner moins que `settings.deadline_s` ; lui laisser en demander plus contournerait
        NFR3 depuis l'extérieur du serveur.
        """
        s = self._settings
        return RequestBudget(deadline_s=min(deadline_s, s.deadline_s) if deadline_s is not None
                             else s.deadline_s,
                             max_attempts=s.max_llm_attempts, max_cost_eur=s.max_cost_eur_per_request)

    async def parse(
        self,
        *,
        tier: Tier,
        system_prefix: str,
        messages: list[dict[str, Any]],
        output_model: type[T],
        budget: RequestBudget,
        step: StepTrace,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        effort: Literal["low", "medium", "high", "max"] | None = None,
        prompt_cache: bool = True,
    ) -> LlmResult[T]:
        """Un appel structuré : préfixe caché, timeout borné par la deadline, 1 retry sur parse invalide."""
        settings = self._settings
        max_tokens = max_tokens or settings.llm_max_output_tokens
        request, adapter = structured_request_parts(
            tier=tier, system_prefix=system_prefix, messages=messages,
            output_model=output_model, max_tokens=max_tokens, tools=tools,
            effort=effort, prompt_cache=prompt_cache)
        model = request["model"]
        system = request["system"]
        output_config = request["output_config"]
        extra_body = request["extra_body"]
        schema = adapter.json_schema()
        champs = _champs_du_schema(schema)
        # Story 1.4 (reprise B5) : empreinte du préfixe facturable — modèle + système + tools + schéma de
        # sortie. Déjà vue dans la requête ⇒ l'estimation compte le préfixe au tarif `cache_read` ; notée
        # seulement après une réponse du fournisseur *qui a effectivement caché le préfixe* (un échec
        # d'appel, comme un préfixe trop court pour être cachable, n'écrit rien dans le cache).
        prefix_digest = _cache_key({"model": model, "system": system, "tools": tools,
                                    "output_schema": output_config["format"]})

        msgs = list(messages)
        retried = False
        while True:
            if budget.remaining() <= 0:
                raise Timeout(f"deadline épuisée avant l'appel ({budget.remaining():.1f} s restantes)")

            body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": msgs,
                    "output_config": output_config, "tools": tools, "extra_body": extra_body}
            key = _cache_key(body)
            if self._cache is not None and (hit := self._cache.get(key)) is not None:
                try:
                    message = anthropic.types.Message.model_validate(hit["response"])
                    parsed_hit = adapter.validate_json(_text_of(message))
                except (pydantic.ValidationError, KeyError, TypeError) as exc:
                    raise LlmParse(f"entrée de cache d'évals invalide : {type(exc).__name__}") from exc
                usage = Usage(cached_response=True, cost_eur=0.0, cost_eur_original=hit["cost_eur"])
                call = LLMCall(model=message.model, ms=0, usage=usage)
                self._note_call(step, call, tier)
                return LlmResult(parsed=parsed_hit, usage=usage, call=call)

            if budget.attempts >= budget.max_attempts:
                raise BudgetExceeded(f"plafond d'appels atteint ({budget.attempts}/{budget.max_attempts})")
            estimate = estimate_cost(model, system, msgs, max_tokens, settings, tools=tools,
                                     output_schema=output_config["format"],
                                     prefix_cached=(prompt_cache and budget.prefix_seen(prefix_digest)),
                                     prompt_cache=prompt_cache)
            if budget.cost_eur + estimate > budget.max_cost_eur:
                raise BudgetExceeded(
                    f"plafond de coût par requête : {budget.cost_eur:.4f} € déjà engagés "
                    f"+ {estimate:.4f} € estimés > {budget.max_cost_eur:.4f} €"
                )
            self._refuser_hors_campagne(estimate)

            timeout = budget.timeout_for_call(settings.llm_timeout_s)
            kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system,
                                      "messages": msgs, "output_config": output_config, "timeout": timeout}
            if tools is not None:
                kwargs["tools"] = tools
            if extra_body is not None:
                kwargs["extra_body"] = extra_body

            tool_names = [t.get("name", "") for t in tools] if tools else []
            budget.attempts += 1
            t0 = time.monotonic()
            try:
                message = await self._anthropic.messages.parse(**kwargs)
            except Exception as exc:  # noqa: BLE001 — mapping total des erreurs SDK, le reste est relancé tel quel
                # AD-10 : l'appel en échec est tracé aussi — modèle demandé, durée, usage nul
                # (l'API n'a rien renvoyé : timeout, 429, 529, réseau…).
                ms = int((time.monotonic() - t0) * 1000)
                self._note_call(step, LLMCall(model=model, ms=ms, usage=Usage(), tools=tool_names),
                                tier)
                raise map_provider_error(exc) from exc
            ms = int((time.monotonic() - t0) * 1000)

            usage = cost_from_usage(model, message.usage, settings.usd_eur)
            budget.note_call(usage)
            self._noter_campagne(usage)
            cache_write = self._cache_write_tokens(message.usage)
            if prompt_cache and (cache_write or usage.cached):
                # AD-9 / NFR4 (revue 1.4) : l'empreinte n'est notée que si le fournisseur a réellement
                # écrit (ou lu) le préfixe. Un préfixe sous la taille minimale cacheable du modèle
                # (2 048 tokens sur Haiku 4.5, 1 024 sur Sonnet/Opus) n'est jamais mis en cache : le
                # compter ensuite au tarif `cache_read` ferait sous-estimer l'appel suivant et
                # `estimate_cost` cesserait de majorer. Constaté sur les fixtures live de *comprendre*
                # (préfixe ≈ 900 tokens : `cache_creation` et `cache_read_input_tokens` à 0).
                budget.note_prefix(prefix_digest)
            call = LLMCall(model=message.model, ms=ms, usage=usage,
                           cache_read=usage.cached, cache_write=cache_write,
                           tools=tool_names)
            self._note_call(step, call, tier)
            # AD-10 (revue Codex 1.3, I1) : le seuil porte sur le coût cumulé de la requête — un appel
            # cher isolé le franchit aussi ; le check n'est ajouté qu'une fois, au franchissement.
            if budget.cost_eur > settings.cost_alert_eur and not budget.cost_alerted:
                budget.cost_alerted = True
                step.checks.append(CheckResult(
                    name="cout_eleve", ok=False,
                    detail=f"coût cumulé de la requête {budget.cost_eur:.4f} € > "
                           f"cost_alert_eur {settings.cost_alert_eur:.4f} € (dernier appel : {usage.cost_eur:.4f} €)"))

            text = _text_of(message)
            if message.stop_reason == "refusal":
                raise LlmParse("le modèle a refusé de répondre (stop_reason=refusal)")
            if message.stop_reason in ("tool_use", "pause_turn"):
                # revue P9 : le dialogue d'outils (story 2.6) n'est pas supporté par ce client — pas de retry,
                # rejouer la même requête reproduirait le même stop_reason.
                raise LlmParse(f"dialogue d'outils non supporté par ce client (story 2.6) — "
                               f"stop_reason={message.stop_reason}")

            problem: str | None = None
            if message.stop_reason == "max_tokens":
                problem = f"réponse tronquée (stop_reason=max_tokens, max_tokens={max_tokens})"
            else:
                try:
                    parsed = adapter.validate_json(text)
                except pydantic.ValidationError as exc:
                    problem = f"réponse non conforme au schéma : {self._validation_motive(exc, champs)}"
            if problem is None:
                if self._cache is not None:
                    self._cache.set(key, {"response": message.to_dict(), "cost_eur": usage.cost_eur})
                return LlmResult(parsed=parsed, usage=usage, call=call)

            can_retry = (not retried and budget.attempts < budget.max_attempts
                         and budget.remaining() > settings.llm_retry_margin_s)
            if not can_retry:
                raise LlmParse(problem)
            retried = True
            step.checks.append(CheckResult(name="parse_retry", ok=False, detail=problem))
            # Le préfixe reste byte-identique : le motif est porté par un tour supplémentaire.
            # AD-15 (revue Codex 1.4, B7) : le motif est composé à partir de la réponse du modèle
            # (chemins pydantic, messages de validateurs) — il est donc délimité comme tout contenu non
            # fiable, jamais concaténé en clair dans une consigne. Seule la consigne est du texte de
            # confiance ; `untrusted()` neutralise en outre toute balise portée par le motif.
            # Une sortie arrêtée par `max_tokens` est incomplète par définition. La réinjecter en
            # entier dans la relance ne fournit donc aucune vérité utile, mais refacture son texte en
            # entrée et incite le modèle à prolonger la même réponse trop longue. On conserve le tour
            # assistant pour l'alternance du dialogue, avec un marqueur constant, puis on demande une
            # régénération concise. Les erreurs de schéma gardent au contraire la réponse reçue : elle
            # permet au modèle de corriger précisément le champ signalé par le motif.
            tronquee = message.stop_reason == "max_tokens"
            reponse_precedente = "(réponse tronquée omise)" if tronquee else (text or "(réponse vide)")
            correction = ("Repars de zéro. Respecte intégralement toutes les listes et cardinalités "
                          "exigées par le schéma ; rends seulement le texte libre de chaque champ "
                          "au plus concis, sans omettre aucun élément requis. "
                          if tronquee else "Corrige exactement ce qu'il décrit. ")
            msgs = [*msgs,
                    {"role": "assistant", "content": reponse_precedente},
                    {"role": "user", "content": "Ta réponse précédente était invalide. Le contrôle a relevé :\n"
                                                + untrusted("motif", problem)
                                                + f"\n{correction}Réponds à nouveau, uniquement avec le JSON "
                                                  "conforme au schéma."}]

    async def tool_turn(
        self,
        *,
        tier: Tier,
        system_prefix: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        budget: RequestBudget,
        step: StepTrace,
        max_tokens: int,
        prompt_cache: bool = True,
    ) -> ToolTurnResult:
        """Un tour brut d'outils, avec les mêmes bornes, prix, cache et traces que `parse()`.

        Contrairement à `parse()`, cette couture n'impose aucun schéma de sortie et conserve le
        `stop_reason` brut : l'étape appelante exécute les outils, borne le dialogue et traite
        `max_tokens`, `refusal` et `pause_turn` comme une navigation tronquée. Il n'existe donc
        aucun retry de parse caché.
        """
        settings = self._settings
        model = model_for(tier)
        caps = MODEL_CAPS[model]
        cache_control: dict[str, Any] = {"type": "ephemeral"}
        if caps["cache_ttl"] == "1h":
            cache_control["ttl"] = "1h"
        system_block: dict[str, Any] = {"type": "text", "text": system_prefix}
        if prompt_cache:
            system_block["cache_control"] = cache_control
        system = [system_block]
        output_config = {"effort": EFFORT[tier]} if caps["effort"] else None
        extra_body = {"temperature": 0} if caps["temperature"] else None
        prefix_digest = _cache_key({"model": model, "system": system, "tools": tools})
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": messages, "tools": tools, "output_config": output_config,
                "extra_body": extra_body}
        key = _cache_key(body)
        # Une fixture n'autorise jamais à dépasser la deadline : le cache évite le fournisseur, pas
        # les bornes de la requête.
        if budget.remaining() <= 0:
            raise Timeout(f"deadline épuisée avant l'appel ({budget.remaining():.1f} s restantes)")
        if self._cache is not None and (hit := self._cache.get(key)) is not None:
            try:
                message = anthropic.types.Message.model_validate(hit["response"])
                cost_eur = hit["cost_eur"]
                if (isinstance(cost_eur, bool) or not isinstance(cost_eur, (int, float))
                        or not math.isfinite(cost_eur) or cost_eur < 0):
                    raise ValueError("cost_eur invalide")
                usage = Usage(cached_response=True, cost_eur=0.0,
                              cost_eur_original=float(cost_eur))
            except (pydantic.ValidationError, KeyError, TypeError, ValueError) as exc:
                raise LlmParse(f"entrée de cache d'évals invalide : {type(exc).__name__}") from exc
            call = LLMCall(model=message.model, ms=0, usage=usage,
                           tools=[str(t.get("name", "")) for t in tools])
            self._note_call(step, call, tier)
            return ToolTurnResult(message=message, usage=usage, call=call)

        if budget.attempts >= budget.max_attempts:
            raise BudgetExceeded(f"plafond d'appels atteint ({budget.attempts}/{budget.max_attempts})")
        estimate = estimate_cost(model, system, messages, max_tokens, settings, tools=tools,
                                 prefix_cached=(prompt_cache and budget.prefix_seen(prefix_digest)),
                                 prompt_cache=prompt_cache)
        if budget.cost_eur + estimate > budget.max_cost_eur:
            raise BudgetExceeded(
                f"plafond de coût par requête : {budget.cost_eur:.4f} € déjà engagés "
                f"+ {estimate:.4f} € estimés > {budget.max_cost_eur:.4f} €")
        self._refuser_hors_campagne(estimate)

        timeout = budget.timeout_for_call(settings.llm_timeout_s)
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system,
                                  "messages": messages, "tools": tools, "timeout": timeout}
        if output_config is not None:
            kwargs["output_config"] = output_config
        if extra_body is not None:
            kwargs["extra_body"] = extra_body
        tool_names = [str(t.get("name", "")) for t in tools]
        budget.attempts += 1
        t0 = time.monotonic()
        try:
            message = await self._anthropic.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 — même mapping total que `parse`
            ms = int((time.monotonic() - t0) * 1000)
            self._note_call(step, LLMCall(model=model, ms=ms, usage=Usage(), tools=tool_names),
                            tier)
            raise map_provider_error(exc) from exc
        ms = int((time.monotonic() - t0) * 1000)
        usage = cost_from_usage(model, message.usage, settings.usd_eur)
        budget.note_call(usage)
        self._noter_campagne(usage)
        cache_write = self._cache_write_tokens(message.usage)
        if prompt_cache and (cache_write or usage.cached):
            budget.note_prefix(prefix_digest)
        call = LLMCall(model=message.model, ms=ms, usage=usage,
                       cache_read=usage.cached, cache_write=cache_write, tools=tool_names)
        self._note_call(step, call, tier)
        if budget.cost_eur > settings.cost_alert_eur and not budget.cost_alerted:
            budget.cost_alerted = True
            step.checks.append(CheckResult(
                name="cout_eleve", ok=False,
                detail=f"coût cumulé de la requête {budget.cost_eur:.4f} € > "
                       f"cost_alert_eur {settings.cost_alert_eur:.4f} € "
                       f"(dernier appel : {usage.cost_eur:.4f} €)"))
        if self._cache is not None:
            self._cache.set(key, {"response": message.to_dict(), "cost_eur": usage.cost_eur})
        return ToolTurnResult(message=message, usage=usage, call=call)

    async def count_tokens(self, model: str, system: str | None, messages: list[dict[str, Any]]) -> int:
        """Tokens réels d'une requête au tokenizer du modèle (`/v1/messages/count_tokens`)."""
        kwargs: dict[str, Any] = {"model": model, "messages": messages,
                                  "timeout": self._settings.count_tokens_timeout_s}
        if system is not None:
            kwargs["system"] = system
        try:
            counted = await self._anthropic.messages.count_tokens(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise map_provider_error(exc) from exc
        return counted.input_tokens

    @staticmethod
    def _validation_motive(exc: pydantic.ValidationError, champs: frozenset[str] = frozenset(), *,
                           max_errors: int = 4, max_len: int = 500) -> str:
        """Motif de relance qui dit *quoi* corriger (story 1.4).

        `error_count()` seul ne motive rien : le modèle rejoue la même réponse (observé en live sur
        `AnswerDraft`, deux quotes du même bloc dans une claim). On rend donc le chemin du champ, le
        code d'erreur et le message du validateur, sans la valeur reçue (`include_input=False`) : la
        recopier gonflerait la requête pour rien.

        Ce motif part dans `StepTrace.checks` (AD-10) et dans la relance : il ne doit donc contenir que
        du texte produit par **notre** code. Le chemin est pour cela ramené aux noms de champs déclarés
        par le schéma (`champs`) et aux indices de liste ; tout autre segment — le nom d'un champ
        surnuméraire inventé par le modèle, que pydantic met dans le `loc` de l'erreur
        `extra_forbidden` — devient `<champ inconnu>` (revue Codex 1.4, B7, tour 2). Les codes d'erreur
        de pydantic sont des constantes ; les messages sont composés à partir du schéma, sauf ceux de
        `_MSG_CITE_LA_VALEUR`, effacés. Les messages des validateurs du domaine n'interpolent aucune
        valeur reçue (règle vérifiée par `tests/test_domain.py`). La borne `max_len` reste une ceinture,
        et la relance délimite le motif avec `untrusted()`.
        """
        errors = exc.errors(include_url=False, include_input=False, include_context=False)
        lines = []
        for err in errors[:max_errors]:
            parts = [str(p) if isinstance(p, int) or p in champs else "<champ inconnu>"
                     for p in err.get("loc", ())]
            loc = ".".join(parts) or "(racine)"
            code = str(err.get("type", "?"))
            msg = "" if code in _MSG_CITE_LA_VALEUR else str(err.get("msg", "")).replace("Value error, ", "")
            lines.append(f"{loc} [{code}] : {msg}" if msg else f"{loc} [{code}]")
        motive = f"{exc.error_count()} erreur(s) de validation — " + " ; ".join(lines)
        if len(errors) > max_errors:
            motive += f" ; … ({len(errors) - max_errors} autre(s))"
        return motive[:max_len]

    @staticmethod
    def _cache_write_tokens(api_usage: Any) -> int:
        creation = getattr(api_usage, "cache_creation", None)
        if creation is not None:
            return int(getattr(creation, "ephemeral_5m_input_tokens", 0) or 0) + \
                int(getattr(creation, "ephemeral_1h_input_tokens", 0) or 0)
        return int(getattr(api_usage, "cache_creation_input_tokens", 0) or 0)

    @staticmethod
    def _note_call(step: StepTrace, call: LLMCall, tier: Tier | None = None) -> None:
        """Point unique qui pousse un `LLMCall` — donc le seul endroit où le tier employé s'écrit.

        Story 4.2e : `parse()` et `tool_turn()` reçoivent déjà le `tier` ; il n'était publié nulle
        part par appel. Le renseigner ici plutôt qu'à chaque construction garantit qu'aucun chemin
        (succès, échec, réponse rejouée depuis le cache d'évals) ne publie un appel sans son tier.
        """
        if tier is not None:
            call.tier = tier
        step.calls.append(call)
        u, s = call.usage, step.usage
        s.input += u.input
        s.cached += u.cached
        s.output += u.output
        s.cost_eur = round(s.cost_eur + u.cost_eur, 4)
        s.cost_eur_original = round(s.cost_eur_original + u.cost_eur_original, 4)
