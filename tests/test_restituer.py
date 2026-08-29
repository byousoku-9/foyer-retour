"""Matrice I/O de *restituer* (spec 1.5) : le rendu est déterministe, le refus est explicite.

*Restituer* n'appelle aucun modèle (AD-9 : `restituer → aucun tier`) : ces tests n'ont donc ni client
ni fixture — ce qui est exactement la propriété à préserver.
"""

from __future__ import annotations

from typing import get_args

import pytest

from server.app.domain.answer import (
    LACUNES_PLURALISEES,
    AbsenceProof,
    AnswerSegment,
    ClaimStatus,
    Lacune,
    LacuneKind,
    LecturePartielle,
    RejectedClaim,
    Verification,
    VerifiedClaim,
    VerifiedQuote,
)
from server.app.domain.langue import LANGUES_SERVIES
from server.app.domain.question import QuestionScope
from server.app.domain.verdict import Verdict
from server.app.steps.restituer import (
    PHRASES_DE_LECTURE_PARTIELLE,
    PHRASES_DE_REFUS,
    PHRASES_DE_REFUS_SINISTRE,
    PHRASES_DE_LACUNE,
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
        claims=[_claim()], rejected_claims=[_rejet()], found=True, complete=True)
    answer, step = restituer(language="fr", verification=verification)
    assert answer.texte == "Soutenue. Mixte."  # « Rejetée. » n'est jamais affichée
    # le segment conservé ne renvoie plus vers une claim absente de `claims[]` (AD-3/AD-4)
    assert [s.claim_ids for s in answer.segments] == [["c1"], ["c1"]]
    assert [c.claim_id for c in answer.rejected_claims] == ["c2"]
    assert [c.name for c in step.checks] == ["segments_retires"]


ABSENCE_KINDS = sorted(get_args(AbsenceProof.model_fields["kind"].annotation))
LACUNE_KINDS = sorted(get_args(LacuneKind))


@pytest.mark.parametrize("kind", ABSENCE_KINDS)
def test_every_absence_kind_has_its_own_sentence(kind: str) -> None:
    reason = AbsenceProof(kind=kind, terms_searched=["école"], blocks_scanned=506, documents=["lux-guide"])
    answer, step = restituer(language="fr", reason=reason)
    assert answer.found is False and answer.complete is False
    assert answer.texte == PHRASES_DE_REFUS["fr"][kind] and answer.claims == []
    # un refus rend **un** segment `limite` : l'UI n'a qu'une chose à afficher
    assert [(s.kind, s.text) for s in answer.segments] == [
        ("limite", PHRASES_DE_REFUS["fr"][kind]),
    ]
    assert answer.reason is reason and step.checks[0].detail == kind
    # une phrase distincte par kind : « hors périmètre » ne se confond pas avec « rien trouvé ».
    # Dérivé du Literal du domaine : un cinquième kind sans phrase serait un KeyError en production.
    assert set(PHRASES_DE_REFUS["fr"]) == set(ABSENCE_KINDS)
    assert len(set(PHRASES_DE_REFUS["fr"].values())) == len(ABSENCE_KINDS)


def test_every_lacune_kind_has_a_non_empty_sentence_in_every_served_language() -> None:
    """Le registre dérive du `Literal` : un nouveau kind ne peut pas attendre le chemin production."""
    assert set(PHRASES_DE_LACUNE) == {"fr", "en", "de", "pt"}
    for langue, phrases in PHRASES_DE_LACUNE.items():
        assert set(phrases) == set(LACUNE_KINDS), langue
        for kind, patron in phrases.items():
            if kind in LACUNES_PLURALISEES:
                assert isinstance(patron, tuple) and len(patron) == 2
                assert all(forme.strip() for forme in patron)
            else:
                assert isinstance(patron, str) and patron.strip()


@pytest.mark.parametrize("language", ["fr", "en", "de", "pt"])
@pytest.mark.parametrize("kind", sorted(LACUNES_PLURALISEES))
@pytest.mark.parametrize("n,index", [(1, 0), (2, 1)])
def test_each_pluralized_lacune_renders_singular_and_plural(
        language: str, kind: str, n: int, index: int) -> None:
    verification = Verification(
        segments=[AnswerSegment(text="Réponse.", kind="factuel", claim_ids=["c1"])],
        claims=[_claim()], found=True, complete=False, lacunes=[Lacune(kind=kind, n=n)])
    answer, _step = restituer(language=language, verification=verification)
    patron = PHRASES_DE_LACUNE[language][kind]
    assert isinstance(patron, tuple)
    assert answer.unknown == [patron[index].format(n=n)]


