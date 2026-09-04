"""Story 5.7 (L1q), défaut 2 — deux natures d'exigence, et la garantie sœur citée en surplus.

Hermétique : ni LLM, ni réseau. Deux règles, mesurées le 04/09/2026 sur le cas des plaques à
induction (`retours-1400/sinistre-induction.txt`), et leurs contre-épreuves.

1. **Une définition descriptive que la clause écrit est établie ; un qualificatif du lexique reste
   dû au client.** La clause `p34:7` du contrat AXA écrit « des flammes se **propageant** … en dehors
   de leur domaine normal » ; le vérificateur rendait le fait manquant « **propagation** des flammes
   hors de leur domaine normal ». Aucun des deux mots n'est le préfixe de l'autre : le contrôle ne
   les rapprochait pas, et le dossier redemandait à l'assuré la propagation que la clause qu'il
   lisait venait de nommer.
2. **Une garantie établie n'est pas rétrogradée par une garantie sœur.** `3.1.1.1.1` et `3.1.1.1.6`
   sont deux des six périls que `p34:6` annonce. L'incendie porte la couverture ; le caractère
   soudain qu'exige le sixième item reste une question, pas un `ne_tranche_pas`.
"""

from __future__ import annotations

from server.app.domain.verdict import ChampsApplicabilite, ClaimJugee, ClauseCitee, decider
from server.app.steps.verifier import (
    _dit_la_qualite,
    _ecrite_par_la_clause,
    _meme_mot,
    _qualifie_par_la_clause,
)
from server.app.corpus.text import normalize

MIN = 5  # `Settings.qualite_mot_min_chars`
ASK_MAX = 8

# Le texte de `axa-lu-optihome-2017:p34:7`, relu dans le corpus servi.
INCENDIE = normalize(
    "3.1.1.1.1 L’incendie, c’est-à-dire la destruction par des flammes se propageant ou "
    "susceptibles de se propager en dehors de leur domaine normal ou d’objets dont la destination "
    "n’est pas, à ce moment, de brûler ;")
# Celui de `p34:12`, le péril sœur.
SOUDAIN = normalize(
    "3.1.1.1.6 Les dégâts occasionnés au mobilier assuré et au bâtiment désigné par un événement "
    "soudain, résultant de l’action subite de la chaleur ou du contact direct et immédiat avec le "
    "feu ou une substance incandescente")


# --- 1. la définition descriptive, et le qualificatif qui lui résiste --------------------
def test_deux_derives_de_la_meme_racine_sont_le_meme_mot() -> None:
    assert _meme_mot("propagation", "propageant", min_chars=MIN)
    assert _meme_mot("debordement", "deborde", min_chars=MIN)
    assert _meme_mot("deborde", "debordement", min_chars=MIN)  # symétrique, dans les deux sens
    assert _meme_mot("subit", "subite", min_chars=MIN)  # l'ancien préfixe reste subsumé


def test_deux_mots_qui_ne_partagent_pas_leur_racine_restent_distincts() -> None:
    assert not _meme_mot("soudain", "canape", min_chars=MIN)
    assert not _meme_mot("normal", "norme", min_chars=MIN)  # quatre lettres partagées, pas cinq
    assert not _meme_mot("incendie", "incident", min_chars=MIN)


def test_la_definition_que_la_clause_ecrit_est_lue_malgre_la_derivation() -> None:
    """**Rouge avant L1q** : `propagation` n'était cherché que tel quel dans le texte de la clause."""
    libelle = "propagation des flammes hors de leur domaine normal"
    assert _ecrite_par_la_clause(libelle, INCENDIE, min_chars=MIN)
    # Et c'est bien une définition descriptive : aucun qualificatif du lexique, donc la porte s'ouvre.
    assert _qualifie_par_la_clause(libelle, INCENDIE, min_chars=MIN)


def test_un_qualificatif_du_lexique_reste_du_au_client() -> None:
    """Contre-épreuve du cas bougie : `soudain` et `subite` ne se déduisent d'aucune circonstance."""
    for libelle in ("caractère soudain de l'événement",
                    "action subite de la chaleur",
                    "contact direct et immédiat avec le feu"):
        # Le texte de la clause les écrit — ce n'est pas ce qui les établit.
        assert _ecrite_par_la_clause(libelle, SOUDAIN, min_chars=MIN)
        assert not _qualifie_par_la_clause(libelle, SOUDAIN, min_chars=MIN)


