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
    assert s.deadline_s == 55 and s.llm_timeout_s == 40  # 40 s : AD-16 amendé en 1.9, sur mesure
    assert s.raison_publiable_max_chars == RAISON_PUBLIABLE_MAX_DEFAULT == 500
    assert s.quote_min_chars == 25 and s.quote_min_ratio == 0.6
    assert s.max_opens == 6 and s.node_window == 30 and s.search_limit == 20 and s.max_llm_turns == 2
    assert s.max_llm_attempts == 8 and s.retrouver_outils_max_tokens == 1024
    assert s.retrouver_outils_tier == "micro"
    assert s.rediger_max_tokens == 2048
    assert "outils_rediger_max_tokens" not in Settings.model_fields
    assert s.max_cost_eur_per_request == 0.12 and s.cost_alert_eur == 0.05
    # story 1.10 : AD-9 remplace le plafond **par requête** par un plafond **par run** en évals ;
    # CLAUDE.md exige « la clé **et un plafond** ». `--max-cost` ne fait que surcharger celui-ci.
    assert s.evals_max_cost_eur == 1.0
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
    assert s.verifier_max_claims == 8 and s.verifier_max_tokens == 1024
    # story 1.8 : contrat servi par le pipeline sinistre, et les bornes de son appel groupé
    assert s.sinistre_doc_id == "axa-lu-optihome-2017"
    assert s.verifier_sinistre_max_tokens == 3072
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
    assert t.thresholds["max_cost_eur_per_request"] == 0.12
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
    with pytest.raises(ValidationError, match="max_llm_turns"):
        Settings(_env_file=None, max_llm_turns=3)
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
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{name: 0})
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
        "structure_max_cost_eur": (5.0, 0),
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
                "DICTIONARY_BATCH_TIMEOUT_S", "PERIMETRE_MAX_CHARS",
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
        '{"variant":"outils","tier":"micro","prompt_cache":true}', encoding="utf-8")
    monkeypatch.setattr(module, "RETRIEVAL_DEFAULT_PATH", path)
    first = module.Settings(_env_file=None)
    path.write_text(
        '{"variant":"full_context","tier":"reason","prompt_cache":false}', encoding="utf-8")
    fresh = module.Settings(_env_file=None)

    assert (first.retrieval_variant, first.retrouver_outils_tier,
            first.retrieval_prompt_cache) == ("outils", "micro", True)
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
