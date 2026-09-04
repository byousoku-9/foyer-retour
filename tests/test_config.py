from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.app.config import (RAISON_PUBLIABLE_MAX_DEFAULT, REPO_ROOT, SEUILS_DE_GATE,
                               SEUILS_DEXPLOITATION, Settings)
from server.app.domain.trace import Trace

THRESHOLD_VARS = [k.upper() for k in Settings.model_fields] + ["ENV", "ALLOW_UNGATED"]


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in THRESHOLD_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_match_spine_hypotheses() -> None:
    s = Settings(_env_file=None)
    # `deadline_s` : 250 depuis la story 5.6 (T1c, 03/09/2026) — la navigation par le modèle
    # (AD-1 amendé, 6 à 8 tours, *rédiger* fusionné) majore à 240,8 s le pire chemin nominal, une
    # fois la dérivation refaite sur le pipeline intégré (T1b) et sur le vérificateur revenu à
    # l'effort `medium` (T1c). 165 s le laissait sortir en 503 dès la relance d'AD-3.
    # `llm_timeout_s` : 55 depuis le correctif du tour 3, et **inchangé** par 5.6 — il borne un
    # appel, pas la chaîne, et la plus longue sortie d'étape (4 096 tokens depuis T1c) demande
    # 53,2 s, soit 3,3 % sous le délai : c'est `_coherence` qui tient cette marge-là. À 40, le
    # plafond de sortie du vérificateur sinistre était inatteignable dans le temps qu'on lui
    # laissait, et une réponse valide mourait sur son délai d'appel.
    # `client_abort_margin_s` : 65 depuis T1c — l'ordre d'AD-11 (client 315 s > Cloud Run 300 s >
    # serveur 250 s) la dicte ; elle ne se choisit plus « un peu au-dessus de la deadline », elle est
    # le **reste** entre la patience du client et la deadline serveur.
    assert s.deadline_s == 290 and s.llm_timeout_s == 78
    assert s.client_abort_margin_s == 25
    assert s.deadline_s + s.client_abort_margin_s == 315
    assert s.raison_publiable_max_chars == RAISON_PUBLIABLE_MAX_DEFAULT == 500
    assert s.quote_min_chars == 25 and s.quote_min_ratio == 0.6
    assert s.max_opens == 6 and s.node_window == 30 and s.search_limit == 20
    # `variante_nombre_max_part` : 0,01 depuis l'amendement AD-1 du 03/09/2026 (tâche T2), qui
    # renomme `facette_variante_max_part` sans toucher à sa valeur ni à son raisonnement — il ne
    # borne plus une sous-question mais l'élargissement de la **requête** de l'outil `chercher`.
    # 1 % sépare, sur le contrat servi, les mots rares qui nomment une clause (`fumées` 0,07 %,
    # `bris` 0,57 %) de ceux que le document porte partout (`liés` 1,36 %, `dommages` 8,9 %).
    assert s.variante_nombre_max_part == 0.01
    # 15 depuis l'amendement AD-1 du 03/09/2026 : la séquence servie compte les tours de
    # navigation et l'ébauche rendue dans la même conversation (voir `tests/test_budget.py`).
    assert s.max_llm_attempts == 15 and s.retrouver_outils_max_tokens == 1024
    # Le chemin servi et ses trois bornes, mesurés sur le prototype du 03/09/2026.
    assert s.retrieval_variant == "navigation" and s.navigation_tier == "reason"
    assert s.navigation_max_llm_turns == 8 and s.navigation_budget_tokens == 12000
    assert s.navigation_search_limit == 20
    # Story 5.6 T1b : la place de l'ébauche de navigation, re-dérivée sur les trois réponses A16 du
    # pipeline intégré — et les champs `draft_*` qui doivent la couvrir, puisque la fusion de
    # relance de `pipelines/sinistre.py` s'y borne sans savoir quel étage a rédigé.
    assert s.navigation_draft_max_claims == 6 and s.navigation_draft_max_segments == 9
    # Story 5.6 T13 : re-dérivé sur le gate Baloise du 03/09 13 h 14 — 3 200 de contrat JSON
    # (pire mesuré 2 558, majoré de 25 %) et 1 856 de réserve, ce que la deadline laisse.
    assert s.navigation_rediger_max_tokens == 5056
    # Story 5.6 T11 : l'effort du **seul** tour terminal. Mesuré à 0 token de réflexion sur
    # les trois runs A16 de `f858a28`, alors que c'est l'unique appel qui choisit les
    # clauses citées. Les tours d'outils gardent le défaut de leur palier.
    # Repli du 03/09 12:11 : `high` a saturé 3 072 tokens de réflexion sans JSON (503) ; `medium`
    # est l'effort mesuré sans troncature sur la matinée. T13 : `medium` tronque aussi sur les
    # ébauches Baloise, et c'est le plafond qui a été relevé — la marche suivante reste `low`.
    # T14 : elle a été prise. `medium` a tronqué **aussi** sur le plafond relevé (gate Baloise
    # 13 h 43, `b-bougie-canape` rép. 3) ; `low` est le seul effort dont aucune mesure ne rend une
    # troncature, et le levier contre l'omission d'une clause lue est l'inventaire des blocs
    # décisionnels du message terminal (T11), pas l'effort de ce tour.
    assert s.navigation_draft_effort == "low"
    assert s.draft_max_claims == 6 and s.draft_max_segments == 9
    assert s.retrouver_outils_tier == "reason"
    assert (s.comprendre_tier, s.rediger_tier, s.verifier_tier) == (
        "reason", "reason", "reason")
    assert s.rediger_max_tokens == 2048
    assert "outils_rediger_max_tokens" not in Settings.model_fields
    assert s.max_cost_eur_per_request == 1.30 and s.cost_alert_eur == 1.00
    # story 1.10 : AD-9 remplace le plafond **par requête** par un plafond **par run** en évals ;
    # CLAUDE.md exige « la clé **et un plafond** ». `--max-cost` ne fait que surcharger celui-ci.
    assert s.evals_max_cost_eur == 20.0 and s.live_budget_eur == 20.0
    assert s.rate_limit_per_minute == 10 and s.rate_limit_per_day == 100
    assert s.coverage_threshold == 0.8 and s.kind_confidence_min == 0.7
    assert s.mixed_page_image_density == 0.2 and s.ocr_dpi == 300
    assert s.quality_min_words == 12 and s.foreign_signal_min == 3 and s.french_signal_ratio_min == 0.08
    assert s.gibberish_ratio_max == 0.35 and s.residual_header_min_pages_ratio == 0.3
    assert s.toc_page_number_baseline_pt == 8.0 and s.toc_column_tolerance_pt == 80.0
    assert s.toc_indent_tolerance_pt == 5.0 and s.toc_line_gap_ratio == 1.5
    assert s.toc_title_prefix_min_chars == 20
    assert s.dedent_tolerance_pt == 1.0 and s.dedent_starter_max_lines == 2
    assert s.env == "dev" and s.allow_ungated is True
    # story 1.5 : pipeline guide, historique borné (AD-11), bornes de *vérifier* (AD-4)
    assert s.guide_doc_id == "lux-guide" and s.historique_max_turns == 6
    assert s.verifier_max_claims == 8 and s.verifier_max_tokens == 6144
    # story 1.8 : contrat servi par le pipeline sinistre, et les bornes de son appel groupé
    assert s.sinistre_doc_id == "axa-lu-optihome-2017"
    # Correctif du tour 2 : la borne est **dérivée**, contrat JSON + réserve de réflexion. Elle
    # valait 3 072 de JSON sans un token pour la réflexion, alors que celle-ci est comptée dans le
    # même `max_tokens` et représente 55 à 91 % de la sortie mesurée. La somme atteint exactement
    # le plafond du client : le contrôle de cohérence mord, et c'est voulu.
    # Corrigé au tour 3 : le JSON réellement rendu vaut 329 à 510 tokens (2 048 majorait un contrat
    # que le sinistre ne produit pas), et la réflexion mesurée 2 394 — la réserve du tour 2 était
    # déjà dépassée quand elle a été écrite.
    # Re-dérivé le 03/09/2026 (T1c), les deux moitiés pour deux raisons distinctes : le contrat JSON
    # sur `navigation_draft_max_claims` (6 affirmations × 161 tokens, le pire mesuré, ≈ 968), la réserve sur
    # le retour de cet appel à l'effort `medium` du palier. La somme retrouve **exactement** le
    # plafond du client, comme au tour 2 : le contrôle de cohérence mord, et c'est voulu.
    # Re-dérivé le 03/09/2026 (T1d) sur ce que l'audit de T1c a mesuré — le contrat JSON sur les 27
    # appels non tronqués de T1b (droite affine, 791 à six affirmations ; enveloppe, 904), la
    # réserve sur les deux appels `medium` qui ont **saturé** leur plafond (mesure censurée à
    # 4 096, majorée de 25 %). La somme vaut de nouveau exactement le plafond du client, qui monte
    # avec elle : c'est ce qui force `llm_timeout_s` et `deadline_s` à être re-dérivées ensemble.
    assert s.verifier_sinistre_json_tokens == 1024
    assert s.verifier_thinking_reserve_tokens == 5120
    assert s.verifier_sinistre_max_tokens == 6144 <= s.llm_max_output_tokens
    assert s.fait_manquant_max_chars == 200 and s.ask_client_max == 8
    assert s.pdf_highlight_max_lines == 40 and s.pdf_highlight_max_blocks == 10
    assert s.pdf_render_concurrency == 2 and s.pdf_render_queue_timeout_s == 2.0
    assert s.pdf_render_cache_pages == 32 and s.pdf_render_dpi == 144
    assert s.pdf_render_max_pixels == 16_000_000
    # Revue 2.7 I2 : la longueur du contexte est bornée par son majorant de tokens ; le nombre de
    # blocs revient à sa valeur de rappel antérieure et ne sert plus de réglage de coût au cas par cas.
    assert s.retrieval_max_blocks == 30 and s.retrieval_max_tokens == 3500


