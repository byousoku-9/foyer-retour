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

from pydantic import Field, model_validator

from server.app.config import Settings
from server.app.domain.document import DomainModel
from server.app.domain.errors import PipelineError
from server.app.domain.profil import Profil
from server.app.domain.question import (
    ClarificationRequise,
    Faits,
    Intent,
    ParsedQuestion,
    QuestionScope,
    Turn,
)
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import STEP_TIERS
from server.app.llm.prompting import load_prompt, render_prompt, untrusted


# Borne de **forme** du nombre d'éléments des trois listes rendues par le modèle (reprise différée
# `target_story: 2.1`). Elle ne dit qu'une chose : « au-delà, ce n'est plus une liste de termes,
# c'est un déversement » — et elle est généreuse pour qu'un modèle un peu bavard soit ramené **par
# notre code** à ses bornes de travail (`question_max_terms`, `scope_max_themes`,
# `question_max_facettes`, perte bornée et dite en trace) plutôt que rejeté en `LlmParse`, qui est un
# échec terminal.
#
# Elle reste un **littéral de l'étape**, et la Convention Seuils le veut ainsi (revue Codex 2.1, M3,
# reprise en story 2.2) : cette borne-ci entre dans le schéma JSON envoyé au modèle, donc dans la clé
# de requête et dans le préfixe caché (AD-9). La rendre réglable par `.env` laisserait un poste de
# travail déplacer en silence ce qui est facturé et invalider toutes les fixtures enregistrées. Sa
# jumelle de longueur, elle, est appliquée par le code : elle a rejoint `config.py` sous le nom
# `libelle_max_chars`, et elle est publiée dans `Trace.thresholds`.
LISTE_MAX = 32


class SortieComprendre(DomainModel):
    """Sortie structurée de l'appel `micro` : plate, tous champs requis (aucun défaut).

    `question_resolue` et `clarification` sont les deux issues exclusives d'AD-5 : exactement l'une
    des deux est renseignée. L'invariant est porté par le schéma de sortie et non par le code de
    l'étape, pour que sa violation emprunte la relance motivée du client (comme les invariants
    d'`AnswerDraft` pour *rédiger*) plutôt que de choisir arbitrairement une issue.

    Elle hérite de `DomainModel` (`extra="forbid"`) comme tous les autres schémas de sortie du
    projet : un champ surnuméraire inventé par le modèle est une violation de contrat, pas une
    donnée à garder. Elle héritait de `BaseModel`, donc de `extra="ignore"` (reprise différée de la
    revue 1.4). Le changement modifie le schéma JSON envoyé, donc la clé de requête : il n'était
    faisable que le jour où les fixtures live sont réécrites de toute façon — c'est cette story,
    puisque `$perimetre_guide` change déjà le préfixe.
    """

    intent: Intent
    question_resolue: str | None
    clarification: str | None
    language: str
    terms: list[str] = Field(max_length=LISTE_MAX)
    themes: list[str] = Field(max_length=LISTE_MAX)
    facettes: list[str] = Field(max_length=LISTE_MAX)
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


def _libelles(bruts: list[str], *, max_chars: int, garder: int) -> tuple[list[str], int]:
    """Les libellés retenus, et **combien** ont été écartés (revue Codex 2.1, M3 ; story 2.2).

    Deux règles, une seule sortie, parce que ce sont deux façons de perdre un libellé et que les
    taire toutes les deux serait le même silence : la **longueur** (au-delà de `max_chars`, ce n'est
    plus un terme mais un déversement — écarté, jamais coupé) et le **nombre** (au-delà de `garder`,
    les derniers tombent : l'ordre du modèle est celui de la priorité qu'il leur prête).

    Le compte rendu suit `QuestionScope.borner` (story 1.9, D4) : le pipeline en fait un
    `CheckResult` qui nomme les **listes** appauvries, jamais leur contenu (AD-10 interdit le texte
    dans la trace). Sans lui, une question dont la moitié des termes a disparu produisait la même
    recherche appauvrie qu'une question pauvre, sans que rien ne les distingue.
    """
    propres = [t for t in (s.strip() for s in bruts) if t]
    tenus = [t for t in propres if len(t) <= max_chars][:garder]
    return tenus, len(propres) - len(tenus)


