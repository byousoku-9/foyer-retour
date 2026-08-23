"""Matrice I/O de *retrouver* déterministe (spec 1.4) : code pur, zéro appel modèle — ouverture groupée
bornée par `RetrievalBudget`, suivi d'un niveau des renvois et des définitions hors quota, `truncated`,
hits non ouverts en `discarded_block_ids`, blocs relus du corpus, aucune absence affirmée."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.domain import Block, BlockRef, Document, Node, NodeRef, RetrievalBudget
from server.app.domain.question import ParsedQuestion, QuestionScope
from server.app.steps.retrouver import retrouver_deterministe

ROOT = Path(__file__).resolve().parents[1]


def _budget(**kw) -> RetrievalBudget:
    base = {"max_opens": 6, "node_window": 30, "search_limit": 20, "max_llm_turns": 2}
    return RetrievalBudget(**(base | kw))


def _parsed(terms: list[str], themes: list[str] | None = None) -> ParsedQuestion:
    return ParsedQuestion(question_resolue="q", intent="question", terms=terms,
                          scope=QuestionScope(themes=themes or []))


def _corpus() -> Corpus:
    """Trois nœuds : n1 (hit + renvoi), n2 (hit), n3 (cible du renvoi + définition « contenu »)."""
    blocks = [
        Block(block_id="d:p1:1", text="Le matricule est délivré par la commune.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Voir aussi la définition.", loc="p1", seq=2, refs=["d:p1:5"]),
        Block(block_id="d:p2:1", text="Le matricule sert partout.", loc="p2", seq=1),
        Block(block_id="d:p3:1", text="Le contenu désigne les meubles du logement.", loc="p3", seq=1,
              kind="definition", defines="contenu"),
        Block(block_id="d:p1:5", text="Cible du renvoi, sans aucun terme cherché.", loc="p1", seq=5),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2"), NodeRef(node_id="n3")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1"), BlockRef(block_id="d:p1:2")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1")]),
             Node(node_id="n3", items=[BlockRef(block_id="d:p3:1"), BlockRef(block_id="d:p1:5")])]
    doc = Document(doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)
    return Corpus(documents={"d": doc})


def test_nominal_opens_candidate_nodes_follows_refs_and_reads_blocks_from_the_corpus() -> None:
    corpus = _corpus()
    index = Index(corpus)
    result, step = retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=index, budget=_budget())
    # n1 et n2 ouverts (fenêtres complètes), puis la cible du renvoi de d:p1:2 — hors quota
    assert result.opened_block_ids == ["d:p1:1", "d:p1:2", "d:p2:1", "d:p1:5"]
    assert result.truncated is False and result.discarded_block_ids == []
    # blocs relus depuis le corpus (mêmes objets), jamais modifiés
    doc = corpus.documents["d"]
    assert all(b is doc.block(b.block_id) for b in result.blocs)
    # l'étape crée sa propre trace : variante déterministe ⇒ tier=None, aucun appel modèle
    assert step.name == "retrouver" and step.tier is None and step.calls == [] and step.ms >= 0
    assert step.opened_block_ids == result.opened_block_ids
    assert step.discarded_block_ids == result.discarded_block_ids


def test_bounded_by_max_opens_discards_unopened_hits_and_truncates() -> None:
    corpus = _corpus()
    index = Index(corpus)
    result, _ = retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=index,
                                       budget=_budget(max_opens=1))
    assert result.opened_block_ids == ["d:p1:1", "d:p1:2", "d:p1:5"]  # n1 seul + renvoi hors quota
    assert result.truncated is True
    assert result.discarded_block_ids == ["d:p2:1"]  # hit non ouvert


def test_definitions_of_terms_come_in_beyond_the_quota() -> None:
    corpus = _corpus()
    index = Index(corpus)
    result, _ = retrouver_deterministe(_parsed(["matricule", "contenu"]), corpus=corpus, index=index,
                                       budget=_budget(max_opens=1))
    # n1 ouvert (meilleur score) ; la définition « contenu » entre hors quota même si son nœud
    # candidat (n3) est au-delà de max_opens
    assert "d:p3:1" in result.opened_block_ids
    assert result.truncated is True
    assert set(result.discarded_block_ids) == {"d:p2:1"}


def test_truncated_window_marks_the_result() -> None:
    blocks = [Block(block_id=f"d:p1:{i}", text="terme cherché" if i == 5 else f"bloc {i}", loc="p1", seq=i)
              for i in range(1, 9)]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    corpus = Corpus(documents={"d": Document(doc_id="d", kind="guide", title="t", edition="e",
                                             nodes=nodes, blocks=blocks)})
    index = Index(corpus)
    result, _ = retrouver_deterministe(_parsed(["cherché"]), corpus=corpus, index=index,
                                       budget=_budget(node_window=3))
    assert result.truncated is True  # nœud > node_window : fenêtre coupée, pas de pagination
    assert "d:p1:5" in result.opened_block_ids  # la fenêtre contient le meilleur hit du nœud
    assert len(result.opened_block_ids) == 3


def test_zero_hit_returns_an_empty_result_without_asserting_absence() -> None:
    corpus = _corpus()
    index = Index(corpus)
    for parsed in (_parsed(["zzz-introuvable"]), _parsed([])):
        result, step = retrouver_deterministe(parsed, corpus=corpus, index=index, budget=_budget())
        assert result.blocs == [] and result.opened_block_ids == [] and result.discarded_block_ids == []
        assert result.truncated is False  # aucune absence affirmée : c'est l'affaire de *vérifier* (1.5)
        assert step.calls == []


def test_scope_themes_enrich_the_search() -> None:
    corpus = _corpus()
    index = Index(corpus)
    by_terms, _ = retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=index, budget=_budget())
    by_themes, _ = retrouver_deterministe(_parsed([], themes=["matricule"]), corpus=corpus, index=index,
                                          budget=_budget())
    assert by_themes.opened_block_ids == by_terms.opened_block_ids


def test_max_blocks_cuts_the_window_tail_and_keeps_the_out_of_quota_targets() -> None:
    # Revue 1.4 : la borne de coût coupe la queue des fenêtres, pas les renvois. Sans quoi la cible
    # d:p1:5 — ajoutée en dernier parce qu'elle est suivie hors quota — serait la première perdue,
    # et le suivi des renvois d'AD-1 ne survivrait à aucune question réelle.
    corpus = _corpus()
    index = Index(corpus)
    result, step = retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=index,
                                          budget=_budget(max_blocks=2))
    assert len(result.blocs) == 2 and result.truncated is True
    assert result.opened_block_ids == ["d:p1:1", "d:p1:5"]  # 1er bloc de fenêtre + cible du renvoi
    # la coupe se trace : le hit jamais transmis, puis le bloc de fenêtre coupé
    assert result.discarded_block_ids == ["d:p2:1", "d:p1:2"]
    assert step.discarded_block_ids == result.discarded_block_ids


def test_max_blocks_smaller_than_the_out_of_quota_targets_cuts_them_too() -> None:
    corpus = _corpus()
    index = Index(corpus)
    result, _ = retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=index,
                                       budget=_budget(max_blocks=1))
    assert result.opened_block_ids == ["d:p1:5"] and result.truncated is True
    assert sorted(result.discarded_block_ids) == ["d:p1:1", "d:p1:2", "d:p2:1"]


def test_on_the_real_corpus_with_config_thresholds() -> None:
    s = Settings(_env_file=None)
    corpus = load_corpus(ROOT / "data", allow_ungated=True)
    index = Index(corpus)
    budget = RetrievalBudget(max_opens=s.max_opens, node_window=s.node_window,
                             search_limit=s.search_limit, max_llm_turns=s.max_llm_turns,
                             max_blocks=s.retrieval_max_blocks)
    result, step = retrouver_deterministe(_parsed(["matricule", "commune"]), corpus=corpus, index=index,
                                          budget=budget, doc_id="lux-guide")
    assert result.blocs and all(b.block_id.startswith("lux-guide:") for b in result.blocs)
    assert any(b.block_id.startswith("lux-guide:farrivee:") for b in result.blocs)  # la fiche attendue
    assert result.opened_block_ids == [b.block_id for b in result.blocs]
    assert set(result.discarded_block_ids).isdisjoint(result.opened_block_ids)
    assert step.calls == [] and step.tier is None
    # définitions réelles : le terme « contenu » ramène la définition AXA hors quota
    axa, _ = retrouver_deterministe(_parsed(["contenu"]), corpus=corpus, index=index, budget=budget,
                                    doc_id="axa-lu-optihome-2017")
    assert "axa-lu-optihome-2017:p9:2" in axa.opened_block_ids
    # …et le saut « définitions » reste dans le document demandé : interrogé sur le guide, le même
    # terme ne ramène jamais la définition du contrat (revue 1.4 — sans `doc_id`, elle passait).
    guide, _ = retrouver_deterministe(_parsed(["contenu"]), corpus=corpus, index=index, budget=budget,
                                      doc_id="lux-guide")
    assert all(b.block_id.startswith("lux-guide:") for b in guide.blocs)


def test_unknown_doc_id_raises_like_the_index() -> None:
    corpus = _corpus()
    index = Index(corpus)
    # y compris sans aucun terme : `chercher` n'est alors pas appelé, mais un doc_id fautif reste une
    # faute de l'appelant — la taire rendrait une faute de frappe invisible (revue 1.4).
    for parsed in (_parsed(["matricule"]), _parsed([])):
        with pytest.raises(KeyError):
            retrouver_deterministe(parsed, corpus=corpus, index=index, budget=_budget(), doc_id="inconnu")
