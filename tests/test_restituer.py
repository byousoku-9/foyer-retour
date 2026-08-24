"""Matrice I/O de *restituer* (spec 1.5) : le rendu est déterministe, le refus est explicite.

*Restituer* n'appelle aucun modèle (AD-9 : `restituer → aucun tier`) : ces tests n'ont donc ni client
ni fixture — ce qui est exactement la propriété à préserver.
"""

from __future__ import annotations

import pytest

from server.app.domain.answer import (
    AbsenceProof,
    AnswerSegment,
    ClaimStatus,
    RejectedClaim,
    Verification,
    VerifiedClaim,
    VerifiedQuote,
)
from server.app.steps.restituer import PHRASES_DE_REFUS, restituer


def _claim(claim_id: str = "c1", *, pertinente: bool = True) -> VerifiedClaim:
    return VerifiedClaim(claim_id=claim_id, text=f"Affirmation {claim_id}.",
                         quotes=[VerifiedQuote(block_id="mini:p1:2", quote="huit jours", start=0, end=10)],
                         status=ClaimStatus(retrouvee=True, pertinente=pertinente, edition="git:test"))


def _rejet(claim_id: str = "c2") -> RejectedClaim:
    return RejectedClaim(claim_id=claim_id, text="Affirmation rejetée.",
                         quotes=[{"block_id": "mini:p1:3", "quote": "inventé"}],
                         status=ClaimStatus(retrouvee=False, pertinente=None, edition="git:test"),
                         rejection_kind="non_retrouvee", motif="citation introuvable")


def test_texte_is_rendered_deterministically_from_the_surviving_segments() -> None:
    verification = Verification(
        segments=[AnswerSegment(text="  Vous avez huit jours. ", kind="factuel", claim_ids=["c1"]),
                  AnswerSegment(text="Par ailleurs,", kind="transition"),
                  AnswerSegment(text="Je ne sais pas pour les frontaliers.", kind="limite")],
        claims=[_claim()], found=True, complete=False, unknown=["Je ne sais pas pour les frontaliers."])
    answer, step = restituer(language="fr", verification=verification)
    assert answer.texte == ("Vous avez huit jours. Par ailleurs, Je ne sais pas pour les frontaliers.")
    # rendre deux fois la même vérification donne le même texte, au caractère près
    assert restituer(language="fr", verification=verification)[0].texte == answer.texte
    assert answer.found is True and answer.complete is False and answer.lang == "fr"
    assert answer.lang_fallback is False and answer.reason is None
    assert [s.kind for s in answer.segments] == ["factuel", "transition", "limite"]
    assert answer.unknown == ["Je ne sais pas pour les frontaliers."]
    assert step.name == "restituer" and step.tier is None and step.calls == []


def test_a_factual_segment_without_a_surviving_claim_is_removed() -> None:
    verification = Verification(
        segments=[AnswerSegment(text="Soutenue.", kind="factuel", claim_ids=["c1"]),
                  AnswerSegment(text="Rejetée.", kind="factuel", claim_ids=["c2"]),
                  AnswerSegment(text="Mixte.", kind="factuel", claim_ids=["c1", "c2"])],
        claims=[_claim()], rejected_claims=[_rejet()], found=True)
    answer, step = restituer(language="fr", verification=verification)
    assert answer.texte == "Soutenue. Mixte."  # « Rejetée. » n'est jamais affichée
    # le segment conservé ne renvoie plus vers une claim absente de `claims[]` (AD-3/AD-4)
    assert [s.claim_ids for s in answer.segments] == [["c1"], ["c1"]]
    assert [c.claim_id for c in answer.rejected_claims] == ["c2"]
    assert [c.name for c in step.checks] == ["segments_retires"]


@pytest.mark.parametrize("kind", ["hors_perimetre", "zero_hit", "claims_rejetes", "clarification_requise"])
def test_every_absence_kind_has_its_own_sentence(kind: str) -> None:
    reason = AbsenceProof(kind=kind, terms_searched=["école"], blocks_scanned=506, documents=["lux-guide"])
    answer, step = restituer(language="fr", reason=reason)
    assert answer.found is False and answer.complete is False
    assert answer.texte == PHRASES_DE_REFUS[kind] and answer.claims == []
    # un refus rend **un** segment `limite` : l'UI n'a qu'une chose à afficher
    assert [(s.kind, s.text) for s in answer.segments] == [("limite", PHRASES_DE_REFUS[kind])]
    assert answer.reason is reason and step.checks[0].detail == kind
    # quatre phrases distinctes : « hors périmètre » ne se confond pas avec « rien trouvé »
    assert len(set(PHRASES_DE_REFUS.values())) == 4


def test_a_refusal_in_another_language_says_it_falls_back_to_french() -> None:
    answer, _step = restituer(language="en", reason=AbsenceProof(kind="hors_perimetre"))
    # la traduction du refus est l'AC de 2.4 : d'ici là, le repli est déclaré, jamais masqué
    assert answer.lang == "fr" and answer.lang_fallback is True
    fr, _ = restituer(language="fr", reason=AbsenceProof(kind="hors_perimetre"))
    assert fr.lang_fallback is False


def test_a_refusal_keeps_the_rejected_claims_and_the_clarification() -> None:
    verification = Verification(segments=[AnswerSegment(text="Rejetée.", kind="factuel", claim_ids=["c2"])],
                                rejected_claims=[_rejet()], found=False, unknown=["frontaliers"])
    answer, _step = restituer(language="fr", verification=verification,
                              reason=AbsenceProof(kind="claims_rejetes"))
    # AD-3 : les claims non retrouvées sont **conservées**, jamais silencieusement effacées
    assert [c.claim_id for c in answer.rejected_claims] == ["c2"] and answer.unknown == ["frontaliers"]
    clarif, _ = restituer(language="fr", reason=AbsenceProof(kind="clarification_requise"),
                          clarification="De quelles personnes parlez-vous ?")
    assert clarif.clarification == "De quelles personnes parlez-vous ?" and clarif.found is False


def test_restituer_refuses_the_two_incoherent_calls() -> None:
    """AD-16 : ni réponse sans preuve d'absence, ni réponse *et* preuve d'absence."""
    with pytest.raises(ValueError, match="AbsenceProof"):
        restituer(language="fr", verification=Verification(found=False))
    with pytest.raises(ValueError, match="AbsenceProof"):
        restituer(language="fr", verification=Verification(claims=[_claim()], found=True),
                  reason=AbsenceProof(kind="zero_hit"))


def test_a_surviving_claim_no_segment_cites_is_visible_in_the_trace() -> None:
    verification = Verification(segments=[], claims=[_claim()], found=True)
    answer, step = restituer(language="fr", verification=verification)
    assert answer.texte == "" and [c.name for c in step.checks] == ["texte_vide"]