def test_a_refusal_is_rendered_in_the_selected_language_without_inventing_a_fallback() -> None:
    answer, _step = restituer(language="en", reason=AbsenceProof(kind="hors_perimetre"))
    assert answer.lang == "en" and answer.lang_fallback is False
    assert answer.texte == PHRASES_DE_REFUS["en"]["hors_perimetre"]
    fr, _ = restituer(language="fr", reason=AbsenceProof(kind="hors_perimetre"))
    assert fr.lang_fallback is False

    repli, _ = restituer(language="fr", lang_fallback=True,
                         reason=AbsenceProof(kind="hors_perimetre"))
    assert repli.lang == "fr" and repli.lang_fallback is True


def test_a_refusal_keeps_the_rejected_claims_and_the_clarification() -> None:
    verification = Verification(segments=[AnswerSegment(text="Rejetée.", kind="factuel", claim_ids=["c2"])],
                                rejected_claims=[_rejet()], found=False,
                                unknown=["frontaliers", PHRASES_DE_LACUNE["fr"]["sans_decoupage"]],
                                lacunes=[Lacune(kind="sans_decoupage")])
    answer, _step = restituer(language="fr", verification=verification,
                              reason=AbsenceProof(kind="claims_rejetes"))
    # AD-3 : les claims non retrouvées sont **conservées**, jamais silencieusement effacées
    assert [c.claim_id for c in answer.rejected_claims] == ["c2"]
    assert answer.unknown == ["frontaliers", PHRASES_DE_LACUNE["fr"]["sans_decoupage"]]
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
                                claims=[_claim()], found=True, complete=True, verdict=_verdict())
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
                                claims=[_claim()], found=True, complete=True)
    answer, _step = restituer(language="fr", verification=verification)
    assert answer.verdict is None


def test_two_verdicts_for_one_answer_is_an_incoherent_call() -> None:
    """Un seul verdict par requête, calculé par *vérifier* : deux sources ne s'arbitrent pas ici."""
    verification = Verification(found=False, verdict=_verdict("ne_tranche_pas"))
    with pytest.raises(ValueError, match="deux verdicts"):
        restituer(language="fr", verification=verification, reason=AbsenceProof(kind="claims_rejetes"),
                  verdict=_verdict("couvert"))


# --- le registre du refus (story 1.8, revue) : le sinistre ne parle pas du guide ------
@pytest.mark.parametrize("kind", sorted(PHRASES_DE_REFUS["fr"]))
def test_a_sinistre_refusal_never_serves_the_guide_sentences(kind: str) -> None:
    """Le **texte servi** est celui du registre demandé, et il ne nomme pas le mauvais document.

    Un gestionnaire qui vient de décrire un sinistre sur un contrat AXA lisait « Cette question sort
    de ce que couvre **le guide** » : la seule phrase que l'utilisateur voit désignait un document que
    sa requête ne touche pas.
    """
    answer, _step = restituer(language="fr", reason=AbsenceProof(kind=kind),
                              registre=REGISTRE_SINISTRE)
    assert answer.texte == PHRASES_DE_REFUS_SINISTRE["fr"][kind]
    assert answer.segments == [AnswerSegment(text=answer.texte, kind="limite")]
    assert "guide" not in answer.texte
    assert answer.texte != PHRASES_DE_REFUS["fr"][kind]
    # et le guide, lui, ne bouge pas d'un octet
    guide, _s = restituer(language="fr", reason=AbsenceProof(kind=kind))
    assert guide.texte == PHRASES_DE_REFUS["fr"][kind]


def test_every_registry_covers_exactly_the_absence_kinds_of_ad4() -> None:
    """Un registre traduit les mêmes situations ; il n'en ajoute ni n'en retire aucune."""
    kinds = set(get_args(AbsenceProof.model_fields["kind"].annotation))
    assert set(PHRASES_DE_REFUS) == set(PHRASES_DE_REFUS_SINISTRE) == {"fr", "en", "de", "pt"}
    for nom, langues in REGISTRES.items():
        for langue, phrases in langues.items():
            assert set(phrases) == kinds, (nom, langue)
            assert all(p.strip() for p in phrases.values()), (nom, langue)


