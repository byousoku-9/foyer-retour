"""Story 4.2b — Le plancher pré-enregistré : chargement, digest, non-diminution, règle mécanique.

Et les deux gardes qui l'entourent : le budget de campagne du client (`LIVE_BUDGET_EUR`) et la
quarantaine des digests non concordants sous gate `full`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from server.app.config import Settings
from server.app.corpus.loader import _gate_alerts
from server.app.domain.errors import BudgetExceeded
from server.app.domain.ingest import Gate, GateContext, ManifestEntry
from server.app.llm.client import LlmClient
from server.app.domain.trace import Usage
from server.evals.plancher import (Configuration, PlancherInvalide, charger_plancher,
                                   classer_configurations, PLANCHER_PATH)


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


# --- chargement et digest --------------------------------------------------------------------------

def test_le_plancher_livre_se_charge_et_porte_son_digest() -> None:
    """AC 4.2b : chaque témoin porte plancher/N/numérateur/dénominateur/règles d'incident."""
    charge = charger_plancher()
    assert len(charge.digest) == 64 and int(charge.digest, 16)
    assert charge.plancher.n_minimum >= 3
    assert charge.plancher.producer_de_preuve == "orchestrator"
    for temoin in charge.plancher.temoins:
        assert temoin.plancher >= 0 and temoin.n >= 1
        assert temoin.numerateur.strip() and temoin.denominateur.strip() and temoin.incident.strip()
    # Le floor 4.2a est importé sans diminution : les quatre témoins existent, au moins à 1.0 / n>=3.
    for metric in ("offline_tests_pass_rate", "bougie_post_success_rate",
                   "a16_post_success_rate", "decision_claim_rate"):
        temoin = charge.plancher.temoin(metric)
        assert temoin is not None, f"témoin {metric} du floor 4.2a absent"
        assert temoin.plancher >= 1.0 and temoin.n >= 3
    # La règle trusted fait foi : budget 0,50 €, variable LIVE_BUDGET_EUR, refus sans question.
    assert charge.plancher.budget.default_eur == 0.50
    assert charge.plancher.budget.environment_variable == "LIVE_BUDGET_EUR"
    assert charge.plancher.budget.on_exceeded.get("ask_first") is False
    # Les trois splits sont pré-enregistrés, B hors dépôt, C jamais exécuté.
    assert set(charge.plancher.splits) >= {"A", "B", "C"}


def test_le_digest_change_avec_le_fichier(tmp_path: Path) -> None:
    copie = tmp_path / "plancher.yaml"
    copie.write_bytes(PLANCHER_PATH.read_bytes())
    original = charger_plancher(copie)
    copie.write_bytes(PLANCHER_PATH.read_bytes() + b"\n# commentaire\n")
    assert charger_plancher(copie).digest != original.digest


def _plancher_modifie(tmp_path: Path, mutation) -> Path:
    brut = yaml.safe_load(PLANCHER_PATH.read_text("utf-8"))
    mutation(brut)
    copie = tmp_path / "plancher.yaml"
    copie.write_text(yaml.safe_dump(brut, allow_unicode=True), "utf-8")
    return copie


def test_abaisser_un_seuil_du_floor_4_2a_est_refuse(tmp_path: Path) -> None:
    """Block If 4.2b : abaisser un seuil du plancher est un refus, jamais une approximation."""
    def _abaisse(brut: dict) -> None:
        for temoin in brut["temoins"]:
            if temoin["metric"] == "bougie_post_success_rate":
                temoin["plancher"] = 0.9

    with pytest.raises(PlancherInvalide, match="floor 4.2a"):
        charger_plancher(_plancher_modifie(tmp_path, _abaisse))


def test_retirer_un_temoin_du_floor_est_refuse(tmp_path: Path) -> None:
    def _retire(brut: dict) -> None:
        brut["temoins"] = [t for t in brut["temoins"] if t["metric"] != "a16_post_success_rate"]

    with pytest.raises(PlancherInvalide, match="retirer un témoin est interdit"):
        charger_plancher(_plancher_modifie(tmp_path, _retire))


def test_diminuer_le_budget_trusted_est_refuse(tmp_path: Path) -> None:
    def _change(brut: dict) -> None:
        brut["budget"]["default_eur"] = 1.00

    with pytest.raises(PlancherInvalide, match="trusted"):
        charger_plancher(_plancher_modifie(tmp_path, _change))


def test_un_plancher_illisible_est_un_refus(tmp_path: Path) -> None:
    absent = tmp_path / "absent.yaml"
    with pytest.raises(PlancherInvalide):
        charger_plancher(absent)
    (tmp_path / "casse.yaml").write_text("- pas: [un objet", "utf-8")
    with pytest.raises(PlancherInvalide):
        charger_plancher(tmp_path / "casse.yaml")


# --- la règle mécanique du checkpoint --------------------------------------------------------------

