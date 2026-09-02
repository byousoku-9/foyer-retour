from __future__ import annotations

import time

from server.app.domain.trace import Usage
from server.app.llm.budget import RequestBudget


def test_budget_starts_clean() -> None:
    b = RequestBudget(deadline_s=55, max_attempts=4, max_cost_eur=0.10)
    assert b.attempts == 0 and b.cost_eur == 0.0
    assert 54 < b.remaining() <= 55


def test_remaining_is_monotonic_and_can_go_negative() -> None:
    b = RequestBudget(deadline_s=0.01, max_attempts=1, max_cost_eur=1.0)
    first = b.remaining()
    time.sleep(0.02)
    assert b.remaining() < first
    assert b.remaining() < 0


def test_timeout_for_call_is_min_of_timeout_and_remaining() -> None:
    b = RequestBudget(deadline_s=10, max_attempts=1, max_cost_eur=1.0)
    assert b.timeout_for_call(25.0) <= 10
    assert b.timeout_for_call(0.5) == 0.5


def test_note_call_accumulates_cost_rounded() -> None:
    b = RequestBudget(deadline_s=10, max_attempts=4, max_cost_eur=1.0)
    b.note_call(Usage(cost_eur=0.0101))
    b.note_call(Usage(cost_eur=0.0202))
    assert b.cost_eur == 0.0303
    assert b.attempts == 0  # les attempts sont comptés à l'envoi par le client, pas ici


def test_prefix_tracking_starts_empty_and_remembers_digests() -> None:
    # Story 1.4 (reprise B5) : le budget mémorise les empreintes de préfixes déjà écrits dans la requête.
    b = RequestBudget(deadline_s=10, max_attempts=4, max_cost_eur=1.0)
    assert not b.prefix_seen("abc")
    b.note_prefix("abc")
    assert b.prefix_seen("abc")
    assert not b.prefix_seen("def")
    b2 = RequestBudget(deadline_s=10, max_attempts=4, max_cost_eur=1.0)
    assert not b2.prefix_seen("abc")  # jamais partagé entre requêtes


# --- les bornes remesurées du tour « budgets Sonnet » (02/09/2026) ------------------------------
def test_le_plafond_dappels_laisse_sa_place_au_retry_de_la_sequence_la_plus_longue() -> None:
    """`max_llm_attempts` doit couvrir la séquence la plus longue **plus** le retry motivé d'AD-16.

    Chaque terme est lu là où le produit le décide, jamais recopié : *comprendre* (1), la navigation
    d'AD-1 (`max_llm_turns`), *rédiger* et *vérifier* (2), la relance d'AD-3
    (`pipelines/commun.APPELS_DE_LA_RELANCE`), la reprise de 4.2e
    (`pipelines/sinistre.APPELS_DE_LA_REPRISE`), et le **seul** retry qu'AD-16 accorde à un parse
    invalide. À 8, la somme valait exactement le plafond : le premier parse invalide de la chaîne
    ressortait en `BudgetExceeded` terminal sur un chemin conforme — les deux pré-contrôles
    (`budget.attempts + APPELS_DE_LA_… > budget.max_attempts`) n'avaient plus rien à arbitrer.

    Ce témoin rougit aussi si l'un des termes grandit : c'est ce qu'on veut, chacun vit dans un
    module différent et aucun ne sait ce que les autres consomment.
    """
    from server.app.config import Settings
    from server.app.pipelines.commun import APPELS_DE_LA_RELANCE
    from server.app.pipelines.sinistre import APPELS_DE_LA_REPRISE

    settings = Settings(_env_file=None, anthropic_api_key="")
    RETRY_DE_PARSE = 1  # AD-16 : « 1 retry », et un seul
    sequence_la_plus_longue = (1 + settings.max_llm_turns + 2
                               + APPELS_DE_LA_RELANCE + APPELS_DE_LA_REPRISE)
    assert sequence_la_plus_longue == 8, sequence_la_plus_longue
    assert settings.max_llm_attempts >= sequence_la_plus_longue + RETRY_DE_PARSE, (
        f"{settings.max_llm_attempts} appels pour une séquence de {sequence_la_plus_longue} : "
        "un parse invalide n'a plus de place et devient terminal")
    # …et pas davantage : une unité de plus autoriserait un **second** retry, donc une boucle.
    assert settings.max_llm_attempts == sequence_la_plus_longue + RETRY_DE_PARSE


def test_lalerte_de_cout_ne_se_leve_plus_sur_une_chaine_ordinaire() -> None:
    """`cout_eleve` est de l'observabilité (AD-10) : il doit désigner l'anormal, pas tout le monde.

    Bornes mesurées le 02/09/2026 sur la chaîne sinistre servie (usages enregistrés rejoués au tarif
    du tier servi) : 0,0548 € engagés avant *rédiger*, 0,2295 € pour la séquence la plus longue.
    À 0,05 € l'alerte se levait donc au troisième appel de **toutes** les requêtes. Elle doit se
    situer au-dessus de ce qu'une chaîne enregistrée a jamais coûté, et sous le plafond qui refuse.
    """
    from server.app.config import Settings

    settings = Settings(_env_file=None, anthropic_api_key="")
    ENGAGE_AVANT_REDIGER = 0.0548  # mesuré : *comprendre* + les deux tours de navigation
    CHAINE_LA_PLUS_LONGUE = 0.2295  # mesuré : les huit appels, sorties enregistrées
    assert settings.cost_alert_eur > CHAINE_LA_PLUS_LONGUE > ENGAGE_AVANT_REDIGER, (
        f"cost_alert_eur {settings.cost_alert_eur} € se lève sur une chaîne ordinaire")
    assert settings.cost_alert_eur < settings.max_cost_eur_per_request, (
        "une alerte qui ne précède pas le refus n'annonce rien")


def test_les_deux_plafonds_devals_laissent_partir_un_gate_vertical_repete() -> None:
    """Le budget qu'un run confronte à son majorant est `min(--max-cost, LIVE_BUDGET_EUR)`.

    Relever `evals_max_cost_eur` seul est donc **inopérant** : c'est le plus petit des deux qui
    décide. Le majorant d'une campagne est `exécutions × max_cost_eur_per_request`
    (`llm/pricing.estimate_run_majorant`), si bien que relever le plafond par requête resserre
    mécaniquement ce que les campagnes peuvent faire — c'est cet enchaînement-là que ce témoin tient.
    """
    from server.app.config import Settings
    from server.app.llm.pricing import estimate_run_majorant

    settings = Settings(_env_file=None, anthropic_api_key="")
    CAS_VERTICAUX = 5  # `server/evals/cases/**` : guide 1, sinistre 1, baloise 3
    REPETITIONS = 3    # la répétition minimale d'un gate chiffré (AD-14)
    majorant = estimate_run_majorant(CAS_VERTICAUX * REPETITIONS, settings)
    budget_effectif = min(settings.evals_max_cost_eur, settings.live_budget_eur)
    assert budget_effectif >= majorant, (
        f"un gate vertical à --repeat {REPETITIONS} est refusé avant son premier appel : "
        f"majorant {majorant:.2f} € contre un budget effectif de {budget_effectif:.2f} €")
    # Et il reste un plafond, pas une autorisation : le profil `full` continue d'exiger `--max-cost`.
    CAS_FULL = 56
    assert budget_effectif < estimate_run_majorant(CAS_FULL, settings)