@pytest.mark.parametrize("kind", ABSENCE_KINDS)
def test_les_refus_portugais_du_guide_et_anglais_du_sinistre_sont_servis_tels_quels(
        kind: str) -> None:
    guide, _ = restituer(language="pt", reason=AbsenceProof(kind=kind))
    sinistre, _ = restituer(language="en", reason=AbsenceProof(kind=kind),
                            registre=REGISTRE_SINISTRE)
    assert guide.lang == "pt" and guide.texte == PHRASES_DE_REFUS["pt"][kind]
    assert sinistre.lang == "en"
    assert sinistre.texte == PHRASES_DE_REFUS_SINISTRE["en"][kind]


def test_an_unknown_registry_is_an_incoherent_call() -> None:
    with pytest.raises(ValueError, match="registre de refus inconnu"):
        restituer(language="fr", reason=AbsenceProof(kind="zero_hit"), registre="autre")


def test_an_unsupported_restitution_language_is_an_incoherent_call() -> None:
    with pytest.raises(ValueError, match="langue de restitution inconnue"):
        restituer(language="es", reason=AbsenceProof(kind="zero_hit"))


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


def test_les_relations_declarees_precedent_les_clauses_sans_devenir_une_claim() -> None:
    """A9 : la concision contractuelle ne fusionne pas cause progressive et effet soudain."""
    verification = Verification(segments=[AnswerSegment(text="La clause traite les écoulements.",
                                                         kind="factuel", claim_ids=["c1"])],
                                claims=[_claim()], found=True, complete=True)
    scope = _scope(evenement="effondrement soudain du plafond", cause="fuite progressive",
                   moment="hier, après plusieurs mois de fuite")

    answer, step = restituer(language="fr", verification=verification, faits_compris=scope,
                             registre=REGISTRE_SINISTRE)

    assert answer.segments[0] == AnswerSegment(
        text=("Faits compris — cause : fuite progressive ; puis événement : effondrement soudain "
              "du plafond ; moment : hier, après plusieurs mois de fuite."),
        kind="transition")
    assert answer.segments[0].claim_ids == []
    assert answer.texte.startswith(answer.segments[0].text)
    assert "La clause traite les écoulements." in answer.texte
    assert step.checks == []


def test_un_repere_isole_ne_gonfle_pas_la_reponse_sinistre() -> None:
    verification = Verification(segments=[AnswerSegment(text="Clause.", kind="factuel",
                                                        claim_ids=["c1"])],
                                claims=[_claim()], found=True, complete=True)
    answer, step = restituer(language="fr", verification=verification,
                             faits_compris=QuestionScope(moment="hier"),
                             registre=REGISTRE_SINISTRE)
    assert answer.segments[0] == AnswerSegment(
        text="Faits compris — moment : hier.", kind="transition")
    assert answer.texte == "Faits compris — moment : hier. Clause."
    assert len(answer.segments[0].text) < 50
    assert step.checks == []


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
    borne, ignores = scope.borner(200, 6)
    assert borne.bien is None and borne.lieu is None
    assert borne.evenement == "incendie"  # ce qui tient dans la borne est conservé **tel quel**
    assert borne.themes == ["auto"]
    assert sorted(ignores) == ["bien", "lieu", "themes"]
    # Le champ n'est jamais tronqué : rien ne porte un préfixe du libellé écarté.
    assert "x" * 10 not in (borne.model_dump_json())


def test_borner_bounds_the_number_of_themes_not_only_their_length() -> None:
    """Deux cents thèmes courts passent tous la borne de longueur (revue 1.9, tour 2).

    La page les joint en **une seule ligne** sous « Ce que j'ai compris du sinistre » : sans borne
    de nombre, un modèle bavard produisait une ligne sans fin à l'écran. Les thèmes en trop sont
    écartés par la fin — l'ordre du modèle est celui de la pertinence qu'il leur prête — et l'écart
    se dit, comme tout ce qui est retiré.
    """
    scope = QuestionScope(themes=[f"theme-{i}" for i in range(200)])
    borne, ignores = scope.borner(200, 6)
    assert borne.themes == [f"theme-{i}" for i in range(6)]
    assert ignores == ["themes"]
    # Exactement la borne : rien n'est retiré, donc rien n'est signalé.
    pile, rien = QuestionScope(themes=[f"t{i}" for i in range(6)]).borner(200, 6)
    assert len(pile.themes) == 6 and rien == []


def test_borner_is_a_copy_and_leaves_the_original_untouched() -> None:
    scope = QuestionScope(bien="x" * 201, evenement="incendie")
    borne, ignores = scope.borner(200, 6)
    assert scope.bien == "x" * 201  # l'original n'est pas muté
    assert borne is not scope and ignores == ["bien"]


