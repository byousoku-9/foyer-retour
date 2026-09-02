from __future__ import annotations

import time

import pytest

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
def _cas_payants_par_profil() -> tuple[int, int]:
    """Les cas payants d'un run `vertical` et d'un run `full`, lus sur `server/evals/cases/**`.

    Deux règles du produit, appliquées telles quelles : un run `vertical` ne retient que les cas de
    ce profil (`evals/run.py`, sélection des cas), un run `full` les retient tous ; et seuls les cas
    dont la suite n'est pas `parsing` sont **payants** (`executions_payantes`, même fichier). Un
    compte écrit à la main ici périmerait au premier cas ajouté — et il l'était : « 56 » ne
    correspondait à aucun compte réel, ni aux fichiers, ni aux exécutions payantes d'un run `full`,
    qui sont **52** au moment où cette ligne est écrite. Et cette fonction, écrite pour cela, n'était
    appelée par personne : le littéral périmé vivait à côté d'elle.
    """
    import re
    from pathlib import Path

    racine = Path(__file__).resolve().parents[1] / "server" / "evals" / "cases"
    verticaux = full = 0
    for chemin in racine.rglob("*.yaml"):
        texte = chemin.read_text("utf-8")
        profil = re.search(r"^\s*profile:\s*(\w+)", texte, re.M)
        suite = re.search(r"^\s*suite:\s*(\w+)", texte, re.M)
        if suite is not None and suite.group(1) == "parsing":
            continue  # jamais payant
        full += 1
        if profil is not None and profil.group(1) == "vertical":
            verticaux += 1
    assert verticaux and full, f"aucun cas lu sous {racine}"
    return verticaux, full


