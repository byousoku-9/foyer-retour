"""Table AD-6 et dérivation d'`applicable` — **sans LLM**, sans corpus, sans étape (spec 1.8).

`domain/verdict.py` est le découpage d'exécution d'AD-6 : le modèle extrait des valeurs typées, le
code décide. Ce fichier couvre chaque ligne de la matrice I/O de la story par du code pur ; le
branchement sur les vrais blocs du contrat est vérifié par `test_verifier.py` et
`test_pipeline_sinistre.py`.
"""

from __future__ import annotations

import pytest

from server.app.domain.verdict import (
    KINDS_DECISIONNELS,
    ChampsApplicabilite,
    ClaimJugee,
    ClauseCitee,
    applicable_de_claim,
    decider,
)

ASK_MAX = 8
SOCLE = "d:n1"
EXTENSION = "d:n2"


def _clause(kind: str, *, block_id: str = "d:p1:1", confirmed: bool = True,
            portee: set[str] | None = None, node_id: str = SOCLE, socle: bool = True) -> ClauseCitee:
    return ClauseCitee(block_id=block_id, kind=kind, kind_confirmed=confirmed,
                       portee={node_id} if portee is None else portee, node_id=node_id, socle=socle)


def _champs(present: bool = True, *, option: bool = False, cp: bool = False,
            manquant: str | None = None) -> ChampsApplicabilite:
    return ChampsApplicabilite(fait_requis_present=present, option_requise=option, cp_requise=cp,
                               fait_manquant=manquant)


def _claim(claim_id: str, kind: str | None, champs: ChampsApplicabilite | None = None, **kw) -> ClaimJugee:
    clauses = [] if kind is None else [kw.pop("clause", None) or _clause(kind, block_id=f"d:p1:{claim_id[-1]}")]
    return ClaimJugee(claim_id=claim_id, clauses=clauses, champs=champs, **kw)


# --- (a) dérivation d'`applicable` ------------------------------------------
def test_the_four_decisional_kinds_are_exactly_those_of_ad6() -> None:
    assert KINDS_DECISIONNELS == {"garantie", "exclusion", "condition", "franchise"}


def test_a_claim_citing_no_decisional_block_has_no_applicability() -> None:
    """D2 : une définition ou un paragraphe n'a pas d'applicabilité — `None`, jamais `humain`."""
    assert applicable_de_claim(_claim("c1", None, _champs())) is None


def test_an_unconfirmed_kind_is_always_human() -> None:
    """AD-6, littéralement : bloc sans `kind` confirmé ⇒ `humain` (et verdict plafonné, cf. plus bas)."""
    claim = ClaimJugee(claim_id="c1", clauses=[_clause("garantie", confirmed=False)], champs=_champs())
    assert applicable_de_claim(claim) == "humain"


def test_a_clause_without_scope_is_human() -> None:
    """La table compare des portées : une clause dont on ignore où elle s'applique ne se tranche pas."""
    claim = ClaimJugee(claim_id="c1", clauses=[_clause("exclusion", portee=set())], champs=_champs())
    assert applicable_de_claim(claim) == "humain"


def test_missing_typed_fields_are_never_guessed() -> None:
    """AC : aucun champ rendu pour une claim décisionnelle ⇒ `humain`."""
    assert applicable_de_claim(_claim("c1", "garantie", None)) == "humain"


def test_a_required_fact_known_to_be_contrary_makes_the_clause_inapplicable() -> None:
    """D1 : `fait_requis_present=false` **et** aucun `fait_manquant` = le fait est connu et contraire.

    C'est ce qui écarte l'exclusion p. 46 du cas bougie : elle vise le bâtiment et les extensions
    3.1.8.3-6, le sinistre porte sur le contenu du domicile.
    """
    assert applicable_de_claim(_claim("c1", "exclusion", _champs(False))) == "non"
    # un libellé vide ou blanc ne vaut pas un fait manquant : il ne rend pas la clause incertaine
    assert applicable_de_claim(_claim("c1", "exclusion", _champs(False, manquant="   "))) == "non"


def test_an_unknown_fact_is_human_not_inapplicable() -> None:
    """D1 : `fait_manquant` renseigné = fait **inconnu** ⇒ `humain`, jamais `non`."""
    claim = _claim("c1", "garantie", _champs(False, manquant="caractère subit de l'action de la chaleur"))
    assert applicable_de_claim(claim) == "humain"


@pytest.mark.parametrize("champs", [_champs(option=True), _champs(cp=True)])
def test_an_option_or_particular_conditions_make_the_clause_human(champs: ChampsApplicabilite) -> None:
    assert applicable_de_claim(_claim("c1", "garantie", champs)) == "humain"


def test_a_fact_established_without_option_is_applicable() -> None:
    assert applicable_de_claim(_claim("c1", "garantie", _champs(True))) == "oui"


