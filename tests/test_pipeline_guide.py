"""Matrice I/O de `pipelines/guide.py` (spec 1.5), LLM mocké : les cinq étapes, les court-circuits,
la relance unique d'AD-3 et la trace d'AD-10.

`FakeAnthropic` lève sur tout appel non scripté : la **longueur du script est une assertion**. C'est
ainsi que « aucun appel `reason` pour un refus » (AD-5) se vérifie sans compter les appels à la main.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index, words
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.answer import Lacune, Verification
from server.app.domain.document import Document, Node
from server.app.digests import pipeline_digest, prompts_digest
import anthropic
import httpx

from server.app.domain.errors import (
    BudgetExceeded,
    CorpusUnavailable,
    InvalidRequest,
    LlmParse,
    LlmUnavailable,
    Timeout,
)
from server.app.domain.ingest import ManifestEntry
from server.app.domain.profil import Profil
from server.app.domain.question import Turn
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.llm.pricing import PRICES
from server.app.corpus.dictionary import Dictionnaire
from server.app.pipelines.commun import dictionnaire_de, domine
from server.app.pipelines.guide import repondre_guide
from server.app.steps.restituer import PHRASES_DE_LACUNE, PHRASES_DE_REFUS
from tests.llm_fake import FakeAnthropic, fake_message

DOC_ID = "mini"
ARRIVEE = ("Vous disposez de huit jours après votre arrivée pour vous déclarer au Biergercenter de "
           "votre commune de résidence.")
ECOLE = ("L'inscription à l'école se fait auprès de la commune, avant le 1er septembre, sur "
         "présentation du certificat de résidence.")
Q_ARRIVEE = "huit jours après votre arrivée pour vous déclarer"
Q_ECOLE = "L'inscription à l'école se fait auprès de la commune"


@pytest.fixture(scope="module")
def index() -> Index:
    blocs = [
        {"block_id": f"{DOC_ID}:f1:1", "loc": "f1", "seq": 1, "kind": "heading", "text": "Déclarer son arrivée"},
        {"block_id": f"{DOC_ID}:f1:2", "loc": "f1", "seq": 2, "kind": "para", "text": ARRIVEE},
        {"block_id": f"{DOC_ID}:f2:1", "loc": "f2", "seq": 1, "kind": "para", "text": ECOLE},
        {"block_id": f"{DOC_ID}:f3:1", "loc": "f3", "seq": 1, "kind": "para",
         "text": "Le mobilier de jardin relève du contenu de l'habitation."},
    ]
    doc = Document(doc_id=DOC_ID, kind="guide", title="Mini guide", edition="git:test",
                   nodes=[Node(node_id=f"{DOC_ID}:f1", title="Arrivée",
                               items=[{"block_id": f"{DOC_ID}:f1:1"}, {"block_id": f"{DOC_ID}:f1:2"}]),
                          Node(node_id=f"{DOC_ID}:f2", title="École", items=[{"block_id": f"{DOC_ID}:f2:1"}]),
                          Node(node_id=f"{DOC_ID}:f3", title="Divers", items=[{"block_id": f"{DOC_ID}:f3:1"}]),
                          Node(node_id=f"{DOC_ID}:root", title="Mini",
                               items=[{"node_id": f"{DOC_ID}:f1"}, {"node_id": f"{DOC_ID}:f2"},
                                      {"node_id": f"{DOC_ID}:f3"}])],
                   blocks=blocs)
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    manifest = {DOC_ID: ManifestEntry(status="servi", source_hash="sha-source", ingest_fingerprint="fp-1",
                                      document_hash="sha-doc", edition="git:test")}
    return Index(Corpus(documents={DOC_ID: doc}, manifest=manifest,
                        summaries={DOC_ID: "# Mini guide\n- f1 Déclarer son arrivée\n- f2 École"}))


def _settings(**kw) -> Settings:
    kw.setdefault("guide_doc_id", DOC_ID)
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget(deadline_s: float = 30.0) -> RequestBudget:
    return RequestBudget(deadline_s=deadline_s, max_attempts=6, max_cost_eur=0.20)


def test_la_trace_desarme_un_dictionnaire_signe_applique_a_un_autre_document() -> None:
    """M1, revue 2.5 : `dictionnaire_de` porte l'activation au document de la requête."""
    dico = Dictionnaire(charge=True, doc_id=DOC_ID, validated=True, corpus_ok=True)
    trace_dico = dictionnaire_de(dico, "autre-document")
    assert trace_dico is not None and trace_dico.court_circuit_actif is False


def _comprendre(intent: str = "question", *, terms: list[str] | None = None,
                clarification: str | None = None, language: str = "fr",
                facettes: list[str] | None = None, question_resolue: str | None = None) -> dict:
    """*comprendre* arrête aussi le découpage de la question en facettes (AD-4, revue Codex 1.5,
    tour 3) : c'est lui le barème de `complete`, et il est fixé avant toute rédaction.

    `question_resolue` (story 2.2) rend explicite ce que le mock résout : sans lui, la question
    brute et la question résolue se confondaient dans tous les tests, et rien ne pouvait montrer sur
    **laquelle des deux** le court-circuit d'AD-5 porte. Défaut inchangé — les tests écrits avant
    2.2 gardent leur script byte-identique.

    Les deux champs sont **exclusifs** (AD-5 : « exactement l'un des deux renseigné, sinon relance
    motivée »), et le helper le fait respecter au lieu d'en ignorer un (revue 2.2, P12) : un helper
    de test qui ignore silencieusement ce qu'on lui demande fait passer des tests qui ne testent
    rien — ici, une clarification scriptée avec une `question_resolue` aurait laissé croire que le
    pipeline arbitre, alors que c'est le schéma de sortie qui tranche.
    """
    if clarification is not None and question_resolue is not None:
        raise ValueError("AD-5 : `clarification` et `question_resolue` sont exclusifs — pas les deux")
    resolue = None if clarification else (question_resolue or "Quel délai pour déclarer mon arrivée ?")
    return fake_message(model=TIERS["micro"], text=json.dumps({
        "intent": intent, "question_resolue": resolue, "clarification": clarification,
        "language": language, "terms": terms if terms is not None else ["arrivée", "école"],
        "themes": [], "facettes": facettes if facettes is not None else ["délai de déclaration"],
        "bien": None, "evenement": None, "lieu": None, "cause": None, "moment": None}))


def _rediger(*claims: tuple[str, str, list[tuple[str, str]]], transition: bool = False,
             limite: str | None = None) -> dict:
    segments = [{"text": f"Segment {cid}.", "kind": "factuel", "claim_ids": [cid]} for cid, _, _ in claims]
    if transition:
        segments.append({"text": "En résumé.", "kind": "transition", "claim_ids": []})
    if limite is not None:
        segments.append({"text": limite, "kind": "limite", "claim_ids": []})
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": segments,
        "claims": [{"claim_id": cid, "text": texte,
                    "quotes": [{"block_id": b, "quote": q} for b, q in quotes]}
                   for cid, texte, quotes in claims]}))


def _verdicts(*paires: tuple[str, bool], facettes: list[list[str]] | None = None,
              segments: dict[int, bool] | None = None, nb_segments: int = 8) -> dict:
    """Verdicts groupés + phrases soutenues + couverture des facettes (AD-4). `facettes[i]` = les
    `claim_id` attribués à la facette de rang `i` de la question (le découpage vient de
    *comprendre*). Par défaut une facette, couverte par les claims jugées pertinentes (la
    question-témoin des tests n'en pose qu'une), et toutes les phrases soumises jugées soutenues."""
    if facettes is None:
        facettes = [[c for c, ok in paires if ok]]
    soutiens = {i: True for i in range(nb_segments)} | (segments or {})
    return fake_message(model=TIERS["micro"], text=json.dumps(
        {"verdicts": [{"claim_id": c, "pertinente": p} for c, p in paires],
         "facettes": [{"facette": rang, "claim_ids": ids} for rang, ids in enumerate(facettes)],
         "segments": [{"segment": i, "soutenu": ok} for i, ok in sorted(soutiens.items())]}))


async def _run(index: Index, script: list, *, historique: list[Turn] | None = None,
               settings: Settings | None = None, budget: RequestBudget | None = None,
               question: str = "Quel délai pour déclarer mon arrivée ?", lang: str | None = None,
               dictionnaire: Any = None, variant: str | None = "deterministe"):
    settings = settings or _settings()
    fake = FakeAnthropic(script)
    client = LlmClient(settings, anthropic_client=fake)
    answer, trace = await repondre_guide(question, historique or [], Profil(), corpus=index.corpus,
                                         index=index, client=client, settings=settings,
                                         request_id="req-test", lang=lang, budget=budget or _budget(),
                                         dictionnaire=dictionnaire, variant=variant)
    return answer, trace, fake


def _avec_navigation_outils(script: list, variant: str) -> list:
    """Ajoute le tour minimal de *retrouver* aux scénarios communs paramétrés sur les deux variantes."""
    if variant != "outils":
        return script
    navigation = fake_message(
        model=TIERS["micro"], stop_reason="tool_use", content=[
            {"type": "tool_use", "id": "chercher-commun", "name": "chercher",
             "input": {"termes": ["arrivée", "école"]}},
            {"type": "tool_use", "id": "ouvrir-arrivee", "name": "ouvrir_noeud",
             "input": {"node_id": f"{DOC_ID}:f1", "focus_block_id": f"{DOC_ID}:f1:2"}},
            {"type": "tool_use", "id": "ouvrir-ecole", "name": "ouvrir_noeud",
             "input": {"node_id": f"{DOC_ID}:f2", "focus_block_id": f"{DOC_ID}:f2:1"}},
        ],
    )
    conclusion = fake_message(model=TIERS["reason"], stop_reason="end_turn", content=[])
    return [script[0], navigation, conclusion, *script[1:]]


def _dictionnaire(tmp_path: Path, index: Index, termes: dict[str, list[str]], *, validated: bool,
                  source_hash: str = "sha-source", doc_id: str = DOC_ID,
                  intents: dict[str, list[str]] | None = None):
    """Un `Dictionnaire` **chargé depuis un fichier**, comme le serveur le fait au démarrage."""
    import json

    from server.app.corpus.dictionary import load_dictionary
    from server.app.corpus.racine import _lecture_interne_sans_racine

    # `tmp_path` et non `tempfile.mkdtemp()` : ces helpers sont appelés en boucle, et des dossiers
    # jamais nettoyés s'accumulaient dans `/tmp` à chaque exécution de la suite.
    dossier = tmp_path / (f"dico-{len(termes)}-{validated}-"
                          f"{abs(hash((source_hash, tuple(sorted(termes)), tuple(sorted(intents or {})))))}")
    dossier.mkdir(parents=True, exist_ok=True)
    signature = ({"validated": True, "validated_by": "Lancelot Oudin",
                  "validated_at": "2026-08-25T10:00:00Z"} if validated else
                 {"validated": False, "validated_by": None, "validated_at": None})
    (dossier / "dictionary.json").write_text(json.dumps(
        # Les empreintes suivent le corpus **réellement** passé, jamais `DOC_ID` en dur : un test
        # qui monte son propre document se retrouvait sinon avec un dictionnaire déclaré périmé.
        {"schema_version": "1",
         "corpus_source_hashes": {d: source_hash for d in index.corpus.served},
         "corpus": termes,
         # Story 2.5 : les déclencheurs d'intention. Ils ne décident rien (AD-5) — ils permettent
         # au pipeline de **compter** ce qui corrobore l'intention rendue par *comprendre*.
         "intents": intents or {}, "candidate_questions": {}, **signature}, ensure_ascii=False), "utf-8")
    with _lecture_interne_sans_racine(dossier) as lecture:
        return load_dictionary(dossier, index.corpus, doc_id, lecture=lecture)


BONNE = ("c1", "Le délai est de huit jours.", [(f"{DOC_ID}:f1:2", Q_ARRIVEE)])
BONNE_2 = ("c2", "L'inscription passe par la commune.", [(f"{DOC_ID}:f2:1", Q_ECOLE)])
MAUVAISE = ("c3", "Le délai est de quinze jours.", [(f"{DOC_ID}:f1:2", "quinze jours après votre arrivée")])
AUTRE_ECOLE = ("c2", "L'inscription se fait avant le 1er septembre.",
               [(f"{DOC_ID}:f2:1", "avant le 1er septembre, sur présentation du certificat")])


# --- nominal -----------------------------------------------------------------
async def test_a_sourced_answer_runs_the_five_steps_and_carries_its_trace(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, BONNE_2, transition=True),
                                             _verdicts(("c1", True), ("c2", True))])
    assert fake.remaining_script == 0 and len(fake.requests) == 3  # trois appels reason — pas un de plus
    assert answer.found is True and answer.reason is None
    assert answer.texte == "Segment c1. Segment c2. En résumé."
    assert [c.claim_id for c in answer.claims] == ["c1", "c2"]
    assert all(c.status.retrouvee and c.status.pertinente for c in answer.claims)
    assert all(c.status.edition == "git:test" for c in answer.claims)

    # AD-1 : la chaîne est fixe et complète, dans cet ordre
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier", "restituer"]
    assert [s.tier for s in trace.steps] == ["reason", "reason", "reason", "reason", None]
    assert trace.steps[1].calls == []  # *retrouver* déterministe n'appelle aucun modèle
    assert trace.pipeline == "guide" and trace.variant == "deterministe" and trace.request_id == "req-test"
    # Story 1.9 : `faits_compris` et `verdict` sont les deux champs de l'unique `Answer` que le guide
    # ne remplit **pas**. Le profil du guide n'est pas « ce qu'on a compris d'un sinistre », et une
    # question du guide n'a pas de verdict à porter (AD-4).
    assert answer.faits_compris is None and answer.verdict is None
    assert fake.requests[1]["max_tokens"] == _settings().rediger_max_tokens == 2048
    # AD-10 : `intent` est le seul champ de la ligne de log que l'API ne peut pas lire sur l'`Answer` —
    # il n'existe que dans la trace, et seul le pipeline le produit.
    assert trace.intent == "question"
    assert trace.source_hash == {DOC_ID: "sha-source"} and trace.ingest_fingerprint == {DOC_ID: "fp-1"}
    assert trace.pipeline_digest and trace.prompts_digest
    assert trace.thresholds["quote_min_chars"] == 25 and trace.retries == 0
    assert trace.total_cost_eur > 0 and trace.deadline_remaining_s is not None


async def test_variante_outils_dispatches_only_retrouver_and_keeps_the_fixed_chain(index: Index) -> None:
    tool_call = fake_message(
        model=TIERS["micro"], stop_reason="tool_use",
        content=[{"type": "tool_use", "id": "toolu_search", "name": "chercher",
                  "input": {"termes": ["arrivée"]}},
                 {"type": "tool_use", "id": "toolu_open", "name": "ouvrir_noeud",
                  "input": {"node_id": f"{DOC_ID}:f1", "focus_block_id": f"{DOC_ID}:f1:2"}}])
    answer, trace, fake = await _run(
        index, [_comprendre(terms=["arrivée"]), tool_call,
                fake_message(model=TIERS["reason"], stop_reason="end_turn", content=[]),
                _rediger(BONNE), _verdicts(("c1", True))], variant="outils")
    assert answer.found and trace.variant == "outils"
    assert [s.name for s in trace.steps] == [
        "comprendre", "retrouver", "rediger", "verifier", "restituer"]
    retrouver = trace.steps[1]
    assert len(retrouver.calls) == 2 and retrouver.tier == "reason"
    assert retrouver.opened_block_ids == [f"{DOC_ID}:f1:1", f"{DOC_ID}:f1:2"]
    assert all(call.tools == ["sommaire", "ouvrir_noeud", "chercher", "definitions"]
               for call in retrouver.calls)
    assert fake.requests[3]["max_tokens"] == _settings().rediger_max_tokens == 2048
    assert "outils_rediger_max_tokens" not in trace.thresholds
    assert len(fake.requests) == 5