def test_the_served_documents_of_the_defaults_exist_in_the_real_corpus() -> None:
    """Les deux `*_doc_id` par défaut désignent des documents que le corpus livré sert vraiment.

    Tous les tests de pipeline les surchargent par un corpus synthétique : une faute de frappe dans le
    défaut ne se verrait donc nulle part, et **toute** requête non paramétrée ressortirait en 503
    `corpus_unavailable` — en production d'abord (revue 1.8). Le manifeste suffit à le dire, sans
    charger les documents.
    """
    s = Settings(_env_file=None)
    manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_text("utf-8"))
    servis = set(manifest.get("documents", manifest))
    assert {s.guide_doc_id, s.sinistre_doc_id} <= servis, sorted(servis)


def test_thresholds_feed_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUOTE_MIN_CHARS", "30")
    monkeypatch.setenv("ALLOW_UNGATED", "false")
    s = Settings(_env_file=None)
    assert s.quote_min_chars == 30 and s.allow_ungated is False
    t = Trace(request_id="r", pipeline="guide", thresholds=s.thresholds())
    assert t.thresholds["quote_min_chars"] == 30
    assert t.thresholds["raison_publiable_max_chars"] == 500
    assert t.thresholds["max_cost_eur_per_request"] == 1.30
    assert {"max_opens", "node_window", "search_limit", "max_llm_attempts",
            "retrouver_outils_max_tokens", "max_cost_eur_per_request",
            "rate_limit_per_minute", "rate_limit_per_day", "deadline_s",
            # story 1.4 : plafonds de sortie par étape et borne en blocs de *retrouver*
            "comprendre_max_tokens", "rediger_max_tokens",
            "retrieval_max_blocks",
            # story 1.5 : bornes du pipeline et de *vérifier*
            "historique_max_turns", "verifier_max_claims", "verifier_max_tokens",
            # story 3.4 : rendu paresseux, concurrence, cache, résolution et lignes surlignées
            "pdf_highlight_max_lines", "pdf_highlight_max_blocks", "pdf_render_concurrency",
            "pdf_render_cache_pages", "pdf_render_dpi", "pdf_render_max_pixels",
            "pdf_render_queue_timeout_s",
            # story 1.8 : les deux bornes posées sur ce que le modèle fait afficher au sinistre
            "fait_manquant_max_chars", "ask_client_max",
            # story 1.10 : le plafond de coût d'un run d'évals (AD-9, AD-14)
            "evals_max_cost_eur",
            # corrective 4.2a : la borne de définitions auxiliaires de la rédaction sinistre
            "draft_max_definitions",
            # amendement AD-1 du 03/09/2026 (T2) : la part des blocs au-delà de laquelle une forme
            # de nombre n'élargit plus la requête de `chercher`
            "variante_nombre_max_part",
            # story 5.6 (T11) : l'effort du tour terminal, publié comme `navigation_tier_reason`
            # — un opérateur doit pouvoir lire dans la trace si ce tour a payé la profondeur
            "navigation_draft_effort_high",
            # story 3.1 : seuils génériques de densité, OCR et qualité PDF
            "mixed_page_image_density", "ocr_dpi", "quality_min_words", "foreign_signal_min",
            "french_signal_ratio_min",
            "gibberish_ratio_max", "residual_header_min_pages_ratio",
            "toc_page_number_baseline_pt", "toc_column_tolerance_pt", "toc_indent_tolerance_pt",
            "toc_line_gap_ratio", "toc_title_prefix_min_chars"} <= set(t.thresholds)
    assert {"dedent_tolerance_pt", "dedent_starter_max_lines"} <= set(t.thresholds)
    assert all(isinstance(v, (int, float)) for v in t.thresholds.values())
    # `guide_doc_id` et `sinistre_doc_id` sont des slugs, pas des seuils : ils n'ont rien à faire dans
    # `Trace.thresholds` (typé `dict[str, float | int]` — les y mettre ferait échouer la sérialisation).
    assert "guide_doc_id" not in t.thresholds and "sinistre_doc_id" not in t.thresholds


def test_chaque_seuil_publie_est_classe_seuil_de_gate_ou_interrupteur() -> None:
    """Story 5.6 (T20) : un seuil publié est **classé**, sinon ce test rougit.

    La règle de classement vit dans `config.py` : un seuil entre dans le contexte de gate s'il peut
    changer une claim, un verdict ou une citation ; le reste est un interrupteur d'exploitation. Une
    règle écrite ne se tient pas toute seule — c'est précisément un seuil publié sans être pensé
    comme l'un ou l'autre (`prefix_keepalive_enabled`, arrivé en T5) qui a fait refuser trois
    déploiements avec `gate_perime` sur les trois documents. Le témoin refuse donc l'oubli lui-même :
    ni clé non classée, ni clé dans les deux listes, ni entrée fantôme que `thresholds()` ne publie
    plus.
    """
    reglages = Settings(_env_file=None)
    publies = set(reglages.thresholds())
    assert not (publies - (SEUILS_DE_GATE | SEUILS_DEXPLOITATION)), (
        "seuil(s) publié(s) sans classement : les ranger dans `SEUILS_DE_GATE` (il peut changer une "
        "claim, un verdict ou une citation) ou dans `SEUILS_DEXPLOITATION` (il décide de ce que le "
        f"service dépense, garde ou refuse) — {sorted(publies - (SEUILS_DE_GATE | SEUILS_DEXPLOITATION))}")
    assert not (SEUILS_DE_GATE & SEUILS_DEXPLOITATION), sorted(SEUILS_DE_GATE & SEUILS_DEXPLOITATION)
    assert not ((SEUILS_DE_GATE | SEUILS_DEXPLOITATION) - publies), (
        "classement d'un seuil que `thresholds()` ne publie plus : "
        f"{sorted((SEUILS_DE_GATE | SEUILS_DEXPLOITATION) - publies)}")
    # Le sous-ensemble est une **projection** de `thresholds()` : mêmes valeurs, jamais recalculées.
    assert reglages.gate_thresholds() == {
        nom: valeur for nom, valeur in reglages.thresholds().items() if nom in SEUILS_DE_GATE}
    # Les trois interrupteurs qui diffèrent par construction entre la mesure et la production.
    assert {"llm_audit_exact", "prefix_keepalive_enabled",
            "live_budget_eur"} <= SEUILS_DEXPLOITATION
    # Et quelques seuils dont personne ne doit pouvoir dire qu'ils ne changent pas une réponse.
    assert {"deadline_s", "navigation_draft_max_claims", "verifier_max_claims", "quote_max_chars",
            "rediger_tier_reason", "retrieval_max_blocks"} <= SEUILS_DE_GATE


