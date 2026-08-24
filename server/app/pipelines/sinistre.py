"""AD-1 / AD-6 / AD-16 — Le pipeline sinistre : les mêmes cinq étapes, un verdict au bout.

`comprendre → retrouver → rédiger → vérifier → restituer`, dans cet ordre, toujours — exactement la
chaîne du guide (AD-1 : « l'ordre est constant »). Le sinistre ne fabrique **aucune** étape nouvelle :
il donne aux étapes existantes des prompts dédiés, leur transmet les faits déclarés, et laisse
*vérifier* dériver `applicable` puis appliquer la table d'AD-6. Le contrat de sortie est l'unique
`Answer` d'AD-4, dont `verdict` est un champ — il n'y a pas de second objet de réponse.

Trois différences avec `guide.py`, et rien d'autre :

- **les faits** : `Faits` est une entrée de plein droit (AD-5 les nomme déjà), bornée par le domaine
  (`description ≤ 2 000` caractères) et rejetée, jamais tronquée ;
- **la recherche** : *retrouver* reçoit `kinds_prioritaires` — à score égal, les blocs
  `garantie|exclusion|condition|franchise` passent devant (AC de la story) ;
- **le refus** : AD-16 interdit tout repli côté sinistre, et un refus sinistre porte donc un verdict
  `ne_tranche_pas` composé par le code, jamais un verdict vide. `ne_tranche_pas` n'est pas un repli :
  c'est le résultat d'une table qui n'a rien trouvé à trancher, et il est dit comme tel.

Comme le guide, le pipeline ne voit ni `corpus` ni `llm` (table des couches) : `corpus`, `index` et
`client` sont annotés `Any` et lui viennent de l'appelant (l'API, story 1.9).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from server.app.config import Settings
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
from server.app.domain.question import ClarificationRequise, Faits, ParsedQuestion
from server.app.domain.trace import CheckResult, StepTrace, Trace
from server.app.domain.verdict import KINDS_DECISIONNELS, MissingPackage, PORTEE, Verdict
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

PIPELINE = "sinistre"
# AD-1 : *retrouver* a deux variantes (déterministe en code pur, agentique en epic 4). À J+1 le
# sinistre n'en connaît qu'une, et une variante inconnue est refusée **avant** tout appel facturé
# plutôt que traitée comme la déterministe (AD-16 : jamais de dégradé silencieux).
VARIANTES = frozenset({"deterministe"})
VARIANT = "deterministe"

# Ce que le refus d'un sinistre annonce, par `AbsenceProof.kind`. Composé par le **code**, comme les
# phrases de `restituer.PHRASES_DE_REFUS` : aucune de ces situations n'est une lecture du contrat.
RAISONS_DE_REFUS: dict[str, str] = {
    "hors_perimetre": "La demande ne porte pas sur ce que couvre un contrat d'assurance habitation",
    "zero_hit": "Aucune clause du contrat n'a été retrouvée sur les termes du sinistre décrit",
    "claims_rejetes": "Aucune clause citée n'a passé le contrôle des citations",
    "clarification_requise": "La demande n'a pas pu être rendue autonome : rien n'a été cherché",
}


def _verdict_de_refus(kind: str) -> Verdict:
    """AD-16 : un refus sinistre porte `ne_tranche_pas`, jamais rien.

    Le front sinistre affiche d'abord un badge de verdict ; sans verdict, il n'aurait qu'une absence à
    montrer là où l'utilisateur attend une position. `ne_tranche_pas` **est** cette position, et sa
    raison dit franchement pourquoi la table n'a rien eu à trancher. Le paquet manquant reste entier :
    rien n'a été vérifié, donc rien n'est acquis.
    """
    return Verdict(value="ne_tranche_pas",
                   reason=f"{RAISONS_DE_REFUS.get(kind, RAISONS_DE_REFUS['claims_rejetes'])} ({PORTEE})",
                   missing=MissingPackage(),
                   escalate=["Aucune clause du contrat n'a pu être opposée au sinistre : "
                             "reprendre le dossier à la main."])


# Borne du domaine, lue sur le schéma : `Faits.description` la porte (AD-11), et la recopier ici en
# dur la ferait mentir le jour où elle bouge.
_DESCRIPTION_MAX = next((m.max_length for m in Faits.model_fields["description"].metadata
                         if getattr(m, "max_length", None) is not None), None)


def _faits(faits: Faits | Mapping[str, Any]) -> Faits:
    """Borne d'entrée (AD-11/AD-16) : les faits sont **rejetés** hors bornes, jamais tronqués.

    Le domaine porte déjà la borne (`Faits.description ≤ 2 000` caractères) ; ce qu'ajoute le pipeline,
    c'est le **code d'erreur** : une `ValidationError` pydantic remontée telle quelle deviendrait un
    500 `internal` (AD-16), là où l'appelant a simplement envoyé une description trop longue. Tronquer
    serait pire : la fin d'une description de sinistre porte souvent la cause.
    """
    if isinstance(faits, Faits):
        return faits
    try:
        return Faits.model_validate(dict(faits))
    except ValidationError as exc:
        # AD-15 : rien de ce que l'appelant a envoyé n'entre dans le message — ni la valeur, ni le
        # chemin pydantic, qui peut porter une clé inconnue de son cru. Seuls le **compte** d'erreurs
        # et notre propre borne, lue sur le schéma pour qu'elle ne se désynchronise pas du domaine.
        raise InvalidRequest(f"faits du sinistre hors bornes : {exc.error_count()} champ(s) invalide(s) "
                             f"(la description est limitée à {_DESCRIPTION_MAX} caractères, "
                             f"jamais tronquée côté serveur)") from exc


async def run(doc_id: str | None, question: str, faits: Faits | Mapping[str, Any], *, corpus: Any,
              index: Any, client: Any, settings: Settings, request_id: str,
              variant: str = "deterministe", lang: str | None = None, deadline_s: float | None = None,
              budget: Any = None, pipeline_digest_hex: str | None = None,
              prompts_digest_hex: str | None = None) -> tuple[Answer, Trace]:
    """Un sinistre décrit → l'unique `Answer` d'AD-4, verdict compris, et sa `Trace`.

    Toute sortie normale — verdict, refus, clauses toutes rejetées — est un `Answer` (l'API en fera un
    200, AD-11). Seules les entrées hors bornes (`InvalidRequest`), le document non servi
    (`CorpusUnavailable`) et les échecs terminaux des étapes (`Timeout`, `LlmParse`, `BudgetExceeded`,
    `LlmUnavailable`) remontent — sans repli, AD-16.
    """
    if variant not in VARIANTES:
        # Avant tout appel facturé : une variante inconnue est une faute d'appel, pas un cas à traiter.
        raise InvalidRequest(f"variante de recherche inconnue : {variant!r} "
                             f"(connues : {', '.join(sorted(VARIANTES))})")
    faits = _faits(faits)
    doc_id = doc_id or settings.sinistre_doc_id
    if doc_id not in corpus.documents:
        # Contrat absent, mal nommé ou en quarantaine (AD-7) : levé **avant** le premier appel modèle.
        raise CorpusUnavailable(f"document {doc_id!r} non servi (absent du corpus ou en quarantaine)")
    if budget is None:
        budget = client.new_budget(deadline_s=deadline_s) if deadline_s is not None else client.new_budget()

    steps: list[StepTrace] = []
    relances = 0
    truncated = False
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
        retries = sum(1 for s in steps for c in s.checks if c.name == "parse_retry") + relances
        return Trace(
            request_id=request_id, pipeline=PIPELINE, variant=variant, intent=intent, steps=steps,
            total_cost_eur=round(budget.cost_eur, 4),
            source_hash={doc_id: entry.source_hash} if entry is not None else {},
            ingest_fingerprint={doc_id: entry.ingest_fingerprint} if entry is not None else {},
            pipeline_digest=digest_pipeline, prompts_digest=digest_prompts,
            thresholds=settings.thresholds(), retries=retries, truncations=int(truncated),
            deadline_remaining_s=round(budget.remaining(), 3),
        )

    def absence(kind: str, parsed: ParsedQuestion | None) -> AbsenceProof:
        """Preuve d'absence (AD-4) : ce qui a été cherché, jamais les variantes ni les déclencheurs."""
        if parsed is None:  # rien n'a été cherché : ni termes, ni passages parcourus
            return AbsenceProof(kind=kind)
        document = corpus.documents.get(doc_id)
        return AbsenceProof(kind=kind, terms_searched=parsed.termes_de_recherche(), variants_count=0,
                            blocks_scanned=len(document.blocks) if document is not None else 0,
                            documents=[doc_id] if document is not None else [])

    def refuser(kind: str, parsed: ParsedQuestion | None, *, language: str,
                clarification: str | None = None) -> tuple[Answer, Trace]:
        echeance("restituer")  # *restituer* est une étape : la deadline se vérifie avant elle aussi
        answer, step = restituer(language=language, reason=absence(kind, parsed),
                                 clarification=clarification, verdict=_verdict_de_refus(kind))
        steps.append(step)
        return answer, tracer()

    async def chaine() -> tuple[Answer, Trace]:
        """Les cinq étapes. Sortie normale : un `Answer` et sa `Trace`. Échec terminal : `PipelineError`."""
        nonlocal relances, truncated, intent
        # --- comprendre -----------------------------------------------------
        echeance("comprendre")
        # Ni historique ni profil : un dossier de sinistre n'est pas une conversation, et le profil du
        # guide (enfants, véhicule, statut) n'oriente aucune clause. Ce sont les **faits** qui portent
        # le contexte, et ils partent délimités par `untrusted()` (AD-15).
        parsed, step_comprendre = await comprendre(question, [], Profil(), client=client, budget=budget,
                                                   settings=settings, lang=lang,
                                                   prompt="comprendre_sinistre", faits=faits)
        steps.append(step_comprendre)
        intent = parsed.intent

        if isinstance(parsed, ClarificationRequise):
            return refuser("clarification_requise", None, language=parsed.language,
                           clarification=parsed.clarification)
        if parsed.intent in INTENTS_REFUSES:
            # Court-circuit d'AD-5 : l'étage `reason` n'est jamais atteint pour un refus.
            return refuser("hors_perimetre", None, language=parsed.language)

        # --- retrouver (code pur) -------------------------------------------
        echeance("retrouver")
        retrieval, step_retrouver = retrouver_deterministe(
            parsed, corpus=corpus, index=index, budget=retrieval_budget(settings), settings=settings,
            doc_id=doc_id, kinds_prioritaires=KINDS_DECISIONNELS)
        steps.append(step_retrouver)
        truncated = retrieval.truncated
        if not retrieval.blocs and retrieval.truncated:
            # AD-1 : un retrieval vidé **par le budget** ne dit rien du contrat. Le convertir en
            # `zero_hit` fabriquerait une absence à partir d'une borne qui est la nôtre.
            raise BudgetExceeded(
                f"le budget de retrieval n'a laissé passer aucun bloc ({settings.retrieval_max_blocks} blocs, "
                f"{settings.retrieval_max_tokens} tokens) : aucune absence du contrat n'est affirmée")
        if not retrieval.blocs:
            return refuser("zero_hit", parsed, language=parsed.language)

        # --- rédiger --------------------------------------------------------
        echeance("rediger")
        draft, step_rediger = await rediger(parsed, retrieval, [], client=client, budget=budget,
                                            index=index, doc_id=doc_id, settings=settings,
                                            prompt="rediger_sinistre")
        steps.append(step_rediger)

        # --- vérifier -------------------------------------------------------
        echeance("verifier")
        verification, step_verifier = await verifier(draft, parsed=parsed, retrieval=retrieval,
                                                     corpus=corpus, index=index, client=client,
                                                     budget=budget, settings=settings, faits=faits)
        steps.append(step_verifier)

        # --- relance unique (AD-3) ------------------------------------------
        if verification.motif and relance_utile(verification, settings):
            acquise = verification
            appels_avant = budget.attempts
            try:
                if budget.remaining() <= settings.llm_retry_margin_s:
                    raise Timeout(f"marge insuffisante pour la relance ({budget.remaining():.1f} s restantes)")
                if budget.attempts + APPELS_DE_LA_RELANCE > budget.max_attempts:
                    raise BudgetExceeded(
                        f"plafond d'appels trop bas pour la relance et sa vérification "
                        f"({budget.attempts}/{budget.max_attempts}, {APPELS_DE_LA_RELANCE} requis)")
                draft_2, step_rediger_2 = await rediger(parsed, retrieval, [], client=client, budget=budget,
                                                        index=index, doc_id=doc_id, settings=settings,
                                                        motif=verification.motif, prompt="rediger_sinistre")
                steps.append(step_rediger_2)
                appels_avant = budget.attempts  # la relance a abouti : seule la suite peut encore rater
                relances += 1
                if draft_2.digest() == draft.digest():
                    step_rediger_2.checks.append(CheckResult(
                        name="relance_sans_effet", ok=False,
                        detail="l'ébauche relancée est identique (même hash canonique) : arrêt sur la première vérification"))
                else:
                    seconde, step_verifier_2 = await verifier(draft_2, parsed=parsed, retrieval=retrieval,
                                                              corpus=corpus, index=index, client=client,
                                                              budget=budget, settings=settings, faits=faits)
                    steps.append(step_verifier_2)
                    if domine(seconde, acquise):
                        verification = seconde
                    else:
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
                # Même partage qu'au guide (AD-16, revue Codex 1.5, B5) : un appel **commencé** qui
                # échoue reste terminal, une relance qui n'a jamais démarré laisse l'acquis servir.
                commence = budget.attempts > appels_avant or (exc.step is not None and bool(exc.step.calls))
                if commence:
                    if exc.step is not None:
                        steps.append(exc.step)
                    exc.trace = tracer()
                    raise
                verification = acquise.model_copy(update={"complete": False})
                step_verifier.checks.append(CheckResult(
                    name="relance_abandonnee", ok=False,
                    detail=f"relance de rédiger non démarrée ({exc.code.value}) : "
                           f"la première vérification fait foi — {exc.message}"))
                if not verification.found:
                    step_verifier.checks[-1].detail += " ; aucune affirmation n'avait survécu"

        # --- restituer ------------------------------------------------------
        echeance("restituer")
        if not verification.found:
            # AD-3 : zéro claim survivante après la relance ⇒ refus motivé. Le verdict, lui, a bien été
            # calculé par *vérifier* sur zéro clause affichée : c'est un `ne_tranche_pas` gagné, et
            # *restituer* le recopie tel quel (AD-16 : jamais un refus sinistre sans verdict).
            verification = _verdict_par_defaut(verification)
            answer, step_restituer = restituer(language=parsed.language, verification=verification,
                                               reason=absence("claims_rejetes", parsed))
        else:
            answer, step_restituer = restituer(language=parsed.language, verification=verification)
        steps.append(step_restituer)
        return answer, tracer()

    try:
        return await chaine()
    except PipelineError as exc:
        # AD-16 : « 503 avec trace partielle ». Ce qui a déjà tourné voyage avec l'erreur.
        if exc.trace is None:
            exc.trace = tracer()
        raise


def _verdict_par_defaut(verification: Verification) -> Verification:
    """Ceinture d'AD-16 : une vérification sinistre sans verdict n'atteint jamais *restituer*.

    *vérifier* en mode sinistre en calcule toujours un — y compris sur zéro claim affichée, où la
    table rend `ne_tranche_pas` par sa règle (0bis). Le cas ne se produit donc que si l'acquis vient
    d'ailleurs (une vérification construite hors du mode sinistre par un appelant futur) ; il vaut
    mieux le combler ici, où l'on sait que la requête est un sinistre, que laisser passer un refus
    sans verdict jusqu'au front.
    """
    if verification.verdict is not None:
        return verification
    return verification.model_copy(update={"verdict": _verdict_de_refus("claims_rejetes")})