async def comprendre(question: str, historique: list[Turn], profil: Profil, *, client: LlmClient,
                     budget: RequestBudget, settings: Settings, lang: str | None = None,
                     prompt: str = "comprendre", perimetre: str = "",
                     faits: Faits | None = None) -> tuple[ParsedQuestion | ClarificationRequise, StepTrace]:
    """`prompt` nomme le fichier de `llm/prompts/` qui suit `commun.md` ; `faits` sont ceux du sinistre.

    `perimetre` (story 2.1) est la projection des titres du document servi (`Corpus.perimetres`),
    rendue dans `$perimetre_guide`. L'étape ne la calcule pas : elle ne voit pas le corpus, et c'est
    le chargement qui en fait autorité (AD-7). Vide par défaut — le prompt du sinistre n'a pas ce
    placeholder, et `Template.substitute` ignore un kwarg en trop.

    Story 1.8 : le sinistre réutilise l'étape telle quelle (AD-1 — la chaîne est fixe, ce sont les
    consignes qui changent) avec `prompt="comprendre_sinistre"`. Les deux paramètres ont un défaut
    qui **est** le guide : son préfixe et son message restent byte-identiques, et les fixtures live
    enregistrées en 1.4/1.5/1.7 — clefées sur le contenu de la requête — se rejouent sans réseau.
    AD-5 nomme déjà les faits du sinistre parmi les entrées de l'étape (« question + historique +
    profil (ou `faits` du sinistre) ») ; ils sont délimités par `untrusted()` comme le reste.
    """
    t0 = time.monotonic()
    step = StepTrace(name="comprendre", tier=STEP_TIERS["comprendre"])
    prefix = load_prompt("commun") + "\n\n" + render_prompt(
        prompt, question_min_terms=settings.question_min_terms,
        question_max_terms=settings.question_max_terms,
        question_max_facettes=settings.question_max_facettes,
        perimetre_guide=perimetre)
    parts = [
        untrusted("historique", json.dumps([{"role": t.role, "texte": t.texte} for t in historique],
                                           ensure_ascii=False)),
        untrusted("profil", json.dumps(profil.filtered(), ensure_ascii=False, sort_keys=True)),
    ]
    if faits is not None:
        parts.append(untrusted("faits", json.dumps(faits.model_dump(), ensure_ascii=False, sort_keys=True)))
    parts.append(untrusted("question", question))
    content = "\n\n".join(parts)
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
        # `terms`, `themes` et `facettes` sont ramenés **par le code** à leurs seuils de travail
        # (reprise différée de la revue 1.4), et ce qui tombe est désormais **dit** (revue Codex 2.1,
        # M3, reprise en story 2.2). Le prompt demande `question_max_terms` termes : quand le modèle
        # en rend plus, ce sont les derniers — les moins prioritaires selon lui — qui tombent, et la
        # recherche reste bornée à ce que `search_limit` peut classer. Écarter par la fin, jamais
        # couper un libellé — et écarter aussi celui qui dépasse `libelle_max_chars`
        # (`Field(max_length=…)` sur une liste ne compte que ses éléments, si bien qu'un « terme » de
        # dix mille caractères passait et partait dans `terms_searched`).
        #
        # Le découpage en facettes, lui, est arrêté ici, avant *retrouver* et *rédiger* (AD-4, revue
        # Codex 1.5, tour 3, B3) : borné par `question_max_facettes`, et jamais deviné — un modèle
        # muet laisse la liste vide, et `complete` restera `False` faute de preuve.
        terms, hors_terms = _libelles(out.terms, max_chars=settings.libelle_max_chars,
                                      garder=settings.question_max_terms)
        facettes, hors_facettes = _libelles(out.facettes, max_chars=settings.libelle_max_chars,
                                            garder=settings.question_max_facettes)
        themes, hors_themes = _libelles(out.themes, max_chars=settings.libelle_max_chars,
                                        garder=settings.scope_max_themes)
        appauvries = [f"{nom} ({n})" for nom, n in
                      (("terms", hors_terms), ("facettes", hors_facettes), ("themes", hors_themes)) if n]
        if appauvries:
            # AD-10 : le check nomme les listes et compte, jamais le texte écarté — ce texte vient du
            # modèle, et la trace part au client (AD-15).
            step.checks.append(CheckResult(
                name="libelles_hors_borne", ok=False,
                detail=f"libellé(s) écarté(s) par les bornes de l'étape : {', '.join(appauvries)} "
                       f"(longueur > {settings.libelle_max_chars}, ou au-delà du nombre retenu)"))
        sortie = ParsedQuestion(
            question_resolue=(out.question_resolue or "").strip(),
            intent=out.intent,
            language=language,
            terms=terms,
            facettes=facettes,
            scope=QuestionScope(themes=themes,
                                bien=out.bien or None, evenement=out.evenement or None, lieu=out.lieu or None,
                                cause=out.cause or None, moment=out.moment or None),
        )
    step.ms = int((time.monotonic() - t0) * 1000)
    return sortie, step