def test_allow_ungated_est_ferme_en_production_et_libre_en_dev() -> None:
    """AC 1.10 : « `ALLOW_UNGATED` est **désactivé** en production à la fin de cette story ».

    Avant la revue Codex 1.10 (B3), `prod` ne dérivait `False` que lorsque la variable était absente :
    `ENV=prod ALLOW_UNGATED=true` — un `--set-env-vars` au déploiement — armait la dérogation en
    production. Elle y est maintenant refusée, et le refus reste dicible (`ungated_demande_en_prod`).
    """
    assert Settings(_env_file=None, env="prod").allow_ungated is False
    prod = Settings(_env_file=None, env="prod", allow_ungated=True)
    assert prod.allow_ungated is False and prod.ungated_demande_en_prod is True
    assert Settings(_env_file=None, env="prod", allow_ungated=False).ungated_demande_en_prod is False
    # En `dev`, rien ne change : la dérogation y est le mode de travail normal (AD-7).
    assert Settings(_env_file=None, env="dev").allow_ungated is True
    assert Settings(_env_file=None, env="dev", allow_ungated=False).allow_ungated is False
    assert Settings(_env_file=None, env="dev", allow_ungated=True).ungated_demande_en_prod is False


def test_bounds_and_coherence() -> None:
    with pytest.raises(ValidationError, match="llm_timeout_s"):
        Settings(_env_file=None, llm_timeout_s=60, deadline_s=55)
    with pytest.raises(ValidationError, match="llm_retry_margin_s"):
        Settings(_env_file=None, llm_retry_margin_s=60, deadline_s=55)
    # revue 1.4 : un plafond par étape ne peut pas dépasser le plafond de sortie du client — il part
    # tel quel au fournisseur et entre au tarif `output` dans le majorant `estimate_cost`.
    with pytest.raises(ValidationError, match="rediger_max_tokens"):
        Settings(_env_file=None, rediger_max_tokens=8192, llm_max_output_tokens=4096)
    with pytest.raises(ValidationError, match="comprendre_max_tokens"):
        Settings(_env_file=None, comprendre_max_tokens=8192, llm_max_output_tokens=4096)
    with pytest.raises(ValidationError, match="verifier_max_tokens"):
        Settings(_env_file=None, verifier_max_tokens=8192, llm_max_output_tokens=4096)
    with pytest.raises(ValidationError, match="retrouver_outils_max_tokens"):
        Settings(_env_file=None, retrouver_outils_max_tokens=8192, llm_max_output_tokens=4096)
    with pytest.raises(ValidationError, match="retrouver_outils_tier"):
        Settings(_env_file=None, retrouver_outils_tier="ingest")
    for field in ("comprendre_tier", "verifier_tier", "retrouver_outils_tier"):
        with pytest.raises(ValidationError, match="baseline_tiers"):
            Settings(_env_file=None, **{field: "micro"})
    with pytest.raises(ValidationError, match="baseline_tiers"):
        Settings(_env_file=None, env="prod", baseline_tiers=True)
    # story 1.5 : *vérifier* doit pouvoir juger tout ce que *rédiger* peut produire, sinon des claims
    # retrouvées seraient rejetées « non évaluées » par pure configuration (dégradé silencieux).
    with pytest.raises(ValidationError, match="verifier_max_claims"):
        Settings(_env_file=None, verifier_max_claims=2, draft_max_claims=4)
    with pytest.raises(ValidationError, match="draft_max_claims.*draft_max_segments"):
        Settings(_env_file=None, draft_max_claims=4, draft_max_segments=3)
    Settings(_env_file=None, verifier_max_claims=4, draft_max_claims=4)
    # Story 5.6 T1b : `navigation_rediger_max_tokens` entre dans le même contrôle, et la place de
    # l'ébauche de navigation porte le même invariant claims <= segments que celle de *rédiger*.
    with pytest.raises(ValidationError, match="navigation_rediger_max_tokens"):
        Settings(_env_file=None, navigation_rediger_max_tokens=8192, llm_max_output_tokens=4096)
    with pytest.raises(ValidationError,
                       match="navigation_draft_max_claims.*navigation_draft_max_segments"):
        Settings(_env_file=None, navigation_draft_max_claims=4, navigation_draft_max_segments=3)
    # La part qui élargit une requête reste une **part** : zéro n'élargirait jamais rien, et
    # au-delà de 1 elle admettrait toutes les formes, y compris celles des mots que le document
    # porte partout — la borne `gt=0, le=1` a survécu au renommage de T2.
    for bad in ({"deadline_s": 0}, {"quote_min_ratio": 1.5}, {"max_opens": 0}, {"max_cost_eur_per_request": -1},
                {"evals_max_cost_eur": -1}, {"rate_limit_per_day": 0},
                {"variante_nombre_max_part": 0.0}, {"variante_nombre_max_part": 1.5}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **bad)


@pytest.mark.parametrize("tier", ["micro", "reason"])
def test_la_configuration_baseline_mesure_la_matrice_micro_reason(tier: str) -> None:
    settings = Settings(
        _env_file=None, baseline_tiers=True,
        comprendre_tier=tier, rediger_tier=tier, verifier_tier=tier,
        retrouver_outils_tier=tier,
    )
    attendu = int(tier == "reason")
    assert settings.thresholds()["baseline_tiers"] == 1
    assert tuple(settings.thresholds()[field] for field in (
        "comprendre_tier_reason", "rediger_tier_reason", "verifier_tier_reason",
        "retrouver_outils_tier_reason",
    )) == (attendu,) * 4


def test_env_file_is_read_from_repo_root(tmp_path: Path) -> None:
    assert Settings.model_config["env_file"] == REPO_ROOT / ".env"
    assert (REPO_ROOT / "pyproject.toml").is_file()
    env = tmp_path / ".env"
    env.write_text('ANTHROPIC_API_KEY="sk-test-123"\nUSD_EUR=0.5\n')
    s = Settings(_env_file=env)
    assert s.anthropic_api_key == "sk-test-123" and s.usd_eur == 0.5


def test_env_example_loads_as_is() -> None:
    s = Settings(_env_file=REPO_ROOT / ".env.example")
    assert s.anthropic_api_key == "" and s.env == "dev" and s.allow_ungated is True and s.usd_eur == 0.92


def test_empty_env_values_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNGATED", "")
    monkeypatch.setenv("MAX_OPENS", "")
    assert Settings(_env_file=None).max_opens == 6


# --- story 2.1 : les seuils du dictionnaire et du périmètre ------------------

def test_les_seuils_du_dictionnaire_sont_ceux_de_la_spec() -> None:
    s = Settings(_env_file=None)
    assert s.dictionary_term_max_chars == 60 and s.dictionary_term_max_words == 4
    assert s.dictionary_max_variants_per_term == 8 and s.dictionary_max_terms_per_fiche == 20
    assert s.dictionary_question_max_chars == 160 and s.dictionary_max_questions_per_fiche == 5
    assert s.dictionary_max_intent_triggers == 30 and s.dictionary_max_output_tokens == 16000
    assert s.dictionary_flat_max_blocks_per_request == 20
    assert s.dictionary_flat_max_input_chars == 12000
    assert s.dictionary_flat_max_terms_per_block == 3
    assert s.dictionary_flat_max_output_tokens == 4096
    assert s.dictionary_max_cost_eur == 3.0
    assert s.dictionary_batch_poll_s == 20.0 and s.dictionary_batch_timeout_s == 3600.0
    # AD-9 : le palier de la campagne d'enrichissement se lit ici. Le défaut est l'affectation du
    # spine (`ingest/* → ingest`) — une surcharge est un acte, jamais un changement de comportement.
    assert s.dictionary_tier == "ingest"
    assert s.thresholds()["dictionary_tier_reason"] == 0
    assert Settings(_env_file=None, dictionary_tier="reason").thresholds()[
        "dictionary_tier_reason"] == 1
    assert s.perimetre_max_chars == 4000
    # Convention Seuils : un nombre nouveau vit ici **et** se publie.
    t = s.thresholds()
    for nom in ("dictionary_term_max_chars", "dictionary_term_max_words",
                "dictionary_max_variants_per_term", "dictionary_max_terms_per_fiche",
                "dictionary_question_max_chars", "dictionary_max_questions_per_fiche",
                "dictionary_max_intent_triggers", "dictionary_max_output_tokens",
                "dictionary_flat_max_blocks_per_request", "dictionary_flat_max_input_chars",
                "dictionary_flat_max_terms_per_block", "dictionary_flat_max_output_tokens",
                "dictionary_max_cost_eur", "dictionary_batch_poll_s",
                "dictionary_batch_timeout_s", "perimetre_max_chars"):
        assert t[nom] == getattr(s, nom), nom


