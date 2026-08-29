"""Story 4.2b — Le plancher pré-enregistré : chargement, digest, non-diminution, règle mécanique.

Et les deux gardes qui l'entourent : le budget de campagne du client (`LIVE_BUDGET_EUR`) et la
quarantaine des digests non concordants sous gate `full`.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
import yaml

from server.app.config import Settings
from server.app.corpus.loader import _gate_alerts
from server.app.domain.errors import BudgetExceeded
from server.app.domain.ingest import Gate, GateContext, GateDecision, ManifestEntry
from server.app.llm.client import LlmClient
from server.app.domain.trace import Usage
from server.app.config import REPO_ROOT
from server.evals.plancher import (CandidatClassement, ClassementInvalide, ClassementOppose,
                                   Configuration, PlancherInvalide, charger_plancher,
                                   classer_configurations, PLANCHER_PATH)
from server.evals.revision import ARBRE_NON_VERIFIABLE, revision_executee
from server.evals.campaign import CampaignLedger, CampaignLedgerError


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


# La révision candidate commune des tables de classement de ce fichier — **celle du checkout**.
#
# Elle valait `"a" * 40` jusqu'au tour correctif 2/3, et les fixtures consacraient donc exactement le
# défaut B1 : un rapport auto-cohérent fabriqué pour un commit qui n'existe dans aucun dépôt était
# accepté, admissible et classé en tête. Depuis, `classer_configurations` oppose la révision au
# checkout réellement exécuté ; une table de classement ne peut donc plus se réclamer d'un commit
# fantôme, et ces fixtures sont **refaites**, pas complétées.
REVISION_DU_CHECKOUT, ARBRE_DU_CHECKOUT = revision_executee(REPO_ROOT)
REVISION_CLASSEE = REVISION_DU_CHECKOUT or "a" * 40


def _exige_un_checkout_opposable() -> None:
    """Le classement n'est exerçable que là où une révision de checkout peut être établie.

    Ce n'est pas une assertion affaiblie mais une **condition d'environnement** : une copie extraite
    par `git archive`, ou une image sans `.git` ni `GIT_SHA`, ne porte aucune révision à opposer, et
    `classer_configurations` y refuse — à juste titre. Le dire par un `skip` explicite vaut mieux
    que de faire passer la sonde par un refus qui ne mesure pas ce qu'elle vise.
    """
    if REVISION_DU_CHECKOUT is None:
        pytest.skip("aucune révision de checkout à opposer (dépôt absent) : le classement refuse "
                    "ici pour une raison d'environnement, pas pour celle que ce test mesure")
    if ARBRE_NON_VERIFIABLE in ARBRE_DU_CHECKOUT:
        pytest.skip("l'état de l'arbre n'a pas pu être lu : le classement refuse ici pour une "
                    "raison d'environnement")

# Sentinelle des fabriques : « ce champ n'est pas fourni » se distingue de « ce champ vaut None ».
_ABSENT = object()


def _config(name: str, *, admissible: bool, cost_eur: float, latency_ms: int,
            candidate_revision: str = REVISION_CLASSEE,
            run_digest: str | None = None,
            report_digest: str | None = None) -> Configuration:
    """Une configuration **résultat** complète — l'identité est dérivée du nom, jamais absente.

    Depuis le tour correctif 1/3, `Configuration` n'est plus une entrée de classement : c'est ce que
    `classer_configurations` produit en recalculant les empreintes depuis les octets d'un rapport.
    Ce constructeur ne sert donc plus qu'à éprouver le **modèle** lui-même.

    Dériver `run_digest` et `report_digest` du nom garde les tables lisibles tout en donnant à
    chaque configuration une identité **distincte** et bien formée : deux configurations d'un même
    classement viennent de deux runs différents.
    """
    import hashlib

    empreinte = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return Configuration(
        name=name, admissible=admissible, cost_eur=cost_eur, latency_ms=latency_ms,
        candidate_revision=candidate_revision,
        run_digest=run_digest if run_digest is not None else empreinte,
        report_digest=(report_digest if report_digest is not None
                       else hashlib.sha256(empreinte.encode("utf-8")).hexdigest()))


# --- ce qu'un classement accepte : des **octets de rapport**, opposés à des références ancrées ----
#
# `classer_configurations` n'accepte plus de `Configuration` déclaratives, et ne reçoit plus non plus
# le plancher ni l'image auxquels elle les oppose : elle les **dérive du processus**. Les rapports
# ci-dessous portent donc l'image et le plancher **réels** du dépôt — c'est la seule façon d'exercer
# le chemin que le produit emprunte. Un rapport bâti sur une image synthétique est précisément le
# contre-exemple de P1, et il a son test.

@functools.cache
def _image_reelle_figee() -> str:
    """L'image que le classement dérive du processus, sérialisée une fois pour toutes les fixtures.

    Mémoïsée parce que `pipeline_digest()` et `prompts_digest()` relisent les sources du dépôt : une
    table de six candidats la reconstruirait six fois par test.
    """
    import json as _json

    from server.evals.plancher import image_du_depot

    return _json.dumps(image_du_depot(charger_plancher().digest), sort_keys=True)


def _image_reelle() -> dict:
    import json as _json

    return _json.loads(_image_reelle_figee())


def _octets_de_rapport(*, nom: str, admissible: bool = True, cost_eur: float = 0.0,
                       latency_ms: int = 0, revision: str = REVISION_CLASSEE,
                       run_digest: object = _ABSENT,
                       image: dict | None = None,
                       plancher_racine: object = _ABSENT,
                       identite: dict | None = None,
                       **racine: object) -> bytes:
    """Les octets d'un rapport candidat — identité **recalculée**, sauf demande contraire.

    Tous les leviers servent aux contre-exemples : `run_digest` pose une empreinte à la main, `image`
    et `plancher_racine` fabriquent une identité externe, `identite` remplace l'identité entière, et
    `racine` surcharge ou retire n'importe quelle clé du corps. Le nominal, lui, ne passe aucun de
    ces arguments.
    """
    import json as _json

    from server.evals.cache import empreinte_canonique

    if identite is None:
        identite = {"candidate_revision": revision,
                    "image": _image_reelle() if image is None else image,
                    "scope": {"nom": nom}}
        identite["run_digest"] = (empreinte_canonique(identite) if run_digest is _ABSENT
                                  else run_digest)
    corps: dict = {
        "schema_version": 3, "complete": True, "unexecuted_cases": [],
        "cost_eur": cost_eur, "metrics": {"latency_p50_ms": latency_ms},
        "decisions": [{"status": "green" if admissible else "red", "producer": "orchestrator"}],
        "identity": identite,
        "plancher_digest": (charger_plancher().digest if plancher_racine is _ABSENT
                            else plancher_racine),
    }
    for cle, valeur in racine.items():
        if valeur is _ABSENT:
            corps.pop(cle, None)
        else:
            corps[cle] = valeur
    return (_json.dumps(corps) + "\n").encode("utf-8")


def _candidat(name: str, **kw: object) -> CandidatClassement:
    return CandidatClassement(name=name, report_bytes=_octets_de_rapport(nom=name, **kw))  # type: ignore[arg-type]


def _classement(candidats: object, *, revision: str = REVISION_CLASSEE) -> ClassementOppose:
    """Le classement, tel que le produit l'appelle : **aucune référence n'est passée** (revue P1).

    Il rend son ordre **et** l'identité qu'il a effectivement opposée (revue B1, volet TOCTOU).
    """
    _exige_un_checkout_opposable()
    return classer_configurations(candidats, candidate_revision=revision)


def _classer(candidats: object, *, revision: str = REVISION_CLASSEE) -> list[Configuration]:
    """Le seul ordre, pour les tests qui n'éprouvent que la règle de tri."""
    return _classement(candidats, revision=revision).configurations


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
    candidats = [
        _candidat("chere-rapide", admissible=True, cost_eur=0.09, latency_ms=100),
        _candidat("inadmissible-gratuite", admissible=False, cost_eur=0.0, latency_ms=1),
        _candidat("economique-lente", admissible=True, cost_eur=0.03, latency_ms=900),
        _candidat("economique-rapide", admissible=True, cost_eur=0.03, latency_ms=200),
    ]
    classement = [c.name for c in _classer(candidats)]
    assert classement == ["economique-rapide", "economique-lente", "chere-rapide",
                          "inadmissible-gratuite"]


