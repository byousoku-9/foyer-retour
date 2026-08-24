"""AD-1 / AD-10 — Le pipeline du guide : les cinq étapes, les court-circuits, la relance unique, la trace.

`comprendre → retrouver → rédiger → vérifier → restituer`, dans cet ordre, toujours. Ce que le
pipeline ajoute aux étapes — et qui n'appartient à aucune d'elles :

- **les bornes d'entrée** : `RetrievalBudget` rempli depuis `settings`, historique borné par
  `historique_max_turns` (au-delà : `InvalidRequest`, **jamais** de troncature silencieuse — AD-11,
  AD-16), deadline vérifiée avant chaque étape ;
- **les court-circuits d'AD-5** : `intent ∈ {meteo, bavardage, hors_perimetre}`, clarification
  requise, ou retrieval vide ⇒ *restituer* directement, **sans** appel `reason` ;
- **la relance d'AD-3** : une claim rejetée ⇒ **une** relance de *rédiger* avec le motif de
  *vérifier* ; un draft identique (même hash canonique) arrête là ;
- **la trace d'AD-10** : les `StepTrace` de chaque étape, les digests, les seuils, les hashes du
  corpus — et jamais le texte d'un bloc.

Le pipeline ne voit ni `corpus` ni `llm` (table des couches du spine) : `corpus`, `index` et `client`
sont annotés `Any` et lui viennent de l'appelant (l'API, story 1.6). Ce n'est pas un renoncement au
typage, c'est l'invariant lui-même : un pipeline qui ne peut pas importer `llm` ne peut pas appeler
un modèle hors d'une étape.
"""

from __future__ import annotations

from typing import Any

from server.app.config import Settings
from server.app.domain.answer import AbsenceProof, Answer
from server.app.domain.errors import (
    BudgetExceeded,
    CorpusUnavailable,
    InvalidRequest,
    LlmParse,
    LlmUnavailable,
    PipelineError,
    Timeout,
)
from server.app.domain.profil import Profil
from server.app.domain.question import ClarificationRequise, ParsedQuestion, Turn
from server.app.domain.trace import CheckResult, StepTrace, Trace
from server.app.pipelines.commun import (
    APPELS_DE_LA_RELANCE,
    INTENTS_REFUSES,
    blocs_cites,
    digests,
    domine,
    relance_utile,
    retrieval_budget,
)
from server.app.steps.comprendre import comprendre
from server.app.steps.rediger import rediger
from server.app.steps.restituer import restituer
from server.app.steps.retrouver import retrouver_deterministe
from server.app.steps.verifier import verifier

PIPELINE = "guide"
VARIANT = "deterministe"


def _absence(kind: str, parsed: ParsedQuestion | None, *, doc_id: str, corpus: Any) -> AbsenceProof:
    """Preuve d'absence (AD-4) : ce qui a été cherché, jamais les variantes ni les déclencheurs.

    `variants_count=0` tant que le dictionnaire n'est pas validé (AD-5, story 2.1) : aucune variante
    n'a été essayée, et l'annoncer autrement serait faux.
    """
    if parsed is None:  # rien n'a été cherché : ni termes, ni passages parcourus
        return AbsenceProof(kind=kind)
    document = corpus.documents.get(doc_id)
    return AbsenceProof(kind=kind, terms_searched=parsed.termes_de_recherche(), variants_count=0,
                        blocks_scanned=len(document.blocks) if document is not None else 0,
                        documents=[doc_id] if document is not None else [])