def test_les_seuils_du_typage_clauses_sont_bornes_publies_et_documentes() -> None:
    s = Settings(_env_file=None)
    expected = {
        "type_clauses_max_blocks_per_request": 10,
        "type_clauses_max_input_chars": 60000,
        "type_clauses_max_requests_per_batch": 1000,
        "type_clauses_max_output_tokens": 2048,
        "type_clauses_max_cost_eur": 12.0,
        "type_clauses_batch_poll_s": 20.0,
        "type_clauses_batch_timeout_s": 7200.0,
        "type_clauses_standard_concurrency": 8,
        "type_clauses_standard_max_retries": 3,
        "type_clauses_standard_retry_base_s": 1.0,
        "type_clauses_max_article_refs": 12,
        "type_clauses_max_scope_articles": 20,
        "type_clauses_max_relations": 6,
        "type_clauses_ref_expansion_max_blocks": 30,
        "type_clauses_definition_max_chars": 120,
        "type_clauses_definition_max_words": 12,
    }
    thresholds = s.thresholds()
    for name, value in expected.items():
        assert getattr(s, name) == value and thresholds[name] == value
        invalid = -1 if name == "type_clauses_standard_max_retries" else 0
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{name: invalid})
    assert Settings(_env_file=None, type_clauses_standard_max_retries=0).\
        type_clauses_standard_max_retries == 0
    cited = set(_seuils_commentes())
    assert {name.upper() for name in expected} <= cited


def _seuils_documentes() -> dict[str, str]:
    """Les paires `NOM=valeur` que `docs/choix-et-limites.md` publie comme réglages.

    Même rôle que `_seuils_commentes` pour `.env.example` — un second texte faisant autorité sur les
    mêmes nombres, donc à garder — mais sur la surface où cette story documente ses réglages. Les
    accents graves ne sont pas exclus ici : ce document est du markdown, où un nom de variable
    s'écrit entre accents graves, et la liste visée est une énumération de réglages, jamais de la
    prose décrivant une situation.
    """
    import re

    connus = {k.upper() for k in Settings.model_fields}
    paire = re.compile(r"\b([A-Z][A-Z0-9_]*)=([^\s`]+)")
    trouves: dict[str, str] = {}
    for nom, valeur in paire.findall((REPO_ROOT / "docs" / "choix-et-limites.md").read_text("utf-8")):
        if nom in connus:
            trouves[nom] = valeur.rstrip(".,;)")
    return trouves


def test_les_seuils_des_colonnes_et_de_la_structure_sont_bornes_publies_et_documentes() -> None:
    """Story 4.2c, convention Seuils : borné dans `Settings`, publié par `thresholds()`, documenté.

    La documentation vit dans `docs/choix-et-limites.md`, à côté de la géométrie qu'elle règle, et
    non dans `.env.example` : la plage produit d'une story ne peut porter aucun fichier `.env*`, que
    la checklist de passation refuse sans distinguer le gabarit d'un vrai fichier d'environnement.
    La preuve n'y perd rien et y gagne l'égalité des valeurs — chaque nombre publié est relu par
    `Settings` et comparé au défaut, là où `.env.example` n'aurait prouvé que la présence du nom.

    Que chacune des cinq bornes de colonne soit un **levier** réel — l'abaisser change la détection —
    est prouvé par `tests/test_colonnes.py`, et que celles du vérificateur en soient aussi l'est par
    `tests/test_structure_proposee.py` : ce test ne redouble ni l'un ni l'autre. Il ferme le seul
    trou qui restait : les dix nombres de la story n'étaient publiés qu'à moitié, et aucun n'était
    documenté.
    """
    s = Settings(_env_file=None)
    # nom → (défaut attendu, une valeur que la borne doit refuser)
    attendus: dict[str, tuple[float | int, float | int]] = {
        "column_gutter_min_pt": (18.0, 0),
        "column_min_lines": (4, 1),
        "column_min_span_ratio": (0.35, 0),
        "column_row_pairing_max_ratio": (0.5, 0),
        "column_min_fill_ratio": (0.6, 0),
        "structure_max_depth": (6, 0),
        "structure_max_nodes": (2000, 0),
        "structure_max_children": (256, 0),
        "structure_min_coverage": (1.0, -0.1),
        "structure_max_input_chars": (900000, 0),
        "structure_max_output_tokens": (16000, 0),
        "structure_max_cost_eur": (8.0, 0),
    }
    thresholds = s.thresholds()
    for nom, (valeur, refuse) in attendus.items():
        assert getattr(s, nom) == valeur and thresholds[nom] == valeur, nom
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{nom: refuse})
    for nom in ("column_min_span_ratio", "column_row_pairing_max_ratio", "column_min_fill_ratio",
                "structure_min_coverage"):
        with pytest.raises(ValidationError):  # une part reste une part : jamais au-delà de 1
            Settings(_env_file=None, **{nom: 1.5})
    documentes = _seuils_documentes()
    manquants = sorted({nom.upper() for nom in attendus} - set(documentes))
    assert not manquants, f"seuils de la story non documentés dans choix-et-limites.md : {manquants}"
    ecarts = []
    for nom in attendus:
        # La valeur publiée est du texte : on la relit par `Settings`, comme un `.env` le ferait.
        relu = getattr(Settings(_env_file=None, **{nom: documentes[nom.upper()]}), nom)
        if relu != getattr(s, nom):
            ecarts.append(f"{nom.upper()}={documentes[nom.upper()]} documenté, {getattr(s, nom)!r} dans config.py")
    assert not ecarts, "\n".join(ecarts)


@pytest.mark.parametrize("bad", [
    {"dictionary_term_max_chars": 0}, {"dictionary_term_max_words": 0},
    {"dictionary_max_variants_per_term": 0}, {"dictionary_max_terms_per_fiche": 0},
    {"dictionary_question_max_chars": 0}, {"dictionary_max_questions_per_fiche": 0},
    {"dictionary_max_intent_triggers": 0}, {"dictionary_max_output_tokens": 0},
    {"dictionary_flat_max_blocks_per_request": 0}, {"dictionary_flat_max_input_chars": 0},
    {"dictionary_flat_max_terms_per_block": 0}, {"dictionary_flat_max_output_tokens": 0},
    {"dictionary_max_cost_eur": 0}, {"dictionary_batch_poll_s": 0},
    {"dictionary_batch_timeout_s": 0}, {"perimetre_max_chars": 0},
])
def test_chaque_seuil_du_dictionnaire_est_borne(bad: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **bad)


def test_le_plafond_de_sortie_de_lingestion_nest_pas_borne_par_celui_du_client() -> None:
    """`llm_max_output_tokens` borne les appels du **serveur** (deadline, plafond par requête, AD-9).

    L'ingestion est hors ligne, en Batch API, et son garde-fou est `dictionary_max_cost_eur`, vérifié
    avant toute soumission. Les confondre plafonnerait une requête de batch à 4 096 tokens pour une
    raison qui ne la concerne pas.
    """
    s = Settings(_env_file=None)
    assert s.dictionary_max_output_tokens > s.llm_max_output_tokens


# --- reprise différée 1.11 : les seuils **commentés** de `.env.example` ------

def _seuils_commentes() -> dict[str, str]:
    """Les paires `NOM=valeur` que `.env.example` cite dans ses commentaires.

    `test_env_example_loads_as_is` ne voit que les lignes **actives** ; tout ce qui est commenté est
    un second texte faisant autorité sur les mêmes nombres, sans garde. La story 1.11 y a corrigé
    `LLM_TIMEOUT_S=25` → `40`, périmé depuis l'amendement AD-16 de la story 1.9 : la dérive avait
    duré une story entière.
    """
    import re

    connus = {k.upper() for k in Settings.model_fields}
    # Une paire **citée** (entre accents graves) est de la prose qui décrit une situation
    # (« `ENV=prod` + `ALLOW_UNGATED=true` produit l'alerte… »), pas la documentation d'un défaut :
    # la comparer à `Settings` ferait rougir le garde-fou sur une phrase parfaitement juste.
    paire = re.compile(r"(?<!`)\b([A-Z][A-Z0-9_]*)=([^\s`]+)")
    trouves: dict[str, str] = {}
    for ligne in (REPO_ROOT / ".env.example").read_text("utf-8").splitlines():
        if not ligne.lstrip().startswith("#"):
            continue
        for nom, valeur in paire.findall(ligne):
            if nom in connus:
                trouves[nom] = valeur.rstrip(".,;)")
    return trouves