def test_inapplicability_wins_over_an_option_it_would_never_reach() -> None:
    """Ordre de D1 : (5) précède (6) — une clause qui ne vise pas le cas rend l'option sans objet."""
    assert applicable_de_claim(_claim("c1", "exclusion", _champs(False, option=True))) == "non"


# --- (b) la table, ligne par ligne ------------------------------------------
def test_an_applicable_exclusion_covering_the_case_excludes_it() -> None:
    """Règle (1) : exclusion `oui` dont la portée intersecte les nœuds du cas ⇒ `non_couvert`."""
    garantie = ClaimJugee(claim_id="c1", clauses=[_clause("garantie", block_id="d:p1:1")],
                          champs=_champs(True))
    exclusion = ClaimJugee(claim_id="c2", clauses=[_clause("exclusion", block_id="d:p1:2")],
                           champs=_champs(True))
    v = decider([garantie, exclusion], ask_client_max=ASK_MAX)
    assert v.value == "non_couvert" and "exclusion" in v.reason
    assert "conditions générales seules" in v.reason  # AD-6 : la portée est toujours dite


def test_an_exclusion_whose_scope_misses_the_case_does_not_exclude() -> None:
    """La portée est le calcul unique d'AD-2 : hors des nœuds du cas, l'exclusion ne mord pas."""
    garantie = ClaimJugee(claim_id="c1", clauses=[_clause("garantie", block_id="d:p1:1")],
                          champs=_champs(True))
    ailleurs = _clause("exclusion", block_id="d:p1:2", portee={EXTENSION}, node_id=EXTENSION, socle=False)
    exclusion = ClaimJugee(claim_id="c2", clauses=[ailleurs], champs=_champs(True))
    assert decider([garantie, exclusion], ask_client_max=ASK_MAX).value == "couvert"


def test_an_exclusion_alone_never_covers_itself() -> None:
    """D3 : à défaut de garantie, les nœuds du cas excluent les blocs de l'exclusion testée."""
    exclusion = ClaimJugee(claim_id="c1", clauses=[_clause("exclusion")], champs=_champs(True))
    assert decider([exclusion], ask_client_max=ASK_MAX).value == "ne_tranche_pas"


def test_a_baseline_guarantee_alone_is_covered() -> None:
    """Règle (3) : garantie du socle `oui`, aucune claim `humain` ⇒ `couvert`."""
    v = decider([_claim("c1", "garantie", _champs(True))], ask_client_max=ASK_MAX)
    assert v.value == "couvert" and v.missing.faits == [] and v.escalate == []
    assert v.missing.conditions_particulieres and v.missing.options_souscrites  # le paquet reste dû
    # AD-6 : « toujours avec le paquet manquant et les questions à poser » — même sur un `couvert`,
    # qui ne vaut qu'au regard des conditions générales (D8).
    assert len(v.ask_client) == 2 and all("Fait à établir" not in q for q in v.ask_client)


def test_a_guarantee_with_an_open_condition_is_conditional() -> None:
    """Règle (2), politique conservatrice : une condition `humain` suffit à ouvrir le verdict."""
    garantie = _claim("c1", "garantie", _champs(True))
    condition = _claim("c2", "condition", _champs(False, manquant="franchise applicable"))
    v = decider([garantie, condition], ask_client_max=ASK_MAX)
    assert v.value == "sous_conditions"
    assert v.missing.faits == ["franchise applicable"]
    assert any("franchise applicable" in q for q in v.ask_client)


def test_a_guarantee_outside_the_baseline_is_conditional() -> None:
    """Règle (2) : « ou la garantie dépend d'une extension » — hors socle, jamais `couvert`."""
    hors_socle = _clause("garantie", portee={EXTENSION}, node_id=EXTENSION, socle=False)
    garantie = ClaimJugee(claim_id="c1", clauses=[hors_socle], champs=_champs(True))
    v = decider([garantie], ask_client_max=ASK_MAX)
    assert v.value == "sous_conditions" and "socle" in v.reason


@pytest.mark.parametrize("champs", [_champs(True, option=True), _champs(True, cp=True)])
def test_a_guarantee_depending_on_an_option_is_conditional(champs: ChampsApplicabilite) -> None:
    """Règle (2bis) : la garantie est `humain` **par** l'option — une garantie `oui` ne peut pas l'être."""
    v = decider([_claim("c1", "garantie", champs)], ask_client_max=ASK_MAX)
    assert v.value == "sous_conditions" and "option" in v.reason
    assert v.missing.faits == []
    # la question du paquet manquant se **précise** quand une clause citée en dépend (D8)
    assert any("clause citée" in q for q in v.ask_client)