async def test_outils_without_a_useful_tool_falls_back_to_deterministic_retrieval(index: Index) -> None:
    answer, trace, fake = await _run(
        index, [_comprendre(terms=["arrivée"]),
                fake_message(model=TIERS["reason"], stop_reason="end_turn", content=[]),
                _rediger(BONNE), _verdicts(("c1", True))], variant="outils")
    assert answer.found and trace.variant == "outils"
    retrouver = trace.steps[1]
    assert retrouver.tier == "reason" and len(retrouver.calls) == 1
    assert retrouver.opened_block_ids == [f"{DOC_ID}:f1:1", f"{DOC_ID}:f1:2"]
    assert any(c.name == "repli_deterministe" and not c.ok for c in retrouver.checks)
    assert [request["model"] for request in fake.requests] == [
        TIERS["reason"], TIERS["reason"], TIERS["reason"], TIERS["reason"]]


async def test_outils_empty_truncated_result_falls_back_and_merges_trace(
        index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.app.pipelines import guide as guide_module

    real_fallback = guide_module.retrouver_deterministe

    def fallback_with_check(*args: Any, **kwargs: Any):
        result, step = real_fallback(*args, **kwargs)
        step.checks.append(CheckResult(name="controle_deterministe", ok=True, detail="fusionné"))
        return result, step

    monkeypatch.setattr(guide_module, "retrouver_deterministe", fallback_with_check)
    tool_candidates = [b for b, _ in index.chercher(
        ["école"], limit=_settings().search_limit, doc_id=DOC_ID,
        groupes_prioritaires=["délai de déclaration"])]
    tool_call = fake_message(
        model=TIERS["micro"], stop_reason="tool_use",
        content=[{"type": "tool_use", "id": "toolu_search", "name": "chercher",
                  "input": {"termes": ["école"]}}])
    answer, trace, _fake = await _run(
        index, [_comprendre(terms=["arrivée"]), tool_call,
                fake_message(model=TIERS["micro"], stop_reason="end_turn", content=[]),
                _rediger(BONNE), _verdicts(("c1", True))], variant="outils")

    assert answer.found
    retrouver = trace.steps[1]
    assert retrouver.opened_block_ids == [f"{DOC_ID}:f1:1", f"{DOC_ID}:f1:2"]
    assert retrouver.discarded_block_ids == tool_candidates
    assert [check.name for check in retrouver.checks] == [
        "candidats_non_ouverts", "repli_deterministe", "controle_deterministe"]
    assert retrouver.calls and retrouver.usage.cost_eur > 0


async def test_outils_partial_truncated_result_keeps_its_useful_blocks_without_fallback(
        index: Index) -> None:
    tool_candidates = [b for b, _ in index.chercher(["école"], limit=_settings().search_limit,
                                                     doc_id=DOC_ID)]
    tool_call = fake_message(
        model=TIERS["micro"], stop_reason="max_tokens",
        content=[{"type": "tool_use", "id": "toolu_search", "name": "chercher",
                  "input": {"termes": ["école"]}},
                 {"type": "tool_use", "id": "toolu_open", "name": "ouvrir_noeud",
                  "input": {"node_id": f"{DOC_ID}:f2"}}])
    answer, trace, fake = await _run(
        index, [_comprendre(terms=["arrivée"]), tool_call,
                fake_message(model=TIERS["micro"], stop_reason="end_turn", content=[]),
                _rediger(BONNE_2), _verdicts(("c2", True))], variant="outils")

    retrouver = trace.steps[1]
    assert retrouver.opened_block_ids == [f"{DOC_ID}:f2:1"]
    assert retrouver.discarded_block_ids == [
        block_id for block_id in tool_candidates if block_id != f"{DOC_ID}:f2:1"]
    assert not any(check.name == "repli_deterministe" for check in retrouver.checks)
    assert answer.found and not answer.complete and answer.reason is None
    assert trace.truncations == 1 and len(fake.requests) == 5


async def test_variante_inconnue_is_rejected_before_any_paid_call(index: Index) -> None:
    fake = FakeAnthropic([])
    client = LlmClient(_settings(), anthropic_client=fake)
    budget = _budget()
    with pytest.raises(InvalidRequest, match="variant inconnu"):
        await repondre_guide(
            "q", [], Profil(), corpus=index.corpus, index=index, client=client,
            settings=_settings(), request_id="req-test", budget=budget, variant="inconnue")
    assert fake.requests == [] and budget.attempts == 0 and budget.cost_eur == 0


async def test_variante_outils_keeps_its_failed_call_in_the_partial_trace(index: Index) -> None:
    panne = anthropic.APIStatusError("529", response=httpx.Response(
        529, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
    with pytest.raises(LlmUnavailable) as capture:
        await _run(index, [_comprendre(terms=["arrivée"]), panne], variant="outils")
    trace = capture.value.trace
    assert trace is not None and trace.variant == "outils"
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver"]
    assert len(trace.steps[-1].calls) == 1


async def test_full_context_traverses_the_real_pipeline_and_publishes_its_trace(index: Index) -> None:
    selection = fake_message(
        model=TIERS["micro"], text=json.dumps({"block_ids": [f"{DOC_ID}:f1:2"]}))
    answer, trace, fake = await _run(
        index, [_comprendre(terms=["arrivée"]), selection,
                _rediger(BONNE), _verdicts(("c1", True))],
        variant="full_context")

    assert answer.found is True and fake.remaining_script == 0
    assert trace.variant == "full_context"
    retrieval = next(step for step in trace.steps if step.name == "retrouver")
    assert retrieval.opened_block_ids == [f"{DOC_ID}:f1:2"]
    assert len(retrieval.calls) == 1 and retrieval.calls[0].tier == "reason"


async def test_direct_pipeline_without_variant_uses_runtime_versioned_setting(index: Index) -> None:
    selection = fake_message(
        model=TIERS["micro"], text=json.dumps({"block_ids": [f"{DOC_ID}:f1:2"]}))
    _answer, trace, fake = await _run(
        index, [_comprendre(terms=["arrivée"]), selection,
                _rediger(BONNE), _verdicts(("c1", True))],
        settings=_settings(retrieval_variant="full_context"), variant=None)
    assert trace.variant == "full_context" and fake.remaining_script == 0


async def test_full_context_preflight_incident_keeps_partial_pipeline_trace(
        index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.app.steps import retrouver as retrieval_module

    caps = dict(retrieval_module.MODEL_CAPS[TIERS["reason"]])
    monkeypatch.setitem(
        retrieval_module.MODEL_CAPS, TIERS["reason"], {**caps, "context_window": 1})
    with pytest.raises(BudgetExceeded) as capture:
        await _run(index, [_comprendre(terms=["arrivée"])], variant="full_context")

    trace = capture.value.trace
    assert trace is not None and trace.variant == "full_context"
    assert [step.name for step in trace.steps] == ["comprendre", "retrouver"]
    assert trace.steps[-1].calls == []
    assert trace.steps[-1].opened_block_ids == []


async def test_the_trace_never_carries_the_text_of_a_block(index: Index) -> None:
    """AD-10 : « la trace ne contient jamais le texte des blocs »."""
    _answer, trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE),
                                               _verdicts(("c1", True))])
    serialise = trace.model_dump_json()
    for bloc in index.corpus.documents[DOC_ID].blocks:
        assert bloc.text not in serialise
    assert Q_ARRIVEE not in serialise  # ni le texte du bloc, ni la citation qui en est tirée
    assert f"{DOC_ID}:f1:2" in serialise  # les identifiants, eux, y sont (AD-10 : blocs ouverts)


async def test_the_pipeline_fills_the_retrieval_budget_from_the_settings(index: Index) -> None:
    """Reprise 1.4 : `max_blocks`/`max_tokens` existaient sans que personne ne les renseigne.

    **Revue Codex 2.3 (B3), puis story 4.2f.** La borne mord au point que le bloc École cité
    n'est plus transmis, la claim est rejetée (« bloc non fourni »), la relance rejoue la même
    ébauche. La 2.3 avait fermé le mensonge — publier un `AbsenceProof` au terme d'une lecture
    tronquée affirme l'exhaustivité que la troncature dément — par un échec terminal 503. La 4.2f
    ferme le même mensonge **sans la panne** : la chaîne s'achève, *restituer* rend un `Answer`
    `found=false` qui ne prouve aucune absence mais chiffre ce qui a été lu.
    """
    settings = _settings(retrieval_max_blocks=1)
    answer, trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE_2), _rediger(BONNE_2)],
                                      settings=settings)
    retrouver = trace.steps[1]
    assert retrouver.name == "retrouver"
    # I2 rend d'abord le compagnon hit de la fenêtre Arrivée ; le bloc École du draft reste donc
    # explicitement hors budget, ce qui conserve la preuve de rejet visée par ce test.
    assert retrouver.opened_block_ids == [f"{DOC_ID}:f1:2"]
    assert trace.truncations == 1
    # toute la chaîne a bien tourné, relance comprise (la seconde ébauche est identique, donc pas
    # de seconde vérification), et *restituer* la termine désormais au lieu d'une exception.
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "rediger", "restituer"]
    # Aucune absence affirmée : `reason` reste vide, c'est le porteur typé de 4.2f qui parle.
    assert answer.found is False and answer.complete is False and answer.reason is None
    assert answer.lecture_partielle is not None
    assert answer.lecture_partielle.blocks_read == 1
    assert answer.lecture_partielle.nodes_read == 1
    assert answer.lecture_partielle.documents == [DOC_ID]
    # La réponse dit ce qui lui manque, et ce qui a été écarté reste visible.
    assert PHRASES_DE_LACUNE["fr"]["lecture_bornee"] in answer.unknown
    assert answer.rejected_claims
    assert any(c.name == "lecture_partielle" for c in trace.steps[-1].checks)


async def test_sous_lecture_bornee_une_relance_qui_trouve_bat_un_acquis_vide(index: Index) -> None:
    """Conséquence assumée de la story 4.2f sur la dominance d'AD-3, épinglée pour être visible.

    `domine()` exige que la relance ne laisse pas **plus de manques** que l'acquis. Tant qu'un refus
    sous lecture bornée ne portait aucune lacune, son `nb_manques` valait zéro : une relance qui
    trouvait réellement une affirmation vérifiée — et portait donc la lacune `lecture_bornee` — ne
    dominait pas, l'acquis vide faisait foi, et la requête ressortait en 503. Un acquis vide n'a
    pourtant rien à perdre : c'est exactement le raisonnement que `sinistre.relance_trouve_clause`
    écrivait déjà pour l'autre pipeline (« une clause vérifiée bat zéro claim […] seul l'axe des
    manques peut reculer, ce qui est exactement le vide qu'une lecture tronquée transformait en
    503 »). Depuis que *vérifier* nomme la borne sur ce refus, les deux pipelines se comportent
    pareil, sans qu'aucune règle de dominance ait été touchée.
    """
    settings = _settings(retrieval_max_blocks=2)  # f2:1 reste fermé : la lecture est bornée
    answer, trace, fake = await _run(
        index, [_comprendre(), _rediger(MAUVAISE), _rediger(BONNE), _verdicts(("c1", True))],
        settings=settings)
    assert fake.remaining_script == 0 and trace.truncations == 1
    # La relance a été adoptée : la réponse vérifiée est servie, jamais jetée au profit d'un refus.
    assert answer.found is True and answer.reason is None and answer.lecture_partielle is None
    assert [c.claim_id for c in answer.claims] == ["c1"]
    # La borne se dit alors où AD-4 la veut : dans `unknown[]`, avec `complete=False`.
    assert answer.complete is False
    assert PHRASES_DE_LACUNE["fr"]["lecture_bornee"] in answer.unknown


async def test_a_refusal_without_truncation_stays_a_served_answer(index: Index) -> None:
    """Le pendant du précédent (revue Codex 2.3, B3) : la garde ne mange pas le refus honnête.

    Lecture **non** tronquée et zéro claim survivante : rien n'a borné ce que nous avons lu, donc la
    preuve d'absence dit vrai et l'utilisateur reçoit un `Answer` complet (200), jamais une 503.
    """
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(MAUVAISE), _rediger(MAUVAISE)])
    assert fake.remaining_script == 0 and trace.truncations == 0
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "claims_rejetes"


# --- court-circuits d'AD-5 ---------------------------------------------------
async def test_an_out_of_scope_intent_never_reaches_the_reason_tier(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre("meteo")])
    assert fake.remaining_script == 0 and len(fake.requests) == 1  # le seul appel est `reason`
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "hors_perimetre" and answer.reason.terms_searched == []
    assert answer.texte and answer.segments[0].kind == "limite"


async def test_un_refus_autonome_contradictoire_est_normalise_puis_refuse_sans_retrieval(
        index: Index) -> None:
    answer, trace, fake = await _run(index, [
        _comprendre("hors_perimetre", clarification="Quel objet désignez-vous ?"),
    ], question="Sa demande complète concerne un autre sujet.")

    assert [step.name for step in trace.steps] == ["comprendre", "restituer"]
    assert len(fake.requests) == 1 and "retrouver" not in [step.name for step in trace.steps]
    assert answer.reason is not None and answer.reason.kind == "hors_perimetre"
    assert answer.clarification is None
    comprendre = next(step for step in trace.steps if step.name == "comprendre")
    (neutralisee,) = [
        check for check in comprendre.checks
        if check.name == "clarification_refus_neutralisee"
    ]
    assert neutralisee.ok is False
    assert [check for check in comprendre.checks if check.name == "intention_expliquee"] == []


