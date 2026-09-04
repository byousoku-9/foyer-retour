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

    Chaque terme est lu là où le produit le décide, jamais recopié : *comprendre* (1), les tours de
    navigation du chemin **servi** (`navigation_max_llm_turns`, amendement AD-1 du 03/09/2026),
    l'ébauche terminale rendue dans la même conversation et *vérifier* (2), la relance d'AD-3
    (`pipelines/commun.APPELS_DE_LA_RELANCE`), la reprise de 4.2e
    (`pipelines/sinistre.APPELS_DE_LA_REPRISE`), et le **seul** retry qu'AD-16 accorde à un parse
    invalide. Quand la somme vaut exactement le plafond, le premier parse invalide de la chaîne
    ressort en `BudgetExceeded` terminal sur un chemin conforme — les deux pré-contrôles
    (`budget.attempts + APPELS_DE_LA_… > budget.max_attempts`) n'ont plus rien à arbitrer. La
    séquence est passée de 9 à 14 avec la navigation par le modèle : ses tours remplacent les trois
    de la variante `outils`, et la rédaction se fait dans le même fil.

    Ce témoin rougit aussi si l'un des termes grandit : c'est ce qu'on veut, chacun vit dans un
    module différent et aucun ne sait ce que les autres consomment.
    """
    from server.app.config import Settings
    from server.app.pipelines.commun import APPELS_DE_LA_RELANCE
    from server.app.pipelines.sinistre import APPELS_DE_LA_REPRISE

    settings = Settings(_env_file=None, anthropic_api_key="")
    RETRY_DE_PARSE = 1  # AD-16 : « 1 retry », et un seul
    sequence_la_plus_longue = (1 + settings.navigation_max_llm_turns + 2
                               + APPELS_DE_LA_RELANCE + APPELS_DE_LA_REPRISE)
    assert sequence_la_plus_longue == 14, sequence_la_plus_longue
    assert settings.max_llm_attempts >= sequence_la_plus_longue + RETRY_DE_PARSE, (
        f"{settings.max_llm_attempts} appels pour une séquence de {sequence_la_plus_longue} : "
        "un parse invalide n'a plus de place et devient terminal")
    # …et pas davantage : une unité de plus autoriserait un **second** retry, donc une boucle.
    assert settings.max_llm_attempts == sequence_la_plus_longue + RETRY_DE_PARSE


def test_lalerte_de_cout_ne_se_leve_plus_sur_une_chaine_ordinaire() -> None:
    """`cout_eleve` est de l'observabilité (AD-10) : il doit désigner l'anormal, pas tout le monde.

    **Re-mesuré le 03/09/2026 (T7), sur le chemin froid.** Les bornes du 02/09 (0,0548 € avant
    *rédiger*, 0,2295 € pour la séquence la plus longue) datent d'une chaîne dont la lecture était
    faite par du code. Depuis l'amendement AD-1, le premier tour de navigation écrit le sommaire
    complet dans le préfixe cacheable : le run réel du cas bougie a engagé **0,5557 €** avant
    *vérifier*, et l'appel qui suivait était majoré à 0,1979 € — la chaîne froide coûte donc au plus
    0,7536 €, et 0,9279 € quand la relance d'AD-3 s'y ajoute (ses deux appels, préfixes chauds, sont
    majorés à 0,1743 €). À 0,25 € l'alerte se lèverait au premier tour de **toute** requête froide.
    Elle doit se situer au-dessus de ce qu'une chaîne mesurée a jamais coûté, et sous le plafond.
    """
    from server.app.config import Settings

    settings = Settings(_env_file=None, anthropic_api_key="")
    ENGAGE_AVANT_VERIFIER = 0.5557    # mesuré le 03/09 à 10 h 05, cas bougie, cache froid
    CHAINE_FROIDE_COMPLETE = 0.9279   # + le majorant de *vérifier* (0,1979) et la relance (0,1743)
    assert settings.cost_alert_eur > CHAINE_FROIDE_COMPLETE > ENGAGE_AVANT_VERIFIER, (
        f"cost_alert_eur {settings.cost_alert_eur} € se lève sur une chaîne ordinaire à cache froid")
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
    # 250 depuis T1d : c'est la deadline d'avant ce tour, et il faut qu'elle **dépasse** le plafond
    # par appel (78 s) pour que les deux côtés accordent bien le même temps. 55 était l'ancienne
    # valeur de `llm_timeout_s`, pas une deadline, et elle bornait désormais le côté « étroit ».
    ANCIENNE = 250.0
    # Au départ, les deux accordent exactement le même temps à un appel : le plafond par appel.
    for deadline in (ANCIENNE, settings.deadline_s):
        budget = RequestBudget(deadline_s=deadline, max_attempts=settings.max_llm_attempts,
                               max_cost_eur=1.0)
        # `approx` et non l'égalité : l'horloge monotone a déjà avancé de quelques microsecondes
        # quand le budget est construit. Ce que le témoin tient est que le plafond par appel est le
        # même des deux côtés, pas la milliseconde.
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
    # À 60 s consommées, une deadline courte refuse déjà (restant négatif) là où la servie accorde
    # encore un appel entier : c'est exactement le 503 nominal que le relèvement supprime. La valeur
    # est **écrite ici** et non reprise d'`ANCIENNE` depuis T1d : les deux rôles ont divergé —
    # `ANCIENNE` doit dépasser le plafond par appel (78 s) pour que la boucle ci-dessus compare des
    # timeouts égaux, celle-ci doit être épuisée par 60 s consommées.
    COURTE = 55.0
    epuise = RequestBudget(deadline_s=COURTE, max_attempts=1, max_cost_eur=1.0)
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


def test_le_plafond_dun_appel_ne_coupe_plus_une_requete_que_la_deadline_permettait() -> None:
    """L1l — le rejeu du 04/09/2026 08 h 45, rejoué sur l'horloge et sans réseau.

    Chiffres du rejeu (`proto/g-partir-l1j.json`) : `llm_timeout_s = 78`, l'appel de *vérifier* les
    franchit, et il restait **112,2 s de deadline**. La requête sort en 503 `timeout` sur un plafond
    qui garde contre un appel **pendu**, pas contre un appel long — trois étapes sur quatre avaient
    abouti, et la personne n'a rien reçu.

    Les quatre bords de la dérivation, dans l'ordre où ils mordent.
    """
    from server.app.config import Settings

    settings = Settings(_env_file=None, anthropic_api_key="")
    borne, marge = settings.llm_timeout_s, settings.llm_latence_marge_s
    facteur = settings.llm_timeout_facteur
    assert facteur > 1.0  # sans quoi le témoin ne prouverait rien du cas servi

    def budget(restant: float) -> RequestBudget:
        b = RequestBudget(deadline_s=restant, max_attempts=4, max_cost_eur=1.0)
        return b

    # (a) Le cas du rejeu : 112,2 s restantes, la borne étire jusqu'à `restant - marge`.
    delai = budget(112.2).timeout_for_call(borne, facteur=facteur, marge=marge)
    assert delai == pytest.approx(112.2 - marge, abs=0.2)
    assert delai > borne  # ce que L1j n'avait pas, et qui lui aurait servi la réponse

    # (b) Le facteur est un plafond, pas une licence : au large, il borne l'étirement.
    delai = budget(borne * facteur * 4).timeout_for_call(borne, facteur=facteur, marge=marge)
    assert delai == pytest.approx(borne * facteur, abs=0.2)

    # (c) L'étirement ne **raccourcit** jamais : à peine plus de marge que de temps restant, le
    #     délai reste celui d'avant L1l — un facteur qui rendrait moins que sa propre borne serait
    #     devenu un plafond plus court que celui qu'il étire.
    restant = marge + 1.0
    delai = budget(restant).timeout_for_call(borne, facteur=facteur, marge=marge)
    assert delai == pytest.approx(min(borne, restant), abs=0.2)

    # (d) L'étirement ne **dépasse** jamais la deadline : c'est elle qui coupe, avec l'erreur
    #     existante (AD-16), et le résultat retombe exactement sur `min(borne, restant)`.
    for restant in (borne / 2, borne - 1.0):
        delai = budget(restant).timeout_for_call(borne, facteur=facteur, marge=marge)
        assert delai == pytest.approx(restant, abs=0.2)
        assert delai <= restant

    # (e) `facteur = 1.0` rend la formule d'avant, à l'identique, quel que soit le temps restant :
    #     c'est par cette valeur, et par elle seule, que la généralisation L1x se désarme.
    for restant in (5.0, borne, 300.0):
        b = budget(restant)
        assert (b.timeout_for_call(borne, facteur=1.0, marge=marge)
                == pytest.approx(b.timeout_for_call(borne), abs=0.2))
