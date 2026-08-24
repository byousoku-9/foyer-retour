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
            request_id=request_id, pipeline=PIPELINE, variant=VARIANT, steps=steps,
            total_cost_eur=round(budget.cost_eur, 4),
            source_hash={doc_id: entry.source_hash} if entry is not None else {},
            ingest_fingerprint={doc_id: entry.ingest_fingerprint} if entry is not None else {},
            pipeline_digest=digest_pipeline, prompts_digest=digest_prompts,
            thresholds=settings.thresholds(), retries=retries, truncations=int(truncated),
            deadline_remaining_s=round(budget.remaining(), 3),
        )

    def refuser(kind: str, parsed: ParsedQuestion | None, *, language: str,
                clarification: str | None = None) -> tuple[Answer, Trace]:
        answer, step = restituer(language=language, reason=_absence(kind, parsed, doc_id=doc_id, corpus=corpus),
                                 clarification=clarification)
        steps.append(step)
        return answer, tracer()

    # --- comprendre -----------------------------------------------------
    echeance("comprendre")
    parsed, step_comprendre = await comprendre(question, historique, profil, client=client, budget=budget,
                                               settings=settings, lang=lang)
    steps.append(step_comprendre)

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
        try:
            if budget.remaining() <= settings.llm_retry_margin_s:
                # AD-1, littéralement : « aucun retry ne démarre sans marge ». Le retry ne démarre
                # pas ; la requête, elle, a déjà sa réponse vérifiée.
                raise Timeout(f"marge insuffisante pour la relance ({budget.remaining():.1f} s restantes)")
            draft_2, step_rediger_2 = await rediger(parsed, retrieval, historique, client=client, budget=budget,
                                                    index=index, doc_id=doc_id, settings=settings,
                                                    motif=verification.motif)
            steps.append(step_rediger_2)
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
                if len(seconde.claims) >= len(acquise.claims):
                    verification = seconde
                else:
                    # AD-3 relance pour **améliorer** : rien ne garantit que la seconde ébauche fasse
                    # mieux. Elle retient moins — la remplacer jetterait des affirmations déjà
                    # vérifiées, et pourrait transformer une réponse en refus `claims_rejetes`
                    # (revue 1.5). L'acquis fait foi, et la trace dit que la relance n'a pas payé.
                    step_verifier_2.checks.append(CheckResult(
                        name="relance_moins_bonne", ok=False,
                        detail=f"la relance retient {len(seconde.claims)} affirmation(s) contre "
                               f"{len(acquise.claims)} : la première vérification fait foi"))
        except (BudgetExceeded, Timeout, LlmParse, LlmUnavailable) as exc:
            # La relance est une **tentative d'amélioration**, pas la réponse. Le plafond par requête
            # (NFR4) ou la deadline (AD-1) l'arrêtent avant tout appel facturé — mesuré en 1.5 : sur un
            # cache de préfixe froid, `comprendre + rédiger` engagent déjà ≈ 0,065 € des 0,10 €, et le
            # majorant de la relance seul vaut ≈ 0,044 €. Un second appel `reason` peut aussi échouer
            # au parse ou chez le fournisseur : même surface d'échec, même traitement (revue 1.5).
            # Rendre 503 ici jetterait une réponse déjà vérifiée : ce n'est pas un dégradé silencieux,
            # c'est le contraire, et la trace le dit. Un draft relancé mais **non vérifié** n'est
            # jamais montré (AD-3) : on repart de la vérification acquise.
            # AD-4 : `complete=True` exige « aucune troncature de budget ». Une relance que le
            # plafond ou la deadline ont empêchée en est une : la réponse est servie, mais elle n'est
            # pas donnée pour complète.
            verification = acquise.model_copy(update={"complete": False})
            step_verifier.checks.append(CheckResult(
                name="relance_abandonnee", ok=False,
                detail=f"relance de rédiger non menée à terme ({exc.code.value}) : "
                       f"la première vérification fait foi — {exc.message}"))
            if not verification.found:
                # Rien de vérifié à servir : le refus motivé d'AD-3 est la seule sortie honnête, et
                # c'est un `Answer` complet (200), pas une erreur.
                step_verifier.checks[-1].detail += " ; aucune affirmation n'avait survécu"

    # --- restituer ------------------------------------------------------
    if not verification.found:
        # AD-3 : zéro claim survivante après la relance ⇒ refus motivé, jamais un dégradé silencieux.
        answer, step_restituer = restituer(
            language=parsed.language, verification=verification,
            reason=_absence("claims_rejetes", parsed, doc_id=doc_id, corpus=corpus))
    else:
        answer, step_restituer = restituer(language=parsed.language, verification=verification)
    steps.append(step_restituer)
    return answer, tracer()