def test_aucun_admissible_est_un_rouge_publie() -> None:
    """Boundaries 4.2b : `aucun_admissible` est un résultat rouge, jamais une question humaine."""
    classement = _classer([_candidat("seule", admissible=False, cost_eur=0.01, latency_ms=10)])
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
MESURES_TABLE: tuple[tuple[str, bool, float, int], ...] = (
    ("a-sous-plancher-gratuite", False, 0.000, 1),
    ("b-admissible-lente", True, 0.010, 9_000),
    ("c-sous-plancher-instantanee", False, 0.001, 2),
    ("d-admissible-rapide", True, 0.050, 100),
    ("e-sous-plancher-chere", False, 9.999, 99_000),
    ("f-admissible-egale", True, 0.010, 8_000),
)


def _table_mesuree(*, revision: str = REVISION_CLASSEE) -> list[CandidatClassement]:
    """Les six candidats de la table, sous forme d'octets de rapports.

    Une fonction et non une constante : les octets dépendent du plancher chargé et de l'image
    synthétique, dont les fabriques vivent plus bas dans ce fichier.
    """
    return [_candidat(nom, admissible=admissible, cost_eur=cout, latency_ms=latence,
                      revision=revision)
            for nom, admissible, cout, latence in MESURES_TABLE]



def test_une_configuration_sous_le_plancher_nest_jamais_devant_une_admissible() -> None:
    """Aucune dépense ni aucune latence ne rachète le plancher — c'est la première clé du tri.

    La preuve porte sur la **table**, pas sur un exemple : la plus rapide *et* la moins chère du lot
    sont inadmissibles, si bien qu'un classement qui commencerait par le coût ou par la latence les
    remonterait en tête.
    """
    classement = _classer(_table_mesuree())
    admissibles = [c.name for c in classement if c.admissible]
    inadmissibles = [c.name for c in classement if not c.admissible]
    positions = {c.name: rang for rang, c in enumerate(classement)}

    assert len(admissibles) == 3 and len(inadmissibles) == 3
    assert max(positions[n] for n in admissibles) < min(positions[n] for n in inadmissibles)
    # La moins chère du lot et la plus rapide du lot sont inadmissibles : elles restent derrière.
    moins_chere = min(classement, key=lambda c: c.cost_eur)
    plus_rapide = min(classement, key=lambda c: c.latency_ms)
    assert not moins_chere.admissible and not plus_rapide.admissible
    assert positions[moins_chere.name] >= len(admissibles)
    assert positions[plus_rapide.name] >= len(admissibles)


def test_le_departage_economique_nopere_quentre_admissibles() -> None:
    """Coût puis latence, et seulement au sein du groupe admissible : aucune pondération croisée."""
    classement = _classer(_table_mesuree())
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
    table = _table_mesuree()
    attendu = [c.name for c in _classer(table)]
    for depart in range(len(table)):
        permutee = table[depart:] + table[:depart]
        assert [c.name for c in _classer(permutee)] == attendu


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


IMAGE_CANDIDATE = {
    "pipeline_digest": "1" * 64,
    "prompts_digest": "2" * 64,
    "model_ids": {"fast": "modele-test"},
    "normalize_version": "v-test",
    "plancher_digest": None,  # renseigné à l'appel : c'est le plancher chargé
}


def _image_candidate(**surcharges: object) -> dict:
    """L'image **complète** du run courant : cinq champs, jamais un sous-ensemble (revue B1)."""
    image = dict(IMAGE_CANDIDATE)
    image["plancher_digest"] = charger_plancher().digest
    image.update(surcharges)
    return image


def _identite_candidate_revision(**champs: object) -> dict:
    """Une identité de run **cohérente** : son `run_digest` est celui que le runner calculerait.

    `run.identite_run` définit `run_digest` comme l'empreinte canonique de l'identité privée de sa
    propre clé. Une identité dont le digest est posé à la main est un rapport fabriqué — et c'est
    exactement ce que la liaison doit refuser (revue B1).
    """
    from server.evals.cache import empreinte_canonique

    identite: dict = {"candidate_revision": REVISION_A_candidate_revision,
                      "image": _image_candidate(), "scope": {}}
    identite.update(champs)
    identite["run_digest"] = empreinte_canonique(
        {cle: valeur for cle, valeur in identite.items() if cle != "run_digest"})
    return identite


