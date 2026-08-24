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
from server.app.domain.question import QuestionScope
from server.app.domain.verdict import Verdict
from server.app.steps.restituer import (
    PHRASES_DE_REFUS,
    PHRASES_DE_REFUS_SINISTRE,
    REGISTRE_SINISTRE,
    REGISTRES,
    restituer,
)


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


# --- le verdict du sinistre (story 1.8) : *restituer* recopie, il ne calcule pas ------
def _verdict(value: str = "sous_conditions") -> Verdict:
    return Verdict(value=value, reason="raison composée par le code (au regard des conditions "
                                       "générales seules)", ask_client=["Quelles options ?"])


def test_the_verdict_of_the_verification_reaches_the_single_answer() -> None:
    """AD-4 : `Verdict` voyage dans l'unique `Answer` ; AD-6 : il est calculé par *vérifier*."""
    verification = Verification(segments=[AnswerSegment(text="Clause c1.", kind="factuel", claim_ids=["c1"])],
                                claims=[_claim()], found=True, verdict=_verdict())
    answer, _step = restituer(language="fr", verification=verification)
    assert answer.verdict is not None and answer.verdict.value == "sous_conditions"
    assert answer.verdict is verification.verdict  # recopié tel quel, jamais recomposé


def test_a_refusal_still_carries_its_verdict() -> None:
    """AD-16 : « aucun repli pour le sinistre » — un refus sinistre porte `ne_tranche_pas`, jamais rien."""
    verification = Verification(rejected_claims=[_rejet()], found=False,
                                verdict=_verdict("ne_tranche_pas"))
    answer, _step = restituer(language="fr", verification=verification,
                              reason=AbsenceProof(kind="claims_rejetes"))
    assert answer.found is False and answer.verdict is not None
    assert answer.verdict.value == "ne_tranche_pas"


def test_a_short_circuited_refusal_receives_its_verdict_from_the_pipeline() -> None:
    """Court-circuit d'AD-5 : aucune vérification n'a tourné, le pipeline compose le `ne_tranche_pas`."""
    answer, _step = restituer(language="fr", reason=AbsenceProof(kind="zero_hit"),
                              verdict=_verdict("ne_tranche_pas"))
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert answer.reason is not None and answer.reason.kind == "zero_hit"


def test_the_guide_never_carries_a_verdict() -> None:
    verification = Verification(segments=[AnswerSegment(text="Clause c1.", kind="factuel", claim_ids=["c1"])],
                                claims=[_claim()], found=True)
    answer, _step = restituer(language="fr", verification=verification)
    assert answer.verdict is None


def test_two_verdicts_for_one_answer_is_an_incoherent_call() -> None:
    """Un seul verdict par requête, calculé par *vérifier* : deux sources ne s'arbitrent pas ici."""
    verification = Verification(found=False, verdict=_verdict("ne_tranche_pas"))
    with pytest.raises(ValueError, match="deux verdicts"):
        restituer(language="fr", verification=verification, reason=AbsenceProof(kind="claims_rejetes"),
                  verdict=_verdict("couvert"))


# --- le registre du refus (story 1.8, revue) : le sinistre ne parle pas du guide ------
@pytest.mark.parametrize("kind", sorted(PHRASES_DE_REFUS))
def test_a_sinistre_refusal_never_serves_the_guide_sentences(kind: str) -> None:
    """Le **texte servi** est celui du registre demandé, et il ne nomme pas le mauvais document.

    Un gestionnaire qui vient de décrire un sinistre sur un contrat AXA lisait « Cette question sort
    de ce que couvre **le guide** » : la seule phrase que l'utilisateur voit désignait un document que
    sa requête ne touche pas.
    """
    answer, _step = restituer(language="fr", reason=AbsenceProof(kind=kind),
                              registre=REGISTRE_SINISTRE)
    assert answer.texte == PHRASES_DE_REFUS_SINISTRE[kind]
    assert answer.segments == [AnswerSegment(text=answer.texte, kind="limite")]
    assert "guide" not in answer.texte
    assert answer.texte != PHRASES_DE_REFUS[kind]
    # et le guide, lui, ne bouge pas d'un octet
    guide, _s = restituer(language="fr", reason=AbsenceProof(kind=kind))
    assert guide.texte == PHRASES_DE_REFUS[kind]