async def test_a_clarification_short_circuits_and_is_carried_by_the_answer(index: Index) -> None:
    """AD-5 + AD-4 (assertions complétées en story 2.2) : la clarification est une **absence
    intégrale**, pas un refus de recherche.

    *retrouver* n'a pas tourné — la question n'était pas autonome, donc rien n'a été cherché — et la
    preuve d'absence doit le dire : `terms_searched == []` et `blocks_scanned == 0` (AD-4). Annoncer
    des termes cherchés ou des blocs parcourus ici affirmerait une recherche qui n'a pas eu lieu, et
    *comprendre* n'a de toute façon construit aucune `question_resolue` d'où les tirer.
    """
    answer, trace, fake = await _run(index, [_comprendre(clarification="De quelles personnes parlez-vous ?")],
                                     question="Et pour eux, c'est pareil ?")
    assert fake.remaining_script == 0 and [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert "retrouver" not in [s.name for s in trace.steps]
    assert len(fake.requests) == 1  # le seul appel est `reason` : aucun appel de plus n'est facturé
    assert answer.found is False and answer.clarification == "De quelles personnes parlez-vous ?"
    assert answer.reason is not None and answer.reason.kind == "clarification_requise"
    assert answer.reason.terms_searched == [] and answer.reason.blocks_scanned == 0
    assert answer.reason.variants_count == 0 and answer.reason.documents == []


async def test_une_langue_forcee_est_demandee_pour_la_clarification_elle_meme(index: Index) -> None:
    """Revue Codex 2.4, B1 : `lang="de"` sur une question française rendait un refus allemand
    accompagné d'une clarification française, sans qu'aucun champ ne le signale (`lang_fallback`
    reste faux sur un forçage). La langue de réponse est désormais transmise à l'unique appel de
    *comprendre*, le seul endroit où la clarification s'écrit (AD-5, AD-16)."""
    answer, _trace, fake = await _run(
        index, [_comprendre(clarification="Von welchen Personen sprechen Sie?", language="fr")],
        question="Et pour eux, c'est pareil ?", lang="de")
    (req,) = fake.requests
    consigne = req["messages"][0]["content"].split("</untrusted>")[-1]
    assert "Écris `clarification` en de (allemand), quelle que soit la langue de la question." in consigne
    assert answer.lang == "de" and answer.lang_fallback is False
    assert answer.clarification == "Von welchen Personen sprechen Sie?"


async def test_une_detection_non_servie_pose_quand_meme_la_question_de_clarification(index: Index) -> None:
    """Revue Codex 2.4, tour 2 (NB1) : la détection n'est connue qu'**après** la réponse — la
    clarification est déjà écrite, dans la langue de l'utilisateur, et rien ne peut plus la traduire
    (AD-5 : « un **seul** appel `reason` » avant le court-circuit). AD-5 exige néanmoins
    `Answer.clarification` sur ce chemin, et l'AC 2.2 la reconduit en tour d'historique : elle est
    servie. Ce qui n'est pas tu, c'est la divergence — `lang_fallback` la publie, le refus français
    de *restituer* entoure la question, et *comprendre* la trace (AD-16)."""
    answer, trace, _fake = await _run(
        index, [_comprendre(clarification="¿De qué personas habla?", language="es")],
        question="¿Y para ellos, es lo mismo?")
    assert answer.lang == "fr" and answer.lang_fallback is True
    assert answer.clarification == "¿De qué personas habla?"
    assert answer.reason is not None and answer.reason.kind == "clarification_requise"
    assert "précisez-la" in answer.texte  # la phrase française de *restituer* reste servie
    comprendre_step = next(s for s in trace.steps if s.name == "comprendre")
    (check,) = [c for c in comprendre_step.checks if c.name == "clarification_langue_non_affirmee"]
    assert check.ok is False

async def test_an_empty_retrieval_never_pays_for_a_reason_call(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre(terms=["hippopotame"])])
    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "restituer"]
    assert answer.found is False and answer.reason is not None
    # l'`AbsenceProof` dit ce qui a été cherché, jamais que l'information n'existe pas (AD-1/AD-4)
    assert answer.reason.kind == "zero_hit" and answer.reason.terms_searched == ["hippopotame"]
    assert answer.reason.blocks_scanned == 4 and answer.reason.documents == [DOC_ID]
    assert answer.reason.variants_count == 0  # aucun dictionnaire validé (AD-5, story 2.1)


# --- relance unique (AD-3) ---------------------------------------------------
async def test_a_rejected_claim_triggers_exactly_one_retry_that_can_save_the_answer(index: Index) -> None:
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)),
        _rediger(BONNE, BONNE_2), _verdicts(("c1", True), ("c2", True))])
    assert fake.remaining_script == 0
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "rediger", "verifier", "restituer"]
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1", "c2"]
    assert trace.retries == 1
    # la claim rejetée au premier tour n'est plus dans la réponse : c'est le second draft qui compte
    assert answer.rejected_claims == []


async def test_an_identical_retry_stops_the_loop(index: Index) -> None:
    """AD-3 : « chaque relance change quelque chose (un draft identique arrête la relance) »."""
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(MAUVAISE), _rediger(MAUVAISE)])
    assert fake.remaining_script == 0  # aucune seconde vérification : elle rendrait le même résultat
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "rediger", "restituer"]
    assert [c.name for c in trace.steps[4].checks] == ["relance_sans_effet"]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "claims_rejetes"
    assert [c.claim_id for c in answer.rejected_claims] == ["c3"]


async def test_no_surviving_claim_after_the_retry_is_a_motivated_refusal(index: Index) -> None:
    autre_mauvaise = ("c4", "Le délai est de trente jours.",
                      [(f"{DOC_ID}:f1:2", "trente jours après votre arrivée")])
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(MAUVAISE), _rediger(autre_mauvaise)])
    assert fake.remaining_script == 0
    assert answer.found is False and answer.claims == []
    assert answer.reason is not None and answer.reason.kind == "claims_rejetes"
    assert answer.reason.terms_searched == ["arrivée", "école"]
    # exactement **une** relance : deux appels `reason`, pas trois
    assert sum(1 for s in trace.steps if s.name == "rediger") == 2 and trace.retries == 1
    assert [c.claim_id for c in answer.rejected_claims] == ["c4"]


async def test_the_retry_carries_the_motive_composed_by_our_code(index: Index) -> None:
    _answer, _trace, fake = await _run(index, [_comprendre(), _rediger(MAUVAISE), _rediger(MAUVAISE)])
    relance = fake.requests[2]["messages"][0]["content"]
    assert '<untrusted kind="motif">' in relance  # AD-15 : le motif est délimité comme tout le reste
    assert "introuvable dans le bloc mini:f1:2" in relance
    premier = fake.requests[1]["messages"][0]["content"]
    assert '<untrusted kind="motif">' not in premier
    # le préfixe système est byte-identique entre l'appel et sa relance (AD-9, cache)
    assert fake.requests[1]["system"] == fake.requests[2]["system"]


# --- bornes d'entrée ---------------------------------------------------------
async def test_a_history_beyond_the_bound_is_refused_never_truncated(index: Index) -> None:
    historique = [Turn(role="user", texte=f"tour {i}") for i in range(7)]
    with pytest.raises(InvalidRequest, match="historique de 7 tours"):
        await _run(index, [], historique=historique)  # script vide : rien n'a été facturé
    ok = [Turn(role="user", texte=f"tour {i}") for i in range(6)]
    answer, _trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE), _verdicts(("c1", True))],
                                       historique=ok)
    assert answer.found is True


async def test_an_exhausted_deadline_stops_before_the_first_call(index: Index) -> None:
    with pytest.raises(Timeout, match="comprendre"):
        await _run(index, [], budget=_budget(deadline_s=0.0))


async def test_the_forced_language_travels_to_the_answer(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [_comprendre(language="fr"), _rediger(BONNE),
                                               _verdicts(("c1", True))], lang="en")
    assert answer.lang == "en" and answer.lang_fallback is False


async def test_a_supported_detection_drives_the_answer_without_fallback(index: Index) -> None:
    answer, _trace, _fake = await _run(
        index, [_comprendre(language="en", terms=["arrivée"]), _rediger(BONNE),
                _verdicts(("c1", True))])
    assert answer.lang == "en" and answer.lang_fallback is False


async def test_an_english_refusal_produced_by_the_guide_pipeline_stays_english(index: Index) -> None:
    answer, _trace, fake = await _run(index, [_comprendre("meteo", language="en")])
    assert fake.remaining_script == 0
    assert answer.lang == "en" and answer.lang_fallback is False
    assert answer.texte == PHRASES_DE_REFUS["en"]["hors_perimetre"]


async def test_an_unsupported_detection_falls_back_on_a_found_answer_too(index: Index) -> None:
    answer, _trace, fake = await _run(
        index, [_comprendre(language="es"), _rediger(BONNE), _verdicts(("c1", True))])
    assert fake.remaining_script == 0
    assert answer.found is True and answer.lang == "fr" and answer.lang_fallback is True


async def test_an_unsupported_detection_falls_back_to_a_french_refusal(index: Index) -> None:
    answer, _trace, fake = await _run(index, [_comprendre("meteo", language="es")])
    assert fake.remaining_script == 0
    assert answer.lang == "fr" and answer.lang_fallback is True


@pytest.mark.parametrize("lang", ["es", "eng", "XX"])
async def test_an_unsupported_forced_language_is_rejected_before_any_call(index: Index,
                                                                          lang: str) -> None:
    with pytest.raises(InvalidRequest, match="français.*anglais.*allemand.*portugais"):
        await _run(index, [], lang=lang)


# --- quand la relance n'a pas lieu (AD-3 lu, NFR4 tenu) ----------------------
async def test_a_standing_answer_never_pays_a_second_reason_call_for_a_relevance_rejection(
        index: Index) -> None:
    """AD-3 motive la relance par des défauts de **citation** ; une claim écartée par le seul jugement
    de pertinence est déjà « conservée dans rejected_claims[] »."""
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, BONNE_2),
                                             _verdicts(("c1", True), ("c2", False))])
    assert fake.remaining_script == 0  # aucun second appel `reason` : le script vide l'atteste
    assert sum(1 for s in trace.steps if s.name == "rediger") == 1 and trace.retries == 0
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert [c.claim_id for c in answer.rejected_claims] == ["c2"]
    assert answer.texte == "Segment c1."


async def test_a_relevance_rejection_that_leaves_nothing_does_trigger_the_retry(index: Index) -> None:
    """Rien n'a survécu : la relance est le seul chemin vers une réponse (AD-3, « après cette relance »)."""
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE), _verdicts(("c1", False)),
                                             _rediger(BONNE_2), _verdicts(("c2", True))])
    assert fake.remaining_script == 0 and trace.retries == 1
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c2"]


async def test_the_relevance_retry_can_be_switched_on_by_configuration(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, BONNE_2),
                                             _verdicts(("c1", True), ("c2", False)),
                                             _rediger(BONNE, AUTRE_ECOLE),
                                             _verdicts(("c1", True), ("c2", True))],
                                     settings=_settings(relance_sur_non_pertinence=True))
    assert fake.remaining_script == 0 and trace.retries == 1
    assert [c.claim_id for c in answer.claims] == ["c1", "c2"]


async def test_an_unaffordable_retry_serves_the_verified_answer_instead_of_a_503(index: Index) -> None:
    """NFR4 : la relance est une tentative d'amélioration. Refusée faute de budget, elle ne doit pas
    emporter une réponse déjà vérifiée — et la trace le dit."""
    budget = RequestBudget(deadline_s=30.0, max_attempts=3, max_cost_eur=0.20)  # 3 appels, pas 4
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True))], budget=budget)
    assert fake.remaining_script == 0 and trace.retries == 0
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert [c.claim_id for c in answer.rejected_claims] == ["c3"]
    verifier = next(s for s in trace.steps if s.name == "verifier")
    (check,) = [c for c in verifier.checks if c.name == "relance_abandonnee"]
    assert check.ok is False and "budget_exceeded" in check.detail


async def test_a_retry_that_could_not_be_verified_never_starts_at_all(index: Index) -> None:
    """AD-1, « aucun retry ne démarre sans marge », appliqué au **compteur d'appels** : une relance,
    c'est deux appels — rédiger, puis vérifier ce qu'elle a rendu — et AD-3 interdit de montrer un
    draft relancé mais non vérifié. Avec la place pour un seul, démarrer *rédiger* serait payer un
    appel `reason` dont rien ne pourrait sortir (NFR4). Mesuré en live (revue Codex 1.5, tour 3) :
    le plafond par défaut coupait pile entre les deux, et la question ressortait en 503."""
    budget = RequestBudget(deadline_s=30.0, max_attempts=4, max_cost_eur=0.20)  # 3 + 1 : pas les deux
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True))], budget=budget)
    assert fake.remaining_script == 0 and trace.retries == 0  # aucun appel de relance n'a été payé
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert answer.complete is False  # AD-4 : une relance empêchée est une troncature
    # Story 2.3 : et la troncature **se dit**. `complete=False` seul redonnerait un « PARTIEL » nu,
    # que le domaine refuse désormais (`complete ⟺ found ∧ unknown = []`).
    assert PHRASES_DE_LACUNE["fr"]["relance_abandonnee"] in answer.unknown
    verifier = next(s for s in trace.steps if s.name == "verifier")
    (check,) = [c for c in verifier.checks if c.name == "relance_abandonnee"]
    assert "budget_exceeded" in check.detail and "2 requis" in check.detail


async def test_a_second_verification_that_never_starts_keeps_the_verified_answer(index: Index) -> None:
    """La ligne de partage d'AD-16 se lit sur l'appel qui **rate**, pas sur la relance entière : le
    plafond de coût atteint *après* une relance rédigée n'est pas un appel raté, c'est un appel qui
    n'a jamais démarré. Le compteur de référence est donc ré-armé après chaque appel réussi (revue
    Codex 1.5, tour 3) — sans quoi la réponse déjà vérifiée partait en 503."""
    budget = _ferme_le_budget_apres(RequestBudget(deadline_s=30.0, max_attempts=6, max_cost_eur=10.0), 4)
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True)), _rediger(BONNE, BONNE_2)],
                                     budget=budget)
    assert fake.remaining_script == 0  # la relance a bien été rédigée, la seconde vérification non
    assert trace.retries == 1 and budget.attempts == 4
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert answer.complete is False
    verifier = next(s for s in trace.steps if s.name == "verifier")
    (check,) = [c for c in verifier.checks if c.name == "relance_abandonnee"]
    assert "budget_exceeded" in check.detail


def _ferme_le_budget_apres(budget: RequestBudget, appels: int) -> RequestBudget:
    """Referme le plafond de coût dès que `appels` appels ont été envoyés (le client vérifie le coût
    **avant** d'envoyer : c'est exactement le « jamais démarré » d'AD-16)."""
    original = type(budget).note_call

    def note_call(self, *a, **kw):
        out = original(self, *a, **kw)
        if self.attempts >= appels:
            self.max_cost_eur = 0.0
        return out

    budget.note_call = note_call.__get__(budget, type(budget))
    return budget


async def test_a_retry_without_margin_never_starts_and_keeps_the_answer(index: Index) -> None:
    """AD-1 : « aucun retry ne démarre sans marge » — le retry ne démarre pas, la requête garde sa réponse."""
    budget = RequestBudget(deadline_s=3.0, max_attempts=6, max_cost_eur=0.20)  # < llm_retry_margin_s
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE),
                                             _verdicts(("c1", True))], budget=budget)
    assert fake.remaining_script == 0 and answer.found is True
    verifier = next(s for s in trace.steps if s.name == "verifier")
    assert [c.name for c in verifier.checks if not c.ok] == ["citations", "relance_abandonnee"]
    assert "timeout" in verifier.checks[-1].detail


async def test_an_unaffordable_retry_with_nothing_verified_is_a_refusal_not_an_error(index: Index) -> None:
    budget = RequestBudget(deadline_s=30.0, max_attempts=2, max_cost_eur=0.20)  # comprendre + rediger
    answer, trace, fake = await _run(index, [_comprendre(), _rediger(MAUVAISE)], budget=budget)
    assert fake.remaining_script == 0
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "claims_rejetes"  # un `Answer` complet (200), jamais une 503
    verifier = next(s for s in trace.steps if s.name == "verifier")
    assert "aucune affirmation n'avait survécu" in verifier.checks[-1].detail


# --- correctifs de revue 1.5 -------------------------------------------------
@pytest.mark.parametrize("intent", ["meteo", "bavardage", "hors_perimetre"])
async def test_every_out_of_scope_intent_short_circuits(index: Index, intent: str) -> None:
    """AD-5 : « l'étage `reason` n'est jamais atteint pour un refus » — pour les trois intents."""
    answer, trace, fake = await _run(index, [_comprendre(intent)])
    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert trace.intent == intent  # AD-10 : le log dit de quoi il s'agissait, même pour un refus
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "hors_perimetre"


