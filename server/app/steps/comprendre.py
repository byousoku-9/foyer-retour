"""AD-5 — *comprendre* : un seul appel `micro` transforme question + historique + profil en `ParsedQuestion`.

Le modèle de sortie LLM est dédié et plat, tous champs requis (un schéma sans défauts force le modèle à
tout remplir), puis converti en `ParsedQuestion` du domaine. `language` = `lang` si fourni, sinon la
détection du modèle, repli `fr` ; `terms` toujours en français ; `scope.themes` dérivé du profil
(enfants → école/allocations, véhicule → auto, statut → affiliation). Le court-circuit
(intent ∈ {meteo, bavardage, hors_perimetre} ⇒ pas d'appel `reason`) est une propriété du pipeline
(story 1.5) : ici, l'intent seul suffit à le décider. Une anaphore que l'historique ne permet pas de
résoudre ne produit **pas** une `question_resolue` inventée : la question est reprise telle quelle et
`clarification` porte la question à poser à l'utilisateur (AD-5, revue Codex 1.4, B4). Le préfixe
système est statique (instructions décrivant le périmètre du guide — pas le sommaire : `micro` cache
5 min, seuil de cache Haiku élevé).
Question, historique et profil sont chacun délimités par `untrusted()` (AD-15).
"""

from __future__ import annotations

import json
import time

from pydantic import BaseModel

from server.app.config import Settings
from server.app.domain.profil import Profil
from server.app.domain.question import Intent, ParsedQuestion, QuestionScope, Turn
from server.app.domain.trace import StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import STEP_TIERS
from server.app.llm.prompting import load_prompt, render_prompt, untrusted


class SortieComprendre(BaseModel):
    """Sortie structurée de l'appel `micro` : plate, tous champs requis (aucun défaut)."""

    intent: Intent
    question_resolue: str
    clarification: str | None
    language: str
    terms: list[str]
    themes: list[str]
    bien: str | None
    evenement: str | None
    lieu: str | None
    cause: str | None
    moment: str | None


async def comprendre(question: str, historique: list[Turn], profil: Profil, *, client: LlmClient,
                     budget: RequestBudget, settings: Settings,
                     lang: str | None = None) -> tuple[ParsedQuestion, StepTrace]:
    t0 = time.monotonic()
    step = StepTrace(name="comprendre", tier=STEP_TIERS["comprendre"])
    prefix = load_prompt("commun") + "\n\n" + render_prompt(
        "comprendre", question_min_terms=settings.question_min_terms,
        question_max_terms=settings.question_max_terms)
    content = "\n\n".join((
        untrusted("historique", json.dumps([{"role": t.role, "texte": t.texte} for t in historique],
                                           ensure_ascii=False)),
        untrusted("profil", json.dumps(profil.filtered(), ensure_ascii=False, sort_keys=True)),
        untrusted("question", question),
    ))
    result = await client.parse(tier=STEP_TIERS["comprendre"], system_prefix=prefix,
                                messages=[{"role": "user", "content": content}],
                                output_model=SortieComprendre, budget=budget, step=step,
                                max_tokens=settings.comprendre_max_tokens)
    out = result.parsed
    parsed = ParsedQuestion(
        question_resolue=out.question_resolue,
        clarification=(out.clarification or "").strip() or None,
        intent=out.intent,
        language=lang if lang is not None else out.language,  # normalisé par ParsedQuestion
        terms=[t for t in (s.strip() for s in out.terms) if t],
        scope=QuestionScope(themes=[t for t in (s.strip() for s in out.themes) if t],
                            bien=out.bien or None, evenement=out.evenement or None, lieu=out.lieu or None,
                            cause=out.cause or None, moment=out.moment or None),
    )
    step.ms = int((time.monotonic() - t0) * 1000)
    return parsed, step
