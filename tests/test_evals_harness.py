"""Contrat 4.1 du cache, des empreintes et des artefacts, entièrement hors réseau."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from server.app.config import Settings
from server.app.corpus.dictionary import Dictionnaire
from server.app.domain.trace import CheckResult, LLMCall, StepTrace, Trace, Usage
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.evals.cache import CacheCorrompu, PersistentResponseCache, empreinte_canonique
from server.evals import run as runner
from server.evals.run import (Cas, RefusDeTourner, Resultat, construire_rapport, ecrire_rapports,
                              namespace_cache, rendre_markdown, variante_du_cas)
from tests.llm_fake import FakeAnthropic, fake_message


class _Mot(BaseModel):
    mot: str


def _namespace() -> dict[str, object]:
    return {
        "models": {"micro": "claude-haiku"},
        "parameters": {"max_tokens": 42},
        "schema": {"type": "object"},
        "variant": "outils",
        "pipeline_digest": "p",
        "prompts_digest": "q",
        "source_hash": "s",
        "ingest_fingerprint": "i",
        "normalize_version": "2",
        "input": {"question": "où ?"},
    }


def _cas() -> Cas:
    return Cas.model_validate({
        "id": "g-un",
        "suite": "guide",
        "profile": "vertical",
        "question": "où ?",
        "expected": {"found": True, "block_ids": ["guide:b1"]},
        "truth": {"source": "lecture_humaine", "validated_by_expert": False},
        "mode_attendu": "bonne_reponse",
    })


def test_cache_persistant_hit_cout_original_et_ecriture_atomique(tmp_path: Path) -> None:
    cache = PersistentResponseCache(tmp_path / "cache", _namespace())
    key = "a" * 64
    value = {"response": {"id": "msg_1", "content": []}, "cost_eur": 0.0123}
    cache.set(key, value)
    assert cache.get(key) == value
    assert not list(tmp_path.rglob("*.tmp"))


async def test_deux_appels_identiques_reutilisent_le_cache_disque_sans_fournisseur(
        tmp_path: Path) -> None:
    cache = PersistentResponseCache(tmp_path / "cache", _namespace())
    fake = FakeAnthropic([fake_message(model=TIERS["micro"], input_tokens=1000, output_tokens=100)])
    client = LlmClient(Settings(_env_file=None, anthropic_api_key=""), anthropic_client=fake,
                       cache=cache)

    async def appel() -> object:
        return await client.parse(
            tier="micro", system_prefix="préfixe", messages=[{"role": "user", "content": "q"}],
            output_model=_Mot, budget=RequestBudget(30, 4, 0.1), step=StepTrace(name="test"),
        )

    premier = await appel()
    second = await appel()
    assert len(fake.requests) == 1
    assert premier.usage.cost_eur > 0 and not premier.usage.cached_response
    assert second.usage.cost_eur == 0 and second.usage.cached_response
    assert second.usage.cost_eur_original == premier.usage.cost_eur


async def test_retry_invalide_puis_valide_cree_un_hit_logique_au_cout_total(
        tmp_path: Path) -> None:
    racine = tmp_path / "cache"
    cache = PersistentResponseCache(racine, _namespace())
    fake = FakeAnthropic([
        fake_message(text='{"inattendu": true}', model=TIERS["micro"],
                     input_tokens=1000, output_tokens=20),
        fake_message(text='{"mot": "valide"}', model=TIERS["micro"],
                     input_tokens=1200, output_tokens=30),
    ])
    client = LlmClient(Settings(_env_file=None, anthropic_api_key=""), anthropic_client=fake,
                       cache=cache)
    step = StepTrace(name="test")
    premier = await client.parse(
        tier="micro", system_prefix="préfixe", messages=[{"role": "user", "content": "q"}],
        output_model=_Mot, budget=RequestBudget(30, 4, 0.1), step=step)
    cout_total = round(sum(call.usage.cost_eur_original for call in step.calls), 4)
    cache.finalize_logical_costs([cout_total])

    cache_suivant = PersistentResponseCache(racine, _namespace())
    sans_fournisseur = FakeAnthropic([])
    second = await LlmClient(
        Settings(_env_file=None, anthropic_api_key=""), anthropic_client=sans_fournisseur,
        cache=cache_suivant).parse(
            tier="micro", system_prefix="préfixe", messages=[{"role": "user", "content": "q"}],
            output_model=_Mot, budget=RequestBudget(30, 4, 0.1), step=StepTrace(name="test"))

    assert premier.parsed.mot == second.parsed.mot == "valide"
    assert len(fake.requests) == 2 and sans_fournisseur.requests == []
    assert second.usage.cached_response and second.usage.cost_eur == 0
    assert second.usage.cost_eur_original == cout_total


def test_alias_de_retry_interrompu_est_refinalise_avec_le_cout_logique(tmp_path: Path) -> None:
    cache = PersistentResponseCache(tmp_path / "cache", _namespace())
    root, retry = "1" * 64, "2" * 64
    value = {"response": {"id": "valide"}, "cost_eur": 0.03}
    assert cache.get(root) is None
    cache.set(retry, value)
    cache._path(root).unlink()

    reprise = PersistentResponseCache(tmp_path / "cache", _namespace())
    assert reprise.get(root) is None
    assert reprise.get(retry) == value
    reprise.finalize_logical_costs([0.05])
    assert reprise.get(root) == {"response": {"id": "valide"}, "cost_eur": 0.05}


@pytest.mark.parametrize("component", [
    "models", "parameters", "schema", "variant", "pipeline_digest", "prompts_digest",
    "source_hash", "ingest_fingerprint", "normalize_version", "input",
])
def test_toute_composante_normative_invalide_la_namespace(component: str) -> None:
    avant = _namespace()
    apres = copy.deepcopy(avant)
    apres[component] = {"changed": True}
    assert empreinte_canonique(avant) != empreinte_canonique(apres)


@pytest.mark.parametrize("component", [
    "schema", "models", "parameters", "variant", "pipeline_digest", "prompts_digest", "source_hash",
    "ingest_fingerprint", "overlay_hash", "dictionary_fingerprint", "normalize_version", "input",
])
def test_namespace_cache_reelle_varie_et_produit_un_miss_persistant(
        component: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cas = _cas()
    entry = SimpleNamespace(source_hash="s", ingest_fingerprint="i", overlay_hash="o")
    corpus = SimpleNamespace(manifest={"doc": entry})
    ctx = SimpleNamespace(
        settings=Settings(_env_file=None), index=SimpleNamespace(corpus=corpus),
        pipeline_digest_hex="p", prompts_digest_hex="q",
        dictionnaire=Dictionnaire(doc_id="doc"), dictionnaires={},
    )
    avant = namespace_cache(cas, ctx, doc_id="doc", variant="outils")

    variant = "outils"
    if component == "schema":
        monkeypatch.setattr(runner, "CACHE_NAMESPACE_SCHEMA", 2)
    elif component == "models":
        monkeypatch.setitem(runner.TIERS, "micro", "modele-change")
    elif component == "parameters":
        ctx.settings = Settings(_env_file=None, usd_eur=0.5)
    elif component == "variant":
        variant = "deterministe"
    elif component == "pipeline_digest":
        ctx.pipeline_digest_hex = "p2"
    elif component == "prompts_digest":
        ctx.prompts_digest_hex = "q2"
    elif component == "source_hash":
        entry.source_hash = "s2"
    elif component == "ingest_fingerprint":
        entry.ingest_fingerprint = "i2"
    elif component == "overlay_hash":
        entry.overlay_hash = "o2"
    elif component == "dictionary_fingerprint":
        ctx.dictionnaire = Dictionnaire(doc_id="doc", raison="différent")
    elif component == "normalize_version":
        monkeypatch.setattr(runner, "normalize_version", "autre")
    elif component == "input":
        cas = cas.model_copy(update={"question": "autre question"})
    apres = namespace_cache(cas, ctx, doc_id="doc", variant=variant)

    cache = PersistentResponseCache(tmp_path / "cache", avant)
    key = "c" * 64
    cache.set(key, {"response": {"id": "valide"}, "cost_eur": 0.01})
    cache.set_namespace(apres)
    assert avant != apres and cache.get(key) is None


def test_entree_corrompue_est_refusee_et_jamais_un_hit(tmp_path: Path) -> None:
    cache = PersistentResponseCache(tmp_path / "cache", _namespace())
    key = "b" * 64
    cache.set(key, {"response": {"id": "msg"}, "cost_eur": 0.01})
    path = next((tmp_path / "cache").rglob("*.json"))
    path.write_text("{tronqué", encoding="utf-8")
    with pytest.raises(CacheCorrompu, match="JSON invalide"):
        cache.get(key)


def test_cache_utf8_invalide_est_cache_corrompu(tmp_path: Path) -> None:
    cache = PersistentResponseCache(tmp_path / "cache", _namespace())
    key = "d" * 64
    cache.set(key, {"response": {"id": "msg"}, "cost_eur": 0.01})
    next((tmp_path / "cache").rglob("*.json")).write_bytes(b"\xff\xfe")
    with pytest.raises(CacheCorrompu, match="UTF-8"):
        cache.get(key)


@pytest.mark.parametrize("version", [True, 1.0])
def test_cache_schema_version_est_exactement_entier_un(tmp_path: Path, version: object) -> None:
    cache = PersistentResponseCache(tmp_path / "cache", _namespace())
    key = "e" * 64
    cache.set(key, {"response": {"id": "msg"}, "cost_eur": 0.01})
    path = next((tmp_path / "cache").rglob("*.json"))
    doc = json.loads(path.read_text("utf-8"))
    doc["schema_version"] = version
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(CacheCorrompu, match="version"):
        cache.get(key)


def test_le_cout_du_retry_ne_fusionne_pas_un_appel_logique_anterieur() -> None:
    trace = Trace(request_id="r", pipeline="guide", steps=[StepTrace(
        name="etape",
        calls=[
            LLMCall(model="m", usage=Usage(cost_eur_original=0.01)),
            LLMCall(model="m", usage=Usage(cost_eur_original=0.02)),
            LLMCall(model="m", usage=Usage(cost_eur_original=0.03)),
        ],
        checks=[CheckResult(name="parse_retry", ok=False)],
    )])
    assert runner._couts_logiques_non_caches(trace) == [0.01, 0.05]


def test_le_cout_du_retry_ignore_un_hit_anterieur_mais_conserve_le_hit_de_relance() -> None:
    trace = Trace(request_id="r", pipeline="guide", steps=[StepTrace(
        name="etape",
        calls=[
            LLMCall(model="m", usage=Usage(cost_eur_original=0.90, cached_response=True)),
            LLMCall(model="m", usage=Usage(cost_eur_original=0.02)),
            LLMCall(model="m", usage=Usage(cost_eur_original=0.03, cached_response=True)),
        ],
        checks=[CheckResult(name="parse_retry", ok=False)],
    )])
    assert runner._couts_logiques_non_caches(trace) == [0.05]


def test_snapshot_mixte_garde_les_octets_disque_dans_son_identite(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    fichier = cases_dir / "guide" / "g-disque.yaml"
    fichier.parent.mkdir(parents=True)
    fichier.write_text("octets initiaux\n", encoding="utf-8")
    disque = _cas().model_copy(update={"id": "g-disque"})
    disque._case_path = fichier
    memoire = _cas().model_copy(update={"id": "g-memoire"})

    avant = runner.snapshot_cas([disque, memoire], cases_dir)
    fichier.write_text("octets modifies\n", encoding="utf-8")
    apres = runner.snapshot_cas([disque, memoire], cases_dir)

    assert avant.cases_hash != apres.cases_hash
    assert fichier.resolve() in avant.files


def test_recall_compte_les_fiches_attendues_et_pas_seulement_found() -> None:
    resultat = Resultat(
        id="g", suite="guide", label="doc_manque", found=True, expected_found=True,
        expected_fiche_ids=["guide:attendue"], cited_fiche_ids=["guide:autre"],
    )
    assert runner._recall([resultat]) == 0.0
    resultat.cited_fiche_ids.append("guide:attendue")
    assert runner._recall([resultat]) == 1.0


def test_variante_incompatible_est_refusee_au_preflight() -> None:
    cas = Cas.model_validate({
        "id": "s-un", "suite": "sinistre", "profile": "vertical", "question": "couvert ?",
        "faits": {"description": "un dégât"}, "expected": {"found": True},
        "truth": {"source": "lecture_humaine", "validated_by_expert": False},
        "mode_attendu": "bonne_reponse",
    })
    with pytest.raises(RefusDeTourner, match="incompatible"):
        variante_du_cas(cas, "outils")


@pytest.mark.parametrize("field", ["block_ids", "fiche_ids"])
def test_les_identifiants_attendus_dupliques_sont_refuses(field: str) -> None:
    brut = _cas().model_dump(mode="json")
    brut["expected"][field] = ["meme", "meme"]
    with pytest.raises(ValueError, match=field):
        Cas.model_validate(brut)


def test_rapport_partiel_distingue_les_non_executes_et_agrege_toutes_les_mesures(
        tmp_path: Path) -> None:
    cas = _cas()
    resultat = Resultat(
        id=cas.id, suite="guide", label="bonne_reponse", variant="outils",
        cost_eur=0.0, cost_eur_original=0.02, ms=11, found=True, expected_found=True,
        expected_block_ids=["guide:b1"],
        claims=[{"claim_id": "c1", "quotes": [{"block_id": "guide:b1", "quote": "preuve"}]}],
    )
    rapport = construire_rapport(
        [resultat], [cas, cas.model_copy(update={"id": "g-deux"})], cases_dir=tmp_path,
        profile="full", max_cost_eur=0.01, complete=False,
        stop_reason="plafond atteint avant g-deux", non_executes=["g-deux"],
    )
    assert rapport["complete"] is False and rapport["unexecuted_cases"] == ["g-deux"]
    assert rapport["metrics"] == {
        "labels": {label: int(label == "bonne_reponse") for label in (
            "bonne_reponse", "mauvais_doc", "doc_manque", "claim_non_soutenu", "faux_refus",
            "citation_introuvable", "parsing")},
        "variants": {"outils": 1},
        "recall": 1.0,
        "average_cost_eur": 0.0,
        "latency_p50_ms": 11,
        "ne_tranche_pas_rate": 0.0,
    }
    md = rendre_markdown(rapport)
    for attendu in ("cases_hash", "recall", "coût moyen", "latence p50", "ne_tranche_pas",
                    "<code>bonne_reponse</code>", "<code>outils</code>", "Cas non exécutés"):
        assert attendu in md
    json_path, md_path = tmp_path / "out" / "result.json", tmp_path / "out" / "result.md"
    ecrire_rapports(rapport, json_path, md_path)
    assert json.loads(json_path.read_text("utf-8"))["complete"] is False
    assert md_path.read_text("utf-8") == md
    assert not list(tmp_path.rglob("*.tmp"))


def test_markdown_echappe_toutes_les_valeurs_dynamiques() -> None:
    rapport = {
        "complete": False, "profile": "p|`\nligne", "cases_completed": 0,
        "cases_planned": 1, "stop_reason": "diag|`\nligne", "cases_hash": "h|`\n",
        "unexecuted_cases": ["c|`\nligne"],
        "metrics": {
            "recall": 0.0, "average_cost_eur": 0.0, "latency_p50_ms": 0,
            "ne_tranche_pas_rate": 0.0, "labels": {label: 0 for label in runner.LABELS},
            "variants": {"v|`\nligne": 1},
        },
        "results": [{
            "id": "i|`\nligne", "suite": "s|`\nligne", "variant": "v|`\nligne",
            "label": "l|`\nligne", "cost_eur": 0.0, "cost_eur_original": 0.0,
            "latency_ms": 0,
        }],
    }
    markdown = rendre_markdown(rapport)
    for brut in ("p|`", "diag|`", "c|`", "i|`", "s|`", "v|`", "l|`"):
        assert brut not in markdown
    assert "<code>" in markdown and "&#124;" in markdown and "&#96;" in markdown and "<br>" in markdown
    assert "`i&#124;" not in markdown, "une entité ne doit pas être enfermée dans un code span"


def test_ladaptateur_ci_transmet_quick_et_tous_ses_chemins(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_evals_live import arguments_evals

    monkeypatch.setenv("EVALS_PROFILE", "full")
    monkeypatch.setenv("EVALS_QUICK", "true")
    monkeypatch.setenv("EVALS_CACHE_DIR", str(tmp_path / "cache-ci"))
    monkeypatch.setenv("EVALS_OUTPUT_JSON", str(tmp_path / "ci.json"))
    monkeypatch.setenv("EVALS_OUTPUT_MARKDOWN", str(tmp_path / "ci.md"))
    args, json_path, md_path = arguments_evals(0.7, tmp_path)

    assert args[:4] == ["--profile", "full", "--max-cost", "0.7"]
    assert "--quick" in args and str(tmp_path / "cache-ci") in args
    assert args[args.index("--exclude-suite") + 1] == "parsing"
    assert json_path == tmp_path / "ci.json" and md_path == tmp_path / "ci.md"
