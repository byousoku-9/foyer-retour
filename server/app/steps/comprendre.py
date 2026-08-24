"""AD-5 — *comprendre* : un seul appel `micro` transforme question + historique + profil en une question comprise.

Le modèle de sortie LLM est dédié et plat, tous champs requis (un schéma sans défauts force le modèle à
tout remplir), puis converti en un type du domaine. `language` = `lang` si fourni, sinon la
détection du modèle, repli `fr` ; `terms` toujours en français ; `scope.themes` dérivé du profil
(enfants → école/allocations, véhicule → auto, statut → affiliation) ; `facettes` = les sous-questions
distinctes que la question pose, arrêtées **ici** parce qu'AD-4 les nomme « facettes de
`ParsedQuestion` » et qu'une facette omise ne serait pas détectable si celui qui répond était aussi
celui qui dit ce qu'il fallait couvrir (revue Codex 1.5, tour 3, B3). Le court-circuit
(intent ∈ {meteo, bavardage, hors_perimetre} ⇒ pas d'appel `reason`) est une propriété du pipeline
(story 1.5) : ici, l'intent seul suffit à le décider. L'étape a **deux** issues typées (AD-5, revue
Codex 1.4, B4) : `ParsedQuestion` quand la question est autonome, `ClarificationRequise` quand une
anaphore reste irrésoluble avec l'historique — dans ce second cas aucune `question_resolue` n'est
construite, ni inventée ni reprise telle quelle, et c'est le schéma de sortie qui l'impose
(exactement l'un des deux champs renseigné, sinon relance motivée). Le préfixe
système est statique (instructions décrivant le périmètre du guide — pas le sommaire : `micro` cache
5 min, seuil de cache Haiku élevé).
Question, historique et profil sont chacun délimités par `untrusted()` (AD-15).
"""

from __future__ import annotations

import json
import time

from pydantic import BaseModel, model_validator

from server.app.config import Settings
from server.app.domain.errors import PipelineError
from server.app.domain.profil import Profil
from server.app.domain.question import ClarificationRequise, Intent, ParsedQuestion, QuestionScope, Turn
from server.app.domain.trace import StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import STEP_TIERS
from server.app.llm.prompting import load_prompt, render_prompt, untrusted


class SortieComprendre(BaseModel):
    """Sortie structurée de l'appel `micro` : plate, tous champs requis (aucun défaut).

    `question_resolue` et `clarification` sont les deux issues exclusives d'AD-5 : exactement l'une
    des deux est renseignée. L'invariant est porté par le schéma de sortie et non par le code de
    l'étape, pour que sa violation emprunte la relance motivée du client (comme les invariants
    d'`AnswerDraft` pour *rédiger*) plutôt que de choisir arbitrairement une issue.
    """

    intent: Intent
    question_resolue: str | None
    clarification: str | None
    language: str
    terms: list[str]
    themes: list[str]
    facettes: list[str]
    bien: str | None
    evenement: str | None
    lieu: str | None
    cause: str | None
    moment: str | None

    @model_validator(mode="after")
    def _une_seule_issue(self) -> SortieComprendre:
        resolue = bool((self.question_resolue or "").strip())
        clarif = bool((self.clarification or "").strip())
        if resolue == clarif:
            raise ValueError("renseigne soit question_resolue (question autonome), soit clarification "
                             "(anaphore irrésoluble), jamais les deux ni aucune des deux")
        return self


async def comprendre(question: str, historique: list[Turn], profil: Profil, *, client: LlmClient,
                     budget: RequestBudget, settings: Settings,
                     lang: str | None = None) -> tuple[ParsedQuestion | ClarificationRequise, StepTrace]:
    t0 = time.monotonic()
    step = StepTrace(name="comprendre", tier=STEP_TIERS["comprendre"])
    prefix = load_prompt("commun") + "\n\n" + render_prompt(
        "comprendre", question_min_terms=settings.question_min_terms,
        question_max_terms=settings.question_max_terms,
        question_max_facettes=settings.question_max_facettes)
    content = "\n\n".join((
        untrusted("historique", json.dumps([{"role": t.role, "texte": t.texte} for t in historique],
                                           ensure_ascii=False)),
        untrusted("profil", json.dumps(profil.filtered(), ensure_ascii=False, sort_keys=True)),
        untrusted("question", question),
    ))
    try:
        result = await client.parse(tier=STEP_TIERS["comprendre"], system_prefix=prefix,
                                    messages=[{"role": "user", "content": content}],
                                    output_model=SortieComprendre, budget=budget, step=step,
                                    max_tokens=settings.comprendre_max_tokens)
    except PipelineError as exc:
        # AD-10/AD-16 : un appel facturé qui échoue reste tracé — l'étape voyage avec l'erreur, comme
        # dans *rédiger* et *vérifier* (revue Codex 1.5, tour 2, B5). La règle vaut pour les trois
        # étapes qui appellent un modèle : aucune ne décide à la place de l'appelant.
        step.ms = int((time.monotonic() - t0) * 1000)
        exc.step = step
        raise
    out = result.parsed
    language = lang if lang is not None else out.language  # normalisé par le modèle du domaine
    clarification = (out.clarification or "").strip()
    if clarification:  # AD-5 : aucune `question_resolue` n'est construite dans ce cas
        sortie: ParsedQuestion | ClarificationRequise = ClarificationRequise(
            clarification=clarification, intent=out.intent, language=language)
    else:
        sortie = ParsedQuestion(
            question_resolue=(out.question_resolue or "").strip(),
            intent=out.intent,
            language=language,
            terms=[t for t in (s.strip() for s in out.terms) if t],
            # Le découpage en facettes est arrêté ici, avant *retrouver* et *rédiger* (AD-4, revue
            # Codex 1.5, tour 3, B3) : borné par `question_max_facettes`, et jamais deviné — un
            # modèle muet laisse la liste vide, et `complete` restera `False` faute de preuve.
            facettes=[f for f in (s.strip() for s in out.facettes) if f][: settings.question_max_facettes],
            scope=QuestionScope(themes=[t for t in (s.strip() for s in out.themes) if t],
                                bien=out.bien or None, evenement=out.evenement or None, lieu=out.lieu or None,
                                cause=out.cause or None, moment=out.moment or None),
        )
    step.ms = int((time.monotonic() - t0) * 1000)
    return sortie, step
