"""AD-1 — `sommaire`, `ouvrir_noeud` (fenêtre, curseur, bloc visé) et `chercher` (mot entier, tri, limite)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.dictionary import load_dictionary
from server.app.corpus.index import Index, reading_order, words
from server.app.corpus.loader import Corpus, load_corpus
from server.app.corpus.racine import _lecture_interne_sans_racine
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
    with _lecture_interne_sans_racine(d.parent) as lecture:
        return Index(load_corpus(d.parent, allow_ungated=True, lecture=lecture))


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


def test_pdf_summary_never_advertises_a_node_with_no_citable_block() -> None:
    """Revue 3.1 I2 : chaque nœud annoncé produit une fenêtre de blocs utile."""
    toc = Block(block_id="d:p1:1", text="Garanties .... 4", loc="p1", seq=1, page=1, kind="autre")
    clause = Block(block_id="d:p2:1", text="Garantie incendie", loc="p2", seq=1, page=2)
    document = Document(
        doc_id="d", kind="contrat", title="Contrat", edition="e", blocks=[toc, clause],
        nodes=[
            Node(node_id="d", items=[NodeRef(node_id="d:tdm"), NodeRef(node_id="d:a1")]),
            Node(node_id="d:tdm", level=1, title="Sommaire", items=[BlockRef(block_id=toc.block_id)]),
            Node(node_id="d:a1", level=1, title="Garanties", items=[BlockRef(block_id=clause.block_id)]),
        ],
    )
    from server.ingest.pdf_to_blocks import build_summary

    summary = build_summary(document)
    index = Index(Corpus(documents={"d": document}, summaries={"d": summary}))
    advertised = [line.split("`")[1] for line in summary.splitlines() if "`" in line]
    assert advertised == ["d:a1"]
    assert all(index.ouvrir_noeud(node_id, node_window=8).blocks for node_id in advertised)


def test_ouvrir_noeud_window_focus_and_cursor() -> None:
    ix = Index(Corpus(documents={"d": _doc(8)}))
    debut = ix.ouvrir_noeud("n", node_window=3)
    assert [b.block_id for b in debut.blocks] == ["d:p1:1", "d:p1:2", "d:p1:3"]
    assert debut.truncated and debut.next_cursor == 3
    milieu = ix.ouvrir_noeud("n", cursor=debut.next_cursor, node_window=3)
    assert [b.block_id for b in milieu.blocks] == ["d:p1:4", "d:p1:5", "d:p1:6"]
    assert milieu.truncated and milieu.next_cursor == 6
    fin = ix.ouvrir_noeud("n", cursor=milieu.next_cursor, node_window=3)
    assert [b.block_id for b in fin.blocks] == ["d:p1:7", "d:p1:8"]
    assert not fin.truncated and fin.next_cursor is None

    w = ix.ouvrir_noeud("n", focus_block_id="d:p1:6", node_window=3)
    assert [b.block_id for b in w.blocks] == ["d:p1:4", "d:p1:5", "d:p1:6"]
    assert w.truncated and w.next_cursor == 6 and w.node_id == "n"
    w = ix.ouvrir_noeud("n", cursor=w.next_cursor, node_window=3)
    assert [b.block_id for b in w.blocks] == ["d:p1:7", "d:p1:8"] and not w.truncated
    focus_fin = ix.ouvrir_noeud("n", focus_block_id="d:p1:8", node_window=3)
    assert focus_fin.next_cursor is None and focus_fin.truncated  # les six premiers blocs sont omis
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


def test_ouvrir_noeud_exposes_category_title_and_titled_children() -> None:
    block = Block(block_id="d:p1:1", text="Bloc", loc="p1", seq=1)
    doc = Document(
        doc_id="d", kind="guide", title="Guide", edition="e", blocks=[block],
        nodes=[Node(node_id="root", title="Catégorie", items=[NodeRef(node_id="child")]),
               Node(node_id="child", title="Fiche enfant", items=[BlockRef(block_id=block.block_id)])])
    window = Index(Corpus(documents={"d": doc})).ouvrir_noeud("root", node_window=3)
    assert window.title == "Catégorie"
    assert [(c.node_id, c.title) for c in window.children] == [("child", "Fiche enfant")]
    assert window.blocks == [] and not window.truncated


def test_autre_preliminaire_reste_dans_le_document_sans_consumer_le_rappel() -> None:
    cover = Block(block_id="d:p1:1", text="Couverture contrat", loc="p1", seq=1, kind="autre")
    clause = Block(block_id="d:p2:1", text="Garantie incendie", loc="p2", seq=1)
    doc = Document(doc_id="d", kind="contrat", title="Contrat", edition="e", blocks=[cover, clause],
                   nodes=[Node(node_id="root", items=[BlockRef(block_id=cover.block_id),
                                                      BlockRef(block_id=clause.block_id)])])
    index = Index(Corpus(documents={"d": doc}))
    assert len(index) == 2 and doc.block(cover.block_id).kind == "autre"
    assert index.chercher(["couverture"], limit=10) == []
    assert [block.block_id for block in index.ouvrir_noeud("root", node_window=10).blocks] == [clause.block_id]


def test_preliminary_and_toc_tables_are_non_citable_but_ordinary_table_remains_citable() -> None:
    preliminary = Block(block_id="d:p1:1", text="secret couverture", loc="p1", seq=1,
                        kind="table", source_field="preliminaire")
    toc = Block(block_id="d:p2:1", text="secret sommaire", loc="p2", seq=1,
                kind="table", source_field="tdm")
    ordinary = Block(block_id="d:p3:1", text="secret garantie", loc="p3", seq=1, kind="table")
    doc = Document(doc_id="d", kind="contrat", title="Contrat", edition="e",
                   blocks=[preliminary, toc, ordinary],
                   nodes=[Node(node_id="root", items=[BlockRef(block_id=block.block_id)
                                                      for block in (preliminary, toc, ordinary)])])
    index = Index(Corpus(documents={"d": doc}))
    assert index.chercher(["couverture", "sommaire"], limit=10) == []
    assert index.chercher(["garantie"], limit=10) == [(ordinary.block_id, "root")]
    assert [block.block_id for block in index.ouvrir_noeud("root", node_window=10).blocks] == [ordinary.block_id]
    assert index._block_frequencies["d"] == {"garantie": 1, "secret": 1}


def test_autre_preliminaire_does_not_change_document_frequencies_or_citable_rank() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="mot commun", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="mot rare", loc="p1", seq=2),
        Block(block_id="d:p1:3", text="encore commun", loc="p1", seq=3),
        Block(block_id="d:p1:4", text="préliminaire rare", loc="p1", seq=4, kind="autre"),
    ]
    doc = Document(doc_id="d", kind="contrat", title="Contrat", edition="e", blocks=blocks,
                   nodes=[Node(node_id="root", items=[BlockRef(block_id=block.block_id) for block in blocks])])
    index = Index(Corpus(documents={"d": doc}))
    assert len(index) == 4 and doc.block("d:p1:4").kind == "autre"
    assert index.chercher(["rare commun"], limit=3, doc_id="d")[0] == ("d:p1:2", "root")
    assert "d:p1:4" in [block.block_id for block in doc.blocks]


def test_index_computes_missing_text_norm_and_refuses_id_collisions() -> None:
    ix = Index(Corpus(documents={"d": _doc(2)}))
    assert ix.chercher(["bloc"], limit=5) == [("d:p1:1", "n"), ("d:p1:2", "n")]
    with pytest.raises(ValueError, match="node_id 'root'"):
        Index(Corpus(documents={"d": _doc(2), "e": _doc(2, doc_id="e")}))
    # même doc_id sous deux clés du corpus ⇒ mêmes block_id
    with pytest.raises(ValueError, match="block_id 'd:p1:1'"):
        Index(Corpus(documents={"d": _doc(2), "d2": _doc(2, prefix="x-")}))


def test_chercher_puts_decisional_blocks_first_at_equal_score_only() -> None:
    """Story 1.8 / D7 : `kinds_prioritaires` départage les **ex æquo**, il ne filtre ni ne surclasse.

    Trois blocs : un paragraphe qui touche les deux termes, une garantie et un paragraphe qui n'en
    touchent qu'un. À score égal, la garantie passe devant le paragraphe ; au-dessus d'elle, le bloc
    qui répond le mieux reste premier — sans quoi le typage manuel de quatre clauses (J+1) évincerait
    le rappel lexical, seul rappel réel tant que le typage automatique (3.2) n'existe pas.
    """
    blocks = [Block(block_id="d:p1:1", text="La chaleur du mobilier assuré.", loc="p1", seq=1),
              Block(block_id="d:p1:2", text="Le mobilier de jardin.", loc="p1", seq=2),
              Block(block_id="d:p1:3", text="Le mobilier assuré est garanti.", loc="p1", seq=3,
                    kind="garantie", kind_source="manual")]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    ix = Index(Corpus(documents={"d": Document(doc_id="d", kind="contrat", title="t", edition="e",
                                               nodes=nodes, blocks=blocks)}))
    termes = {"mobilier": [], "chaleur": []}
    # sans priorité : score puis ordre de lecture
    assert [b for b, _ in ix.chercher(termes, limit=5)] == ["d:p1:1", "d:p1:2", "d:p1:3"]
    # avec priorité : la garantie passe devant `d:p1:2` (même score), jamais devant `d:p1:1` (score 2)
    assert [b for b, _ in ix.chercher(termes, limit=5, kinds_prioritaires={"garantie", "exclusion"})] == [
        "d:p1:1", "d:p1:3", "d:p1:2"]
    # priorité vide ou sans effet : ordre inchangé (le guide ne passe rien)
    assert [b for b, _ in ix.chercher(termes, limit=5, kinds_prioritaires=set())] == [
        "d:p1:1", "d:p1:2", "d:p1:3"]
    # et il ne **filtre** pas : les trois blocs restent rendus
    assert len(ix.chercher(termes, limit=5, kinds_prioritaires={"garantie"})) == 3


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
    # La variante « huit jours » est pleinement couverte dès le titre « Les huit premiers jours » :
    # l'ordre et la contiguïté ne sont plus exigés (story 2.7 / R1).
    assert mini_index.chercher({"delai legal": ["huit jours"]}, limit=5)[0] == ("lux-guide:farrivee:1", "lux-guide:farrivee")


def test_chercher_score_la_couverture_complete_puis_partielle_sans_exiger_la_suite() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="La commune idéale dépend de votre choix quotidien.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Le choix dépend de vos trajets.", loc="p1", seq=2),
        Block(block_id="d:p1:3", text="Le relogement temporaire est organisé.", loc="p1", seq=3),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    ix = Index(Corpus(documents={"d": Document(doc_id="d", kind="contrat", title="t", edition="e",
                                                nodes=nodes, blocks=blocks)}))

    # R1 : les deux mots, dans n'importe quel ordre et non contigus, valent une couverture pleine.
    # R2 : un seul mot reste un hit positif, mais vient après la couverture pleine.
    assert ix.chercher(["choix commune"], limit=20) == [("d:p1:1", "n"), ("d:p1:2", "n")]
    # R3 : le mot entier reste l'unité ; « logement » n'existe pas dans « relogement ».
    assert ix.chercher(["logement"], limit=20) == []


def test_chercher_retient_la_meilleure_forme_par_canonique_et_somme_les_groupes() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Choix commune et logement.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Trouver le bon logement après le choix de la commune.", loc="p1", seq=2),
        Block(block_id="d:p1:3", text="Choix commune : quelle commune retenir ?", loc="p1", seq=3),
        Block(block_id="d:p1:4", text="Choix commune.", loc="p1", seq=4,
              kind="garantie", kind_source="manual"),
        Block(block_id="d:p1:5", text="Choix commune.", loc="p1", seq=5),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    ix = Index(Corpus(documents={"d": Document(doc_id="d", kind="guide", title="t", edition="e",
                                                nodes=nodes, blocks=blocks)}))
    termes = {"choix commune": ["quelle commune"], "recherche logement": ["trouver logement"]}

    # R4 : p1:2 couvre les deux canoniques. Les deux formes complètes du premier canonique dans
    # p1:3 ne le comptent jamais deux fois. À nombre de pleins égal, les fragments des autres groupes
    # sont ignorés : une forme composée concise précède sa présence dispersée, puis kind et ordre
    # départagent une même densité.
    hits = ix.chercher(termes, limit=4, kinds_prioritaires={"garantie"})
    assert hits == [("d:p1:2", "n"), ("d:p1:4", "n"), ("d:p1:5", "n"), ("d:p1:1", "n")]
    # À score égal, la priorité de kind précède l'ordre de lecture ; la limite reste stricte.
    assert len(hits) == 4 and ("d:p1:3", "n") not in hits


def test_chercher_un_plein_ne_peut_pas_etre_evince_par_une_somme_de_partiels() -> None:
    """Cadrage 2.7 : le rappel partiel reste un filet, jamais une priorité supérieure au plein."""
    blocks = [
        Block(block_id="d:p1:1", text="La garantie couvre les dégâts des eaux.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Animal, mobilier et fuite.", loc="p1", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    ix = Index(Corpus(documents={"d": Document(doc_id="d", kind="contrat", title="t", edition="e",
                                                nodes=nodes, blocks=blocks)}))
    termes = {
        "dégâts eaux": [],
        "animal domestique": [],
        "mobilier précieux": [],
        "fuite canalisation": [],
    }

    # p1:2 cumule trois couvertures partielles ; p1:1 satisfait un canonique en entier et reste devant.
    assert ix.chercher(termes, limit=2) == [("d:p1:1", "n"), ("d:p1:2", "n")]


def test_a_egalite_de_pleins_les_partiels_ne_departagent_pas() -> None:
    """Cadrage 2.7 : dès qu'un bloc porte un plein, ses fragments ne bonifient plus son rang."""
    blocks = [
        Block(block_id="d:p1:1", text="Responsabilité civile.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Responsabilité civile et dommage déclaré.", loc="p1", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    ix = Index(Corpus(documents={"d": Document(doc_id="d", kind="contrat", title="t", edition="e",
                                                nodes=nodes, blocks=blocks)}))

    assert ix.chercher(["responsabilité civile", "dommage accidentel"], limit=2) == [
        ("d:p1:1", "n"), ("d:p1:2", "n")]


def test_chercher_classe_dabord_le_nombre_total_de_canoniques_pleins() -> None:
    """AC 2.7 : un composé plein ne prime pas cinq canoniques simples pleins."""
    blocks = [
        Block(block_id="d:p1:1", text="impôt logement école travail santé", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="marché immobilier", loc="p1", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    ix = Index(Corpus(documents={"d": Document(doc_id="d", kind="guide", title="t", edition="e",
                                                nodes=nodes, blocks=blocks)}))

    assert ix.chercher(
        ["impôt", "logement", "école", "travail", "santé", "marché immobilier"], limit=2,
    ) == [("d:p1:1", "n"), ("d:p1:2", "n")]


def test_un_titre_plein_departage_un_paragraphe_long_plein_sur_le_corpus_reel() -> None:
    corpus = load_corpus(ROOT / "data", allow_ungated=True)
    ix = Index(corpus)
    dictionnaire = load_dictionary(ROOT / "data", corpus, "lux-guide")
    hits = ix.chercher(dictionnaire.expand(["choix de la commune"]), limit=20, doc_id="lux-guide")
    assert hits[0][0] == "lux-guide:fchoisir_commune:1"


def test_chercher_garde_la_garantie_et_lexclusion_du_cas_multiple_dans_les_cinq_premiers() -> None:
    """Cadrage 2.7 : les partiels fréquents ne noient plus les deux clauses décisionnelles."""
    s = Settings(_env_file=None)
    ix = Index(load_corpus(ROOT / "data", allow_ungated=True))
    hits = [block_id for block_id, _node_id in ix.chercher(
        {"dégâts des eaux": [], "fuite de machine": [], "mobilier": [],
         "dégâts causés par un animal": [], "mâchonnement": []},
        limit=s.search_limit,
        doc_id="axa-lu-optihome-2017",
        kinds_prioritaires={"garantie", "exclusion", "condition", "franchise"},
    )]

    assert hits.index("axa-lu-optihome-2017:p37:13") < 5
    assert hits.index("axa-lu-optihome-2017:p35:2") < 5


def test_chercher_un_mot_partiel_frequent_contribue_moins_quun_mot_rare() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Le mot commun apparaît ici.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Le mot rare apparaît ici.", loc="p1", seq=2),
        Block(block_id="d:p1:3", text="Encore le mot commun.", loc="p1", seq=3),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    ix = Index(Corpus(documents={"d": Document(doc_id="d", kind="guide", title="t", edition="e",
                                                nodes=nodes, blocks=blocks)}))

    # Les deux blocs ne couvrent qu'un mot sur deux. `rare` n'apparaît que dans un bloc, `commun`
    # dans deux : sa contribution est donc supérieure malgré son ordre de lecture plus tardif.
    assert ix.chercher(["commun rare"], limit=3) == [
        ("d:p1:2", "n"), ("d:p1:1", "n"), ("d:p1:3", "n")]


def test_chercher_reserve_un_noeud_distinct_par_groupe_prioritaire_avant_la_coupe() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Thème profil.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Thème profil répété.", loc="p2", seq=1),
        Block(block_id="d:p3:1", text="Première démarche utile.", loc="p3", seq=1),
        Block(block_id="d:p4:1", text="Seconde démarche utile.", loc="p4", seq=1),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id=f"n{i}") for i in range(1, 5)])]
    nodes.extend(Node(node_id=f"n{i}", items=[BlockRef(block_id=block.block_id)])
                 for i, block in enumerate(blocks, 1))
    ix = Index(Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)}))

    assert ix.chercher(["thème profil"], limit=2) == [("d:p1:1", "n1"), ("d:p2:1", "n2")]
    assert ix.chercher(["thème profil", "première démarche", "seconde démarche"], limit=2,
                       groupes_prioritaires=["première démarche", "seconde démarche"]) == [
        ("d:p3:1", "n3"), ("d:p4:1", "n4")]


