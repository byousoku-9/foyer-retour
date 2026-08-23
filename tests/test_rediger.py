"""Matrice I/O de *rédiger* (spec 1.4) : requête envoyée — préfixe cacheable 1 h = commun + rediger +
sommaire versionné, blocs / question / historique délimités par `untrusted()` après le préfixe,
`max_tokens=rediger_max_tokens`, `motif` sur le dernier message utilisateur — et draft incohérent ⇒
retry motivé puis `LlmParse` (invariants `AnswerDraft` du domaine)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.domain.answer import AnswerDraft
from server.app.domain.errors import LlmParse
from server.app.domain.question import ParsedQuestion, Turn
from server.app.domain.retrieval import RetrievalResult
from server.app.domain.trace import StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.llm.prompting import load_prompt
from server.app.steps.rediger import rediger
from server.ingest import kb_to_blocks as k
from tests.llm_fake import FakeAnthropic, fake_message

SONNET = TIERS["reason"]
MINI = Path(__file__).parent / "data" / "mini_kb.js"
UNTRUSTED = re.compile(r'<untrusted kind="([a-z0-9_]+)">\n(.*?)\n</untrusted>', re.DOTALL)


@pytest.fixture(scope="module")
def mini_index(tmp_path_factory: pytest.TempPathFactory) -> Index:
    d = tmp_path_factory.mktemp("data") / "lux-guide"
    d.mkdir(parents=True)
    shutil.copy(MINI, d / "source.js")
    k.run(d, edition="git:test")
    return Index(load_corpus(d.parent, allow_ungated=True))


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget(deadline_s: float = 30.0) -> RequestBudget:
    return RequestBudget(deadline_s=deadline_s, max_attempts=4, max_cost_eur=0.10)


def _retrieval(index: Index, block_ids: list[str]) -> RetrievalResult:
    doc = index.corpus.documents["lux-guide"]
    return RetrievalResult(blocs=[doc.block(b) for b in block_ids], opened_block_ids=list(block_ids))


def _parsed(**over) -> ParsedQuestion:
    base = {"question_resolue": "Quel délai pour déclarer l'arrivée ?", "intent": "question",
            "terms": ["déclaration d'arrivée"]}
    return ParsedQuestion(**(base | over))


def _draft(**over) -> str:
    base = {
        "segments": [{"text": "Vous avez huit jours.", "kind": "factuel", "claim_ids": ["c1"]},
                     {"text": "Bonne installation.", "kind": "transition", "claim_ids": []}],
        "claims": [{"claim_id": "c1", "text": "Le délai de déclaration est de huit jours.",
                    "quotes": [{"block_id": "lux-guide:farrivee:3",
                                "quote": "huit jours pour déclarer votre arrivée"}]}],
    }
    return json.dumps(base | over, ensure_ascii=False)


def _client(script: list) -> tuple[LlmClient, FakeAnthropic]:
    fake = FakeAnthropic(script)
    return LlmClient(_settings(), anthropic_client=fake), fake


async def _rediger(client: LlmClient, index: Index, *, block_ids: list[str] | None = None,
                   historique: list[Turn] | None = None, parsed: ParsedQuestion | None = None, **kw):
    retrieval = _retrieval(index, block_ids or ["lux-guide:farrivee:3", "lux-guide:fbail_test:3"])
    return await rediger(parsed or _parsed(), retrieval, historique or [], client=client,
                         budget=kw.pop("budget", None) or _budget(), index=index, doc_id="lux-guide",
                         settings=_settings(), **kw)


async def test_nominal_returns_the_draft_and_its_own_step_trace(mini_index: Index) -> None:
    client, fake = _client([fake_message(text=_draft(), model=SONNET)])
    draft, step = await _rediger(client, mini_index)
    assert isinstance(draft, AnswerDraft)
    assert draft.claims[0].quotes[0].block_id == "lux-guide:farrivee:3"
    assert step.name == "rediger" and step.tier == "reason" and step.ms >= 0
    assert step.opened_block_ids == ["lux-guide:farrivee:3", "lux-guide:fbail_test:3"]
    assert len(step.calls) == 1 and step.calls[0].model == SONNET


async def test_request_shape_cacheable_prefix_with_summary_then_delimited_content(mini_index: Index) -> None:
    client, fake = _client([fake_message(text=_draft(), model=SONNET)])
    historique = [Turn(role="user", texte="on vient d'arriver"), Turn(role="assistant", texte="bienvenue")]
    await _rediger(client, mini_index, historique=historique)
    (req,) = fake.requests
    s = _settings()
    # préfixe système byte-identique : commun + rediger + sommaire versionné (en-tête de hash inclus),
    # breakpoint cache 1 h (reason)
    expected_prefix = load_prompt("commun") + "\n\n" + load_prompt("rediger") + "\n\n" + \
        mini_index.sommaire("lux-guide")
    assert req["system"] == [{"type": "text", "text": expected_prefix,
                             "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    assert mini_index.sommaire("lux-guide").startswith("<!-- lux-guide · edition git:test")
    assert req["output_config"]["effort"] == "medium"
    assert req["max_tokens"] == s.rediger_max_tokens == 2048
    # historique, blocs (avec leur block_id) et question résolue après le préfixe, chacun sous untrusted()
    (msg,) = req["messages"]
    found = UNTRUSTED.findall(msg["content"])
    kinds = [kind for kind, _ in found]
    assert kinds == ["historique", "document", "document", "question"]
    docs = [text for kind, text in found if kind == "document"]
    doc = mini_index.corpus.documents["lux-guide"]
    assert docs[0] == "lux-guide:farrivee:3\n" + doc.block("lux-guide:farrivee:3").text
    assert docs[1] == "lux-guide:fbail_test:3\n" + doc.block("lux-guide:fbail_test:3").text
    assert [t for kind, t in found if kind == "question"] == ["Quel délai pour déclarer l'arrivée ?"]
    assert json.loads([t for kind, t in found if kind == "historique"][0]) == \
        [{"role": "user", "texte": "on vient d'arriver"}, {"role": "assistant", "texte": "bienvenue"}]
    # aucun texte utilisateur ni documentaire hors balises ; seule la consigne de langue reste
    outside = UNTRUSTED.sub("", msg["content"])
    assert outside.strip() == "Langue de rédaction : fr."
    for fragment in ("huit jours", "on vient d'arriver", "bienvenue", "déclarer l'arrivée ?"):
        assert fragment not in outside


async def test_motif_is_appended_to_the_last_user_message_only_when_present(mini_index: Index) -> None:
    client, fake = _client([fake_message(text=_draft(), model=SONNET),
                            fake_message(text=_draft(), model=SONNET)])
    await _rediger(client, mini_index)
    assert "Motif de relance" not in fake.requests[0]["messages"][-1]["content"]
    await _rediger(client, mini_index, motif="quote introuvable dans lux-guide:farrivee:3")
    content = fake.requests[1]["messages"][-1]["content"]
    assert content.endswith("Motif de relance : quote introuvable dans lux-guide:farrivee:3")


async def test_language_of_the_parsed_question_drives_the_writing_language(mini_index: Index) -> None:
    client, fake = _client([fake_message(text=_draft(), model=SONNET)])
    await _rediger(client, mini_index, parsed=_parsed(language="en"))
    assert "Langue de rédaction : en." in fake.requests[0]["messages"][0]["content"]


async def test_incoherent_draft_triggers_one_motivated_retry_then_parses(mini_index: Index) -> None:
    bad = _draft(segments=[{"text": "f", "kind": "factuel", "claim_ids": ["c9"]}])  # claim inconnue
    client, fake = _client([fake_message(text=bad, model=SONNET), fake_message(text=_draft(), model=SONNET)])
    step = StepTrace(name="rediger")
    retrieval = _retrieval(mini_index, ["lux-guide:farrivee:3"])
    draft, _ = await rediger(_parsed(), retrieval, [], client=client, budget=_budget(), index=mini_index,
                             doc_id="lux-guide", settings=_settings())
    assert isinstance(draft, AnswerDraft) and len(fake.requests) == 2
    retry_msg = fake.requests[1]["messages"][-1]["content"]
    assert "invalide" in retry_msg
    assert fake.requests[1]["system"] == fake.requests[0]["system"]  # préfixe byte-identique au retry


@pytest.mark.parametrize("bad_draft", [
    lambda: _draft(segments=[{"text": "f", "kind": "factuel", "claim_ids": ["c9"]}]),  # claim_ids inconnus
    lambda: _draft(segments=[{"text": "f", "kind": "factuel", "claim_ids": []}]),  # factuel sans claim
])
async def test_persistently_incoherent_draft_raises_llm_parse(mini_index: Index, bad_draft) -> None:
    client, fake = _client([fake_message(text=bad_draft(), model=SONNET),
                            fake_message(text=bad_draft(), model=SONNET)])
    with pytest.raises(LlmParse):
        await _rediger(client, mini_index)
    assert len(fake.requests) == 2  # 1 retry motivé, puis LlmParse (code llm_parse)
