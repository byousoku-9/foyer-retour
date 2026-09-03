from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.app.config import RAISON_PUBLIABLE_MAX_DEFAULT, REPO_ROOT, Settings
from server.app.domain.trace import Trace

THRESHOLD_VARS = [k.upper() for k in Settings.model_fields] + ["ENV", "ALLOW_UNGATED"]


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in THRESHOLD_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_match_spine_hypotheses() -> None:
    s = Settings(_env_file=None)
    # `deadline_s` : 165 depuis la story 5.6 (T3, 03/09/2026) — la navigation par le modèle
    # (AD-1 amendé, 6 à 8 tours, *rédiger* fusionné) majore à 141,4 s le pire chemin nominal
    # mesuré sur le prototype validé ; 100 s le laissait sortir en 503.
    # `llm_timeout_s` : 55 depuis le correctif du tour 3, et **inchangé** par 5.6 — il borne un
    # appel, pas la chaîne, et la plus longue sortie d'étape (3 456 tokens) demande 45,7 s. À 40, le
    # plafond de sortie du vérificateur sinistre était inatteignable dans le temps qu'on lui
    # laissait, et une réponse valide mourait sur son délai d'appel.
    # `client_abort_margin_s` : 150 depuis 5.6 — l'ordre d'AD-11 (client 315 s > Cloud Run 300 s >
    # serveur 165 s) la dicte ; elle ne se choisit plus « un peu au-dessus de la deadline ».
    assert s.deadline_s == 165 and s.llm_timeout_s == 55
    assert s.client_abort_margin_s == 150
    assert s.raison_publiable_max_chars == RAISON_PUBLIABLE_MAX_DEFAULT == 500
    assert s.quote_min_chars == 25 and s.quote_min_ratio == 0.6
    # `max_llm_turns` : trois depuis le correctif du tour 2 — à deux, le verdict terminal de la
    # navigation est structurellement inatteignable (les résultats du dernier tour ne sont
    # jamais réinjectés), et la suffisance sémantique reste toujours refusée.
    assert s.max_opens == 6 and s.node_window == 30 and s.search_limit == 20 and s.max_llm_turns == 3
    assert s.max_llm_attempts == 10 and s.retrouver_outils_max_tokens == 1024
    assert s.retrouver_outils_tier == "reason"
    assert (s.comprendre_tier, s.rediger_tier, s.verifier_tier) == (
        "reason", "reason", "reason")
    assert s.rediger_max_tokens == 2048
    assert "outils_rediger_max_tokens" not in Settings.model_fields
    assert s.max_cost_eur_per_request == 0.75 and s.cost_alert_eur == 0.25
    # story 1.10 : AD-9 remplace le plafond **par requête** par un plafond **par run** en évals ;
    # CLAUDE.md exige « la clé **et un plafond** ». `--max-cost` ne fait que surcharger celui-ci.
    assert s.evals_max_cost_eur == 12.0
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
    assert s.verifier_max_claims == 8 and s.verifier_max_tokens == 3072
    # story 1.8 : contrat servi par le pipeline sinistre, et les bornes de son appel groupé
    assert s.sinistre_doc_id == "axa-lu-optihome-2017"
    # Correctif du tour 2 : la borne est **dérivée**, contrat JSON + réserve de réflexion. Elle
    # valait 3 072 de JSON sans un token pour la réflexion, alors que celle-ci est comptée dans le
    # même `max_tokens` et représente 55 à 91 % de la sortie mesurée. La somme atteint exactement
    # le plafond du client : le contrôle de cohérence mord, et c'est voulu.
    # Corrigé au tour 3 : le JSON réellement rendu vaut 329 à 510 tokens (2 048 majorait un contrat
    # que le sinistre ne produit pas), et la réflexion mesurée 2 394 — la réserve du tour 2 était
    # déjà dépassée quand elle a été écrite.
    assert s.verifier_sinistre_json_tokens == 768
    assert s.verifier_thinking_reserve_tokens == 2688
    assert s.verifier_sinistre_max_tokens == 3456 <= s.llm_max_output_tokens
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
    assert t.thresholds["max_cost_eur_per_request"] == 0.75
    assert {"max_opens", "node_window", "search_limit", "max_llm_attempts", "max_llm_turns",
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
            # story 2.3 : les places réservées, parmi `max_opens`, aux nœuds que le profil désigne
            "profil_max_opens",
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
    with pytest.raises(ValidationError, match="max_llm_turns"):
        Settings(_env_file=None, max_llm_turns=4)
    # story 1.5 : *vérifier* doit pouvoir juger tout ce que *rédiger* peut produire, sinon des claims
    # retrouvées seraient rejetées « non évaluées » par pure configuration (dégradé silencieux).
    with pytest.raises(ValidationError, match="verifier_max_claims"):
        Settings(_env_file=None, verifier_max_claims=2, draft_max_claims=4)
    with pytest.raises(ValidationError, match="draft_max_claims.*draft_max_segments"):
        Settings(_env_file=None, draft_max_claims=4, draft_max_segments=3)
    Settings(_env_file=None, verifier_max_claims=4, draft_max_claims=4)
    for bad in ({"deadline_s": 0}, {"quote_min_ratio": 1.5}, {"max_opens": 0}, {"max_cost_eur_per_request": -1},
                {"evals_max_cost_eur": -1}, {"rate_limit_per_day": 0}):
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

    path = tmp_path / "retrieval-default.json"
    path.write_text(
        '{"variant":"outils","tier":"reason","prompt_cache":true}', encoding="utf-8")
    monkeypatch.setattr(module, "RETRIEVAL_DEFAULT_PATH", path)
    first = module.Settings(_env_file=None)
    path.write_text(
        '{"variant":"full_context","tier":"reason","prompt_cache":false}', encoding="utf-8")
    fresh = module.Settings(_env_file=None)

    assert (first.retrieval_variant, first.retrouver_outils_tier,
            first.retrieval_prompt_cache) == ("outils", "reason", True)
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
    REFLEXION_MESUREE = 2394
    JSON_MESURE = 510
    assert s.verifier_thinking_reserve_tokens >= REFLEXION_MESUREE, (
        "la réserve doit couvrir la réflexion mesurée, sinon elle rogne sur le JSON")
    assert s.verifier_sinistre_json_tokens >= JSON_MESURE
    assert (s.verifier_sinistre_max_tokens
            == s.verifier_sinistre_json_tokens + s.verifier_thinking_reserve_tokens)
    # Le contrôle de cohérence mord : la somme ne peut plus dépasser le plafond du client en silence.
    with pytest.raises(ValidationError, match="verifier_sinistre_max_tokens"):
        Settings(_env_file=None, anthropic_api_key="", verifier_thinking_reserve_tokens=3500)


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
        Settings(_env_file=None, anthropic_api_key="", llm_max_output_tokens=6000,
                 rediger_max_tokens=6000)


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
    AXA complet de 42 967 tokens en préfixe caché) donne les charges de navigation ; les charges de
    *comprendre*, *rédiger* et *vérifier* restent les maxima des 108 réponses Sonnet enregistrées,
    déjà retenus par `tests/test_budget.py`.

    Le témoin est écrit contre la **cible du spine** (8 tours), et non contre `max_llm_turns` : la
    deadline doit couvrir le chemin que l'architecture rend légitime, que le code de l'étape ait
    déjà été réécrit ou non. La seconde assertion tient l'autre bout — une configuration qui
    autoriserait plus de tours que la cible sortirait de la dérivation sans que rien ne rougisse.
    """
    from server.app.config import Settings

    # AD-1, amendement du 03/09/2026 : « navigation par le modèle sur sommaire complet en 6–8 tours ».
    TOURS_CIBLE_AD1 = 8
    # Maxima enregistrés (108 réponses Sonnet, cf. `deadline_s` dans `config.py`).
    COMPRENDRE = 220
    TOUR_TERMINAL = 1_509   # l'ébauche `AnswerDraft` : pire *rédiger* enregistré
    VERIFIER = 820          # pire *vérifier* enregistré
    # Pire tour d'outils du prototype : 729 tokens, dont 657 de réflexion adaptative (A16 run 1,
    # tour 3). Les tours terminaux mesurés du prototype (709 à 900) restent sous `TOUR_TERMINAL`.
    TOUR_D_OUTILS = 729
    # Latence d'amorçage par appel, **majorée**. Mesurée sur la série 2, une fois le débit minoré à
    # 85 tokens/s : 0,77 s (run 1, 4 appels), 0,22 s (run 2), 0 s (run 3, plus rapide que le
    # minorant), 0,98 s (bougie). Aucun appel au-dessus de 1 s ; on majore du double.
    LATENCE_PAR_APPEL_S = 2.0

    s = Settings(_env_file=None, anthropic_api_key="")
    assert s.max_llm_turns <= TOURS_CIBLE_AD1, (
        f"max_llm_turns ({s.max_llm_turns}) dépasse la cible d'AD-1 ({TOURS_CIBLE_AD1}) : la "
        "deadline a été dérivée pour ce nombre de tours, il faut la re-dériver avant de le franchir")

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
    # Le débit publié **minore** encore la mesure : 85,3 tokens/s au plus lent des quatre runs du
    # prototype (bougie : 1 194 tokens de sortie en 16,0 s, deux appels).
    assert s.llm_output_tokens_per_s_min <= 85.3
    assert s.llm_timeout_s < s.deadline_s and s.llm_retry_margin_s < s.deadline_s