def test_le_classement_est_admissible_puis_moins_cher_puis_plus_rapide() -> None:
    configurations = [
        Configuration(name="chere-rapide", admissible=True, cost_eur=0.09, latency_ms=100),
        Configuration(name="inadmissible-gratuite", admissible=False, cost_eur=0.0, latency_ms=1),
        Configuration(name="economique-lente", admissible=True, cost_eur=0.03, latency_ms=900),
        Configuration(name="economique-rapide", admissible=True, cost_eur=0.03, latency_ms=200),
    ]
    classement = [c.name for c in classer_configurations(configurations)]
    assert classement == ["economique-rapide", "economique-lente", "chere-rapide",
                          "inadmissible-gratuite"]


def test_aucun_admissible_est_un_rouge_publie() -> None:
    """Boundaries 4.2b : `aucun_admissible` est un résultat rouge, jamais une question humaine."""
    classement = classer_configurations([
        Configuration(name="seule", admissible=False, cost_eur=0.01, latency_ms=10)])
    assert not any(c.admissible for c in classement)


# --- budget de campagne du client (LIVE_BUDGET_EUR) ------------------------------------------------

def test_le_client_refuse_lappel_qui_deborde_la_campagne() -> None:
    client = LlmClient(_settings(anthropic_api_key="cle-de-test"), anthropic_client=object(),
                       campaign_budget_eur=0.10)
    client._noter_campagne(Usage(cost_eur=0.08))
    assert client.campaign_cost_eur == 0.08
    with pytest.raises(BudgetExceeded) as exc:
        client._refuser_hors_campagne(0.05)
    message = str(exc.value)
    # Les trois chiffres du rapport trusted, avant tout appel — jamais une question.
    assert "configured_budget_eur=0.1000" in message
    assert "accrued_cost_eur=0.0800" in message
    assert "refused_cost_eur=0.0500" in message
    # Sous le budget, aucun refus ; sans budget de campagne (serveur HTTP), jamais de refus.
    client._refuser_hors_campagne(0.01)
    LlmClient(_settings(anthropic_api_key="cle-de-test"),
              anthropic_client=object())._refuser_hors_campagne(10.0)


def test_live_budget_vit_dans_config_et_suit_lenvironnement(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings().live_budget_eur == 0.50
    assert _settings().thresholds()["live_budget_eur"] == 0.50
    monkeypatch.setenv("LIVE_BUDGET_EUR", "0.25")
    assert Settings(_env_file=None).live_budget_eur == 0.25


def test_les_tiers_par_etape_sont_pilotables_et_publies() -> None:
    reglages = _settings(comprendre_tier="reason", rediger_tier="micro", verifier_tier="reason")
    seuils = reglages.thresholds()
    assert seuils["comprendre_tier_reason"] == 1
    assert seuils["rediger_tier_reason"] == 0
    assert seuils["verifier_tier_reason"] == 1
    # Les défauts restent l'affectation d'AD-9.
    defauts = _settings().thresholds()
    assert (defauts["comprendre_tier_reason"], defauts["rediger_tier_reason"],
            defauts["verifier_tier_reason"]) == (0, 1, 0)


# --- digests non concordants sous gate full : quarantaine ------------------------------------------

def _entry(profile: str) -> ManifestEntry:
    return ManifestEntry(
        status="servi", source_hash="s", ingest_fingerprint="f", document_hash="d", edition="e",
        gate=Gate(profile=profile, source_hash="s", ingest_fingerprint="f", overlay_hash=None,
                  cases_hash="c", cases=1, countersigned=False, pipeline_digest="ancien",
                  prompts_digest="ancien", model_ids={"micro": "m"}, evals_ok=True,
                  date="2026-08-28"))


def test_digests_divergents_sous_full_mettent_le_document_en_quarantaine() -> None:
    """AC 4.2b : la non-concordance sous `full` met le document en quarantaine, pas en alerte."""
    courant = GateContext(pipeline_digest="nouveau", prompts_digest="nouveau",
                          model_ids={"micro": "m"})
    raison, alertes = _gate_alerts(_entry("full"), courant, allow_ungated=False)
    assert raison == "gate_perime" and alertes == []


def test_digests_divergents_sous_vertical_restent_une_alerte() -> None:
    courant = GateContext(pipeline_digest="nouveau", prompts_digest="nouveau",
                          model_ids={"micro": "m"})
    raison, alertes = _gate_alerts(_entry("vertical"), courant, allow_ungated=False)
    assert raison == "" and alertes == ["gate_perime"]


def test_digests_concordants_ne_changent_rien() -> None:
    courant = GateContext(pipeline_digest="ancien", prompts_digest="ancien",
                          model_ids={"micro": "m"})
    for profile in ("vertical", "full"):
        assert _gate_alerts(_entry(profile), courant, allow_ungated=False) == ("", [])
