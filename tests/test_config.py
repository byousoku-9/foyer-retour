from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.app.config import REPO_ROOT, Settings
from server.app.domain.trace import Trace

THRESHOLD_VARS = [k.upper() for k in Settings.model_fields] + ["ENV", "ALLOW_UNGATED"]


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in THRESHOLD_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_match_spine_hypotheses() -> None:
    s = Settings(_env_file=None)
    assert s.deadline_s == 55 and s.llm_timeout_s == 25
    assert s.quote_min_chars == 25 and s.quote_min_ratio == 0.6
    assert s.max_opens == 6 and s.node_window == 30 and s.search_limit == 20 and s.max_llm_turns == 2
    assert s.max_cost_eur_per_request == 0.10 and s.cost_alert_eur == 0.05
    assert s.rate_limit_per_minute == 10 and s.rate_limit_per_day == 100
    assert s.coverage_threshold == 0.8 and s.kind_confidence_min == 0.7
    assert s.env == "dev" and s.allow_ungated is True
    # story 1.5 : pipeline guide, historique borné (AD-11), bornes de *vérifier* (AD-4)
    assert s.guide_doc_id == "lux-guide" and s.historique_max_turns == 6
    assert s.verifier_max_claims == 8 and s.verifier_max_tokens == 1024
    # story 1.8 : contrat servi par le pipeline sinistre, et les bornes de son appel groupé
    assert s.sinistre_doc_id == "axa-lu-optihome-2017"
    assert s.verifier_sinistre_max_tokens == 3072
    assert s.fait_manquant_max_chars == 200 and s.ask_client_max == 8


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
    assert {"max_opens", "node_window", "search_limit", "max_llm_attempts", "max_cost_eur_per_request",
            "rate_limit_per_minute", "rate_limit_per_day", "deadline_s",
            # story 1.4 : plafonds de sortie par étape et borne en blocs de *retrouver*
            "comprendre_max_tokens", "rediger_max_tokens", "retrieval_max_blocks",
            # story 1.5 : bornes du pipeline et de *vérifier*
            "historique_max_turns", "verifier_max_claims", "verifier_max_tokens",
            # story 1.8 : les deux bornes posées sur ce que le modèle fait afficher au sinistre
            "fait_manquant_max_chars", "ask_client_max"} <= set(t.thresholds)
    assert all(isinstance(v, (int, float)) for v in t.thresholds.values())
    # `guide_doc_id` et `sinistre_doc_id` sont des slugs, pas des seuils : ils n'ont rien à faire dans
    # `Trace.thresholds` (typé `dict[str, float | int]` — les y mettre ferait échouer la sérialisation).
    assert "guide_doc_id" not in t.thresholds and "sinistre_doc_id" not in t.thresholds


def test_allow_ungated_follows_env_unless_explicit() -> None:
    assert Settings(_env_file=None, env="prod").allow_ungated is False
    assert Settings(_env_file=None, env="prod", allow_ungated=True).allow_ungated is True
    assert Settings(_env_file=None, env="dev", allow_ungated=False).allow_ungated is False


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
    # story 1.5 : *vérifier* doit pouvoir juger tout ce que *rédiger* peut produire, sinon des claims
    # retrouvées seraient rejetées « non évaluées » par pure configuration (dégradé silencieux).
    with pytest.raises(ValidationError, match="verifier_max_claims"):
        Settings(_env_file=None, verifier_max_claims=2, draft_max_claims=4)
    Settings(_env_file=None, verifier_max_claims=4, draft_max_claims=4)
    for bad in ({"deadline_s": 0}, {"quote_min_ratio": 1.5}, {"max_opens": 0}, {"max_cost_eur_per_request": -1},
                {"rate_limit_per_day": 0}):
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