def test_every_registry_covers_exactly_the_absence_kinds_of_ad4() -> None:
    """Un registre traduit les mêmes situations ; il n'en ajoute ni n'en retire aucune."""
    kinds = set(get_args(AbsenceProof.model_fields["kind"].annotation))
    for nom, phrases in REGISTRES.items():
        assert set(phrases) == kinds, nom
        assert all(p.strip() for p in phrases.values()), nom


def test_an_unknown_registry_is_an_incoherent_call() -> None:
    with pytest.raises(ValueError, match="registre de refus inconnu"):
        restituer(language="fr", reason=AbsenceProof(kind="zero_hit"), registre="autre")


# --- `faits_compris` (story 1.9, D4) : *restituer* recopie, il ne calcule pas ---------

def _scope(**kw: object) -> QuestionScope:
    return QuestionScope(**{"bien": "mobilier de salon", "cause": "bougie", **kw})  # type: ignore[arg-type]


def test_faits_compris_travels_in_the_single_answer_on_the_normal_path() -> None:
    """D4 : l'AC de la story 1.9 exige « les faits compris » à l'écran, et aucun canal ne les publiait."""
    verification = Verification(segments=[AnswerSegment(text="Affirmation c1.", kind="factuel",
                                                        claim_ids=["c1"])],
                                claims=[_claim()], found=True, complete=True)
    answer, _step = restituer(language="fr", verification=verification, faits_compris=_scope(),
                              registre=REGISTRE_SINISTRE)
    assert answer.faits_compris is not None
    assert answer.faits_compris.bien == "mobilier de salon"
    assert answer.faits_compris.cause == "bougie"


def test_faits_compris_travels_on_the_refusal_path_too() -> None:
    """C'est sur un refus qu'ils comptent le plus : le seul écran où l'on voit qu'on a été mal compris."""
    answer, _step = restituer(language="fr", reason=AbsenceProof(kind="zero_hit"),
                              verdict=_verdict("ne_tranche_pas"), faits_compris=_scope(),
                              registre=REGISTRE_SINISTRE)
    assert answer.found is False
    assert answer.faits_compris is not None and answer.faits_compris.bien == "mobilier de salon"


def test_the_guide_publishes_no_faits_compris() -> None:
    """`None` en guide, comme le verdict : le profil du guide n'est pas « ce qu'on a compris d'un sinistre »."""
    verification = Verification(segments=[AnswerSegment(text="Affirmation c1.", kind="factuel",
                                                        claim_ids=["c1"])],
                                claims=[_claim()], found=True, complete=True)
    answer, _step = restituer(language="fr", verification=verification)
    assert answer.faits_compris is None
    refus, _s = restituer(language="fr", reason=AbsenceProof(kind="zero_hit"))
    assert refus.faits_compris is None


# --- `QuestionScope.borner` : borner, jamais tronquer (D8 de 1.8, D4 de 1.9) ----------

def test_borner_drops_out_of_bound_labels_and_names_the_fields() -> None:
    """Hors borne, le libellé est **ignoré** — une demi-phrase de cause induirait en erreur."""
    scope = QuestionScope(bien="x" * 201, evenement="incendie", lieu="y" * 300, cause=None,
                          themes=["auto", "z" * 400])
    borne, ignores = scope.borner(200)
    assert borne.bien is None and borne.lieu is None
    assert borne.evenement == "incendie"  # ce qui tient dans la borne est conservé **tel quel**
    assert borne.themes == ["auto"]
    assert sorted(ignores) == ["bien", "lieu", "themes"]
    # Le champ n'est jamais tronqué : rien ne porte un préfixe du libellé écarté.
    assert "x" * 10 not in (borne.model_dump_json())


def test_borner_is_a_copy_and_leaves_the_original_untouched() -> None:
    scope = QuestionScope(bien="x" * 201, evenement="incendie")
    borne, ignores = scope.borner(200)
    assert scope.bien == "x" * 201  # l'original n'est pas muté
    assert borne is not scope and ignores == ["bien"]


def test_borner_reports_nothing_when_everything_fits() -> None:
    scope = QuestionScope(bien="mobilier", evenement="brûlure", lieu="salon", cause="bougie",
                          moment="2026-08-01", themes=["habitation"])
    borne, ignores = scope.borner(200)
    assert ignores == []
    assert borne.model_dump() == scope.model_dump()