async def test_a_retry_call_that_fails_is_terminal_and_carries_its_partial_trace(index: Index) -> None:
    """AD-16, littéralement : « un appel LLM en timeout, un parse invalide après 1 retry, un 429/529
    fournisseur ⇒ **503 avec trace partielle** ». Un appel **commencé** — donc facturé — qui échoue
    n'est pas une relance qui n'a pas démarré : l'avaler rendrait 200 sur une panne du fournisseur
    (revue Codex 1.5, B5)."""
    casse = fake_message(model=TIERS["reason"], text="{ pas du json")
    with pytest.raises(LlmParse) as exc:
        await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)),
                           casse, casse])  # les deux tentatives du client (appel + relance motivée)
    trace = exc.value.trace
    assert trace is not None and trace.request_id == "req-test"
    # Trace **partielle** d'un échec survenu après *comprendre* : l'intention comprise y est restée.
    assert trace.intent == "question"
    # la `StepTrace` de l'appel raté est dans la trace : son coût y compte, il ne disparaît pas
    rediger_steps = [s for s in trace.steps if s.name == "rediger"]
    assert len(rediger_steps) == 2 and len(rediger_steps[1].calls) == 2
    assert trace.total_cost_eur > 0


async def test_a_provider_failure_on_the_retry_is_terminal_too(index: Index) -> None:
    answer_script = [_comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)),
                     anthropic.APIStatusError("529", response=httpx.Response(
                         529, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)]
    with pytest.raises(LlmUnavailable) as exc:
        await _run(index, answer_script)
    assert exc.value.trace is not None and [s.name for s in exc.value.trace.steps][-1] == "rediger"


@pytest.mark.parametrize("variant", ["deterministe", "outils"])
async def test_a_retry_whose_call_never_started_keeps_the_verified_answer(
        index: Index, variant: str) -> None:
    """La contrepartie : plafond d'appels atteint, rien n'a été facturé, la réponse acquise reste due
    (AD-1 « aucun retry ne démarre sans marge », étendu aux euros par AD-4)."""
    budget = RequestBudget(deadline_s=30.0, max_attempts=3 + 2 * (variant == "outils"),
                           max_cost_eur=0.20)
    script = _avec_navigation_outils(
        [_comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True))], variant)
    answer, trace, fake = await _run(index, script, budget=budget,
                                     variant=variant)
    assert fake.remaining_script == 0 and answer.found is True
    rediger_steps = [s for s in trace.steps if s.name == "rediger"]
    assert len(rediger_steps) == 1  # l'étape avortée n'a rien appelé : elle n'entre pas dans la trace
    verifier = next(s for s in trace.steps if s.name == "verifier")
    assert [c.name for c in verifier.checks if c.name == "relance_abandonnee"]


@pytest.mark.parametrize("variant", ["deterministe", "outils"])
async def test_a_retry_that_verifies_worse_never_replaces_the_answer(
        index: Index, variant: str) -> None:
    """AD-3 relance pour améliorer : une seconde ébauche moins bonne ne jette pas l'acquis."""
    script = _avec_navigation_outils(
        [_comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)), _rediger(MAUVAISE)],
        variant)
    answer, trace, fake = await _run(index, script,
                                     variant=variant)
    assert fake.remaining_script == (1 if variant == "outils" else 0)
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    verifier_2 = [s for s in trace.steps if s.name == "verifier"][-1]
    checks = [c for c in verifier_2.checks if c.name == "relance_moins_bonne"]
    if variant == "deterministe":
        (check,) = checks
        assert "0 affirmation(s) contre 1" in check.detail
    else:
        assert checks == []  # la suffisance Sonnet n'a pas demandé de relance inutile


@pytest.mark.parametrize("variant", ["deterministe", "outils"])
async def test_a_max_tokens_draft_is_retried_under_the_variant_output_cap(
        index: Index, variant: str) -> None:
    """M4 : la troncature de *rédiger* est éprouvée sous l'autorité unique des variantes."""
    tronquee = _rediger(BONNE)
    tronquee["stop_reason"] = "max_tokens"
    settings = _settings(rediger_max_tokens=19)
    expected = 19
    script = _avec_navigation_outils([_comprendre(), tronquee, tronquee], variant)
    with pytest.raises(LlmParse, match=rf"tronquée.*max_tokens={expected}"):
        await _run(index, script, variant=variant, settings=settings)


async def test_an_abandoned_retry_forbids_declaring_the_answer_complete(index: Index) -> None:
    """AD-4 : `complete=True` exige « aucune troncature de budget » — une relance refusée en est une."""
    settings = _settings(retrieval_max_blocks=2)  # pas de troncature « naturelle » ici
    budget = RequestBudget(deadline_s=30.0, max_attempts=3, max_cost_eur=0.20)
    answer, _trace, _fake = await _run(index, [_comprendre(terms=["arrivée"]),
                                               _rediger(BONNE, MAUVAISE), _verdicts(("c1", True))],
                                       settings=settings, budget=budget)
    assert answer.found is True and answer.complete is False


async def test_an_unserved_document_is_refused_before_any_paid_call(index: Index) -> None:
    with pytest.raises(CorpusUnavailable, match="non servi"):
        await _run(index, [], settings=_settings(guide_doc_id="mini-inexistant"))


async def test_without_a_budget_the_pipeline_asks_the_client_for_one(index: Index) -> None:
    """Chemin de la story 1.6 : l'API appelle `repondre_guide` sans budget."""
    settings = _settings()
    fake = FakeAnthropic([_comprendre(), _rediger(BONNE), _verdicts(("c1", True))])
    client = LlmClient(settings, anthropic_client=fake)
    answer, trace = await repondre_guide("Quel délai ?", [], Profil(), corpus=index.corpus, index=index,
                                         client=client, settings=settings, request_id="sans-budget",
                                         variant="deterministe")
    assert answer.found is True and trace.deadline_remaining_s is not None
    # le budget vient des seuils actifs : la deadline restante ne dépasse jamais `deadline_s`
    assert 0 < trace.deadline_remaining_s <= settings.deadline_s
    budget = client.new_budget()
    assert (budget.deadline_s, budget.max_attempts, budget.max_cost_eur) == (
        settings.deadline_s, settings.max_llm_attempts, settings.max_cost_eur_per_request)


async def test_the_trace_digests_are_the_real_ones_and_can_be_supplied(index: Index) -> None:
    _answer, trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE), _verdicts(("c1", True))])
    assert trace.pipeline_digest == pipeline_digest() != prompts_digest()
    assert trace.prompts_digest == prompts_digest()

    settings = _settings()
    fake = FakeAnthropic([_comprendre(), _rediger(BONNE), _verdicts(("c1", True))])
    _a, fournis = await repondre_guide(
        "Quel délai ?", [], Profil(), corpus=index.corpus, index=index,
        client=LlmClient(settings, anthropic_client=fake), settings=settings, request_id="digests",
        budget=_budget(), pipeline_digest_hex="pipe-de-l-image", prompts_digest_hex="prompts-de-l-image",
        variant="deterministe")
    # story 1.6 : l'API les calcule au démarrage et les passe ; ils sont repris tels quels
    assert (fournis.pipeline_digest, fournis.prompts_digest) == ("pipe-de-l-image", "prompts-de-l-image")


async def test_trace_retries_counts_the_client_parse_retries_too(index: Index) -> None:
    """AD-10 ne définit pas `retries` : le pipeline le fait, et le test l'épingle."""
    casse = fake_message(model=TIERS["reason"], text="{ pas du json")
    _answer, trace, fake = await _run(index, [_comprendre(), casse, _rediger(BONNE),
                                              _verdicts(("c1", True))])
    assert fake.remaining_script == 0
    rediger_step = next(s for s in trace.steps if s.name == "rediger")
    assert [c.name for c in rediger_step.checks] == ["parse_retry"]
    assert trace.retries == 1  # la relance motivée du client, sans relance d'AD-3


async def test_a_retrieval_emptied_by_the_budget_never_becomes_a_proof_of_absence(index: Index) -> None:
    """AD-1 / NFR2 : « budget épuisé ou troncature non résolue ⇒ `complete=False` et **aucune absence
    du corpus n'est affirmée** ». Un `zero_hit` produit par notre propre borne serait une preuve
    d'absence fabriquée (revue Codex 1.5, B4)."""
    settings = _settings(retrieval_max_tokens=1)  # aucune unité n'entre dans le budget
    with pytest.raises(BudgetExceeded, match="aucun bloc"):
        await _run(index, [_comprendre()], settings=settings)
    # la trace partielle voyage avec l'erreur (AD-16), et *retrouver* y figure avec sa troncature
    try:
        await _run(index, [_comprendre()], settings=settings)
    except BudgetExceeded as exc:
        assert exc.trace is not None and [s.name for s in exc.trace.steps] == ["comprendre", "retrouver"]
        assert exc.trace.truncations == 1
    # sans troncature, un retrieval vide reste un refus `zero_hit` motivé : rien n'a empêché la recherche
    answer, _trace, _fake = await _run(index, [_comprendre(terms=["hippopotame"])])
    assert answer.reason is not None and answer.reason.kind == "zero_hit"


async def test_a_retry_that_ties_on_claims_but_loses_completeness_never_replaces_the_answer(
        index: Index) -> None:
    """La dominance porte sur tous les axes, pas sur le seul décompte : à nombre égal, une relance qui
    perd `complete` ou allonge `unknown` dégrade encore la réponse (revue Codex 1.5, I2)."""
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)),
        _rediger(BONNE, limite="Je ne sais rien des frontaliers."), _verdicts(("c1", True))])
    assert fake.remaining_script == 0
    # même nombre d'affirmations, mais la seconde ébauche déclare un inconnu de plus et n'est plus complète
    assert answer.found is True and [c.claim_id for c in answer.claims] == ["c1"]
    assert answer.unknown == [] and answer.complete is True  # l'acquis, pas la relance
    verifier_2 = [s for s in trace.steps if s.name == "verifier"][-1]
    (check,) = [c for c in verifier_2.checks if c.name == "relance_moins_bonne"]
    assert "ne domine pas" in check.detail


def test_domine_compares_the_number_of_typed_lacunes_directly() -> None:
    acquise = Verification(found=True, complete=False,
                           lacunes=[Lacune(kind="lecture_bornee")])
    sans_lacune = Verification(found=True, complete=False)
    assert domine(sans_lacune, acquise) is True
    assert domine(acquise, sans_lacune) is False


class _BudgetQuiExpire(RequestBudget):
    """Budget dont la deadline s'épuise juste après le n-ième appel facturé (horloge factice).

    Compter les appels plutôt que les secondes rend le test déterministe : l'instant d'expiration est
    exactement l'entre-deux-étapes que l'on veut éprouver, sans dépendre de l'horloge de la machine.
    """

    def __init__(self, apres_appels: int) -> None:
        super().__init__(deadline_s=30.0, max_attempts=6, max_cost_eur=0.10)
        self._restants = apres_appels

    def note_call(self, usage) -> None:
        super().note_call(usage)
        self._restants -= 1

    def remaining(self) -> float:
        return 30.0 if self._restants > 0 else -1.0


@pytest.mark.parametrize("appels, etape", [(1, "retrouver"), (2, "verifier"), (3, "restituer")])
async def test_the_deadline_is_checked_before_every_step_including_restituer(index: Index, appels: int,
                                                                             etape: str) -> None:
    """Contrat du pipeline : « deadline vérifiée **avant chaque étape** ». *restituer* en est une —
    elle ne l'était pas, et une requête dont le temps venait d'expirer rendait une réponse normale
    (revue Codex 1.5, I3). AD-16 : « la deadline globale (AD-9) produit `timeout` »."""
    with pytest.raises(Timeout, match=etape):
        await _run(index, [_comprendre(), _rediger(BONNE), _verdicts(("c1", True))],
                   budget=_BudgetQuiExpire(appels))


async def test_the_deadline_is_checked_before_a_refusal_too(index: Index) -> None:
    """Les chemins courts appellent *restituer* comme les autres : `refuser()` contrôle aussi."""
    with pytest.raises(Timeout, match="restituer"):
        await _run(index, [_comprendre("meteo")], budget=_BudgetQuiExpire(1))


async def test_a_provider_failure_during_the_second_verification_is_terminal_too(index: Index) -> None:
    """La ligne de partage est « un appel a-t-il démarré ? », et elle se tranche sur le **budget**.

    *vérifier* ne renseignait pas `PipelineError.step` : le pipeline concluait « relance non démarrée »
    et servait la première réponse en 200 sur une panne du fournisseur survenue pendant la **seconde
    vérification** (revue Codex 1.5, tour 2, B5). AD-16 : « un 429/529 fournisseur ⇒ 503 avec trace
    partielle »."""
    panne = anthropic.APIStatusError("529", response=httpx.Response(
        529, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
    with pytest.raises(LlmUnavailable) as exc:
        await _run(index, [_comprendre(), _rediger(BONNE, MAUVAISE), _verdicts(("c1", True)),
                           _rediger(BONNE, BONNE_2), panne])
    trace = exc.value.trace
    assert trace is not None and [s.name for s in trace.steps][-1] == "verifier"
    # le `StepTrace` de l'appel raté voyage avec l'erreur : son coût ne disparaît pas de la trace
    assert len([s for s in trace.steps if s.name == "verifier"]) == 2
    assert trace.total_cost_eur > 0


DEUX_FACETTES = ["délai de déclaration", "inscription à l'école"]


async def test_a_limit_written_by_the_model_never_reaches_the_answer_text(index: Index) -> None:
    """De bout en bout : « le guide ne dit rien de X » est une assertion d'absence, qu'aucun passage
    ne peut soutenir (revue Codex 1.5, tour 3, B1). Elle ne s'affiche pas dans la réponse ; elle est
    dite par `unknown[]`, qui interdit déjà `complete=True`. La seule phrase d'absence que
    l'utilisateur lise reste celle du refus, composée par le code."""
    limite = "Le guide ne dit rien des frontaliers."
    answer, _trace, fake = await _run(index, [
        _comprendre(), _rediger(BONNE, transition=True, limite=limite), _verdicts(("c1", True))])
    assert fake.remaining_script == 0
    assert answer.found is True
    assert "frontaliers" not in answer.texte
    assert [s.kind for s in answer.segments] == ["factuel", "transition"]
    assert answer.unknown == [limite] and answer.complete is False


async def test_a_facet_of_the_question_the_answer_never_covers_forbids_complete(index: Index) -> None:
    """AD-4, littéralement : « toutes les facettes de `ParsedQuestion` couvertes ». Le découpage est
    celui qu'a arrêté *comprendre*, avant tout retrieval — celui qui répond ne peut donc plus effacer
    la sous-question qu'il a manquée en ne la rendant pas (revue Codex 1.5, tour 3, B3)."""
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=DEUX_FACETTES), _rediger(BONNE),
        _verdicts(("c1", True), facettes=[["c1"], []])])
    assert fake.remaining_script == 0
    # Story 2.3 : la sous-question omise est **nommée** dans `unknown[]` — le front l'affiche sous
    # « Ce que je ne sais pas », et l'état « partiel » cesse d'être un mot nu. La phrase est composée
    # par le code : ni le libellé de la facette ni un `block_id` n'y entrent (AD-10, AD-16).
    assert answer.found is True and answer.complete is False
    assert answer.unknown == ["Il reste 1 sous-question sans réponse dans ce que vous m'avez demandé."]
    verifier = [s for s in trace.steps if s.name == "verifier"][-1]
    (check,) = [c for c in verifier.checks if c.name == "facettes_non_couvertes"]
    assert "1 facette(s) couverte(s) par une affirmation affichée sur 2 posée(s)" in check.detail
    # et le libellé de la facette, qui vient de la question, ne fuit pas dans la trace (AD-10)
    assert "école" not in json.dumps([s.model_dump(mode="json") for s in trace.steps], ensure_ascii=False)