def _ecrire_rapport_candidate_revision(chemin: Path, *, plancher_racine: str | None = None,
                                       **champs: object) -> dict:
    import json as _json

    identite = _identite_candidate_revision(**champs)
    chemin.write_text(_json.dumps({
        "schema_version": 3, "decisions": [], "identity": identite,
        "plancher_digest": plancher_racine if plancher_racine is not None
        else charger_plancher().digest,
    }) + "\n", encoding="utf-8")
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
    verifiee = verifier_liaison_preuve(
        preuve, plancher_digest=charger_plancher().digest,
        candidate_revision=REVISION_A_candidate_revision,
        report_bytes=rapport_candidate_revision.read_bytes(),
        image_courante=_image_candidate())
    # Ce que la vérification rend n'est plus une chaîne mais l'**ancrage** que la publication
    # exigera pour admettre une décision portant l'empreinte d'un autre run (revue B5, tour
    # correctif 2/3) : un `run_digest` nu se recopiait dans un rapport, qui pouvait ensuite se
    # déclarer lui-même digne de confiance.
    assert verifiee.run_digest == preuve["run_digest"] and len(verifiee.run_digest) == 64
    assert verifiee.run_digests == frozenset({preuve["run_digest"]})
    assert verifiee.candidate_revision == REVISION_A_candidate_revision


def test_une_preuve_dune_autre_candidate_revision_est_refusee(
        rapport_candidate_revision: Path) -> None:
    """La reproduction M2 : même rapport, même plancher, **autre** révision produit ⇒ refus."""
    from server.evals.plancher import verifier_liaison_preuve

    preuve = _preuve_candidate_revision(rapport_candidate_revision)
    with pytest.raises(PlancherInvalide, match="candidate_revision"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_B_candidate_revision,
            report_bytes=rapport_candidate_revision.read_bytes(),
        image_courante=_image_candidate())
    # Une révision mal formée est refusée avant même la comparaison.
    with pytest.raises(PlancherInvalide, match="hexadécimaux"):
        verifier_liaison_preuve(
            _preuve_candidate_revision(rapport_candidate_revision, candidate_revision="court"),
            plancher_digest=charger_plancher().digest,
            candidate_revision="court", report_bytes=b"", image_courante=_image_candidate())


def test_un_report_digest_non_concordant_est_refuse_pour_la_candidate_revision(
        rapport_candidate_revision: Path) -> None:
    """Un rapport modifié après coup détache la preuve de ce qu'elle prétend résumer."""
    from server.evals.plancher import verifier_liaison_preuve

    preuve = _preuve_candidate_revision(rapport_candidate_revision)
    with pytest.raises(PlancherInvalide, match="rapport modifié"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=rapport_candidate_revision.read_bytes() + b"\n",
            image_courante=_image_candidate())
    # Un rapport absent n'est pas « pas de contrainte » : c'est un refus.
    with pytest.raises(PlancherInvalide, match="absent ou illisible"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision, report_bytes=None,
            image_courante=_image_candidate())


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
            report_bytes=rapport_candidate_revision.read_bytes(),
        image_courante=_image_candidate())
    manquante = _preuve_candidate_revision(rapport_candidate_revision)
    manquante.pop("run_digest")
    with pytest.raises(PlancherInvalide, match="manquantes"):
        verifier_liaison_preuve(
            manquante, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=rapport_candidate_revision.read_bytes(),
        image_courante=_image_candidate())


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
            report_bytes=rapport_candidate_revision.read_bytes(),
        image_courante=_image_candidate())
    vide = _preuve_candidate_revision(rapport_candidate_revision, decisions=[])
    with pytest.raises(PlancherInvalide, match="liste non vide"):
        verifier_liaison_preuve(
            vide, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=rapport_candidate_revision.read_bytes(),
        image_courante=_image_candidate())


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
            candidate_revision=REVISION_B_candidate_revision, report_path=rapport,
            image_courante=_image_candidate())
    # Et le cas nominal traverse bien : la liaison n'est pas un refus systématique.
    decisions, verifiee = runner.charger_decisions_orchestrateur(
        preuve, plancher=charger_plancher(),
        candidate_revision=REVISION_A_candidate_revision, report_path=rapport,
        image_courante=_image_candidate())
    assert [d.metric for d in decisions] == ["offline_tests_pass_rate"]
    assert decisions[0].status == "green" and decisions[0].producer == "orchestrator"
    # La preuve vérifiée remonte au runner : c'est elle, et rien du rapport, qui autorisera la
    # publication d'une décision portant l'empreinte du run mesuré par la preuve.
    assert verifiee.run_digests == {d.run_digest for d in decisions}


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
            report_bytes=autre_run.read_bytes(), image_courante=_image_candidate())

    autre_revision = rapport_candidate_revision.parent / "autre-revision.json"
    _ecrire_rapport_candidate_revision(autre_revision,
                                       candidate_revision=REVISION_B_candidate_revision)
    preuve = _preuve_candidate_revision(autre_revision)
    with pytest.raises(PlancherInvalide, match="a mesuré la révision"):
        verifier_liaison_preuve(
            preuve, plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision,
            report_bytes=autre_revision.read_bytes(), image_courante=_image_candidate())

    # Un rapport sans identité de run ne prouve rien non plus.
    muet = rapport_candidate_revision.parent / "muet.json"
    muet.write_text('{"schema_version": 3}\n', encoding="utf-8")
    with pytest.raises(PlancherInvalide, match="identité de run"):
        verifier_liaison_preuve(
            _preuve_candidate_revision(muet), plancher_digest=charger_plancher().digest,
            candidate_revision=REVISION_A_candidate_revision, report_bytes=muet.read_bytes(),
            image_courante=_image_candidate())


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
            report_bytes=fabrique.read_bytes(), image_courante=_image_candidate())


