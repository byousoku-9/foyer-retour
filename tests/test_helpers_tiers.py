"""Le témoin du contrôle d'étage : il suit la configuration, et il mord quand l'étage diverge.

Un helper d'oracle qui ne serait pas lui-même sous témoin verdirait n'importe quoi — et c'est
précisément le défaut qu'il remplace (un tier recopié en littéral, vert par hasard). Les trois
propriétés à tenir sont donc épinglées ici, hors réseau et sans fixture :

1. sur un `StepTrace` conforme, le contrôle passe **quelle que soit** la valeur configurée ;
2. quand l'appel porte le modèle d'un autre tier, il échoue en nommant le tier attendu et le modèle
   observé — le message est la moitié utile de l'oracle ;
3. sous une surcharge de tier par étape (story 4.2b), l'attente **suit la configuration** : le même
   `StepTrace` est accepté ou refusé selon ce que `Settings` sert, ce qu'aucune constante ne peut
   faire.
"""

from __future__ import annotations

from typing import get_args

import pytest

from server.app.config import Settings
from server.app.domain.trace import LLMCall, StepTrace
from server.app.llm.models import STEP_TIERS, TIERS
from tests.helpers_tiers import modele_attendu, tier_attendu, verifier_etage


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _step(etape: str, tier: str | None, *, modele: str | None, appels: int = 1) -> StepTrace:
    """Une `StepTrace` fabriquée : le tier publié et le modèle appelé sont choisis par le test."""
    calls = [] if modele is None else [LLMCall(model=modele, tier=tier) for _ in range(appels)]
    return StepTrace(name=etape, tier=tier, calls=calls)


def _autre_tier(tier: str) -> str:
    """Un tier de `TIERS` qui n'est pas celui-là — lu sur la table, jamais nommé en littéral."""
    return next(t for t in TIERS if t != tier)


def _autre_tier_configurable(champ: str, tier: str) -> str:
    """Un tier que `Settings` **accepte** pour ce champ et qui n'est pas celui-là.

    Lu sur l'annotation du champ, et non sur `TIERS` : la surcharge par étape de la story 4.2b n'ouvre
    pas l'étage d'ingestion, et un test qui l'écrirait en dur casserait au prochain réglage de la
    matrice baseline.
    """
    return next(t for t in get_args(Settings.model_fields[champ].annotation) if t != tier)


# Nom de la `StepTrace` publiée par chaque clef d'étape du helper : *retrouver* par outils publie
# `"retrouver"` mais lit la surcharge `retrouver_outils_tier` (le déterministe n'appelle personne).
ETAPES = {"comprendre": "comprendre", "rediger": "rediger", "verifier": "verifier",
          "retrouver_outils": "retrouver"}


@pytest.mark.parametrize("etape", sorted(ETAPES))
def test_letage_conforme_passe_pour_chaque_etape_appelante(etape: str) -> None:
    settings = _settings()
    assert tier_attendu(etape, settings) is not None
    step = _step(ETAPES[etape], tier_attendu(etape, settings),
                 modele=modele_attendu(etape, settings))
    verifier_etage(step, settings, etape=etape, appels=1)


def test_un_modele_dun_autre_tier_fait_echouer_le_controle_en_le_nommant() -> None:
    settings = _settings()
    attendu = settings.comprendre_tier
    intrus = _autre_tier(attendu)
    step = _step("comprendre", attendu, modele=TIERS[intrus])

    with pytest.raises(AssertionError) as echec:
        verifier_etage(step, settings)
    message = str(echec.value)
    assert repr(attendu) in message and TIERS[attendu] in message  # le tier et le modèle attendus
    assert TIERS[intrus] in message  # et le modèle réellement appelé


def test_un_tier_publie_divergent_fait_echouer_le_controle() -> None:
    settings = _settings()
    attendu = settings.comprendre_tier
    intrus = _autre_tier(attendu)
    step = _step("comprendre", intrus, modele=TIERS[attendu])

    with pytest.raises(AssertionError) as echec:
        verifier_etage(step, settings)
    assert repr(intrus) in str(echec.value) and repr(attendu) in str(echec.value)


def test_lattente_suit_la_configuration_et_non_une_constante() -> None:
    """La propriété qui distingue l'oracle du littéral : le **même** step change de verdict."""
    defaut = _settings()
    # `baseline_tiers=true` est ce que `config.py` exige pour descendre une étape protégée sous le
    # plancher `reason` : la surcharge est un mode de mesure, et le test l'emprunte tel quel.
    surcharge = _settings(
        baseline_tiers=True,
        comprendre_tier=_autre_tier_configurable("comprendre_tier", defaut.comprendre_tier))
    assert surcharge.comprendre_tier != defaut.comprendre_tier

    tel_que_configure = _step("comprendre", surcharge.comprendre_tier,
                              modele=TIERS[surcharge.comprendre_tier])
    verifier_etage(tel_que_configure, surcharge)  # l'attente a suivi la surcharge
    with pytest.raises(AssertionError):
        verifier_etage(tel_que_configure, defaut)  # et elle refuse l'ancien tier

    tel_quavant = _step("comprendre", defaut.comprendre_tier, modele=TIERS[defaut.comprendre_tier])
    verifier_etage(tel_quavant, defaut)
    with pytest.raises(AssertionError):
        verifier_etage(tel_quavant, surcharge)


def test_le_nombre_dappels_est_juge_quand_lac_en_depend() -> None:
    settings = _settings()
    step = _step("verifier", settings.verifier_tier,
                 modele=modele_attendu("verifier", settings), appels=2)
    verifier_etage(step, settings)  # les deux appels sont au bon étage…
    with pytest.raises(AssertionError, match="1 appel"):
        verifier_etage(step, settings, appels=1)  # … mais AD-9 amendé n'en veut qu'un


def test_une_etape_sans_appel_modele_attend_aucun_tier() -> None:
    settings = _settings()
    assert tier_attendu("restituer", settings) is STEP_TIERS["restituer"] is None
    verifier_etage(_step("restituer", None, modele=None), settings)
    with pytest.raises(ValueError, match="n'appelle aucun modèle"):
        modele_attendu("restituer", settings)


def test_une_etape_inconnue_ne_recoit_jamais_de_tier_par_defaut() -> None:
    with pytest.raises(ValueError, match="étape inconnue"):
        tier_attendu("etape_qui_nexiste_pas", _settings())