def test_borner_reports_nothing_when_everything_fits() -> None:
    scope = QuestionScope(bien="mobilier", evenement="brûlure", lieu="salon", cause="bougie",
                          moment="2026-08-01", themes=["habitation"])
    borne, ignores = scope.borner(200, 6)
    assert ignores == []
    assert borne.model_dump() == scope.model_dump()


# --- ce que *restituer* constate lui-même (story 2.3, revue coordonnée) -----
def test_un_segment_retire_empeche_complete_et_dit_ce_qui_manque() -> None:
    """A2 : une réponse amputée ne peut pas être badgée « sûr ».

    *vérifier* exclut délibérément ce cas de son compte `ecartes` : une phrase dont **toutes** les
    claims ont été jugées non pertinentes est soutenue au sens du contrôle groupé, et c'est la règle
    mécanique d'AD-3 qui la retire — ici, et nulle part ailleurs. Elle disparaissait donc du texte
    servi avec `complete=True` et un simple `CheckResult`, que l'utilisateur ne voit pas.
    """
    verification = Verification(
        segments=[AnswerSegment(text="Soutenue.", kind="factuel", claim_ids=["c1"]),
                  AnswerSegment(text="Rejetée.", kind="factuel", claim_ids=["c2"])],
        claims=[_claim()], rejected_claims=[_rejet()], found=True, complete=True)
    answer, step = restituer(language="fr", verification=verification)
    assert answer.texte == "Soutenue."
    assert answer.complete is False
    assert answer.unknown == [PHRASES_DE_LACUNE["fr"]["segments_retires"]]
    assert [c.name for c in step.checks] == ["segments_retires"]


def test_une_lacune_du_code_est_rendue_dans_la_langue_de_la_reponse() -> None:
    base = dict(segments=[AnswerSegment(text="The deadline is eight days.", kind="factuel",
                                        claim_ids=["c1"])],
                claims=[_claim()], found=True)
    du_code = Verification(**base, complete=False, lacunes=[Lacune(kind="lecture_bornee")])
    answer, _step = restituer(language="en", verification=du_code)
    assert answer.lang == "en" and answer.lang_fallback is False
    assert answer.unknown == [PHRASES_DE_LACUNE["en"]["lecture_bornee"]]

    # Une lacune **du modèle** est rédigée dans la langue de la réponse : aucun repli à signaler.
    du_modele = Verification(**base, complete=False, unknown=["I could not check the fine print."])
    anglais, _s = restituer(language="en", verification=du_modele)
    assert anglais.lang_fallback is False and anglais.unknown == ["I could not check the fine print."]

    # Et en français, une lacune du code ne signale évidemment aucun repli.
    francais, _s2 = restituer(language="fr", verification=du_code)
    assert francais.lang_fallback is False


def test_une_reponse_partielle_anglaise_rend_les_causes_dans_leur_ordre() -> None:
    verification = Verification(
        segments=[AnswerSegment(text="The answer is partial.", kind="factuel", claim_ids=["c1"])],
        claims=[_claim()], found=True, complete=False,
        lacunes=[Lacune(kind="lecture_bornee"),
                 Lacune(kind="facettes_sans_reponse", n=2)])
    answer, _ = restituer(language="en", verification=verification)
    assert answer.unknown == [
        PHRASES_DE_LACUNE["en"]["lecture_bornee"],
        PHRASES_DE_LACUNE["en"]["facettes_sans_reponse"][1].format(n=2),
    ]


def test_les_deux_canaux_sont_affiches_dans_une_seule_liste() -> None:
    """Le modèle d'abord, le code ensuite, sans doublon : les deux fronts rendent `unknown[]` tel quel."""
    verification = Verification(
        segments=[AnswerSegment(text="Vous avez huit jours.", kind="factuel", claim_ids=["c1"])],
        claims=[_claim()], found=True, complete=False,
        unknown=["Je ne sais rien des frontaliers.", PHRASES_DE_LACUNE["fr"]["lecture_bornee"]],
        lacunes=[Lacune(kind="lecture_bornee"), Lacune(kind="renvoi_non_resolu")])
    answer, _step = restituer(language="fr", verification=verification)
    assert answer.unknown == [
        "Je ne sais rien des frontaliers.",
        PHRASES_DE_LACUNE["fr"]["lecture_bornee"],
        PHRASES_DE_LACUNE["fr"]["renvoi_non_resolu"],
    ]


# --- story 4.2f : le troisième porteur, et sa phrase dans les deux registres ----------------------

