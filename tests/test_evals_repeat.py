"""Story 4.2b — Répétitions sans cache, stabilité, préflight de budget, décisions du gate.

Mêmes doubles que `tests/test_evals_run.py` : aucun réseau, aucun vrai pipeline — les fixtures
simulent des cas payants (AC : « un cas payant simulé »).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from server.app.config import Settings
from server.app.corpus.loader import Corpus
from server.app.corpus.index import Index
from server.app.corpus.text import normalize
from server.app.domain.answer import Answer, AnswerSegment, ClaimStatus, VerifiedClaim, VerifiedQuote
from server.app.domain.document import Document, Node
from server.app.domain.errors import LlmUnavailable, TruncatedRead
from server.app.domain.ingest import Gate, GateDecision, ManifestEntry
from server.app.domain.trace import LLMCall, StepTrace, Trace, Usage
from server.evals import run as runner
from server.evals.plancher import charger_plancher
from tests.helpers_espace import poser_espace

GUIDE = "mini-guide"
CONTRAT = "mini-contrat"
TEXTE_GUIDE = "LuxTrust s'obtient au meilleur prix par une banque luxembourgeoise."
TEXTE_CONTRAT = "Les degats occasionnes au mobilier assure par un evenement soudain sont couverts."


def _settings(**kw: Any) -> Settings:
    defauts: dict[str, Any] = {"anthropic_api_key": "cle-de-test", "guide_doc_id": GUIDE,
                               "sinistre_doc_id": CONTRAT}
    defauts.update(kw)
    return Settings(_env_file=None, **defauts)


def _document(doc_id: str, kind: str, texte: str, loc: str) -> Document:
    doc = Document(
        doc_id=doc_id, kind=kind, title=f"Doc {doc_id}", edition="2020",
        source_hash="s", ingest_fingerprint="f",
        nodes=[Node(node_id=f"{doc_id}:n1", level=1, title="N1",
                    items=[{"block_id": f"{doc_id}:{loc}:1"}])],
        blocks=[{"block_id": f"{doc_id}:{loc}:1", "loc": loc, "seq": 1, "kind": "para",
                 "text": texte}])
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    return doc


def _corpus() -> tuple[Corpus, Index]:
    docs = {GUIDE: _document(GUIDE, "guide", TEXTE_GUIDE, "ffiche"),
            CONTRAT: _document(CONTRAT, "contrat", TEXTE_CONTRAT, "p3")}
    manifest = {d: ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                                 document_hash="d", edition="2020") for d in docs}
    corpus = Corpus(documents=docs, manifest=manifest,
                    summaries={d: f"# {d}" for d in docs}, alerts={d: [] for d in docs})
    return corpus, Index(corpus)


def _citation(index: Index, block_id: str, extrait: str) -> VerifiedQuote:
    texte = index.corpus.documents[index.doc_of(block_id)].block(block_id).text
    debut = texte.index(extrait)
    return VerifiedQuote(block_id=block_id, quote=extrait, start=0, end=len(normalize(extrait)),
                         text_start=debut, text_end=debut + len(extrait))


def _claim(quote: VerifiedQuote, claim_id: str = "c1") -> VerifiedClaim:
    return VerifiedClaim(claim_id=claim_id, text="Une affirmation.", quotes=[quote],
                         status=ClaimStatus(retrouvee=True, pertinente=True, edition="2020"))


def _reponse(claims: list[VerifiedClaim]) -> Answer:
    return Answer(found=True, complete=True, texte="Une affirmation.",
                  segments=[AnswerSegment(text="Une affirmation.", kind="factuel",
                                          claim_ids=[c.claim_id for c in claims])],
                  claims=claims)


def _trace(pipeline: str = "guide") -> Trace:
    # Même défaut que le runner : le sinistre navigue par outils depuis la story 4.2d.
    variant = runner.DEFAUT_PAR_SUITE[pipeline]
    return Trace(request_id="eval", pipeline=pipeline, variant=variant, total_cost_eur=0.01,
                 steps=[StepTrace(name="comprendre", tier="micro",
                                  opened_block_ids=[f"{GUIDE}:ffiche:1"],
                                  calls=[LLMCall(model="modele-test",
                                                 usage=Usage(cost_eur=0.01,
                                                             cost_eur_original=0.01))])])


class DoublePipeline:
    def __init__(self, resultats: list[Any], *, cout: float = 0.02) -> None:
        self.resultats = list(resultats)
        self.cout = cout
        self.appels: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kw: Any) -> tuple[Answer, Trace]:
        self.appels.append({"args": args, "kw": kw})
        budget = kw.get("budget")
        if budget is not None:
            budget.cost_eur = round(budget.cost_eur + self.cout, 4)
        suivant = self.resultats.pop(0)
        if isinstance(suivant, BaseException):
            raise suivant
        return suivant


def _contexte(reponses_guide: list[Any], *, cout: float = 0.02) -> runner.Contexte:
    _corpus_obj, index = _corpus()
    ctx = runner.Contexte(settings=_settings(), index=index, client=object(),
                          pipeline_digest_hex="pd", prompts_digest_hex="pp")
    ctx._guide = DoublePipeline(reponses_guide, cout=cout)  # type: ignore[attr-defined]
    ctx._sinistre = DoublePipeline([], cout=cout)           # type: ignore[attr-defined]
    return ctx


@pytest.fixture(autouse=True)
def _pipelines_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    async def guide(*args: Any, **kw: Any) -> Any:
        return await _COURANT["guide"](*args, **kw)

    async def sinistre(*args: Any, **kw: Any) -> Any:
        return await _COURANT["sinistre"](*args, **kw)

    monkeypatch.setattr(runner, "repondre_guide", guide)
    monkeypatch.setattr(runner.pipeline_sinistre, "run", sinistre)


_COURANT: dict[str, Any] = {}


def _armer(ctx: runner.Contexte) -> runner.Contexte:
    _COURANT["guide"] = ctx._guide          # type: ignore[attr-defined]
    _COURANT["sinistre"] = ctx._sinistre    # type: ignore[attr-defined]
    return ctx


def _cas(id: str = "g-luxtrust") -> runner.Cas:
    return runner.Cas.model_validate({
        "id": id, "suite": "guide", "profile": "vertical",
        "question": "Quelle est la façon la moins chère d'obtenir LuxTrust ?",
        "expected": {"found": True, "block_ids": [f"{GUIDE}:ffiche:1"]},
        "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
        "mode_attendu": "bonne_reponse",
    })


def _executer(ctx: runner.Contexte, cas: list[runner.Cas], *, max_cost: float = 1.0,
              repeat: int = 1) -> list[runner.Resultat]:
    import asyncio
    _armer(ctx)
    return asyncio.run(runner.executer(cas, ctx, max_cost_eur=max_cost, sortie=io.StringIO(),
                                       repeat=repeat))


def _bonne(index: Index) -> tuple[Answer, Trace]:
    return (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())


# --- répétitions sans cache ------------------------------------------------------------------------

def test_repeat_3_publie_trois_resultats_complets_et_payants() -> None:
    """AC 4.2b : trois exécutions payantes, résultat complet publié par répétition."""
    ctx = _contexte([])
    ctx._guide = DoublePipeline([_bonne(ctx.index) for _ in range(3)])  # type: ignore[attr-defined]
    resultats = _executer(ctx, [_cas()], repeat=3)
    assert [r.repetition for r in resultats] == [1, 2, 3]
    assert len(ctx._guide.appels) == 3  # type: ignore[attr-defined]
    for r in resultats:
        assert r.cost_eur == 0.02  # chaque répétition est payée : aucun hit de cache
        assert r.proofs and r.proofs[0]["block_id"] == f"{GUIDE}:ffiche:1"
        assert r.proofs[0]["quote_hash"] and r.proofs[0]["kind"] == "para"
        assert r.opened_block_ids == [f"{GUIDE}:ffiche:1"]
        assert r.steps and r.steps[0]["tier"] == "micro"

    rapport = runner.construire_rapport(
        resultats, [_cas()], cases_dir=Path("/inexistant"), profile="vertical",
        max_cost_eur=0.5, complete=True, repeat=3,
        snapshot=runner.CasesSnapshot(cases_hash="h"))
    assert all(r["expected_blocks_not_opened"] == [] for r in rapport["results"])
    resultats[0].opened_block_ids = []
    rapport_manquant = runner.construire_rapport(
        resultats, [_cas()], cases_dir=Path("/inexistant"), profile="vertical",
        max_cost_eur=0.5, complete=True, repeat=3,
        snapshot=runner.CasesSnapshot(cases_hash="h"))
    assert rapport_manquant["results"][0]["expected_blocks_not_opened"] == [
        f"{GUIDE}:ffiche:1"]


def test_repeat_refuse_un_cache_arme() -> None:
    """AC 4.2b : aucun cache de réponse n'est consulté sous --repeat."""
    ctx = _contexte([])
    ctx.response_cache = object()  # type: ignore[assignment]
    with pytest.raises(runner.RefusDeTourner, match="cache désarmé"):
        _executer(ctx, [_cas()], repeat=3)


