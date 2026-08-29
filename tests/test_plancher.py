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
from server.app.domain.ingest import Gate, GateContext, GateDecision, ManifestEntry
from server.app.llm.client import LlmClient
from server.app.domain.trace import Usage
from server.evals.plancher import (Configuration, PlancherInvalide, charger_plancher,
                                   classer_configurations, PLANCHER_PATH)
from server.evals.campaign import CampaignLedger, CampaignLedgerError


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
        assert temoin.plancher >= 0 and temoin.n >= charge.plancher.n_minimum
        assert temoin.numerateur.strip() and temoin.denominateur.strip() and temoin.incident.strip()
    # Le floor 4.2a est importé sans diminution : les quatre témoins existent, au moins à 1.0 / n>=3.
    for metric in ("offline_tests_pass_rate", "bougie_post_success_rate",
                   "a16_post_success_rate", "decision_claim_rate"):
        temoin = charge.plancher.temoin(metric)
        assert temoin is not None, f"témoin {metric} du floor 4.2a absent"
        assert temoin.plancher >= 1.0 and temoin.n >= 3
    # La règle trusted fait foi : budget agrégé 1,00 €, variable LIVE_BUDGET_EUR.
    assert charge.plancher.budget.default_eur == 1.00
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


def test_le_plancher_se_charge_hors_du_depot_parent(tmp_path: Path) -> None:
    reference = tmp_path / "produit-autonome" / "server" / "evals" / "reference"
    reference.mkdir(parents=True)
    for nom in ("plancher.yaml", "floor-4.2a.yaml", "trusted-automation-plancher.yaml"):
        (reference / nom).write_bytes((PLANCHER_PATH.parent / nom).read_bytes())
    assert charger_plancher(reference / "plancher.yaml").plancher.story == "4.2b"
    with pytest.raises(PlancherInvalide, match="preuve non vérifiable"):
        charger_plancher(reference / "plancher.yaml", producer="orchestrator")


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


def test_abaisser_import_et_temoin_ensemble_reste_refuse(tmp_path: Path) -> None:
    """L'import n'est pas sa propre autorité : la source parent reste le plancher de comparaison."""
    def _abaisse_les_deux(brut: dict) -> None:
        brut["imports"]["floor_4_2a"]["thresholds"]["bougie_post_success_rate"] = 0.2
        for temoin in brut["temoins"]:
            if temoin["metric"] == "bougie_post_success_rate":
                temoin["plancher"] = 0.2

    with pytest.raises(PlancherInvalide, match="snapshot figé|source d'autorité"):
        charger_plancher(_plancher_modifie(tmp_path, _abaisse_les_deux))


def test_diminuer_le_budget_trusted_est_refuse(tmp_path: Path) -> None:
    def _change(brut: dict) -> None:
        brut["budget"]["default_eur"] = 0.50

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
    assert _settings().live_budget_eur == 1.00
    assert _settings().thresholds()["live_budget_eur"] == 1.00
    monkeypatch.setenv("LIVE_BUDGET_EUR", "0.25")
    assert Settings(_env_file=None).live_budget_eur == 0.25


def test_ledger_persiste_le_cout_et_refuse_une_seconde_baseline(tmp_path: Path) -> None:
    with CampaignLedger(tmp_path, campaign_id="story-4.2b", budget_eur=1.0) as ledger:
        ledger.register_series(kind="baseline", series_id="baseline-A",
                               witnesses=["s-temoin"], max_series=1)
        ledger.record_cost(0.123456)
        assert ledger.accrued_cost_eur == pytest.approx(0.123456)
    with CampaignLedger(tmp_path, campaign_id="story-4.2b", budget_eur=1.0) as ledger:
        assert ledger.accrued_cost_eur == pytest.approx(0.123456)
        ledger.register_series(kind="baseline", series_id="baseline-A",
                               witnesses=["s-temoin"], max_series=1)
        with pytest.raises(CampaignLedgerError, match="seconde série baseline"):
            ledger.register_series(kind="baseline", series_id="baseline-B",
                                   witnesses=["s-temoin"], max_series=1)