def test_an_unconfirmed_kind_caps_the_verdict_at_conditional() -> None:
    """AD-6 : « verdict max `sous_conditions` » — la garantie non confirmée n'est jamais `oui`."""
    garantie = ClaimJugee(claim_id="c1", clauses=[_clause("garantie", confirmed=False)],
                          champs=_champs(True))
    v = decider([garantie], ask_client_max=ASK_MAX)
    assert v.value != "couvert"
    assert any("typage" in e for e in v.escalate)


def test_a_contradiction_between_two_displayed_claims_settles_nothing() -> None:
    garantie = _claim("c1", "garantie", _champs(True), contredit=True)
    exclusion = _claim("c2", "exclusion", _champs(True))
    v = decider([garantie, exclusion], ask_client_max=ASK_MAX)
    assert v.value == "ne_tranche_pas" and "contredisent" in v.reason
    assert any("arbitrage humain" in e for e in v.escalate)


def test_an_unresolved_reference_on_a_decisional_claim_settles_nothing() -> None:
    garantie = _claim("c1", "garantie", _champs(True), renvoi_ouvert=True)
    v = decider([garantie], ask_client_max=ASK_MAX)
    assert v.value == "ne_tranche_pas" and "renvoie" in v.reason


def test_an_unresolved_reference_on_a_non_decisional_claim_is_not_a_blocker() -> None:
    """AD-4 dit « renvoi non résolu sur une claim **décisionnelle** » : une définition n'en est pas une."""
    garantie = _claim("c1", "garantie", _champs(True))
    definition = _claim("c2", None, None, renvoi_ouvert=True)
    assert decider([garantie, definition], ask_client_max=ASK_MAX).value == "couvert"


def test_without_a_guarantee_or_an_exclusion_nothing_is_settled() -> None:
    """Règle (0bis) : une condition seule n'est pas une clause fondatrice."""
    v = decider([_claim("c1", "condition", _champs(True))], ask_client_max=ASK_MAX)
    assert v.value == "ne_tranche_pas" and "Aucune garantie ni exclusion" in v.reason


def test_no_claim_at_all_settles_nothing() -> None:
    """Le refus d'AD-3 (« zéro claim survivante ») donne bien `ne_tranche_pas`, jamais rien."""
    v = decider([], ask_client_max=ASK_MAX)
    assert v.value == "ne_tranche_pas" and v.reason


def test_only_displayed_claims_enter_the_table() -> None:
    """D4 : une claim non retenue ne fonde aucun verdict — elle n'est pas sous les yeux de l'utilisateur."""
    cachee = _claim("c1", "garantie", _champs(True), retenue=False)
    assert decider([cachee], ask_client_max=ASK_MAX).value == "ne_tranche_pas"


def test_ask_client_is_deduplicated_and_bounded() -> None:
    """D8 : les libellés du modèle sont dédupliqués et bornés en nombre ; le reste est composé ici."""
    claims = [_claim(f"c{i}", "condition", _champs(False, manquant=f"fait {i % 2}")) for i in range(6)]
    claims.append(_claim("c9", "garantie", _champs(True, option=True)))
    v = decider(claims, ask_client_max=4)
    assert v.missing.faits == ["fait 0", "fait 1"]  # six libellés, deux distincts
    assert len(v.ask_client) == 4
    assert v.ask_client[0].startswith("Quelles options") and "à cette condition" in v.ask_client[0]
    assert [q for q in v.ask_client if q.endswith("fait 0")] and [q for q in v.ask_client
                                                                  if q.endswith("fait 1")]
    # la borne coupe les questions **et** les libellés retenus : rien n'est affiché hors quota
    assert len(decider(claims, ask_client_max=2).ask_client) == 2


def test_the_bougie_case_derives_exactly_as_the_spec_says() -> None:
    """Exemple de la spec 1.8 : garantie `humain`, exclusion `non`, définitions `None` ⇒ `ne_tranche_pas`."""
    p34 = ClaimJugee(
        claim_id="c1", clauses=[_clause("garantie", block_id="d:p34:12", node_id=SOCLE, socle=True)],
        champs=_champs(False, manquant="caractère subit de l'action de la chaleur"))
    p46 = ClaimJugee(
        claim_id="c2", clauses=[_clause("exclusion", block_id="d:p46:1", portee={EXTENSION},
                                        node_id=EXTENSION, socle=False)],
        champs=_champs(False))
    definitions = ClaimJugee(claim_id="c3", clauses=[], champs=None)
    assert applicable_de_claim(p34) == "humain"
    assert applicable_de_claim(p46) == "non"
    assert applicable_de_claim(definitions) is None
    v = decider([p34, p46, definitions], ask_client_max=ASK_MAX)
    assert v.value == "ne_tranche_pas"
    assert v.missing.faits == ["caractère subit de l'action de la chaleur"]
    assert any("caractère subit" in q for q in v.ask_client)