def test_le_fragment_des_faits_du_cas_bougie_n_etablit_toujours_rien() -> None:
    fait = "Une bougie allumée posée sur une table basse est tombée sur le canapé"
    for libelle in ("caractère soudain de l'événement", "action subite de la chaleur"):
        assert not _dit_la_qualite(libelle, fait, min_chars=MIN)


# --- 2. la garantie sœur citée en surplus ------------------------------------------------
def _peril(claim_id: str, block: str, node: str, *, champs: ChampsApplicabilite) -> ClaimJugee:
    """Un item de l'énumération `p34:6` — même amorce, nœuds frères, socle commun."""
    return ClaimJugee(
        claim_id=claim_id, champs=champs,
        clauses=[ClauseCitee(block_id=block, kind="garantie", kind_confirmed=True,
                             portee={node}, node_id=node, socle=True, amorce_de="d:p34:6")])


def _incendie_etabli() -> ClaimJugee:
    return _peril("c1", "d:p34:7", "d:a3.1.1.1.1",
                  champs=ChampsApplicabilite(fait_requis_present=True))


def _soeur_ouverte() -> ClaimJugee:
    return _peril("c2", "d:p34:12", "d:a3.1.1.1.6",
                  champs=ChampsApplicabilite(
                      fait_requis_present=False, fait_manquant="caractère soudain de l'événement",
                      qualites_exigees=["caractère soudain de l'événement"],
                      qualites_non_etablies=["caractère soudain de l'événement"]))


def test_une_garantie_soeur_ouverte_n_annule_plus_la_garantie_etablie() -> None:
    """**Rouge avant L1q** : la règle (3) refusait de trancher, la table tombait en `ne_tranche_pas`."""
    verdict = decider([_incendie_etabli(), _soeur_ouverte()], ask_client_max=ASK_MAX)
    assert verdict.value == "sous_conditions"
    assert "même énumération" in verdict.reason
    # Les qualités de la sœur restent des questions au client — secondaires, pas un blocage.
    assert any("soudain" in q for q in verdict.ask_client)


def test_une_garantie_ouverte_d_une_autre_enumeration_bloque_toujours() -> None:
    """La sororité se lit sur l'amorce partagée : une garantie d'ailleurs pèse comme avant."""
    ailleurs = ClaimJugee(
        claim_id="c3",
        champs=ChampsApplicabilite(fait_requis_present=False, fait_manquant="occupation du bien"),
        clauses=[ClauseCitee(block_id="d:p50:1", kind="garantie", kind_confirmed=True,
                             portee={"d:a5"}, node_id="d:a5", socle=True, amorce_de="d:p50:0")])
    verdict = decider([_incendie_etabli(), ailleurs], ask_client_max=ASK_MAX)
    assert verdict.value == "ne_tranche_pas"


def test_une_condition_ouverte_reste_une_condition_ouverte() -> None:
    """Une condition `humain` n'est pas une sœur : elle ouvre le verdict par son propre motif."""
    condition = ClaimJugee(
        claim_id="c4",
        champs=ChampsApplicabilite(fait_requis_present=False, fait_manquant="permanence de l'occupation"),
        clauses=[ClauseCitee(block_id="d:p34:20", kind="condition", kind_confirmed=True,
                             portee={"d:a3.1.1.1.1"}, node_id="d:a3.1.1.1.1", socle=True,
                             amorce_de="d:p34:6")])
    verdict = decider([_incendie_etabli(), condition], ask_client_max=ASK_MAX)
    assert verdict.value == "sous_conditions"
    assert "une condition, une franchise ou une exclusion citée reste ouverte" in verdict.reason


def test_sans_soeur_ouverte_la_garantie_seule_reste_couverte() -> None:
    verdict = decider([_incendie_etabli()], ask_client_max=ASK_MAX)
    assert verdict.value == "couvert"