def _lecture_partielle(nodes: int = 2, blocs: int = 5) -> LecturePartielle:
    return LecturePartielle(nodes_read=nodes, blocks_read=blocs, documents=["doc-synthetique"])


def _refus_verification() -> Verification:
    """Ce que *vérifier* rend sur ce chemin : rien de retenu, une claim écartée, sa lacune de borne."""
    return Verification(claims=[], rejected_claims=[_rejet()], found=False, complete=False,
                        lacunes=[Lacune(kind="lecture_bornee")])


def test_une_lecture_partielle_est_une_reponse_et_non_un_refus() -> None:
    """AD-4 : *restituer* reste la fabrique unique de l'`Answer`, et cette issue en est une troisième.

    Elle ne porte **aucune** preuve d'absence — c'est tout l'objet de la story — mais elle porte ses
    compteurs, ses affirmations écartées et sa lacune, et sa trace la nomme.
    """
    answer, step = restituer(language="fr", verification=_refus_verification(),
                             lecture_partielle=_lecture_partielle())
    assert answer.found is False and answer.complete is False and answer.reason is None
    assert answer.lecture_partielle is not None
    assert (answer.lecture_partielle.nodes_read, answer.lecture_partielle.blocks_read) == (2, 5)
    assert answer.texte == PHRASES_DE_LECTURE_PARTIELLE["guide"]["fr"]
    assert [s.kind for s in answer.segments] == ["limite"]
    assert answer.unknown == [PHRASES_DE_LACUNE["fr"]["lecture_bornee"]]
    assert [c.claim_id for c in answer.rejected_claims] == ["c2"]
    assert [c.name for c in step.checks] == ["lecture_partielle"]
    assert step.checks[0].ok is False and "2 nœud(s) lu(s)" in step.checks[0].detail
    assert step.tier is None and step.calls == []


@pytest.mark.parametrize("registre", ["guide", REGISTRE_SINISTRE])
@pytest.mark.parametrize("langue", ["fr", "en", "de", "pt"])
def test_la_phrase_de_lecture_partielle_existe_dans_les_deux_registres_et_les_quatre_langues(
        registre: str, langue: str) -> None:
    """Un registre neuf doit apporter sa garde : celle-ci est vérifiée servie, pas seulement chargée."""
    answer, _step = restituer(language=langue, registre=registre,
                              verification=_refus_verification(),
                              lecture_partielle=_lecture_partielle(),
                              verdict=Verdict(value="ne_tranche_pas", reason="r")
                              if registre == REGISTRE_SINISTRE else None)
    assert answer.lang == langue
    assert answer.texte == PHRASES_DE_LECTURE_PARTIELLE[registre][langue]
    assert answer.texte.strip() and answer.unknown == [PHRASES_DE_LACUNE[langue]["lecture_bornee"]]


def test_les_deux_registres_ne_disent_pas_la_meme_chose() -> None:
    """Le guide parle du guide, le sinistre parle du contrat : c'est la règle de la story 1.8, et un
    registre neuf ne l'échappe pas."""
    for langue in ("fr", "en", "de", "pt"):
        guide = PHRASES_DE_LECTURE_PARTIELLE["guide"][langue]
        contrat = PHRASES_DE_LECTURE_PARTIELLE[REGISTRE_SINISTRE][langue]
        assert guide != contrat, langue
    assert "guide" in PHRASES_DE_LECTURE_PARTIELLE["guide"]["fr"]
    assert "contrat" in PHRASES_DE_LECTURE_PARTIELLE[REGISTRE_SINISTRE]["fr"]


def test_le_registre_de_lecture_partielle_couvre_exactement_les_registres_de_refus() -> None:
    """L'invariant de chargement, vérifié aussi comme propriété : aucun registre en trop ni en moins,
    et les quatre langues servies pour chacun. Sans lui, un `KeyError` sur le chemin le plus exposé."""
    assert set(PHRASES_DE_LECTURE_PARTIELLE) == set(REGISTRES)
    for phrases in PHRASES_DE_LECTURE_PARTIELLE.values():
        assert set(phrases) == set(LANGUES_SERVIES)
        assert all(isinstance(p, str) and p.strip() for p in phrases.values())


