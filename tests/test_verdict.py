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
    ConditionDeSection,
    MissingPackage,
    applicable_de_claim,
    applicabilites_des_claims,
    decider,
    nomme_la_couverture,
)

ASK_MAX = 8
SOCLE = "d:n1"
EXTENSION = "d:n2"

# Le dossier complet : conditions particulières, options, avenants et date d'effet **au dossier**.
# Il ne décide **pas** de la valeur du verdict (revue Codex 1.8, B1 : la seconde branche de la règle
# (2) d'AD-6 porte sur la clause — « la garantie **dépend** d'une option / extension / condition
# particulière inconnue » —, pas sur le dossier) ; il décide de ce qu'il reste à **demander**. Rien
# ne le produit à J+1 : le pipeline ne lit que les conditions générales.
PAQUET_ETABLI = MissingPackage(conditions_particulieres=False, options_souscrites=False,
                               avenants=False, date_effet=False)


def _clause(kind: str, *, block_id: str = "d:p1:1", confirmed: bool = True,
            portee: set[str] | None = None, node_id: str = SOCLE, socle: bool = True) -> ClauseCitee:
    return ClauseCitee(block_id=block_id, kind=kind, kind_confirmed=confirmed,
                       portee={node_id} if portee is None else portee, node_id=node_id, socle=socle)


