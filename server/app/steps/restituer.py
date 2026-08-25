"""AD-3 / AD-4 — *restituer* : la seule étape qui fabrique l'`Answer`, sans jamais appeler un modèle.

Deux issues, et rien entre les deux :

- **une réponse** — `Answer.texte` est rendu **déterministement** depuis les segments survivants de
  l'ébauche (AD-3 : « un segment factuel dont toutes les claims sont rejetées est retiré ; aucun
  texte libre parallèle n'est affiché ») ; `Answer.segments[]` est conservé tel quel pour que l'UI
  place chaque citation sous la phrase qu'elle soutient (FR3) ;
- **un refus** — un unique segment `limite` porteur d'une phrase composée **par le code**, et
  l'`AbsenceProof` qui dit ce qui a été cherché. C'est la **seule** phrase d'absence que
  l'utilisateur lise : une limite rédigée par le modèle (« le guide ne dit rien de X ») n'est
  soutenue par aucun passage — aucune citation ne prouve une absence — et *vérifier* l'a déjà
  renvoyée vers `Answer.unknown[]` plutôt que vers le texte affiché (revue Codex 1.5, tour 3, B1). Une phrase par `AbsenceProof.kind` : l'utilisateur
  doit pouvoir distinguer « hors périmètre » de « rien trouvé » et de « rien qui tienne la
  vérification » — ce sont trois situations différentes, et une seule d'entre elles vaut la peine de
  reformuler sa question.

Les phrases de refus sont **en français** : le corpus l'est, et les composer dans la langue de la
question demanderait de les traduire — c'est l'AC de la story 2.4. `Answer.lang="fr"` et
`lang_fallback=True` le disent alors franchement (convention Langue du spine), plutôt que de laisser
croire que le refus a été rédigé dans la langue demandée.

`restituer → aucun tier` (AD-9) : le `StepTrace` porte donc `tier=None` et `calls=[]`.
"""

from __future__ import annotations

import time

from server.app.domain.answer import AbsenceProof, Answer, AnswerSegment, Verification
from server.app.domain.question import QuestionScope
from server.app.domain.trace import CheckResult, StepTrace
from server.app.domain.verdict import Verdict
from server.app.llm.models import STEP_TIERS

# Une phrase par `AbsenceProof.kind` (AD-4). Elles ne nomment jamais un terme cherché ni un extrait :
# le détail chiffré est dans `Answer.reason`, que le front rend séparément (« N variantes essayées,
# M passages parcourus »), et les termes eux-mêmes ne sont jamais logués (AD-10).
PHRASES_DE_REFUS: dict[str, str] = {
    "hors_perimetre":
        "Cette question sort de ce que couvre le guide : je n'y réponds pas plutôt que d'y répondre à côté.",
    "zero_hit":
        "Je n'ai trouvé aucun passage du guide qui traite de cette question. Je préfère ne rien "
        "affirmer que d'avancer une réponse sans source.",
    "claims_rejetes":
        "Je n'ai gardé aucune affirmation : les passages cités ne soutenaient pas la réponse, ou ne "
        "répondaient pas à la question. Rien ne vous est montré sans une source vérifiée.",
    "clarification_requise":
        "Je n'ai pas pu déterminer à quoi votre question fait référence ; précisez-la et je chercherai.",
}

# Les mêmes quatre situations, dites pour un dossier de sinistre (story 1.8, revue). Un refus servait
# jusqu'ici les phrases du guide — « Cette question sort de ce que couvre **le guide** », « aucun
# passage **du guide** » — à un gestionnaire qui vient de décrire un sinistre sur un contrat AXA :
# le texte affiché nommait un document que la requête ne touche pas. Les clés sont exactement celles
# de `PHRASES_DE_REFUS` (`REGISTRES` le vérifie au chargement) : un registre n'ajoute jamais un kind,
# il traduit les mêmes.
PHRASES_DE_REFUS_SINISTRE: dict[str, str] = {
    "hors_perimetre":
        "Cette demande ne relève pas de ce que couvre un contrat d'assurance habitation : je ne la "
        "traite pas plutôt que de la rapprocher d'une clause qui ne la vise pas.",
    "zero_hit":
        "Je n'ai trouvé aucune clause du contrat qui traite du sinistre décrit. Je préfère ne rien "
        "conclure que d'opposer au dossier un passage qui ne le concerne pas.",
    "claims_rejetes":
        "Je n'ai retenu aucune clause : les passages cités ne soutenaient pas ce qui en était dit, ou "
        "ne répondaient pas au sinistre décrit. Aucune clause ne vous est montrée sans vérification.",
    "clarification_requise":
        "Je n'ai pas pu déterminer sur quoi porte la demande ; précisez-la et je chercherai dans le "
        "contrat.",
}

