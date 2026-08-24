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

from functools import lru_cache
from typing import Any

from server.app.config import Settings
from server.app.digests import pipeline_digest, prompts_digest
from server.app.domain.answer import AbsenceProof, Answer, Verification
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
from server.app.domain.retrieval import RetrievalBudget
from server.app.domain.trace import CheckResult, StepTrace, Trace
from server.app.steps.comprendre import comprendre
from server.app.steps.rediger import rediger
from server.app.steps.restituer import restituer
from server.app.steps.retrouver import retrouver_deterministe
from server.app.steps.verifier import verifier

PIPELINE = "guide"
VARIANT = "deterministe"
# AD-5 : les intents qui se tranchent sur la seule sortie de *comprendre*, avant tout appel `reason`.
INTENTS_REFUSES = frozenset({"meteo", "bavardage", "hors_perimetre"})


@lru_cache(maxsize=1)
def _digests() -> tuple[str, str]:
    """Repli mémoïsé : `pipeline_digest()`/`prompts_digest()` relisent toute l'arborescence du code.

    L'appelant les calcule une fois au démarrage (story 1.6) et les passe ; sans lui, on les calcule
    au premier appel et on les garde — jamais à chaque requête (des dizaines de fichiers lus).
    """
    return pipeline_digest(), prompts_digest()


def _retrieval_budget(settings: Settings) -> RetrievalBudget:
    """AD-1 : le budget borne **toute** l'étape. Reprise 1.4 : `max_blocks`/`max_tokens` venaient de
    `config.py` mais personne ne les renseignait — *rédiger* levait `BudgetExceeded` sur une fiche
    entière au lieu de recevoir un retrieval borné."""
    return RetrievalBudget(max_opens=settings.max_opens, node_window=settings.node_window,
                           search_limit=settings.search_limit, max_llm_turns=settings.max_llm_turns,
                           max_blocks=settings.retrieval_max_blocks,
                           max_tokens=settings.retrieval_max_tokens)


# AD-3 nomme les motifs de relance par des défauts de **citation** (« quote introuvable dans block_id
# X, bloc heading, quote trop courte ») : ce sont eux que le modèle peut corriger en recopiant mieux.
REJETS_DE_CITATION = frozenset({"non_retrouvee", "ambigue"})
# Ce que coûte la relance d'AD-3 en **appels** : rédiger une seconde fois, puis vérifier ce qu'elle a
# rendu. Les deux sont indissociables — AD-3 interdit de montrer un draft relancé mais non vérifié.
APPELS_DE_LA_RELANCE = 2


def _blocs_cites(verification: Verification) -> set[str]:
    """Les blocs du corpus sur lesquels repose ce qui est **affiché** : l'identité stable d'un contenu.

    Ni les `claim_id` (refaits à neuf par chaque appel de *rédiger*) ni les offsets d'une quote (qui
    bougent dès que le modèle recopie un passage un peu plus large) ne sont comparables d'une ébauche
    à l'autre. Le `block_id`, lui, est **notre** identifiant, produit par l'ingestion : deux ébauches
    de la même question qui s'appuient sur le même passage citent le même bloc.
    """
    return {q.block_id for c in verification.claims for q in c.quotes}


def _domine(seconde: Verification, acquise: Verification) -> bool:
    """La seconde vérification est-elle au moins aussi bonne que l'acquise, sur **tous** les axes ?

    AD-3 relance pour *améliorer*. Compter les seules claims laissait passer une relance qui, à
    nombre égal, perdait `complete`, ajoutait un `unknown` ou remplaçait une affirmation par une
    autre moins bien placée (revue Codex 1.5, I2). La dominance est donc explicite, et elle porte sur
    des **ensembles** là où des compteurs ne suffisent pas : deux vérifications qui couvrent chacune
    une facette *différente* ont le même compte, et prendre la seconde échangerait une sous-question
    contre une autre (tour 3, I2). Sont donc exigés : trouver au moins autant, garder au moins autant
    d'affirmations, couvrir **au moins les mêmes facettes**, s'appuyer sur **au moins les mêmes
    blocs**, ne pas déclarer moins complet, ne pas déclarer plus d'inconnu. À égalité non dominante,
    l'acquis fait foi.

    Les rangs de facettes sont stables entre les deux ébauches : le découpage vient de *comprendre*,
    qui n'a tourné qu'une fois pour la requête (AD-4). Les `block_id` le sont aussi — ils viennent de
    l'ingestion. C'est ce qui rend la comparaison possible sans rien inventer ; l'appariement plus fin
    des passages (mêmes offsets, même phrase) reste une reprise ouverte vers 4.2.
    """
    return (seconde.found >= acquise.found
            and len(seconde.claims) >= len(acquise.claims)
            and set(seconde.facettes_couvertes) >= set(acquise.facettes_couvertes)
            and _blocs_cites(seconde) >= _blocs_cites(acquise)
            and seconde.complete >= acquise.complete
            and len(seconde.unknown) <= len(acquise.unknown))


def _relance_utile(verification: Verification, settings: Settings) -> bool:
    """Une relance de *rédiger* a-t-elle une chance de changer quelque chose (AD-3) ?

    Oui dans deux cas, et deux seulement : un défaut de citation (le modèle peut recopier
    correctement), ou **rien** qui ait survécu (la relance est alors le seul chemin vers une réponse,
    et AD-3 fait du refus `claims_rejetes` ce qui vient *après* elle).

    Non quand la réponse tient déjà et que les seuls rejets sont des jugements de pertinence : les
    claims écartées sont **conservées** dans `rejected_claims[]` comme AD-3 le demande, et payer un
    second appel `reason` (≈ 0,03 €, le tiers du budget de la requête) pour rattraper une affirmation
    que le code a décidé de ne pas montrer contredirait NFR4. C'est une lecture d'AD-3 — dont les
    exemples de motifs sont tous des défauts de citation — et non une évidence : le seuil
    `relance_sur_non_pertinence` la rend explicite et mesurable par les questions-témoins (4.2).
    """
    if not verification.rejected_claims:
        return False
    if not verification.found:
        return True
    if settings.relance_sur_non_pertinence:
        return True
    return any(c.rejection_kind in REJETS_DE_CITATION for c in verification.rejected_claims)


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
            defaut = _digests()
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
                                                           budget=_retrieval_budget(settings),
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
        if verification.motif and _relance_utile(verification, settings):
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
                    if _domine(seconde, acquise):
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
                                   f"{len(_blocs_cites(seconde))} bloc(s) cité(s) contre "
                                   f"{len(_blocs_cites(acquise))}, "
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