def test_lagregat_de_stabilite_compare_les_preuves() -> None:
    """Stabilité guide : même statut/found/complete/label/fiches ⇒ stable ; écart ⇒ instable."""
    ctx = _contexte([])
    stable_cas = _cas("g-stable")
    ctx._guide = DoublePipeline([_bonne(ctx.index) for _ in range(3)])  # type: ignore[attr-defined]
    resultats = _executer(ctx, [stable_cas], repeat=3)
    stabilite = runner.agreger_stabilite(resultats, [stable_cas], repeat=3)
    assert stabilite["cases"]["g-stable"]["stable"] is True

    instable_cas = _cas("g-instable")
    refus = Answer(found=False, complete=False, texte="",
                   reason={"kind": "zero_hit"})  # type: ignore[arg-type]
    ctx2 = _contexte([])
    ctx2._guide = DoublePipeline(  # type: ignore[attr-defined]
        [_bonne(ctx2.index), _bonne(ctx2.index), (refus, _trace())])
    resultats2 = _executer(ctx2, [instable_cas], repeat=3)
    stabilite2 = runner.agreger_stabilite(resultats2, [instable_cas], repeat=3)
    detail = stabilite2["cases"]["g-instable"]
    assert detail["stable"] is False
    assert "signatures divergentes entre répétitions" in detail["raisons"]
    # La dispersion est publiée, pas masquée.
    assert len(detail["signatures"]) == 3 and len(detail["cost_eur"]) == 3