# Story 2.3 (revue coordonnée, A2). *vérifier* exclut délibérément ce cas de son compte `ecartes` :
# une phrase dont **toutes** les claims ont été jugées non pertinentes est soutenue au sens du
# contrôle groupé, et c'est la règle mécanique d'AD-3 qui la retire, ici et nulle part ailleurs. Elle
# n'en était pas moins une part de la réponse que l'ébauche voulait donner et que l'utilisateur ne
# voit pas : la servir sous un badge « sûr » est exactement le défaut que la story corrige. Composée
# par le code, en français, comme les autres lacunes — elle voyage donc dans le canal `lacunes`.
PHRASE_SEGMENTS_RETIRES = ("J'ai retiré de ma réponse ce que je ne pouvais pas sourcer : les "
                           "affirmations qui la portaient n'ont pas passé la vérification.")

REGISTRE_GUIDE = "guide"
REGISTRE_SINISTRE = "sinistre"
# Le registre choisit **le vocabulaire du refus**, jamais sa logique : mêmes kinds, mêmes règles, même
# `AbsenceProof`. Le défaut est le guide, à l'octet près — ses fixtures et ses tests en dépendent.
REGISTRES: dict[str, dict[str, str]] = {
    REGISTRE_GUIDE: PHRASES_DE_REFUS,
    REGISTRE_SINISTRE: PHRASES_DE_REFUS_SINISTRE,
}
# Invariant de chargement : aucun registre n'invente ni n'oublie un kind d'`AbsenceProof`. Un `KeyError`
# à la première phrase de refus servie serait un 500 sur le chemin le plus exposé (AD-16).
_MANQUANTS = {nom: set(PHRASES_DE_REFUS) ^ set(phrases) for nom, phrases in REGISTRES.items()}
if any(_MANQUANTS.values()):  # pragma: no cover — invariant vérifié au chargement du module
    raise RuntimeError(f"registre(s) de refus incomplet(s) : "
                       f"{ {n: sorted(k) for n, k in _MANQUANTS.items() if k} }")


def _texte(segments: list[AnswerSegment]) -> str:
    """Rendu déterministe : les textes des segments survivants, dans l'ordre, séparés par une espace."""
    return " ".join(s.text.strip() for s in segments if s.text.strip())