def test_un_mot_absent_du_document_ne_change_pas_le_score_partiel() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="a seul", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="a y", loc="p1", seq=2),
        Block(block_id="d:p1:3", text="y", loc="p1", seq=3),
        Block(block_id="d:p1:4", text="y", loc="p1", seq=4),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    ix = Index(Corpus(documents={"d": Document(doc_id="d", kind="guide", title="t", edition="e",
                                                nodes=nodes, blocks=blocks)}))
    entry = next(e for e in ix._entries if e.block.block_id == "d:p1:1")
    frequencies = ix._block_frequencies["d"]

    score_absent = ix._hit(entry, frozenset({"a", "xabsent"}), frequencies)
    score_present_ailleurs = ix._hit(entry, frozenset({"a", "y"}), frequencies)
    assert score_absent == 1
    assert score_absent > score_present_ailleurs


def _def_doc() -> Document:
    blocks = [
        Block(block_id="d:p1:1", text="Le contenu désigne les meubles.", loc="p1", seq=1,
              kind="definition", defines="contenu"),
        Block(block_id="d:p1:2", text="Mobilier de jardin : tables et chaises.", loc="p1", seq=2,
              kind="definition", defines="mobilier de jardin"),
        Block(block_id="d:p1:3", text="contenu sans defines", loc="p1", seq=3, kind="definition"),
        Block(block_id="d:p1:4", text="para qui définit le contenu", loc="p1", seq=4),  # kind ≠ definition
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    return Document(doc_id="d", kind="contrat", title="t", edition="e", nodes=nodes, blocks=blocks)


def test_definitions_matches_normalized_defines_whole_word() -> None:
    ix = Index(Corpus(documents={"d": _def_doc()}))
    # terme exact, insensible à la casse et aux accents (normalize)
    assert ix.definitions(["Contenu"]) == [("d:p1:1", "n")]
    # Un terme plus précis de la question peut trouver le libellé défini qui le contient. L'inverse
    # n'est pas une preuve : une expression plus large ne demande pas automatiquement sa composante.
    assert ix.definitions(["jardin"]) == [("d:p1:2", "n")]
    assert ix.definitions(["le contenu assuré"]) == []
    assert ix.definitions(["mobilier de jardin"]) == [("d:p1:2", "n")]
    # jamais de correspondance partielle de mot, ni via le texte du bloc
    assert ix.definitions(["conten"]) == []
    assert ix.definitions(["meubles"]) == []
    # variantes d'un dict ; ordre de lecture quand plusieurs matchent
    assert ix.definitions({"biens": ["contenu", "jardin"]}) == [("d:p1:1", "n"), ("d:p1:2", "n")]
    # termes vides, doc_id filtrant ou inconnu, chaîne nue
    assert ix.definitions([]) == [] and ix.definitions({"": []}) == []
    assert ix.definitions(["contenu"], doc_id="d") == [("d:p1:1", "n")]
    with pytest.raises(KeyError):
        ix.definitions(["contenu"], doc_id="autre")
    with pytest.raises(TypeError):
        ix.definitions("contenu")


def _scoped_def_doc() -> Document:
    """Deux définitions du même terme : la commune (art. 1) et une dérogation propre à une extension."""
    blocks = [
        Block(block_id="d:p1:1", text="Le contenu désigne les meubles.", loc="p1", seq=1,
              kind="definition", defines="contenu"),
        Block(block_id="d:p9:1", text="Le vol du contenu est garanti.", loc="p9", seq=1),
        Block(block_id="d:p9:2", text="Par dérogation, le contenu comprend ici les espèces.", loc="p9", seq=2,
              kind="definition", defines="contenu", scope_node_id="d:ext", overrides="d:p1:1"),
    ]
    nodes = [Node(node_id="root", level=0, items=[NodeRef(node_id="d:defs"), NodeRef(node_id="d:ext")]),
             Node(node_id="d:defs", level=1, items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="d:ext", level=1, items=[BlockRef(block_id="d:p9:1"), BlockRef(block_id="d:p9:2")])]
    return Document(doc_id="d", kind="contrat", title="t", edition="e", nodes=nodes, blocks=blocks)


def test_definitions_follow_terms_met_in_the_opened_blocks() -> None:
    """AD-1 (revue Codex 1.4, B2) : « les définitions des termes rencontrés dans les blocs ouverts ».
    Une clause qui introduit elle-même un terme défini gardait sinon sa définition hors contexte."""
    ix = Index(Corpus(documents={"d": _def_doc()}))
    assert ix.definitions([], blocs_ouverts=["d:p1:4"]) == [("d:p1:1", "n")]  # « contenu » dans le bloc
    assert ix.definitions([], blocs_ouverts=[]) == []
    assert ix.definitions([], blocs_ouverts=["d:p1:2"]) == []  # « tables et chaises » ne définit rien
    assert ix.definitions([], blocs_ouverts=["inexistant"]) == []
    # une définition ne se déclenche jamais sur son propre texte
    assert ix.definitions([], blocs_ouverts=["d:p1:1"]) == []


def test_definitions_resolve_scope_overrides_and_the_nearest_one() -> None:
    """AD-1 « valides dans la portée » + AD-2 « la plus proche dans la portée …, puis remontée vers les
    définitions communes ; la dérogation prime dans sa portée » (revue Codex 1.4, B2)."""
    ix = Index(Corpus(documents={"d": _scoped_def_doc()}))
    # sans contexte, la définition commune (sans portée) fait foi — une seule par terme défini
    assert ix.definitions(["contenu"]) == [("d:p1:1", "d:defs")]
    # avec un bloc ouvert dans la portée de la dérogation, c'est elle qui s'applique, et elle évince
    # la définition commune qu'elle déroge (`overrides`)
    assert ix.definitions(["contenu"], blocs_ouverts=["d:p9:1"]) == [("d:p9:2", "d:ext")]
    # un bloc ouvert hors de cette portée retombe sur la commune
    assert ix.definitions(["contenu"], blocs_ouverts=["d:p1:1"]) == [("d:p1:1", "d:defs")]


def test_a_global_override_wins_even_when_both_definitions_have_no_scope() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Accident désigne un événement.", loc="p1", seq=1,
              kind="definition", defines="accident"),
        Block(block_id="d:p1:2", text="Par dérogation, accident désigne un choc soudain.",
              loc="p1", seq=2, kind="definition", defines="accident", overrides="d:p1:1"),
        Block(block_id="d:p1:3", text="La garantie vise un accident.", loc="p1", seq=3,
              kind="garantie"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]), Node(
        node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    ix = Index(Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="t", edition="e", nodes=nodes, blocks=blocks)}))

    assert ix.definitions(["accident"], blocs_ouverts=["d:p1:3"]) == [("d:p1:2", "n")]