def test_une_interruption_laisse_les_repetitions_manquantes_rouges_au_denominateur() -> None:
    """AC 4.2b : un run interrompu laisse ses répétitions manquantes rouges au dénominateur."""
    ctx = _contexte([])
    ctx._guide = DoublePipeline(  # type: ignore[attr-defined]
        [_bonne(ctx.index), LlmUnavailable("panne fournisseur")])
    with pytest.raises(runner.IncidentTechnique) as exc:
        _executer(ctx, [_cas()], repeat=3)
    incident = exc.value
    assert len(incident.resultats) == 1  # l'acquis est conservé
    assert incident.non_executes == ["g-luxtrust#r2", "g-luxtrust#r3"]
    rapport = runner.construire_rapport(
        incident.resultats, [_cas()], cases_dir=Path("/inexistant"), profile="vertical",
        max_cost_eur=0.5, complete=False, stop_reason=str(incident),
        non_executes=incident.non_executes, repeat=3,
        snapshot=runner.CasesSnapshot(cases_hash="h"), plancher=charger_plancher())
    assert rapport["executions_planned"] == 3
    assert rapport["executions_completed"] == 1
    assert rapport["executions_interrupted"] == 2
    stabilite = rapport["stability"]["cases"]["g-luxtrust"]
    assert stabilite["stable"] is False and "répétitions manquantes : 2 sur 3" in stabilite["raisons"]
    decisions = {d["metric"]: d for d in rapport["decisions"]}
    assert decisions["cases_ok_rate"]["status"] == "red"
    assert decisions["cases_ok_rate"]["n"] == 3
    assert decisions["stabilite_guide"]["status"] == "red"
    assert decisions["executions_completes"]["status"] == "red"


def test_une_lecture_tronquee_est_rouge_et_les_repetitions_continuent() -> None:
    erreurs = []
    for _ in range(3):
        erreur = TruncatedRead("aucune claim après lecture bornée")
        erreur.trace = _trace()
        erreurs.append(erreur)
    ctx = _contexte([])
    ctx._guide = DoublePipeline(erreurs)  # type: ignore[attr-defined]
    resultats = _executer(ctx, [_cas()], repeat=3)
    assert len(resultats) == 3
    assert [r.repetition for r in resultats] == [1, 2, 3]
    assert all(r.label == "claim_non_soutenu" and r.http == 503 and not r.ok
               for r in resultats)