async def test_a_question_without_any_facet_is_never_declared_complete(index: Index) -> None:
    """*comprendre* muet sur le découpage : rien ne prouve la couverture, donc `complete=False`.
    L'absence de mesure ne vaut jamais complétude (AD-4)."""
    answer, _trace, fake = await _run(index, [
        _comprendre(facettes=[]), _rediger(BONNE), _verdicts(("c1", True), facettes=[])])
    assert fake.remaining_script == 0
    assert answer.found is True and answer.complete is False


async def test_a_retry_that_covers_fewer_facets_never_replaces_the_answer(index: Index) -> None:
    """Dernier axe de la dominance : à nombre d'affirmations, `complete` et `unknown` égaux, une
    relance peut encore échanger une facette de la question contre une autre (revue Codex 1.5,
    tour 2, I2). Les facettes sont celles de la question, arrêtées une seule fois par *comprendre* :
    leurs rangs sont donc stables d'une ébauche à l'autre, là où les `claim_id` sont refaits à neuf
    par chaque appel de *rédiger*."""
    limite = "Je ne sais rien des frontaliers."
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=DEUX_FACETTES), _rediger(BONNE, BONNE_2, MAUVAISE, limite=limite),
        _verdicts(("c1", True), ("c2", True), facettes=[["c1"], ["c2"]]),
        _rediger(BONNE, BONNE_2, limite=limite),
        _verdicts(("c1", True), ("c2", True), facettes=[["c1"], []])])
    assert fake.remaining_script == 0
    assert answer.found is True and len(answer.claims) == 2 and answer.complete is False
    verifier_2 = [s for s in trace.steps if s.name == "verifier"][-1]
    (check,) = [c for c in verifier_2.checks if c.name == "relance_moins_bonne"]
    assert "1 facette(s) couverte(s) contre 2" in check.detail


async def test_a_retry_that_swaps_one_facet_for_another_never_replaces_the_answer(index: Index) -> None:
    """Compter les facettes ne suffisait pas : deux vérifications qui en couvrent chacune **une**,
    mais pas la même, ont le même compte, et la seconde était acceptée — elle échangeait une
    sous-question contre une autre (revue Codex 1.5, tour 3, I2). La dominance porte donc sur
    l'**ensemble** des rangs couverts, stables parce que le découpage vient de *comprendre*."""
    answer, trace, fake = await _run(index, [
        _comprendre(facettes=DEUX_FACETTES), _rediger(BONNE, MAUVAISE),
        _verdicts(("c1", True), facettes=[["c1"], []]),
        _rediger(BONNE_2),
        _verdicts(("c2", True), facettes=[[], ["c2"]])])
    assert fake.remaining_script == 0
    # la première réponse est servie : la seconde couvre autant de facettes, mais pas les mêmes
    assert [c.claim_id for c in answer.claims] == ["c1"]
    verifier_2 = [s for s in trace.steps if s.name == "verifier"][-1]
    assert [c.name for c in verifier_2.checks if c.name == "relance_moins_bonne"]


async def test_a_retry_that_drops_a_cited_block_never_replaces_the_answer(index: Index) -> None:
    """Deuxième identité stable entre deux ébauches (revue Codex 1.5, tour 3, I2) : le `block_id`,
    produit par **notre** ingestion. Une relance qui répond à la même facette, en même nombre, mais
    en s'appuyant sur un autre passage, remplacerait une affirmation vérifiée par une autre sans que
    rien ne le dise. Les `claim_id` ne peuvent pas l'attraper (refaits à neuf) ; les blocs, si."""
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(BONNE, MAUVAISE),
        _verdicts(("c1", True)),
        _rediger(BONNE_2),          # même facette, même compte, **autre** bloc cité
        _verdicts(("c2", True))])
    assert fake.remaining_script == 0
    assert [c.claim_id for c in answer.claims] == ["c1"]
    assert {q.block_id for c in answer.claims for q in c.quotes} == {f"{DOC_ID}:f1:2"}
    verifier_2 = [s for s in trace.steps if s.name == "verifier"][-1]
    (check,) = [c for c in verifier_2.checks if c.name == "relance_moins_bonne"]
    assert "1 bloc(s) cité(s) contre 1" in check.detail


# --- story 2.1 : le court-circuit « zéro hit » d'AD-5 ------------------------
# `FakeAnthropic` lève sur tout appel non scripté : un script d'un seul `_comprendre()` **est**
# l'assertion « aucun appel `reason` n'a eu lieu » (AD-5 : l'étage `reason` n'est jamais atteint
# pour un refus).

HIPPO = {"matricule": ["numéro national"]}


async def test_un_hit_partiel_dune_forme_composee_atteint_retrouver_et_la_chaine_fixe(
        index: Index, tmp_path: Path) -> None:
    """Story 2.7 / R2 : un seul mot présent interdit le pré-refus `zero_hit`."""
    dico = _dictionnaire(tmp_path, index, {"choix commune": ["quelle commune"]},
                         validated=True)

    answer, trace, fake = await _run(
        index,
        [_comprendre(terms=["choix commune"]), _rediger(BONNE), _verdicts(("c1", True))],
        dictionnaire=dico,
    )

    assert fake.remaining_script == 0
    assert [step.name for step in trace.steps] == [
        "comprendre", "retrouver", "rediger", "verifier", "restituer"]
    assert answer.found is True and answer.reason is None


async def test_aucun_mot_daucune_forme_composee_refuse_avant_reason_avec_le_canonique(
        index: Index, tmp_path: Path) -> None:
    """Story 2.7 / R5 : l'absence réelle garde une preuve canonique et zéro appel `reason`."""
    dico = _dictionnaire(tmp_path, index, {"océan profond": ["mer immense"]}, validated=True)

    answer, trace, fake = await _run(
        index, [_comprendre(terms=["mer immense"])], dictionnaire=dico)

    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [step.name for step in trace.steps] == ["comprendre", "restituer"]
    assert answer.reason is not None and answer.reason.kind == "zero_hit"
    assert answer.reason.terms_searched == ["océan profond"]
    assert "mer immense" not in answer.reason.model_dump_json()


async def test_zero_hit_dictionnaire_valide_refuse_avant_retrouver(index: Index, tmp_path: Path) -> None:
    """AC 2.1, mot pour mot : `found=False`, `reason.kind == "zero_hit"`, `variants_count > 0`,
    aucun appel au tier `reason`, et `Trace.steps` **ne contient pas** l'étape `retrouver`."""
    dico = _dictionnaire(tmp_path, index, HIPPO, validated=True)
    assert dico.court_circuit_actif is True

    answer, trace, fake = await _run(index, [_comprendre(terms=["hippopotame", "matricule"])],
                                     dictionnaire=dico)

    assert fake.remaining_script == 0 and len(fake.requests) == 1  # le seul appel est `reason`
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert "retrouver" not in [s.name for s in trace.steps]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "zero_hit"
    assert answer.reason.variants_count == 1
    # AD-4 : `terms_searched[] (canoniques)`. Ici les deux termes **sont** leurs propres canoniques
    # (« matricule » est une clé du dictionnaire, « hippopotame » lui est inconnu) : la preuve les
    # rend inchangés. Le cas où l'un est une variante est exercé juste en dessous.
    assert answer.reason.terms_searched == ["hippopotame", "matricule"]
    assert answer.reason.blocks_scanned == 4 and answer.reason.documents == [DOC_ID]


async def test_la_preuve_dabsence_nomme_le_canonique_pas_la_variante(index: Index,
                                                                     tmp_path: Path) -> None:
    """AD-4, mot pour mot : `AbsenceProof(…, terms_searched[] (canoniques), …)`.

    Revue Codex 2.1 (B5) : `terms_searched` recopiait les termes de *comprendre*, si bien qu'un terme
    reconnu comme **variante** était publié tel quel — reproduit sur l'artefact livré avec
    « Arbeitsamt », du groupe canonique « ADEM ». La preuve dit désormais les notions cherchées ;
    aucune variante n'y paraît, et un terme inconnu du dictionnaire reste lui-même.
    """
    dico = _dictionnaire(tmp_path, index, {"matricule": ["Sozialversicherungsnummer"]},
                         validated=True)

    answer, _trace, fake = await _run(
        index, [_comprendre(terms=["Sozialversicherungsnummer", "hippopotame"])], dictionnaire=dico)

    assert fake.remaining_script == 0 and answer.reason is not None
    assert answer.reason.kind == "zero_hit"
    assert answer.reason.terms_searched == ["matricule", "hippopotame"]
    assert "Sozialversicherungsnummer" not in answer.model_dump_json()


async def test_zero_hit_dictionnaire_non_valide_passe_par_retrouver(index: Index, tmp_path: Path) -> None:
    """AC 2.1 : « le même dictionnaire avec `validated: false` ⇒ `Trace.steps` contient `retrouver` ».

    Le refus vient alors du garde-fou « zéro bloc » de 1.5, avec la **même** preuve : c'est le même
    fait pour l'utilisateur, et ce qui distingue les deux court-circuits est observable dans la trace.
    """
    dico = _dictionnaire(tmp_path, index, HIPPO, validated=False)
    assert dico.utilisable is True and dico.court_circuit_actif is False

    answer, trace, fake = await _run(index, [_comprendre(terms=["hippopotame", "matricule"])],
                                     dictionnaire=dico)

    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "restituer"]
    assert answer.reason is not None and answer.reason.kind == "zero_hit"
    assert answer.reason.variants_count == 1  # les variantes **ont** servi, elles n'ont rien trouvé


async def test_le_refus_par_intent_reste_actif_dictionnaire_ou_pas(index: Index, tmp_path: Path) -> None:
    """AD-5 : « le refus par intent reste actif dans tous les cas ». C'est le seul des deux
    court-circuits qui ne dépende que de l'`intent`."""
    for dico in (None, _dictionnaire(tmp_path, index, HIPPO, validated=False),
                 _dictionnaire(tmp_path, index, HIPPO, validated=True)):
        answer, trace, fake = await _run(index, [_comprendre("meteo")], dictionnaire=dico)
        assert fake.remaining_script == 0 and [s.name for s in trace.steps] == ["comprendre", "restituer"]
        assert answer.reason is not None and answer.reason.kind == "hors_perimetre"


async def test_un_dictionnaire_dun_autre_corpus_ne_court_circuite_pas(index: Index, tmp_path: Path) -> None:
    """AD-5 : « `validated=false` **ou** `corpus_source_hashes` ≠ corpus chargé ⇒ court-circuit
    désactivé, la requête poursuit vers *retrouver* ». Signé, mais sur un autre corpus."""
    perime = _dictionnaire(tmp_path, index, HIPPO, validated=True, source_hash="empreinte-dun-autre-corpus")
    assert perime.validated is True and perime.court_circuit_actif is False

    answer, trace, _fake = await _run(index, [_comprendre(terms=["hippopotame"])],
                                      dictionnaire=perime)
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "restituer"]
    # Aucune variante n'a été essayée : l'annoncer autrement serait faux (AD-4).
    assert answer.reason is not None and answer.reason.variants_count == 0


async def test_un_hit_par_variante_ne_court_circuite_pas_et_repond(index: Index, tmp_path: Path) -> None:
    """Le court-circuit porte sur « aucun terme **ni aucune variante** n'a de hit » (AD-5) : une
    question qui ne touche le guide que par une variante doit être **répondue**, pas refusée."""
    dico = _dictionnaire(tmp_path, index, {"arrivée": ["Anmeldung"]}, validated=True)

    answer, trace, fake = await _run(
        index, [_comprendre(terms=["Anmeldung"]), _rediger(BONNE), _verdicts(("c1", True))],
        dictionnaire=dico)

    assert fake.remaining_script == 0
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "restituer"]
    assert answer.found is True
    check = next(c for c in trace.steps[1].checks if c.name == "dictionnaire")
    assert check.detail == "1 variante(s) ajoutée(s) à 1 terme(s)"


async def test_sans_terme_extrait_rien_nest_court_circuite(index: Index, tmp_path: Path) -> None:
    """AD-1 : « aucune absence du corpus n'est affirmée » sans recherche. Zéro terme, c'est zéro
    recherche : le court-circuit d'AD-5 ne peut pas conclure, et le garde-fou de 1.5 tranche après."""
    dico = _dictionnaire(tmp_path, index, HIPPO, validated=True)
    answer, trace, _fake = await _run(index, [_comprendre(terms=[])], dictionnaire=dico)
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "restituer"]
    assert answer.reason is not None and answer.reason.terms_searched == []


async def test_labsence_serialisee_ne_porte_jamais_une_variante_ni_un_declencheur(index: Index, tmp_path: Path) -> None:
    """AD-4 : `AbsenceProof` ne porte **jamais** la liste des variantes ni des déclencheurs.

    Le contrôle est fait sur l'objet **sérialisé** : c'est ce qui part au front, et c'est là que la
    fuite se produirait — terme par terme, dans une réponse publique.

    Le terme cherché est **inconnu** du dictionnaire : aucun groupe n'est touché, donc aucun
    canonique ne sort non plus. C'est la portée exacte de la règle depuis la revue Codex 2.1 (B5) —
    `terms_searched` nomme le canonique d'un groupe que la question a fait chercher, et rien d'autre.
    """
    dico = _dictionnaire(tmp_path, index, {"matricule": ["numéro national", "Sozialversicherungsnummer"]},
                         validated=True)
    answer, _trace, _fake = await _run(index, [_comprendre(terms=["hippopotame"])], dictionnaire=dico)
    assert answer.reason is not None
    serialise = answer.reason.model_dump_json()
    for variante in ("numéro national", "Sozialversicherungsnummer", "matricule"):
        assert variante not in serialise
    assert "hippopotame" in serialise  # ce que *comprendre* a produit, lui, y est


async def test_le_variants_count_dun_refus_de_claims_est_reel_aussi(index: Index, tmp_path: Path) -> None:
    """`variants_count` décrit la recherche, pas le kind du refus : un `claims_rejetes` l'annonce
    comme un `zero_hit`, sinon deux refus de la même requête donneraient deux comptes différents."""
    dico = _dictionnaire(tmp_path, index, {"arrivée": ["Anmeldung"]}, validated=True)
    answer, _trace, fake = await _run(
        index, [_comprendre(terms=["arrivée"]), _rediger(MAUVAISE), _rediger(MAUVAISE)],
        dictionnaire=dico)
    assert fake.remaining_script == 0
    assert answer.reason is not None and answer.reason.kind == "claims_rejetes"
    assert answer.reason.variants_count == 1


async def test_le_perimetre_du_corpus_part_a_comprendre(index: Index) -> None:
    """Reprise différée `target_story: 2.1` : la liste de périmètre vient du **corpus** servi."""
    index.corpus.perimetres[DOC_ID] = "- Arrivée : Déclarer son arrivée"
    try:
        _answer, _trace, fake = await _run(index, [_comprendre(), _rediger(BONNE),
                                                   _verdicts(("c1", True))])
    finally:  # l'index est partagé par le module : rien ne fuit vers le test suivant
        index.corpus.perimetres.pop(DOC_ID, None)
    assert "- Arrivée : Déclarer son arrivée" in fake.requests[0]["system"][0]["text"]


