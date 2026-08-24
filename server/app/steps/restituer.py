"""AD-3 / AD-4 — *restituer* : la seule étape qui fabrique l'`Answer`, sans jamais appeler un modèle.

Deux issues, et rien entre les deux :

- **une réponse** — `Answer.texte` est rendu **déterministement** depuis les segments survivants de
  l'ébauche (AD-3 : « un segment factuel dont toutes les claims sont rejetées est retiré ; aucun
  texte libre parallèle n'est affiché ») ; `Answer.segments[]` est conservé tel quel pour que l'UI
  place chaque citation sous la phrase qu'elle soutient (FR3) ;
- **un refus** — un unique segment `limite` porteur d'une phrase composée **par le code**, et
  l'`AbsenceProof` qui dit ce qui a été cherché. Une phrase par `AbsenceProof.kind` : l'utilisateur
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
from server.app.domain.trace import CheckResult, StepTrace
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


def _texte(segments: list[AnswerSegment]) -> str:
    """Rendu déterministe : les textes des segments survivants, dans l'ordre, séparés par une espace."""
    return " ".join(s.text.strip() for s in segments if s.text.strip())


def restituer(*, language: str, verification: Verification | None = None,
              reason: AbsenceProof | None = None,
              clarification: str | None = None) -> tuple[Answer, StepTrace]:
    """`Answer` + son `StepTrace`. `reason` est obligatoire dès que la vérification n'a rien retenu."""
    t0 = time.monotonic()
    step = StepTrace(name="restituer", tier=STEP_TIERS["restituer"])
    trouve = verification is not None and verification.found

    if not trouve and reason is None:
        # AD-16 : un `Answer` sans réponse **et** sans preuve d'absence serait un dégradé silencieux —
        # le domaine le refuserait de toute façon, autant le dire à l'appelant avec ses mots.
        raise ValueError("restituer sans réponse retenue exige une AbsenceProof (reason)")
    if trouve and reason is not None:
        raise ValueError("restituer avec une réponse retenue n'admet pas d'AbsenceProof (reason)")

    if not trouve:
        assert reason is not None  # garanti par le contrôle ci-dessus (mypy/lecture)
        phrase = PHRASES_DE_REFUS[reason.kind]
        answer = Answer(
            found=False, complete=False, lang="fr", lang_fallback=language != "fr", texte=phrase,
            segments=[AnswerSegment(text=phrase, kind="limite")],
            rejected_claims=list(verification.rejected_claims) if verification is not None else [],
            reason=reason, unknown=list(verification.unknown) if verification is not None else [],
            clarification=clarification,
        )
        step.checks.append(CheckResult(name="refus", ok=True, detail=reason.kind))
        step.ms = int((time.monotonic() - t0) * 1000)
        return answer, step

    assert verification is not None
    survivantes = {c.claim_id for c in verification.claims}
    # AD-3 : un segment `factuel` dont toutes les claims sont rejetées est **retiré**. Les autres
    # (`transition`, `limite`) ne portent aucune affirmation à soutenir : ils restent.
    segments = [s for s in verification.segments
                if s.kind != "factuel" or (set(s.claim_ids) & survivantes)]
    # Les `claim_ids` d'un segment conservé sont ramenés aux claims survivantes : l'UI place les
    # citations sous la phrase, elle ne doit pas chercher une claim qui n'est plus dans `claims[]`.
    segments = [AnswerSegment(text=s.text, kind=s.kind,
                              claim_ids=[cid for cid in s.claim_ids if cid in survivantes])
                for s in segments]
    texte = _texte(segments)
    if not texte:
        # Dégénéré mais possible : une claim survivante qu'aucun segment ne cite (l'ébauche n'oblige
        # que l'inverse — un segment factuel cite une claim). Visible dans la trace plutôt que muet.
        step.checks.append(CheckResult(name="texte_vide", ok=False,
                                       detail="aucun segment survivant ne porte de texte"))
    retires = len(verification.segments) - len(segments)
    if retires:
        step.checks.append(CheckResult(name="segments_retires", ok=False,
                                       detail=f"{retires} segment(s) factuel(s) sans claim survivante retiré(s)"))
    answer = Answer(
        found=True, complete=verification.complete, lang=language, lang_fallback=False,
        texte=texte, segments=segments, claims=list(verification.claims),
        rejected_claims=list(verification.rejected_claims), reason=None,
        unknown=list(verification.unknown), clarification=clarification,
    )
    step.ms = int((time.monotonic() - t0) * 1000)
    return answer, step