def test_une_identite_externe_amputee_ferme_au_lieu_de_passer(
        rapport_candidate_revision: Path) -> None:
    """B1 : **toute identité obligatoire absente, vide ou mal formée ferme.**

    Contre-exemple reproduit par la revue : un rapport auto-cohérent **sans `plancher_digest`
    racine** et avec **`identity.image = {}`** était accepté alors que l'image opposée était
    complète et entièrement différente — zéro des cinq champs n'était comparé. La règle disait « si
    le champ est présent, je le compare » ; quatre façons d'obtenir le vide suffisaient donc à tout
    contourner.
    """
    import hashlib
    import json as _json

    from server.evals.plancher import verifier_liaison_preuve

    courant = charger_plancher().digest

    def _refuse(chemin: Path, motif: str, **kw: object) -> None:
        preuve = _preuve_candidate_revision(chemin)
        preuve["report_digest"] = hashlib.sha256(chemin.read_bytes()).hexdigest()
        with pytest.raises(PlancherInvalide, match=motif):
            verifier_liaison_preuve(
                preuve, plancher_digest=courant,
                candidate_revision=REVISION_A_candidate_revision,
                report_bytes=chemin.read_bytes(),
                image_courante=kw.get("image_courante", _image_candidate()))  # type: ignore[arg-type]

    dossier = rapport_candidate_revision.parent

    def _ecrire(nom: str, rapport: dict) -> Path:
        chemin = dossier / nom
        chemin.write_text(_json.dumps(rapport) + "\n", encoding="utf-8")
        return chemin

    # CE-1 : plancher racine absent **et** image vide — le contre-exemple exact de la revue.
    identite = _identite_candidate_revision(image={})
    _refuse(_ecrire("ce1.json", {"schema_version": 3, "decisions": [], "identity": identite}),
            "ne porte pas de plancher_digest")

    # CE-2 : aucune clé `image`.
    identite = _identite_candidate_revision()
    identite.pop("image")
    identite["run_digest"] = __import__(
        "server.evals.cache", fromlist=["empreinte_canonique"]).empreinte_canonique(
        {k: v for k, v in identite.items() if k != "run_digest"})
    _refuse(_ecrire("ce2.json", {"schema_version": 3, "decisions": [], "identity": identite,
                                 "plancher_digest": courant}),
            "ne porte pas d'identité d'image")

    # CE-3 : les cinq champs présents mais à `None` — « absent » déguisé en « présent ».
    identite = _identite_candidate_revision(image={champ: None for champ in _image_candidate()})
    _refuse(_ecrire("ce3.json", {"schema_version": 3, "decisions": [], "identity": identite,
                                 "plancher_digest": courant}),
            "identité d'image du rapport est incomplète")

    # CE-4 : `image` non-dict, autrefois rabattu sur `{}`.
    identite = _identite_candidate_revision(image=[])
    _refuse(_ecrire("ce4.json", {"schema_version": 3, "decisions": [], "identity": identite,
                                 "plancher_digest": courant}),
            "ne porte pas d'identité d'image")

    # Frère : une `image_courante` **amputée** est refusée à la source. Sans cela, un appelant qui
    # n'oppose que trois des cinq champs retire les deux autres du contrôle en silence.
    _refuse(rapport_candidate_revision, "image du run courant est incomplète",
            image_courante={"pipeline_digest": "1" * 64})

    # Et le champ qui manquait vraiment : `normalize_version`. Une preuve issue d'une autre recette
    # de normalisation décrit d'autres `text_norm`, donc d'autres `quote_hash`.
    divergent = dossier / "normalize.json"
    _ecrire_rapport_candidate_revision(
        divergent, image=_image_candidate(normalize_version="v-DIVERGENT"))
    _refuse(divergent, "normalize_version")

    # Le plancher racine divergent reste refusé, et le nominal reste accepté.
    _refuse(_ecrire("autre-plancher.json",
                    {"schema_version": 3, "decisions": [],
                     "identity": _identite_candidate_revision(), "plancher_digest": "f" * 64}),
            "racine du rapport")
    preuve = _preuve_candidate_revision(rapport_candidate_revision)
    assert verifier_liaison_preuve(
        preuve, plancher_digest=courant, candidate_revision=REVISION_A_candidate_revision,
        report_bytes=rapport_candidate_revision.read_bytes(),
        image_courante=_image_candidate())


def test_le_classement_oppose_le_rapport_avant_de_le_croire() -> None:
    """B1 : ce qui décide de la promotion oppose l'identité du rapport **avant** de la lire.

    Ces contrôles étaient éprouvés sur un helper que la production n'appelait plus (revue P7) : il
    a été supprimé, et ils sont désormais éprouvés sur le chemin que le produit emprunte réellement
    — `classer_configurations` —, sans quoi la preuve porterait sur du code que rien n'exécute.
    """
    # Digest arbitraire **et** plancher racine absent.
    with pytest.raises(ClassementInvalide, match="plancher_digest"):
        _classer([_candidat("candidat-fabrique", run_digest="e" * 64, plancher_racine=None)])
    # Plancher présent mais digest non recalculable : refusé aussi.
    with pytest.raises(ClassementInvalide, match="ne se recalcule pas"):
        _classer([_candidat("c", run_digest="e" * 64)])
    # Révision divergente : refusée quand le classement en nomme une.
    with pytest.raises(ClassementInvalide, match="révision"):
        _classer([_candidat("c")], revision=REVISION_B_candidate_revision)
    # Un rapport hors schéma 3, sans décisions, ou illisible : trois refus nommés.
    with pytest.raises(ClassementInvalide, match="hors schéma 3"):
        _classer([_candidat("c", schema_version=2)])
    with pytest.raises(ClassementInvalide, match="sans décisions"):
        _classer([_candidat("c", decisions=[])])
    with pytest.raises(ClassementInvalide, match="illisible"):
        _classer([CandidatClassement(name="c", report_bytes=b"pas du json")])
    # Un `metrics` **truthy mais non-objet** : un refus dit, pas une AttributeError (revue P11).
    with pytest.raises(ClassementInvalide, match="'metrics' doit être un objet"):
        _classer([_candidat("c", metrics="pas-un-objet")])
    # Le nominal, lui, se classe — et porte l'identité recalculée.
    classement = _classer([_candidat("c")])
    assert classement[0].admissible is True
    assert classement[0].candidate_revision == REVISION_CLASSEE


