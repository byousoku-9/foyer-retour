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
from server.app.domain.answer import AnswerDraft
from server.app.domain.question import ParsedQuestion, Turn
from server.app.domain.retrieval import RetrievalResult
from server.app.domain.trace import StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import STEP_TIERS
from server.app.llm.prompting import load_prompt, render_prompt, untrusted


async def rediger(parsed: ParsedQuestion, retrieval: RetrievalResult, historique: list[Turn], *,
                  client: LlmClient, budget: RequestBudget, index: Index, doc_id: str,
                  settings: Settings, motif: str | None = None) -> tuple[AnswerDraft, StepTrace]:
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
        "rediger", quote_min_chars=settings.quote_min_chars, quote_max_chars=settings.quote_max_chars,
        draft_max_segments=settings.draft_max_segments, draft_max_claims=settings.draft_max_claims,
    ) + "\n\n" + index.sommaire(doc_id)
    parts = [untrusted("historique", json.dumps([{"role": t.role, "texte": t.texte} for t in historique],
                                                ensure_ascii=False))]
    parts += [untrusted("document", f"{b.block_id}\n{b.text}") for b in retrieval.blocs]
    parts.append(untrusted("question", parsed.question_resolue))
    tail = f"Langue de rédaction : {parsed.language}."  # normalisée par le domaine
    if motif is not None:
        # AD-15 : le motif vient de *vérifier* (1.5), qui le compose à partir de la sortie du modèle et
        # du texte des blocs — il est délimité comme tout le reste, jamais concaténé en clair.
        tail += "\n" + untrusted("motif", motif)
    content = "\n\n".join(parts) + "\n\n" + tail
    result = await client.parse(tier=STEP_TIERS["rediger"], system_prefix=prefix,
                                messages=[{"role": "user", "content": content}], output_model=AnswerDraft,
                                budget=budget, step=step, max_tokens=settings.rediger_max_tokens)
    step.ms = int((time.monotonic() - t0) * 1000)
    return result.parsed, step