def test_ledger_verrouille_les_processus_concurrents(tmp_path: Path) -> None:
    premier = CampaignLedger(tmp_path, campaign_id="story-4.2b", budget_eur=1.0)
    with premier:
        with pytest.raises(CampaignLedgerError, match="déjà active"):
            with CampaignLedger(tmp_path, campaign_id="story-4.2b", budget_eur=1.0):
                pass


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
    """Une entrée de manifest gatée. Sous `full`, le gate porte son protocole (story 4.5)."""
    digest = "a" * 64
    complet = {"plancher_digest": "b" * 64, "candidate_revision": "c" * 40,
               "report_digest": "d" * 64} if profile == "full" else {}
    return ManifestEntry(
        status="servi", source_hash="s", ingest_fingerprint="f", document_hash="d", edition="e",
        gate=Gate(profile=profile, source_hash="s", ingest_fingerprint="f", overlay_hash=None,
                  cases_hash="c", cases=1, countersigned=False, pipeline_digest="ancien",
                  prompts_digest="ancien", model_ids={"micro": "m"}, evals_ok=True,
                  date="2026-08-28", run_digest=digest, **complet,
                  decisions=[GateDecision(
                      metric="temoin", producer="orchestrator", threshold=1.0,
                      scope="run", n=3, run_digest=digest, value=1.0, status="green")]))


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


def test_un_changement_de_tier_perime_un_gate_full() -> None:
    entree = _entry("full")
    assert entree.gate is not None
    entree.gate.pipeline_settings = {"rediger_tier_reason": 1}
    courant = GateContext(pipeline_digest="ancien", prompts_digest="ancien",
                          model_ids={"micro": "m"},
                          pipeline_settings={"rediger_tier_reason": 0})
    assert _gate_alerts(entree, courant, allow_ungated=False) == ("gate_perime", [])


def test_un_gate_full_preprotocole_est_mis_en_quarantaine() -> None:
    entree = _entry("full")
    assert entree.gate is not None
    entree.gate.decisions = []
    entree.gate.run_digest = None
    assert _gate_alerts(entree, None, allow_ungated=False) == ("gate_preprotocole", [])


# --- story 4.2e : la règle de classement, prouvée sur une table synthétique de mesures -------------

# Six configurations mesurées, dont deux seulement passent le plancher. Les chiffres sont choisis
# pour que **chaque** ordre naïf se trompe : la moins chère et la plus rapide du lot sont toutes deux
# inadmissibles, et l'admissible la moins chère est aussi la plus lente des admissibles.
TABLE_MESUREE = [
    Configuration(name="a-sous-plancher-gratuite", admissible=False, cost_eur=0.000, latency_ms=1),
    Configuration(name="b-admissible-lente", admissible=True, cost_eur=0.010, latency_ms=9_000),
    Configuration(name="c-sous-plancher-instantanee", admissible=False, cost_eur=0.001, latency_ms=2),
    Configuration(name="d-admissible-rapide", admissible=True, cost_eur=0.050, latency_ms=100),
    Configuration(name="e-sous-plancher-chere", admissible=False, cost_eur=9.999, latency_ms=99_000),
    Configuration(name="f-admissible-egale", admissible=True, cost_eur=0.010, latency_ms=8_000),
]


def test_une_configuration_sous_le_plancher_nest_jamais_devant_une_admissible() -> None:
    """Aucune dépense ni aucune latence ne rachète le plancher — c'est la première clé du tri.

    La preuve porte sur la **table**, pas sur un exemple : la plus rapide *et* la moins chère du lot
    sont inadmissibles, si bien qu'un classement qui commencerait par le coût ou par la latence les
    remonterait en tête.
    """
    classement = classer_configurations(TABLE_MESUREE)
    admissibles = [c.name for c in classement if c.admissible]
    inadmissibles = [c.name for c in classement if not c.admissible]
    positions = {c.name: rang for rang, c in enumerate(classement)}

    assert len(admissibles) == 3 and len(inadmissibles) == 3
    assert max(positions[n] for n in admissibles) < min(positions[n] for n in inadmissibles)
    # La moins chère du lot et la plus rapide du lot sont inadmissibles : elles restent derrière.
    moins_chere = min(TABLE_MESUREE, key=lambda c: c.cost_eur)
    plus_rapide = min(TABLE_MESUREE, key=lambda c: c.latency_ms)
    assert not moins_chere.admissible and not plus_rapide.admissible
    assert positions[moins_chere.name] >= len(admissibles)
    assert positions[plus_rapide.name] >= len(admissibles)


