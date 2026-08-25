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
from server.app.domain.answer import AnswerDraft, AnswerSegment, Claim, Quote
from server.app.domain.langue import LANGUES_SERVIES
from server.app.domain.errors import PipelineError
from server.app.domain.question import ParsedQuestion, Turn
from server.app.domain.retrieval import RetrievalResult
from server.app.domain.trace import StepTrace
from server.app.domain.verdict import KINDS_DECISIONNELS
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import STEP_TIERS
from server.app.llm.prompting import load_prompt, render_prompt, untrusted


def _inclure_clause_decisionnelle_de_tete(
        draft: AnswerDraft, retrieval: RetrievalResult, settings: Settings) -> AnswerDraft:
    """Conserve la première clause confirmée que le rappel a classée devant toutes les autres.

    Campagne A6 réelle : `p34:12` était le premier bloc du contexte, mais *rédiger* pouvait ne garder
    que l'exclusion plus lointaine `p46:1`. La clause de garantie disparaissait alors avant même le
    contrôle de pertinence. Ce n'est ni une décision de couverture ni un nouveau rappel : le code
    remet seulement sous forme de claim le premier bloc **déjà choisi et ouvert** par *retrouver*,
    mot pour mot depuis le corpus. *Vérifier* garde tous ses contrôles de citation, pertinence,
    applicabilité et AD-6.

    La ceinture ne s'active que pour une clause décisionnelle au typage confirmé et dans la place
    `draft_max_claims` existante. Un titre, une définition, un passage non typé ou une clause déjà
    citée ne change rien ; le guide n'appelle jamais cette fonction.
    """
    if not retrieval.blocs or len(draft.claims) >= settings.draft_max_claims:
        return draft
    bloc = retrieval.blocs[0]
    if bloc.kind not in KINDS_DECISIONNELS or not bloc.kind_confirmed:
        return draft
    if any(quote.block_id == bloc.block_id for claim in draft.claims for quote in claim.quotes):
        return draft
    ids = {claim.claim_id for claim in draft.claims}
    rang = 1
    while f"c{rang}" in ids:
        rang += 1
    claim = Claim(claim_id=f"c{rang}", text=bloc.text,
                  quotes=[Quote(block_id=bloc.block_id, quote=bloc.text)])
    return draft.model_copy(update={"claims": [*draft.claims, claim]})


def _rattacher_claims_sinistre(draft: AnswerDraft) -> tuple[AnswerDraft, int]:
    """Fait des claims atomiques le texte factuel effectivement soumis à *vérifier*.

    Campagne réelle 2.7 : le rédacteur savait citer l'exclusion animale de `p35:2`, mais pouvait
    créer une claim distincte sans la rattacher à aucun segment. La citation et sa pertinence
    passaient alors tous les contrôles, avant que la claim ne soit justement rejetée `non_citee`.

    En sinistre, `Claim.text` est déjà l'affirmation atomique (« une seule clause par affirmation »)
    que *vérifier* confronte aux passages. Le segment factuel ne gagne donc rien à la paraphraser :
    on le remplace par la concaténation exacte des claims qu'il annonce, puis on ajoute une phrase
    factuelle pour chaque claim oubliée. Aucun sujet, aucune clause et aucun seuil ne sont ajoutés ;
    les transitions et limites du modèle restent inchangées. Le guide conserve son brouillon à
    l'octet près, comme sa variante 2.6.
    """
    par_id = {claim.claim_id: claim for claim in draft.claims}
    rattachees: set[str] = set()
    segments: list[AnswerSegment] = []
    changements = 0
    for segment in draft.segments:
        if segment.kind != "factuel":
            segments.append(segment)
            continue
        ids = list(dict.fromkeys(cid for cid in segment.claim_ids if cid in par_id))
        texte = " ".join(par_id[cid].text.strip() for cid in ids if par_id[cid].text.strip())
        if not ids or not texte:
            # Les deux cas sont normalement fermés par `AnswerDraft`; garder le segment permet au
            # vérificateur d'appliquer son refus conservateur si un producteur futur les autorise.
            segments.append(segment)
            continue
        aligne = AnswerSegment(text=texte, kind="factuel", claim_ids=ids)
        segments.append(aligne)
        rattachees.update(ids)
        changements += int(aligne != segment)
    for claim in draft.claims:
        if claim.claim_id in rattachees or not claim.text.strip():
            continue
        segments.append(AnswerSegment(text=claim.text.strip(), kind="factuel",
                                      claim_ids=[claim.claim_id]))
        rattachees.add(claim.claim_id)
        changements += 1
    if not changements:
        return draft, 0
    return draft.model_copy(update={"segments": segments}), changements


async def rediger(parsed: ParsedQuestion, retrieval: RetrievalResult, historique: list[Turn], *,
                  client: LlmClient, budget: RequestBudget, index: Index, doc_id: str,
                  settings: Settings, motif: str | None = None,
                  prompt: str = "rediger") -> tuple[AnswerDraft, StepTrace]:
    """`prompt` nomme le fichier de `llm/prompts/` inséré entre `commun.md` et le sommaire.

    Story 1.8 : le sinistre passe `prompt="rediger_sinistre"` — mêmes contrats d'entrée et de sortie,
    consigne « une seule clause par affirmation » en plus (AD-6). Le défaut **est** le guide : son
    préfixe reste byte-identique, donc cacheable (AD-9) et rejouable depuis ses fixtures live.
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
    if motif is not None:
        # AD-15 : le motif vient de *vérifier* (1.5), qui le compose à partir de la sortie du modèle et
        # du texte des blocs — il est délimité comme tout le reste, jamais concaténé en clair.
        tail += "\n" + untrusted("motif", motif)
    content = "\n\n".join(parts) + "\n\n" + tail
    try:
        result = await client.parse(tier=STEP_TIERS["rediger"], system_prefix=prefix,
                                    messages=[{"role": "user", "content": content}], output_model=AnswerDraft,
                                    budget=budget, step=step, max_tokens=settings.rediger_max_tokens)
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
        draft = _inclure_clause_decisionnelle_de_tete(draft, retrieval, settings)
        draft, _changements = _rattacher_claims_sinistre(draft)
    step.ms = int((time.monotonic() - t0) * 1000)
    return draft, step
