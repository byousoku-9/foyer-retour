"""AD-1 — `sommaire`, `ouvrir_noeud` (fenêtre, curseur, bloc visé) et `chercher` (mot entier, tri, limite)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index, reading_order, words
from server.app.corpus.loader import Corpus, load_corpus
from server.app.domain import Block, BlockRef, Document, Node, NodeRef
from server.ingest import kb_to_blocks as k

ROOT = Path(__file__).resolve().parents[1]
MINI = Path(__file__).parent / "data" / "mini_kb.js"


def _doc(n_blocks: int, doc_id: str = "d", prefix: str = "") -> Document:
    blocks = [Block(block_id=f"{doc_id}:p1:{i}", text=f"Bloc {i}", loc="p1", seq=i) for i in range(1, n_blocks + 1)]
    nodes = [Node(node_id=f"{prefix}root", items=[NodeRef(node_id=f"{prefix}n")]),
             Node(node_id=f"{prefix}n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    return Document(doc_id=doc_id, kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)


@pytest.fixture
def mini_index(tmp_path: Path) -> Index:
    d = tmp_path / "data" / "lux-guide"
    d.mkdir(parents=True)
    shutil.copy(MINI, d / "source.js")
    k.run(d, edition="git:test")
    return Index(load_corpus(d.parent, allow_ungated=True))


def test_reading_order_follows_items_from_root(mini_index: Index) -> None:
    doc = mini_index.corpus.documents["lux-guide"]
    order = reading_order(doc)
    assert [b for b, _ in order[:3]] == ["lux-guide:farrivee:1", "lux-guide:farrivee:2", "lux-guide:farrivee:3"]
    assert order[-1] == ("lux-guide:q2:2", "lux-guide:q2")
    assert len(order) == len(doc.blocks) == len(mini_index)
    assert mini_index.parent_node("lux-guide:fbail_test:2") == "lux-guide:fbail_test"
    assert mini_index.doc_of("lux-guide:q1:1") == "lux-guide"


def test_sommaire(mini_index: Index) -> None:
    assert mini_index.sommaire("lux-guide").startswith("<!-- lux-guide · edition git:test")
    with pytest.raises(KeyError):
        mini_index.sommaire("inconnu")


def test_ouvrir_noeud_window_focus_and_cursor() -> None:
    ix = Index(Corpus(documents={"d": _doc(8)}))
    w = ix.ouvrir_noeud("n", focus_block_id="d:p1:6", node_window=3)
    assert [b.block_id for b in w.blocks] == ["d:p1:4", "d:p1:5", "d:p1:6"]
    assert w.truncated and w.next_cursor == 6 and w.node_id == "n"
    w = ix.ouvrir_noeud("n", cursor=w.next_cursor, node_window=3)
    assert [b.block_id for b in w.blocks] == ["d:p1:7", "d:p1:8"] and w.truncated and w.next_cursor is None
    w = ix.ouvrir_noeud("n", node_window=3)
    assert [b.block_id for b in w.blocks] == ["d:p1:1", "d:p1:2", "d:p1:3"] and w.next_cursor == 3
    w = ix.ouvrir_noeud("n", node_window=30)
    assert len(w.blocks) == 8 and not w.truncated and w.next_cursor is None
    assert ix.ouvrir_noeud("root", node_window=3).blocks == []
    assert [b.block_id for b in ix.ouvrir_noeud("n", cursor=4, node_window=3).blocks] == ["d:p1:4", "d:p1:5", "d:p1:6"]
    with pytest.raises(KeyError):
        ix.ouvrir_noeud("inconnu", node_window=3)
    with pytest.raises(KeyError):
        ix.ouvrir_noeud("n", focus_block_id="d:p1:99", node_window=3)
    with pytest.raises(ValueError, match="exclusifs"):
        ix.ouvrir_noeud("n", focus_block_id="d:p1:1", cursor=0, node_window=3)
    for bad in (-1, 8, 99):
        with pytest.raises(ValueError, match="cursor"):
            ix.ouvrir_noeud("n", cursor=bad, node_window=3)
    with pytest.raises(ValueError, match="node_window"):
        ix.ouvrir_noeud("inconnu", node_window=0)


def test_index_computes_missing_text_norm_and_refuses_id_collisions() -> None:
    ix = Index(Corpus(documents={"d": _doc(2)}))
    assert ix.chercher(["bloc"], limit=5) == [("d:p1:1", "n"), ("d:p1:2", "n")]
    with pytest.raises(ValueError, match="node_id 'root'"):
        Index(Corpus(documents={"d": _doc(2), "e": _doc(2, doc_id="e")}))
    # même doc_id sous deux clés du corpus ⇒ mêmes block_id
    with pytest.raises(ValueError, match="block_id 'd:p1:1'"):
        Index(Corpus(documents={"d": _doc(2), "d2": _doc(2, prefix="x-")}))


def test_chercher_whole_word_ranking_and_limit(mini_index: Index) -> None:
    hits = mini_index.chercher({"commune": ["biergercenter"], "matricule": []}, limit=20)
    # corps[2] « matricule, délivré par la commune » touche les deux termes ; il précède tout bloc à un seul terme
    assert hits[0] == ("lux-guide:farrivee:5", "lux-guide:farrivee")
    assert len(hits) <= 20 and len({b for b, _ in hits}) == len(hits)
    single = [b for b, _ in hits[1:]]
    assert "lux-guide:farrivee:2" in single and "lux-guide:farrivee:3" in single  # ordre de lecture entre ex æquo
    assert single.index("lux-guide:farrivee:2") < single.index("lux-guide:farrivee:3")
    # un seul terme : ex æquo partout, donc ordre de lecture ; `limit` tronque
    assert mini_index.chercher({"commune": []}, limit=2) == [("lux-guide:farrivee:2", "lux-guide:farrivee"),
                                                            ("lux-guide:farrivee:3", "lux-guide:farrivee")]
    # mot entier : « commun » ne touche pas « commune » ; la variante multi-mots marche sur text_norm
    assert mini_index.chercher(["commun"], limit=5) == []
    assert words("l'arrivee, c'est : ici-meme 8 jours") == ["l", "arrivee", "c", "est", "ici", "meme", "8", "jours"]
    assert mini_index.chercher({"délai": ["huit jours"]}, limit=50)
    assert mini_index.chercher(["Biergercenter"], limit=5) == [("lux-guide:farrivee:3", "lux-guide:farrivee")]
    # termes vides ⇒ []
    assert mini_index.chercher({"": []}, limit=5) == [] and mini_index.chercher([], limit=5) == []
    assert mini_index.chercher(["commune"], limit=2, doc_id="lux-guide") == mini_index.chercher(["commune"], limit=2)
    with pytest.raises(KeyError):
        mini_index.chercher(["commune"], limit=5, doc_id="autre")
    with pytest.raises(TypeError):
        mini_index.chercher("commune", limit=5)
    # deux clés de même forme normalisée ne comptent qu'une fois : « Commune » + « commune » ≠ score 2
    assert mini_index.chercher({"Commune": [], "commune": [], "matricule": []}, limit=20) == hits
    # seule une variante multi-mots touche : la clé canonique « delai legal » n'apparaît nulle part
    assert mini_index.chercher({"delai legal": ["huit jours"]}, limit=5)[0] == ("lux-guide:farrivee:3", "lux-guide:farrivee")


def test_chercher_on_real_corpus_uses_config_thresholds() -> None:
    s = Settings(_env_file=None)
    ix = Index(load_corpus(ROOT / "data", allow_ungated=True))
    hits = ix.chercher({"matricule": [], "commune": []}, limit=s.search_limit)
    assert 0 < len(hits) <= 20
    docs = ix.corpus.documents  # le corpus contient aussi le contrat AXA depuis la story 1.2
    both = [b for b, _ in hits if {"matricule", "commune"} <= set(words(docs[b.split(":")[0]].block(b).text_norm))]
    assert [b for b, _ in hits[:3]] == ["lux-guide:farrivee:2", "lux-guide:farrivee:6", "lux-guide:farrivee:11"]
    assert both[:3] == [b for b, _ in hits[:3]] and hits[0][1] == "lux-guide:farrivee"
    assert all(b in both for b, _ in hits[: len(both)])
    w = ix.ouvrir_noeud("lux-guide:farrivee", node_window=s.node_window)
    assert w.blocks[0].block_id == "lux-guide:farrivee:1"