async def test_une_variante_anglaise_ouvre_la_fiche_francaise(index: Index, tmp_path: Path) -> None:
    """L'AC, **littéralement** : « quelqu'un qui écrit en anglais tombe sur la bonne fiche ».

    Les autres cas de variante l'exerçaient en français (une paraphrase) et en allemand
    (« Anmeldung ») : ni l'un ni l'autre ne montre le chemin que la story promet — un terme
    **anglais** rendu par *comprendre*, absent mot pour mot du guide français, qui ouvre malgré tout
    la fiche du canonique et donne une réponse sourcée (revue coordonnée 2.1).

    *comprendre* rend des termes « toujours en français » (AD-5), mais rien ne garantit qu'ils
    existent dans le guide : c'est précisément le cas que le dictionnaire rattrape, et il se produit
    dès qu'un terme du domaine n'a pas d'équivalent français employé par les fiches.
    """
    dico = _dictionnaire(tmp_path, index, {"arrivée": ["residence registration"]}, validated=True)

    answer, trace, fake = await _run(
        index, [_comprendre(terms=["residence registration"]), _rediger(BONNE),
                _verdicts(("c1", True))], dictionnaire=dico)

    assert fake.remaining_script == 0
    # Le court-circuit n'a pas mordu : la variante a un hit, donc la chaîne complète a tourné.
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "restituer"]
    assert answer.found is True
    # La fiche du canonique **français** est ouverte à partir du seul terme anglais.
    assert f"{DOC_ID}:f1:2" in trace.steps[1].opened_block_ids
    assert [c.claim_id for c in answer.claims] == ["c1"]


async def test_le_pre_controle_ne_refuse_pas_ce_quune_definition_couvre(tmp_path: Path) -> None:
    """Le court-circuit doit être **au moins aussi large** que ce que *retrouver* trouve.

    *retrouver* peuple `retrieval.blocs` avec **deux** outils : `index.chercher()` et
    `index.definitions()`, dont les résultats sont hors des hits du premier. Conclure sur `chercher`
    seul refusait d'avance une question sans hit mais couverte par un bloc `defines` — alors que la
    chaîne complète y répondait. Inatteignable sur le corpus servi aujourd'hui (aucun bloc du guide
    ne porte de `defines`), armé par le typage automatique de la story 3.2 : c'est la règle d'AD-5
    (« aucun terme canonique **ni ses variantes** n'a de hit dans l'index ») appliquée à l'index
    entier, pas un correctif spéculatif (revue coordonnée 2.1).
    """
    # Un corpus où « contenu » n'apparaît dans **aucun** texte de bloc, mais où un bloc le *définit*.
    blocs = [
        {"block_id": "def:f1:1", "loc": "f1", "seq": 1, "kind": "para",
         "text": "Le mobilier de jardin voyage avec le logement assuré."},
        {"block_id": "def:f1:2", "loc": "f1", "seq": 2, "kind": "definition", "defines": "contenu",
         "text": "Ce bloc explique ce que recouvre le mot défini, sans jamais l'employer."},
    ]
    doc = Document(doc_id="def", kind="guide", title="Def", edition="git:test",
                   nodes=[Node(node_id="def:f1", title="Définitions",
                               items=[{"block_id": "def:f1:1"}, {"block_id": "def:f1:2"}])],
                   blocks=blocs)
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    manifest = {"def": ManifestEntry(status="servi", source_hash="sha-source",
                                     ingest_fingerprint="fp-1", document_hash="sha-doc",
                                     edition="git:test")}
    idx = Index(Corpus(documents={"def": doc}, manifest=manifest, summaries={"def": "# Def"}))
    reglages = _settings(guide_doc_id="def")

    dico = _dictionnaire(tmp_path, idx, {"contenu": []}, validated=True, doc_id="def")
    assert dico.court_circuit_actif is True
    # `chercher` seul ne trouve rien : c'est ce qui faisait refuser d'avance.
    assert idx.chercher(["contenu"], limit=1, doc_id="def") == []
    assert idx.definitions(["contenu"], doc_id="def") != []

    answer, trace, fake = await _run(
        idx, [_comprendre(terms=["contenu"]), _rediger(("c1", "Le contenu est défini.",
                                                        [("def:f1:2",
                                                          "explique ce que recouvre le mot défini")])),
              _verdicts(("c1", True))],
        settings=reglages, dictionnaire=dico,
        question="Qu'est-ce que le contenu ?")

    assert fake.remaining_script == 0
    assert "retrouver" in [s.name for s in trace.steps]   # la question n'a pas été refusée d'avance
    assert answer.found is True


async def test_le_pre_controle_verifie_la_deadline_comme_les_etapes(index: Index,
                                                                    tmp_path: Path) -> None:
    """Le pipeline vérifie la deadline **avant chaque étape** ; le pré-contrôle balaye l'index et
    n'en était pas précédé (revue coordonnée 2.1). Deadline épuisée ⇒ `Timeout`, pas un refus."""
    dico = _dictionnaire(tmp_path, index, HIPPO, validated=True)
    budget = _budget(deadline_s=30.0)
    fake = FakeAnthropic([_comprendre(terms=["hippopotame"])])
    client = LlmClient(_settings(), anthropic_client=fake)

    from server.app.pipelines import guide as module_guide

    vrai = module_guide.comprendre

    async def epuiser(*a, **kw):
        resultat = await vrai(*a, **kw)
        budget.deadline_s = 0.0      # la deadline tombe **pendant** *comprendre*
        return resultat

    module_guide.comprendre = epuiser
    try:
        with pytest.raises(Timeout, match="court-circuit zéro hit"):
            await repondre_guide("q", [], Profil(), corpus=index.corpus, index=index, client=client,
                                 settings=_settings(), request_id="r", budget=budget,
                                 dictionnaire=dico)
    finally:
        module_guide.comprendre = vrai
    assert fake.remaining_script == 0  # *comprendre* a bien tourné : c'est bien le pré-contrôle qui coupe


# --- story 2.2 : le suivi, et la question **résolue** comme seule base du refus ---
LOGEMENT = "Quelles démarches pour mon logement en arrivant ?"
REPONSE_LOGEMENT = ("Vous disposez de huit jours après votre arrivée pour vous déclarer au "
                    "Biergercenter de votre commune de résidence.")
VOITURE = "Quelles démarches dois-je faire pour ma voiture en arrivant au Luxembourg ?"


def _texte_envoye(requete: dict) -> str:
    """Tout ce qu'une requête porte de texte : le préfixe système **et** les messages.

    Chercher l'historique dans le seul `messages[0]["content"]` prouverait trop peu : une étape
    pourrait le faire voyager par le préfixe (qui est, lui, censé rester déterministe et cacheable —
    AD-9) sans que l'assertion s'en aperçoive.
    """
    systeme = "".join(bloc.get("text", "") for bloc in requete.get("system") or [])
    messages = "".join(str(m.get("content", "")) for m in requete.get("messages") or [])
    return systeme + "\n" + messages


def _aurait_un_hit(index: Index, dico, termes: list[str]) -> bool:
    """Le prédicat **exact** que `pipelines/guide.py` évalue avant de refuser d'avance (AD-5).

    Deux bras, pas un : `index.chercher(dictionnaire.expand(termes))` **et**
    `index.definitions(termes)`. Une précondition qui n'appellerait que `chercher`, sans expansion,
    ne dirait pas ce que le pré-contrôle aurait décidé sur ces mots-là — or c'est tout l'argument
    des deux tests ci-dessous : montrer que lus sur la question **brute**, ces mots auraient (ou
    n'auraient pas) fait refuser la requête (revue 2.2, P7).
    """
    return bool(index.chercher(dico.expand(termes), limit=1, doc_id=DOC_ID)) \
        or bool(index.definitions(termes, doc_id=DOC_ID))


async def test_un_suivi_resolu_traverse_la_chaine_et_lhistorique_ne_va_qua_deux_etapes(index: Index) -> None:
    """AC 2.2 + AD-1 : « seules *comprendre* et *rédiger* reçoivent l'historique ».

    Le contrat statique de `tests/test_layers.py` interdit qu'une autre étape le *déclare* ; ce
    test-ci dit ce qui s'est réellement passé sur le fil — l'historique est dans les deux requêtes
    qui y ont droit, dans aucune autre, et la question **brute** (« Et pour la voiture ? », non
    autonome) ne quitte jamais *comprendre* : c'est `question_resolue` que *rédiger* et *vérifier*
    reçoivent. Une chaîne qui repasserait la brute plus bas chercherait puis rédigerait sur une
    anaphore, exactement ce qu'AD-5 a fermé en story 1.4.
    """
    historique = [Turn(role="user", texte=LOGEMENT), Turn(role="assistant", texte=REPONSE_LOGEMENT)]
    answer, trace, fake = await _run(
        index, [_comprendre("suivi", question_resolue=VOITURE, terms=["arrivée"],
                            facettes=["démarches pour le véhicule"]),
                _rediger(BONNE), _verdicts(("c1", True))],
        question="Et pour la voiture ?", historique=historique)

    assert fake.remaining_script == 0 and len(fake.requests) == 3
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "restituer"]
    assert answer.found is True and trace.intent == "suivi"

    comprendre_req, rediger_req, verifier_req = (_texte_envoye(r) for r in fake.requests)
    # AD-15 : l'historique voyage délimité, jamais concaténé en clair — c'est la balise qu'on relève.
    assert '<untrusted kind="historique">' in comprendre_req
    assert '<untrusted kind="historique">' in rediger_req
    assert '<untrusted kind="historique">' not in verifier_req
    for tour in (LOGEMENT, REPONSE_LOGEMENT):
        assert tour in comprendre_req and tour in rediger_req
        assert tour not in verifier_req
    # La question brute reste à *comprendre* ; en dessous, seule la résolue circule (AD-5).
    assert "Et pour la voiture ?" in comprendre_req
    assert "Et pour la voiture ?" not in rediger_req and "Et pour la voiture ?" not in verifier_req
    assert VOITURE in rediger_req and VOITURE in verifier_req


async def test_le_court_circuit_zero_hit_porte_sur_la_question_resolue_pas_sur_la_brute(
        index: Index, tmp_path: Path) -> None:
    """AD-5, mot pour mot : « le court-circuit s'applique sur `question_resolue`, jamais sur la
    question brute ».

    L'invariant n'est démontrable qu'en faisant **diverger** les deux (AC 2.2). Une question de suivi
    n'a par construction aucun mot du guide — c'est la résolution qui lui en donne —, et un
    court-circuit qui la lirait refuserait d'avance tous les suivis.

    **Ce que ce test prouve exactement** (revue 2.2, P8) : le refus lit les termes de
    `ParsedQuestion`, jamais la chaîne postée par la page. Il ne prouve pas que ces termes *dérivent*
    de la question résolue — le mock les pose côte à côte, et il passerait à l'identique avec une
    `question_resolue` vide. Cette autre moitié n'est tenable que sur un appel réel : c'est
    `tests/test_suivi_live.py::test_un_suivi_est_resolu_par_lhistorique` qui la tient, en montrant
    qu'une question autonome et des `terms` visant le véhicule sortent du **même** appel `reason`.
    """
    dico = _dictionnaire(tmp_path, index, HIPPO, validated=True)
    # Le prédicat que le pipeline exécute est `court_circuit_pour(doc_id)`, pas `court_circuit_actif`
    # (revue Codex 2.1, B3 : un dictionnaire n'arme un refus que sur le document dont il porte
    # l'empreinte). Le test étant **négatif** — aucun refus attendu —, un dictionnaire non armé pour
    # ce document-ci le ferait passer sans rien prouver (revue 2.2, P6).
    assert dico.court_circuit_pour(DOC_ID) is True
    brute = "Et celui-ci, quand faut-il s'en occuper ?"
    # Lue sur la question brute — découpée par le **tokenizer de l'index**, pas par un `split()`
    # approximatif —, la requête n'a aucun hit : le refus d'avance serait certain.
    assert not _aurait_un_hit(index, dico, words(normalize(brute)))

    answer, trace, fake = await _run(
        index, [_comprendre("suivi", question_resolue=VOITURE, terms=["arrivée"]),
                _rediger(BONNE), _verdicts(("c1", True))],
        question=brute, historique=[Turn(role="user", texte=LOGEMENT),
                                    Turn(role="assistant", texte=REPONSE_LOGEMENT)],
        dictionnaire=dico)

    assert fake.remaining_script == 0
    assert "retrouver" in [s.name for s in trace.steps]  # la question n'a pas été refusée d'avance
    assert answer.found is True and answer.reason is None


async def test_le_miroir_une_brute_pleine_de_mots_du_guide_ne_sauve_pas_des_termes_sans_hit(
        index: Index, tmp_path: Path) -> None:
    """Le même invariant vu de l'autre côté : la brute ne peut pas non plus **empêcher** le refus.

    Sans ce miroir, un court-circuit qui lirait les deux questions (« l'une ou l'autre a un hit »)
    passerait le test précédent tout en laissant filer vers l'étage `reason` une question dont rien,
    dans ce qui sera réellement cherché, ne touche le corpus.

    Même portée qu'au-dessus (revue 2.2, P8) : ce qui est épinglé est `ParsedQuestion.terms`, la
    seule entrée que le refus consulte. Que ces termes soient *ceux de la question résolue* est
    mesuré en live, pas ici.
    """
    dico = _dictionnaire(tmp_path, index, HIPPO, validated=True)
    assert dico.court_circuit_pour(DOC_ID) is True
    brute = "Quel délai pour déclarer mon arrivée à la commune et inscrire mes enfants à l'école ?"
    # Le miroir exact de la précondition précédente, même prédicat : lue sur la brute, la requête
    # aurait des hits en pagaille — et pourtant le refus tombe, parce qu'il lit les termes résolus.
    assert _aurait_un_hit(index, dico, words(normalize(brute)))

    answer, trace, fake = await _run(index, [_comprendre(terms=["hippopotame"],
                                                         question_resolue="Où déclarer un hippopotame ?")],
                                     question=brute, dictionnaire=dico)

    assert fake.remaining_script == 0 and len(fake.requests) == 1  # aucun appel `reason` facturé
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "zero_hit" and answer.reason.terms_searched == ["hippopotame"]


# --- le profil désigne, le pipeline réserve (story 2.3) ---------------------
PARCOURS_TEXTES = {
    "f1": "Vous vous déclarez à la commune dans les huit jours qui suivent votre arrivée.",
    "f2": "La commune tient la liste des crèches conventionnées.",
    "f3": "L'inscription scolaire se fait auprès de la commune avant le 1er septembre.",
}


@pytest.fixture(scope="module")
def index_parcours() -> Index:
    """Trois fiches également candidates sur « commune », et un parcours qui conditionne la dernière.

    Les trois portent le même terme, donc le même score : l'ordre des candidats est l'ordre de
    lecture, `f3` est troisième, et c'est ce qui rend la promotion observable avec `max_opens=2`.
    """
    blocs = [{"block_id": f"{DOC_ID}:{loc}:1", "loc": loc, "seq": 1, "kind": "para", "text": texte}
             for loc, texte in PARCOURS_TEXTES.items()]
    doc = Document(
        doc_id=DOC_ID, kind="guide", title="Mini guide", edition="git:test",
        nodes=[*(Node(node_id=f"{DOC_ID}:{loc}", title=loc, items=[{"block_id": f"{DOC_ID}:{loc}:1"}])
                 for loc in PARCOURS_TEXTES),
               Node(node_id=f"{DOC_ID}:root", title="Mini",
                    items=[{"node_id": f"{DOC_ID}:{loc}"} for loc in PARCOURS_TEXTES])],
        blocks=blocs,
        # La donnée de la source : « cette fiche concerne un profil qui a des enfants ».
        parcours=[{"node_id": f"{DOC_ID}:f3", "si": {"enfants": True}}])
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    manifest = {DOC_ID: ManifestEntry(status="servi", source_hash="sha-source", ingest_fingerprint="fp-1",
                                      document_hash="sha-doc", edition="git:test")}
    return Index(Corpus(documents={DOC_ID: doc}, manifest=manifest, summaries={DOC_ID: "# Mini guide"}))


