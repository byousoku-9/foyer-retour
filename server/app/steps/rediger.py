"""AD-3 / AD-9 — *rédiger* : un appel `reason`, sortie structurée `AnswerDraft` du domaine — ses
invariants (min 1 quote, une quote par bloc, cohérence segments/claims) se valident au parse et
déclenchent le retry motivé du client.

Préfixe système byte-identique = `commun.md` + `rediger.md` + sommaire versionné du document (avec
son en-tête de hash) — FR13 : le sommaire vit dans le préfixe cacheable (breakpoint 1 h, relu à 0,1×
après la première écriture). Historique, blocs (avec leur `block_id`) et question résolue viennent
**après**, chacun délimité par `untrusted()` (AD-15) — le `motif` de relance de la story 1.5 aussi,
quand il est présent : il est composé à partir de sorties de modèle et de texte de blocs.
"""

from __future__ import annotations

import json
import time

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.domain.answer import AnswerDraft, AnswerSegment
from server.app.domain.langue import LANGUES_SERVIES
from server.app.domain.errors import PipelineError
from server.app.domain.question import ParsedQuestion, Turn
from server.app.domain.retrieval import RetrievalResult
from server.app.domain.trace import StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import EFFORT_PAR_PROMPT, STEP_TIERS
from server.app.llm.prompting import load_prompt, render_prompt, untrusted


def _rattacher_claims_sinistre(draft: AnswerDraft, settings: Settings) -> tuple[AnswerDraft, int]:
    """Fait des claims atomiques le texte factuel effectivement soumis à *vérifier*.

    Campagne réelle 2.7 : le rédacteur savait citer l'exclusion animale de `p35:2`, mais pouvait
    créer une claim distincte sans la rattacher à aucun segment. La citation et sa pertinence
    passaient alors tous les contrôles, avant que la claim ne soit justement rejetée `non_citee`.

    En sinistre, `Claim.text` est déjà l'affirmation atomique (« une seule clause par affirmation »)
    que *vérifier* confronte aux passages. La projection reconstruit donc exactement un segment
    factuel par claim : ordre de la première référence dans le brouillon, puis claims orphelines dans
    leur ordre. Les transitions et limites ne gardent aucun `claim_id` et n'occupent que les places
    restantes sous `draft_max_segments`. L'invariant de configuration
    `draft_max_claims <= draft_max_segments` garantit ainsi qu'aucune claim autorisée n'est perdue.
    Le guide conserve son brouillon à l'octet près, comme sa variante 2.6.
    """
    par_id = {claim.claim_id: claim for claim in draft.claims}
    ordre: list[str] = []
    vus: set[str] = set()
    for segment in draft.segments:
        if segment.kind != "factuel":
            continue
        for cid in segment.claim_ids:
            if cid in par_id and cid not in vus:
                ordre.append(cid)
                vus.add(cid)
    for claim in draft.claims:
        if claim.claim_id not in vus:
            ordre.append(claim.claim_id)
            vus.add(claim.claim_id)

    if len(ordre) > settings.draft_max_segments:
        raise ValueError("plus de claims que de segments autorisés : la configuration doit garantir "
                         "draft_max_claims <= draft_max_segments")
    factuels = [AnswerSegment(text=par_id[cid].text.strip(), kind="factuel", claim_ids=[cid])
                for cid in ordre]
    place = settings.draft_max_segments - len(factuels)
    non_factuels = [AnswerSegment(text=segment.text, kind=segment.kind, claim_ids=[])
                    for segment in draft.segments if segment.kind != "factuel"][:place]
    segments = [*factuels, *non_factuels]
    changements = sum(1 for avant, apres in zip(draft.segments, segments, strict=False)
                       if avant != apres) + abs(len(draft.segments) - len(segments))
    if not changements:
        return draft, 0
    return draft.model_copy(update={"segments": segments}), changements