def test_a_scoped_override_covers_the_whole_governed_branch() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Accident désigne un événement.", loc="p1", seq=1,
              kind="definition", defines="accident"),
        Block(block_id="d:p2:1", text="Par dérogation, accident désigne un choc soudain.",
              loc="p2", seq=1, kind="definition", defines="accident", scope_node_id="d:branche",
              overrides="d:p1:1"),
        Block(block_id="d:p3:1", text="La garantie vise un accident.", loc="p3", seq=1,
              kind="garantie"),
    ]
    nodes = [
        Node(node_id="root", items=[NodeRef(node_id="d:defs"), NodeRef(node_id="d:branche")]),
        Node(node_id="d:defs", items=[BlockRef(block_id="d:p1:1")]),
        Node(node_id="d:branche", items=[BlockRef(block_id="d:p2:1"), NodeRef(node_id="d:enfant")]),
        Node(node_id="d:enfant", items=[BlockRef(block_id="d:p3:1")]),
    ]
    ix = Index(Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="t", edition="e", nodes=nodes, blocks=blocks)}))

    assert ix.definitions(["accident"], blocs_ouverts=["d:p3:1"]) == [("d:p2:1", "d:branche")]


def test_a_common_definition_keeps_its_structural_owner_without_becoming_local() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Le contenu désigne les meubles confiés.", loc="p1", seq=1,
              kind="definition", kind_source="model_verified", defines="contenu"),
        Block(block_id="d:p9:1", text="Le contenu endommagé est examiné.", loc="p9", seq=1),
    ]
    nodes = [Node(node_id="root", level=0, items=[NodeRef(node_id="d:defs"),
                                                   NodeRef(node_id="d:sinistre")]),
             Node(node_id="d:defs", level=1, scope={"kind": "commun"},
                  items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="d:sinistre", level=1, scope={"kind": "special"},
                  items=[BlockRef(block_id="d:p9:1")])]
    ix = Index(Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="t", edition="e", nodes=nodes, blocks=blocks)}))

    assert ix.definitions(["contenu"], blocs_ouverts=["d:p9:1"]) == \
        [("d:p1:1", "d:defs")]