def test_le_departage_economique_nopere_quentre_admissibles() -> None:
    """Coût puis latence, et seulement au sein du groupe admissible : aucune pondération croisée."""
    classement = classer_configurations(TABLE_MESUREE)
    admissibles = [c for c in classement if c.admissible]

    # Coût croissant d'abord ; à coût égal, latence croissante ; à égalité parfaite, le nom.
    assert [c.name for c in admissibles] == ["f-admissible-egale", "b-admissible-lente",
                                             "d-admissible-rapide"]
    assert [c.cost_eur for c in admissibles] == sorted(c.cost_eur for c in admissibles)
    # L'admissible la moins chère est la plus lente des deux à coût égal : elle passe quand même
    # devant la plus rapide du groupe, qui coûte cinq fois plus.
    assert admissibles[0].latency_ms > admissibles[-1].latency_ms

    # Le classement des inadmissibles entre eux suit la même règle — il ne les promeut jamais.
    inadmissibles = [c.name for c in classement if not c.admissible]
    assert inadmissibles == ["a-sous-plancher-gratuite", "c-sous-plancher-instantanee",
                             "e-sous-plancher-chere"]


def test_le_classement_ne_depend_pas_de_lordre_dentree() -> None:
    """Une règle mécanique : la même table, mélangée, rend le même classement (départage par nom)."""
    attendu = [c.name for c in classer_configurations(TABLE_MESUREE)]
    for depart in range(len(TABLE_MESUREE)):
        permutee = TABLE_MESUREE[depart:] + TABLE_MESUREE[:depart]
        assert [c.name for c in classer_configurations(permutee)] == attendu


# --- M2 (story 4.5) : la preuve trusted est liée à la révision qu'elle mesure ----------------------
#
# Reproduction demandée mot pour mot par l'entrée différée : « construire un rapport orchestrateur
# pour une révision A, modifier la révision produit sans changer les autres digests, puis vérifier
# que `pytest -q tests/test_plancher.py -k candidate_revision` refuse sa réutilisation ».
#
# Sans ce lien, une preuve externe — qui porte des mesures que le runner ne fait pas lui-même :
# tests hors ligne, A16, gardes anti-rustine et métamorphique — pouvait être rejouée sur n'importe
# quel commit ultérieur. Elle validait alors un code qu'elle n'avait jamais vu, et les digests de
# protocole seuls n'y voyaient rien : ils décrivent le plancher, pas le produit.

REVISION_A_candidate_revision = "a" * 40
REVISION_B_candidate_revision = "b" * 40


def _preuve_candidate_revision(rapport: Path, **kw: object) -> dict:
    import hashlib
    import json as _json

    identite = _json.loads(rapport.read_text(encoding="utf-8")).get("identity") or {}
    run_digest = str(identite.get("run_digest", "c" * 64))
    base = {
        "plancher_digest": charger_plancher().digest,
        "candidate_revision": REVISION_A_candidate_revision,
        "report_digest": hashlib.sha256(rapport.read_bytes()).hexdigest(),
        "run_digest": run_digest,
        "decisions": [{"metric": "offline_tests_pass_rate", "n": 3, "value": 1.0,
                       "run_digest": run_digest}],
    }
    base.update(kw)  # type: ignore[arg-type]
    return base


def _identite_candidate_revision(**champs: object) -> dict:
    """Une identité de run **cohérente** : son `run_digest` est celui que le runner calculerait.

    `run.identite_run` définit `run_digest` comme l'empreinte canonique de l'identité privée de sa
    propre clé. Une identité dont le digest est posé à la main est un rapport fabriqué — et c'est
    exactement ce que la liaison doit refuser (revue B1).
    """
    from server.evals.cache import empreinte_canonique

    identite: dict = {"candidate_revision": REVISION_A_candidate_revision,
                      "image": {}, "scope": {}}
    identite.update(champs)
    identite["run_digest"] = empreinte_canonique(
        {cle: valeur for cle, valeur in identite.items() if cle != "run_digest"})
    return identite