async def rediger(parsed: ParsedQuestion, retrieval: RetrievalResult, historique: list[Turn], *,
                  client: LlmClient, budget: RequestBudget, index: Index, doc_id: str,
                  settings: Settings, motif: str | None = None,
                  prompt: str = "rediger", max_tokens: int | None = None
                  ) -> tuple[AnswerDraft, StepTrace]:
    """`prompt` nomme le fichier de `llm/prompts/` inséré entre `commun.md` et le sommaire.

    Story 1.8 : le sinistre passe `prompt="rediger_sinistre"` — mêmes contrats d'entrée et de sortie,
    consigne « une seule clause par affirmation » en plus (AD-6). Le défaut **est** le guide : son
    préfixe reste byte-identique, donc cacheable (AD-9) et rejouable depuis ses fixtures live.
    `max_tokens=None` conserve le plafond commun ; l'override permet au seul pipeline outils de
    transmettre son seuil mesuré sans dupliquer l'appel LLM ni modifier les autres chemins.
    """
    t0 = time.monotonic()
    step = StepTrace(name="rediger", tier=STEP_TIERS["rediger"],
                     opened_block_ids=[b.block_id for b in retrieval.blocs])
    etrangers = [b.block_id for b in retrieval.blocs if index.doc_of(b.block_id) != doc_id]
    if etrangers:
        # AD-1/AD-9 (revue Codex 1.4, I3) : le sommaire du préfixe situe les blocs dans *leur* document.
        # Un `doc_id` qui ne recouvre pas les blocs reçus enverrait au modèle le mauvais plan de lecture
        # sans aucune erreur — AD-16 : jamais de dégradé silencieux.
        raise ValueError(f"blocs hors du document {doc_id!r} : {etrangers}")
    prefix = load_prompt("commun") + "\n\n" + render_prompt(
        prompt, quote_min_chars=settings.quote_min_chars, quote_max_chars=settings.quote_max_chars,
        draft_max_segments=settings.draft_max_segments, draft_max_claims=settings.draft_max_claims,
    ) + "\n\n" + index.sommaire(doc_id)
    parts = [untrusted("historique", json.dumps([{"role": t.role, "texte": t.texte} for t in historique],
                                                ensure_ascii=False))]
    parts += [untrusted("document", f"{b.block_id}\n{b.text}") for b in retrieval.blocs]
    parts.append(untrusted("question", parsed.question_resolue))
    tail = (f"Langue de rédaction : {LANGUES_SERVIES[parsed.language]} ({parsed.language}). "
            "Les citations restent recopiées mot pour mot dans la langue du bloc source.")
    if prompt == "rediger_sinistre":
        # A11 : avec deux facettes déjà arrêtées par *comprendre*, le modèle pouvait développer
        # quatre claims et plusieurs transitions jusqu'à `max_tokens`, puis recommencer. Le nombre
        # ci-dessous vient du contrat `ParsedQuestion` de la requête — ce n'est ni un nouveau seuil,
        # ni une déduction depuis le texte. La consigne vit dans le message dynamique pour garder le
        # préfixe cacheable byte-identique, et demande seulement d'éviter les redites entre facettes.
        tail += (f"\nPlan de sortie concis : {len(parsed.facettes)} facette(s) ont déjà été extraites. "
                 "Traite chacune au plus une fois, dès les premiers segments, avec seulement les "
                 "claims directement nécessaires. Pour chaque clause utile, rends une claim d'une "
                 "seule phrase courte et la plus courte quote contiguë qui la soutient ; n'énumère "
                 "pas les autres items d'une liste contractuelle. N'ajoute ni transition, ni "
                 "reformulation de contexte, ni segment limite si les claims factuelles suffisent.")
        limites_portees = [b.block_id for b in retrieval.blocs
                            if b.kind in {"exclusion", "condition", "franchise"}
                            and b.scope_node_ids]
        if limites_portees:
            # Story 3.3 : le code aval est seul autorisé à décider qu'une portée explicite ne couvre
            # pas le cas. Si la rédaction omet la clause, cette décision pure ne peut jamais devenir
            # visible. Les IDs viennent du corpus typé, pas de la question, et la consigne s'applique
            # uniformément à toute limite explicitement bornée retrouvée.
            tail += ("\nLimites à rendre vérifiables : " + ", ".join(limites_portees) +
                     ". Pour chacun de ces blocs à portée explicite, rends une claim courte avec une "
                     "citation contiguë, même si sa portée semble différente du cas : ne décide pas "
                     "toi-même de son applicabilité, le code la calculera et affichera la raison.")
        definitions = [b.block_id for b in retrieval.blocs
                       if b.kind == "definition" and b.defines]
        if definitions:
            # `definitions()` a déjà résolu la proximité de portée et les overrides. Une définition
            # ainsi sélectionnée mais omise par la rédaction rendrait cette résolution invisible ;
            # le modèle la transcrit, sans refaire le choix sémantique acquis par le code.
            tail += ("\nDéfinitions applicables à rendre vérifiables : " + ", ".join(definitions) +
                     ". Pour chacun de ces blocs déjà résolus par portée, rends une claim courte "
                     "avec une citation contiguë ; n'en substitue pas une autre et n'en déduis pas "
                     "une conclusion que son texte ne porte pas.")
    if motif is not None:
        # AD-15 : le motif vient de *vérifier* (1.5), qui le compose à partir de la sortie du modèle et
        # du texte des blocs — il est délimité comme tout le reste, jamais concaténé en clair.
        tail += "\n" + untrusted("motif", motif)
    content = "\n\n".join(parts) + "\n\n" + tail
    try:
        result = await client.parse(tier=STEP_TIERS["rediger"], system_prefix=prefix,
                                    messages=[{"role": "user", "content": content}], output_model=AnswerDraft,
                                    budget=budget, step=step,
                                    max_tokens=(settings.rediger_max_tokens
                                                if max_tokens is None else max_tokens),
                                    # La rédaction sinistre transcrit des clauses déjà retrouvées ;
                                    # son raisonnement de couverture appartient à *vérifier*. Avec
                                    # `medium`, le raisonnement invisible pouvait consommer les 2 048
                                    # tokens malgré un JSON court et forcer un retry. `low` conserve le
                                    # même modèle, le même schéma et les mêmes bornes, et ne touche pas
                                    # la variante guide 2.6.
                                    effort=EFFORT_PAR_PROMPT.get(prompt))
    except PipelineError as exc:
        # AD-10/AD-16 : l'appel raté a pu être facturé (`step.calls` le porte, `budget` aussi). Sans
        # ce rattachement, l'étape disparaît de la trace alors que son coût y compte, et l'appelant ne
        # peut pas distinguer un appel **commencé** d'un appel qui n'a jamais démarré (revue Codex
        # 1.5, B5). L'erreur reste terminale : c'est l'appelant qui décide, pas nous.
        step.ms = int((time.monotonic() - t0) * 1000)
        exc.step = step
        raise
    draft = result.parsed
    if prompt == "rediger_sinistre":
        draft, _changements = _rattacher_claims_sinistre(draft, settings)
    step.ms = int((time.monotonic() - t0) * 1000)
    return draft, step