def test_un_verdict_de_sinistre_traverse_la_lecture_partielle_sans_etre_recalcule() -> None:
    """AD-16 : jamais un sinistre sans verdict — et AD-6 : jamais un verdict de remplacement."""
    verdict = Verdict(value="ne_tranche_pas", reason="aucune clause retenue")
    answer, _step = restituer(language="fr", registre=REGISTRE_SINISTRE,
                              verification=_refus_verification(),
                              lecture_partielle=_lecture_partielle(),
                              verdict=verdict,
                              faits_compris=QuestionScope(bien="registre commun"))
    assert answer.verdict is verdict
    assert answer.faits_compris is not None and answer.faits_compris.bien == "registre commun"


def test_les_deux_porteurs_ensemble_sont_refuses() -> None:
    """« Exactement un porteur » : deux comptes rendus du même refus, dont l'un annonce
    l'exhaustivité que l'autre dément."""
    with pytest.raises(ValueError, match="deux porteurs d'absence"):
        restituer(language="fr", verification=_refus_verification(),
                  reason=AbsenceProof(kind="claims_rejetes"),
                  lecture_partielle=_lecture_partielle())


def test_une_reponse_retenue_nadmet_pas_de_lecture_partielle() -> None:
    """Une réponse retenue dont la lecture a été bornée dit sa borne dans `unknown[]`, pas ici."""
    verification = Verification(
        segments=[AnswerSegment(text="Une affirmation.", kind="factuel", claim_ids=["c1"])],
        claims=[_claim()], found=True, complete=False, lacunes=[Lacune(kind="lecture_bornee")])
    with pytest.raises(ValueError, match="n'admet pas de LecturePartielle"):
        restituer(language="fr", verification=verification,
                  lecture_partielle=_lecture_partielle())


def test_sans_reponse_et_sans_porteur_restituer_refuse_toujours() -> None:
    with pytest.raises(ValueError, match="AbsenceProof.*ou une"):
        restituer(language="fr", verification=_refus_verification())


def test_une_lecture_partielle_sans_borne_dite_est_une_erreur_de_contrat_explicite() -> None:
    """P11 : la fabrique dit ses contradictions avec ses mots, jamais par une `ValidationError`.

    Le domaine refuserait de toute façon (« une lecture partielle dit ce qui lui manque »), mais il
    le ferait par pydantic — donc un 500 générique sur le chemin que la story vient d'ouvrir. Les
    deux autres contradictions de contrat de cette fonction (deux porteurs, un porteur sous une
    réponse retenue) lèvent déjà un `ValueError` nommé ; celle-ci le fait aussi.
    """
    muette = Verification(claims=[], rejected_claims=[_rejet()], found=False, complete=False)
    with pytest.raises(ValueError, match="exige une vérification qui dise sa borne"):
        restituer(language="fr", verification=muette, lecture_partielle=_lecture_partielle())
    # Et sans vérification du tout : même contrat, même message.
    with pytest.raises(ValueError, match="exige une vérification qui dise sa borne"):
        restituer(language="fr", lecture_partielle=_lecture_partielle())


# --- story 4.2e : la phrase de la demande de contexte restée ouverte ------------------------------

@pytest.mark.parametrize("language", ["fr", "en", "de", "pt"])
def test_la_lacune_de_contexte_non_relu_se_dit_dans_les_quatre_langues(language: str) -> None:
    """Une lacune sans phrase casserait l'invariant de chargement du registre (AD-16).

    Elle n'est **pas** pluralisée : le bornage de la story rend une demande unique par vérification,
    donc son cardinal vaut toujours zéro — et le domaine refuse l'autre cas.
    """
    verification = Verification(
        segments=[AnswerSegment(text="Réponse.", kind="factuel", claim_ids=["c1"])],
        claims=[_claim()], found=True, complete=False,
        lacunes=[Lacune(kind="contexte_non_relu")])
    answer, _step = restituer(language=language, verification=verification)

    patron = PHRASES_DE_LACUNE[language]["contexte_non_relu"]
    assert isinstance(patron, str) and patron.strip()
    assert answer.unknown == [patron]
    assert answer.complete is False
    assert "contexte_non_relu" not in LACUNES_PLURALISEES


def test_la_phrase_de_contexte_non_relu_ne_se_confond_avec_aucune_autre() -> None:
    """« un renvoi non résolu » et « ma lecture a été bornée » disent autre chose : trois causes."""
    for langue, phrases in PHRASES_DE_LACUNE.items():
        rendues = [p for kind, p in phrases.items() if isinstance(p, str)]
        assert len(set(rendues)) == len(rendues), langue
        assert phrases["contexte_non_relu"] != phrases["renvoi_non_resolu"]
        assert phrases["contexte_non_relu"] != phrases["lecture_bornee"]