def test_le_classement_ancre_son_plancher_et_son_image_au_processus() -> None:
    """P1 : les deux références auxquelles le rapport est opposé sont **dérivées**, pas reçues.

    Contre-exemple exact du verdict : un rapport parfaitement auto-cohérent — `empreinte_canonique`
    est publique, donc n'importe qui peut recalculer un `run_digest` juste — mais mesuré sur une
    image et un plancher **inventés**. Le tour précédent le classait `admissible=True` en tête, car
    `plancher_digest` et `image_courante` étaient des paramètres : il suffisait de passer les mêmes
    valeurs. Il n'y a plus de paramètre à passer.
    """
    image_inventee = {"pipeline_digest": "1" * 64, "prompts_digest": "2" * 64,
                      "model_ids": {"fast": "modele-invente"},
                      "normalize_version": "v-inventee", "plancher_digest": "9" * 64}
    fabrique = _candidat("fabriquee", image=image_inventee, plancher_racine="9" * 64)
    with pytest.raises(ClassementInvalide, match="plancher"):
        _classer([fabrique])
    # Et l'image seule suffit : le plancher racine juste ne rachète pas une image inventée.
    with pytest.raises(ClassementInvalide, match="normalize_version|pipeline_digest"):
        _classer([_candidat("image-inventee",
                            image=_image_reelle() | {"normalize_version": "v-inventee"})])
    # La signature elle-même ne les accepte plus : la voie de contournement n'existe pas.
    with pytest.raises(TypeError):
        classer_configurations([fabrique], plancher_digest="9" * 64,  # type: ignore[call-arg]
                               image_courante=image_inventee,
                               candidate_revision=REVISION_CLASSEE)


def test_la_promotion_exige_la_revision_candidate_et_la_porte(tmp_path: Path) -> None:
    """B1 : une décision de promotion **reçoit, valide et oppose** la révision candidate.

    Contre-exemple reproduit : un rapport valide sous tous les autres contrôles, dont le seul écart
    est `identity.candidate_revision = null` — exactement ce qu'écrit `identite_run` sans
    `--candidate-revision` —, était classé `admissible: true`, `aucun_admissible: false`, code 0.
    Le contrôle existait mais restait **opt-in de l'appelant** : l'option n'était pas requise et la
    signature acceptait `None`.

    Aggravant fermé du même geste : le JSON de classement ne portait la révision **nulle part**, ni
    à la racine ni par configuration — l'artefact de promotion était inauditable après coup.
    """
    import json as _json

    from server.evals.plancher import _main

    # 1. La révision est **obligatoire**, et 2. **validée** : `None` et toute forme qui n'est pas
    #    40 hexadécimaux ferment, quelle que soit la qualité du rapport.
    for mauvaise in (None, "", "abc1234", "z" * 40):
        with pytest.raises(ClassementInvalide, match="obligatoire"):
            _classer([_candidat("c")], revision=mauvaise)  # type: ignore[arg-type]
    # 3. Elle est **opposée** : le contre-exemple exact — un rapport sans révision mesurée.
    sans_revision = _candidat("c", revision=None)  # type: ignore[arg-type]
    with pytest.raises(ClassementInvalide, match="révision"):
        _classer([sans_revision])
    # 4. Le nominal se classe, et **porte** la révision opposée.
    classement = _classer([_candidat("c")])
    assert classement[0].admissible is True
    assert classement[0].candidate_revision == REVISION_CLASSEE

    # 5. La CLI refuse `--classer` sans révision, plutôt que de promouvoir un candidat anonyme.
    (tmp_path / "nominal.json").write_bytes(_octets_de_rapport(nom="c"))
    configs = tmp_path / "configs.json"
    configs.write_text(_json.dumps([{"name": "c", "report": "nominal.json"}]), encoding="utf-8")
    assert _main(["--classer", str(configs)]) == 2


def test_un_rapport_sans_unexecuted_cases_ne_contribue_pas_a_une_promotion() -> None:
    """B5, chemin frère **dans la décision qui promeut** : la clé absente n'est pas « aucune ».

    `not rapport.get("unexecuted_cases")` traitait un rapport qui **omet** la clé comme n'ayant
    aucune exécution manquante, et il contribuait donc à `admissible=True`.
    """
    with pytest.raises(ClassementInvalide, match="unexecuted_cases"):
        _classer([_candidat("c", unexecuted_cases=_ABSENT)])
    with pytest.raises(ClassementInvalide, match="unexecuted_cases"):
        _classer([_candidat("c", unexecuted_cases="pas-une-liste")])