def test_les_seuils_cites_en_commentaire_valent_les_defauts_de_settings() -> None:
    defauts = Settings(_env_file=None)
    cites = _seuils_commentes()
    assert cites, ".env.example ne cite plus aucun seuil : le garde-fou ne garde plus rien"
    ecarts = []
    for nom, valeur in sorted(cites.items()):
        attendu = getattr(defauts, nom.lower())
        # La valeur commentée est du texte : on la relit par `Settings`, comme un `.env` le ferait.
        relu = getattr(Settings(_env_file=None, **{nom.lower(): valeur}), nom.lower())
        if relu != attendu:
            ecarts.append(f"{nom}={valeur} dans .env.example, {attendu!r} dans config.py")
    assert not ecarts, "\n".join(ecarts)


def test_les_seuils_de_la_story_sont_documentes_dans_env_example() -> None:
    """Convention Seuils : « documenté dans `.env.example` ». Un seuil que personne ne sait régler
    n'est pas un réglage, c'est une constante cachée."""
    cites = set(_seuils_commentes())
    attendus = {"DICTIONARY_TERM_MAX_CHARS", "DICTIONARY_TERM_MAX_WORDS",
                "DICTIONARY_MAX_VARIANTS_PER_TERM", "DICTIONARY_MAX_TERMS_PER_FICHE",
                "DICTIONARY_QUESTION_MAX_CHARS", "DICTIONARY_MAX_QUESTIONS_PER_FICHE",
                "DICTIONARY_MAX_INTENT_TRIGGERS", "DICTIONARY_MAX_OUTPUT_TOKENS",
                "DICTIONARY_MAX_COST_EUR", "DICTIONARY_BATCH_POLL_S",
                "DICTIONARY_BATCH_TIMEOUT_S", "DICTIONARY_TIER", "PERIMETRE_MAX_CHARS",
                "TYPE_CLAUSES_MAX_BLOCKS_PER_REQUEST", "TYPE_CLAUSES_MAX_INPUT_CHARS",
                "TYPE_CLAUSES_MAX_REQUESTS_PER_BATCH", "TYPE_CLAUSES_MAX_OUTPUT_TOKENS",
                "TYPE_CLAUSES_MAX_COST_EUR", "TYPE_CLAUSES_BATCH_POLL_S",
                "TYPE_CLAUSES_BATCH_TIMEOUT_S", "TYPE_CLAUSES_MAX_ARTICLE_REFS",
                "TYPE_CLAUSES_MAX_SCOPE_ARTICLES", "TYPE_CLAUSES_MAX_RELATIONS",
                "TYPE_CLAUSES_REF_EXPANSION_MAX_BLOCKS", "TYPE_CLAUSES_DEFINITION_MAX_CHARS",
                "TYPE_CLAUSES_DEFINITION_MAX_WORDS"}
    assert attendus <= cites, sorted(attendus - cites)