def _ecrire_rapport_candidate_revision(chemin: Path, **champs: object) -> dict:
    import json as _json

    identite = _identite_candidate_revision(**champs)
    chemin.write_text(_json.dumps({
        "schema_version": 3, "decisions": [], "identity": identite}) + "\n", encoding="utf-8")
    return identite


@pytest.fixture
def rapport_candidate_revision(tmp_path: Path) -> Path:
    """Un rapport qui **se reconnaît** dans la preuve : son identité porte le run et la révision.

    Recouper les seuls octets prouvait que le fichier n'avait pas bougé, jamais qu'il décrivait ce
    run-là : une preuve pouvait référencer le rapport d'un autre run, d'une autre révision.
    """
    chemin = tmp_path / "rapport.json"
    _ecrire_rapport_candidate_revision(chemin)
    return chemin


def test_la_preuve_nominale_liee_a_la_candidate_revision_est_acceptee(
        rapport_candidate_revision: Path) -> None:
    """Le cas nominal : protocole, révision, rapport et run concordent — la preuve est utilisable."""
    from server.evals.plancher import verifier_liaison_preuve

    preuve = _preuve_candidate_revision(rapport_candidate_revision)
    run_digest = verifier_liaison_preuve(
        preuve, plancher_digest=charger_plancher().digest,
        candidate_revision=REVISION_A_candidate_revision,
        report_bytes=rapport_candidate_revision.read_bytes())
    assert run_digest == preuve["run_digest"] and len(run_digest) == 64


def test_une_preuve_dune_autre_candidate_revision_est_refusee(
        rapport_candidate_revision: Path) -> None:
    """La reproduction M2 : même rapport, même plancher, **autre** révision produit ⇒ refus."""
    from server.evals.plancher import verifier_liaison_preuve

    preuve = _preuve_candidate_revision(rapport_candidate_revision)
    with pytest.raises(PlancherInvalide, match="candidate_revision"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_B_candidate_revision,
            report_bytes=rapport_candidate_revision.read_bytes())
    # Une révision mal formée est refusée avant même la comparaison.
    with pytest.raises(PlancherInvalide, match="hexadécimaux"):
        verifier_liaison_preuve(
            _preuve_candidate_revision(rapport_candidate_revision, candidate_revision="court"),
            plancher_digest=charger_plancher().digest,
            candidate_revision="court", report_bytes=b"")


def test_un_report_digest_non_concordant_est_refuse_pour_la_candidate_revision(
        rapport_candidate_revision: Path) -> None:
    """Un rapport modifié après coup détache la preuve de ce qu'elle prétend résumer."""
    from server.evals.plancher import verifier_liaison_preuve

    preuve = _preuve_candidate_revision(rapport_candidate_revision)
    with pytest.raises(PlancherInvalide, match="rapport modifié"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=rapport_candidate_revision.read_bytes() + b"\n")
    # Un rapport absent n'est pas « pas de contrainte » : c'est un refus.
    with pytest.raises(PlancherInvalide, match="absent ou illisible"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision, report_bytes=None)


def test_une_cle_racine_en_trop_est_refusee_pour_la_candidate_revision(
        rapport_candidate_revision: Path) -> None:
    """Vocabulaire fermé : un fichier qui déclare en plus son propre `status` serait à moitié lu.

    Le lecteur en ignorerait la moitié, et l'auteur du fichier croirait l'avoir dit. Un vocabulaire
    fermé se contrôle par **égalité**, comme `Cas` et `Temoin`.
    """
    from server.evals.plancher import CLES_PREUVE_TRUSTED, verifier_liaison_preuve

    assert CLES_PREUVE_TRUSTED == {"plancher_digest", "candidate_revision", "report_digest",
                                   "run_digest", "decisions"}
    preuve = _preuve_candidate_revision(rapport_candidate_revision, status="green")
    with pytest.raises(PlancherInvalide, match="en trop"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=rapport_candidate_revision.read_bytes())
    manquante = _preuve_candidate_revision(rapport_candidate_revision)
    manquante.pop("run_digest")
    with pytest.raises(PlancherInvalide, match="manquantes"):
        verifier_liaison_preuve(
            manquante, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=rapport_candidate_revision.read_bytes())