def restituer(*, language: str, verification: Verification | None = None,
              reason: AbsenceProof | None = None, clarification: str | None = None,
              verdict: Verdict | None = None, faits_compris: QuestionScope | None = None,
              registre: str = REGISTRE_GUIDE) -> tuple[Answer, StepTrace]:
    """`Answer` + son `StepTrace`. `reason` est obligatoire dès que la vérification n'a rien retenu.

    `registre` (story 1.8, revue) choisit le **vocabulaire** de la phrase de refus, et rien d'autre :
    le guide parle du guide, le sinistre parle du contrat. Le paramètre est explicite plutôt que
    déduit de la présence d'un verdict — deux appelants, deux registres, aucune inférence — et son
    défaut laisse le guide inchangé à l'octet près.

    `verdict` (story 1.8) : *restituer* **recopie**, il ne calcule pas. AD-4 fait de `Verdict` un
    champ de l'unique `Answer` et AD-6 confie la table à *vérifier* ; ce qui arrive ici est donc soit
    `Verification.verdict`, soit — quand le pipeline sinistre court-circuite *vérifier* (question non
    autonome, aucun bloc citable) — le `ne_tranche_pas` que le pipeline a composé. AD-16 : « aucun
    repli pour le sinistre » se lit aussi dans l'autre sens — un refus sinistre porte un verdict,
    jamais rien, sans quoi le front n'aurait qu'une absence à afficher. Les deux sources sont
    exclusives : un `Verification` qui porte déjà un verdict n'en admet pas un second.

    `faits_compris` (story 1.9, D4) : *restituer* **recopie** ici aussi. C'est
    `ParsedQuestion.scope`, borné par l'appelant — le pipeline est le seul à voir `settings` et la
    question comprise —, et il est recopié tel quel sur **les deux** issues. Sur un refus surtout :
    « je n'ai trouvé aucune clause » se lit tout autrement quand on voit à côté ce que le système a
    cru comprendre du sinistre, et c'est le seul écran où l'utilisateur peut constater qu'il a été
    mal compris. `None` en guide, comme le verdict.
    """
    t0 = time.monotonic()
    step = StepTrace(name="restituer", tier=STEP_TIERS["restituer"])
    trouve = verification is not None and verification.found
    if verdict is not None and verification is not None and verification.verdict is not None:
        raise ValueError("restituer reçoit deux verdicts (celui de la vérification et un second) : "
                         "un seul est calculé par requête, par *vérifier*")
    if verification is not None and verification.verdict is not None:
        verdict = verification.verdict

    if not trouve and reason is None:
        # AD-16 : un `Answer` sans réponse **et** sans preuve d'absence serait un dégradé silencieux —
        # le domaine le refuserait de toute façon, autant le dire à l'appelant avec ses mots.
        raise ValueError("restituer sans réponse retenue exige une AbsenceProof (reason)")
    if trouve and reason is not None:
        raise ValueError("restituer avec une réponse retenue n'admet pas d'AbsenceProof (reason)")

    if registre not in REGISTRES:
        raise ValueError(f"registre de refus inconnu : {registre!r} "
                         f"(connus : {', '.join(sorted(REGISTRES))})")

    if not trouve:
        assert reason is not None  # garanti par le contrôle ci-dessus (mypy/lecture)
        phrase = REGISTRES[registre][reason.kind]
        answer = Answer(
            found=False, complete=False, lang="fr", lang_fallback=language != "fr", texte=phrase,
            segments=[AnswerSegment(text=phrase, kind="limite")],
            rejected_claims=list(verification.rejected_claims) if verification is not None else [],
            reason=reason, verdict=verdict, faits_compris=faits_compris,
            # `manques` et non `unknown` (story 2.3) : un refus ne reçoit aucune lacune du code —
            # l'`AbsenceProof` dit déjà tout — mais un appelant autre que nos deux pipelines peut en
            # avoir posé une, et la perdre serait un dégradé silencieux. Le refus est de toute façon
            # déjà en français, `lang_fallback` est déjà levé.
            unknown=verification.manques if verification is not None else [],
            clarification=clarification,
        )
        step.checks.append(CheckResult(name="refus", ok=True, detail=reason.kind))
        step.ms = int((time.monotonic() - t0) * 1000)
        return answer, step

    assert verification is not None
    survivantes = {c.claim_id for c in verification.claims}
    # AD-3 : un segment `factuel` dont toutes les claims sont rejetées est **retiré**. Les
    # `transition` ne portent aucune affirmation à soutenir — mais ils portent du texte, et
    # *vérifier* a déjà retiré de `Verification.segments` toute phrase, de n'importe quel kind, qui
    # avance plus que ses passages (revue Codex 1.5, tour 2, B1), puis les `limite` en entier, qui
    # affirment une absence qu'aucun passage ne prouve (tour 3, B1). Ce qui arrive ici est donc déjà
    # le texte contrôlé : *restituer* n'applique plus que la règle mécanique d'AD-3.
    segments = [s for s in verification.segments
                if s.kind != "factuel" or (set(s.claim_ids) & survivantes)]
    # Seconde ceinture sur l'assertion d'absence (revue Codex 1.5, tour 3, B1). *vérifier* a déjà
    # sorti les `limite` de `Verification.segments` ; *restituer* est le **seul** endroit où un
    # `Answer` se fabrique (guide aujourd'hui, sinistre en 1.8, API en 1.6), et c'est donc ici que
    # l'invariant doit tenir quel que soit l'appelant : rien de ce qui affirme une absence n'entre
    # dans le texte affiché. La lacune n'est pas perdue pour autant — elle rejoint `unknown[]`.
    retires = len(verification.segments) - len(segments)  # compté avant, il ne nomme que la règle d'AD-3
    limites = [s.text.strip() for s in segments if s.kind == "limite" and s.text.strip()]
    segments = [s for s in segments if s.kind != "limite"]
    if limites:
        step.checks.append(CheckResult(
            name="limites_non_affichees", ok=False,
            detail=f"{len(limites)} phrase(s) d'absence retirée(s) du texte affiché : aucune citation "
                   "ne prouve une absence — elles restent dans unknown[]"))
    # Les `claim_ids` d'un segment conservé sont ramenés aux claims survivantes : l'UI place les
    # citations sous la phrase, elle ne doit pas chercher une claim qui n'est plus dans `claims[]`.
    segments = [AnswerSegment(text=s.text, kind=s.kind,
                              claim_ids=[cid for cid in s.claim_ids if cid in survivantes])
                for s in segments]
    texte = _texte(segments)
    if not any(s.kind == "factuel" and s.text.strip() for s in segments):
        # AD-16, « réponse vide présentée comme réponse » : une réponse dont plus aucune phrase
        # n'affirme quoi que ce soit n'est pas une réponse. *vérifier* garantit l'inverse — il écarte
        # (`non_citee`) toute claim qu'aucun segment factuel affiché ne cite, donc `found=True` implique
        # un segment factuel affiché. Y arriver quand même est une incohérence d'appel, au même titre
        # que les deux ci-dessus, et elle se dit avec les mêmes mots (revue Codex 1.5, B6).
        raise ValueError("restituer avec found=True exige au moins un segment factuel survivant "
                         "portant du texte (une réponse vide n'est pas une réponse)")
    if retires:
        step.checks.append(CheckResult(name="segments_retires", ok=False,
                                       detail=f"{retires} segment(s) factuel(s) sans claim survivante retiré(s)"))
    # Ce que le code constate qu'il manque, à cet étage : les lacunes que *vérifier* a déjà nommées,
    # plus celle des segments retirés ici (A2). Un `CheckResult` ne suffisait pas — il n'atteint ni
    # l'utilisateur ni `complete`, et une réponse amputée sortait badgée « sûr ».
    lacunes = list(verification.lacunes)
    if retires and PHRASE_SEGMENTS_RETIRES not in lacunes:
        lacunes.append(PHRASE_SEGMENTS_RETIRES)
    # Ce que le modèle a déclaré, puis ce que le code constate : une seule liste affichée, dans cet
    # ordre — les deux fronts rendent `unknown[]` sans une ligne de changement.
    declare = list(verification.unknown) + [t for t in limites if t not in verification.unknown]
    unknown = declare + [t for t in lacunes if t not in declare]
    answer = Answer(
        found=True, complete=verification.complete and not retires and not limites,
        lang=language,
        # AD-16 / convention Langue : les phrases du code sont composées **en français** (leur
        # traduction est l'AC de 2.4), exactement comme les phrases de refus ci-dessus. Une réponse
        # rédigée en anglais qui emporte une lacune française n'est pas entièrement dans la langue
        # demandée, et le contrat le dit plutôt que de le taire (revue coordonnée 2.3, A3).
        lang_fallback=bool(lacunes) and language != "fr",
        texte=texte, segments=segments, claims=list(verification.claims),
        rejected_claims=list(verification.rejected_claims), reason=None, verdict=verdict,
        faits_compris=faits_compris,
        unknown=unknown,
        clarification=clarification,
    )
    step.ms = int((time.monotonic() - t0) * 1000)
    return answer, step