def test_la_cle_posee_mais_vide_fait_foi(monkeypatch: pytest.MonkeyPatch) -> None:
    """AD-14, et depuis la story 2.1 l'ingestion du dictionnaire aussi : « sans clé, ça refuse ».

    `Settings` laisse tomber une variable **vide** (`env_ignore_empty=True`) et retombe sur le `.env`
    du poste : sans cette règle, `ANTHROPIC_API_KEY= uv run …` tournait et facturait — l'inverse
    exact de ce que la commande dit vouloir. Mesuré en 2.1 : la version naïve de l'ingestion a
    réellement soumis un lot de Batch API sous cette commande.
    """
    from server.app.config import cle_absente

    avec = Settings(_env_file=None, anthropic_api_key="sk-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert cle_absente(avec) is True          # posée et vide : elle fait foi
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert cle_absente(avec) is True
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-y")
    assert cle_absente(Settings(_env_file=None, anthropic_api_key="")) is False
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert cle_absente(avec) is False         # non posée : c'est `.env` qui répond
    assert cle_absente(Settings(_env_file=None, anthropic_api_key="")) is True


def test_les_deux_commandes_qui_exigent_la_cle_appliquent_la_meme_regle() -> None:
    """Une promesse identique ne peut pas être tenue par deux codes différents."""
    from server.app.config import cle_absente
    from server.evals.run import cle_absente as evals_cle_absente

    s = Settings(_env_file=None, anthropic_api_key="")
    assert evals_cle_absente(s) is cle_absente(s)


def test_la_borne_de_nombre_des_listes_de_comprendre_vit_ici_et_se_publie() -> None:
    """Revue Codex 2.2 (I2) : la Convention Seuils ne souffre pas d'exception de domicile.

    `LISTE_MAX = 32` vivait en dur dans `steps/comprendre.py`, au motif qu'elle entre dans le schéma
    JSON envoyé au modèle et qu'un `.env` la ferait varier d'un poste à l'autre. Le motif est bon,
    la conclusion ne l'était pas : `comprendre_max_tokens` entre lui aussi dans la requête — donc
    dans la clé des fixtures — et il est un champ de `Settings` depuis la story 1.4. Ce qui protège
    le schéma n'est pas le fichier, c'est de ne pas être un champ `.env`. La borne est donc une
    **constante de module** de `config.py`, publiée dans `Trace.thresholds` comme tout seuil actif,
    et l'étape n'en garde qu'un alias de lecture.
    """
    from server.app.config import LISTE_MAX_ITEMS
    from server.app.steps import comprendre as etape

    assert LISTE_MAX_ITEMS == 32
    assert etape.LISTE_MAX is LISTE_MAX_ITEMS  # l'étape lit, elle ne décide plus
    assert Settings(_env_file=None).thresholds()["liste_max_items"] == LISTE_MAX_ITEMS
    # Pas un champ `.env` : le schéma de sortie de *comprendre* ne doit pas dépendre du poste.
    assert "liste_max_items" not in Settings.model_fields
    # Et plus aucun littéral de borne dans le corps de l'étape (Convention Seuils, lettre exacte).
    source = (REPO_ROOT / "server" / "app" / "steps" / "comprendre.py").read_text("utf-8")
    corps = [ligne for ligne in source.splitlines()
             if not ligne.lstrip().startswith("#") and "= LISTE_MAX_ITEMS" not in ligne]
    assert not [ligne for ligne in corps if re.search(r"=\s*\d{2,}\s*$", ligne)], corps


def test_lordre_des_mecanismes_est_une_permutation_fermee_et_le_defaut_est_versionne() -> None:
    import json

    from pydantic import ValidationError

    from server.app.config import RETRIEVAL_DEFAULT_PATH

    permute = Settings(
        _env_file=None, retrieval_mechanism_order="faq,dictionnaire,outils,sommaire")
    assert permute.retrieval_mechanisms() == ("faq", "dictionnaire", "outils", "sommaire")
    with pytest.raises(ValidationError, match="retrieval_mechanism_order"):
        Settings(_env_file=None, retrieval_mechanism_order="faq,faq,sommaire,outils")
    triplet = json.loads(RETRIEVAL_DEFAULT_PATH.read_text(encoding="utf-8"))
    settings = Settings(_env_file=None)
    assert triplet == {"variant": settings.retrieval_variant,
                       "tier": settings.retrouver_outils_tier,
                       "prompt_cache": settings.retrieval_prompt_cache}


def test_a_fresh_settings_snapshot_adopts_the_promoted_versioned_triplet(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.app import config as module

    # Les deux triplets promus emploient des variantes qui **existent** : depuis T6, `outils` n'est
    # plus un nom acceptable, ni pour le triplet versionné ni pour `Settings` (`RetrievalVariant`).
    # Ce que le témoin mesure est la relecture de l'artefact par une instance neuve, pas le nom.
    path = tmp_path / "retrieval-default.json"
    path.write_text(
        '{"variant":"navigation","tier":"reason","prompt_cache":true}', encoding="utf-8")
    monkeypatch.setattr(module, "RETRIEVAL_DEFAULT_PATH", path)
    first = module.Settings(_env_file=None)
    path.write_text(
        '{"variant":"full_context","tier":"reason","prompt_cache":false}', encoding="utf-8")
    fresh = module.Settings(_env_file=None)

    assert (first.retrieval_variant, first.retrouver_outils_tier,
            first.retrieval_prompt_cache) == ("navigation", "reason", True)
    assert (fresh.retrieval_variant, fresh.retrouver_outils_tier,
            fresh.retrieval_prompt_cache) == ("full_context", "reason", False)


# --- story 4.5 : la disjonction d'AD-7 a trois termes (dette D1) ------------

def test_en_dev_allow_ungated_false_explicite_sert_quand_meme_un_document_sans_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AD-7, mot pour mot : « servi ssi aucun bloquant statique **et** (`gate.evals_ok` **ou**
    `ENV=dev` **ou** `ALLOW_UNGATED`) ».

    C'est la **seule** configuration où l'ancienne expression et la nouvelle diffèrent, et c'est
    pourquoi ce test existe : `bool(self.allow_ungated)` restait vert partout ailleurs, si bien que
    le correctif D1 n'était épinglé par rien et qu'un revert littéral serait passé inaperçu.

    Ici l'opérateur écrit explicitement « non » à la dérogation, en dev. L'AD la lui accorde tout de
    même — par son deuxième terme —, et le document sans gate est servi avec l'alerte `sans_gate`.
    """
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("ALLOW_UNGATED", "false")
    reglages = Settings(_env_file=None)
    # Les deux faits restent distincts : ce que l'opérateur a demandé, et ce que la règle décide.
    assert reglages.allow_ungated is False
    assert reglages.deroger_au_gate is True


def test_en_prod_la_disjonction_est_fausse_par_ses_trois_termes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """La fermeture de l'AC 1.10 est intacte : en `prod`, aucun des trois termes ne tient.

    Le pendant du test précédent, et il compte autant : un correctif qui honore le deuxième terme
    partout aurait rouvert en production la dérogation que la story 1.10 a fermée.
    """
    monkeypatch.setenv("ENV", "prod")
    for demande in ("true", "false"):
        monkeypatch.setenv("ALLOW_UNGATED", demande)
        reglages = Settings(_env_file=None)
        assert reglages.allow_ungated is False
        assert reglages.deroger_au_gate is False
    # Sans la variable non plus.
    monkeypatch.delenv("ALLOW_UNGATED", raising=False)
    assert Settings(_env_file=None).deroger_au_gate is False


def test_le_nom_du_fichier_de_publication_ne_peut_pas_sortir_de_data(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue 4.5, P12 : une seule autorité pour ce nom, et un motif qui interdit tout chemin.

    Le nom est composé avec `data_dir` par le lecteur **et** par l'écrivain : une valeur portant un
    séparateur ou `..` ferait lire — et écrire — hors de `data/`. Un réglage d'environnement ne doit
    pas pouvoir choisir un chemin.
    """
    from server.app.config import EVALS_PUBLICATION_FILE
    from server.evals.publication import PUBLICATION_JSON

    # Une seule autorité, partagée par l'écrivain (`evals`) et le lecteur (`api`).
    assert PUBLICATION_JSON == EVALS_PUBLICATION_FILE
    assert Settings(_env_file=None).evals_publication_file == EVALS_PUBLICATION_FILE
    for hostile in ("../../etc/passwd", "sous/dossier.json", "/absolu.json", "..", ".",
                    "avec espace.json"):
        monkeypatch.setenv("EVALS_PUBLICATION_FILE", hostile)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
    monkeypatch.setenv("EVALS_PUBLICATION_FILE", "autre-nom.json")
    assert Settings(_env_file=None).evals_publication_file == "autre-nom.json"


def test_la_revision_publiee_est_une_projection_de_la_revision_complete() -> None:
    """B2 : une seule source de vérité (`git_sha`), une projection pour l'affichage.

    AD-11 promet `GET /api/v1/sante` → `version: sha7`, et `scripts/smoke.py` compare cette valeur
    au sha7 du commit déployé. Mais le **gate** se compare sur la révision complète : servir la
    valeur brute des deux côtés obligeait à choisir, et le choix fait — sept caractères partout —
    rendait la comparaison de gate incapable de distinguer deux commits.
    """
    from server.app.config import SHA_COURT

    complete = "0123456789abcdef" * 2 + "01234567"
    assert len(complete) == 40
    reglages = Settings(_env_file=None, git_sha=complete)
    assert reglages.git_sha == complete
    assert reglages.version_publiee == complete[:SHA_COURT]
    assert len(reglages.version_publiee) == 7
    # Hors conteneur, la valeur n'est pas une révision : elle est publiée telle quelle.
    assert Settings(_env_file=None).version_publiee == "dev"
    # Une valeur déjà courte n'est pas retronquée en silence : elle n'est pas une révision.
    assert Settings(_env_file=None, git_sha="abc1234").version_publiee == "abc1234"


def test_la_borne_du_verificateur_sinistre_reserve_la_reflexion_quelle_paie() -> None:
    """Rapport rédiger C — la réflexion est comptée dans le même `max_tokens` que la sortie.

    Mesuré sur les 20 appels `verifier_sinistre` audités : 55 à 91 % de la sortie est de la
    réflexion, **1 904 tokens au maximum observé**, pour 300 à 1 100 caractères de JSON utile. La
    borne « tenait » par accident — `draft_max_claims = 4` rend inatteignables les 8 claims du
    calcul, et la moitié de budget ainsi libérée absorbait la réflexion. Une sortie tronquée est un
    `LlmParse` terminal, donc un 503 sur un sinistre nominal.
    """
    from server.app.config import Settings

    s = Settings(_env_file=None, anthropic_api_key="")
    # Maximum observé sur les appels audités du vérificateur sinistre. Il valait 1 904 au tour 2 ;
    # l'audit du tour 3 le corrige à 2 394 — la réserve d'alors était **déjà** dépassée.
    #
    # **T1d, 03/09/2026 : les deux termes sont relus sur l'audit complet, et le premier change de
    # nature.** À `low`, les 27 appels non tronqués de `a16-t1b/llm-calls.jsonl` donnent au pire
    # 2 932 tokens de réflexion et 738 de JSON (à cinq affirmations) — et c'est de nouveau l'effort
    # servi depuis T10. À `medium`,
    # les deux seuls appels de `a16-t1c/llm-calls.jsonl` ont **saturé** leur plafond : 4 096 et
    # 4 095 tokens de réflexion pour 4 096 de sortie, zéro JSON rendu. La mesure est **censurée** :
    # elle dit « au moins 4 096 », pas combien. Ce témoin retient donc le plancher qu'elle prouve —
    # une réserve qui ne le couvre pas ne peut que tronquer — sans prétendre qu'il majore quoi que
    # ce soit ; c'est le commentaire de `verifier_thinking_reserve_tokens` qui porte le pari.
    #
    # **T10 : l'effort servi est redescendu à `low`, et ce plancher est conservé tel quel.** Il ne
    # décrit plus l'appel qu'on fait ; il décrit ce que la réserve doit pouvoir absorber si l'effort
    # remontait, et il est la contrainte la plus serrée que la mesure ait jamais prouvée. La borne
    # large qu'il impose est exactement ce que `verifier_thinking_reserve_tokens` assume.
    REFLEXION_MESUREE = 4096
    JSON_MESURE = 738
    assert s.verifier_thinking_reserve_tokens >= REFLEXION_MESUREE, (
        "la réserve doit couvrir la réflexion mesurée, sinon elle rogne sur le JSON")
    assert s.verifier_sinistre_json_tokens >= JSON_MESURE
    assert (s.verifier_sinistre_max_tokens
            == s.verifier_sinistre_json_tokens + s.verifier_thinking_reserve_tokens)
    # Le contrôle de cohérence mord : la somme ne peut plus dépasser le plafond du client en silence.
    with pytest.raises(ValidationError, match="verifier_sinistre_max_tokens"):
        Settings(_env_file=None, anthropic_api_key="", verifier_thinking_reserve_tokens=5500)


def test_le_delai_dappel_laisse_ecrire_la_plus_longue_sortie_detape() -> None:
    """R3 — un plafond de sortie qu'on n'a pas le temps d'écrire est un 503 qui s'ignore.

    À 4 096 tokens et 40 s, la borne effective du vérificateur sinistre était 3 575 tokens (87 % du
    plafond déclaré) au débit mesuré, et la deuxième réponse A16 est morte là — sur son délai
    d'appel, avec la meilleure ébauche des trois, alors que la deadline lui laissait encore 73 s.
    Les deux nombres vivaient dans deux dérivations qui s'ignoraient.
    """
    from server.app.config import Settings

    s = Settings(_env_file=None, anthropic_api_key="")
    plus_longue = max(s.verifier_sinistre_max_tokens, s.verifier_max_tokens, s.rediger_max_tokens,
                      s.comprendre_max_tokens, s.retrouver_outils_max_tokens)
    assert plus_longue / s.llm_output_tokens_per_s_min + s.llm_latence_marge_s <= s.llm_timeout_s
    # Le débit publié **minore** la mesure (89 à 95 tokens/s sur les quatre appels audités) :
    # majorer une durée demande de sous-estimer la vitesse, pas de la moyenner.
    assert s.llm_output_tokens_per_s_min <= 89.0

    # L'invariante mord dans les deux sens : un délai trop court comme un plafond trop grand.
    with pytest.raises(ValidationError, match="ne laisse pas écrire"):
        Settings(_env_file=None, anthropic_api_key="", llm_timeout_s=40.0)
    with pytest.raises(ValidationError, match="ne laisse pas écrire"):
        # Le plafond du client relevé **et** une étape qui le remplit : la borne par étape passe,
        # c'est bien le temps d'écriture qui refuse.
        Settings(_env_file=None, anthropic_api_key="", llm_max_output_tokens=7000,
                 rediger_max_tokens=7000)


def test_la_deadline_couvre_la_chaine_de_navigation_par_le_modele() -> None:
    """Story 5.6, T3 — la deadline doit couvrir le chemin qu'AD-1 rend nominal, pas l'ancien.

    L'amendement AD-1 du 03/09/2026 remplace la retrouvaille par heuristiques par une **navigation
    par le modèle en 6 à 8 tours**, *rédiger* fusionné dans la même conversation. La deadline de
    100 s était dérivée d'un chemin à **deux** tours dont la sélection était faite par le code : le
    chemin nominal d'AD-1 la dépasse, et le dépasser signifie un `Timeout` **terminal** (503) sur
    une question parfaitement nominale — les deux mécanismes qui le produisent, `remaining() <= 0`
    avant chaque étape et `timeout_for_call() = min(llm_timeout_s, remaining())`, vivent hors de
    tout `except`.

    Les termes sont **mesurés**, pas choisis. Le prototype validé (`automation/runs/
    20260902-structure-index/proto-runs/serie2/`, 03/09/2026 07 h 00, A16 ×3 + bougie ×1, sommaire
    AXA complet de 42 967 tokens en préfixe caché) donne la charge d'un tour d'outils ; les trois
    réponses A16 du pipeline intégré (`.../a16-t1b/a16-r{1,2,3}.json`) donnent celles de *comprendre*
    et du tour terminal.

    **La règle des termes, depuis T11 (03/09/2026) : mesuré quand la mesure existe à l'effort
    servi, plafond envoyé sinon.** Elle a changé de camp pour deux termes en même temps, et pour la
    même raison — c'est l'effort de chaque appel qui décide lequel des deux est honnête :

    - *le tour terminal* passe au **plafond envoyé** (`navigation_rediger_max_tokens`). Jusqu'ici
      son terme était mesuré (2 386 = 1 890 de JSON + 496 de réflexion) parce que sa réflexion
      l'avait été. Depuis que `navigation_draft_effort` le sert à `high`, plus aucune mesure ne
      décrit sa réflexion, et sur Sonnet 5 rien ne la borne que `max_tokens` (`llm/models.py`,
      T1d/T10) : le seul majorant non inventé est le plafond lui-même ;
    - *vérifier* passe au **maximum mesuré majoré**. Son terme lisait le plafond envoyé
      (`verifier_sinistre_max_tokens` = 6 144) parce que sa dépense n'avait jamais été mesurée à
      l'effort servi — c'était vrai à `medium` (T1c/T1d, mesure censurée « ≥ 4 096 »). Depuis le
      repli à `low` de T10, elle l'est : 1 732 / 1 971 / 2 175 tokens de réflexion sur les trois
      runs A16 de `f858a28` (`.../a16-final2/`), pour un contrat JSON de 1 024. C'est cette
      mesure-là qui majore, et l'assertion sur `EFFORT_PAR_PROMPT` plus bas est ce qui la tient :
      remonter l'effort de cet appel rougit ce témoin **avant** de coûter une deadline.

    Ce que le témoin n'affirme donc plus, et qu'il faut savoir : aux deux **plafonds envoyés** pris
    ensemble (5 056 + 6 144, deux fois), la queue majorée vaut 351,8 s et dépasse `deadline_s`. La
    deadline couvre le chemin servi, pas un vérificateur qui saturerait sa réserve de réflexion —
    ce que seul un retour à `medium` rendrait à nouveau atteignable, et que l'assertion interdit.

    **T13, 03/09/2026 : le terme du tour terminal passe de 3 072 à 5 056, et la marge du témoin
    tombe de 48 s à 1,4 s.** C'est la mesure du gate Baloise qui l'a relevé (voir
    `navigation_rediger_max_tokens`), et le chiffre que ce témoin rend est ce qui a **borné** le
    plafond : la dérivation prescrivait 5 888, la queue en aurait fait 308,1 s. La marge de 1,4 s
    n'est pas un confort qui s'est réduit, c'est le fait que la chaîne d'AD-1 est désormais à son
    budget : le prochain terme qui monte — le tour terminal, *vérifier*, ou les tours d'outils dont
    le majorant de 729 est un prototype quand le gate les voit saturer leurs 1 024 — ne pourra plus
    être absorbé, et c'est l'ordre d'AD-11 qu'il faudra rouvrir.

    **T14, 03/09/2026 : le chemin de reprise du tour terminal tronqué n'est pas un terme de cette
    dérivation, et ce témoin dit pourquoi.** `steps/naviguer.py::_appel_terminal` redemande une fois
    l'ébauche à l'effort `low` quand la sortie a été coupée par `max_tokens`. Compté honnêtement — un
    appel de plus **au plafond**, comme les deux autres tours terminaux —, il porte la queue majorée
    à 350,0 s, bien au-delà des 290 s : la deadline ne peut pas l'absorber, exactement comme elle ne
    pouvait pas absorber les 5 888 tokens que T13 prescrivait. La reprise n'est donc pas une dépense
    garantie mais une tentative **conditionnée au temps réellement restant** : le code ne l'envoie
    que si `remaining()` couvre `duree_majoree_pour(navigation_rediger_max_tokens)` (64,5 s à la
    marge de latence du client, plus stricte que les 2 s de cette dérivation), et rend sinon la
    troncature telle quelle. C'est ce qui la rend compatible avec `deadline_s` sans l'amender —
    l'assertion ci-dessous tient ce raisonnement, en refusant qu'on la compte comme acquise.

    Le témoin est écrit contre la **cible du spine** (8 tours), et non contre le plafond du code :
    la deadline doit couvrir le chemin que l'architecture rend légitime. La seconde assertion tient
    l'autre bout — une configuration qui autoriserait plus de tours que la cible sortirait de la
    dérivation sans que rien ne rougisse. Depuis la tâche T2 du 03/09/2026, le plafond de tours du
    chemin servi est `navigation_max_llm_turns` : `max_llm_turns` bornait la variante `outils`, qui
    n'est plus servie et n'existe plus.
    """
    from server.app.config import Settings
    from server.app.llm.models import EFFORT_PAR_PROMPT

    # AD-1, amendement du 03/09/2026 : « navigation par le modèle sur sommaire complet en 6–8 tours ».
    TOURS_CIBLE_AD1 = 8
    # **Re-mesuré le 03/09/2026 (T1c) : 360, et non 220.** 220 était le maximum des 108 réponses
    # Sonnet enregistrées, sur un prompt que la navigation a remplacé. Les trois réponses A16 du
    # pipeline intégré rendent 316 / 336 / 359 tokens ; 360 majore le pire.
    COMPRENDRE = 360
    # **Re-dérivé le 03/09/2026 (T1b), et c'est le seul terme que T1b déplace.** 1 509 était le pire
    # *rédiger* enregistré **à quatre claims**, sur l'ancien étage. Depuis que la place de l'ébauche
    # de navigation vaut `navigation_draft_max_claims` (6), ce maximum ne majore plus rien. Mesure :
    # les trois réponses A16 du pipeline intégré rendent 1 574 / 1 181 / 1 259 tokens au tour
    # terminal, dont 496 / 0 / 0 de réflexion — soit 1 078 / 1 181 / 1 259 de JSON à quatre claims,
    # au pire ≈ 315 par claim. À six claims : 6 × 315 ≈ 1 890 de JSON, plus les 496 tokens de
    # réflexion réellement observés sur ce tour.
    # T11 : plus de nombre figé ici — le plafond envoyé, pour la raison dite dans la docstring.
    # T13 : il vaut 5 056, dont 3 200 de contrat JSON à six claims — le pire JSON mesuré par le
    # gate Baloise (2 558) majoré de 25 % — et 1 856 laissés à la réflexion de `medium`.
    # Pire tour d'outils du prototype : 729 tokens, dont 657 de réflexion adaptative (A16 run 1,
    # tour 3). Les tours terminaux mesurés du prototype (709 à 900) restent sous `TOUR_TERMINAL`.
    TOUR_D_OUTILS = 729
    # Latence d'amorçage par appel, **majorée**. Mesurée sur la série 2, une fois le débit minoré à
    # 85 tokens/s : 0,77 s (run 1, 4 appels), 0,22 s (run 2), 0 s (run 3, plus rapide que le
    # minorant), 0,98 s (bougie). Aucun appel au-dessus de 1 s ; on majore du double.
    LATENCE_PAR_APPEL_S = 2.0

    s = Settings(_env_file=None, anthropic_api_key="")
    TOUR_TERMINAL = s.navigation_rediger_max_tokens
    # *vérifier* à `low` : 1 024 de contrat JSON, plus 2 432 de réflexion — le pire des trois runs
    # A16 (2 175) majoré de 12 %. Le terme vaut donc 3 456, et il n'est vrai qu'à cet effort-là.
    VERIFIER = 3_456
    assert EFFORT_PAR_PROMPT["verifier_sinistre"] == "low", (
        "le terme de *vérifier* de cette dérivation est la dépense **mesurée** à `low` ; à un "
        "autre effort elle n'est plus mesurée, et la deadline doit être re-dérivée sur le plafond "
        f"envoyé ({s.verifier_sinistre_max_tokens} tokens) avant de servir cet effort")
    assert s.navigation_draft_effort in ("low", "medium", "high")
    assert s.navigation_max_llm_turns <= TOURS_CIBLE_AD1, (
        f"navigation_max_llm_turns ({s.navigation_max_llm_turns}) dépasse la cible d'AD-1 "
        f"({TOURS_CIBLE_AD1}) : la deadline a été dérivée pour ce nombre de tours, il faut la "
        "re-dériver avant de le franchir")

    # Le pire chemin nominal : *comprendre*, sept tours d'outils, le tour terminal qui rend
    # l'ébauche, *vérifier*, puis la relance atomique d'AD-3 — *rédiger* et *vérifier* indissociables.
    tokens = (COMPRENDRE + (TOURS_CIBLE_AD1 - 1) * TOUR_D_OUTILS + TOUR_TERMINAL + VERIFIER
              + TOUR_TERMINAL + VERIFIER)
    appels = 1 + TOURS_CIBLE_AD1 + 1 + 2
    queue = tokens / s.llm_output_tokens_per_s_min + appels * LATENCE_PAR_APPEL_S

    assert s.deadline_s >= queue, (
        f"deadline {s.deadline_s} s sous la queue majorée du chemin de navigation d'AD-1 "
        f"({queue:.1f} s pour {tokens} tokens à {s.llm_output_tokens_per_s_min} tokens/s et "
        f"{appels} appels) : un `Timeout` terminal reste atteignable sur une question nominale")
    # T14 : la reprise du tour terminal tronqué, comptée au plafond, ne tient pas dans la deadline —
    # c'est le fait qui interdit d'en faire un terme et qui oblige à la conditionner au reste.
    reprise = TOUR_TERMINAL / s.llm_output_tokens_per_s_min + LATENCE_PAR_APPEL_S
    assert queue + reprise > s.deadline_s, (
        f"la reprise du tour terminal tronqué ({reprise:.1f} s au plafond) tiendrait dans la "
        f"deadline ({queue + reprise:.1f} s ≤ {s.deadline_s} s) : elle cesserait d'avoir besoin "
        "d'être conditionnée au temps restant, et `_appel_terminal` peut alors être simplifié")
    # Le débit publié **minore** encore la mesure : 85,3 tokens/s au plus lent des quatre runs du
    # prototype (bougie : 1 194 tokens de sortie en 16,0 s, deux appels).
    assert s.llm_output_tokens_per_s_min <= 85.3
    # T1b : le plafond du tour terminal reste sous le terme le plus long de `_coherence`
    # (`verifier_sinistre_max_tokens`), donc `llm_timeout_s` n'a pas à être re-dérivé — mais il est
    # bien écrit qu'on a le temps de l'écrire.
    assert s.duree_majoree_pour(s.navigation_rediger_max_tokens) <= s.llm_timeout_s
    assert s.llm_timeout_s < s.deadline_s and s.llm_retry_margin_s < s.deadline_s


def test_le_plafond_de_cout_couvre_le_chemin_froid_de_la_navigation() -> None:
    """Story 5.6, T7 — le plafond par requête doit couvrir le chemin servi **à cache froid**.

    Le garde-fou de coût compare `budget.cost_eur + estimate_cost(...)` **avant** chaque envoi
    (`llm/client.py`) et lève `BudgetExceeded`, qui est terminal. Un plafond sous le pire chemin
    nominal ne borne donc pas une dérive : il rend un 503 de configuration atteignable sur une
    question nominale, ce qu'AD-16 interdit. C'est ce qui s'est produit le 03/09/2026 à 10 h 05, au
    ré-enregistrement de la fixture du cas bougie : `0,5557 € déjà engagés + 0,1979 € estimés >
    0,7500 €`, sur une chaîne parfaitement conforme dont le seul tort était d'avoir le cache froid.

    Le pire chemin est **le même** que celui de `deadline_s` — c'est le même AD-1 qui le rend
    légitime, et deux dérivations qui divergeraient sur ce qu'est « nominal » finiraient par se
    contredire. Les bornes de tokens, elles, sont celles que **l'estimateur** voit sur la chaîne
    sinistre scriptée (préfixes réels, `estimate_chars_per_token` / `estimate_tokenizer_factor`), et
    non les tokens réels du fournisseur : c'est l'estimation qui décide du refus, pas la facture.

    L'engagé réel avant un appel est majoré par la somme des majorants qui le précèdent. La valeur
    que le plafond doit couvrir est donc la somme des douze, dans le régime que le fournisseur sert
    réellement — préfixe écrit une fois, relu ensuite (`RequestBudget.prefix_seen`). Le régime où
    aucun préfixe n'est jamais relu reste le mur, et le témoin exige que le plafond reste dessous :
    sans quoi le garde-fou cesserait de mordre sur une requête réellement anormale.
    """
    from server.app.config import Settings
    from server.app.llm.models import TIERS
    from server.app.llm.pricing import PRICES

    # Bornes **de l'estimateur**, relevées le 03/09/2026 sur la chaîne sinistre scriptée.
    PREFIXE = {"comprendre": 5_355, "navigation": 55_983, "terminal": 56_843, "verifier": 19_539}
    MESSAGES = {"comprendre": 361, "navigation": 8_414, "terminal": 8_672, "verifier": 779}
    TOURS_CIBLE_AD1 = 8  # la même cible que la dérivation de `deadline_s`

    s = Settings(_env_file=None, anthropic_api_key="")
    p = PRICES[TIERS["reason"]]
    eur = s.usd_eur / 1_000_000

    def majorant(cle: str, sortie: int, *, froid: bool) -> float:
        taux = p["cache_write_1h"] if froid else p["cache_read"]
        return (PREFIXE[cle] * taux + MESSAGES[cle] * p["input"] + sortie * p["output"]) * eur

    def chemin(*, jamais_relu: bool) -> float:
        """La somme des douze majorants du pire chemin nominal d'AD-1."""
        total = majorant("comprendre", s.comprendre_max_tokens, froid=True)
        for tour in range(TOURS_CIBLE_AD1 - 1):
            total += majorant("navigation", s.retrouver_outils_max_tokens,
                              froid=jamais_relu or tour == 0)
        verifier = s.verifier_sinistre_json_tokens + s.verifier_thinking_reserve_tokens
        total += majorant("terminal", s.navigation_rediger_max_tokens, froid=jamais_relu)
        total += majorant("verifier", verifier, froid=True)
        # relance atomique d'AD-3 : le tour terminal et *vérifier* une seconde fois
        total += majorant("terminal", s.navigation_rediger_max_tokens, froid=jamais_relu)
        total += majorant("verifier", verifier, froid=jamais_relu)
        return total

    pire_nominal = chemin(jamais_relu=False)
    mur = chemin(jamais_relu=True)
    assert s.navigation_max_llm_turns <= TOURS_CIBLE_AD1, (
        "le plafond de coût a été dérivé pour la cible d'AD-1 : re-dérive-le avant de la franchir")
    assert s.max_cost_eur_per_request >= pire_nominal, (
        f"plafond {s.max_cost_eur_per_request} € sous le pire chemin nominal à cache froid "
        f"({pire_nominal:.4f} €) : un `BudgetExceeded` terminal reste atteignable sur une question "
        "nominale")
    assert s.max_cost_eur_per_request < mur, (
        f"plafond {s.max_cost_eur_per_request} € au-dessus du régime où aucun préfixe n'est relu "
        f"({mur:.4f} €) : le garde-fou ne mord plus sur rien")
