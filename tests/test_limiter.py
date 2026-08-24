"""AD-13 — Le limiteur seul : identité réelle, fenêtres par instance, table bornée.

Il se teste sans HTTP parce qu'il est le garde-fou du budget : ce qu'il compte, quand il refuse et
ce qu'il annonce sont des propriétés de lui-même, pas de la route. Le temps est injecté
(`maintenant=`) — un limiteur qui ne se teste qu'en dormant 60 secondes ne se teste pas.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from server.app.api.limiter import ENTETE, IDENTITE_DEV, RateLimiter, identite_client
from server.app.config import Settings
from server.app.domain.errors import ErrorCode, InvalidRequest, PipelineError


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _requete(xff: str | None = None) -> Request:
    entetes = [] if xff is None else [(ENTETE.lower().encode(), xff.encode())]
    return Request({"type": "http", "method": "POST", "path": "/api/v1/chat", "headers": entetes})


# --- identité -------------------------------------------------------------

def test_ipv4_est_prise_entiere() -> None:
    assert identite_client(_requete("203.0.113.7"), env="prod") == "203.0.113.7"


def test_ipv6_est_tronquee_au_64() -> None:
    # AD-13 : un abonné se voit couramment déléguer un /64 entier ; compter par adresse lui
    # donnerait 2**64 identités, c'est-à-dire aucun quota.
    a = identite_client(_requete("2001:db8:abcd:0012::1"), env="prod")
    b = identite_client(_requete("2001:db8:abcd:0012:ffff:ffff:ffff:ffff"), env="prod")
    assert a == b == "2001:db8:abcd:12::/64"


def test_seul_le_premier_element_compte() -> None:
    # Les suivants sont ce que le client a bien voulu écrire : seul le premier vient du proxy.
    assert identite_client(_requete("203.0.113.7, 10.0.0.1, 192.168.1.1"), env="prod") == "203.0.113.7"


def test_le_port_est_retire() -> None:
    assert identite_client(_requete("203.0.113.7:41234"), env="prod") == "203.0.113.7"


def test_ipv6_entre_crochets_avec_port_est_acceptee() -> None:
    # Forme RFC 7239 : un proxy peut poser « [2001:db8::1]:41234 ». Les crochets ne font pas partie
    # de l'adresse ; sans les retirer, un client IPv6 légitime serait refusé en 400.
    assert identite_client(_requete("[2001:db8:abcd:12::1]:41234"), env="prod") == "2001:db8:abcd:12::/64"
    assert identite_client(_requete("[2001:db8:abcd:12::1]"), env="prod") == "2001:db8:abcd:12::/64"


def test_absent_en_dev_donne_une_identite_locale() -> None:
    assert identite_client(_requete(None), env="dev") == IDENTITE_DEV


def test_absent_hors_dev_est_refuse() -> None:
    with pytest.raises(InvalidRequest):
        identite_client(_requete(None), env="prod")


def test_valeur_qui_nest_pas_une_ip_est_refusee() -> None:
    # Sinon chaque chaîne inventée serait une identité neuve, donc un quota neuf.
    with pytest.raises(InvalidRequest):
        identite_client(_requete("pas-une-adresse"), env="prod")


# --- fenêtres -------------------------------------------------------------

def test_la_fenetre_minute_laisse_passer_le_quota_puis_refuse() -> None:
    limiteur = RateLimiter(_settings(env="prod", rate_limit_per_minute=3, rate_limit_per_day=100))
    requete = _requete("203.0.113.7")
    for i in range(3):
        assert limiteur.check(requete, maintenant=1000.0 + i) == "203.0.113.7"
    with pytest.raises(PipelineError) as exc:
        limiteur.check(requete, maintenant=1003.0)
    assert exc.value.code is ErrorCode.rate_limited
    assert exc.value.retry_after_s >= 1


def test_la_fenetre_minute_retombe() -> None:
    limiteur = RateLimiter(_settings(env="prod", rate_limit_per_minute=2))
    requete = _requete("203.0.113.7")
    limiteur.check(requete, maintenant=0.0)
    limiteur.check(requete, maintenant=1.0)
    with pytest.raises(PipelineError):
        limiteur.check(requete, maintenant=2.0)
    limiteur.check(requete, maintenant=61.0)  # nouvelle fenêtre : le compteur repart


def test_deux_identites_ne_se_partagent_pas_le_quota() -> None:
    limiteur = RateLimiter(_settings(env="prod", rate_limit_per_minute=1))
    limiteur.check(_requete("203.0.113.7"), maintenant=0.0)
    limiteur.check(_requete("198.51.100.4"), maintenant=0.0)  # ne lève pas
    with pytest.raises(PipelineError):
        limiteur.check(_requete("203.0.113.7"), maintenant=0.0)


def test_la_fenetre_jour_refuse_meme_en_dessous_de_la_minute() -> None:
    limiteur = RateLimiter(_settings(env="prod", rate_limit_per_minute=100, rate_limit_per_day=3))
    requete = _requete("203.0.113.7")
    for i in range(3):
        limiteur.check(requete, maintenant=float(i * 120))
    with pytest.raises(PipelineError) as exc:
        limiteur.check(requete, maintenant=400.0)
    assert "jour" in exc.value.message


def test_une_requete_refusee_par_la_minute_compte_quand_meme_dans_la_journee() -> None:
    # Sinon marteler le serveur une fois la minute pleine coûterait zéro quota journalier.
    limiteur = RateLimiter(_settings(env="prod", rate_limit_per_minute=1, rate_limit_per_day=3))
    requete = _requete("203.0.113.7")
    limiteur.check(requete, maintenant=0.0)
    for t in (1.0, 2.0):  # refusées par la minute, comptées par le jour
        with pytest.raises(PipelineError):
            limiteur.check(requete, maintenant=t)
    with pytest.raises(PipelineError) as exc:
        limiteur.check(requete, maintenant=120.0)  # minute retombée, journée pleine
    assert "jour" in exc.value.message


def test_retry_after_est_borne_par_le_reglage() -> None:
    # Le reste de la fenêtre journalière se compte en heures : l'annoncer n'aiderait personne.
    limiteur = RateLimiter(_settings(env="prod", rate_limit_per_minute=100, rate_limit_per_day=1,
                                     retry_after_s=45))
    requete = _requete("203.0.113.7")
    limiteur.check(requete, maintenant=0.0)
    with pytest.raises(PipelineError) as exc:
        limiteur.check(requete, maintenant=1.0)
    assert exc.value.retry_after_s == 45


def test_retry_after_suit_le_reste_de_la_fenetre_quand_il_est_plus_court() -> None:
    limiteur = RateLimiter(_settings(env="prod", rate_limit_per_minute=1, retry_after_s=600))
    requete = _requete("203.0.113.7")
    limiteur.check(requete, maintenant=0.0)
    with pytest.raises(PipelineError) as exc:
        limiteur.check(requete, maintenant=50.0)
    assert exc.value.retry_after_s == 10


# --- table bornée ---------------------------------------------------------

def test_la_table_des_clients_est_bornee_et_evince_les_plus_anciens() -> None:
    limiteur = RateLimiter(_settings(env="prod", rate_limit_max_clients=3, rate_limit_per_minute=1))
    for i in range(5):
        limiteur.check(_requete(f"203.0.113.{i}"), maintenant=0.0)
    assert len(limiteur) == 3
    # Le plus ancien a été évincé : il repart à zéro (le « best-effort » d'AD-13, écrit).
    limiteur.check(_requete("203.0.113.0"), maintenant=0.0)


def test_une_identite_revue_remonte_en_tete_et_nest_pas_evincee() -> None:
    limiteur = RateLimiter(_settings(env="prod", rate_limit_max_clients=2, rate_limit_per_minute=10))
    fidele = _requete("203.0.113.1")
    limiteur.check(fidele, maintenant=0.0)
    limiteur.check(_requete("203.0.113.2"), maintenant=0.0)
    limiteur.check(fidele, maintenant=1.0)          # remonte en tête
    limiteur.check(_requete("203.0.113.3"), maintenant=2.0)  # évince 203.0.113.2
    # Discriminant : `fidele` a déjà été compté **deux** fois. Il lui reste donc 8 appels avant le
    # refus. Si l'éviction l'avait touché, son compteur serait reparti de zéro et le 9ᵉ passerait.
    for _ in range(8):
        limiteur.check(fidele, maintenant=3.0)
    with pytest.raises(PipelineError):
        limiteur.check(fidele, maintenant=3.0)