def _champs(present: bool = True, *, option: bool = False, cp: bool = False,
            manquant: str | None = None, exigees: list[str] | None = None,
            non_etablies: list[str] | None = None) -> ChampsApplicabilite:
    return ChampsApplicabilite(fait_requis_present=present, option_requise=option, cp_requise=cp,
                               fait_manquant=manquant, qualites_exigees=exigees or [],
                               qualites_non_etablies=non_etablies or [])


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
    # dossier au complet : sans lui, la règle (2) tranche avant qu'on puisse voir si l'exclusion mord
    v = decider([garantie, exclusion], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert v.value == "couvert"
    assert applicabilites_des_claims([garantie, exclusion])["c2"] == ("non", "hors_portee")


def test_an_exclusion_without_a_known_scope_stays_human_even_with_case_nodes() -> None:
    garantie = _claim("c1", "garantie", _champs(True))
    exclusion = _claim(
        "c2", "exclusion", _champs(True),
        clause=_clause("exclusion", block_id="d:p1:2", portee=set()),
    )
    assert applicabilites_des_claims([garantie, exclusion])["c2"] == ("humain", None)


def test_an_exclusion_without_a_displayed_guarantee_has_no_case_scope() -> None:
    """Une condition citée n'établit pas à elle seule la branche contractuelle du cas."""
    condition = ClaimJugee(
        claim_id="c1",
        clauses=[_clause("condition", block_id="d:p1:1", portee={SOCLE}, node_id=SOCLE)],
        champs=_champs(True),
    )
    exclusion = ClaimJugee(
        claim_id="c2",
        clauses=[_clause("exclusion", block_id="d:p1:2", portee={EXTENSION},
                         node_id=EXTENSION, socle=False)],
        champs=_champs(True),
    )

    assert applicabilites_des_claims([condition, exclusion])["c2"] == ("humain", None)


def test_an_exclusion_alone_never_covers_itself() -> None:
    """D3 : à défaut de garantie, aucune exclusion ne peut établir elle-même le cas."""
    exclusion = ClaimJugee(claim_id="c1", clauses=[_clause("exclusion")], champs=_champs(True))
    assert decider([exclusion], ask_client_max=ASK_MAX).value == "ne_tranche_pas"


def test_a_baseline_guarantee_alone_is_covered() -> None:
    """Règle (3) : garantie du socle `oui`, aucune claim `humain` ⇒ `couvert`.

    La fixture que l'AC de la story exige nommément (« `couvert` (garantie socle) »), et **sans
    précondition ajoutée** : l'AC énonce la règle (3) « garantie du socle `oui` sans condition
    ouverte ⇒ `couvert` », pas « et le dossier au complet ». Revue Codex 1.8 (B1, tour 2) : le tour 1
    lisait « dépend d'une … condition particulière **inconnue** » sur `MissingPackage`, donc sur le
    dossier global, ce qui rendait la règle (3) inatteignable sans un argument que l'AC ne mentionne
    nulle part. La dépendance se lit sur la **clause** — `option_requise`, `cp_requise`, le socle.
    """
    v = decider([_claim("c1", "garantie", _champs(True))], ask_client_max=ASK_MAX)
    assert v.value == "couvert" and v.missing.faits == [] and v.escalate == []


def test_an_unknown_package_is_announced_and_asked_but_never_decides() -> None:
    """`MissingPackage` accompagne le verdict ; il ne le fixe pas (revue Codex 1.8, B1, tour 2).

    Un `couvert` rendu « au regard des conditions générales seules » annonce quand même les quatre
    pièces qu'il n'a pas lues et les réclame — **une question par pièce** : annoncer quatre absences
    et n'en demander que deux laisserait le gestionnaire deviner (D8). Ce que le paquet ne fait plus,
    c'est décider de la valeur.
    """
    v = decider([_claim("c1", "garantie", _champs(True))], ask_client_max=ASK_MAX)
    assert v.value == "couvert"
    assert v.missing.conditions_particulieres and v.missing.options_souscrites  # le paquet reste dû
    assert len(v.ask_client) == 3 and all("Fait à établir" not in q for q in v.ask_client)
    assert any("options" in q for q in v.ask_client)
    assert any("conditions particulières" in q for q in v.ask_client)
    assert any("avenant" in q and "date" in q for q in v.ask_client)
    # et le dossier au complet ne change que ce qu'il y a à réclamer
    complet = decider([_claim("c1", "garantie", _champs(True))], ask_client_max=ASK_MAX,
                      missing=PAQUET_ETABLI)
    assert complet.value == "couvert" and complet.ask_client == []


@pytest.mark.parametrize("piece", ["conditions_particulieres", "options_souscrites"])
def test_neither_missing_piece_changes_the_verdict_value(piece: str) -> None:
    """Le pendant du test précédent, pièce par pièce : la valeur ne bouge pas, la question apparaît."""
    presque = PAQUET_ETABLI.model_copy(update={piece: True})
    v = decider([_claim("c1", "garantie", _champs(True))], ask_client_max=ASK_MAX, missing=presque)
    assert v.value == "couvert"
    assert len(v.ask_client) == 1


def test_a_quality_the_clause_requires_is_always_asked_even_when_said_established() -> None:
    """Revue Codex 1.8 (B3) : l'AC « `ask_client` mentionne […] la nature « subite » » devient du code.

    Mesuré : sur un run réel, le modèle a déclaré la qualité subite **établie**, `fait_manquant` est
    resté nul, et aucune question ne l'a mentionnée — l'AC était donc violé par un run vert. Le code
    ne peut pas juger si « soudain » est établi ; il peut poser la question dès que la clause exige la
    qualité, quelle qu'ait été la réponse du modèle. Un verdict rendu au regard des seules conditions
    générales ne prouve de toute façon aucune qualité de l'événement.
    """
    subite = "caractère subit de l'action de la chaleur"
    etablie = _champs(True, exigees=[subite])  # le modèle dit : exigée **et** établie
    v = decider([_claim("c1", "garantie", etablie)], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert v.value == "couvert"  # établie : la clause reste applicable…
    assert v.missing.faits == []  # …et ce n'est pas un fait manquant…
    assert any(subite in q and "confirmer" in q for q in v.ask_client)  # …mais on le fait confirmer


def test_a_required_quality_left_unestablished_keeps_the_guarantee_human() -> None:
    """Revue Codex 1.8 (B3) : `fait_requis_present=true` ne suffit plus à faire un `oui`.

    Un run réel a rendu la qualité « subite » pour établie sur des circonstances qui ne la disent pas,
    et rien ne pouvait contredire ce booléen. Le modèle **énumère** désormais ce que la clause exige et
    ce que les faits établissent ; le code fait la différence (AD-6 : « il extrait des valeurs typées,
    le code compare »). Une qualité exigée non établie rend `humain`, quoi que vaille le booléen.
    """
    subite = "caractère subit de l'action de la chaleur"
    champs = _champs(True, exigees=[subite], non_etablies=[subite])
    assert applicable_de_claim(_claim("c1", "garantie", champs)) == "humain"
    v = decider([_claim("c1", "garantie", champs)], ask_client_max=ASK_MAX)
    assert v.value == "ne_tranche_pas"
    # « forcer `humain` **et produire une question bornée** » : la qualité manquante est demandée
    assert v.missing.faits == [subite]
    assert any(subite in q for q in v.ask_client)
    # une qualité exigée **et** établie ne retire rien
    assert applicable_de_claim(_claim("c1", "garantie", _champs(True, exigees=[subite]))) == "oui"


def test_an_unestablished_package_alone_never_produces_a_verdict_out_of_thin_air() -> None:
    """La règle (2) **ouvre** un verdict, elle n'en crée pas : sans garantie `oui`, rien ne change."""
    sans_garantie = _claim("c1", "condition", _champs(True))
    assert decider([sans_garantie], ask_client_max=ASK_MAX).value == "ne_tranche_pas"
    humaine = _claim("c2", "garantie", _champs(False, manquant="caractère subit"))
    assert decider([humaine], ask_client_max=ASK_MAX).value == "ne_tranche_pas"


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
    v = decider([garantie, definition], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert v.value == "couvert"  # aucun blocage : ni la règle (0), ni la règle (2)


def test_without_a_guarantee_or_an_exclusion_nothing_is_settled() -> None:
    """Règle (0bis) : une condition seule n'est pas une clause fondatrice."""
    v = decider([_claim("c1", "condition", _champs(True))], ask_client_max=ASK_MAX)
    assert v.value == "ne_tranche_pas"
    assert "passages ont été retrouvés et affichés" in v.reason
    assert "aucun n'est confirmé comme garantie ou exclusion fondatrice" in v.reason


def test_no_claim_at_all_settles_nothing() -> None:
    """Le refus d'AD-3 (« zéro claim survivante ») donne bien `ne_tranche_pas`, jamais rien."""
    v = decider([], ask_client_max=ASK_MAX)
    assert v.value == "ne_tranche_pas" and "Aucun passage n'a été retenu et affiché" in v.reason


def test_only_displayed_claims_enter_the_table() -> None:
    """D4 : une claim non retenue ne fonde aucun verdict — elle n'est pas sous les yeux de l'utilisateur."""
    cachee = _claim("c1", "garantie", _champs(True), retenue=False)
    assert decider([cachee], ask_client_max=ASK_MAX).value == "ne_tranche_pas"


def test_a_nonempty_partial_resolution_map_is_recomputed_without_key_error() -> None:
    garantie = _claim("c1", "garantie", _champs(True))
    condition = _claim("c2", "condition", _champs(False, manquant="fait à établir"))
    partial = {"c1": ("oui", None)}

    verdict = decider(
        [garantie, condition], ask_client_max=ASK_MAX, resolutions=partial,
    )

    assert verdict.value == "sous_conditions"
    assert verdict.missing.faits == ["fait à établir"]


def test_ask_client_is_deduplicated_and_bounded() -> None:
    """D8 : les libellés du modèle sont dédupliqués et bornés en nombre ; le reste est composé ici."""
    claims = [_claim(f"c{i}", "condition", _champs(False, manquant=f"fait {i % 2}")) for i in range(6)]
    claims.append(_claim("c9", "garantie", _champs(True, option=True)))
    v = decider(claims, ask_client_max=5)
    assert v.missing.faits == ["fait 0", "fait 1"]  # six libellés, deux distincts
    assert len(v.ask_client) == 5
    assert v.ask_client[0].startswith("Quelles options") and "à cette condition" in v.ask_client[0]
    assert [q for q in v.ask_client if q.endswith("fait 0")] and [q for q in v.ask_client
                                                                  if q.endswith("fait 1")]


def test_a_missing_fact_that_no_question_can_carry_is_not_announced_either() -> None:
    """Revue 1.8 : `missing.faits` et `ask_client` disent la même chose, ou ne disent rien.

    Les trois questions du paquet manquant occupent les premières places ; ce qu'elles laissent borne
    les libellés du modèle. Sans cela, le front affichait « fait à établir : X » dans le paquet et
    aucune question pour le demander — un manque annoncé que rien ne réclame.
    """
    claims = [_claim(f"c{i}", "condition", _champs(False, manquant=f"fait {i}")) for i in range(4)]
    claims.append(_claim("c9", "garantie", _champs(True)))
    # exactement la place du paquet : aucun libellé ne rentre, donc aucun n'est annoncé
    juste = decider(claims, ask_client_max=3)
    assert len(juste.ask_client) == 3 and juste.missing.faits == []
    # une place de plus : le premier libellé entre, et il est **demandé**
    une = decider(claims, ask_client_max=4)
    assert une.missing.faits == ["fait 0"]
    assert [q for q in une.ask_client if q.endswith("fait 0")]
    # sous la taille du paquet, les questions elles-mêmes sont coupées, et rien n'est annoncé de plus
    serre = decider(claims, ask_client_max=2)
    assert len(serre.ask_client) == 2 and serre.missing.faits == []
    # dans tous les cas, tout ce que `missing.faits` annonce a sa question
    for v in (juste, une, serre):
        assert all(any(q.endswith(libelle) for q in v.ask_client) for libelle in v.missing.faits)


@pytest.mark.parametrize("kind", ["condition", "franchise"])
@pytest.mark.parametrize("manquant", [None, "occupation permanente du bien"])
def test_a_condition_or_a_franchise_is_never_declared_inapplicable(kind: str,
                                                                   manquant: str | None) -> None:
    """Revue 1.8 : `non` est réservé aux clauses fondatrices.

    Sur une condition, `fait_requis_present=false` ne distingue pas « cette condition ne concerne pas
    ce cas » de « cette condition n'est **pas remplie** ». Lire la première rendrait `non`, sortirait
    la clause de la règle (2) et laisserait passer un `couvert` alors qu'une condition citée est en
    défaut — l'inverse exact de la politique conservatrice d'AD-6.
    """
    claim = _claim("c1", kind, _champs(False, manquant=manquant))
    assert applicable_de_claim(claim) == "humain"
    garantie = _claim("c2", "garantie", _champs(True))
    v = decider([garantie, claim], ask_client_max=ASK_MAX)
    assert v.value == "sous_conditions"


@pytest.mark.parametrize("kind", ["garantie", "exclusion"])
def test_a_founding_clause_keeps_the_inapplicable_reading(kind: str) -> None:
    """Le pendant : sur une garantie ou une exclusion, « connu et contraire » veut dire « pas ce cas »."""
    assert applicable_de_claim(_claim("c1", kind, _champs(False))) == "non"


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


# --- Correctif du tour 6 (F3) : `couvert` répond à toute la demande, ou ne tranche pas ----------


def test_une_sous_question_sans_clause_empeche_un_couvert() -> None:
    """`couvert` est le seul verdict qui affirme quelque chose de la **totalité** de la demande.

    Mesuré sur un run réel : une question à deux sous-questions — le bris d'une vitre d'insert et
    les dommages par la fumée — est ressortie `couvert` sur la **seule** clause des fumées, la
    sous-question du bris n'ayant reçu aucune clause décisionnelle. La même réponse portait, au même
    moment, `complete=false` et « il reste 1 sous-question sans réponse » : le verdict disait le
    contraire du reste de la réponse. Le compte vient de la mesure du code, jamais d'une déclaration.
    """
    garantie = _claim("c1", "garantie", _champs())
    couvert = decider([garantie], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert couvert.value == "couvert"

    partiel = decider([garantie], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI,
                      facettes_sans_reponse=1)
    assert partiel.value == "ne_tranche_pas"
    assert "aucune clause du contrat" in partiel.reason
    # La question passe devant les autres : c'est elle qui empêche de trancher.
    assert partiel.ask_client[0].startswith("1 sous-question de votre demande")
    assert len(partiel.ask_client) <= ASK_MAX


def test_les_autres_verdicts_ne_bougent_pas_pour_une_sous_question_sans_clause() -> None:
    """La borne : seul `couvert` prétend quelque chose de ce qui manque, donc seul lui recule.

    `sous_conditions` et `ne_tranche_pas` disent déjà qu'ils ne tranchent pas tout ; `non_couvert`
    repose sur une exclusion applicable, que l'absence d'une autre sous-question ne dément pas.
    """
    garantie = ClaimJugee(claim_id="c1", clauses=[_clause("garantie", block_id="d:p1:1")],
                          champs=_champs(True))
    exclusion = ClaimJugee(claim_id="c2", clauses=[_clause("exclusion", block_id="d:p1:2")],
                           champs=_champs(True))
    assert decider([garantie, exclusion], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI,
                   facettes_sans_reponse=2).value == "non_couvert"
    hors_socle = _claim("c2", "garantie", _champs(),
                        clause=_clause("garantie", node_id=EXTENSION, socle=False))
    assert decider([hors_socle], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI,
                   facettes_sans_reponse=1).value == "sous_conditions"
    # Une question à une seule sous-question, couverte : le cas témoin de la bougie ne bouge pas.
    assert decider([_claim("c3", "garantie", _champs())], ask_client_max=ASK_MAX,
                   missing=PAQUET_ETABLI, facettes_sans_reponse=0).value == "couvert"


# --- Lecture utilisateur des runs A16 (story 5.6, T8) ---------------------------------------------
def test_deux_libelles_qui_exigent_la_meme_qualite_ne_sont_demandes_quune_fois() -> None:
    """Run 1 : quatre « faits à établir » pour deux exigences, dont un mot pour mot inclus dans l'autre.

    Le modèle nomme un fait manquant, puis énumère les qualités non établies dans d'autres termes ;
    la déduplication n'était que l'égalité de chaînes, et le gestionnaire lisait « action subite de
    la chaleur ou contact direct avec le foyer » **puis** « action subite de la chaleur ». La
    comparaison porte désormais sur ce que le libellé exige — les racines du lexique —, et la
    formulation la plus complète (celle qui porte le plus d'exigences) est celle qui reste.
    """
    manquant = "action subite de la chaleur ou contact direct avec le foyer"
    soudain = "caractère soudain de l'événement"
    immediat = "contact direct et immédiat avec un foyer ou une substance incandescente"
    champs = _champs(False, manquant=manquant,
                     exigees=[soudain, "action subite de la chaleur", immediat],
                     non_etablies=[soudain, "action subite de la chaleur", immediat])
    v = decider([_claim("c1", "garantie", champs)], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert v.missing.faits == [manquant, soudain, immediat]  # « action subite… » seule a disparu
    assert len([q for q in v.ask_client if "subite" in q]) == 1


def test_le_libelle_compose_par_le_code_ne_double_pas_les_mots_du_modele() -> None:
    """Run 3 : « caractère accidentel du bris », puis « caractère « accidentel » exigé par la clause ».

    Les deux libellés viennent de deux claims et de deux sources — le modèle pour le premier, le code
    pour le second (`steps.verifier._qualites_de_la_clause`, qui ne dédoublonne que dans **sa** claim).
    À exigence égale, ce sont les mots du modèle qui restent : ils nomment le fait du dossier, là où la
    phrase composée ne fait que renvoyer à la clause. La plus complète n'est pas la plus longue.
    """
    du_modele = "caractère accidentel du bris"
    du_code = "caractère « accidentel » exigé par la clause citée"
    claims = [_claim("c1", "garantie", _champs(False, manquant=du_modele)),
              _claim("c2", "exclusion", _champs(True, exigees=[du_code], non_etablies=[du_code]))]
    v = decider(claims, ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert v.missing.faits == [du_modele]
    assert [q for q in v.ask_client if du_code in q] == []


def test_une_qualite_deja_demandee_comme_fait_nest_pas_redemandee_pour_confirmation() -> None:
    """Une même exigence, deux claims, deux préfixes : « à établir » suffit, « à confirmer » double."""
    subite = "caractère subit de l'action de la chaleur"
    manquante = _claim("c1", "garantie", _champs(False, manquant=subite))
    etablie = _claim("c2", "condition", _champs(True, exigees=["nature subite du sinistre"]))
    v = decider([manquante, etablie], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert v.missing.faits == [subite]
    assert [q for q in v.ask_client if "confirmer" in q] == []


def test_une_exclusion_hors_portee_ne_fonde_aucun_fait_a_etablir() -> None:
    """Reprise différée `libelles-manquants-verse-les-claims-inapplicables`, raison `hors_portee`.

    Run 3 : le gestionnaire devait établir « rayures, égratignures ou écaillements » au titre d'une
    exclusion que la table venait d'écarter. Une clause `applicable="non"` n'exige rien de ce
    dossier — ce qu'elle demandait est sans objet, et le demander rouvre une piste déjà refermée.
    """
    garantie = ClaimJugee(claim_id="c1", clauses=[_clause("garantie", block_id="d:p1:1")],
                          champs=_champs(True))
    ailleurs = _clause("exclusion", block_id="d:p1:2", portee={EXTENSION}, node_id=EXTENSION, socle=False)
    hors_portee = ClaimJugee(claim_id="c2", clauses=[ailleurs],
                             champs=_champs(True, manquant="rayures, égratignures ou écaillements",
                                            exigees=["défaut de réparation ou d'entretien des châssis"],
                                            non_etablies=["défaut de réparation ou d'entretien des châssis"]))
    assert applicabilites_des_claims([garantie, hors_portee])["c2"] == ("non", "hors_portee")
    v = decider([garantie, hors_portee], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert v.value == "couvert" and v.missing.faits == []
    assert [q for q in v.ask_client if "rayures" in q or "châssis" in q] == []


def test_une_clause_dont_le_fait_est_connu_et_contraire_ne_fonde_rien_non_plus() -> None:
    """L'autre chemin vers `non` : le fait exigé est connu et contraire (aucun `fait_manquant`).

    Les qualités que le code a composées pour cette clause (`qualites_non_etablies`) partaient encore
    dans le paquet manquant, alors que la table venait de la déclarer sans objet pour ce dossier.
    """
    contraire = _claim("c2", "exclusion",
                       _champs(False, exigees=["caractère intentionnel du dommage"],
                               non_etablies=["caractère intentionnel du dommage"]))
    assert applicable_de_claim(contraire) == "non"
    v = decider([_claim("c1", "garantie", _champs(True)), contraire],
                ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert v.value == "couvert" and v.missing.faits == [] and v.ask_client == []


# --- (c) L1e : la condition écrite en tête de la section d'une garantie -------
CONDITION_CP = ConditionDeSection(
    block_id="d:p37:11", titre="3.1.4 Dégâts des eaux",
    texte="Les présentes conditions spéciales sont applicables si les conditions particulières "
          "mentionnent que la garantie “ dégâts des eaux ” est souscrite.",
    renvoie_cp=True)


def _garantie_conditionnee(claim_id: str = "c1", *,
                           condition: ConditionDeSection = CONDITION_CP) -> ClaimJugee:
    clause = _clause("garantie", block_id="d:p37:13")
    return ClaimJugee(claim_id=claim_id,
                      clauses=[clause.model_copy(update={"condition_section": condition})],
                      champs=_champs(True))


def test_une_garantie_conditionnee_par_les_cp_ne_sort_jamais_couverte() -> None:
    """Témoin (a) de L1e — le cas mesuré : S2 sans claim sur `p37:11`.

    La garantie est du socle par sa portée, ses champs typés sont les plus favorables qui soient, et
    aucune autre clause n'est ouverte : c'est exactement le jeu qui rendait `couvert`. Le contrat
    écrit pourtant, en tête de « 3.1.4 Dégâts des eaux », qu'elle n'est applicable que si les
    conditions particulières la mentionnent — et personne ne les a lues.
    """
    v = decider([_garantie_conditionnee()], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert v.value == "sous_conditions", v.value
    # La condition est **montrée** : le bloc et son texte, pour que « sous conditions » se comprenne.
    assert "d:p37:11" in v.reason and "dégâts des eaux" in v.reason
    assert ("Vos conditions particulières mentionnent-elles la garantie "
            "« 3.1.4 Dégâts des eaux » ?") in v.ask_client


def test_une_garantie_sans_condition_en_tete_de_section_ne_bouge_pas() -> None:
    """Témoin (b) — le plafond ne ferme pas la règle (3) : c'est une propriété de la section citée."""
    v = decider([_claim("c1", "garantie", _champs(True))], ask_client_max=ASK_MAX,
                missing=PAQUET_ETABLI)
    assert v.value == "couvert", v.value
    assert not any("conditions particulières mentionnent" in q for q in v.ask_client)


def test_aucune_combinaison_de_claims_ne_couvre_une_section_conditionnee() -> None:
    """Témoin (c), la contre-épreuve : la condition ne se lève que par une claim retenue `oui`.

    On lui ajoute successivement tout ce qui, dans la table, pousse vers `couvert` : une seconde
    garantie de socle inconditionnelle, une exclusion écartée, et la claim de condition elle-même —
    d'abord telle que le pipeline la produit (`cp_requise` forcé par le texte, donc `humain`), puis au
    mieux-disant. Seul le dernier cas la lève, et il est hors d'atteinte tant que les CP ne sont pas
    au dossier.
    """
    conditionnee = _garantie_conditionnee()
    autre = _claim("c2", "garantie", _champs(True))
    ecartee = _claim("c3", "exclusion", _champs(False))
    citee_humain = ClaimJugee(claim_id="c4",
                              clauses=[_clause("condition", block_id="d:p37:11")],
                              champs=_champs(True, cp=True))
    for jeu in ([conditionnee], [conditionnee, autre], [conditionnee, autre, ecartee],
                [conditionnee, citee_humain]):
        assert decider(jeu, ask_client_max=ASK_MAX, missing=PAQUET_ETABLI).value != "couvert", jeu
    citee_etablie = ClaimJugee(claim_id="c4",
                               clauses=[_clause("condition", block_id="d:p37:11")],
                               champs=_champs(True))
    assert applicable_de_claim(citee_etablie) == "oui"
    v = decider([conditionnee, citee_etablie], ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert v.value == "couvert", v.value


def test_une_condition_de_section_qui_ne_renvoie_pas_aux_cp_se_demande_dans_ses_termes() -> None:
    """Le lexique `RENVOIS_CP` est un **témoin**, pas la règle : il choisit les mots de la question.

    La règle est structurelle — un bloc `condition` en tête de section, avant les sous-sections —, et
    elle attrape aussi des conditions qui ne renvoient à aucune pièce du dossier. Leur demander « vos
    conditions particulières mentionnent-elles… » n'aurait aucun sens ; le plafond, lui, ne change pas.
    """
    interne = CONDITION_CP.model_copy(update={
        "block_id": "d:p76:6", "titre": "4.1.3.5 Frais de recours",
        "texte": "Lorsque, avec l’accord écrit préalable de la Compagnie, il y a lieu de solliciter "
                 "l’avis d’un expert…", "renvoie_cp": False})
    v = decider([_garantie_conditionnee(condition=interne)], ask_client_max=ASK_MAX,
                missing=PAQUET_ETABLI)
    assert v.value == "sous_conditions", v.value
    assert ("La condition posée en tête de « 4.1.3.5 Frais de recours » est-elle remplie ?"
            in v.ask_client)


def test_une_garantie_ecartee_ne_fait_pas_poser_la_question_de_sa_section() -> None:
    """Même doctrine que `_libelles_manquants` : une clause sans objet ne subordonne rien."""
    clause = _clause("garantie", block_id="d:p37:13").model_copy(
        update={"condition_section": CONDITION_CP})
    ecartee = ClaimJugee(claim_id="c1", clauses=[clause], champs=_champs(False))
    assert applicable_de_claim(ecartee) == "non"
    v = decider([ecartee, _claim("c2", "garantie", _champs(True))],
                ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert not any("3.1.4 Dégâts des eaux" in q for q in v.ask_client)
    assert v.value == "couvert", v.value


# --- L1m : une exigence qui nomme la couverture elle-même --------------------
COUVERTURE = "caractère couvert du sinistre"


@pytest.mark.parametrize("libelle", [
    COUVERTURE, "sinistre garanti", "dommages indemnisés", "prise en charge du sinistre",
    "caractère assuré de l'événement"])
def test_a_label_that_only_names_the_coverage_is_circular(libelle: str) -> None:
    """L1m : le libellé ne dit rien d'autre que « ce sinistre est couvert » — c'est le verdict."""
    assert nomme_la_couverture(libelle)


@pytest.mark.parametrize("libelle", [
    "", "défaut d'entretien", "caractère « soudain » exigé par la clause citée",
    "qualité d'assuré de la personne en cause, exigée par la clause citée",
    "garde du bien par l'assuré, exigée par la clause citée", "biens assurés désignés",
    "garantie souscrite"])
def test_a_label_that_names_a_fact_besides_the_coverage_stays_due(libelle: str) -> None:
    """Le garde-fou du lexique : porter « assuré » ne suffit pas, il faut ne rien dire d'autre."""
    assert not nomme_la_couverture(libelle)


def test_a_coverage_requirement_is_never_a_missing_fact_nor_a_question() -> None:
    """Règle 1 : ni `fait_manquant`, ni qualité non établie — et `fait_requis_present` remis à vrai.

    L'effacer seul aurait donné la signature du « fait connu et contraire » (`applicable="non"`) :
    une clause écartée là où elle doit suivre sa garantie.
    """
    champs = _champs(False, manquant=COUVERTURE, exigees=[COUVERTURE, "caractère « soudain » exigé"],
                     non_etablies=[COUVERTURE])
    assert champs.fait_manquant is None
    assert champs.fait_requis_present is True
    assert champs.qualites_non_etablies == []
    assert COUVERTURE in champs.qualites_exigees  # la trace garde ce que la clause écrivait
    assert champs.reference_a_la_couverture is True


def _etendue_et_sa_garantie(principale: ChampsApplicabilite) -> list[ClaimJugee]:
    """Le nœud « 3.1.4 Dégâts des eaux » du parcours de prod : la garantie, puis sa clause d'étendue.

    `p38:2` — « La perte d'eau subie à l'occasion d'un sinistre couvert est prise en charge à
    concurrence de 1.000 € » — n'exige du sinistre que d'être couvert : elle suit `p37:1`.
    """
    garantie = ClaimJugee(claim_id="c1", clauses=[_clause("garantie", block_id="d:p37:1")],
                          champs=principale)
    etendue = ClaimJugee(
        claim_id="c2", clauses=[_clause("garantie", block_id="d:p38:2")],
        champs=_champs(False, manquant=COUVERTURE, exigees=[COUVERTURE], non_etablies=[COUVERTURE]))
    return [garantie, etendue]


@pytest.mark.parametrize("principale, attendu", [
    (_champs(True), "oui"), (_champs(True, cp=True), "humain"), (_champs(False), "non")])
def test_an_extent_clause_follows_the_main_guarantee_of_its_node(
        principale: ChampsApplicabilite, attendu: str) -> None:
    """Règle 2 : `oui` si la garantie l'est, `humain` si elle l'est, `non` sinon (L1m)."""
    claims = _etendue_et_sa_garantie(principale)
    assert applicabilites_des_claims(claims)["c2"][0] == attendu


def test_an_extent_clause_alone_at_its_node_stays_human() -> None:
    """Aucune garantie principale au nœud : rien n'établit que le sinistre est couvert."""
    etendue = _etendue_et_sa_garantie(_champs(True))[1]
    assert applicabilites_des_claims([etendue])["c2"][0] == "humain"


def test_an_extent_clause_never_tips_the_verdict_to_ne_tranche_pas() -> None:
    """Le témoin du parcours de prod du 04/09 : `p38:2` rendait `ne_tranche_pas` un dossier couvert.

    La garantie du socle s'applique, plus rien n'est ouvert — et la seule clause encore `humain`
    l'était pour une exigence qui nommait le verdict lui-même.
    """
    verdict = decider(_etendue_et_sa_garantie(_champs(True)), ask_client_max=ASK_MAX,
                      missing=PAQUET_ETABLI)
    assert verdict.value == "couvert"
    assert not any("couvert" in question for question in verdict.ask_client)
    assert verdict.missing.faits == []


def test_a_real_quality_of_the_same_clause_is_still_asked() -> None:
    """Témoin (b) : la référence à la couverture part, « soudain » reste dû au client."""
    claims = _etendue_et_sa_garantie(_champs(True))
    claims[1].champs = _champs(False, manquant=COUVERTURE, exigees=[COUVERTURE, "caractère soudain"],
                               non_etablies=[COUVERTURE, "caractère soudain"])
    assert applicabilites_des_claims(claims)["c2"][0] == "humain"
    verdict = decider(claims, ask_client_max=ASK_MAX, missing=PAQUET_ETABLI)
    assert verdict.missing.faits == ["caractère soudain"]
    # La clause reste ouverte, donc le verdict aussi : `couvert` est hors d'atteinte tant que
    # « soudain » n'est pas établi. Que la table rende ici `ne_tranche_pas` plutôt que
    # `sous_conditions` ne tient pas à L1m — la règle (2) ne compte comme « clause ouverte » qu'une
    # condition, une franchise ou une exclusion, jamais une seconde garantie `humain`.
    assert verdict.value != "couvert"