async def _run_profil(index: Index, profil: Profil, script: list, **kw):
    # `max_opens=2` avec une réserve de 1 : la réserve reste **strictement** inférieure au quota,
    # sans quoi le profil remplacerait la question au lieu de l'ordonner (revue coordonnée, A4).
    kw.setdefault("profil_max_opens", 1)
    settings = _settings(max_opens=2, **kw)
    fake = FakeAnthropic(script)
    answer, trace = await repondre_guide(
        "Où inscrire mon enfant ?", [], profil, corpus=index.corpus, index=index,
        client=LlmClient(settings, anthropic_client=fake), settings=settings, request_id="req-profil",
        budget=_budget(), variant="deterministe")
    return answer, trace, fake


def _script_commune() -> list:
    claim = ("c1", "La commune tient le registre.",
             [(f"{DOC_ID}:f1:1", "vous déclarez à la commune dans les huit jours")])
    return [_comprendre(terms=["commune"]), _rediger(claim), _verdicts(("c1", True))]


async def test_le_profil_fait_ouvrir_la_fiche_qui_le_concerne_et_la_trace_le_dit(
        index_parcours: Index) -> None:
    """AC : « profil `enfants`, question scolaire ⇒ la fiche est ouverte même si son meilleur hit la
    classait hors de `max_opens`, et la trace le dit »."""
    _answer, trace, fake = await _run_profil(index_parcours, Profil(enfants="2"), _script_commune())
    assert fake.remaining_script == 0
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    assert retrouver.opened_block_ids == [f"{DOC_ID}:f1:1", f"{DOC_ID}:f3:1"]
    (check,) = [c for c in retrouver.checks if c.name == "noeuds_du_profil"]
    assert check.ok is True
    assert check.detail == f"1 place(s) réservée(s) sur 1 ({DOC_ID}:f3) ; 1 nœud(s) cédé(s) ({DOC_ID}:f2)"


async def test_un_profil_vide_laisse_le_pipeline_identique_a_lavant_story(index_parcours: Index) -> None:
    """AC : profil vide ⇒ résultat de *retrouver* identique — le parcours du document n'y change rien
    tant qu'aucune condition n'est satisfaite (inversion assumée : dans le doute, on ne promeut pas)."""
    _answer, trace, fake = await _run_profil(index_parcours, Profil(), _script_commune())
    assert fake.remaining_script == 0
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    assert retrouver.opened_block_ids == [f"{DOC_ID}:f1:1", f"{DOC_ID}:f2:1"]
    assert [c.name for c in retrouver.checks] == []


async def test_le_profil_najoute_jamais_une_fiche_que_la_question_ne_touche_pas(
        index_parcours: Index) -> None:
    """**Le profil ordonne, il n'ajoute jamais** : sur des termes que `f3` ne porte pas, la fiche
    désignée n'est pas candidate — et rien n'entre dans le contexte du modèle du fait du profil."""
    claim = ("c1", "Les crèches sont conventionnées.",
             [(f"{DOC_ID}:f2:1", "La commune tient la liste des crèches conventionnées.")])
    script = [_comprendre(terms=["crèches"]), _rediger(claim), _verdicts(("c1", True))]
    _answer, trace, fake = await _run_profil(index_parcours, Profil(enfants="2"), script)
    assert fake.remaining_script == 0
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    assert retrouver.opened_block_ids == [f"{DOC_ID}:f2:1"]  # le seul hit, et rien de plus
    (check,) = [c for c in retrouver.checks if c.name == "noeuds_du_profil"]
    assert check.detail.startswith("aucune place réservée")


async def test_le_profil_ne_voyage_pas_dans_la_trace(index_parcours: Index) -> None:
    """AD-10 : la trace ne porte pas de données personnelles au-delà du profil déclaré, et rien n'a
    besoin d'y écrire ses **clés** — le `CheckResult` ne dit que des `node_id`, produits par nous."""
    _answer, trace, _fake = await _run_profil(index_parcours, Profil(enfants="2"), _script_commune())
    peint = json.dumps([s.model_dump(mode="json") for s in trace.steps], ensure_ascii=False)
    assert "enfants" not in peint
    assert trace.thresholds["profil_max_opens"] == 1  # le seuil actif, lui, est publié (AD-10)


# --- story 2.5 : « pourquoi cette réponse » et mode dégradé honnête -----------
#
# Trois faits qui voyageaient déjà sans être lisibles (blocs, gate, dictionnaire), et deux règles que
# le mode dégradé devait à la trace : d'où vient un refus par intent (M11), et ce qu'un périmètre
# tronqué interdit d'affirmer (M13).

@pytest.fixture
def gate_du_mini_guide(index: Index):
    """Pose un gate `vertical` sur l'entrée de manifest du mini-guide, et le retire après.

    Le corpus de ce module est construit une fois (`scope="module"`) : un test qui le modifierait
    sans le rendre créerait une dépendance à l'ordre d'exécution — le défaut que la fixture
    `_etat_propre` de `tests/test_api.py` corrige côté HTTP.
    """
    from server.app.domain.ingest import Gate

    entree = index.corpus.manifest[DOC_ID]
    entree.gate = Gate(profile="vertical", source_hash=entree.source_hash,
                       ingest_fingerprint=entree.ingest_fingerprint, cases_hash="c",
                       pipeline_digest="p", prompts_digest="q", evals_ok=True,
                       date="2026-08-25", cases=2, countersigned=False)
    yield entree.gate
    entree.gate = None


@pytest.fixture
def perimetre_tronque(index: Index):
    """`corpus.alerts[doc_id]` porte `perimetre_tronque`, comme après le palier 3 du loader."""
    index.corpus.alerts[DOC_ID] = ["perimetre_tronque"]
    yield
    index.corpus.alerts.pop(DOC_ID, None)


DECLENCHEURS_METEO = ["météo", "pluie", "il fera beau", "weather"]


async def test_la_trace_resout_les_blocs_quelle_nomme_en_titres_de_fiches(index: Index) -> None:
    """AC 2.5 : « les blocs ouverts **et** écartés avec le titre de leur fiche ».

    `StepTrace.opened_block_ids` ne porte que des identifiants : à l'écran, « mini:f1:2 » ne dit pas
    à l'utilisateur ce qui a été lu. `Trace.blocs` les résout — sans jamais ajouter le **texte** d'un
    bloc (AD-10), et en suivant la règle de `fiche_id` d'`api/presenter._source_item`, pour que le
    panneau et `sources[]` ne puissent pas nommer la même fiche de deux façons.
    """
    _answer, trace, fake = await _run(index, [_comprendre(), _rediger(BONNE, BONNE_2),
                                              _verdicts(("c1", True), ("c2", True))])
    assert fake.remaining_script == 0

    nommes = [b for s in trace.steps for b in (*s.opened_block_ids, *s.discarded_block_ids)]
    assert nommes, "le chemin nominal ouvre des blocs : sans quoi ce test ne vérifie rien"
    # Tout ce que la trace nomme est résolu, une seule fois, et rien de plus n'est inventé.
    assert [b.block_id for b in trace.blocs] == list(dict.fromkeys(nommes))
    par_id = {b.block_id: b for b in trace.blocs}
    assert par_id[f"{DOC_ID}:f1:2"].node_id == f"{DOC_ID}:f1"
    assert par_id[f"{DOC_ID}:f1:2"].fiche_id == "1"  # `{doc_id}:f` retiré, comme dans `sources[]`
    assert par_id[f"{DOC_ID}:f1:2"].titre == "Arrivée"
    assert par_id[f"{DOC_ID}:f1:2"].doc_id == DOC_ID

    # AD-10 : le titre d'un **nœud**, jamais le texte d'un bloc.
    peint = json.dumps([b.model_dump() for b in trace.blocs], ensure_ascii=False)
    for bloc in index.corpus.documents[DOC_ID].blocks:
        if bloc.kind != "heading":
            assert bloc.text not in peint


async def test_un_bloc_dun_autre_document_est_omis_jamais_devine(index: Index) -> None:
    """M3 : « un id non résolu est **omis**, jamais inventé ».

    Le front a l'identifiant par `StepTrace` et affichera la ligne sans titre ; ce qu'il ne fera
    jamais, c'est deviner à quelle fiche il appartient (AD-16).
    """
    from server.app.pipelines.commun import libelles_de_blocs

    etape = StepTrace(name="retrouver", opened_block_ids=[f"{DOC_ID}:f1:2", "autre:f9:1"],
                      discarded_block_ids=[f"{DOC_ID}:f1:2", f"{DOC_ID}:f2:1"])
    blocs = libelles_de_blocs(index.corpus, DOC_ID, [etape])

    assert [b.block_id for b in blocs] == [f"{DOC_ID}:f1:2", f"{DOC_ID}:f2:1"]  # dédupliqué, ordonné
    assert libelles_de_blocs(index.corpus, "document-absent", [etape]) == []


async def test_la_trace_porte_le_gate_du_document_interroge(index: Index,
                                                             gate_du_mini_guide) -> None:
    """AC 2.5 : « le profil de gate ». AD-14 : le compte de cas et la contresignature l'accompagnent.

    `EtatApp.gate_profile` **résume** les documents servis en un scalaire ; la trace, elle, ne parle
    que du document qui a répondu à *cette* question.
    """
    _answer, trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE),
                                               _verdicts(("c1", True))])
    assert trace.gate is not None
    assert trace.gate.profile == "vertical" and trace.gate.cases == 2
    # La contresignature humaine reste due (AD-7/AD-14) : le code l'affiche telle qu'elle est.
    assert trace.gate.countersigned is False
    assert trace.gate.alerts == []


async def test_sans_gate_la_trace_le_dit_au_lieu_den_inventer_un(index: Index) -> None:
    """AD-16 : « absence ≠ valeur ». Le mini-guide n'a pas de gate : les trois champs sont `None`.

    L'objet existe quand même — c'est le manifest qui existe — et il porte les alertes du document :
    c'est ce qui distingue « ce document n'est validé par rien » de « on ne sait rien de lui ».
    """
    _answer, trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE),
                                               _verdicts(("c1", True))])
    assert trace.gate is not None
    assert (trace.gate.profile, trace.gate.cases, trace.gate.countersigned) == (None, None, None)


async def test_la_trace_dit_que_le_refus_zero_hit_est_desarme(index: Index, tmp_path: Path) -> None:
    """M12 : « `data/dictionary.json` non signé ⇒ `trace.dictionnaire.court_circuit_actif = false` ».

    AD-5 désarme le court-circuit « zéro hit » tant qu'aucune main n'a signé. C'était déjà publié par
    `/api/v1/sante` — donc par la page d'accueil — mais nulle part sur l'écran où la question se pose.
    Tant que ce booléen est faux, un refus « zéro hit » est **impossible** : l'écran peut le dire au
    lieu de laisser croire que l'absence de refus prouve quelque chose.
    """
    non_signe = _dictionnaire(tmp_path, index, {"arrivée": ["déclaration"]}, validated=False)
    _answer, trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE),
                                               _verdicts(("c1", True))], dictionnaire=non_signe)
    assert trace.dictionnaire is not None
    assert trace.dictionnaire.charge is True and trace.dictionnaire.corpus_ok is True
    assert trace.dictionnaire.validated is False
    assert trace.dictionnaire.court_circuit_actif is False

    signe = _dictionnaire(tmp_path, index, {"arrivée": ["déclaration"]}, validated=True)
    _answer, trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE),
                                               _verdicts(("c1", True))], dictionnaire=signe)
    assert trace.dictionnaire is not None and trace.dictionnaire.court_circuit_actif is True


async def test_sans_dictionnaire_la_rubrique_disparait_au_lieu_de_mentir(index: Index) -> None:
    """Un pipeline sans dictionnaire (le sinistre) n'en publie pas un inerte : `None`, et l'écran
    n'affiche rien — annoncer « non chargé » laisserait croire qu'on a regardé."""
    _answer, trace, _fake = await _run(index, [_comprendre(), _rediger(BONNE),
                                               _verdicts(("c1", True))])
    assert trace.dictionnaire is None


async def test_un_refus_par_intent_porte_aussi_ses_blocs_son_gate_et_son_dictionnaire(
        index: Index, tmp_path: Path, gate_du_mini_guide) -> None:
    """Le panneau doit exister **aussi** sur un refus : c'est là qu'on cherche pourquoi.

    Un refus par intent n'ouvre aucun bloc (le court-circuit d'AD-5 précède *retrouver*) : `blocs`
    est donc vide, et c'est un fait, pas une rubrique manquante.
    """
    dico = _dictionnaire(tmp_path, index, {"arrivée": []}, validated=False,
                         intents={"meteo": DECLENCHEURS_METEO})
    _answer, trace, fake = await _run(index, [_comprendre("meteo")],
                                      question="Quel temps fera-t-il demain ?", dictionnaire=dico)
    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert trace.blocs == []
    assert trace.gate is not None and trace.gate.profile == "vertical"
    assert trace.dictionnaire is not None and trace.dictionnaire.court_circuit_actif is False


# --- M11 : le refus dit d'où il vient ---------------------------------------

@pytest.mark.parametrize("intent", ["meteo", "bavardage", "hors_perimetre"])
async def test_un_refus_par_intent_compte_les_declencheurs_qui_le_confirment(
        index: Index, tmp_path: Path, intent: str) -> None:
    """M11 : « la trace porte un contrôle qui nomme l'intention et **compte** les déclencheurs ».

    AD-5 : « les déclencheurs d'intention sont distincts des mots du corpus — la présence d'un mot
    n'est jamais une preuve de pertinence ». Le refus reste celui de *comprendre* ; le contrôle
    **explique**, il ne décide pas. Et il ne rend que des **comptes** : un déclencheur reconnu dans
    une question est un fragment de cette question (AD-10).
    """
    dico = _dictionnaire(tmp_path, index, {"arrivée": []}, validated=False,
                         intents={i: DECLENCHEURS_METEO for i in ("meteo", "bavardage",
                                                                  "hors_perimetre")})
    _answer, trace, _fake = await _run(index, [_comprendre(intent, question_resolue="Y aura-t-il de la PLUIE demain ?")],
                                       question="Y aura-t-il de la pluie demain ?", dictionnaire=dico)

    comprendre = next(s for s in trace.steps if s.name == "comprendre")
    (check,) = [c for c in comprendre.checks if c.name == "intention_expliquee"]
    assert check.ok is True
    libelle = {"meteo": "météo", "bavardage": "bavardage",
               "hors_perimetre": "hors périmètre"}[intent]
    assert check.detail == f"intention « {libelle} » — 1 déclencheur(s) du dictionnaire sur 4 la confirment"
    # Jamais les mots eux-mêmes. « météo » est aussi le **libellé public de l'intention** exigé par
    # la spec ; sa présence ne révèle donc pas le déclencheur homonyme. Les trois autres, eux, ne
    # doivent apparaître nulle part, et aucune liste de déclencheurs ne voyage.
    peint = json.dumps([s.model_dump() for s in trace.steps], ensure_ascii=False)
    for mot in DECLENCHEURS_METEO[1:]:
        assert mot not in peint