def test_un_depot_dont_les_sources_sont_illisibles_ferme_le_classement(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """P12 : `pipeline_digest()` et `prompts_digest()` relisent le dépôt, donc peuvent lever.

    Elles étaient appelées **hors** du garde qui rattrape, si bien qu'une source illisible sortait
    en traceback au lieu du refus dit et du code 2 que la CLI promet. Un classement qui ne sait pas
    contre quelle image il oppose ne se rend pas « au mieux ».
    """
    from server.evals import plancher as plancher_mod

    def _illisible(_digest: str) -> dict:
        raise OSError("source du dépôt illisible (injectée)")

    monkeypatch.setattr(plancher_mod, "image_du_depot", _illisible)
    with pytest.raises(ClassementInvalide, match="illisibles"):
        _classer([_candidat("c")])
    # Et la CLI le rend en code 2, avec un refus dit — jamais une trace de pile.
    import json as _json
    import tempfile

    with tempfile.TemporaryDirectory() as brut:
        dossier = Path(brut)
        (dossier / "c.json").write_bytes(_octets_de_rapport(nom="c"))
        configs = dossier / "configs.json"
        configs.write_text(_json.dumps([{"name": "c", "report": "c.json"}]), encoding="utf-8")
        assert plancher_mod._main(["--classer", str(configs),
                                   "--candidate-revision", REVISION_CLASSEE]) == 2


def test_le_classement_refuse_deux_candidats_homonymes(tmp_path: Path) -> None:
    """P6 : un artefact de promotion où l'on ne sait pas lequel a été promu n'est pas auditable.

    La règle vivait à la frontière CLI et n'était éprouvée nulle part ; elle appartient au
    classement, puisque c'est lui qui produit la décision. Éprouvée des **deux** côtés.
    """
    import json as _json

    from server.evals.plancher import _main

    with pytest.raises(ClassementInvalide, match="uniques"):
        _classer([_candidat("jumeau", cost_eur=0.01), _candidat("jumeau", cost_eur=0.02)])
    (tmp_path / "a.json").write_bytes(_octets_de_rapport(nom="jumeau", cost_eur=0.01))
    (tmp_path / "b.json").write_bytes(_octets_de_rapport(nom="jumeau", cost_eur=0.02))
    configs = tmp_path / "configs.json"
    configs.write_text(_json.dumps([{"name": "jumeau", "report": "a.json"},
                                    {"name": "jumeau", "report": "b.json"}]), encoding="utf-8")
    assert _main(["--classer", str(configs), "--candidate-revision", REVISION_CLASSEE]) == 2


# --- B1 : la voie **bibliothèque** du classement, fermée par recalcul ------------------------------
#
# Trois tours l'ont fermée par couches successives, et chacune s'est révélée insuffisante : la voie
# CLI (`--candidate-revision` requis), puis le modèle (`Configuration` refusant une identité absente
# ou mal formée), puis le classement (refusant une liste d'anonymes). Le tour correctif 1/3 a montré
# la faute de fond : présence et syntaxe ne sont pas une liaison. Trois empreintes **bien formées et
# entièrement fabriquées** — `candidate_revision="a"*40`, `run_digest="b"*64`,
# `report_digest="c"*64` — donnaient encore une tête de classement.
#
# `classer_configurations` n'accepte donc plus que des `CandidatClassement`, c'est-à-dire les octets
# d'un rapport, et recalcule lui-même les trois empreintes avant tout tri. Les contre-exemples
# ci-dessous sont rouges sur `4d0abb4` et verts ici ; aucun ne passe par un paramètre, il n'y a pas
# de voie opt-in à désarmer.

def _sans_identite(name: str = "anonyme", *, admissible: bool = True) -> Configuration:
    """Une configuration **sans identité**, construite en contournant le modèle.

    `model_construct` est la voie bibliothèque exacte que le recheck décrit : elle saute la
    validation pydantic, comme le ferait n'importe quel code qui assemblerait l'objet autrement.
    C'est donc bien le **classement** — et non le seul constructeur — qui doit refuser.
    """
    return Configuration.model_construct(
        name=name, admissible=admissible, cost_eur=0.0, latency_ms=0,
        candidate_revision=None, run_digest=None, report_digest=None)


def test_une_configuration_sans_identite_est_refusee_par_le_modele() -> None:
    """B1 : un **résultat** de classement porte toujours son identité, complète et bien formée."""
    for absent in ("candidate_revision", "run_digest", "report_digest"):
        champs = {"candidate_revision": REVISION_CLASSEE, "run_digest": "b" * 64,
                  "report_digest": "c" * 64}
        champs.pop(absent)
        with pytest.raises(ValueError, match=absent):
            Configuration(name="x", admissible=True, cost_eur=0.0, latency_ms=0, **champs)
    # Une identité **mal formée** ferme, admissible ou non : ce n'est pas une empreinte.
    for champ, mauvaise in (("candidate_revision", "a" * 39), ("candidate_revision", "z" * 40),
                            ("run_digest", "b" * 63), ("report_digest", "Z" * 64)):
        champs = {"candidate_revision": REVISION_CLASSEE, "run_digest": "b" * 64,
                  "report_digest": "c" * 64}
        champs[champ] = mauvaise
        with pytest.raises(ValueError, match=champ):
            Configuration(name="x", admissible=False, cost_eur=0.0, latency_ms=0, **champs)
    # Le nominal se construit, et ne se laisse plus dépouiller ensuite (`frozen`).
    nominale = _config("x", admissible=True, cost_eur=0.0, latency_ms=0)
    with pytest.raises(ValueError):
        nominale.candidate_revision = "z" * 40  # type: ignore[misc]


def test_le_classement_refuse_trois_empreintes_bien_formees_mais_fabriquees() -> None:
    """B1, contre-exemple du tour correctif 1/3 : le refus tient au **recalcul**, pas à la forme.

    Sur `4d0abb4`, `classer_configurations([Configuration(admissible=True,
    candidate_revision="a"*40, run_digest="b"*64, report_digest="c"*64)])` rendait
    `['fabriquee']` — trois empreintes irréprochables, aucune adossée à un octet réel.

    Deux refus le ferment, et il faut les deux : une `Configuration` déclarative n'entre plus dans
    un classement, et un **rapport** dont l'identité est posée à la main ne se recalcule pas.
    """
    fabriquee = Configuration(name="fabriquee", admissible=True, cost_eur=0.0, latency_ms=0,
                              candidate_revision="a" * 40, run_digest="b" * 64,
                              report_digest="c" * 64)
    with pytest.raises(ClassementInvalide, match="CandidatClassement"):
        _classer([fabriquee])
    # Et par la voie légitime : les empreintes du rapport sont recalculées depuis ses octets.
    with pytest.raises(ClassementInvalide, match="ne se recalcule pas"):
        _classer([_candidat("fabriquee", admissible=True, cost_eur=0.0, latency_ms=0,
                            run_digest="b" * 64)])
    # Le `report_digest` rendu est bien celui des octets reçus, jamais une valeur annoncée.
    import hashlib

    octets = _octets_de_rapport(nom="nominal", admissible=True, cost_eur=0.0, latency_ms=0)
    classement = _classer([CandidatClassement(name="nominal", report_bytes=octets)])
    assert classement[0].report_digest == hashlib.sha256(octets).hexdigest()


def test_une_identite_de_rapport_mal_formee_ferme_le_classement() -> None:
    """B1 : « longueur ou alphabet » — une chaîne qui ressemble à une empreinte n'en est pas une.

    Le garde `verifier_identite_classement` a été **retiré** (revue P7) : après le recalcul, aucune
    de ses branches n'était plus atteignable, et un contrôle qu'aucun chemin ne peut faire échouer
    donne l'apparence d'une garantie sans en être une. La propriété qu'il décrivait est éprouvée là
    où elle se joue vraiment — sur l'identité du rapport, avant que la `Configuration` n'existe.
    """
    for mauvais in ("a" * 39, "A" * 64, "b" * 65, "zz" + "c" * 62, "", 42, None):
        with pytest.raises(ClassementInvalide, match="run_digest|recalcule"):
            _classer([_candidat("malformee", run_digest=mauvais)])  # type: ignore[arg-type]
    for mauvaise in ("a" * 39, "A" * 40, ""):
        with pytest.raises(ClassementInvalide, match="révision|obligatoire"):
            _classer([_candidat("malformee", revision=mauvaise)])
    # Et le modèle refuse toujours de porter une identité mal formée, quel qu'en soit le chemin.
    for champ, mauvaise in (("candidate_revision", "a" * 39), ("candidate_revision", "A" * 40),
                            ("run_digest", "b" * 65), ("report_digest", "zz" + "c" * 62)):
        champs = {"candidate_revision": REVISION_CLASSEE, "run_digest": "b" * 64,
                  "report_digest": "c" * 64}
        champs[champ] = mauvaise
        with pytest.raises(ValueError, match=champ):
            Configuration(name="x", admissible=False, cost_eur=0.0, latency_ms=0, **champs)


def test_le_classement_refuse_deux_revisions_candidates_dans_la_meme_liste() -> None:
    """B1 : « deux révisions candidates différentes dans un même classement ne sont pas un classement ».

    Comparer les coûts de deux commits et en promouvoir un n'est pas une règle mécanique, c'est une
    confusion — et l'artefact de promotion serait inauditable, puisqu'il porte **une** révision. Par
    l'API publique, le refus vient du seul endroit qui puisse le rendre : le rapport du candidat n'a
    pas mesuré la révision du classement, donc il n'entre pas. Aucune liste hétérogène ne peut plus
    exister en aval.
    """
    with pytest.raises(ClassementInvalide, match="révision"):
        _classer([*_table_mesuree(),
                  _candidat("autre-commit", revision="b" * 40)])
    # La table homogène, elle, se classe : le refus ne mord que sur l'incohérence.
    assert len(_classer(_table_mesuree())) == len(MESURES_TABLE)


def test_la_cli_de_classement_dit_le_refus_au_lieu_de_promouvoir(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """B1 : le refus remonte jusqu'à l'opérateur — code 2, message nommé, aucun classement imprimé.

    Le rapport fourni porte une identité **fabriquée** : ses trois empreintes sont bien formées, et
    aucune ne se recalcule. C'est la sonde du tour correctif 1/3, passée par la CLI.
    """
    import json as _json

    from server.evals import plancher as plancher_mod

    _rapport_classable(tmp_path / "fabrique.json", cout=0.0, latence=0, admissible=True,
                       run_digest="b" * 64)
    configs = tmp_path / "configs.json"
    configs.write_text(_json.dumps([{"name": "c", "report": "fabrique.json"}]), encoding="utf-8")
    assert plancher_mod._main(["--classer", str(configs),
                               "--candidate-revision", REVISION_CLASSEE]) == 2
    capture = capsys.readouterr()
    assert "refus" in capture.err and "ne se recalcule pas" in capture.err
    assert "aucun_admissible" not in capture.out


# --- R2 : le chemin **nominal** de `--classer`, celui que ce cycle a réécrit -----------------------
#
# Les contre-exemples B1 prouvent les refus ; aucun test ne prouvait le succès. La ligne
# `classement[0].candidate_revision if classement else args.candidate_revision` — écrite par ce
# cycle — pouvait être remplacée par `None`, la clé retirée, ou `return 1 if aucun_admissible else 0`
# inversé, sans rougir. Ce test est l'ancrage de l'artefact de promotion : il passe par `_main`, donc
# par la construction réelle des configurations, donc par l'image ancrée du processus.

def _rapport_classable(chemin: Path, *, cout: float, latence: int, admissible: bool,
                       revision: str = REVISION_CLASSEE,
                       run_digest: object = _ABSENT) -> None:
    """Écrit sur disque un rapport que le classement accepte — **la même fabrique** que plus haut.

    Une seconde recette de rapport aurait fini par diverger de celle que les contre-exemples
    emploient, et l'un des deux chemins aurait alors cessé d'être éprouvé.
    """
    chemin.write_bytes(_octets_de_rapport(
        nom=chemin.stem, admissible=admissible, cost_eur=cout, latency_ms=latence,
        revision=revision, run_digest=run_digest))


def test_le_classement_nominal_sort_en_zero_et_porte_la_revision_opposee(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R2 : le chemin de succès de `--classer`, de bout en bout, par `_main`.

    Quatre propriétés que rien n'assérait : le code de sortie 0, la révision **réellement opposée**
    à la racine du JSON de promotion, le `plancher_digest` racine, et l'ordre de rang (admissible
    d'abord, puis la moins chère, puis la plus rapide).
    """
    import json as _json

    from server.evals.plancher import _main

    _rapport_classable(tmp_path / "chere.json", cout=0.09, latence=100, admissible=True)
    _rapport_classable(tmp_path / "economique.json", cout=0.03, latence=900, admissible=True)
    _rapport_classable(tmp_path / "recalee.json", cout=0.00, latence=1, admissible=False)
    configs = tmp_path / "configs.json"
    configs.write_text(_json.dumps([
        {"name": "chere", "report": "chere.json"},
        {"name": "recalee", "report": "recalee.json"},
        {"name": "economique", "report": "economique.json"},
    ]), encoding="utf-8")

    assert _main(["--classer", str(configs),
                  "--candidate-revision", REVISION_CLASSEE]) == 0
    artefact = _json.loads(capsys.readouterr().out)

    assert artefact["candidate_revision"] == REVISION_CLASSEE
    assert artefact["plancher_digest"] == charger_plancher().digest
    assert artefact["aucun_admissible"] is False
    assert [c["name"] for c in artefact["classement"]] == ["economique", "chere", "recalee"]
    # Chaque configuration porte son identité, et toutes la même révision : l'artefact est auditable.
    for configuration in artefact["classement"]:
        assert configuration["candidate_revision"] == REVISION_CLASSEE
        assert len(configuration["run_digest"]) == 64
        assert len(configuration["report_digest"]) == 64
    # Deux runs distincts, deux `run_digest` distincts : l'identité n'est pas un copier-coller.
    assert len({c["run_digest"] for c in artefact["classement"]}) == 3


# --- B1, tour correctif 2/3 : la révision candidate est opposée au **checkout**, pas à elle-même ---
#
# Sonde exacte du recheck sur `2ba74ae` : un rapport auto-cohérent fabriqué pour la révision
# `'a' * 40` — qui n'existe dans aucun dépôt (`git cat-file -e` échoue) — était accepté, admissible,
# et classé **en tête**. Les empreintes étaient recalculées depuis ses octets, le plancher et
# l'image venaient du processus, mais la révision n'était opposée qu'à ce que le rapport en disait.
# Une auto-cohérence n'est pas une vérité : c'est la quatrième couche du même défaut — présence,
# syntaxe, recalcul, puis **ancrage externe**.

def test_un_rapport_fabrique_pour_une_revision_inexistante_ne_se_classe_pas() -> None:
    """Le contre-exemple du recheck : rouge sur `2ba74ae`, vert ici.

    La révision `'a' * 40` n'est celle d'aucun checkout. Le rapport, lui, est parfaitement
    auto-cohérent — même plancher, même image, `run_digest` recalculable depuis son identité : tout
    ce que les tours précédents savaient vérifier passe. Ce qui ferme, c'est l'opposition au
    checkout réellement exécuté.
    """
    _exige_un_checkout_opposable()
    fantome = "a" * 40
    assert fantome != REVISION_DU_CHECKOUT, "la sonde suppose une révision qui n'est pas le HEAD"
    with pytest.raises(ClassementInvalide, match="checkout"):
        classer_configurations([_candidat("fantome", revision=fantome)],
                               candidate_revision=fantome)


def test_un_checkout_illisible_ferme_le_classement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ne pas pouvoir conclure ferme — la règle du module, appliquée au dernier contrôle qui manquait.

    Deux façons de ne pas conclure : aucune révision établissable (ni `git rev-parse HEAD`, ni
    `GIT_SHA`), et un arbre que `git status --porcelain` n'a pas su lire. Les deux refusent, et
    aucune ne se rabat sur « c'est sans doute la bonne ».
    """
    from server.evals import plancher as plancher_mod

    monkeypatch.setattr(plancher_mod, "revision_executee", lambda *a, **k: (None, []))
    with pytest.raises(ClassementInvalide, match="n'a pu être établie"):
        classer_configurations([_candidat("c")], candidate_revision=REVISION_CLASSEE)

    monkeypatch.setattr(plancher_mod, "revision_executee",
                        lambda *a, **k: (REVISION_CLASSEE, [ARBRE_NON_VERIFIABLE]))
    with pytest.raises(ClassementInvalide, match="non vérifiable"):
        classer_configurations([_candidat("c")], candidate_revision=REVISION_CLASSEE)


def test_la_cli_publie_lidentite_que_le_classement_a_opposee(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """B1, volet TOCTOU : la CLI ne relit plus une seconde référence.

    Elle chargeait le plancher pour son propre compte **avant** le classement, puis publiait ce
    premier digest — alors que le classement dérive le sien du processus. Deux lectures, donc deux
    valeurs possibles, et c'est la seconde qui décidait pendant que la première était publiée.

    La sonde : le classement rend une identité, et c'est **elle** qui doit sortir dans l'artefact.
    On fait diverger la lecture indépendante de la CLI ; si l'artefact suivait encore cette
    lecture-là, il publierait un plancher contre lequel rien n'a été opposé.
    """
    import json as _json

    from server.evals import plancher as plancher_mod
    from server.evals.plancher import _main

    _exige_un_checkout_opposable()
    _rapport_classable(tmp_path / "seule.json", cout=0.01, latence=10, admissible=True)
    configs = tmp_path / "configs.json"
    configs.write_text(_json.dumps([{"name": "seule", "report": "seule.json"}]), encoding="utf-8")

    reel = plancher_mod.charger_plancher
    appels = {"n": 0}

    def _charger(*a: object, **kw: object):
        appels["n"] += 1
        charge = reel(*a, **kw)
        # La **première** lecture — celle de la CLI — rend un digest différent ; les suivantes,
        # dont celle du classement, rendent le digest réel.
        if appels["n"] == 1:
            return charge.model_copy(update={"digest": "0" * 64})
        return charge

    monkeypatch.setattr(plancher_mod, "charger_plancher", _charger)
    assert _main(["--classer", str(configs), "--candidate-revision", REVISION_CLASSEE]) == 0
    artefact = _json.loads(capsys.readouterr().out)
    assert artefact["plancher_digest"] == reel().digest
    assert artefact["plancher_digest"] != "0" * 64, (
        "l'artefact publie la lecture indépendante de la CLI, pas celle que le classement a opposée")


def test_aucun_admissible_sort_en_un_et_reste_un_rouge_publie(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R2 : `aucun_admissible` est un **rouge publié** — code 1, artefact complet, jamais une question."""
    import json as _json

    from server.evals.plancher import _main

    _rapport_classable(tmp_path / "recalee.json", cout=0.00, latence=1, admissible=False)
    configs = tmp_path / "configs.json"
    configs.write_text(_json.dumps([{"name": "recalee", "report": "recalee.json"}]),
                       encoding="utf-8")

    assert _main(["--classer", str(configs), "--candidate-revision", REVISION_CLASSEE]) == 1
    artefact = _json.loads(capsys.readouterr().out)
    assert artefact["aucun_admissible"] is True
    assert artefact["candidate_revision"] == REVISION_CLASSEE
    assert [c["name"] for c in artefact["classement"]] == ["recalee"]


def test_un_rapport_dune_autre_revision_ne_se_classe_pas_par_la_cli(tmp_path: Path) -> None:
    """R2, revers du nominal : la révision opposée est bien celle des rapports, pas de l'argument."""
    import json as _json

    from server.evals.plancher import _main

    _rapport_classable(tmp_path / "autre.json", cout=0.01, latence=10, admissible=True,
                       revision="b" * 40)
    configs = tmp_path / "configs.json"
    configs.write_text(_json.dumps([{"name": "autre", "report": "autre.json"}]), encoding="utf-8")
    assert _main(["--classer", str(configs), "--candidate-revision", REVISION_CLASSEE]) == 2


def test_le_classement_ne_consomme_pas_son_argument(tmp_path: Path) -> None:
    """R12 : un générateur épuisé rendait un classement **vide**, donc `aucun_admissible` sur des candidats réels.

    La signature annonce une liste, mais rien n'empêchait un appelant de passer un itérateur : le
    contrôle d'identité le parcourait, puis `sorted` n'y trouvait plus rien. Un classement vide rendu
    en silence est la même faute que celles que ce cycle ferme — une donnée absente présentée comme
    un résultat.
    """
    table = _table_mesuree()
    classement = _classer(iter(table))
    assert [c.name for c in classement] == [c.name for c in _classer(table)]
    assert len(classement) == len(table)
    # Et un générateur d'anonymes ferme toujours : le contrôle n'a pas été contourné au passage.
    with pytest.raises(ClassementInvalide):
        _classer(c for c in [_sans_identite()])