def test_un_run_digest_divergent_est_refuse_pour_la_candidate_revision(
        rapport_candidate_revision: Path) -> None:
    """Les mesures d'une preuve viennent d'**un** run : une compilation n'est pas une preuve."""
    from server.evals.plancher import verifier_liaison_preuve

    preuve = _preuve_candidate_revision(rapport_candidate_revision)
    preuve["decisions"][0]["run_digest"] = "d" * 64
    with pytest.raises(PlancherInvalide, match="run_digest différent"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=rapport_candidate_revision.read_bytes())
    vide = _preuve_candidate_revision(rapport_candidate_revision, decisions=[])
    with pytest.raises(PlancherInvalide, match="liste non vide"):
        verifier_liaison_preuve(
            vide, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=rapport_candidate_revision.read_bytes())


def test_le_runner_refuse_avant_toute_decision_sur_une_candidate_revision_divergente(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 5 : « le run refuse avant toute décision, message chiffré, et aucun gate n'est écrit ».

    Le chemin complet, du fichier de preuve au code de sortie : `charger_decisions_orchestrateur`
    passe par `verifier_liaison_preuve` **avant** de lire la moindre mesure.
    """
    import json as _json

    from server.evals import run as runner

    rapport = tmp_path / "rapport.json"
    _ecrire_rapport_candidate_revision(rapport)
    preuve = tmp_path / "preuve.json"
    preuve.write_text(_json.dumps(_preuve_candidate_revision(rapport)), encoding="utf-8")
    with pytest.raises(runner.RefusDeTourner, match="candidate_revision"):
        runner.charger_decisions_orchestrateur(
            preuve, plancher=charger_plancher(),
            candidate_revision=REVISION_B_candidate_revision, report_path=rapport)
    # Et le cas nominal traverse bien : la liaison n'est pas un refus systématique.
    decisions = runner.charger_decisions_orchestrateur(
        preuve, plancher=charger_plancher(),
        candidate_revision=REVISION_A_candidate_revision, report_path=rapport)
    assert [d.metric for d in decisions] == ["offline_tests_pass_rate"]
    assert decisions[0].status == "green" and decisions[0].producer == "orchestrator"


def test_un_rapport_qui_ne_se_reconnait_pas_dans_la_preuve_est_refuse(
        rapport_candidate_revision: Path) -> None:
    """Revue B1 : les octets concordent, mais le rapport décrit un **autre** run ou une autre révision.

    Recouper le seul `report_digest` prouvait que le fichier n'avait pas bougé — pas qu'il parlait
    de ce run-là. Une preuve pouvait donc référencer le rapport d'une campagne antérieure et passer.
    """
    import hashlib
    import json as _json  # noqa: F401 — lisibilité des fixtures locales

    from server.evals.plancher import verifier_liaison_preuve

    autre_run = rapport_candidate_revision.parent / "autre-run.json"
    _ecrire_rapport_candidate_revision(autre_run, scope={"repeat": 5})
    preuve = _preuve_candidate_revision(rapport_candidate_revision)
    preuve["report_digest"] = hashlib.sha256(autre_run.read_bytes()).hexdigest()
    with pytest.raises(PlancherInvalide, match="ne décrit pas ce rapport"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=autre_run.read_bytes())

    autre_revision = rapport_candidate_revision.parent / "autre-revision.json"
    _ecrire_rapport_candidate_revision(autre_revision,
                                       candidate_revision=REVISION_B_candidate_revision)
    preuve = _preuve_candidate_revision(autre_revision)
    with pytest.raises(PlancherInvalide, match="a mesuré la révision"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=autre_revision.read_bytes())

    # Un rapport sans identité de run ne prouve rien non plus.
    muet = rapport_candidate_revision.parent / "muet.json"
    muet.write_text('{"schema_version": 3}\n', encoding="utf-8")
    with pytest.raises(PlancherInvalide, match="identité de run"):
        verifier_liaison_preuve(
            _preuve_candidate_revision(muet), plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision, report_bytes=muet.read_bytes())


def test_un_run_digest_fabrique_est_refuse_meme_sil_est_auto_coherent(
        rapport_candidate_revision: Path) -> None:
    """B1 : le `run_digest` est **recalculé**, jamais cru sur parole.

    `run.identite_run` le définit comme l'empreinte canonique de l'identité privée de sa propre clé.
    Comparer le `run_digest` de la preuve à celui que le rapport **déclare** ne prouvait donc rien :
    un rapport fabriqué pouvait annoncer n'importe quel couple auto-cohérent, et la preuve s'y
    raccrochait sans que rien ne soit établi.
    """
    import hashlib
    import json as _json

    from server.evals.plancher import verifier_liaison_preuve

    fabrique = rapport_candidate_revision.parent / "fabrique.json"
    fabrique.write_text(_json.dumps({
        "schema_version": 3, "decisions": [],
        # Auto-cohérent — la preuve annoncera ce même digest — mais **non recalculable**.
        "identity": {"run_digest": "e" * 64, "candidate_revision": REVISION_A_candidate_revision,
                     "image": {}, "scope": {}},
    }) + "\n", encoding="utf-8")
    preuve = _preuve_candidate_revision(fabrique)
    preuve["run_digest"] = "e" * 64
    preuve["decisions"][0]["run_digest"] = "e" * 64
    preuve["report_digest"] = hashlib.sha256(fabrique.read_bytes()).hexdigest()
    with pytest.raises(PlancherInvalide, match="ne se recalcule pas"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=fabrique.read_bytes())


def test_un_rapport_dun_autre_plancher_ou_dune_autre_image_est_refuse(
        rapport_candidate_revision: Path) -> None:
    """B1 : deux runs ne se comparent qu'à protocole et image égaux.

    Le rapport porte son `plancher_digest` à **deux** emplacements (racine et `identity.image`) et
    les digests de l'image mesurée. Ni l'un ni l'autre n'était confronté au run courant : une preuve
    tirée d'un autre plancher, ou d'un autre code, passait.
    """
    import hashlib
    import json as _json

    from server.evals.plancher import verifier_liaison_preuve

    courant = charger_plancher().digest

    def _verifier(chemin: Path, **kw: object) -> None:
        preuve = _preuve_candidate_revision(chemin)
        preuve["report_digest"] = hashlib.sha256(chemin.read_bytes()).hexdigest()
        verifier_liaison_preuve(preuve, plancher_digest=courant,
                                candidate_revision=REVISION_A_candidate_revision,
                                report_bytes=chemin.read_bytes(), **kw)  # type: ignore[arg-type]

    # 1. Plancher divergent à la racine du rapport.
    racine = rapport_candidate_revision.parent / "autre-plancher.json"
    identite = _identite_candidate_revision()
    racine.write_text(_json.dumps({
        "schema_version": 3, "decisions": [], "identity": identite,
        "plancher_digest": "f" * 64}) + "\n", encoding="utf-8")
    with pytest.raises(PlancherInvalide, match="racine du rapport"):
        _verifier(racine)

    # 2. Plancher divergent dans l'identité d'image.
    image = rapport_candidate_revision.parent / "autre-plancher-image.json"
    identite = _identite_candidate_revision(image={"plancher_digest": "f" * 64})
    image.write_text(_json.dumps({
        "schema_version": 3, "decisions": [], "identity": identite}) + "\n", encoding="utf-8")
    with pytest.raises(PlancherInvalide, match="identity.image"):
        _verifier(image)

    # 3. Image divergente : un autre code, d'autres prompts, d'autres modèles.
    autre_code = rapport_candidate_revision.parent / "autre-code.json"
    identite = _identite_candidate_revision(image={"pipeline_digest": "1" * 64})
    autre_code.write_text(_json.dumps({
        "schema_version": 3, "decisions": [], "identity": identite}) + "\n", encoding="utf-8")
    with pytest.raises(PlancherInvalide, match="ne mesure pas cette image"):
        _verifier(autre_code, image_courante={"pipeline_digest": "2" * 64})
    # La même image : accepté.
    _verifier(autre_code, image_courante={"pipeline_digest": "1" * 64})