def test_le_plafond_dappels_laisse_sa_place_au_retry_de_la_sequence_la_plus_longue() -> None:
    """`max_llm_attempts` doit couvrir la séquence la plus longue **plus** le retry motivé d'AD-16.

    Chaque terme est lu là où le produit le décide, jamais recopié : *comprendre* (1), la navigation
    d'AD-1 (`max_llm_turns`), *rédiger* et *vérifier* (2), la relance d'AD-3
    (`pipelines/commun.APPELS_DE_LA_RELANCE`), la reprise de 4.2e
    (`pipelines/sinistre.APPELS_DE_LA_REPRISE`), et le **seul** retry qu'AD-16 accorde à un parse
    invalide. Quand la somme vaut exactement le plafond, le premier parse invalide de la chaîne
    ressort en `BudgetExceeded` terminal sur un chemin conforme — les deux pré-contrôles
    (`budget.attempts + APPELS_DE_LA_… > budget.max_attempts`) n'ont plus rien à arbitrer. La
    séquence est passée de 8 à 9 au correctif du tour 2 : le troisième tour de navigation est celui
    de la conclusion, sans lequel aucun verdict de suffisance n'est atteignable.

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
    assert sequence_la_plus_longue == 9, sequence_la_plus_longue
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
    # **Les deux comptes sont lus sur `server/evals/cases/**`, jamais recopiés.** `_cas_payants_par_
    # profil()` existait déjà pour cela et n'était appelée nulle part ; le `CAS_FULL = 56` écrit à
    # côté était faux — le vrai compte payant d'un run `full` est 52 — et il aurait continué de
    # dériver à chaque cas ajouté. Un test de budget qui se trompe de volume ne mesure rien.
    cas_verticaux, cas_full = _cas_payants_par_profil()
    REPETITIONS = 3    # la répétition minimale d'un gate chiffré (AD-14)
    majorant = estimate_run_majorant(cas_verticaux * REPETITIONS, settings)
    budget_effectif = min(settings.evals_max_cost_eur, settings.live_budget_eur)
    assert budget_effectif >= majorant, (
        f"un gate vertical à --repeat {REPETITIONS} est refusé avant son premier appel : "
        f"majorant {majorant:.2f} € contre un budget effectif de {budget_effectif:.2f} €")
    # Et il reste un plafond, pas une autorisation : le profil `full` continue d'exiger `--max-cost`.
    assert budget_effectif < estimate_run_majorant(cas_full, settings)


def test_la_deadline_couvre_la_queue_mesuree_du_chemin_nominal() -> None:
    """La deadline doit couvrir le pire nominal **corrigé de la dispersion**, pas le pire observé.

    Deux mécanismes rendent un `Timeout` **terminal** (503, `HTTP_STATUS[ErrorCode.timeout]`) sur une
    question nominale, et aucun n'est enveloppé d'un `except` : le contrôle `budget.remaining() <= 0`
    posé avant chacune des cinq étapes (`pipelines/sinistre.py`, `pipelines/guide.py`) et le
    `timeout_for_call() = min(llm_timeout_s, remaining())` que le SDK reçoit (`llm/budget.py`). La
    deadline n'est donc pas un confort d'exploitation : c'est ce qui décide si une requête lente
    aboutit ou tombe en 503.

    Les termes ci-dessous sont **mesurés** (02/09/2026, `docs/tests-live.md`), pas choisis :
    la charge de sortie du pire chemin nominal observé sur les 108 réponses Sonnet enregistrées, le
    taux le plus lent relevé sur *rédiger* — la seule étape que le projet ait chronométrée —, et le
    facteur de dispersion de ce même *rédiger*, qui a franchi 25 s deux fois sur six pour un maximum
    typique de 17,6 s.

    Ce témoin rougit si la deadline redescend sous cette queue, et il rougit aussi si l'un des
    termes grandit : c'est ce qu'on veut, ils bougeront avec le modèle servi.
    """
    from server.app.config import Settings

    TOKENS_PIRE_NOMINAL = 2_939  # comprendre 220 + retrouver 195×2 + rédiger 1 509 + vérifier 820
    TAUX_LE_PLUS_LENT_S = 17.6 / 1_130  # *rédiger* : 17,6 s pour 1 130 tokens de sortie
    DISPERSION = 25.0 / 17.6  # le même appel a franchi 25 s deux fois sur six
    ETABLISSEMENT_S = 0.5 * 5  # cinq connexions

    queue_mesuree = TOKENS_PIRE_NOMINAL * TAUX_LE_PLUS_LENT_S * DISPERSION + ETABLISSEMENT_S
    settings = Settings(_env_file=None, anthropic_api_key="")
    assert settings.deadline_s >= queue_mesuree, (
        f"deadline {settings.deadline_s} s sous la queue mesurée du chemin nominal "
        f"({queue_mesuree:.1f} s) : un `Timeout` terminal reste atteignable sur une question nominale")
    # Le budget d'un appel reste borné par le sien, et la marge de relance sous la deadline.
    assert settings.llm_timeout_s < settings.deadline_s
    assert settings.llm_retry_margin_s < settings.deadline_s


def test_relever_la_deadline_ne_rallonge_aucune_requete() -> None:
    """Une deadline est un **budget**, jamais une attente : rien ne patiente qu'elle s'écoule.

    `RequestBudget.remaining()` décroît avec l'horloge et n'est lu que pour **refuser** — avant une
    étape, avant une relance, et pour borner le timeout que le SDK reçoit. Un budget plus large ne
    peut donc qu'autoriser ce qu'un budget étroit refusait ; il ne peut rien ralentir. On le montre
    sur la seule surface qui traduit la deadline en comportement : le timeout d'appel.
    """
    from server.app.config import Settings

    settings = Settings(_env_file=None, anthropic_api_key="")
    ANCIENNE = 55.0
    # Au départ, les deux accordent exactement le même temps à un appel : le plafond par appel.
    for deadline in (ANCIENNE, settings.deadline_s):
        budget = RequestBudget(deadline_s=deadline, max_attempts=settings.max_llm_attempts,
                               max_cost_eur=1.0)
        # `approx` et non l'égalité : depuis le correctif du tour 3, `llm_timeout_s` vaut 55, soit
        # exactement l'ancienne deadline comparée ici — l'horloge monotone a déjà avancé de
        # quelques microsecondes quand le budget est construit. Ce que le témoin tient est que le
        # plafond par appel est le même des deux côtés, pas la milliseconde.
        assert budget.timeout_for_call(settings.llm_timeout_s) == pytest.approx(
            settings.llm_timeout_s, abs=0.01)

    # Et à temps écoulé égal, le budget large n'accorde jamais **moins** que l'étroit : le seul
    # effet d'une deadline plus haute est de laisser aboutir ce que l'autre aurait coupé.
    for consomme in (0.0, 20.0, 45.0, 54.0, 60.0):
        etroit = RequestBudget(deadline_s=ANCIENNE, max_attempts=settings.max_llm_attempts,
                               max_cost_eur=1.0)
        large = RequestBudget(deadline_s=settings.deadline_s,
                              max_attempts=settings.max_llm_attempts, max_cost_eur=1.0)
        etroit._t0 -= consomme  # type: ignore[attr-defined]
        large._t0 -= consomme   # type: ignore[attr-defined]
        assert large.timeout_for_call(settings.llm_timeout_s) >= \
            etroit.timeout_for_call(settings.llm_timeout_s)
    # À 60 s consommées, l'ancienne deadline refusait déjà (restant négatif) là où la nouvelle
    # accorde encore un appel entier : c'est exactement le 503 nominal que le relèvement supprime.
    epuise = RequestBudget(deadline_s=ANCIENNE, max_attempts=1, max_cost_eur=1.0)
    epuise._t0 -= 60.0  # type: ignore[attr-defined]
    tenu = RequestBudget(deadline_s=settings.deadline_s, max_attempts=1, max_cost_eur=1.0)
    tenu._t0 -= 60.0    # type: ignore[attr-defined]
    assert epuise.remaining() < 0 <= tenu.remaining()


def test_un_appel_qui_na_pas_le_temps_daboutir_nest_pas_envoye() -> None:
    """C2 — mesuré sur A16 : un appel envoyé avec 24,08 s pour une sortie qui en demande 45,66.

    `timeout_for_call` tronquait le délai au temps restant sans jamais demander si ce temps
    suffisait. L'appel ne pouvait pas aboutir : il a coûté 24 s, **zéro token**, zéro euro, et la
    totalité de la marge dont la remise de la réponse avait besoin.
    """
    from server.app.config import Settings
    from server.app.domain.errors import Timeout

    settings = Settings(_env_file=None, anthropic_api_key="")
    demande = settings.duree_majoree_pour(settings.verifier_sinistre_max_tokens)

    large = RequestBudget(deadline_s=demande + 10, max_attempts=4, max_cost_eur=1.0)
    large.exiger_le_temps_decrire(demande, etape="verifier")  # passe, rien n'est levé

    etroit = RequestBudget(deadline_s=demande - 10, max_attempts=4, max_cost_eur=1.0)
    with pytest.raises(Timeout, match="appel non envoyé"):
        etroit.exiger_le_temps_decrire(demande, etape="verifier")
    # Rien n'a été envoyé : ni appel compté, ni euro engagé.
    assert etroit.attempts == 0 and etroit.cost_eur == 0.0


def test_la_validation_de_configuration_et_lexecution_lisent_la_meme_derivation() -> None:
    """Une seule dérivation, trois lecteurs : configuration, budget de requête, gardes de cycle.

    Trois copies auraient divergé — et c'est exactement ce qui s'était produit entre le délai
    d'appel et la marge de relance, un nombre fixe sans rapport avec ce que le cycle allait écrire.
    """
    from server.app.config import Settings

    settings = Settings(_env_file=None, anthropic_api_key="")
    for max_tokens in (256, 1024, 2048, 3456, 4096):
        attendu = max_tokens / settings.llm_output_tokens_per_s_min + settings.llm_latence_marge_s
        assert settings.duree_majoree_pour(max_tokens) == attendu
    # L'invariante de démarrage est cette même dérivation appliquée à la plus longue sortie.
    plus_longue = max(settings.verifier_sinistre_max_tokens, settings.verifier_max_tokens,
                      settings.rediger_max_tokens, settings.comprendre_max_tokens,
                      settings.retrouver_outils_max_tokens)
    assert settings.duree_majoree_pour(plus_longue) <= settings.llm_timeout_s