# --- préflight de budget (main) --------------------------------------------------------------------

def _interdit(*args: Any, **kw: Any) -> Any:
    raise AssertionError("le contexte a été construit malgré le refus de budget")


def _poser_racine_budget(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "manifest.json").write_text("{}\n", "utf-8")
    poser_espace(tmp_path, data_dir=data, cibles=(Path("refus.json"), Path("refus.md")))


def test_le_refus_de_budget_survient_avant_le_premier_appel(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """AC 4.2b : majorant estimé > budget effectif ⇒ refus avant tout appel, trois chiffres, code 4."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    monkeypatch.delenv("LIVE_BUDGET_EUR", raising=False)
    monkeypatch.setattr(runner, "construire_contexte", _interdit)
    monkeypatch.setattr(runner, "construire_contexte_parsing", _interdit)
    _poser_racine_budget(tmp_path)
    code = runner.main(["--case", "s-bougie-canape", "--profile", "full",
                        "--repeat", "3", "--max-cost", "0.2",
                        "--data-dir", str(tmp_path / "data"),
                        "--output-json", str(tmp_path / "refus.json"),
                        "--output-markdown", str(tmp_path / "refus.md")])
    assert code == 4
    err = capsys.readouterr().err
    assert "refus de budget avant le premier appel" in err
    # Le budget annoncé est celui de la configuration, jamais un littéral : c'est lui que le
    # relèvement des budgets Sonnet (02/09/2026) a déplacé.
    assert f"configured_budget_eur={Settings(_env_file=None).live_budget_eur:.4f}" in err
    assert "accrued_cost_eur=0.0000" in err
    majorant = runner.estimate_run_majorant(3, _settings())
    assert f"refused_cost_eur={majorant:.4f}" in err
    rapport = json.loads((tmp_path / "refus.json").read_text(encoding="utf-8"))
    assert rapport["complete"] is False and rapport["executions_completed"] == 0
    assert rapport["preflight"] == {
        **rapport["preflight"],
        "configured_budget_eur": Settings(_env_file=None).live_budget_eur,
        "accrued_cost_eur": 0.0,
        "refused_cost_eur": majorant,
    }
    assert rapport["decisions"] and all(d["status"] == "red" for d in rapport["decisions"])


def test_live_budget_env_borne_aussi_le_max_cost(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Le budget effectif est min(--max-cost, LIVE_BUDGET_EUR) : la règle trusted fait foi."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    monkeypatch.setenv("LIVE_BUDGET_EUR", "0.15")
    monkeypatch.setattr(runner, "construire_contexte", _interdit)
    _poser_racine_budget(tmp_path)
    code = runner.main(["--case", "s-bougie-canape", "--profile", "full",
                        "--repeat", "3", "--max-cost", "5.0",
                        "--data-dir", str(tmp_path / "data"),
                        "--output-json", str(tmp_path / "refus.json"),
                        "--output-markdown", str(tmp_path / "refus.md")])
    assert code == 4
    assert "configured_budget_eur=0.1500" in capsys.readouterr().err


def test_une_racine_incomplete_precede_le_refus_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    monkeypatch.setattr(runner, "construire_contexte", _interdit)
    poser_espace(tmp_path, data_dir=tmp_path / "data",
                cibles=(Path("refus.json"), Path("refus.md")))

    code = runner.main(["--case", "s-bougie-canape", "--profile", "full",
                        "--repeat", "3", "--max-cost", "0.2",
                        "--data-dir", str(tmp_path / "data"),
                        "--output-json", str(tmp_path / "refus.json"),
                        "--output-markdown", str(tmp_path / "refus.md")])
    assert code == 2
    assert "manifest publié absent" in capsys.readouterr().err
    assert not (tmp_path / "refus.json").exists()


def test_dry_run_publie_plancher_digest_et_majorant(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Vérification 4.2b : `--dry-run` affiche plancher_digest et majorant, sans appel ni clé."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("LIVE_BUDGET_EUR", raising=False)
    monkeypatch.setattr(runner, "construire_contexte", _interdit)
    assert runner.main(["--profile", "full", "--dry-run"]) == 0
    sortie = capsys.readouterr().out
    assert "plancher_digest=" in sortie
    assert "majorant_estime=" in sortie
    budget = min(Settings(_env_file=None).evals_max_cost_eur,
                 Settings(_env_file=None).live_budget_eur)
    assert f"budget_effectif={budget:.4f}" in sortie


# --- identité de run et décisions ------------------------------------------------------------------

def test_le_plancher_digest_entre_dans_lidentite_et_le_run_digest_existe() -> None:
    """AC 4.2b : le digest du plancher entre dans l'identité de run."""
    ctx = _contexte([])
    charge = charger_plancher()
    identite = runner.identite_run([_cas()], ctx, profile="vertical", quick=False, variant=None,
                                   plancher_digest=charge.digest, repeat=3)
    assert identite["image"]["plancher_digest"] == charge.digest
    assert identite["scope"]["repeat"] == 3
    assert len(identite["run_digest"]) == 64
    # Le digest couvre l'identité : un plancher différent change le run_digest.
    autre = runner.identite_run([_cas()], ctx, profile="vertical", quick=False, variant=None,
                                plancher_digest="0" * 64, repeat=3)
    assert autre["run_digest"] != identite["run_digest"]


def test_les_decisions_du_runner_sont_vertes_et_les_preuves_externes_restent_rouges() -> None:
    ctx = _contexte([])
    ctx._guide = DoublePipeline([_bonne(ctx.index) for _ in range(3)])  # type: ignore[attr-defined]
    resultats = _executer(ctx, [_cas()], repeat=3)
    decisions = runner.construire_decisions(resultats, [_cas()], plancher=charger_plancher(),
                                            repeat=3, run_digest="d" * 64, producer="orchestrator")
    par_metric = {d.metric: d for d in decisions}
    assert par_metric["cases_ok_rate"].status == "green"
    assert par_metric["cases_ok_rate"].n == 3
    assert par_metric["stabilite_guide"].status == "green"
    assert par_metric["offline_tests_pass_rate"].status == "red"
    assert "orchestrateur" in (par_metric["offline_tests_pass_rate"].reason or "")
    for d in decisions:
        assert d.producer == "orchestrator" and d.run_digest == "d" * 64
        assert d.threshold == 1.0


def test_un_run_builder_ne_peut_produire_aucune_decision_verte() -> None:
    ctx = _contexte([])
    ctx._guide = DoublePipeline([_bonne(ctx.index) for _ in range(3)])  # type: ignore[attr-defined]
    resultats = _executer(ctx, [_cas()], repeat=3)
    decisions = runner.construire_decisions(
        resultats, [_cas()], plancher=charger_plancher(), repeat=3,
        run_digest="d" * 64, producer="builder")
    assert decisions and all(d.status == "red" for d in decisions)
    assert any("producteur non probant" in (d.reason or "") for d in decisions)


# --- non-mutation du dernier vert ------------------------------------------------------------------

def _gate(evals_ok: bool, **kw: Any) -> Gate:
    defauts: dict[str, Any] = dict(
        profile="vertical", source_hash="s", ingest_fingerprint="f", overlay_hash=None,
        cases_hash="c", cases=1, countersigned=False, pipeline_digest="pd", prompts_digest="pp",
        model_ids={}, evals_ok=evals_ok, date="2026-08-28",
        run_digest="r" * 64,
        decisions=[GateDecision(metric="cases_ok_rate", producer="orchestrator", threshold=1.0,
                                scope="run", n=3, run_digest="r" * 64,
                                value=1.0 if evals_ok else 0.0,
                                status="green" if evals_ok else "red")])
    defauts.update(kw)
    return Gate(**defauts)


def _manifest(racine: Path, gate: Gate | None) -> Path:
    data = racine / "data"
    data.mkdir(parents=True, exist_ok=True)
    chemin = data / "manifest.json"
    entree = {"status": "servi", "source_hash": "s", "ingest_fingerprint": "f",
              "document_hash": "d", "edition": "e", "overlay_hash": None,
              "gate": gate.model_dump(mode="json") if gate is not None else None}
    chemin.write_text(json.dumps({GUIDE: entree}), "utf-8")
    # `ecrire_gate` dérive l'espace de `manifest_path.parent` (story 4.5, B7) : la disposition doit
    # être posée avant tout appel, comme un opérateur ou la CI la posent.
    poser_espace(racine, data_dir=data)
    return chemin


def test_un_gate_candidat_rouge_ne_remplace_jamais_un_vert(tmp_path: Path,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    """Le candidat certifie le courant sans réécrire le dernier golden set vert."""
    dernier_vert = _gate(True, cases_hash="hash-historique")
    candidat_courant = _gate(False, cases_hash="hash-courant")
    chemin = _manifest(tmp_path, dernier_vert)
    avant = chemin.read_bytes()

    assert candidat_courant.cases_hash != dernier_vert.cases_hash
    ecrit = runner.ecrire_gate(chemin, GUIDE, candidat_courant)

    assert ecrit is False
    assert candidat_courant.cases_hash == "hash-courant"
    assert chemin.read_bytes() == avant
    assert "gate candidat rouge" in capsys.readouterr().err


def test_un_gate_vert_remplace_un_vert_et_un_rouge_remplace_un_rouge(tmp_path: Path) -> None:
    chemin = _manifest(tmp_path, _gate(True))
    assert runner.ecrire_gate(chemin, GUIDE, _gate(True, cases_hash="c2")) is True
    apres = json.loads(chemin.read_text("utf-8"))
    assert apres[GUIDE]["gate"]["cases_hash"] == "c2"
    assert apres[GUIDE]["gate"]["decisions"][0]["metric"] == "cases_ok_rate"
    chemin_rouge = _manifest(tmp_path / "r", _gate(False)) if (tmp_path / "r").mkdir() is None \
        else None
    assert chemin_rouge is not None
    assert runner.ecrire_gate(chemin_rouge, GUIDE, _gate(False, cases_hash="c3")) is True


def test_un_gate_rouge_sur_un_manifest_sans_gate_est_ecrit(tmp_path: Path) -> None:
    """Sans dernier vert, le rouge s'écrit : c'est le verdict honnête du premier run."""
    chemin = _manifest(tmp_path, None)
    assert runner.ecrire_gate(chemin, GUIDE, _gate(False)) is True
    assert json.loads(chemin.read_text("utf-8"))[GUIDE]["gate"]["evals_ok"] is False


def _preuve(block_id: str, *, kind: str = "garantie",
            hash_preuve: str = "h1") -> dict[str, Any]:
    return {"doc_id": "contrat-test", "block_id": block_id, "kind": kind,
            "quote_hash": hash_preuve}


def _resultat_sinistre(repetition: int, *, verdict: str = "sous_conditions",
                       hash_preuve: str = "h1",
                       blocs: tuple[str, ...] = ("contrat-test:p1:1",),
                       expected_block_ids: tuple[str, ...] = ("contrat-test:p1:1",),
                       ) -> runner.Resultat:
    return runner.Resultat(
        id="s-stable", suite="sinistre:contrat-test", label="bonne_reponse",
        variant=runner.DEFAUT_PAR_SUITE["sinistre"],
        found=True, verdict=verdict, repetition=repetition, doc_id="contrat-test",
        expected_block_ids=list(expected_block_ids),
        proofs=[_preuve(b, hash_preuve=hash_preuve) for b in blocs])


def _cas_sinistre_stabilite() -> runner.Cas:
    return runner.Cas.model_validate({
        "id": "s-stable", "suite": "sinistre", "profile": "vertical",
        "question": "Ce dommage est-il couvert ?",
        "faits": {"description": "Un objet chaud est tombé sur un meuble."},
        "expected": {"found": True, "verdict": ["sous_conditions", "ne_tranche_pas"]},
        "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
        "mode_attendu": "bonne_reponse",
    })


def test_la_stabilite_sinistre_exige_la_preuve_attendue_et_le_verdict_admissible() -> None:
    """Story 5.6 T12 : (a) preuve attendue par répétition, (b) verdict admissible identique.

    Un `quote_hash` divergent ne rend plus le cas instable — AD-3 vérifie la citation au caractère
    près, la borne de l'extrait est un choix du modèle sous l'AD-1 amendé. Ce qui rougit reste ce
    que la stabilité doit garantir : une clause attendue perdue, un verdict qui change.
    """
    cas = _cas_sinistre_stabilite()
    stables = [_resultat_sinistre(i) for i in (1, 2, 3)]
    agregat = runner.agreger_stabilite(stables, [cas], repeat=3)
    assert agregat["cases"]["s-stable"]["stable"] is True

    # La borne de la quote bouge : stable, et la dispersion reste publiée.
    bornes_variables = [_resultat_sinistre(1), _resultat_sinistre(2),
                        _resultat_sinistre(3, hash_preuve="h2")]
    detail = runner.agreger_stabilite(bornes_variables, [cas], repeat=3)["cases"]["s-stable"]
    assert detail["stable"] is True and detail["raisons"] == []
    assert detail["preuves"][2] == ["contrat-test:contrat-test:p1:1:garantie:h2"]

    # (a) une répétition perd la clause attendue de l'oracle : rouge, et la clause est nommée.
    preuve_perdue = [_resultat_sinistre(1), _resultat_sinistre(2),
                     _resultat_sinistre(3, blocs=("contrat-test:p9:9",))]
    detail = runner.agreger_stabilite(preuve_perdue, [cas], repeat=3)["cases"]["s-stable"]
    assert detail["stable"] is False
    assert ("preuve attendue absente sur au moins une répétition : contrat-test:p1:1"
            in detail["raisons"])

    # Les trois répétitions perdent **la même** clause : d'accord entre elles, et rouges — c'est la
    # propriété que la comparaison entre répétitions de 4.2b ne savait pas tenir.
    perte_unanime = [_resultat_sinistre(i, blocs=("contrat-test:p9:9",)) for i in (1, 2, 3)]
    detail = runner.agreger_stabilite(perte_unanime, [cas], repeat=3)["cases"]["s-stable"]
    assert detail["stable"] is False
    assert detail["signatures"][0] == detail["signatures"][2]  # (b) tenu, (a) non

    # (b) le verdict change entre répétitions, dans les valeurs admissibles : rouge.
    verdict_change = [_resultat_sinistre(i) for i in (1, 2)] \
        + [_resultat_sinistre(3, verdict="ne_tranche_pas")]
    detail = runner.agreger_stabilite(verdict_change, [cas], repeat=3)["cases"]["s-stable"]
    assert detail["stable"] is False
    assert "signatures divergentes entre répétitions" in detail["raisons"]

    verdict_inadmissible = [_resultat_sinistre(i) for i in (1, 2)] \
        + [_resultat_sinistre(3, verdict="couvert")]
    detail = runner.agreger_stabilite(verdict_inadmissible, [cas], repeat=3)["cases"]["s-stable"]
    assert detail["stable"] is False
    assert "verdict hors des valeurs admissibles sur au moins une répétition" in detail["raisons"]
    assert "signatures divergentes entre répétitions" in detail["raisons"]


def test_le_jeu_b_congelateur_redevient_stable_quand_la_garantie_fondatrice_est_jugee() -> None:
    """Rejeu hors ligne du cas instable du gate Baloise du 03/09 (T15), tel que `.audit/` le porte.

    Blocs cités par les claims retenues, mesurés : rép.1 et rép.2 portent la garantie fondatrice
    `p21:4` et rendent `sous_conditions` ; rép.3 la perd — le contrôle groupé n'avait rendu aucun
    verdict pour la claim qui la citait — et rend `ne_tranche_pas`. Les trois verdicts sont admis par
    l'oracle : seule la règle (b) de la stabilité voit le défaut, et elle le voit. Vert-après : la
    même rép.3 avec la claim jugée porte `p21:4` et `sous_conditions`.

    Le plancher ne bouge pas : c'est le système qui est corrigé, pas le témoin.
    """
    mesures = {1: (("p21:4", "p21:5", "p21:7", "p21:9", "p12:10", "p12:11", "p12:14"),
                   "sous_conditions"),
               2: (("p21:4", "p21:5", "p21:7", "p21:9"), "sous_conditions"),
               3: (("p21:5", "p21:9", "p15:4"), "ne_tranche_pas")}

    def _repetitions(mesures: dict[int, tuple[tuple[str, ...], str]]) -> list[runner.Resultat]:
        return [_resultat_sinistre(rang, verdict=verdict, hash_preuve=f"borne-{rang}",
                                   blocs=tuple(f"contrat-test:{b}" for b in blocs),
                                   expected_block_ids=())
                for rang, (blocs, verdict) in sorted(mesures.items())]

    # Les deux autres cas du gate vertical Baloise étaient stables : `ne_tranche_pas` ×3.
    stables = [runner.Resultat(id=case_id, suite="sinistre:contrat-test", label="bonne_reponse",
                               variant=runner.DEFAUT_PAR_SUITE["sinistre"], found=True,
                               verdict="ne_tranche_pas", repetition=rang, doc_id="contrat-test",
                               expected_block_ids=[], proofs=[_preuve("contrat-test:p12:6")])
               for case_id in ("b-autre-1", "b-autre-2") for rang in (1, 2, 3)]
    cas = [_cas_sinistre_stabilite().model_copy(update={"id": case_id})
           for case_id in ("s-stable", "b-autre-1", "b-autre-2")]

    # Rouge-avant : la signature diverge, le cas est instable, et le gate mesure 2 cas sur 3.
    agregat = runner.agreger_stabilite(_repetitions(mesures) + stables, cas, repeat=3)
    detail = agregat["cases"]["s-stable"]
    assert detail["stable"] is False
    assert "signatures divergentes entre répétitions" in detail["raisons"]
    assert runner._taux_stabilite(agregat, "sinistre") == (0.6667, 3)

    # Vert-après : la claim qui cite `p21:4` a reçu son verdict, la table AD-6 la relit, et la
    # troisième répétition retombe sur le verdict des deux premières.
    reparee = {**mesures, 3: (("p21:4", "p21:5", "p21:9", "p15:4"), "sous_conditions")}
    agregat = runner.agreger_stabilite(_repetitions(reparee) + stables, cas, repeat=3)
    detail = agregat["cases"]["s-stable"]
    assert detail["stable"] is True and detail["raisons"] == []
    assert runner._taux_stabilite(agregat, "sinistre") == (1.0, 3)


def test_les_trois_repetitions_axa_deviennent_stables_sans_perdre_la_propriete() -> None:
    """Témoin rouge-avant / vert-après, sur la forme mesurée au gate AXA vertical du 03/09.

    Les trois répétitions portent les clauses décisives attendues (`p34:11`, `p34:12`, `p39:9`) et
    le même verdict `ne_tranche_pas` ; ce qui varie, ce sont les **extraits d'exclusion
    auxiliaires** (`p39:12`/`p39:14`/`p40:5`/`p40:6`) et les **bornes** des quotes. L'ancienne
    identité de 4.2b les déclarait divergentes — `stabilite_sinistre = 0.0`, gate rouge.
    """
    attendus = ("contrat-test:p34:11", "contrat-test:p34:12", "contrat-test:p39:9")
    auxiliaires = (("contrat-test:p39:12",), ("contrat-test:p39:14", "contrat-test:p40:5"),
                   ("contrat-test:p40:6",))
    repetitions = [
        _resultat_sinistre(i, verdict="ne_tranche_pas", hash_preuve=f"borne-{i}",
                           blocs=attendus + aux, expected_block_ids=attendus)
        for i, aux in enumerate(auxiliaires, start=1)]
    cas = runner.Cas.model_validate({
        "id": "s-stable", "suite": "sinistre", "profile": "vertical",
        "question": "Ce dommage est-il couvert ?",
        "faits": {"description": "Une bougie a mis le feu à un canapé."},
        "expected": {"found": True, "block_ids": list(attendus),
                     "verdict": ["sous_conditions", "ne_tranche_pas"]},
        "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
        "mode_attendu": "bonne_reponse",
    })

    # Rouge-avant : l'identité de 4.2b comparait l'ensemble des preuves, hachage de citation compris.
    avant = [sorted({f"{p['doc_id']}:{p['block_id']}:{p['kind']}:{p['quote_hash']}"
                     for p in r.proofs}) for r in repetitions]
    assert any(preuves != avant[0] for preuves in avant[1:])

    # Vert-après : les trois répétitions portent l'oracle et le même verdict admissible.
    detail = runner.agreger_stabilite(repetitions, [cas], repeat=3)["cases"]["s-stable"]
    assert detail["stable"] is True and detail["raisons"] == []
    assert detail["preuve_attendue_manquante"] == [[], [], []]
    # La dispersion des preuves n'est pas masquée pour autant : elle est publiée telle quelle.
    assert [len(preuves) for preuves in detail["preuves"]] == [4, 5, 4]