async def repondre_guide(question: str, historique: list[Turn], profil: Profil, *, corpus: Any,
                         index: Any, client: Any, settings: Settings, request_id: str,
                         lang: str | None = None, doc_id: str | None = None, budget: Any = None,
                         pipeline_digest_hex: str | None = None,
                         prompts_digest_hex: str | None = None) -> tuple[Answer, Trace]:
    """Une question du guide → l'unique `Answer` d'AD-4 et sa `Trace`.

    Toute sortie normale — réponse, refus, clarification, claims toutes rejetées — est un `Answer`
    (l'API en fera un 200, AD-11). Seules les entrées hors bornes (`InvalidRequest`) et les échecs
    terminaux des étapes (`Timeout`, `LlmParse`, `BudgetExceeded`, `LlmUnavailable`) remontent.
    """
    doc_id = doc_id or settings.guide_doc_id
    if doc_id not in corpus.documents:
        # Document en quarantaine, absent ou mal nommé : `retrouver` lèverait un `KeyError` nu **après**
        # *comprendre*, donc après un appel facturé, et l'API en ferait un 500 (revue 1.5). AD-16 a un
        # code pour ça, et le contrôle a sa place ici, avec les autres bornes d'entrée.
        raise CorpusUnavailable(f"document {doc_id!r} non servi (absent du corpus ou en quarantaine)")
    # La longueur de `question` et celle de chaque tour sont bornées par le contrat HTTP d'AD-11
    # (≤ 1 000 et ≤ 2 000 caractères, story 1.6 ; `Turn.texte` porte déjà la seconde dans le domaine).
    # Le **nombre** de tours, lui, est une borne du pipeline : c'est lui qui passe l'historique à
    # *comprendre* et à *rédiger*.
    if len(historique) > settings.historique_max_turns:
        # Avant le premier appel modèle : une requête refusée ne coûte rien (AD-11 : 400 au-delà,
        # jamais tronqué côté serveur — tronquer perdrait le tour qui porte l'anaphore).
        raise InvalidRequest(f"historique de {len(historique)} tours : la limite est "
                             f"{settings.historique_max_turns} (jamais tronqué côté serveur)")
    if budget is None:
        budget = client.new_budget()

    steps: list[StepTrace] = []
    relances = 0
    truncated = False
    # AD-10 : l'`intent` est logué par l'API, qui ne voit que l'`Answer` et la `Trace` — et `Answer`
    # ne le porte pas. Il est renseigné dès que *comprendre* a rendu l'une de ses deux sorties
    # (`ParsedQuestion` ou `ClarificationRequise` : les deux portent un `intent`), et reste `None` si
    # l'étape n'a pas abouti.
    intent: str | None = None

    def echeance(avant: str) -> None:
        """AD-1/AD-9 : la deadline monotone est vérifiée **avant** chaque étape, jamais après coup."""
        if budget.remaining() <= 0:
            raise Timeout(f"deadline épuisée avant l'étape {avant} ({budget.remaining():.1f} s restantes)")

    def tracer() -> Trace:
        digest_pipeline, digest_prompts = (pipeline_digest_hex, prompts_digest_hex)
        if digest_pipeline is None or digest_prompts is None:
            defaut = digests()
            digest_pipeline = digest_pipeline if digest_pipeline is not None else defaut[0]
            digest_prompts = digest_prompts if digest_prompts is not None else defaut[1]
        entry = corpus.manifest.get(doc_id)
        # AD-10 : `retries` et `truncations` ne sont pas définis par le spine — ici, une fois pour
        # toutes : `retries` = les relances motivées du client (parse invalide) plus les relances de
        # *rédiger* décidées par AD-3 ; `truncations` = le retrieval coupé (0 ou 1 par requête).
        retries = sum(1 for s in steps for c in s.checks if c.name == "parse_retry") + relances
        return Trace(
            request_id=request_id, pipeline=PIPELINE, variant=VARIANT, intent=intent, steps=steps,
            total_cost_eur=round(budget.cost_eur, 4),
            source_hash={doc_id: entry.source_hash} if entry is not None else {},
            ingest_fingerprint={doc_id: entry.ingest_fingerprint} if entry is not None else {},
            pipeline_digest=digest_pipeline, prompts_digest=digest_prompts,
            thresholds=settings.thresholds(), retries=retries, truncations=int(truncated),
            deadline_remaining_s=round(budget.remaining(), 3),
        )

    def refuser(kind: str, parsed: ParsedQuestion | None, *, language: str,
                clarification: str | None = None) -> tuple[Answer, Trace]:
        echeance("restituer")  # *restituer* est une étape : la deadline se vérifie avant elle aussi
        answer, step = restituer(language=language, reason=_absence(kind, parsed, doc_id=doc_id, corpus=corpus),
                                 clarification=clarification)
        steps.append(step)
        return answer, tracer()

    async def chaine() -> tuple[Answer, Trace]:
        """Les cinq étapes. Sortie normale : un `Answer` et sa `Trace`. Échec terminal : `PipelineError`."""
        nonlocal relances, truncated, intent
        # --- comprendre -----------------------------------------------------
        echeance("comprendre")
        parsed, step_comprendre = await comprendre(question, historique, profil, client=client, budget=budget,
                                                   settings=settings, lang=lang)
        steps.append(step_comprendre)
        intent = parsed.intent  # AD-10 : les deux sorties de *comprendre* en portent un

        # AD-5 : deux sorties typées exclusives. Une question non autonome n'atteint jamais *retrouver*.
        if isinstance(parsed, ClarificationRequise):
            return refuser("clarification_requise", None, language=parsed.language,
                           clarification=parsed.clarification)
        if parsed.intent in INTENTS_REFUSES:
            # Court-circuit d'AD-5 : l'étage `reason` n'est jamais atteint pour un refus par intent.
            return refuser("hors_perimetre", None, language=parsed.language)

        # --- retrouver (code pur) -------------------------------------------
        echeance("retrouver")
        retrieval, step_retrouver = retrouver_deterministe(parsed, corpus=corpus, index=index,
                                                           budget=retrieval_budget(settings),
                                                           settings=settings, doc_id=doc_id)
        steps.append(step_retrouver)
        truncated = retrieval.truncated
        if not retrieval.blocs and retrieval.truncated:
            # AD-1, littéralement : « budget épuisé ou troncature non résolue ⇒ `complete=False` et
            # **aucune absence du corpus n'est affirmée** » (NFR2 le répète). Un retrieval vide *parce
            # que* le budget a tout écarté ne dit rien du corpus : le convertir en `zero_hit` fabriquerait
            # une preuve d'absence à partir d'une borne qui est la nôtre (revue Codex 1.5, B4). Il n'y a
            # pas d'`Answer` honnête à rendre ici — c'est une erreur terminale, avec son code.
            raise BudgetExceeded(
                f"le budget de retrieval n'a laissé passer aucun bloc ({settings.retrieval_max_blocks} blocs, "
                f"{settings.retrieval_max_tokens} tokens) : aucune absence du corpus n'est affirmée")
        if not retrieval.blocs:
            # Court-circuit « zéro bloc », **après** *retrouver* : distinct de celui d'AD-5, qui est fondé
            # sur le dictionnaire et reste désactivé tant que `validated=false` (story 2.1). Appeler
            # `reason` sans un seul bloc citable ne peut produire que des claims sans source — et coûte
            # le prix plein. L'`AbsenceProof` dit ce qui a été cherché, jamais que l'information n'existe
            # pas (AD-1).
            return refuser("zero_hit", parsed, language=parsed.language)

        # --- rédiger --------------------------------------------------------
        echeance("rediger")
        draft, step_rediger = await rediger(parsed, retrieval, historique, client=client, budget=budget,
                                            index=index, doc_id=doc_id, settings=settings)
        steps.append(step_rediger)

        # --- vérifier -------------------------------------------------------
        echeance("verifier")
        verification, step_verifier = await verifier(draft, parsed=parsed, retrieval=retrieval, corpus=corpus,
                                                     index=index, client=client, budget=budget, settings=settings)
        steps.append(step_verifier)

        # --- relance unique (AD-3) ------------------------------------------
        if verification.motif and relance_utile(verification, settings):
            acquise = verification  # ce qui est déjà vérifié : une relance ne peut que l'améliorer
            # Compteur d'appels **avant** l'appel en cours : c'est lui qui dit si un appel a démarré,
            # quoi qu'ait fait l'étape qui a échoué (voir le `except` plus bas). Il est **ré-armé**
            # après chaque appel réussi de la relance : une relance rédigée puis laissée sans seconde
            # vérification faute de budget n'est pas un appel raté, c'est un appel qui n'a jamais
            # démarré (revue Codex 1.5, tour 3 — mesuré en live, la chaîne ressortait en 503).
            appels_avant = budget.attempts
            try:
                if budget.remaining() <= settings.llm_retry_margin_s:
                    # AD-1, littéralement : « aucun retry ne démarre sans marge ». Le retry ne démarre
                    # pas ; la requête, elle, a déjà sa réponse vérifiée.
                    raise Timeout(f"marge insuffisante pour la relance ({budget.remaining():.1f} s restantes)")
                if budget.attempts + APPELS_DE_LA_RELANCE > budget.max_attempts:
                    # Même règle, appliquée au **compteur d'appels** : une relance, c'est deux appels
                    # — rédiger puis vérifier — et un draft relancé mais non vérifié n'est jamais
                    # montré (AD-3). Démarrer le premier en sachant que le second ne passera pas,
                    # c'est payer un appel `reason` pour rien (NFR4).
                    raise BudgetExceeded(
                        f"plafond d'appels trop bas pour la relance et sa vérification "
                        f"({budget.attempts}/{budget.max_attempts}, {APPELS_DE_LA_RELANCE} requis)")
                draft_2, step_rediger_2 = await rediger(parsed, retrieval, historique, client=client, budget=budget,
                                                        index=index, doc_id=doc_id, settings=settings,
                                                        motif=verification.motif)
                steps.append(step_rediger_2)
                appels_avant = budget.attempts  # la relance a abouti : seule la suite peut encore rater
                relances += 1
                if draft_2.digest() == draft.digest():
                    # AD-3 : « chaque relance change quelque chose ». Rien n'a changé : re-vérifier rendrait
                    # exactement le même résultat pour le prix d'un appel `micro` de plus.
                    step_rediger_2.checks.append(CheckResult(
                        name="relance_sans_effet", ok=False,
                        detail="l'ébauche relancée est identique (même hash canonique) : arrêt sur la première vérification"))
                else:
                    seconde, step_verifier_2 = await verifier(draft_2, parsed=parsed, retrieval=retrieval,
                                                              corpus=corpus, index=index, client=client,
                                                              budget=budget, settings=settings)
                    steps.append(step_verifier_2)
                    if domine(seconde, acquise):
                        verification = seconde
                    else:
                        # AD-3 relance pour **améliorer** : rien ne garantit que la seconde ébauche fasse
                        # mieux. Elle ne domine pas — la prendre jetterait des affirmations déjà
                        # vérifiées, dégraderait `complete` ou allongerait `unknown`, et pourrait
                        # transformer une réponse en refus `claims_rejetes` (revue 1.5). L'acquis fait
                        # foi, et la trace dit que la relance n'a pas payé.
                        step_verifier_2.checks.append(CheckResult(
                            name="relance_moins_bonne", ok=False,
                            detail=f"la relance ne domine pas la première vérification "
                                   f"({len(seconde.claims)} affirmation(s) contre {len(acquise.claims)}, "
                                   f"{len(seconde.facettes_couvertes)} facette(s) couverte(s) contre "
                                   f"{len(acquise.facettes_couvertes)}, "
                                   f"{len(blocs_cites(seconde))} bloc(s) cité(s) contre "
                                   f"{len(blocs_cites(acquise))}, "
                                   f"complete={seconde.complete} contre {acquise.complete}, "
                                   f"unknown={len(seconde.unknown)} contre {len(acquise.unknown)}) : "
                                   f"la première fait foi"))
            except (BudgetExceeded, Timeout, LlmParse, LlmUnavailable) as exc:
                # Deux situations qu'AD-16 sépare, et qu'il ne faut surtout pas confondre (revue Codex
                # 1.5, B5) :
                #
                # - **aucun appel n'a démarré** (plafond de coût ou d'appels atteint, marge de deadline
                #   insuffisante) : rien n'a été facturé, la relance est une *tentative d'amélioration*
                #   qui n'a pas eu lieu, et la réponse déjà vérifiée reste due. C'est le texte d'AD-1
                #   (« aucun retry ne démarre sans marge »), étendu aux euros par AD-4 ;
                # - **un appel a démarré et a échoué** (parse invalide après le retry du client, erreur
                #   fournisseur, timeout d'appel) : AD-16 est explicite — « un appel LLM en timeout, un
                #   parse invalide après 1 retry, un 429/529 fournisseur ⇒ 503 avec trace partielle ».
                #   L'avaler rendrait 200 sur une panne du fournisseur. L'erreur remonte donc, avec le
                #   `StepTrace` de l'appel raté et la trace partielle attachés.
                #
                # La question « un appel a-t-il démarré ? » se tranche sur le **budget**, pas sur ce
                # que l'étape a bien voulu attacher à son erreur (revue Codex 1.5, tour 2, B5) : le
                # compteur est incrémenté à l'envoi, par le client, pour toutes les étapes. Une étape
                # qui oublierait de renseigner `exc.step` — c'était le cas de *vérifier*, si bien
                # qu'une panne fournisseur pendant la **seconde vérification** ressortait en 200 —
                # ne peut plus faire passer un échec commencé pour une relance jamais partie. Le
                # `StepTrace` reste attaché quand l'étape l'a fourni : il porte le coût de l'appel raté.
                commence = budget.attempts > appels_avant or (exc.step is not None and bool(exc.step.calls))
                if commence:
                    if exc.step is not None:
                        steps.append(exc.step)
                    exc.trace = tracer()
                    raise
                # AD-4 : `complete=True` exige « aucune troncature de budget ». Une relance que le
                # plafond ou la deadline ont empêchée en est une : la réponse est servie, mais elle n'est
                # pas donnée pour complète. Un draft relancé mais **non vérifié** n'est jamais montré
                # (AD-3) : on repart de la vérification acquise.
                verification = acquise.model_copy(update={"complete": False})
                step_verifier.checks.append(CheckResult(
                    name="relance_abandonnee", ok=False,
                    detail=f"relance de rédiger non démarrée ({exc.code.value}) : "
                           f"la première vérification fait foi — {exc.message}"))
                if not verification.found:
                    # Rien de vérifié à servir : le refus motivé d'AD-3 est la seule sortie honnête, et
                    # c'est un `Answer` complet (200), pas une erreur.
                    step_verifier.checks[-1].detail += " ; aucune affirmation n'avait survécu"

        # --- restituer ------------------------------------------------------
        echeance("restituer")
        if not verification.found:
            # AD-3 : zéro claim survivante après la relance ⇒ refus motivé, jamais un dégradé silencieux.
            answer, step_restituer = restituer(
                language=parsed.language, verification=verification,
                reason=_absence("claims_rejetes", parsed, doc_id=doc_id, corpus=corpus))
        else:
            answer, step_restituer = restituer(language=parsed.language, verification=verification)
        steps.append(step_restituer)
        return answer, tracer()

    try:
        return await chaine()
    except PipelineError as exc:
        # AD-16 : « 503 avec trace partielle ». Ce qui a déjà tourné — étapes, appels, coût engagé —
        # voyage avec l'erreur, sinon l'API de 1.6 rendrait une panne sans rien pour la situer.
        if exc.trace is None:
            exc.trace = tracer()
        raise