async def test_un_refus_que_zero_declencheur_ne_confirme_est_un_controle_en_echec(
        index: Index, tmp_path: Path) -> None:
    """M11 : « zéro déclencheur ⇒ contrôle en échec (« aucun déclencheur ne la confirme ») ».

    Le refus tient quand même — c'est *comprendre* qui l'a prononcé, après avoir lu la question
    entière. Ce que le contrôle rouge dit, c'est qu'aucune corroboration lexicale ne l'accompagne :
    un fait sur lequel l'utilisateur peut se faire une opinion, pas une faute du pipeline.
    """
    dico = _dictionnaire(tmp_path, index, {"arrivée": []}, validated=False,
                         intents={"meteo": DECLENCHEURS_METEO})
    _answer, trace, _fake = await _run(
        index, [_comprendre("meteo", question_resolue="Quel est le programme de la fête ?")],
        question="Quel est le programme de la fête ?", dictionnaire=dico)

    comprendre = next(s for s in trace.steps if s.name == "comprendre")
    (check,) = [c for c in comprendre.checks if c.name == "intention_expliquee"]
    assert check.ok is False
    assert check.detail == ("intention « météo » — aucun déclencheur ne la confirme (4 connu(s)) : "
                            "le refus tient au seul jugement du modèle")


async def test_sans_dictionnaire_lintention_reste_expliquee_par_un_zero_honnete(index: Index) -> None:
    """Aucun dictionnaire (ou aucun déclencheur pour cette intention) : « 0 connu(s) », et le contrôle
    échoue. C'est vrai — rien ne corrobore — et rien n'est deviné (AD-16)."""
    _answer, trace, _fake = await _run(index, [_comprendre("bavardage")])
    comprendre = next(s for s in trace.steps if s.name == "comprendre")
    (check,) = [c for c in comprendre.checks if c.name == "intention_expliquee"]
    assert check.ok is False and "(0 connu(s))" in check.detail


async def test_le_compte_des_declencheurs_porte_sur_la_question_resolue(index: Index,
                                                                        tmp_path: Path) -> None:
    """AD-5 : « le court-circuit s'applique sur `question_resolue`, jamais sur la question brute ».

    Le contrôle qui l'explique doit porter sur le **même** texte, sans quoi il commenterait une autre
    question que celle qui a été refusée.
    """
    dico = _dictionnaire(tmp_path, index, {"arrivée": []}, validated=False,
                         intents={"meteo": DECLENCHEURS_METEO})
    _answer, trace, _fake = await _run(
        index, [_comprendre("meteo", question_resolue="Et la météo, alors ?")],
        question="Et ça, alors ?", dictionnaire=dico)  # la question brute ne porte aucun déclencheur

    comprendre = next(s for s in trace.steps if s.name == "comprendre")
    (check,) = [c for c in comprendre.checks if c.name == "intention_expliquee"]
    assert check.ok is True and "1 déclencheur(s)" in check.detail


# --- M13 : le périmètre tronqué désarme le refus qu'il ne peut plus fonder ----

async def test_un_perimetre_tronque_desarme_le_refus_hors_perimetre(index: Index,
                                                                     perimetre_tronque) -> None:
    """M13 + AC : « la question poursuit vers *retrouver* et la trace porte `hors_perimetre_desarme` ».

    `hors_perimetre` est la seule des trois intentions que le modèle rende *en lisant une liste que
    nous lui donnons* — la projection des titres du document, dont le prompt affirme qu'« elle fait
    foi, aucune autre ». Amputée d'une catégorie entière (alerte `perimetre_tronque`), cette
    affirmation est fausse, et le refus porterait sur des sujets que le guide traite. Poursuivre
    laisse une chance de réponse ; s'il n'y a rien, le refus rendu portera une **preuve** au lieu
    d'un jugement sur une liste incomplète.
    """
    answer, trace, fake = await _run(index, [_comprendre("hors_perimetre"), _rediger(BONNE),
                                             _verdicts(("c1", True))])
    assert fake.remaining_script == 0
    # La chaîne complète a tourné : le court-circuit d'AD-5 n'a pas eu lieu.
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "restituer"]
    assert answer.found is True and answer.reason is None
    assert trace.intent == "hors_perimetre"  # ce que le modèle a rendu n'est pas réécrit

    comprendre = next(s for s in trace.steps if s.name == "comprendre")
    (check,) = [c for c in comprendre.checks if c.name == "hors_perimetre_desarme"]
    assert check.ok is False and "perimetre_tronque" in check.detail
    # Désarmé, donc jamais expliqué comme un refus : il n'y a pas eu de refus.
    assert [c.name for c in comprendre.checks if c.name == "intention_expliquee"] == []


async def test_un_perimetre_tronque_ne_desarme_pas_une_clarification_neutralisee(
        index: Index, perimetre_tronque) -> None:
    """B1 : une liste de périmètre amputée ne rend pas autonome une référence irrésolue.

    La sortie modèle contradictoire porte à la fois l'intention que le périmètre tronqué ne permet
    pas de croire et la seule question de clarification disponible. Cette dernière doit être servie
    après l'unique appel ``reason`` ; la question brute non autonome ne doit jamais devenir l'entrée
    de *retrouver*.
    """
    clarification = "De quelle démarche parlez-vous ?"
    answer, trace, fake = await _run(
        index,
        [_comprendre("hors_perimetre", clarification=clarification)],
        question="Et pour celle-ci ?",
    )

    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [step.name for step in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.clarification == clarification
    assert answer.reason is not None and answer.reason.kind == "clarification_requise"
    comprendre = next(step for step in trace.steps if step.name == "comprendre")
    assert [check.name for check in comprendre.checks] == [
        "clarification_refus_neutralisee",
        "clarification_retablie_perimetre_tronque",
    ]
    checks = {check.name: check for check in comprendre.checks}
    assert checks["clarification_refus_neutralisee"].ok is False
    assert checks["clarification_retablie_perimetre_tronque"].ok is False


async def test_une_clarification_neutralisee_retablie_publie_le_repli_de_langue(
        index: Index, perimetre_tronque) -> None:
    """Le chemin privé publie la même divergence de langue qu'une clarification ordinaire."""
    clarification = "¿De qué trámite habla?"
    answer, trace, fake = await _run(
        index,
        [_comprendre("hors_perimetre", clarification=clarification, language="es")],
        question="¿Y para este?",
    )

    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [step.name for step in trace.steps] == ["comprendre", "restituer"]
    assert answer.clarification == clarification
    assert answer.lang == "fr" and answer.lang_fallback is True
    comprendre = next(step for step in trace.steps if step.name == "comprendre")
    assert [check.name for check in comprendre.checks] == [
        "clarification_refus_neutralisee",
        "clarification_langue_non_affirmee",
        "clarification_retablie_perimetre_tronque",
    ]
    assert all(check.ok is False for check in comprendre.checks)


async def test_un_perimetre_tronque_ignore_aussi_le_court_circuit_zero_hit(
        index: Index, perimetre_tronque, tmp_path: Path) -> None:
    """M13 dit que la chaîne poursuit **vers retrouver** : un dictionnaire signé ne doit pas
    réintroduire, quelques lignes plus bas, le refus que le périmètre tronqué vient de désarmer.

    Les termes ne touchent volontairement aucun bloc. La réponse reste un refus `zero_hit`, mais
    seulement **après** l'étape retrouver, avec une preuve issue de la recherche effectivement
    menée — jamais par le pré-contrôle fondé sur le même périmètre incomplet.
    """
    dico = _dictionnaire(tmp_path, index, {"hippopotame": []}, validated=True)
    answer, trace, fake = await _run(
        index, [_comprendre("hors_perimetre", terms=["hippopotame"])], dictionnaire=dico)

    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "restituer"]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "zero_hit"
    assert trace.dictionnaire is not None
    assert trace.dictionnaire.court_circuit_actif is False
    comprendre = next(s for s in trace.steps if s.name == "comprendre")
    assert [c.name for c in comprendre.checks] == ["hors_perimetre_desarme"]


@pytest.mark.parametrize("intent", ["meteo", "bavardage"])
async def test_meteo_et_bavardage_restent_refuses_sous_un_perimetre_tronque(
        index: Index, perimetre_tronque, intent: str) -> None:
    """M13 : « `meteo` / `bavardage` restent refusés ». Ni l'un ni l'autre ne se décide sur le
    périmètre annoncé — une question sur la pluie de demain reste hors du guide quelle que soit la
    liste de ses catégories."""
    answer, trace, fake = await _run(index, [_comprendre(intent)])
    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "hors_perimetre"
    comprendre = next(s for s in trace.steps if s.name == "comprendre")
    assert [c.name for c in comprendre.checks if c.name == "hors_perimetre_desarme"] == []
    assert [c.name for c in comprendre.checks if c.name == "intention_expliquee"] != []


async def test_sans_alerte_le_refus_hors_perimetre_reste_actif(index: Index) -> None:
    """Le pendant du désarmement : sur un périmètre entier, `hors_perimetre` refuse comme avant.

    Sur le corpus livré la liste tient (3 004 caractères sur 4 000) : c'est ce chemin-ci que la
    production emprunte, et c'est celui qu'il faut le plus verrouiller.
    """
    answer, trace, fake = await _run(index, [_comprendre("hors_perimetre")])
    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.reason is not None
    assert answer.reason.kind == "hors_perimetre"


# --- le plafond de sortie de *vérifier* tient sous le budget par requête ------
async def _preflights_de_la_chaine(index: Index, settings: Settings,
                                   monkeypatch: pytest.MonkeyPatch,
                                   variant: str = "outils") -> list[tuple[float, float, int]]:
    """La chaîne guide entière, scriptée, avec le majorant relevé **à chaque** préflight.

    Le budget est celui de la production (`max_cost_eur_per_request`), et non le plafond confortable
    des autres scénarios de ce fichier : ce témoin mesure exactement ce que le client confronte au
    coût déjà engagé avant d'envoyer un appel.
    """
    import server.app.llm.client as client_module

    budget = RequestBudget(deadline_s=settings.deadline_s, max_attempts=settings.max_llm_attempts,
                           max_cost_eur=settings.max_cost_eur_per_request)
    original = client_module.estimate_cost
    releves: list[tuple[float, float, int]] = []

    def relever(*args: Any, **kwargs: Any) -> float:
        estimate = original(*args, **kwargs)
        max_tokens = int(args[3] if len(args) > 3 else kwargs["max_tokens"])
        releves.append((budget.cost_eur, estimate, max_tokens))
        return estimate

    monkeypatch.setattr(client_module, "estimate_cost", relever)
    script = _avec_navigation_outils(
        [_comprendre(), _rediger(BONNE), _verdicts(("c1", True))], variant)
    answer, _trace, fake = await _run(index, script, settings=settings, budget=budget,
                                      variant=variant)
    assert answer.found and fake.remaining_script == 0
    return releves


REFLEXION_MESUREE = 1024
"""Ce que la réflexion étendue de Sonnet a **réellement** dépensé sur l'appel de *vérifier*.

Relevé sur les fixtures du worktree de preuve : quatre réponses du schéma de sortie de *vérifier*
guide portent `stop_reason: max_tokens`, `output_tokens = 1024`, et pour tout contenu un bloc
`thinking` — pas un caractère de JSON. L'effort `medium` du tier `reason` compte la réflexion dans
le **même** `max_tokens` que la sortie : un plafond inférieur ou égal à cette dépense ne laisse rien
pour le contrat.
"""


def _verifier_tronque() -> dict:
    """La forme exacte des quatre réponses tronquées : un bloc `thinking`, aucun texte."""
    return fake_message(model=TIERS[_settings().verifier_tier], stop_reason="max_tokens",
                        output_tokens=REFLEXION_MESUREE,
                        content=[{"type": "thinking", "thinking": "…", "signature": "sig"}])


def _verifier_sous_plafond(settings: Settings, *paires: tuple[str, bool]) -> dict:
    """Ce que l'API rend vraiment : la réponse tronquée si la réflexion remplit le plafond.

    C'est le seul point du témoin où une décision est prise, et elle est celle du fournisseur :
    `max_tokens` borne réflexion **plus** sortie, donc un plafond que la réflexion mesurée remplit
    à elle seule ne rend aucun JSON.
    """
    if REFLEXION_MESUREE >= settings.verifier_max_tokens:
        return _verifier_tronque()
    return _verdicts(*paires)


async def test_le_plafond_retenu_ecarte_la_troncature_mesuree(index: Index) -> None:
    """Le témoin décisif du recalibrage : rouge au plafond d'avant, vert au plafond retenu.

    À 1 024, la réflexion mesurée remplit le plafond, la relance motivée d'AD-16 se fait tronquer à
    son tour, et la chaîne finit en `LlmParse` — un 503 sur une question parfaitement nominale, ce
    qu'AD-16 refuse. Au plafond retenu, le même scénario rend sa réponse vérifiée.

    Ce témoin ne mesure pas un coût : il mesure **la panne que le recalibrage supprime**. La moitié
    coût est mesurée séparément, et dit ce qu'elle prouve.
    """
    ancien = _settings(verifier_max_tokens=REFLEXION_MESUREE)
    script_ancien = [_comprendre(), _rediger(BONNE),
                     _verifier_sous_plafond(ancien, ("c1", True)),
                     _verifier_sous_plafond(ancien, ("c1", True))]
    with pytest.raises(LlmParse, match="tronquée"):
        await _run(index, script_ancien, settings=ancien)

    retenu = _settings()
    assert retenu.verifier_max_tokens > REFLEXION_MESUREE, (
        "le plafond retenu doit laisser de la place au contrat après la réflexion mesurée")
    answer, trace, fake = await _run(
        index, [_comprendre(), _rediger(BONNE), _verifier_sous_plafond(retenu, ("c1", True))],
        settings=retenu)
    assert answer.found and fake.remaining_script == 0
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "restituer"]


async def test_le_relevement_du_plafond_ne_coute_que_les_tokens_ajoutes(
        index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    """La moitié **coût** du recalibrage, et rien de plus que ce qu'elle démontre.

    Le majorant de préflight est la seule dépense qu'un plafond de sortie plus large engage :
    `max_tokens` ne facture pas, il borne. Ce témoin mesure ce delta sur la chaîne entière et exige
    qu'il vaille **exactement** les tokens ajoutés au tarif de sortie du tier servi — il rougit donc
    si le plafond change, si le tier de *vérifier* change, ou si la tarification change.

    Ce qu'il **ne** prouve **pas**, et que la revue a eu raison de relever : que le plafond retenu
    soit le seul admissible. Sur ce corpus, tout l'intervalle légal tient sous le budget par requête ;
    la garde ci-dessous vaut donc comme garde-fou, pas comme preuve du chiffre. Ce qui justifie le
    chiffre est `test_le_plafond_retenu_ecarte_la_troncature_mesuree`.
    """
    settings = _settings()

    async def majorant_de_verifier(plafond: int) -> float:
        reglages = _settings(verifier_max_tokens=plafond)
        preflights = await _preflights_de_la_chaine(index, reglages, monkeypatch)
        return max(majorant for _engage, majorant, tokens in preflights if tokens == plafond)

    ancien = await majorant_de_verifier(REFLEXION_MESUREE)
    retenu = await majorant_de_verifier(settings.verifier_max_tokens)

    sortie_usd = PRICES[TIERS[settings.verifier_tier]]["output"]
    attendu = ((settings.verifier_max_tokens - REFLEXION_MESUREE)
               * sortie_usd / 1_000_000 * settings.usd_eur)
    # Comparé à la précision où le majorant est publié : `estimate_cost` arrondit au centième de
    # centime, et deux majorants arrondis ne peuvent pas rendre un delta plus fin que leur pas.
    assert round(retenu - ancien, 4) == round(attendu, 4), (
        f"delta mesuré {retenu - ancien:.4f} € ; attendu {attendu:.4f} €")

    # Garde-fou, explicitement pas une preuve du chiffre : aucun préflight de la chaîne ne déborde.
    preflights = await _preflights_de_la_chaine(index, settings, monkeypatch)
    assert settings.verifier_max_tokens in {tokens for _e, _m, tokens in preflights}
    assert [(e, m, t) for e, m, t in preflights
            if e + m > settings.max_cost_eur_per_request] == []