def test_definitions_are_resolved_per_opened_block_not_once_for_the_whole_result() -> None:
    """AD-2 « la plus proche dans la portée **du bloc décisionnel** » (revue Codex 1.4, B2, tour 2).

    Deux blocs ouverts de portées différentes reçoivent chacun la définition qui vaut chez lui : la
    dérogation n'évince la commune que là où elle s'applique. Une seule résolution pour tout le
    résultat ne rendait que la plus profonde, et le bloc commun perdait sa définition."""
    ix = Index(Corpus(documents={"d": _scoped_def_doc()}))
    # « contenu » vient de la question : son contexte est l'ensemble des nœuds ouverts
    assert ix.definitions(["contenu"], blocs_ouverts=["d:p1:1", "d:p9:1"]) == \
        [("d:p1:1", "d:defs"), ("d:p9:2", "d:ext")]
    # même résultat quand « contenu » n'est pas cherché mais seulement rencontré dans les deux blocs
    assert ix.definitions([], blocs_ouverts=["d:p1:1", "d:p9:1"]) == \
        [("d:p1:1", "d:defs"), ("d:p9:2", "d:ext")]
    # rencontré dans le seul bloc sous dérogation : sa portée à lui, et rien d'autre
    assert ix.definitions([], blocs_ouverts=["d:p9:1"]) == [("d:p9:2", "d:ext")]


