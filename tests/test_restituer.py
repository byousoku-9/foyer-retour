"""Matrice I/O de *restituer* (spec 1.5) : le rendu est déterministe, le refus est explicite.

*Restituer* n'appelle aucun modèle (AD-9 : `restituer → aucun tier`) : ces tests n'ont donc ni client
ni fixture — ce qui est exactement la propriété à préserver.
"""

from __future__ import annotations

from typing import get_args

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
                         quotes=[VerifiedQuote(block_id="mini:p1:2", quote="huit jours", start=0, end=10,
                                               text_start=0, text_end=10)],
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
    # La phrase d'absence n'est **pas** affichée : aucune citation ne prouve une absence, et la seule
    # phrase d'absence qu'on montre est celle du refus, composée par le code (revue Codex 1.5,
    # tour 3, B1). *vérifier* l'a déjà écartée ; *restituer* le refait, parce qu'il est le seul
    # endroit où un `Answer` se fabrique et que l'invariant doit tenir quel que soit l'appelant.
    assert answer.texte == "Vous avez huit jours. Par ailleurs,"
    # rendre deux fois la même vérification donne le même texte, au caractère près
    assert restituer(language="fr", verification=verification)[0].texte == answer.texte
    assert answer.found is True and answer.complete is False and answer.lang == "fr"
    assert answer.lang_fallback is False and answer.reason is None
    assert [s.kind for s in answer.segments] == ["factuel", "transition"]
    assert answer.unknown == ["Je ne sais pas pour les frontaliers."]  # la lacune, elle, est dite
    assert [c.name for c in step.checks] == ["limites_non_affichees"]
    assert step.name == "restituer" and step.tier is None and step.calls == []


def test_a_limit_never_reaches_the_displayed_text_but_never_disappears_either() -> None:
    """Une limite qu'aucun `unknown` ne porte encore (appelant autre que le pipeline du guide) est
    déplacée, jamais perdue : la lacune reste dite, elle n'est simplement plus affirmée."""
    verification = Verification(
        segments=[AnswerSegment(text="Vous avez huit jours.", kind="factuel", claim_ids=["c1"]),
                  AnswerSegment(text="Le guide ne dit rien des frontaliers.", kind="limite")],
        claims=[_claim()], found=True, complete=False)
    answer, _step = restituer(language="fr", verification=verification)
    assert "frontaliers" not in answer.texte
    assert [s.kind for s in answer.segments] == ["factuel"]
    assert answer.unknown == ["Le guide ne dit rien des frontaliers."]


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


ABSENCE_KINDS = sorted(get_args(AbsenceProof.model_fields["kind"].annotation))


@pytest.mark.parametrize("kind", ABSENCE_KINDS)
def test_every_absence_kind_has_its_own_sentence(kind: str) -> None:
    reason = AbsenceProof(kind=kind, terms_searched=["école"], blocks_scanned=506, documents=["lux-guide"])
    answer, step = restituer(language="fr", reason=reason)
    assert answer.found is False and answer.complete is False
    assert answer.texte == PHRASES_DE_REFUS[kind] and answer.claims == []
    # un refus rend **un** segment `limite` : l'UI n'a qu'une chose à afficher
    assert [(s.kind, s.text) for s in answer.segments] == [("limite", PHRASES_DE_REFUS[kind])]
    assert answer.reason is reason and step.checks[0].detail == kind
    # une phrase distincte par kind : « hors périmètre » ne se confond pas avec « rien trouvé ».
    # Dérivé du Literal du domaine : un cinquième kind sans phrase serait un KeyError en production.
    assert set(PHRASES_DE_REFUS) == set(ABSENCE_KINDS)
    assert len(set(PHRASES_DE_REFUS.values())) == len(ABSENCE_KINDS)


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


def test_an_answer_without_a_surviving_factual_segment_is_never_served() -> None:
    """AD-16, « réponse vide présentée comme réponse » : `found=True` sans une seule phrase qui affirme
    quelque chose n'est pas une réponse. *vérifier* écarte (`non_citee`) toute claim qu'aucun segment
    factuel affiché ne cite ; y arriver quand même est une incohérence d'appel (revue Codex 1.5, B6)."""
    orpheline = Verification(segments=[], claims=[_claim()], found=True)
    with pytest.raises(ValueError, match="segment factuel"):
        restituer(language="fr", verification=orpheline)

    # une transition seule ne remplace pas une affirmation : le seul segment factuel a été retiré
    sans_factuel = Verification(
        segments=[AnswerSegment(text="Rejetée.", kind="factuel", claim_ids=["c2"]),
                  AnswerSegment(text="Cela dit,", kind="transition")],
        claims=[_claim()], rejected_claims=[_rejet()], found=True)
    with pytest.raises(ValueError, match="segment factuel"):
        restituer(language="fr", verification=sans_factuel)

    # un segment factuel qui ne porte que des blancs ne montre rien non plus
    blanc = Verification(segments=[AnswerSegment(text="   ", kind="factuel", claim_ids=["c1"])],
                         claims=[_claim()], found=True)
    with pytest.raises(ValueError, match="segment factuel"):
        restituer(language="fr", verification=blanc)