def test_a_definition_out_of_scope_is_never_returned_when_there_is_a_context() -> None:
    """AD-1 : les définitions rendues sont « valides dans la portée ». Sans définition commune vers
    laquelle remonter, une portée qui ne couvre pas le contexte ne rend rien (revue Codex 1.4, B2)."""
    blocks = [
        Block(block_id="d:p1:1", text="Le vol du contenu est garanti.", loc="p1", seq=1),
        Block(block_id="d:p9:1", text="Le contenu désigne ici les espèces.", loc="p9", seq=1,
              kind="definition", defines="contenu", scope_node_id="d:ext"),
    ]
    nodes = [Node(node_id="root", level=0, items=[NodeRef(node_id="d:base"), NodeRef(node_id="d:ext")]),
             Node(node_id="d:base", level=1, items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="d:ext", level=1, items=[BlockRef(block_id="d:p9:1")])]
    ix = Index(Corpus(documents={"d": Document(doc_id="d", kind="contrat", title="t", edition="e",
                                               nodes=nodes, blocks=blocks)}))
    # le bloc ouvert est hors de la portée `d:ext` : la définition spéciale ne s'y applique pas
    assert ix.definitions(["contenu"], blocs_ouverts=["d:p1:1"]) == []
    # elle s'applique dans sa propre portée…
    assert ix.definitions(["contenu"], blocs_ouverts=["d:p9:1"]) == [("d:p9:1", "d:ext")]
    # … et hors pipeline (aucun bloc ouvert), aucun contexte ne peut l'invalider
    assert ix.definitions(["contenu"]) == [("d:p9:1", "d:ext")]


def test_definitions_on_real_corpus_finds_the_axa_overlay() -> None:
    ix = Index(load_corpus(ROOT / "data", allow_ungated=True))
    assert ix.definitions(["contenu"], doc_id="axa-lu-optihome-2017") == \
        [("axa-lu-optihome-2017:p9:2", "axa-lu-optihome-2017:a1.12")]
    assert ("axa-lu-optihome-2017:p11:12" in [b for b, _ in ix.definitions(["mobilier de jardin"])])
    assert ix.definitions(["matricule"]) == []  # le guide n'a aucun bloc definition


def test_real_corpus_uses_the_rc_accident_override_across_its_branch() -> None:
    ix = Index(load_corpus(ROOT / "data", allow_ungated=True))
    for opened in ["axa-lu-optihome-2017:p65:11", "axa-lu-optihome-2017:p66:10"]:
        definitions = [block_id for block_id, _node_id in ix.definitions(
            ["accident"], doc_id="axa-lu-optihome-2017",
            blocs_ouverts=[opened],
        )]
        assert "axa-lu-optihome-2017:p65:10" in definitions
        assert "axa-lu-optihome-2017:p6:4" not in definitions


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
