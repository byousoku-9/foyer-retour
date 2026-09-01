"""Matrice I/O des variantes déterministe et outils de *retrouver* (specs 1.4 et 2.6)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from server.app.config import Settings
from server.app.corpus.dictionary import Dictionnaire, load_dictionary
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.domain import (
    Block,
    BlockRef,
    Document,
    FullContextSelection,
    Node,
    NodeRef,
    RetrievalBudget,
    RetrievalResult,
)
from server.app.domain.errors import BudgetExceeded
from server.app.domain.question import ParsedQuestion, QuestionScope
from server.app.domain.verdict import KINDS_DECISIONNELS, KINDS_FONDATEURS
from server.app.llm.models import STEP_TIERS, TIERS
from server.app.llm.client import LlmClient
from server.app.llm.budget import RequestBudget
from server.app.llm.pricing import estimate_tokens
from server.app.pipelines.commun import retrieval_budget
from server.app.steps.retrouver import (
    OUTILS_RECHERCHE,
    retrouver_deterministe,
    retrouver_full_context,
    retrouver_outils,
)
from tests.llm_fake import FakeAnthropic, fake_message

ROOT = Path(__file__).resolve().parents[1]


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def _budget(**kw) -> RetrievalBudget:
    base = {"max_opens": 6, "node_window": 30, "search_limit": 20, "max_llm_turns": 2}
    return RetrievalBudget(**(base | kw))


def _parsed(terms: list[str], themes: list[str] | None = None,
            noeuds: list[str] | None = None,
            facettes: list[str] | None = None) -> ParsedQuestion:
    return ParsedQuestion(question_resolue="q", intent="question", terms=terms,
                          scope=QuestionScope(themes=themes or [], noeuds=noeuds or []),
                          facettes=facettes or [])


def _run(parsed: ParsedQuestion, corpus: Corpus, index: Index, budget: RetrievalBudget | None = None,
         *, settings: Settings | None = None, doc_id: str | None = None,
         dictionnaire: Dictionnaire | None = None):
    return retrouver_deterministe(parsed, corpus=corpus, index=index, budget=budget or _budget(),
                                  settings=settings or _s(), doc_id=doc_id,
                                  dictionnaire=dictionnaire)


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
    nodes = [Node(node_id="root", title="Sommaire", items=[
                 NodeRef(node_id="n1"), NodeRef(node_id="n2"), NodeRef(node_id="n3")]),
             Node(node_id="n1", title="Arrivée", items=[
                 BlockRef(block_id="d:p1:1"), BlockRef(block_id="d:p1:2")]),
             Node(node_id="n2", title="Démarches", items=[BlockRef(block_id="d:p2:1")]),
             Node(node_id="n3", title="Définitions", items=[
                 BlockRef(block_id="d:p3:1"), BlockRef(block_id="d:p1:5")])]
    doc = Document(doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)
    return Corpus(documents={"d": doc}, summaries={"d": "root\n  n1\n  n2\n  n3"})


def _linear_doc(n_blocks: int, doc_id: str = "d", prefix: str = "") -> Document:
    blocks = [Block(block_id=f"{doc_id}:p1:{i}", text=f"Bloc {i}", loc="p1", seq=i)
              for i in range(1, n_blocks + 1)]
    nodes = [Node(node_id=f"{prefix}root", items=[NodeRef(node_id=f"{prefix}n")]),
             Node(node_id=f"{prefix}n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    return Document(doc_id=doc_id, kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)


def _tool(name: str, tool_id: str, **args: object) -> dict[str, object]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


def _tool_message(*uses: dict[str, object], stop_reason: str = "tool_use") -> dict[str, object]:
    return fake_message(model="claude-sonnet-5", stop_reason=stop_reason, content=list(uses))


@pytest.mark.parametrize("max_llm_turns", [0, 3])
def test_retrieval_budget_enforces_the_tool_turn_invariant_for_direct_callers(
        max_llm_turns: int) -> None:
    with pytest.raises(ValidationError, match="max_llm_turns"):
        _budget(max_llm_turns=max_llm_turns)


async def _run_outils(script: list[dict[str, object]], *, corpus: Corpus | None = None,
                       parsed: ParsedQuestion | None = None, budget: RetrievalBudget | None = None,
                       settings: Settings | None = None, dictionnaire=None,
                       kinds_suffisants: frozenset[str] | None = None):
    corpus = corpus or _corpus()
    settings = settings or _s(max_cost_eur_per_request=1.0)
    fake = FakeAnthropic(script)
    client = LlmClient(settings, anthropic_client=fake)
    request_budget = RequestBudget(deadline_s=30, max_attempts=8, max_cost_eur=1.0)
    result, step = await retrouver_outils(
        parsed or _parsed(["matricule"]), corpus=corpus, index=Index(corpus),
        budget=budget or _budget(max_blocks=30, max_tokens=6000), settings=settings,
        client=client, request_budget=request_budget, doc_id="d", dictionnaire=dictionnaire,
        kinds_suffisants=kinds_suffisants)
    return result, step, fake, request_budget


def _corpus_contexte_puis_regle() -> Corpus:
    blocks = [
        Block(block_id="d:p1:1", text="Titre de contexte", loc="p1", seq=1,
              kind="heading"),
        Block(block_id="d:p1:2", text="Alpha désigne le contexte initial.", loc="p1", seq=2,
              kind="definition", defines="alpha"),
        Block(block_id="d:p2:1", text="La règle utile décide le cas.", loc="p2", seq=1,
              kind="garantie"),
    ]
    nodes = [
        Node(node_id="root", items=[NodeRef(node_id="contexte"), NodeRef(node_id="regle")]),
        Node(node_id="contexte", items=[
            BlockRef(block_id="d:p1:1"), BlockRef(block_id="d:p1:2")]),
        Node(node_id="regle", items=[BlockRef(block_id="d:p2:1")]),
    ]
    return Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > contexte, regle"})


def _corpus_auxiliaire_puis_fondatrice(
        kind_fondateur: str, *, permute: bool = False,
        hit_auxiliaire_seul: bool = False) -> tuple[Corpus, str, str]:
    """Même relation métier sous deux identités et ordres documentaires synthétiques."""
    page_aux, page_fondatrice = (("p8", "p3") if permute else ("p1", "p2"))
    auxiliaire_id = f"d:{page_aux}:7"
    fondatrice_id = f"d:{page_fondatrice}:4"
    texte_auxiliaire = ("Le signal de rappel apparaît dans le dossier auxiliaire."
                        if hit_auxiliaire_seul
                        else "La règle utile dépend d'une formalité déclarée.")
    texte_fondateur = ("Le signal de rappel gouverne la règle applicable."
                       if hit_auxiliaire_seul
                       else "La règle utile régit la situation décrite.")
    auxiliaire = Block(
        block_id=auxiliaire_id, text=texte_auxiliaire,
        loc=page_aux, seq=7, kind="condition", kind_source="manual")
    fondatrice = Block(
        block_id=fondatrice_id, text=texte_fondateur,
        loc=page_fondatrice, seq=4, kind=kind_fondateur, kind_source="manual")
    noms = (("branche-z", auxiliaire, "branche-a", fondatrice) if permute
            else ("branche-a", auxiliaire, "branche-z", fondatrice))
    noeuds = [
        Node(node_id="root", items=[NodeRef(node_id=noms[0]), NodeRef(node_id=noms[2])]),
        Node(node_id=noms[0], items=[BlockRef(block_id=noms[1].block_id)]),
        Node(node_id=noms[2], items=[BlockRef(block_id=noms[3].block_id)]),
    ]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="Texte synthétique", edition="e",
        nodes=noeuds, blocks=[auxiliaire, fondatrice])},
        summaries={"d": "root > deux branches"})
    return corpus, auxiliaire_id, fondatrice_id


def _corpus_neutre_par_noeuds(*contenu: tuple[str, list[Block]]) -> Corpus:
    """Petit corpus contractuel dont seul l'ordre explicite des nœuds et blocs fait autorité."""
    nodes = [Node(node_id="root", items=[NodeRef(node_id=node_id) for node_id, _blocks in contenu])]
    blocks: list[Block] = []
    for node_id, node_blocks in contenu:
        blocks.extend(node_blocks)
        nodes.append(Node(node_id=node_id, items=[
            BlockRef(block_id=block.block_id) for block in node_blocks]))
    return Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="Texte synthétique", edition="e",
        nodes=nodes, blocks=blocks)}, summaries={"d": "root > branches neutres"})


def _corpus_fondatrices_classees(
        ordre: tuple[str, ...] = ("avant", "initiale", "apres"),
        *, initiale_refs: list[str] | None = None) -> tuple[Corpus, dict[str, Block]]:
    fondatrices = {
        "avant": Block(
            block_id="d:p1:1", text="Signal partagé règle alpha.", loc="p1", seq=1,
            kind="garantie", kind_source="manual"),
        "initiale": Block(
            block_id="d:p2:1", text="Signal partagé règle beta.", loc="p2", seq=1,
            kind="garantie", kind_source="manual", refs=initiale_refs or []),
        "apres": Block(
            block_id="d:p3:1", text="Signal partagé règle gamma.", loc="p3", seq=1,
            kind="exclusion", kind_source="manual"),
    }
    return _corpus_neutre_par_noeuds(*(
        (nom, [fondatrices[nom]]) for nom in ordre
    )), fondatrices


def _corpus_hits_bruts_a_fondatrice_tardive(
        *, kind_source: str = "manual") -> tuple[Corpus, list[Block]]:
    candidats = [
        Block(
            block_id="d:p1:1", text="Signalbrut signalbrut signalbrut signalbrut auxiliaire.",
            loc="p1", seq=1, kind="condition", kind_source="manual"),
        Block(
            block_id="d:p2:1", text="Signalbrut signalbrut signalbrut auxiliaire.",
            loc="p2", seq=1, kind="definition", kind_source="manual", defines="terme neutre"),
        Block(
            block_id="d:p3:1", text="Signalbrut signalbrut auxiliaire.",
            loc="p3", seq=1, kind="para"),
        Block(
            block_id="d:p4:1", text="Signalbrut règle décisionnelle.",
            loc="p4", seq=1, kind="garantie", kind_source=kind_source),
    ]
    corpus = _corpus_neutre_par_noeuds(*(
        (f"branche-{rang}", [candidat])
        for rang, candidat in enumerate(candidats, start=1)
    ))
    return corpus, candidats


async def test_outils_priorise_une_fondatrice_confirmee_parmi_les_hits_bruts_sous_la_derniere_ouverture(
        ) -> None:
    corpus, candidats = _corpus_hits_bruts_a_fondatrice_tardive()
    assert [block_id for block_id, _node_id in Index(corpus).chercher(
        ["signalbrut"], limit=4, doc_id="d")] == [
            candidat.block_id for candidat in candidats]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["signalbrut"]),
            _tool("ouvrir_noeud", "t2", node_id="branche-1",
                  focus_block_id=candidats[0].block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["signalbrut"]),
        budget=_budget(max_opens=2, node_window=1, search_limit=4,
                       max_blocks=2, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [candidats[0].block_id, candidats[3].block_id]
    assert result.discarded_block_ids == [
        candidats[1].block_id, candidats[2].block_id]


async def test_outils_garde_tous_les_hits_bruts_quand_la_capacite_est_abondante(
        ) -> None:
    corpus, candidats = _corpus_hits_bruts_a_fondatrice_tardive()

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["signalbrut"]),
            _tool("ouvrir_noeud", "t2", node_id="branche-1",
                  focus_block_id=candidats[0].block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["signalbrut"]),
        budget=_budget(max_opens=4, node_window=1, search_limit=4,
                       max_blocks=4, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [candidat.block_id for candidat in candidats]
    assert result.discarded_block_ids == []
    assert result.truncated is False


async def test_outils_compte_une_ouverture_par_focus_meme_dans_un_noeud_partage(
        ) -> None:
    candidats = [
        Block(block_id="d:p1:1", text="Signalouverture signalouverture initial.",
              loc="p1", seq=1, kind="condition", kind_source="manual"),
        Block(block_id="d:p2:1", text="Signalouverture signalouverture auxiliaire alpha.",
              loc="p2", seq=1, kind="definition", kind_source="manual",
              defines="terme alpha"),
        Block(block_id="d:p2:2", text="Signalouverture auxiliaire beta.",
              loc="p2", seq=2, kind="para"),
        Block(block_id="d:p4:1", text="Signalouverture règle décisionnelle.",
              loc="p4", seq=1, kind="garantie", kind_source="manual"),
    ]
    corpus = _corpus_neutre_par_noeuds(
        ("initiale", [candidats[0]]),
        ("auxiliaires-partagees", candidats[1:3]),
        ("fondatrice", [candidats[3]]),
    )
    assert [block_id for block_id, _node_id in Index(corpus).chercher(
        ["signalouverture"], limit=4, doc_id="d")] == [
            candidat.block_id for candidat in candidats]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["signalouverture"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=candidats[0].block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["signalouverture"]),
        budget=_budget(max_opens=3, node_window=1, search_limit=4,
                       max_blocks=5, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        candidats[0].block_id, candidats[3].block_id, candidats[1].block_id]
    assert result.discarded_block_ids == [candidats[2].block_id]


async def test_outils_compte_tous_les_blocs_de_la_fenetre_auxiliaire(
        ) -> None:
    initiale = Block(
        block_id="d:p1:1", text="Signalquota signalquota signalquota initial.",
        loc="p1", seq=1, kind="condition", kind_source="manual")
    auxiliaire = Block(
        block_id="d:p2:1", text="Signalquota signalquota auxiliaire.",
        loc="p2", seq=1, kind="condition", kind_source="manual")
    compagnon = Block(
        block_id="d:p2:2", text="Compagnon neutre de la fenêtre auxiliaire.",
        loc="p2", seq=2, kind="para")
    fondatrice = Block(
        block_id="d:p4:1", text="Signalquota règle décisionnelle.",
        loc="p4", seq=1, kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("initiale", [initiale]),
        ("fenetre-auxiliaire", [auxiliaire, compagnon]),
        ("fondatrice", [fondatrice]),
    )
    assert [block_id for block_id, _node_id in Index(corpus).chercher(
        ["signalquota"], limit=3, doc_id="d")] == [
            initiale.block_id, auxiliaire.block_id, fondatrice.block_id]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["signalquota"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=initiale.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["signalquota"]),
        budget=_budget(max_opens=3, node_window=2, search_limit=3,
                       max_blocks=3, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        initiale.block_id, fondatrice.block_id, auxiliaire.block_id]
    assert result.discarded_block_ids == []


async def test_outils_garde_les_hits_qui_tiennent_apres_la_priorite_fondatrice(
        ) -> None:
    corpus, candidats = _corpus_hits_bruts_a_fondatrice_tardive()

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["signalbrut"]),
            _tool("ouvrir_noeud", "t2", node_id="branche-1",
                  focus_block_id=candidats[0].block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["signalbrut"]),
        budget=_budget(max_opens=3, node_window=1, search_limit=4,
                       max_blocks=3, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        candidats[0].block_id, candidats[3].block_id, candidats[1].block_id]
    assert result.discarded_block_ids == [candidats[2].block_id]
    assert result.truncated is False


async def test_outils_priorise_le_focus_fondateur_dans_sa_fenetre_sous_la_derniere_place(
        ) -> None:
    initiale = Block(
        block_id="d:p1:1", text="Signalfenetre signalfenetre signalfenetre initial.",
        loc="p1", seq=1, kind="condition", kind_source="manual")
    auxiliaire = Block(
        block_id="d:p2:1", text="Signalfenetre signalfenetre auxiliaire.",
        loc="p2", seq=1, kind="condition", kind_source="manual")
    fondatrice = Block(
        block_id="d:p2:2", text="Signalfenetre règle décisionnelle.",
        loc="p2", seq=2, kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("initiale", [initiale]), ("fenetre-partagee", [auxiliaire, fondatrice]))
    assert [block_id for block_id, _node_id in Index(corpus).chercher(
        ["signalfenetre"], limit=3, doc_id="d")] == [
            initiale.block_id, auxiliaire.block_id, fondatrice.block_id]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["signalfenetre"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=initiale.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["signalfenetre"]),
        budget=_budget(max_opens=2, node_window=2, search_limit=3,
                       max_blocks=2, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [initiale.block_id, fondatrice.block_id]
    assert result.discarded_block_ids == [auxiliaire.block_id]

    result_complet, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["signalfenetre"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=initiale.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["signalfenetre"]),
        budget=_budget(max_opens=2, node_window=2, search_limit=3,
                       max_blocks=3, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result_complet.opened_block_ids == [
        initiale.block_id, auxiliaire.block_id, fondatrice.block_id]


async def test_outils_ne_priorise_pas_un_kind_fondateur_non_confirme_parmi_les_hits_bruts(
        ) -> None:
    corpus, candidats = _corpus_hits_bruts_a_fondatrice_tardive(kind_source="model")

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["signalbrut"]),
            _tool("ouvrir_noeud", "t2", node_id="branche-1",
                  focus_block_id=candidats[0].block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["signalbrut"]),
        budget=_budget(max_opens=2, node_window=1, search_limit=4,
                       max_blocks=2, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [candidats[0].block_id, candidats[1].block_id]
    assert candidats[3].block_id not in result.opened_block_ids


async def test_outils_garde_lordre_des_hits_bruts_sans_besoin_declare() -> None:
    corpus, candidats = _corpus_hits_bruts_a_fondatrice_tardive()

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["signalbrut"]),
            _tool("ouvrir_noeud", "t2", node_id="branche-1",
                  focus_block_id=candidats[0].block_id)),
    ], corpus=corpus, parsed=_parsed(["signalbrut"]),
        budget=_budget(max_opens=2, node_window=1, search_limit=4,
                       max_blocks=2, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0))

    assert result.opened_block_ids == [candidats[0].block_id]
    assert result.discarded_block_ids == [
        candidats[1].block_id, candidats[2].block_id, candidats[3].block_id]


async def test_outils_ne_cree_aucune_ouverture_quand_le_quota_est_epuise() -> None:
    corpus, candidats = _corpus_hits_bruts_a_fondatrice_tardive()

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["signalbrut"]),
            _tool("ouvrir_noeud", "t2", node_id="branche-1",
                  focus_block_id=candidats[0].block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["signalbrut"]),
        budget=_budget(max_opens=1, node_window=1, search_limit=4,
                       max_blocks=2, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [candidats[0].block_id]
    assert candidats[3].block_id not in result.opened_block_ids
    assert result.truncated is True


@pytest.mark.parametrize("ordre", [
    ("avant", "initiale", "apres"),
    ("initiale", "avant", "apres"),
])
async def test_outils_rejoue_les_fondatrices_autour_du_doublon_initial(
        ordre: tuple[str, ...]) -> None:
    corpus, fondatrices = _corpus_fondatrices_classees(ordre)
    classement = Index(corpus).chercher(
        ["signal partagé"], limit=3, doc_id="d", kinds_confirmes=KINDS_FONDATEURS)
    assert [block_id for block_id, _node_id in classement] == [
        fondatrices[nom].block_id for nom in ordre]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["beta"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=fondatrices["initiale"].block_id)),
    ], corpus=corpus,
        parsed=_parsed(["beta"], facettes=["signal partagé", "branche distincte"]),
        budget=_budget(max_opens=3, node_window=1, search_limit=2,
                       max_blocks=3, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        fondatrices["initiale"].block_id,
        fondatrices["avant"].block_id,
        fondatrices["apres"].block_id,
    ]
    assert result.truncated is False


async def test_outils_filtre_le_doublon_avant_la_coupe_sans_recreer_de_capacite() -> None:
    corpus, fondatrices = _corpus_fondatrices_classees(
        ("initiale", "avant", "apres"))

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["beta"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=fondatrices["initiale"].block_id)),
    ], corpus=corpus,
        parsed=_parsed(["beta"], facettes=["signal partagé", "branche distincte"]),
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=3, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        fondatrices["initiale"].block_id,
        fondatrices["avant"].block_id,
    ]
    assert fondatrices["apres"].block_id not in result.opened_block_ids
    assert result.truncated is False


async def test_outils_filtre_un_focus_deja_tente_avant_la_coupe_globale() -> None:
    tentee = Block(
        block_id="d:p1:1", text="Signal partagé règle alpha.", loc="p1", seq=1,
        kind="garantie", kind_source="manual", refs=["d:p4:1", "d:p5:1"])
    initiale = Block(
        block_id="d:p2:1", text="Signal partagé règle beta.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    suivante = Block(
        block_id="d:p3:1", text="Signal partagé règle gamma.", loc="p3", seq=1,
        kind="exclusion", kind_source="manual")
    dependance_1 = Block(
        block_id="d:p4:1", text="Première dépendance directe.", loc="p4", seq=1,
        kind="condition", kind_source="manual")
    dependance_2 = Block(
        block_id="d:p5:1", text="Seconde dépendance directe.", loc="p5", seq=1,
        kind="condition", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("tentee", [tentee]), ("initiale", [initiale]), ("suivante", [suivante]),
        ("dependance-1", [dependance_1]), ("dependance-2", [dependance_2]))
    assert [block_id for block_id, _node_id in Index(corpus).chercher(
        ["signal partagé"], limit=3, doc_id="d", kinds_confirmes=KINDS_FONDATEURS
    )] == [tentee.block_id, initiale.block_id, suivante.block_id]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["alpha"]),
            _tool("chercher", "t2", termes=["beta"]),
            _tool("ouvrir_noeud", "t3", node_id="tentee", focus_block_id=tentee.block_id),
            _tool("ouvrir_noeud", "t4", node_id="initiale",
                  focus_block_id=initiale.block_id)),
    ], corpus=corpus,
        parsed=_parsed(["beta"], facettes=["signal partagé", "branche distincte"]),
        budget=_budget(max_opens=3, node_window=1, search_limit=1,
                       max_blocks=2, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [initiale.block_id, suivante.block_id]
    assert tentee.block_id in result.discarded_block_ids
    assert result.truncated is True


async def test_outils_priorise_les_facettes_effectives_et_leur_expansion_avant_la_coupe(
        ) -> None:
    initiale = Block(
        block_id="d:p1:1", text="Repère beta règle initiale.", loc="p1", seq=1,
        kind="garantie", kind_source="manual")
    faible = Block(
        block_id="d:p2:1", text="Repère beta règle secondaire.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    premiere = Block(
        block_id="d:p3:1", text="Première facette règle utile.", loc="p3", seq=1,
        kind="exclusion", kind_source="manual")
    seconde = Block(
        block_id="d:p4:1", text="Variante validée règle utile.", loc="p4", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("initiale", [initiale]), ("faible", [faible]),
        ("premiere", [premiere]), ("seconde", [seconde]))
    dictionnaire = Dictionnaire(
        charge=True, corpus_ok=True, doc_id="d",
        _groupes={"seconde facette": ("seconde facette", "variante validee")},
        _canoniques={"seconde facette": ("seconde facette",)})

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["beta"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=initiale.block_id)),
    ], corpus=corpus, dictionnaire=dictionnaire,
        parsed=_parsed(["beta"], facettes=[
            "  première facette  ", "première facette", " ",
            " seconde facette ", "seconde facette",
        ]),
        budget=_budget(max_opens=3, node_window=1, search_limit=2,
                       max_blocks=3, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [initiale.block_id, premiere.block_id, seconde.block_id]
    assert faible.block_id not in result.opened_block_ids
    assert result.truncated is False


@pytest.mark.parametrize(("facettes", "kinds_suffisants"), [
    (["signal partagé"], KINDS_FONDATEURS),
    (["signal partagé", "branche distincte"], None),
])
async def test_outils_preserve_larret_historique_hors_replay_multifacette(
        facettes: list[str], kinds_suffisants: frozenset[str] | None) -> None:
    corpus, fondatrices = _corpus_fondatrices_classees(
        ("initiale", "avant", "apres"))

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["beta"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=fondatrices["initiale"].block_id)),
    ], corpus=corpus, parsed=_parsed(["beta"], facettes=facettes),
        budget=_budget(max_opens=3, node_window=1, search_limit=2,
                       max_blocks=3, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=kinds_suffisants)

    assert result.opened_block_ids == [fondatrices["initiale"].block_id]
    assert result.truncated is False


async def test_outils_remplace_dynamiquement_un_candidat_admis_par_la_fenetre_precedente(
        ) -> None:
    initiale = Block(
        block_id="d:p1:1", text="Repère beta règle initiale.", loc="p1", seq=1,
        kind="garantie", kind_source="manual")
    premiere = Block(
        block_id="d:p2:1", text="Signal commun règle alpha.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    voisine = Block(
        block_id="d:p2:2", text="Signal commun règle delta.", loc="p2", seq=2,
        kind="exclusion", kind_source="manual")
    suivante = Block(
        block_id="d:p3:1", text="Signal commun règle gamma.", loc="p3", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("initiale", [initiale]), ("groupe", [premiere, voisine]),
        ("suivante", [suivante]))
    assert [block_id for block_id, _node_id in Index(corpus).chercher(
        ["signal commun"], limit=3, doc_id="d", kinds_confirmes=KINDS_FONDATEURS
    )] == [premiere.block_id, voisine.block_id, suivante.block_id]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["beta"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=initiale.block_id)),
    ], corpus=corpus,
        parsed=_parsed(["beta"], facettes=["signal commun", "branche distincte"]),
        budget=_budget(max_opens=3, node_window=2, search_limit=2,
                       max_blocks=4, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        initiale.block_id, premiere.block_id, voisine.block_id, suivante.block_id]
    assert result.truncated is False


async def test_outils_garde_atomique_une_limite_classee_apres_sa_garantie() -> None:
    initiale = Block(
        block_id="d:p1:1", text="Repère gamma règle initiale.", loc="p1", seq=1,
        kind="garantie", kind_source="manual")
    garantie = Block(
        block_id="d:p2:1", text="Alphaunique fondement.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    limite = Block(
        block_id="d:p3:1", text="Betaunique restriction.", loc="p3", seq=1,
        kind="exclusion", kind_source="manual",
        relation={"exception_de": garantie.block_id})
    corpus = _corpus_neutre_par_noeuds(
        ("initiale", [initiale]), ("garantie", [garantie]), ("limite", [limite]))
    dictionnaire = Dictionnaire(
        charge=True, corpus_ok=True, doc_id="d",
        _groupes={
            "facette relationnelle": (
                "facette relationnelle", "alphaunique", "betaunique"),
        },
        _canoniques={"facette relationnelle": ("facette relationnelle",)})
    assert [block_id for block_id, _node_id in Index(corpus).chercher(
        dictionnaire.expand(["facette relationnelle"]), limit=2, doc_id="d",
        kinds_confirmes=KINDS_FONDATEURS,
    )] == [garantie.block_id, limite.block_id]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["gamma"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=initiale.block_id)),
    ], corpus=corpus, dictionnaire=dictionnaire,
        parsed=_parsed(["gamma"], facettes=[
            "facette relationnelle", "branche distincte",
        ]),
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [initiale.block_id]
    assert garantie.block_id not in result.opened_block_ids
    assert limite.block_id not in result.opened_block_ids
    assert result.truncated is True


async def test_outils_ne_rejoue_pas_les_facettes_sans_appel_outil_chercher(
        ) -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Repère beta contexte.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    fondatrice = Block(
        block_id="d:p2:1", text="Première facette règle utile.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("auxiliaire", [auxiliaire]), ("fondatrice", [fondatrice]))

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool(
            "ouvrir_noeud", "t1", node_id="auxiliaire",
            focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus,
        parsed=_parsed(["beta"], facettes=["première facette", "branche distincte"]),
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire.block_id]
    assert fondatrice.block_id not in result.opened_block_ids


async def test_outils_parcourt_les_sources_faibles_apres_les_facettes() -> None:
    initiale = Block(
        block_id="d:p1:1", text="Repère beta règle initiale.", loc="p1", seq=1,
        kind="garantie", kind_source="manual")
    faible = Block(
        block_id="d:p2:1", text="Repère beta règle secondaire.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    forte = Block(
        block_id="d:p3:1", text="Première facette règle utile.", loc="p3", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("initiale", [initiale]), ("faible", [faible]), ("forte", [forte]))

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["beta"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=initiale.block_id)),
    ], corpus=corpus,
        parsed=_parsed(["beta"], facettes=["première facette", "branche distincte"]),
        budget=_budget(max_opens=3, node_window=1, search_limit=2,
                       max_blocks=3, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [initiale.block_id, forte.block_id, faible.block_id]
    assert result.truncated is False


async def test_outils_preserve_le_rappel_auxiliaire_admis_avec_une_fondatrice() -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Entrée commune pontunique.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    initiale = Block(
        block_id="d:p2:1", text="Entrée commune règle initiale.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    suivante = Block(
        block_id="d:p3:1", text="Pontunique règle suivante.", loc="p3", seq=1,
        kind="exclusion", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("auxiliaire", [auxiliaire]), ("initiale", [initiale]),
        ("suivante", [suivante]))

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["entrée commune"]),
            _tool("ouvrir_noeud", "t2", node_id="auxiliaire",
                  focus_block_id=auxiliaire.block_id),
            _tool("ouvrir_noeud", "t3", node_id="initiale",
                  focus_block_id=initiale.block_id)),
    ], corpus=corpus, parsed=_parsed(["entrée commune"], facettes=[]),
        budget=_budget(max_opens=3, node_window=1, search_limit=2,
                       max_blocks=3, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        auxiliaire.block_id, initiale.block_id, suivante.block_id]
    assert result.truncated is False


async def test_outils_classe_chaque_source_une_fois_par_fusion(
        monkeypatch: pytest.MonkeyPatch) -> None:
    initiale = Block(
        block_id="d:p1:1", text="Repère beta règle initiale.", loc="p1", seq=1,
        kind="garantie", kind_source="manual")
    premiere = Block(
        block_id="d:p2:1", text="Première facette règle utile.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    seconde = Block(
        block_id="d:p3:1", text="Seconde facette règle utile.", loc="p3", seq=1,
        kind="exclusion", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("initiale", [initiale]), ("premiere", [premiere]), ("seconde", [seconde]))
    original = Index.chercher
    appels = {("première facette",): 0, ("seconde facette",): 0}

    def compter(self: Index, mapping, *args, **kwargs):
        cle = tuple(mapping) if isinstance(mapping, list) else None
        if cle in appels:
            appels[cle] += 1
        return original(self, mapping, *args, **kwargs)

    monkeypatch.setattr(Index, "chercher", compter)
    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["beta"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=initiale.block_id)),
    ], corpus=corpus,
        parsed=_parsed(["beta"], facettes=["première facette", "seconde facette"]),
        budget=_budget(max_opens=1, node_window=1, search_limit=3,
                       max_blocks=3, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [initiale.block_id]
    assert appels == {("première facette",): 1, ("seconde facette",): 1}
    assert result.truncated is False


@pytest.mark.parametrize("avec_source_distincte", [False, True])
async def test_outils_dedoublonne_les_sources_de_facettes_apres_expansion(
        avec_source_distincte: bool) -> None:
    initiale = Block(
        block_id="d:p1:1", text="Repère beta règle initiale.", loc="p1", seq=1,
        kind="garantie", kind_source="manual")
    premiere = Block(
        block_id="d:p2:1", text="Groupe commun règle alpha.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    doublon = Block(
        block_id="d:p3:1", text="Groupe commun règle delta.", loc="p3", seq=1,
        kind="exclusion", kind_source="manual")
    distincte = Block(
        block_id="d:p4:1", text="Facette tierce règle gamma.", loc="p4", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("initiale", [initiale]), ("premiere", [premiere]),
        ("doublon", [doublon]), ("distincte", [distincte]))
    dictionnaire = Dictionnaire(
        charge=True, corpus_ok=True, doc_id="d",
        _groupes={
            "groupe commun": ("groupe commun", "alias commun"),
            "alias commun": ("groupe commun", "alias commun"),
        },
        _canoniques={
            "groupe commun": ("groupe commun",),
            "alias commun": ("groupe commun",),
        })
    facettes = ["groupe commun", "alias commun"]
    if avec_source_distincte:
        facettes.append("facette tierce")

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["beta"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=initiale.block_id)),
    ], corpus=corpus, dictionnaire=dictionnaire,
        parsed=_parsed(["beta"], facettes=facettes),
        budget=_budget(max_opens=3, node_window=1, search_limit=2,
                       max_blocks=3, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    attendu = ([initiale.block_id, premiere.block_id, distincte.block_id]
               if avec_source_distincte else [initiale.block_id])
    assert result.opened_block_ids == attendu
    assert doublon.block_id not in result.opened_block_ids
    assert result.truncated is False


async def test_outils_reduit_les_facettes_brutes_vides_et_repetes_a_une_seule_source(
        ) -> None:
    corpus, fondatrices = _corpus_fondatrices_classees(
        ("initiale", "avant", "apres"))

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["beta"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=fondatrices["initiale"].block_id)),
    ], corpus=corpus,
        parsed=_parsed(["beta"], facettes=[
            "  signal partagé  ", "signal partagé", " ", "signal   partagé",
        ]),
        budget=_budget(max_opens=3, node_window=1, search_limit=2,
                       max_blocks=3, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [fondatrices["initiale"].block_id]
    assert result.truncated is False


async def test_outils_nactive_pas_le_replay_multifacette_sans_recherche_outils_reelle(
        ) -> None:
    corpus, fondatrices = _corpus_fondatrices_classees(
        ("initiale", "avant", "apres"))

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool(
            "ouvrir_noeud", "t1", node_id="initiale",
            focus_block_id=fondatrices["initiale"].block_id)),
    ], corpus=corpus,
        parsed=_parsed(["beta"], facettes=["signal partagé", "branche distincte"]),
        budget=_budget(max_opens=3, node_window=1, search_limit=2,
                       max_blocks=3, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [fondatrices["initiale"].block_id]
    assert result.truncated is False


@pytest.mark.parametrize(("ordre_mecanismes", "variante_attendue"), [
    ("dictionnaire,faq,sommaire,outils", True),
    ("outils,sommaire,dictionnaire,faq", False),
])
async def test_outils_rejoue_une_variante_selon_letat_du_dictionnaire_au_tour_outils(
        ordre_mecanismes: str, variante_attendue: bool) -> None:
    initiale = Block(
        block_id="d:p1:1", text="Repère beta règle initiale.", loc="p1", seq=1,
        kind="garantie", kind_source="manual")
    variante = Block(
        block_id="d:p2:1", text="Variante validée règle suivante.", loc="p2", seq=1,
        kind="exclusion", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(("initiale", [initiale]), ("variante", [variante]))
    dictionnaire = Dictionnaire(
        charge=True, corpus_ok=True, doc_id="d",
        _groupes={"seconde facette": ("seconde facette", "variante validee")},
        _canoniques={"seconde facette": ("seconde facette",)})

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["beta"]),
            _tool("ouvrir_noeud", "t2", node_id="initiale",
                  focus_block_id=initiale.block_id)),
    ], corpus=corpus, dictionnaire=dictionnaire,
        parsed=_parsed(["beta"], facettes=["branche distincte", "seconde facette"]),
        budget=_budget(max_opens=2, node_window=1, search_limit=1,
                       max_blocks=2, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0, limite_liee_max=0,
                    retrieval_mechanism_order=ordre_mecanismes),
        kinds_suffisants=KINDS_FONDATEURS)

    attendu = [initiale.block_id, variante.block_id] if variante_attendue else [initiale.block_id]
    assert result.opened_block_ids == attendu
    assert result.truncated is False


@pytest.mark.parametrize(("kind_fondateur", "permute"), [
    ("garantie", False),
    ("exclusion", True),
])
async def test_outils_complete_une_auxiliaire_par_une_fondatrice_confirmee(
        kind_fondateur: str, permute: bool) -> None:
    corpus, auxiliaire_id, fondatrice_id = _corpus_auxiliaire_puis_fondatrice(
        kind_fondateur, permute=permute, hit_auxiliaire_seul=True)
    termes = ["dossier auxiliaire"]
    assert Index(corpus).chercher(termes, limit=20, doc_id="d") == [
        (auxiliaire_id, corpus.documents["d"].node_of(auxiliaire_id))]
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id=corpus.documents["d"].node_of(auxiliaire_id),
                  focus_block_id=auxiliaire_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire_id, fondatrice_id]
    assert result.blocs[-1].kind == kind_fondateur and result.blocs[-1].kind_confirmed
    assert len(fake.requests) == request_budget.attempts == 2


async def test_outils_ne_confond_pas_premiere_fondatrice_confirmee_et_pertinence() -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Entrée auxiliaire signal neutre.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    premiere = Block(
        block_id="d:p2:1", text="Signal neutre règle étrangère.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    suivante = Block(
        block_id="d:p3:1", text="Signal neutre règle utile.", loc="p3", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("aux", [auxiliaire]), ("premiere", [premiere]), ("suivante", [suivante]))
    termes = ["signal neutre"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux",
                  focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=3, node_window=1, search_limit=3,
                       max_blocks=3, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        auxiliaire.block_id, premiere.block_id, suivante.block_id]


async def test_outils_rejoue_sans_facette_apres_une_fondatrice_initiale_seulement_typee(
        ) -> None:
    etrangere = Block(
        block_id="d:p1:1", text="Signal décisionnel règle étrangère.", loc="p1", seq=1,
        kind="garantie", kind_source="manual")
    utile = Block(
        block_id="d:p2:1", text="Signal décisionnel règle utile.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(("etrangere", [etrangere]), ("utile", [utile]))
    termes = ["signal décisionnel"]
    assert [block_id for block_id, _node_id in Index(corpus).chercher(
        termes, limit=2, doc_id="d", kinds_confirmes=KINDS_FONDATEURS)] == [
            etrangere.block_id, utile.block_id]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="etrangere",
                  focus_block_id=etrangere.block_id)),
    ], corpus=corpus, parsed=_parsed(termes, facettes=[]),
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [etrangere.block_id, utile.block_id]


async def test_outils_rejoue_la_requete_effective_dans_les_kinds_fondateurs() -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Entrée administrative alpha.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    fondatrice = Block(
        block_id="d:p2:1", text="Prise en charge du risque beta.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(("aux", [auxiliaire]), ("regle", [fondatrice]))
    dictionnaire = Dictionnaire(
        charge=True, corpus_ok=True, doc_id="d",
        _groupes={"entree administrative": ("entree administrative", "prise en charge")},
        _canoniques={"entree administrative": ("entrée administrative",)})
    termes = ["entrée administrative"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux",
                  focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes), dictionnaire=dictionnaire,
        budget=_budget(max_opens=2, node_window=1, search_limit=1,
                       max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire.block_id, fondatrice.block_id]


async def test_outils_fusionne_les_doublons_sans_affamer_un_auxiliaire_suivant() -> None:
    auxiliaire_alpha = Block(
        block_id="d:p1:1", text="Entrée brute commun alpha.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    auxiliaire_beta = Block(
        block_id="d:p2:1", text="Entrée brute commun beta.", loc="p2", seq=1,
        kind="condition", kind_source="manual")
    commune = Block(
        block_id="d:p3:1", text="Commun règle première.", loc="p3", seq=1,
        kind="garantie", kind_source="manual")
    alpha = Block(
        block_id="d:p4:1", text="Alpha règle suivante.", loc="p4", seq=1,
        kind="garantie", kind_source="manual")
    beta = Block(
        block_id="d:p5:1", text="Beta règle utile.", loc="p5", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("aux-alpha", [auxiliaire_alpha]), ("aux-beta", [auxiliaire_beta]),
        ("commune", [commune]), ("alpha", [alpha]), ("beta", [beta]))
    termes = ["entrée brute"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux-alpha",
                  focus_block_id=auxiliaire_alpha.block_id),
            _tool("ouvrir_noeud", "t3", node_id="aux-beta",
                  focus_block_id=auxiliaire_beta.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=4, node_window=1, search_limit=2,
                       max_blocks=4, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        auxiliaire_alpha.block_id, auxiliaire_beta.block_id,
        commune.block_id, beta.block_id]


async def test_outils_ignore_les_fragments_delision_comme_ponts_fondateurs() -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Dossier d'objet, l'annexe porte signalutile.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    etrangere = Block(
        block_id="d:p2:1", text="D l règle étrangère.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    utile = Block(
        block_id="d:p3:1", text="Signalutile règle attendue.", loc="p3", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("aux", [auxiliaire]), ("etrangere", [etrangere]), ("utile", [utile]))
    termes = ["dossier"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux",
                  focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=1, search_limit=1,
                       max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire.block_id, utile.block_id]


async def test_outils_garde_atomique_lunite_dune_fondatrice_rappelee() -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Dossier auxiliaire signal atomique.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    fondatrice = Block(
        block_id="d:p2:1", text="Signal atomique règle fondatrice.", loc="p2", seq=1,
        kind="garantie", kind_source="manual", refs=["d:p3:1"])
    dependance = Block(
        block_id="d:p3:1", text="Limite liée à la règle.", loc="p3", seq=1,
        kind="condition", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("aux", [auxiliaire]), ("regle", [fondatrice]), ("limite", [dependance]))
    termes = ["dossier auxiliaire"]
    script = [
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux",
                  focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ]

    admis, _step, _fake, _request_budget = await _run_outils(
        script, corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=3, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)
    refuse, _step, _fake, _request_budget = await _run_outils(
        script, corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert admis.opened_block_ids == [
        auxiliaire.block_id, fondatrice.block_id, dependance.block_id]
    assert refuse.opened_block_ids == [auxiliaire.block_id]
    assert refuse.truncated is True


@pytest.mark.parametrize("cas", ["non_confirmee", "hors_kind", "sans_signal"])
async def test_outils_ne_fabrique_pas_de_fondatrice_depuis_un_hit_auxiliaire(cas: str) -> None:
    corpus, auxiliaire_id, fondatrice_id = _corpus_auxiliaire_puis_fondatrice(
        "garantie", hit_auxiliaire_seul=True)
    fondatrice = corpus.documents["d"].block(fondatrice_id)
    if cas == "non_confirmee":
        fondatrice.kind_source = "model"
    elif cas == "hors_kind":
        fondatrice.kind = "condition"
    else:
        fondatrice.text = "Une clause distincte ne partage aucun terme significatif."
    termes = ["dossier auxiliaire"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id=corpus.documents["d"].node_of(auxiliaire_id),
                  focus_block_id=auxiliaire_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire_id]
    assert all(not (block.kind in KINDS_FONDATEURS and block.kind_confirmed)
               for block in result.blocs)


async def test_outils_ne_recree_pas_de_capacite_pour_le_rappel_depuis_auxiliaire() -> None:
    corpus, auxiliaire_id, fondatrice_id = _corpus_auxiliaire_puis_fondatrice(
        "garantie", hit_auxiliaire_seul=True)
    termes = ["dossier auxiliaire"]
    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id=corpus.documents["d"].node_of(auxiliaire_id),
                  focus_block_id=auxiliaire_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=1, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire_id]
    assert fondatrice_id not in result.opened_block_ids
    assert result.truncated is True


async def test_outils_priorise_le_focus_complementaire_dans_sa_fenetre_sans_reordonner_le_rendu(
        ) -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Dossier auxiliaire avec signal pont.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    voisin = Block(block_id="d:p2:1", text="Voisin documentaire.", loc="p2", seq=1)
    fondatrice = Block(
        block_id="d:p2:2", text="Le signal pont fonde la règle.", loc="p2", seq=2,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("aux", [auxiliaire]), ("regle", [voisin, fondatrice]))
    termes = ["dossier auxiliaire"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux", focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=2, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire.block_id, fondatrice.block_id]
    assert voisin.block_id not in result.opened_block_ids

    rendu_complet, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux", focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=2, max_blocks=3, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)
    assert rendu_complet.opened_block_ids == [
        auxiliaire.block_id, voisin.block_id, fondatrice.block_id]


async def test_outils_fusionne_equitablement_sans_exclure_le_lien_dun_auxiliaire_suivant(
        ) -> None:
    auxiliaire_bruyante = Block(
        block_id="d:p1:1", text="Entrée brute beta gamma delta epsilon.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    auxiliaire_liee = Block(
        block_id="d:p2:1", text="Entrée brute alpha pontx zeta eta theta.", loc="p2", seq=1,
        kind="condition", kind_source="manual")
    fondatrice = Block(
        block_id="d:p3:1", text="Pontx règle fondatrice.", loc="p3", seq=1,
        kind="garantie", kind_source="manual")
    distracteur_1 = Block(
        block_id="d:p4:1", text="Beta gamma delta clause.", loc="p4", seq=1,
        kind="garantie", kind_source="manual")
    distracteur_2 = Block(
        block_id="d:p5:1", text="Beta gamma epsilon clause.", loc="p5", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("aux-1", [auxiliaire_bruyante]), ("aux-2", [auxiliaire_liee]),
        ("fondatrice", [fondatrice]), ("leurre-1", [distracteur_1]),
        ("leurre-2", [distracteur_2]))
    index = Index(corpus)
    assert [block_id for block_id, _node_id in index.chercher(
        ["entrée brute"], limit=2, doc_id="d")] == [
            auxiliaire_bruyante.block_id, auxiliaire_liee.block_id]
    assert [block_id for block_id, _node_id in index.chercher(
        ["entree", "brute", "beta", "gamma", "delta", "epsilon"],
        limit=2, doc_id="d", kinds_confirmes=KINDS_FONDATEURS)] == [
            distracteur_1.block_id, distracteur_2.block_id]
    assert [block_id for block_id, _node_id in index.chercher(
        ["entree", "brute", "alpha", "pontx"],
        limit=2, doc_id="d", kinds_confirmes=KINDS_FONDATEURS)][0] == fondatrice.block_id
    assert fondatrice.block_id not in [
        block_id for block_id, _node_id in index.chercher(
            ["entree", "brute", "beta", "gamma", "delta", "epsilon",
             "alpha", "pontx"],
            limit=2, doc_id="d", kinds_confirmes=KINDS_FONDATEURS)]
    termes = ["entrée brute"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux-1",
                  focus_block_id=auxiliaire_bruyante.block_id),
            _tool("ouvrir_noeud", "t3", node_id="aux-2",
                  focus_block_id=auxiliaire_liee.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=4, node_window=1, search_limit=2,
                       max_blocks=4, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        auxiliaire_bruyante.block_id, auxiliaire_liee.block_id,
        distracteur_1.block_id, fondatrice.block_id]


@pytest.mark.parametrize("signal", ["x42", "2027"])
async def test_outils_accepte_un_pont_lexical_alphanumerique_ou_numerique(signal: str) -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text=f"Dossier auxiliaire porte {signal}.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    fondatrice = Block(
        block_id="d:p2:1", text=f"{signal} gouverne la clause.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(("aux", [auxiliaire]), ("regle", [fondatrice]))
    termes = ["dossier auxiliaire"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux", focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire.block_id, fondatrice.block_id]


async def test_outils_ne_seme_ni_un_hit_refuse_atomiquement_ni_un_voisin_non_hit() -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Recherche brute signalaux.", loc="p1", seq=1,
        kind="condition", kind_source="manual", refs=["d:p3:1", "d:p4:1"])
    voisin = Block(block_id="d:p1:2", text="Signalvoisin admis.", loc="p1", seq=2)
    dependance_1 = Block(block_id="d:p3:1", text="Dépendance une.", loc="p3", seq=1)
    dependance_2 = Block(block_id="d:p4:1", text="Dépendance deux.", loc="p4", seq=1)
    fondatrice = Block(
        block_id="d:p2:1", text="Signalaux signalvoisin fondent la règle.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("lecture", [auxiliaire, voisin]), ("regle", [fondatrice]),
        ("dep-1", [dependance_1]), ("dep-2", [dependance_2]))
    termes = ["recherche brute"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="lecture",
                  focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=2, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [voisin.block_id]
    assert fondatrice.block_id not in result.opened_block_ids
    assert result.discarded_block_ids == [auxiliaire.block_id]


async def test_outils_ignore_un_mot_outil_comme_seul_pont_vers_une_fondatrice() -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Dossier auxiliaire avec annexe.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    fondatrice = Block(
        block_id="d:p2:1", text="Avec une clause étrangère.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(("aux", [auxiliaire]), ("regle", [fondatrice]))
    termes = ["dossier auxiliaire"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux", focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire.block_id]


async def test_outils_tente_dabord_la_premiere_fondatrice_du_classement_complementaire(
        ) -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Dossier auxiliaire signal canonique.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    premiere = Block(
        block_id="d:p8:1", text="Signal canonique première règle.", loc="p8", seq=1,
        kind="garantie", kind_source="manual")
    seconde = Block(
        block_id="d:p2:1", text="Signal canonique seconde règle.", loc="p2", seq=1,
        kind="garantie", kind_source="manual")
    corpus = _corpus_neutre_par_noeuds(
        ("aux", [auxiliaire]), ("premiere", [premiere]), ("seconde", [seconde]))
    termes = ["dossier auxiliaire"]
    classement = Index(corpus).chercher(
        ["signal", "canonique"], limit=2, doc_id="d", kinds_confirmes=KINDS_FONDATEURS)
    assert [block_id for block_id, _node_id in classement] == [
        premiere.block_id, seconde.block_id]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux", focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire.block_id, premiere.block_id]
    assert seconde.block_id not in result.opened_block_ids


@pytest.mark.parametrize("max_opens", [2, 3])
async def test_outils_ne_tronque_pas_sur_les_fondatrices_excedant_la_capacite_restante(
        max_opens: int) -> None:
    auxiliaire = Block(
        block_id="d:p1:1", text="Dossier auxiliaire avec signal rappel.", loc="p1", seq=1,
        kind="condition", kind_source="manual")
    fondatrices = [
        Block(
            block_id=f"d:p{page}:1", text=f"Signal rappel fondatrice {rang}.",
            loc=f"p{page}", seq=1, kind="garantie", kind_source="manual")
        for rang, page in enumerate(range(2, 5), start=1)
    ]
    corpus = _corpus_neutre_par_noeuds(
        ("aux", [auxiliaire]),
        *((f"fondatrice-{rang}", [fondatrice])
          for rang, fondatrice in enumerate(fondatrices, start=1)))
    termes = ["dossier auxiliaire"]

    result, _step, _fake, _request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=termes),
            _tool("ouvrir_noeud", "t2", node_id="aux", focus_block_id=auxiliaire.block_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(termes),
        budget=_budget(max_opens=max_opens, node_window=1, search_limit=3,
                       max_blocks=4, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [
        auxiliaire.block_id,
        *(fondatrice.block_id for fondatrice in fondatrices[:max_opens - 1]),
    ]
    assert result.truncated is False


async def test_outils_garde_larret_substantiel_sans_exigence_declaree() -> None:
    corpus, auxiliaire_id, _fondatrice_id = _corpus_auxiliaire_puis_fondatrice("garantie")
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["règle utile"]),
            _tool("ouvrir_noeud", "t2", node_id=corpus.documents["d"].node_of(auxiliaire_id),
                  focus_block_id=auxiliaire_id)),
    ], corpus=corpus, parsed=_parsed(["règle utile"]),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000))

    assert result.opened_block_ids == [auxiliaire_id]
    assert len(fake.requests) == request_budget.attempts == 1


async def test_outils_sarrete_des_quune_fondatrice_confirmee_est_admise() -> None:
    corpus, _auxiliaire_id, fondatrice_id = _corpus_auxiliaire_puis_fondatrice("exclusion")
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["règle utile"]),
            _tool("ouvrir_noeud", "t2", node_id=corpus.documents["d"].node_of(fondatrice_id),
                  focus_block_id=fondatrice_id)),
    ], corpus=corpus, parsed=_parsed(["règle utile"]),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [fondatrice_id]
    assert len(fake.requests) == request_budget.attempts == 1


async def test_outils_ninvente_pas_de_fondatrice_absente() -> None:
    corpus, auxiliaire_id, autre_id = _corpus_auxiliaire_puis_fondatrice("para")
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["règle utile"]),
            _tool("ouvrir_noeud", "t2", node_id=corpus.documents["d"].node_of(auxiliaire_id),
                  focus_block_id=auxiliaire_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["règle utile"]),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire_id, autre_id]
    assert all(block.kind not in KINDS_FONDATEURS for block in result.blocs)
    assert result.truncated is False
    assert len(fake.requests) == request_budget.attempts == 2


async def test_outils_ne_depasse_pas_le_quota_pour_atteindre_une_fondatrice() -> None:
    corpus, auxiliaire_id, fondatrice_id = _corpus_auxiliaire_puis_fondatrice("garantie")
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["règle utile"]),
            _tool("ouvrir_noeud", "t2", node_id=corpus.documents["d"].node_of(auxiliaire_id),
                  focus_block_id=auxiliaire_id)),
        _tool_message(
            _tool("ouvrir_noeud", "t3", node_id=corpus.documents["d"].node_of(fondatrice_id),
                  focus_block_id=fondatrice_id)),
    ], corpus=corpus, parsed=_parsed(["règle utile"]),
        budget=_budget(max_opens=1, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire_id]
    assert result.truncated is True
    assert len(fake.requests) == request_budget.attempts == 2


async def test_outils_complete_une_recherche_sans_ouverture_jusqua_la_fondatrice() -> None:
    corpus, auxiliaire_id, fondatrice_id = _corpus_auxiliaire_puis_fondatrice("garantie")
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["règle utile"])),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["règle utile"]),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [fondatrice_id, auxiliaire_id]
    fondatrice = next(bloc for bloc in result.blocs if bloc.block_id == fondatrice_id)
    assert fondatrice.kind == "garantie" and fondatrice.kind_confirmed
    assert len(fake.requests) == request_budget.attempts == 2


async def test_outils_ignore_une_fondatrice_non_confirmee_pour_la_suffisance() -> None:
    corpus, non_confirmee_id, confirmee_id = _corpus_auxiliaire_puis_fondatrice("exclusion")
    non_confirmee = corpus.documents["d"].block(non_confirmee_id)
    non_confirmee.kind = "garantie"
    non_confirmee.kind_source = "model"
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["règle utile"]),
            _tool("ouvrir_noeud", "t2", node_id=corpus.documents["d"].node_of(non_confirmee_id),
                  focus_block_id=non_confirmee_id)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["règle utile"]),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [non_confirmee_id, confirmee_id]
    assert not result.blocs[0].kind_confirmed
    assert result.blocs[1].kind == "exclusion" and result.blocs[1].kind_confirmed
    assert len(fake.requests) == request_budget.attempts == 2


async def test_outils_marque_partielle_une_ouverture_directe_sans_preuve_dabsence() -> None:
    corpus, auxiliaire_id, _fondatrice_id = _corpus_auxiliaire_puis_fondatrice("garantie")
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool(
            "ouvrir_noeud", "t1", node_id=corpus.documents["d"].node_of(auxiliaire_id))),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["règle utile"]),
        budget=_budget(max_opens=2, node_window=1, max_blocks=2, max_tokens=6000),
        settings=_s(retrieval_mechanism_order="outils,sommaire,dictionnaire,faq"),
        kinds_suffisants=KINDS_FONDATEURS)

    assert result.opened_block_ids == [auxiliaire_id]
    assert result.truncated is True
    assert len(fake.requests) == request_budget.attempts == 2


async def test_outils_poursuit_apres_un_premier_tour_exclusivement_contextuel() -> None:
    corpus = _corpus_contexte_puis_regle()
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["règle utile"]),
                      _tool("ouvrir_noeud", "t2", node_id="contexte")),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["règle utile"]),
        budget=_budget(max_opens=2, node_window=2, max_blocks=3, max_tokens=6000))

    assert result.opened_block_ids == ["d:p1:1", "d:p1:2", "d:p2:1"]
    assert len(fake.requests) == request_budget.attempts == 2
    first_results = fake.requests[1]["messages"][-1]["content"]
    assert '"block_id": "d:p1:1"' in first_results[1]["content"]
    assert '"block_id": "d:p1:2"' in first_results[1]["content"]


async def test_outils_ne_recree_pas_de_capacite_apres_un_tour_contextuel() -> None:
    corpus = _corpus_contexte_puis_regle()
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["règle utile"]),
                      _tool("ouvrir_noeud", "t2", node_id="contexte")),
        _tool_message(_tool("ouvrir_noeud", "t3", node_id="regle",
                            focus_block_id="d:p2:1")),
    ], corpus=corpus, parsed=_parsed(["règle utile"]),
        budget=_budget(max_opens=1, node_window=2, max_blocks=3, max_tokens=6000))

    assert result.opened_block_ids == ["d:p1:1", "d:p1:2"]
    assert result.truncated is True
    assert len(fake.requests) == request_budget.attempts == 2


async def test_outils_nominal_exposes_four_tools_and_stops_after_one_useful_turn() -> None:
    result, step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["matricule"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1", focus_block_id="d:p1:1")),
    ])
    assert [t["name"] for t in OUTILS_RECHERCHE] == [
        "sommaire", "ouvrir_noeud", "chercher", "definitions"]
    assert len(fake.requests) == request_budget.attempts == 1
    assert all([t["name"] for t in request["tools"]] == [
        "sommaire", "ouvrir_noeud", "chercher", "definitions"] for request in fake.requests)
    assert "root" in fake.requests[0]["system"][0]["text"]
    assert '<untrusted kind="sommaire">' in fake.requests[0]["system"][0]["text"]
    assert fake.requests[0]["max_tokens"] == _s().retrouver_outils_max_tokens
    assert result.opened_block_ids == ["d:p1:1", "d:p1:2", "d:p1:5"]
    assert not result.truncated and step.calls and step.tier == "micro"
    assert fake.requests[0]["model"] == TIERS["micro"]
    assert len(step.calls) == 1 and step.usage.cost_eur == request_budget.cost_eur > 0
    assert step.opened_block_ids == result.opened_block_ids


async def test_les_deux_variantes_partagent_la_meme_fermeture_directe() -> None:
    """Une garantie emporte sa définition, son renvoi direct et la limite déjà classée.

    La cible du renvoi direct possède elle-même un renvoi : ce second niveau doit rester fermé.
    """
    blocks = [
        Block(block_id="d:p1:1", text="Garantie incendie du contenu.", loc="p1", seq=1,
              kind="garantie", refs=["d:p3:1"]),
        Block(block_id="d:p2:1", text="Exclusion incendie volontaire.", loc="p2", seq=1,
              kind="exclusion", relation={"exception_de": "d:p1:1"}),
        Block(block_id="d:p4:1", text="Le contenu désigne les biens mobiliers.", loc="p4", seq=1,
              kind="definition", defines="contenu"),
        Block(block_id="d:p3:1", text="Cible directe sans terme de recherche.", loc="p3", seq=1,
              refs=["d:p5:1"]),
        Block(block_id="d:p5:1", text="Cible de second niveau interdite.", loc="p5", seq=1),
        Block(block_id="d:p6:1", text="Exclusion contenu moins bien classée.", loc="p6", seq=1,
              kind="exclusion"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id=f"n{i}") for i in range(1, 7)])]
    nodes.extend(Node(node_id=f"n{i}", items=[BlockRef(block_id=block.block_id)])
                 for i, block in enumerate(blocks, 1))
    corpus = Corpus(
        documents={"d": Document(doc_id="d", kind="contrat", title="t", edition="e",
                                   nodes=nodes, blocks=blocks)},
        summaries={"d": "root\n  n1\n  n2\n  n3\n  n4\n  n5\n  n6"},
    )
    parsed = _parsed(["incendie", "contenu"])
    budget = _budget(max_opens=2, node_window=1, max_blocks=10, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["incendie", "contenu"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    attendu = {"d:p1:1", "d:p2:1", "d:p3:1", "d:p4:1"}
    assert set(deterministe.opened_block_ids) == set(outils.opened_block_ids) == attendu
    assert "d:p5:1" not in deterministe.opened_block_ids
    assert "d:p5:1" not in outils.opened_block_ids
    assert "d:p6:1" not in deterministe.opened_block_ids
    assert "d:p6:1" not in outils.opened_block_ids


async def test_un_renvoi_heading_emporte_un_passage_citable_sans_suivre_ses_refs() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Consulter la règle de délai.", loc="p1", seq=1,
              refs=["d:p2:1"]),
        Block(block_id="d:p2:1", text="Délai applicable", loc="p2", seq=1, kind="heading"),
        Block(block_id="d:p2:2", text="La demande est déposée dans les trente jours.",
              loc="p2", seq=2, refs=["d:p3:1"]),
        Block(block_id="d:p3:1", text="Second niveau interdit.", loc="p3", seq=1),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="source"), NodeRef(node_id="cible"),
                                          NodeRef(node_id="second")]),
             Node(node_id="source", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="cible", items=[BlockRef(block_id="d:p2:1"), BlockRef(block_id="d:p2:2")]),
             Node(node_id="second", items=[BlockRef(block_id="d:p3:1")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > source, cible, second"})
    parsed = _parsed(["consulter règle"])
    budget = _budget(max_opens=1, node_window=1, max_blocks=3, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["consulter règle"]),
                      _tool("ouvrir_noeud", "t2", node_id="source",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    attendu = ["d:p1:1", "d:p2:1", "d:p2:2"]
    assert deterministe.opened_block_ids == outils.opened_block_ids == attendu
    assert "d:p3:1" not in deterministe.opened_block_ids
    assert "d:p3:1" not in outils.opened_block_ids

    borne, _ = _run(parsed, corpus, Index(corpus),
                    _budget(max_opens=1, node_window=1, max_blocks=2, max_tokens=6000))
    assert borne.opened_block_ids == [] and borne.truncated
    borne_outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["consulter règle"]),
                      _tool("ouvrir_noeud", "t2", node_id="source",
                            focus_block_id="d:p1:1")),
        fake_message(model=TIERS["micro"], stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=parsed,
        budget=_budget(max_opens=1, node_window=1, max_blocks=2, max_tokens=6000))
    assert borne_outils.opened_block_ids == [] and borne_outils.truncated


def _corpus_facettes_distinctes() -> tuple[Corpus, ParsedQuestion]:
    blocks = [
        Block(block_id="d:p1:1", text="Thème profil général.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Encore un thème profil général.", loc="p2", seq=1),
        Block(block_id="d:p3:1", text="Preuve de la première démarche.", loc="p3", seq=1),
        Block(block_id="d:p4:1", text="Preuve de la seconde démarche.", loc="p4", seq=1),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id=f"n{i}") for i in range(1, 5)])]
    nodes.extend(Node(node_id=f"n{i}", items=[BlockRef(block_id=block.block_id)])
                 for i, block in enumerate(blocks, 1))
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2, n3, n4"})
    parsed = _parsed(["première démarche", "seconde démarche"], themes=["thème profil"],
                     facettes=["première démarche", "seconde démarche"])
    return corpus, parsed


def _corpus_facette_titre(*, heading: bool = True) -> tuple[Corpus, ParsedQuestion]:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Seconde démarche utile.", loc="p2", seq=1,
              kind="heading" if heading else "para"),
        Block(block_id="d:p2:2", text="Corps explicatif à citer.", loc="p2", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1"),
                                        BlockRef(block_id="d:p2:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])
    return corpus, parsed


async def test_une_reservation_heading_emporte_son_premier_corps_dans_les_deux_variantes() -> None:
    corpus, parsed = _corpus_facette_titre()
    budget = _budget(max_opens=2, node_window=1, search_limit=2,
                     max_blocks=4, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    attendu = ["d:p1:1", "d:p2:1", "d:p2:2"]
    assert deterministe.opened_block_ids == outils.opened_block_ids == attendu
    assert len(fake.requests) == request_budget.attempts == 1


async def test_une_reservation_heading_est_refusee_atomiquement_si_son_corps_ne_tient_pas() -> None:
    corpus, parsed = _corpus_facette_titre()
    budget = _budget(max_opens=2, node_window=1, search_limit=2,
                     max_blocks=2, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:1"]
    assert deterministe.truncated and outils.truncated


async def test_le_corps_visible_nest_pas_retente_apres_le_refus_du_focus_heading() -> None:
    corpus, parsed = _corpus_facette_titre()
    budget = _budget(max_opens=2, node_window=2, search_limit=2,
                     max_blocks=2, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:1"]
    assert deterministe.truncated and outils.truncated


async def test_un_compagnon_aussi_hit_garde_son_unite_primaire_historique() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Seconde démarche utile.", loc="p2", seq=1,
              kind="heading"),
        Block(block_id="d:p2:2", text="Seconde démarche utile expliquée.", loc="p2", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1"),
                                        BlockRef(block_id="d:p2:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])
    budget = _budget(max_opens=2, node_window=2, search_limit=3,
                     max_blocks=2, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:1", "d:p2:2"]
    assert deterministe.truncated and outils.truncated


async def test_un_compagnon_hit_reste_dans_lordre_de_sa_fenetre_avant_le_noeud_suivant() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première facette utile.", loc="p1", seq=1,
              kind="heading"),
        Block(block_id="d:p1:2", text="Première facette utile expliquée.", loc="p1", seq=2),
        Block(block_id="d:p2:1", text="Seconde facette utile.", loc="p2", seq=1),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1"),
                                        BlockRef(block_id="d:p1:2")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première facette", "seconde facette"],
                     facettes=["première facette", "seconde facette"])
    budget = _budget(max_opens=2, node_window=2, search_limit=3,
                     max_blocks=1, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première facette", "seconde facette"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:2"]
    assert deterministe.blocs == outils.blocs
    assert deterministe.truncated == outils.truncated is True


async def test_h_c_x_garde_le_compagnon_hit_dans_lordre_unique_de_la_fenetre() -> None:
    """H échoue atomiquement ; C doit être tenté avant X, sans réinjection tardive."""
    blocks = [
        Block(block_id="d:p1:1", text="Facette utile.", loc="p1", seq=1, kind="heading"),  # H
        Block(block_id="d:p1:2", text="Facette utile développée.", loc="p1", seq=2),  # C
        Block(block_id="d:p1:3", text="Facette utile voisine.", loc="p1", seq=3),  # X
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=block.block_id) for block in blocks])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n"})
    parsed = _parsed(["facette utile"], facettes=["facette utile"])
    budget = _budget(max_opens=1, node_window=3, search_limit=3,
                     max_blocks=1, max_tokens=6000)

    index = Index(corpus)
    reservations: list[tuple[str, str]] = []
    hits = index.chercher(
        parsed.termes_de_recherche(), limit=budget.search_limit, doc_id="d",
        groupes_prioritaires=parsed.facettes, reservations_out=reservations)
    assert [block_id for block_id, _node_id in hits] == ["d:p1:1", "d:p1:2", "d:p1:3"]
    assert reservations == [("d:p1:1", "n")]

    deterministe, _ = _run(parsed, corpus, index, budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["facette utile"]),
                      _tool("ouvrir_noeud", "t2", node_id="n",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:2"]
    assert deterministe.blocs == outils.blocs
    assert deterministe.truncated == outils.truncated is True


async def test_un_focus_heading_sans_corps_est_refuse_dans_les_deux_variantes() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Seconde démarche utile.", loc="p2", seq=1,
              kind="heading"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])
    budget = _budget(max_opens=2, node_window=2, search_limit=2,
                     max_blocks=4, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:1"]
    assert deterministe.truncated and outils.truncated


async def test_le_focus_heading_prime_localement_sans_changer_lordre_rendu() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Contexte voisin contingent.", loc="p2", seq=1),
        Block(block_id="d:p2:2", text="Seconde démarche utile.", loc="p2", seq=2,
              kind="heading"),
        Block(block_id="d:p2:3", text="Corps explicatif à citer.", loc="p2", seq=3),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1"),
                                        BlockRef(block_id="d:p2:2"),
                                        BlockRef(block_id="d:p2:3")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])
    async def executer(max_blocks: int) -> tuple[RetrievalResult, RetrievalResult]:
        budget = _budget(max_opens=2, node_window=3, search_limit=2,
                         max_blocks=max_blocks, max_tokens=6000)
        deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
        outils, _step, _fake, _request_budget = await _run_outils([
            _tool_message(_tool("chercher", "t1",
                                termes=["première démarche", "seconde démarche"]),
                          _tool("ouvrir_noeud", "t2", node_id="n1",
                                focus_block_id="d:p1:1")),
        ], corpus=corpus, parsed=parsed, budget=budget)
        return deterministe, outils

    deterministe_serre, outils_serre = await executer(3)
    attendu_serre = ["d:p1:1", "d:p2:2", "d:p2:3"]
    assert deterministe_serre.opened_block_ids == outils_serre.opened_block_ids == attendu_serre
    assert deterministe_serre.blocs == outils_serre.blocs
    assert deterministe_serre.truncated == outils_serre.truncated is True

    deterministe_large, outils_large = await executer(4)
    attendu_large = ["d:p1:1", "d:p2:1", "d:p2:2", "d:p2:3"]
    assert deterministe_large.opened_block_ids == outils_large.opened_block_ids == attendu_large
    assert deterministe_large.blocs == outils_large.blocs
    assert deterministe_large.truncated == outils_large.truncated is False


async def test_une_reservation_primaire_ordinaire_reste_seule() -> None:
    corpus, parsed = _corpus_facette_titre(heading=False)
    budget = _budget(max_opens=2, node_window=1, search_limit=2,
                     max_blocks=2, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:1", "d:p2:1"]


async def test_chaque_facette_reserve_un_noeud_avant_les_themes_dans_les_deux_variantes() -> None:
    corpus, parsed = _corpus_facettes_distinctes()
    budget = _budget(max_opens=2, node_window=1, search_limit=2,
                     max_blocks=2, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["thème profil", "première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n3", focus_block_id="d:p3:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p3:1", "d:p4:1"]
    assert len(_fake.requests) == _request_budget.attempts == 1


async def test_une_ouverture_sans_focus_autorise_la_completion_des_facettes() -> None:
    corpus, parsed = _corpus_facettes_distinctes()
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["thème profil", "première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n3")),
    ], corpus=corpus, parsed=parsed,
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=2, max_tokens=6000))

    assert result.opened_block_ids == ["d:p3:1", "d:p4:1"]
    assert len(fake.requests) == request_budget.attempts == 1


async def test_un_sommaire_apres_outils_nouvre_pas_de_reservation_tardive() -> None:
    corpus, parsed = _corpus_facettes_distinctes()
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["première"]),
                      _tool("ouvrir_noeud", "t2", node_id="n3",
                            focus_block_id="d:p3:1")),
    ], corpus=corpus, parsed=parsed,
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=2, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0,
                    retrieval_mechanism_order="outils,sommaire,dictionnaire,faq"))

    assert result.opened_block_ids == ["d:p3:1"]
    assert "d:p4:1" not in result.opened_block_ids
    assert len(fake.requests) == request_budget.attempts == 1


async def test_les_reservations_saccumulent_entre_deux_recherches_successives() -> None:
    corpus, parsed = _corpus_facettes_distinctes()
    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["première"]),
                      _tool("chercher", "t2", termes=["seconde"]),
                      _tool("ouvrir_noeud", "t3", node_id="n4",
                            focus_block_id="d:p4:1")),
    ], corpus=corpus, parsed=parsed,
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=2, max_tokens=6000))

    assert result.opened_block_ids == ["d:p4:1", "d:p3:1"]
    assert len(fake.requests) == request_budget.attempts == 1


async def test_une_fenetre_valide_refusee_laisse_la_facette_suivante_entrer() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1,
              refs=["d:p3:1", "d:p3:2"]),
        Block(block_id="d:p2:1", text="Seconde démarche utile.", loc="p2", seq=1),
        Block(block_id="d:p3:1", text="Précision alpha.", loc="p3", seq=1),
        Block(block_id="d:p3:2", text="Précision bêta.", loc="p3", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2"),
                                         NodeRef(node_id="n3")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1")]),
             Node(node_id="n3", items=[BlockRef(block_id="d:p3:1"),
                                       BlockRef(block_id="d:p3:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2, n3"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])

    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
        fake_message(model=TIERS["micro"], stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=parsed,
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=1, max_tokens=6000))

    assert result.opened_block_ids == ["d:p2:1"]
    assert result.truncated is True
    assert len(fake.requests) == request_budget.attempts == 2


async def test_la_completion_tente_le_focus_avant_son_frere_de_fenetre() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Passage voisin contingent.", loc="p2", seq=1),
        Block(block_id="d:p2:2", text="Seconde démarche utile.", loc="p2", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1"),
                                       BlockRef(block_id="d:p2:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])

    budget = _budget(max_opens=2, node_window=2, search_limit=2,
                     max_blocks=2, max_tokens=6000)
    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    attendu = ["d:p1:1", "d:p2:2"]
    assert deterministe.opened_block_ids == outils.opened_block_ids == attendu
    assert deterministe.blocs == outils.blocs
    assert deterministe.truncated == outils.truncated is True


async def test_le_navigateur_priorise_lui_meme_un_focus_ordinaire_reserve() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Passage voisin contingent.", loc="p2", seq=1),
        Block(block_id="d:p2:2", text="Seconde démarche utile.", loc="p2", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1"),
                                        BlockRef(block_id="d:p2:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])
    budget = _budget(max_opens=2, node_window=2, search_limit=2,
                     max_blocks=2, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1"),
                      _tool("ouvrir_noeud", "t3", node_id="n2",
                            focus_block_id="d:p2:2")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    attendu = ["d:p1:1", "d:p2:2"]
    assert deterministe.opened_block_ids == outils.opened_block_ids == attendu
    assert deterministe.blocs == outils.blocs
    assert deterministe.truncated == outils.truncated is True


async def test_le_navigateur_priorise_lui_meme_un_focus_heading_reserve() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Contexte voisin contingent.", loc="p2", seq=1),
        Block(block_id="d:p2:2", text="Seconde démarche utile.", loc="p2", seq=2,
              kind="heading"),
        Block(block_id="d:p2:3", text="Corps explicatif à citer.", loc="p2", seq=3),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1"),
                                        BlockRef(block_id="d:p2:2"),
                                        BlockRef(block_id="d:p2:3")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])
    budget = _budget(max_opens=2, node_window=3, search_limit=2,
                     max_blocks=3, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1"),
                      _tool("ouvrir_noeud", "t3", node_id="n2",
                            focus_block_id="d:p2:2")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    attendu = ["d:p1:1", "d:p2:2", "d:p2:3"]
    assert deterministe.opened_block_ids == outils.opened_block_ids == attendu
    assert deterministe.blocs == outils.blocs
    assert deterministe.truncated == outils.truncated is True


async def test_une_reservation_non_best_hit_ne_recoit_pas_de_priorite() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Seconde démarche utile.", loc="p1", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id="d:p1:1"),
                                      BlockRef(block_id="d:p1:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])
    budget = _budget(max_opens=1, node_window=2, search_limit=2,
                     max_blocks=1, max_tokens=6000)
    reservations: list[tuple[str, str]] = []
    Index(corpus).chercher(
        parsed.termes_de_recherche(), limit=budget.search_limit, doc_id="d",
        groupes_prioritaires=parsed.facettes, reservations_out=reservations)
    assert reservations == [("d:p1:1", "n"), ("d:p1:2", "n")]

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n",
                            focus_block_id="d:p1:2")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:1"]
    assert deterministe.blocs == outils.blocs
    assert deterministe.truncated == outils.truncated is True


async def test_un_best_hit_faq_precede_le_focus_lexical_reserve_dans_les_deux_variantes() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Contexte FAQ voisin.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Cible lexicale réservée.", loc="p1", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id="d:p1:1"),
                                      BlockRef(block_id="d:p1:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n"})
    parsed = _parsed(["cible lexicale réservée"], facettes=["cible lexicale réservée"])
    dictionnaire = Dictionnaire(
        charge=True, corpus_ok=True, doc_id="d", _candidate_questions={"n": ("q",)})
    settings = _s(retrieval_mechanism_order="faq,sommaire,outils,dictionnaire")
    budget = _budget(max_opens=1, node_window=2, search_limit=2,
                     max_blocks=1, max_tokens=6000)

    deterministe, _ = _run(
        parsed, corpus, Index(corpus), budget, settings=settings, doc_id="d",
        dictionnaire=dictionnaire)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["cible lexicale réservée"]),
                      _tool("ouvrir_noeud", "t2", node_id="n",
                            focus_block_id="d:p1:2")),
    ], corpus=corpus, parsed=parsed, budget=budget, settings=settings,
        dictionnaire=dictionnaire)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:1"]
    assert deterministe.blocs == outils.blocs
    assert deterministe.truncated == outils.truncated is True


async def test_un_best_hit_non_reserve_conserve_lordre_documentaire() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Contexte voisin sans terme.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Cible lexicale unique.", loc="p1", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id="d:p1:1"),
                                      BlockRef(block_id="d:p1:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n"})
    parsed = _parsed(["cible lexicale unique"])
    budget = _budget(max_opens=1, node_window=2, search_limit=2,
                     max_blocks=1, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["cible lexicale unique"]),
                      _tool("ouvrir_noeud", "t2", node_id="n",
                            focus_block_id="d:p1:2")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:1"]
    assert deterministe.blocs == outils.blocs
    assert deterministe.truncated == outils.truncated is True


async def test_une_recherche_ulterieure_ne_remplace_pas_le_best_hit_first_wins() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Alpha repère initial.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Oméga cible finale.", loc="p1", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id="d:p1:1"),
                                      BlockRef(block_id="d:p1:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n"})
    parsed = _parsed(["oméga cible finale"], facettes=["oméga cible finale"])

    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["alpha repère initial"]),
                      _tool("chercher", "t2", termes=["oméga cible finale"]),
                      _tool("ouvrir_noeud", "t3", node_id="n",
                            focus_block_id="d:p1:2")),
    ], corpus=corpus, parsed=parsed,
        budget=_budget(max_opens=1, node_window=2, search_limit=2,
                       max_blocks=1, max_tokens=6000),
        settings=_s(retrieval_mechanism_order="outils,sommaire,dictionnaire,faq"))

    # La seconde recherche réserve bien `p1:2`, mais `p1:1` reste l'autorité first-wins du nœud.
    assert outils.opened_block_ids == ["d:p1:1"]
    assert outils.truncated is True


async def test_une_dependance_deja_admise_retrouve_lordre_de_sa_fenetre_primaire() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1,
              refs=["d:p2:2"]),
        Block(block_id="d:p2:1", text="Passage voisin contingent.", loc="p2", seq=1),
        Block(block_id="d:p2:2", text="Seconde démarche utile.", loc="p2", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1"),
                                        BlockRef(block_id="d:p2:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])
    budget = _budget(max_opens=2, node_window=2, search_limit=2,
                     max_blocks=3, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1"),
                      _tool("ouvrir_noeud", "t3", node_id="n2",
                            focus_block_id="d:p2:2")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    attendu = ["d:p1:1", "d:p2:1", "d:p2:2"]
    assert deterministe.opened_block_ids == outils.opened_block_ids == attendu
    assert deterministe.blocs == outils.blocs
    assert deterministe.truncated == outils.truncated is False


async def test_deux_facettes_du_meme_noeud_sont_completees_fenetre_par_fenetre() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Seconde démarche utile.", loc="p1", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id="d:p1:1"),
                                      BlockRef(block_id="d:p1:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])

    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n",
                            focus_block_id="d:p1:1")),
        fake_message(model=TIERS["micro"], stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=parsed,
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=2, max_tokens=6000))

    assert result.opened_block_ids == ["d:p1:1", "d:p1:2"]
    assert len(fake.requests) == request_budget.attempts == 2


async def test_la_completion_des_facettes_respecte_budget_et_atomicite() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Seconde démarche utile.", loc="p2", seq=1,
              refs=["d:p2:2"]),
        Block(block_id="d:p2:2", text="Précision liée indispensable.", loc="p2", seq=2),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1"),
                                       BlockRef(block_id="d:p2:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])

    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed,
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=2, max_tokens=6000))

    assert result.opened_block_ids == ["d:p1:1"]
    assert result.truncated is True
    assert len(fake.requests) == request_budget.attempts == 1


async def test_la_completion_dedoublonne_les_reservations_deja_ouvertes() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Première démarche utile.", loc="p1", seq=1),
        Block(block_id="d:p2:1", text="Seconde démarche utile.", loc="p2", seq=1),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["première démarche", "seconde démarche"],
                     facettes=["première démarche", "seconde démarche"])

    result, _step, fake, request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1",
                            termes=["première démarche", "seconde démarche"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1",
                            focus_block_id="d:p1:1"),
                      _tool("ouvrir_noeud", "t3", node_id="n2",
                            focus_block_id="d:p2:1")),
    ], corpus=corpus, parsed=parsed,
        budget=_budget(max_opens=2, node_window=1, search_limit=2,
                       max_blocks=2, max_tokens=6000))

    assert result.opened_block_ids == ["d:p1:1", "d:p2:1"]
    assert result.truncated is False
    assert len(fake.requests) == request_budget.attempts == 1


async def test_une_cible_aussi_primaire_reste_atomique_avec_sa_source() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Garantie feu, voir la condition.", loc="p1", seq=1,
              kind="garantie", refs=["d:p1:2"]),
        Block(block_id="d:p1:2", text="Condition feu applicable.", loc="p1", seq=2,
              kind="condition"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]), Node(
        node_id="n", items=[BlockRef(block_id=block.block_id) for block in blocks])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n"})
    parsed = _parsed(["feu"])
    budget = _budget(node_window=2, max_blocks=1, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["feu"]),
                      _tool("ouvrir_noeud", "t2", node_id="n",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:2"]
    assert deterministe.truncated and outils.truncated


async def test_la_limite_contractuellement_proche_prime_dans_lunite_bornee() -> None:
    """La limite partageant la règle opératoire prime un hit lexical antérieur sans rapport.

    Les identifiants, pages et mots du contrat réel n'interviennent pas : seule la proximité entre
    les formulations des clauses départage le cohort déjà trouvé. Avec deux places, la garantie et
    cette limite hors de sa portée entrent atomiquement dans les deux variantes.
    """
    blocks = [
        Block(block_id="d:p1:1",
              text=("Sinistre déclaré : dommages causés par une action subite de la chaleur "
                    "sans embrasement."),
              loc="p1", seq=1, kind="garantie", scope_node_id="n-garantie"),
        Block(block_id="d:p2:1", text="Exclusion du sinistre pour les objets transportés.",
              loc="p2", seq=1, kind="exclusion", scope_node_id="n-autre"),
        Block(block_id="d:p3:1",
              text=("Pour les extensions, les dommages causés par une action subite de la chaleur "
                    "sans embrasement sont exclus."),
              loc="p3", seq=1, kind="exclusion", scope_node_id="n-extension",
              scope_node_ids=["n-extension-enfant"]),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id=n) for n in
                                          ("n-garantie", "n-autre", "n-extension")]),
             Node(node_id="n-garantie", scope={"kind": "commun"},
                  items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n-autre", scope={"kind": "special"},
                  items=[BlockRef(block_id="d:p2:1")]),
             Node(node_id="n-extension", scope={"kind": "extension"},
                  items=[BlockRef(block_id="d:p3:1"),
                         NodeRef(node_id="n-extension-enfant")]),
             Node(node_id="n-extension-enfant", scope={"kind": "extension"})]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n-garantie, n-autre, n-extension"})
    parsed = _parsed(["sinistre"])
    budget = _budget(node_window=1, max_blocks=2, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["sinistre"]),
                      _tool("ouvrir_noeud", "t2", node_id="n-garantie",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    attendu = ["d:p1:1", "d:p3:1"]
    assert deterministe.opened_block_ids == outils.opened_block_ids == attendu
    assert corpus.documents["d"].block("d:p3:1").scope_node_ids == ["n-extension-enfant"]
    assert len(deterministe.blocs) == len(outils.blocs) == budget.max_blocks
    assert deterministe.truncated  # le déterministe publie les candidats tombés hors quota
    assert outils.truncated is False  # un candidat jamais ouvert n'est pas une fenêtre tronquée


async def test_outils_ne_reutilise_pas_la_limite_d_une_recherche_sans_rapport() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Garantie feu du mobilier.", loc="p1", seq=1,
              kind="garantie"),
        Block(block_id="d:p2:1", text="Exclusion eau souterraine.", loc="p2", seq=1,
              kind="exclusion"),
        Block(block_id="d:p3:1", text="Condition feu du mobilier.", loc="p3", seq=1,
              kind="condition"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id=f"n{i}") for i in range(1, 4)])]
    nodes.extend(Node(node_id=f"n{i}", items=[BlockRef(block_id=block.block_id)])
                 for i, block in enumerate(blocks, 1))
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2, n3"})
    parsed = _parsed(["feu", "mobilier"])
    budget = _budget(max_blocks=5, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["eau"]),
                      _tool("chercher", "t2", termes=["feu", "mobilier"]),
                      _tool("ouvrir_noeud", "t3", node_id="n1",
                            focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:1", "d:p3:1"]
    assert "d:p2:1" not in outils.opened_block_ids


async def test_une_limite_ne_partageant_que_des_mots_outils_nest_pas_attachee() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="La garantie est acquise pour le bien.", loc="p1", seq=1,
              kind="garantie", scope_node_id="n1"),
        Block(block_id="d:p2:1", text="La condition est prévue pour le contrat.", loc="p2", seq=1,
              kind="condition", scope_node_id="n2"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1, n2"})
    parsed = _parsed(["garantie", "bien"])
    budget = _budget(max_blocks=4, max_tokens=6000)

    deterministe, _ = _run(parsed, corpus, Index(corpus), budget)
    outils, _step, _fake, _request_budget = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["garantie", "bien"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1", focus_block_id="d:p1:1")),
    ], corpus=corpus, parsed=parsed, budget=budget)

    assert deterministe.opened_block_ids == outils.opened_block_ids == ["d:p1:1"]


async def test_outils_memoise_la_recherche_de_limite_par_bloc(
        monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = _corpus()
    doc = corpus.documents["d"]
    garantie = doc.block("d:p1:1").model_copy(update={"kind": "garantie"}, deep=True)
    corpus = Corpus(documents={"d": Document.model_validate(doc.model_copy(
        update={"blocks": [garantie, *doc.blocks[1:]]}, deep=True).model_dump())},
        summaries=corpus.summaries)
    original = Index.chercher
    calls: list[tuple[object, ...]] = []

    def counted(self: Index, *args: object, **kwargs: object):
        calls.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Index, "chercher", counted)
    await _run_outils([
        _tool_message(
            _tool("chercher", "t1", termes=["matricule"]),
            _tool("ouvrir_noeud", "t2", node_id="n1", focus_block_id="d:p1:1"),
            _tool("ouvrir_noeud", "t3", node_id="n1", focus_block_id="d:p1:1"),
        ),
    ], corpus=corpus, parsed=_parsed(["matricule"]), budget=_budget(max_blocks=10, max_tokens=6000))

    # phase sommaire + recherche du navigateur + une seule recherche liée pour la garantie
    assert len(calls) == 3


async def test_outils_reason_tier_reaches_the_provider_and_the_trace_unchanged() -> None:
    settings = _s(max_cost_eur_per_request=1.0, retrouver_outils_tier="reason")
    result, step, fake, _ = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["matricule"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1")),
    ], settings=settings)
    assert result.opened_block_ids
    assert fake.requests[0]["model"] == TIERS["reason"]
    assert step.tier == "reason" and step.calls[0].model == TIERS["reason"]


async def test_outils_category_without_blocks_returns_title_and_titled_children() -> None:
    _result, _step, fake, _ = await _run_outils([
        _tool_message(_tool("ouvrir_noeud", "t1", node_id="root")),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ])
    second_messages = fake.requests[1]["messages"]
    tool_result = second_messages[-1]["content"][0]["content"]
    assert '"title": "Sommaire"' in tool_result
    assert '"node_id": "n1"' in tool_result and '"title": "Arrivée"' in tool_result
    assert '"blocks": []' in tool_result


async def test_outils_pagination_counts_each_open_and_can_be_completed() -> None:
    corpus = Corpus(documents={"d": _linear_doc(5)}, summaries={"d": "root > n"})
    result, _step, fake, _ = await _run_outils([
        _tool_message(_tool("ouvrir_noeud", "t1", node_id="n")),
        _tool_message(_tool("ouvrir_noeud", "t2", node_id="n", cursor=3)),
    ], corpus=corpus, parsed=_parsed(["bloc"]),
        budget=_budget(node_window=3, max_opens=2, max_blocks=10, max_tokens=6000))
    assert result.opened_block_ids == [f"d:p1:{i}" for i in range(1, 6)]
    assert result.truncated is False
    serialized = fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert '"next_cursor": 3' in serialized
    assert '"block_id": "d:p1:1"' in serialized and '"text": "Bloc 1"' in serialized
    assert '"loc"' not in serialized and '"seq"' not in serialized and '"refs"' not in serialized

    limited, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("ouvrir_noeud", "t1", node_id="n")),
        _tool_message(_tool("ouvrir_noeud", "t2", node_id="n", cursor=3)),
    ], corpus=corpus, parsed=_parsed(["bloc"]),
        budget=_budget(node_window=3, max_opens=1, max_blocks=10, max_tokens=6000))
    assert limited.truncated and limited.opened_block_ids == ["d:p1:1", "d:p1:2", "d:p1:3"]


async def test_outils_budget_is_atomic_and_first_turn_without_tool_is_incomplete() -> None:
    result, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("ouvrir_noeud", "t1", node_id="n1")),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], budget=_budget(max_blocks=1, max_tokens=6000))
    # Le premier bloc tient ; l'unité suivante (bloc + cible du renvoi) est sautée entière.
    assert result.opened_block_ids == ["d:p1:1"] and result.truncated
    assert "d:p1:5" not in result.opened_block_ids

    empty, step, _fake, _ = await _run_outils([
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ])
    assert empty.blocs == [] and empty.truncated and step.discarded_block_ids == []

    summary_only, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("sommaire", "t1", doc_id="d")),
        _tool_message(_tool("sommaire", "t2", doc_id="d")),
    ])
    assert summary_only.blocs == [] and summary_only.truncated


async def test_outils_primary_and_automatic_definition_are_one_atomic_unit() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Le premier cas emploie Alpha.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Le second cas reste autonome.", loc="p1", seq=2),
        Block(block_id="d:p9:1", text="Alpha désigne la première notion.", loc="p9", seq=1,
              kind="definition", defines="alpha"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n"), NodeRef(node_id="defs")]),
             Node(node_id="n", items=[BlockRef(block_id="d:p1:1"), BlockRef(block_id="d:p1:2")]),
             Node(node_id="defs", items=[BlockRef(block_id="d:p9:1")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n"})
    result, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("ouvrir_noeud", "t1", node_id="n")),
    ], corpus=corpus, parsed=_parsed(["second"]),
        budget=_budget(max_blocks=1, max_tokens=6000))
    assert result.opened_block_ids == ["d:p1:2"]
    assert "d:p1:1" not in result.opened_block_ids and "d:p9:1" not in result.opened_block_ids
    assert result.truncated


async def test_definitions_tool_only_serializes_question_or_opened_terms_and_one_level_refs() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Le passage emploie Alpha.", loc="p1", seq=1,
              refs=["d:p2:1"]),
        Block(block_id="d:p1:2", text="Page suivante.", loc="p1", seq=2),
        Block(block_id="d:p2:1", text="Premier renvoi.", loc="p2", seq=1,
              refs=["d:p3:1"]),
        Block(block_id="d:p3:1", text="Second renvoi interdit.", loc="p3", seq=1),
        Block(block_id="d:p9:1", text="Alpha désigne la notion admise.", loc="p9", seq=1,
              kind="definition", defines="alpha"),
        Block(block_id="d:p9:2", text="Bêta désigne une notion sans lien.", loc="p9", seq=2,
              kind="definition", defines="bêta"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id=n) for n in ("n", "r1", "r2", "defs")]),
             Node(node_id="n", items=[BlockRef(block_id="d:p1:1"), BlockRef(block_id="d:p1:2")]),
             Node(node_id="r1", items=[BlockRef(block_id="d:p2:1")]),
             Node(node_id="r2", items=[BlockRef(block_id="d:p3:1")]),
             Node(node_id="defs", items=[BlockRef(block_id="d:p9:1"),
                                           BlockRef(block_id="d:p9:2")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n"})
    result, _step, fake, _ = await _run_outils([
        _tool_message(
            _tool("ouvrir_noeud", "t1", node_id="n"),
            _tool("definitions", "t2", termes=["alpha", "bêta"],
                  blocs_ouverts=["d:p1:1"])),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["passage"]),
        budget=_budget(node_window=1, max_blocks=10, max_tokens=6000))
    assert {"d:p1:1", "d:p2:1", "d:p9:1"} <= set(result.opened_block_ids)
    assert "d:p9:2" not in result.opened_block_ids and "d:p3:1" not in result.opened_block_ids
    serialized = fake.requests[1]["messages"][-1]["content"][1]["content"]
    assert '"block_id": "d:p2:1"' in serialized
    assert '"block_id": "d:p9:1"' in serialized
    assert "d:p9:2" not in serialized and "d:p3:1" not in serialized


async def test_definitions_tool_empty_opened_blocks_cannot_bypass_scope() -> None:
    """I1 : une liste explicite vide ne recrée jamais le cas « aucun bloc ouvert » d'AD-1."""
    blocks = [
        Block(block_id="d:p1:1", text="Le passage emploie Alpha.", loc="p1", seq=1),
        Block(block_id="d:p9:1", text="Alpha désigne une notion d'une autre portée.",
              loc="p9", seq=1, kind="definition", defines="alpha", scope_node_id="n2"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p9:1")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n1 > n2"})

    result, _step, _fake, _ = await _run_outils([
        _tool_message(
            _tool("ouvrir_noeud", "t1", node_id="n1"),
            _tool("definitions", "t2", termes=["alpha"], blocs_ouverts=[]),
        ),
    ], corpus=corpus, parsed=_parsed(["alpha"]))

    assert result.opened_block_ids == ["d:p1:1"]
    assert "d:p9:1" not in result.opened_block_ids


async def test_outils_invalid_open_still_consumes_the_global_open_quota() -> None:
    result, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("ouvrir_noeud", "t1", node_id="inconnu")),
        _tool_message(_tool("ouvrir_noeud", "t2", node_id="n1")),
    ], budget=_budget(max_opens=1, max_blocks=30, max_tokens=6000))
    assert result.opened_block_ids == [] and result.truncated


async def test_outils_multiple_searches_union_candidates_and_discard_only_unopened_hits() -> None:
    result, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["matricule"])),
        _tool_message(_tool("chercher", "t2", termes=["contenu"]),
                      _tool("ouvrir_noeud", "t3", node_id="n1")),
    ])
    assert result.discarded_block_ids == ["d:p2:1", "d:p3:1"]
    (check,) = [c for c in _step.checks if c.name == "candidats_non_ouverts"]
    assert check.ok is False and "2 candidat(s)" in check.detail
    assert result.truncated is False
    # La cible voisine du renvoi est transmise mais n'était pas un hit : jamais "écartée".
    assert "d:p1:5" in result.opened_block_ids and "d:p1:5" not in result.discarded_block_ids


async def test_outils_prioritizes_profile_nodes_and_reports_actual_contribution() -> None:
    """I2 : le profil voyage par `scope.noeuds`, guide la navigation et reste vérifiable en trace."""
    result, step, fake, _ = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["matricule"]),
                      _tool("ouvrir_noeud", "t2", node_id="n2")),
    ], parsed=_parsed(["matricule"], noeuds=["n2"]),
        budget=_budget(profil_max_opens=2, max_blocks=30, max_tokens=6000))

    assert "n2" in fake.requests[0]["system"][0]["text"]
    assert "scope.noeuds" in fake.requests[0]["system"][0]["text"]
    assert "d:p2:1" in result.opened_block_ids
    (check,) = [c for c in step.checks if c.name == "noeuds_du_profil"]
    assert check.ok is True and "n2" in check.detail


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "pause_turn"])
async def test_outils_abnormal_tool_turn_end_is_always_truncated(stop_reason: str) -> None:
    result, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["matricule"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1"), stop_reason=stop_reason),
    ])
    assert result.opened_block_ids and result.truncated


@pytest.mark.parametrize(
    ("searched", "truncated"),
    [([], True), (["zorbule"], False), (["zorbule", "quuxite"], False)],
)
async def test_outils_zero_hit_requires_all_canonical_terms_to_have_been_searched(
        searched: list[str], truncated: bool) -> None:
    actual = [f"{term}-absent" for term in searched]
    result, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=actual)),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], parsed=_parsed(["zorbule-absent", "quuxite-absent"]))
    assert result.blocs == [] and result.truncated is truncated


async def test_outils_rejects_focus_that_was_not_a_search_candidate() -> None:
    result, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["matricule"]),
                      _tool("ouvrir_noeud", "t2", node_id="n3", focus_block_id="d:p3:1")),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ])
    assert result.opened_block_ids == [] and result.truncated


async def test_outils_deduplicates_repeated_windows_and_dependencies() -> None:
    result, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["matricule"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1"),
                      _tool("ouvrir_noeud", "t3", node_id="n1")),
    ])
    assert result.opened_block_ids == ["d:p1:1", "d:p1:2", "d:p1:5"]


async def test_outils_refuses_foreign_document_without_leaking_it() -> None:
    other = _linear_doc(1, doc_id="e", prefix="e-")
    base = _corpus()
    corpus = Corpus(documents={"d": base.documents["d"], "e": other},
                    summaries={"d": "root", "e": "secret other document"})
    result, _step, fake, _ = await _run_outils([
        _tool_message(_tool("ouvrir_noeud", "t1", node_id="e-n")),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus)
    assert result.blocs == [] and result.truncated
    sent = str(fake.requests[1]["messages"])
    assert "secret other document" not in sent and "e:p1:1" not in sent


async def test_outils_never_follows_a_reference_chain_beyond_one_level() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Départ", loc="p1", seq=1, refs=["d:p2:1"]),
        Block(block_id="d:p2:1", text="Premier renvoi", loc="p2", seq=1, refs=["d:p3:1"]),
        Block(block_id="d:p3:1", text="Second renvoi interdit", loc="p3", seq=1),
    ]
    nodes = [Node(node_id="root", title="Sommaire", items=[
                 NodeRef(node_id="n"), NodeRef(node_id="n2"), NodeRef(node_id="n3")]),
             Node(node_id="n", title="Départ", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", title="Renvoi 1", items=[BlockRef(block_id="d:p2:1")]),
             Node(node_id="n3", title="Renvoi 2", items=[BlockRef(block_id="d:p3:1")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)},
        summaries={"d": "root > n"})
    result, _step, _fake, _ = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["départ"]),
                      _tool("ouvrir_noeud", "t2", node_id="n")),
    ], corpus=corpus, parsed=_parsed(["départ"]))
    assert result.opened_block_ids == ["d:p1:1", "d:p2:1"]
    assert "d:p3:1" not in result.opened_block_ids and not result.truncated


def test_nominal_opens_candidate_nodes_follows_refs_and_reads_blocks_from_the_corpus() -> None:
    corpus = _corpus()
    index = Index(corpus)
    result, step = _run(_parsed(["matricule"]), corpus, index)
    # n1 et n2 ouverts (fenêtres complètes), puis la cible du renvoi de d:p1:2 — hors quota
    assert result.opened_block_ids == ["d:p1:1", "d:p1:2", "d:p2:1", "d:p1:5"]
    assert result.truncated is False and result.discarded_block_ids == []
    # blocs relus depuis le corpus (mêmes objets), jamais modifiés
    doc = corpus.documents["d"]
    assert all(b is doc.block(b.block_id) for b in result.blocs)
    assert step.name == "retrouver" and step.ms >= 0
    assert step.opened_block_ids == result.opened_block_ids
    assert step.discarded_block_ids == result.discarded_block_ids


def test_the_trace_carries_the_ad9_tier_and_says_no_model_was_called() -> None:
    # AD-9 fixe l'affectation étape → tier **sans exception** (`retrouver → reason`) ; c'est `calls=[]`
    # qui dit que la variante déterministe n'appelle aucun modèle (revue Codex 1.4, B3).
    corpus = _corpus()
    _, step = _run(_parsed(["matricule"]), corpus, Index(corpus))
    assert step.tier == STEP_TIERS["retrouver"] == "reason"
    assert step.calls == [] and step.usage.cost_eur == 0.0


def test_bounded_by_max_opens_discards_unopened_hits_and_truncates() -> None:
    corpus = _corpus()
    result, _ = _run(_parsed(["matricule"]), corpus, Index(corpus), _budget(max_opens=1))
    assert result.opened_block_ids == ["d:p1:1", "d:p1:2", "d:p1:5"]  # n1 seul + renvoi hors quota
    assert result.truncated is True
    assert result.discarded_block_ids == ["d:p2:1"]  # hit non ouvert


def test_definitions_of_terms_come_in_beyond_the_quota() -> None:
    corpus = _corpus()
    result, _ = _run(_parsed(["matricule", "contenu"]), corpus, Index(corpus), _budget(max_opens=1))
    # n1 ouvert (meilleur score) ; la définition « contenu » entre hors quota même si son nœud
    # candidat (n3) est au-delà de max_opens
    assert "d:p3:1" in result.opened_block_ids
    assert result.truncated is True
    assert set(result.discarded_block_ids) == {"d:p2:1"}


def test_definitions_of_terms_met_in_the_opened_blocks_are_followed_too() -> None:
    # AD-1 : « les définitions des termes rencontrés dans les blocs ouverts » — pas seulement des
    # termes de la question (revue Codex 1.4, B2). Ici « contenu » n'est *pas* cherché : il apparaît
    # dans le bloc ouvert, et sa définition doit suivre.
    blocks = [
        Block(block_id="d:p1:1", text="La garantie couvre le contenu de votre habitation.", loc="p1", seq=1),
        Block(block_id="d:p9:1", text="Le contenu désigne les meubles du logement.", loc="p9", seq=1,
              kind="definition", defines="contenu"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="ndef")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="ndef", items=[BlockRef(block_id="d:p9:1")])]
    corpus = Corpus(documents={"d": Document(doc_id="d", kind="contrat", title="t", edition="e",
                                             nodes=nodes, blocks=blocks)})
    result, _ = _run(_parsed(["garantie"]), corpus, Index(corpus))
    assert result.opened_block_ids == ["d:p1:1", "d:p9:1"]
    assert result.discarded_block_ids == []  # la définition n'est pas un candidat de `chercher`


def test_deterministic_definition_is_atomic_with_the_primary_block() -> None:
    blocks = [
        Block(block_id="d:p1:1", text="Le premier cas emploie Alpha.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Le second cas reste autonome.", loc="p1", seq=2),
        Block(block_id="d:p9:1", text="Alpha désigne la première notion.", loc="p9", seq=1,
              kind="definition", defines="alpha"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n"), NodeRef(node_id="defs")]),
             Node(node_id="n", items=[BlockRef(block_id="d:p1:1"), BlockRef(block_id="d:p1:2")]),
             Node(node_id="defs", items=[BlockRef(block_id="d:p9:1")])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)})
    result, _step = _run(_parsed(["cas"]), corpus, Index(corpus),
                         _budget(max_blocks=1, max_tokens=6000))
    # Le premier bloc rencontre « Alpha » et voyage donc avec sa définition. L'unité de deux blocs
    # ne tient pas ; la fiche suivante, indépendante et plus petite, peut encore être admise.
    assert result.opened_block_ids == ["d:p1:2"]
    assert "d:p1:1" not in result.opened_block_ids and "d:p9:1" not in result.opened_block_ids
    assert result.truncated


def test_deterministic_opens_a_directly_requested_definition_when_text_has_no_hit() -> None:
    """Le pré-contrôle et le retrieval partagent la même frontière sur une définition seule."""
    blocks = [
        Block(block_id="d:p1:1", text="Le mobilier voyage avec le logement.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Ce bloc explique le mot défini sans le répéter.",
              loc="p1", seq=2, kind="definition", defines="contenu"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]), Node(
        node_id="n", items=[BlockRef(block_id=block.block_id) for block in blocks])]
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)})
    index = Index(corpus)

    assert index.chercher(["contenu"], limit=1, doc_id="d") == []
    result, _step = _run(_parsed(["contenu"]), corpus, index, doc_id="d")

    assert result.opened_block_ids == ["d:p1:2"]
    assert result.truncated is False


def test_a12_keeps_the_rc_clause_in_the_real_deterministic_claim_retrieval() -> None:
    settings = _s()
    corpus = load_corpus(ROOT / "data", allow_ungated=True)
    doc_id = settings.sinistre_doc_id
    question = ("Le fils du voisin a cassé ma table en jouant chez moi, c'est leur RC ou mon "
                "contrat habitation qui répond ?")
    parsed = ParsedQuestion(
        question_resolue=question,
        intent="question",
        terms=[
            "responsabilité civile vie privée dommages matériels causés accidentellement à des tiers",
            "mobilier",
        ],
    )

    result, _step = retrouver_deterministe(
        parsed, corpus=corpus, index=Index(corpus), budget=retrieval_budget(settings),
        settings=settings, doc_id=doc_id, kinds_prioritaires=KINDS_DECISIONNELS,
    )

    assert f"{doc_id}:p66:10" in result.opened_block_ids


def test_truncated_window_marks_the_result() -> None:
    blocks = [Block(block_id=f"d:p1:{i}", text="terme cherché" if i == 5 else f"bloc {i}", loc="p1", seq=i)
              for i in range(1, 9)]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n")]),
             Node(node_id="n", items=[BlockRef(block_id=b.block_id) for b in blocks])]
    corpus = Corpus(documents={"d": Document(doc_id="d", kind="guide", title="t", edition="e",
                                             nodes=nodes, blocks=blocks)})
    result, _ = _run(_parsed(["cherché"]), corpus, Index(corpus), _budget(node_window=3))
    assert result.truncated is True  # nœud > node_window : fenêtre coupée, pas de pagination
    assert "d:p1:5" in result.opened_block_ids  # la fenêtre contient le meilleur hit du nœud
    assert len(result.opened_block_ids) == 3


def test_zero_hit_returns_an_empty_result_without_asserting_absence() -> None:
    corpus = _corpus()
    index = Index(corpus)
    for parsed in (_parsed(["zzz-introuvable"]), _parsed([])):
        result, step = _run(parsed, corpus, index)
        assert result.blocs == [] and result.opened_block_ids == [] and result.discarded_block_ids == []
        assert result.truncated is False  # aucune absence affirmée : c'est l'affaire de *vérifier* (1.5)
        assert step.calls == []


def test_scope_themes_enrich_the_search() -> None:
    corpus = _corpus()
    index = Index(corpus)
    by_terms, _ = _run(_parsed(["matricule"]), corpus, index)
    by_themes, _ = _run(_parsed([], themes=["matricule"]), corpus, index)
    assert by_themes.opened_block_ids == by_terms.opened_block_ids


def test_un_seul_mot_dun_terme_compose_ouvre_deja_les_blocs_pertinents() -> None:
    """Story 2.7 / R2 : une couverture partielle atteint la chaîne fixe de retrieval."""
    corpus = _corpus()
    result, step = _run(_parsed(["matricule national"]), corpus, Index(corpus))

    assert result.opened_block_ids[:3] == ["d:p1:1", "d:p1:2", "d:p2:1"]
    assert result.blocs and step.name == "retrouver"


def test_les_formulations_livrees_ouvrent_les_deux_fiches_du_corpus_reel() -> None:
    s = _s()
    corpus = load_corpus(ROOT / "data", allow_ungated=True,
                         perimetre_max_chars=s.perimetre_max_chars)
    index = Index(corpus)
    dictionnaire = load_dictionary(ROOT / "data", corpus, s.guide_doc_id)
    budget = RetrievalBudget(max_opens=s.max_opens, node_window=s.node_window,
                             search_limit=s.search_limit, max_llm_turns=s.max_llm_turns,
                             max_blocks=s.retrieval_max_blocks, max_tokens=s.retrieval_max_tokens)
    attendus = {
        "choix commune": "lux-guide:fchoisir_commune",
        "quelle commune": "lux-guide:fchoisir_commune",
        "trouver logement": "lux-guide:frecherche_logement",
        "recherche logement": "lux-guide:frecherche_logement",
        "chercher un appart": "lux-guide:frecherche_logement",
        "où loger": "lux-guide:frecherche_logement",
        "marché immobilier": "lux-guide:frecherche_logement",
    }

    assert dictionnaire.utilisable_pour(s.guide_doc_id)
    for terme, fiche in attendus.items():
        result, _step = _run(_parsed([terme]), corpus, index, budget, settings=s,
                             doc_id=s.guide_doc_id, dictionnaire=dictionnaire)
        assert any(block_id.startswith(f"{fiche}:") for block_id in result.opened_block_ids), terme


def test_max_blocks_skips_whole_units_and_keeps_the_budget_busy() -> None:
    # Revue 1.4 : la borne de coût ne coupe pas d'abord les renvois (ajoutés en dernier), et depuis la
    # revue Codex 1.4 (B6) elle ne sépare plus une cible de son référent : elle saute l'unité entière
    # qui ne tient pas, puis essaie les suivantes.
    corpus = _corpus()
    result, step = _run(_parsed(["matricule"]), corpus, Index(corpus), _budget(max_blocks=2))
    assert result.opened_block_ids == ["d:p1:1", "d:p2:1"]  # l'unité (d:p1:2 + sa cible) ne tient pas
    assert result.truncated is True
    # AD-10 : `discarded_block_ids` = candidats de `chercher` non transmis. Les deux hits sont passés.
    assert result.discarded_block_ids == []
    assert step.discarded_block_ids == result.discarded_block_ids


def test_a_ref_target_never_travels_without_the_block_that_cites_it() -> None:
    # AD-1 (revue Codex 1.4, B6) : une cible de renvoi seule est inutilisable — le passage qui la rend
    # pertinente manque, et la rédaction peut citer hors contexte.
    corpus = _corpus()
    index = Index(corpus)
    for max_blocks in (1, 2, 3, 4):
        result, _ = _run(_parsed(["matricule"]), corpus, index, _budget(max_blocks=max_blocks))
        rendus = set(result.opened_block_ids)
        assert len(rendus) <= max_blocks
        if "d:p1:5" in rendus:  # la cible n'entre que si son référent est là
            assert "d:p1:2" in rendus


def test_a_window_block_is_kept_on_its_own_merit_even_when_another_one_cites_it() -> None:
    """Revue Codex 1.4, B6, tour 2 — rejet argumenté. L'invariant d'AD-1 porte sur les blocs tirés
    **par** un renvoi (« *retrouver* suit automatiquement les renvois … un niveau ») : une cible n'est
    utile qu'avec le passage qui la cite. Un bloc de fenêtre, lui, est retenu par la recherche
    elle-même — c'est un hit de `chercher`, ou son voisinage de lecture dans le nœud ouvert. Qu'un
    autre bloc le cite ne le transforme pas en dépendance : le compter dans l'unité de son référent
    ferait *retirer* un bloc légitimement retrouvé parce qu'un lien pointe vers lui, et rendrait ici
    un résultat vide là où un hit tenait dans le budget."""
    blocks = [
        Block(block_id="d:p1:1", text="Le matricule est délivré par la commune ; voir aussi.", loc="p1",
              seq=1, refs=["d:p2:1", "d:p9:1"]),
        Block(block_id="d:p2:1", text="Le matricule sert partout.", loc="p2", seq=1),
        Block(block_id="d:p9:1", text="Cible du renvoi, sans aucun terme cherché.", loc="p9", seq=1),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2"), NodeRef(node_id="n3")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p2:1")]),
             Node(node_id="n3", items=[BlockRef(block_id="d:p9:1")])]
    corpus = Corpus(documents={"d": Document(doc_id="d", kind="guide", title="t", edition="e",
                                             nodes=nodes, blocks=blocks)})
    index = Index(corpus)
    # les deux premiers blocs sont des hits ; `d:p1:1` cite l'un d'eux et une cible hors recherche
    assert [b for b, _ in index.chercher(["matricule"], limit=20)] == ["d:p1:1", "d:p2:1"]

    result, _ = _run(_parsed(["matricule"]), corpus, index, _budget(max_blocks=1))
    # l'unité de `d:p1:1` (lui + `d:p9:1`, sa cible hors fenêtre) ne tient pas dans 1 bloc ; `d:p2:1`
    # tient, et c'est un hit — le rendre est plus utile que ne rien rendre.
    assert result.opened_block_ids == ["d:p2:1"] and result.truncated is True
    # l'invariant reste tenu à toute borne : aucune cible tirée par un renvoi ne voyage seule.
    for max_blocks in (1, 2, 3):
        r, _ = _run(_parsed(["matricule"]), corpus, index, _budget(max_blocks=max_blocks))
        rendus = set(r.opened_block_ids)
        assert len(rendus) <= max_blocks
        if "d:p9:1" in rendus:  # `d:p9:1` n'est ni un hit ni un bloc de fenêtre : c'est une cible
            assert "d:p1:1" in rendus


def test_max_tokens_bounds_the_step_where_the_block_count_does_not() -> None:
    # AD-1 : le `RetrievalBudget` borne « blocs, tokens … inclus » (revue Codex 1.4, B1). Deux blocs
    # très longs tiennent dans `max_blocks` et défoncent pourtant le budget de tokens.
    long_text = "matricule " + "texte de remplissage. " * 200  # ≈ 4 500 caractères
    blocks = [Block(block_id=f"d:p{i}:1", text=long_text, loc=f"p{i}", seq=1) for i in (1, 2, 3)]
    nodes = [Node(node_id="root", items=[NodeRef(node_id=f"n{i}") for i in (1, 2, 3)])] + \
        [Node(node_id=f"n{i}", items=[BlockRef(block_id=f"d:p{i}:1")]) for i in (1, 2, 3)]
    corpus = Corpus(documents={"d": Document(doc_id="d", kind="guide", title="t", edition="e",
                                             nodes=nodes, blocks=blocks)})
    index = Index(corpus)
    libre, _ = _run(_parsed(["matricule"]), corpus, index, _budget(max_blocks=10))
    assert len(libre.blocs) == 3 and libre.truncated is False  # le compte de blocs ne borne rien

    borne, _ = _run(_parsed(["matricule"]), corpus, index, _budget(max_blocks=10, max_tokens=7000))
    assert len(borne.blocs) == 2 and borne.truncated is True
    s = _s()
    total = sum(estimate_tokens(f"{b.block_id}\n{b.text}", s) for b in borne.blocs)
    assert total <= 7000


def test_on_the_real_corpus_with_config_thresholds() -> None:
    s = _s()
    corpus = load_corpus(ROOT / "data", allow_ungated=True)
    index = Index(corpus)
    budget = RetrievalBudget(max_opens=s.max_opens, node_window=s.node_window,
                             search_limit=s.search_limit, max_llm_turns=s.max_llm_turns,
                             max_blocks=s.retrieval_max_blocks, max_tokens=s.retrieval_max_tokens)
    result, step = _run(_parsed(["matricule", "commune"]), corpus, index, budget, settings=s,
                        doc_id="lux-guide")
    assert result.blocs and all(b.block_id.startswith("lux-guide:") for b in result.blocs)
    assert any(b.block_id.startswith("lux-guide:farrivee:") for b in result.blocs)  # la fiche attendue
    assert result.opened_block_ids == [b.block_id for b in result.blocs]
    assert set(result.discarded_block_ids).isdisjoint(result.opened_block_ids)
    assert step.calls == [] and step.tier == "reason"
    # définitions réelles : le terme « contenu » ramène la définition AXA hors quota
    axa, _ = _run(_parsed(["contenu"]), corpus, index, budget, settings=s, doc_id="axa-lu-optihome-2017")
    assert "axa-lu-optihome-2017:p9:2" in axa.opened_block_ids
    # …et le saut « définitions » reste dans le document demandé : interrogé sur le guide, le même
    # terme ne ramène jamais la définition du contrat (revue 1.4 — sans `doc_id`, elle passait).
    guide, _ = _run(_parsed(["contenu"]), corpus, index, budget, settings=s, doc_id="lux-guide")
    assert all(b.block_id.startswith("lux-guide:") for b in guide.blocs)


def test_unknown_doc_id_raises_like_the_index() -> None:
    corpus = _corpus()
    index = Index(corpus)
    # y compris sans aucun terme : `chercher` n'est alors pas appelé, mais un doc_id fautif reste une
    # faute de l'appelant — la taire rendrait une faute de frappe invisible (revue 1.4).
    for parsed in (_parsed(["matricule"]), _parsed([])):
        with pytest.raises(KeyError):
            _run(parsed, corpus, index, doc_id="inconnu")


def test_decisional_kinds_are_a_tie_break_never_a_filter() -> None:
    """Story 1.8 / D7 : AC « cherche les blocs `garantie|exclusion|condition|franchise` candidats ».

    L'étape se contente de transmettre le départage à l'index (là où le score existe) : ce qui compte
    ici, c'est qu'aucun bloc ne **disparaisse** parce qu'il n'est pas une clause. À J+1 le typage est
    manuel et ne couvre que quatre blocs du contrat AXA — filtrer viderait le rappel.
    """
    blocks = [
        Block(block_id="d:p1:1", text="Le mobilier de jardin est un paragraphe ordinaire.", loc="p1", seq=1),
        Block(block_id="d:p1:2", text="Le mobilier assuré est garanti par la présente clause.", loc="p1",
              seq=2, kind="garantie", kind_source="manual"),
    ]
    nodes = [Node(node_id="root", items=[NodeRef(node_id="n1"), NodeRef(node_id="n2")]),
             Node(node_id="n1", items=[BlockRef(block_id="d:p1:1")]),
             Node(node_id="n2", items=[BlockRef(block_id="d:p1:2")])]
    corpus = Corpus(documents={"d": Document(doc_id="d", kind="contrat", title="t", edition="e",
                                             nodes=nodes, blocks=blocks)})
    index = Index(corpus)
    parsed = _parsed(["mobilier"])
    sans = _run(parsed, corpus, index)[0]
    avec, step = retrouver_deterministe(parsed, corpus=corpus, index=index, budget=_budget(),
                                        settings=_s(), doc_id="d",
                                        kinds_prioritaires={"garantie", "exclusion"})
    # les deux blocs sont là dans les deux cas : le typage ne filtre rien
    assert {b.block_id for b in sans.blocs} == {b.block_id for b in avec.blocs} == {"d:p1:1", "d:p1:2"}
    # mais la clause est ouverte la première quand la priorité est donnée
    assert [b.block_id for b in avec.blocs][0] == "d:p1:2"
    assert [b.block_id for b in sans.blocs][0] == "d:p1:1"
    assert step.calls == []  # toujours du code pur (AD-1)


# --- story 2.1 : l'élargissement par le dictionnaire (AD-5) ------------------

# Le document du corpus miniature de cette section, et celui auquel le dictionnaire est lié
# (revue Codex 2.1, B3 : un dictionnaire ne vaut que pour le document dont il porte l'empreinte).
DICO_DOC = "d"

def _dictionnaire(tmp_path: Path, corpus: Corpus, termes: dict[str, list[str]], *,
                  validated: bool = False, source_hash: str | None = None):
    """Un `Dictionnaire` chargé depuis un fichier réel : jamais un objet fabriqué à la main.

    C'est ce que le serveur fait au démarrage, et c'est le seul chemin qui exerce à la fois le schéma
    du fichier, la comparaison des empreintes et l'indexation des formes.
    """
    import json

    from server.app.corpus.dictionary import load_dictionary
    from server.app.corpus.racine import _lecture_interne_sans_racine

    # `tmp_path` et non `tempfile.mkdtemp()` : ces helpers sont appelés en boucle, et des dossiers
    # jamais nettoyés s'accumulaient dans `/tmp` à chaque exécution de la suite.
    dossier = tmp_path / f"dico-{len(termes)}-{validated}-{abs(hash((source_hash, tuple(sorted(termes)))))}"
    dossier.mkdir(parents=True, exist_ok=True)
    hashes = {d: (source_hash if source_hash is not None else corpus.manifest[d].source_hash)
              for d in corpus.served}
    signature = ({"validated": True, "validated_by": "Lancelot Oudin",
                  "validated_at": "2026-08-25T10:00:00Z"} if validated else
                 {"validated": False, "validated_by": None, "validated_at": None})
    (dossier / "dictionary.json").write_text(json.dumps(
        {"schema_version": "1", "corpus_source_hashes": hashes, "corpus": termes,
         "intents": {}, "candidate_questions": {}, **signature}, ensure_ascii=False), "utf-8")
    with _lecture_interne_sans_racine(dossier) as lecture:
        return load_dictionary(dossier, corpus, DICO_DOC, lecture=lecture)


async def test_outils_traces_dictionary_expansion_for_terms_actually_searched(tmp_path: Path) -> None:
    corpus = _corpus_avec_manifest()
    dico = _dictionnaire(tmp_path, corpus, {"matricule": ["social security number"]})
    _result, step, _fake, _budget_used = await _run_outils(
        [_tool_message(_tool("chercher", "t1", termes=["contenu"])),
         fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[])], corpus=corpus,
        parsed=_parsed(["social security number"]), dictionnaire=dico)
    check = next(c for c in step.checks if c.name == "dictionnaire")
    assert check.detail == "1 variante(s) ajoutée(s) à 1 terme(s)"


def _corpus_avec_manifest() -> Corpus:
    from server.app.domain.ingest import ManifestEntry

    corpus = _corpus()
    corpus.manifest["d"] = ManifestEntry(status="servi", source_hash="sha-source",
                                         ingest_fingerprint="fp", document_hash="sha-doc",
                                         edition="e")
    return corpus


def test_une_variante_ouvre_la_fiche_du_canonique(tmp_path: Path) -> None:
    """AC 2.1 : « une question dont un terme français est une variante d'un canonique du guide ⇒ la
    fiche du canonique figure dans `RetrievalResult.opened_block_ids` »."""
    corpus = _corpus_avec_manifest()
    dico = _dictionnaire(tmp_path, corpus, {"matricule": ["numéro de sécurité sociale"]})
    assert dico.utilisable is True

    # Sans dictionnaire, ce terme ne touche rien : c'est exactement le faux refus qu'AD-5 vise.
    nu, _ = _run(_parsed(["numéro de sécurité sociale"]), corpus, Index(corpus))
    assert nu.opened_block_ids == []

    result, step = retrouver_deterministe(
        _parsed(["numéro de sécurité sociale"]), corpus=corpus, index=Index(corpus),
        budget=_budget(), settings=_s(), doc_id=DICO_DOC, dictionnaire=dico)
    assert "d:p1:1" in result.opened_block_ids  # la fiche du canonique « matricule »
    (check,) = [c for c in step.checks if c.name == "dictionnaire"]
    assert check.ok is True and check.detail == "1 variante(s) ajoutée(s) à 1 terme(s)"


def test_la_trace_dit_le_nombre_de_variantes_jamais_lesquelles(tmp_path: Path) -> None:
    """AD-4 : `AbsenceProof` ne porte jamais la liste des variantes ; AD-10 : la trace non plus.

    Publier les formes ajoutées ferait fuir le dictionnaire terme par terme, dans un canal que le
    front « pourquoi cette réponse » affiche.
    """
    corpus = _corpus_avec_manifest()
    dico = _dictionnaire(tmp_path, corpus, {"matricule": ["numero national", "social security number"]})
    _result, step = retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=Index(corpus),
                                           budget=_budget(), settings=_s(), doc_id=DICO_DOC, dictionnaire=dico)
    detail = next(c.detail for c in step.checks if c.name == "dictionnaire")
    assert detail == "2 variante(s) ajoutée(s) à 1 terme(s)"
    for variante in ("numero national", "social security number"):
        assert variante not in step.model_dump_json()


def test_un_dictionnaire_inutilisable_ne_change_rien_du_tout(tmp_path: Path) -> None:
    """`corpus_ok` commande les variantes : un dictionnaire d'un **autre** corpus ne dit rien de
    celui-ci, et *retrouver* fait exactement ce qu'il faisait avant cette story — sans check."""
    corpus = _corpus_avec_manifest()
    perime = _dictionnaire(tmp_path, corpus, {"matricule": ["numéro de sécurité sociale"]},
                           source_hash="empreinte-dun-autre-corpus")
    assert perime.charge is True and perime.utilisable is False

    attendu, _ = _run(_parsed(["numéro de sécurité sociale"]), corpus, Index(corpus))
    result, step = retrouver_deterministe(
        _parsed(["numéro de sécurité sociale"]), corpus=corpus, index=Index(corpus),
        budget=_budget(), settings=_s(), doc_id=DICO_DOC, dictionnaire=perime)
    assert result.opened_block_ids == attendu.opened_block_ids == []
    assert [c.name for c in step.checks] == []


def test_les_variantes_servent_sans_validation_humaine(tmp_path: Path) -> None:
    """Design Note 2.1 : `validated` ne commande **que** le court-circuit d'AD-5, pas l'élargissement.

    Élargir n'affirme rien — chaque phrase affichée reste vérifiée contre le corpus (AD-3) ; refuser
    est une affirmation négative, et c'est elle que la signature humaine garde.
    """
    corpus = _corpus_avec_manifest()
    dico = _dictionnaire(tmp_path, corpus, {"matricule": ["numéro de sécurité sociale"]}, validated=False)
    assert (dico.utilisable, dico.court_circuit_actif) == (True, False)
    result, _step = retrouver_deterministe(
        _parsed(["numéro de sécurité sociale"]), corpus=corpus, index=Index(corpus),
        budget=_budget(), settings=_s(), doc_id=DICO_DOC, dictionnaire=dico)
    assert "d:p1:1" in result.opened_block_ids


def test_les_definitions_continuent_de_recevoir_les_termes_seuls(tmp_path: Path) -> None:
    """`definitions()` reçoit toujours les termes canoniques, jamais leurs variantes.

    La fermeture les demande désormais pour chaque bloc primaire : toutes les invocations doivent
    conserver cette même frontière lexicale.
    """
    corpus = _corpus_avec_manifest()
    index = Index(corpus)
    vus: list = []
    reel = index.definitions

    def espion(termes, **kw):
        vus.append(termes)
        return reel(termes, **kw)

    index.definitions = espion  # type: ignore[method-assign]
    dico = _dictionnaire(tmp_path, corpus, {"matricule": ["contenu"]})
    retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=index, budget=_budget(),
                           settings=_s(), doc_id=DICO_DOC, dictionnaire=dico)
    assert vus and all(termes == ["matricule"] for termes in vus)  # jamais le dict élargi


def test_un_dictionnaire_nelargit_que_le_document_dont_il_porte_lempreinte(tmp_path: Path) -> None:
    """Revue Codex 2.1 (B3) : `utilisable` ne suffisait pas — c'est `utilisable_pour(doc_id)` qui décide.

    Le même dictionnaire, la même question : il élargit la recherche du document qu'il décrit, et
    **rien** ailleurs. Une recherche sans `doc_id` — sur tout le corpus — n'est reconnue par aucun
    dictionnaire : rien ne dit que les autres documents sont ceux qu'il décrit, et le vocabulaire
    d'un guide d'installation ouvrirait des blocs de contrat au hasard.
    """
    corpus = _corpus_avec_manifest()
    index = Index(corpus)
    dico = _dictionnaire(tmp_path, corpus, {"matricule": ["social security number"]})
    assert dico.utilisable is True and dico.utilisable_pour(DICO_DOC) is True

    ouvert, step = retrouver_deterministe(_parsed(["social security number"]), corpus=corpus,
                                          index=index, budget=_budget(), settings=_s(),
                                          doc_id=DICO_DOC, dictionnaire=dico)
    assert "d:p1:1" in ouvert.opened_block_ids and [c.name for c in step.checks] == ["dictionnaire"]

    # Sans `doc_id` : ni élargissement, ni `CheckResult` — exactement l'avant-2.1 (AD-16 : rien
    # n'est annoncé qui n'ait été fait).
    tout, step_tout = retrouver_deterministe(_parsed(["social security number"]), corpus=corpus,
                                             index=index, budget=_budget(), settings=_s(),
                                             dictionnaire=dico)
    assert tout.opened_block_ids == [] and [c.name for c in step_tout.checks] == []


def test_un_terme_anglais_ouvre_la_fiche_francaise(tmp_path: Path) -> None:
    """L'AC nommait le cas anglais ; les tests l'exerçaient en français et en allemand.

    Ici le terme cherché n'existe **nulle part** dans le corpus français, et c'est bien la fiche du
    canonique qui s'ouvre — la propriété que la story promet à un arrivant anglophone.
    """
    corpus = _corpus_avec_manifest()
    index = Index(corpus)
    dico = _dictionnaire(tmp_path, corpus, {"matricule": ["social security number"]})

    nu, _ = _run(_parsed(["social security number"]), corpus, index)
    assert nu.opened_block_ids == []  # sans dictionnaire, l'anglophone ne trouve rien

    result, step = retrouver_deterministe(_parsed(["social security number"]), corpus=corpus,
                                          index=index, budget=_budget(), settings=_s(),
                                          doc_id=DICO_DOC, dictionnaire=dico)
    assert "d:p1:1" in result.opened_block_ids
    assert next(c.detail for c in step.checks if c.name == "dictionnaire") == \
        "1 variante(s) ajoutée(s) à 1 terme(s)"


def test_les_deux_nombres_du_check_se_comptent_avec_la_meme_regle(tmp_path: Path) -> None:
    """Le détail annonçait « 0 variante(s) ajoutée(s) à 2 terme(s) » — une contradiction lisible.

    `variants_count` exclut les formes déjà présentes parmi les termes cherchés (une variante qui
    *est* l'un des termes de la question n'ajoute rien à la recherche) ; le compte de termes touchés,
    lui, ne les excluait pas. Deux règles pour une même phrase (revue coordonnée 2.1).
    """
    corpus = _corpus_avec_manifest()
    index = Index(corpus)
    dico = _dictionnaire(tmp_path, corpus, {"matricule": ["numero national"]})

    # La question cherche **déjà** la variante : rien n'est ajouté, donc aucun terme n'est « touché ».
    _r, step = retrouver_deterministe(_parsed(["matricule", "numero national"]), corpus=corpus,
                                      index=index, budget=_budget(), settings=_s(),
                                      doc_id=DICO_DOC, dictionnaire=dico)
    assert next(c.detail for c in step.checks if c.name == "dictionnaire") == \
        "0 variante(s) ajoutée(s) à 0 terme(s)"

    # Et le cas où elle ne la cherche pas : un terme touché, une forme ajoutée.
    _r2, step2 = retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=index,
                                        budget=_budget(), settings=_s(), doc_id=DICO_DOC, dictionnaire=dico)
    assert next(c.detail for c in step2.checks if c.name == "dictionnaire") == \
        "1 variante(s) ajoutée(s) à 1 terme(s)"


# --- la réserve du profil (story 2.3) ---------------------------------------
def _corpus_range(n: int) -> Corpus:
    """`n` nœuds, un bloc chacun, tous porteurs du terme cherché — donc tous candidats.

    Le score de `chercher` est le nombre de groupes de termes touchés : ils sont tous à égalité, et
    l'ordre des candidats est l'ordre de lecture. C'est ce qui rend le rang de chaque nœud
    prévisible, et donc la réserve observable.
    """
    blocks = [Block(block_id=f"d:p{i}:1", text=f"Le matricule, cas {i}.", loc=f"p{i}", seq=1)
              for i in range(1, n + 1)]
    nodes = [Node(node_id="root", items=[NodeRef(node_id=f"n{i}") for i in range(1, n + 1)])]
    nodes += [Node(node_id=f"n{i}", items=[BlockRef(block_id=f"d:p{i}:1")]) for i in range(1, n + 1)]
    doc = Document(doc_id="d", kind="guide", title="t", edition="e", nodes=nodes, blocks=blocks)
    return Corpus(documents={"d": doc})


def _run_profil(corpus: Corpus, noeuds: list[str] | None, *, max_opens: int = 6,
                profil_max_opens: int = 2, terms: list[str] | None = None, **budget_kw):
    """La réserve se règle **dans le budget**, avec le quota qu'elle borne (revue coordonnée, A4).

    Les nœuds désignés arrivent **dans `ParsedQuestion.scope`** et non par un paramètre parallèle
    (revue Codex 2.3, B1) : c'est le seul laissez-passer vers l'étape (AD-1).
    """
    return retrouver_deterministe(_parsed(terms or ["matricule"], noeuds=noeuds), corpus=corpus,
                                  index=Index(corpus),
                                  budget=_budget(max_opens=max_opens,
                                                 profil_max_opens=profil_max_opens, **budget_kw),
                                  settings=_s())


def test_un_noeud_designe_hors_quota_prend_la_place_du_moins_bien_classe() -> None:
    """AC : la fiche désignée est ouverte même si son meilleur hit la classait hors de `max_opens`,
    et la trace le dit. Elle est ouverte **après** les nœuds de la question : l'ordre des nœuds est
    aussi l'ordre d'admission au budget de blocs, et le profil gagne une place, pas la priorité."""
    corpus = _corpus_range(8)  # n1…n8 candidats, `max_opens=6` : n7 et n8 sont hors quota
    result, step = _run_profil(corpus, ["n7"])
    assert result.opened_block_ids == [f"d:p{i}:1" for i in (1, 2, 3, 4, 5, 7)]
    assert result.opened_block_ids[-1] == "d:p7:1"  # ouvert après les nœuds de la question
    (check,) = [c for c in step.checks if c.name == "noeuds_du_profil"]
    assert check.ok is True
    assert check.detail == "1 place(s) réservée(s) sur 2 (n7) ; 1 nœud(s) cédé(s) (n6)"
    # le nœud cédé n'est pas perdu pour la trace : c'est un candidat de `chercher` non ouvert (AD-10)
    assert "d:p6:1" in result.discarded_block_ids


def test_la_reserve_est_bornee_par_profil_max_opens() -> None:
    """Trois nœuds désignés hors quota, deux places : les **mieux classés** entrent, le troisième non.

    Sans cette borne, un profil complet évincerait la moitié des nœuds que la question a classés en
    tête — ce que « le profil ordonne, il n'ajoute jamais » interdit.
    """
    corpus = _corpus_range(10)
    result, step = _run_profil(corpus, ["n9", "n7", "n10"])
    assert result.opened_block_ids == [f"d:p{i}:1" for i in (1, 2, 3, 4, 7, 9)]
    (check,) = [c for c in step.checks if c.name == "noeuds_du_profil"]
    assert check.detail == "2 place(s) réservée(s) sur 2 (n7, n9) ; 2 nœud(s) cédé(s) (n6, n5)"


def test_une_fiche_designee_deja_ouverte_ne_change_rien() -> None:
    """Matrice : `ecole` candidate au rang 2 — ni l'ordre, ni le nombre de nœuds ouverts ne bougent."""
    corpus = _corpus_range(8)
    temoin, _ = _run_profil(corpus, None)
    result, step = _run_profil(corpus, ["n2"])
    assert result.opened_block_ids == temoin.opened_block_ids
    assert result.truncated == temoin.truncated
    (check,) = [c for c in step.checks if c.name == "noeuds_du_profil"]
    assert check.detail.startswith("aucune place réservée")


def test_une_fiche_designee_sans_hit_nest_jamais_ouverte() -> None:
    """**Le profil ordonne, il n'ajoute jamais** : un nœud sans hit n'est pas candidat, donc rien.

    C'est l'invariant qui empêche le profil de faire entrer une fiche dans le contexte du modèle —
    et donc de faire dire à la réponse ce que la question n'a pas demandé.
    """
    corpus = _corpus_range(3)
    temoin, _ = _run_profil(corpus, None)
    result, step = _run_profil(corpus, ["n404"])
    assert result.opened_block_ids == temoin.opened_block_ids
    (check,) = [c for c in step.checks if c.name == "noeuds_du_profil"]
    assert check.detail.startswith("aucune place réservée")


def test_un_profil_vide_laisse_retrouver_identique_a_lavant_story() -> None:
    """AC : profil vide ⇒ résultat identique, **et** aucune trace ajoutée — le `CheckResult` lui-même
    ne paraît pas, sans quoi « à l'octet près » serait faux pour un lecteur de trace."""
    corpus = _corpus_range(8)
    for noeuds in (None, []):
        result, step = _run_profil(corpus, noeuds)
        assert result.opened_block_ids == [f"d:p{i}:1" for i in range(1, 7)]
        assert [c.name for c in step.checks] == []


def test_profil_max_opens_a_zero_desarme_la_reserve() -> None:
    """Convention Seuils : le seuil se règle, et sa valeur nulle rend le comportement d'avant.

    La trace se tait alors — pas par omission : `Trace.thresholds` publie `profil_max_opens`, et un
    `CheckResult` disant « aucune place réservée » laisserait croire que les nœuds désignés n'étaient
    pas candidats, alors que la réserve est simplement désarmée.
    """
    corpus = _corpus_range(8)
    result, step = _run_profil(corpus, ["n7"], profil_max_opens=0)
    assert result.opened_block_ids == [f"d:p{i}:1" for i in range(1, 7)]
    assert [c.name for c in step.checks] == []


def test_un_noeud_designe_ne_cede_jamais_sa_place_a_un_autre_designe() -> None:
    """`n6` est désigné **et** retenu : le céder pour promouvoir `n7` serait un échange nul, et ferait
    perdre au profil ce que la réserve vient de lui donner. C'est `n5` qui cède."""
    corpus = _corpus_range(8)
    result, step = _run_profil(corpus, ["n6", "n7"], profil_max_opens=1)
    assert result.opened_block_ids == [f"d:p{i}:1" for i in (1, 2, 3, 4, 6, 7)]
    (check,) = [c for c in step.checks if c.name == "noeuds_du_profil"]
    assert check.detail == "1 place(s) réservée(s) sur 1 (n7) ; 1 nœud(s) cédé(s) (n5)"


def test_la_reserve_ne_change_pas_le_nombre_de_noeuds_ouverts_ni_truncated() -> None:
    """`max_opens` reste le nombre de nœuds ouverts : la réserve prend une place, elle n'en ajoute pas.

    `truncated` ne bouge pas non plus — il dit que des candidats sont restés hors quota, et c'est
    toujours le cas : la promotion en échange un contre un.
    """
    corpus = _corpus_range(8)
    temoin, _ = _run_profil(corpus, None)
    result, _step = _run_profil(corpus, ["n7", "n8"])
    assert len(result.opened_block_ids) == len(temoin.opened_block_ids) == 6
    assert result.truncated is temoin.truncated is True


def test_une_place_reservee_pour_rien_est_rendue_au_noeud_qui_lavait_cedee() -> None:
    """Le trou de la matrice (revue coordonnée 2.3, A1 ; revue Codex 2.3, I1) : réserver n'est pas
    occuper, et une place réservée pour rien doit être **rendue**.

    Huit nœuds candidats, `max_opens=6`, deux désignés hors quota, mais `max_blocks=4` : l'unité de
    dépendance des deux promus est écartée comme n'importe quelle autre. Avant le correctif, `n5` et
    `n6` avaient cédé leur place pour rien — le profil **retirait** deux nœuds à la question sans lui
    rendre un seul bloc. Ils sont maintenant restitués, la lecture est refaite, et le résultat est
    celui d'un profil vide : le profil n'ajoute rien, mais il ne retire rien non plus.

    Les huit autres tests de la réserve tournent sur un budget sans `max_blocks`/`max_tokens` : c'est
    précisément ce que celui-ci ajoute (patron de
    `test_max_blocks_skips_whole_units_and_keeps_the_budget_busy`).
    """
    corpus = _corpus_range(8)
    temoin, _ = _run_profil(corpus, None, max_blocks=4)
    result, step = _run_profil(corpus, ["n7", "n8"], max_blocks=4)
    assert result.opened_block_ids == temoin.opened_block_ids == [f"d:p{i}:1" for i in range(1, 5)]
    (check,) = [c for c in step.checks if c.name == "noeuds_du_profil"]
    # Rien n'est perdu, mais rien n'est gagné : le seuil est mal réglé pour ce budget, et `ok=False`
    # le dit plutôt que de laisser croire à une promotion réussie.
    assert check.ok is False
    assert check.detail == ("0 place(s) réservée(s) sur 2 (aucune) ; 0 nœud(s) cédé(s) (aucun) ; "
                            "2 promu(s) sans bloc retenu (n7, n8) : le budget de blocs a écarté "
                            "leur fenêtre, leur place a été rendue (n6, n5)")


def test_la_restitution_dune_place_ne_coute_pas_la_promotion_qui_avait_reussi() -> None:
    """Fenêtres de tailles différentes, budget de blocs serré (revue Codex 2.3, I1) : un promu tient,
    l'autre non — celui qui tient garde sa place, celui qui échoue rend la sienne.

    Le bloc de `n8` est long, tous les autres sont courts, et le budget de tokens vaut exactement six
    blocs courts. Avec la réserve, `n5` et `n6` cèdent leur place à `n7` et `n8` : `n7` entre, la
    fenêtre de `n8` ne tient pas. Avant le correctif, `n5` restait évincé pour cette promotion ratée
    et le retrieval rendait cinq blocs là où le budget en autorisait six. `n5` reprend sa place, et
    `n7` — qui, lui, a bien apporté un bloc — garde la sienne.
    """
    doc = _corpus_range(8).documents["d"]
    court = f"d:p1:1\n{doc.block('d:p1:1').text}"
    blocks = [b if b.block_id != "d:p8:1" else
              Block(block_id="d:p8:1", text="Le matricule, " + "encore et encore, " * 200, loc="p8", seq=1)
              for b in doc.blocks]
    corpus = Corpus(documents={"d": Document(doc_id="d", kind="guide", title="t", edition="e",
                                             nodes=list(doc.nodes), blocks=blocks)})
    tokens = 6 * estimate_tokens(court, _s())

    avant, _ = _run_profil(corpus, None, max_tokens=tokens)
    assert avant.opened_block_ids == [f"d:p{i}:1" for i in range(1, 7)]
    result, step = _run_profil(corpus, ["n7", "n8"], max_tokens=tokens)
    assert result.opened_block_ids == [f"d:p{i}:1" for i in (1, 2, 3, 4, 5, 7)]
    (check,) = [c for c in step.checks if c.name == "noeuds_du_profil"]
    assert check.ok is False  # une promotion sur deux a échoué : le réglage se dit
    assert check.detail == ("1 place(s) réservée(s) sur 2 (n7) ; 1 nœud(s) cédé(s) (n6) ; "
                            "1 promu(s) sans bloc retenu (n8) : le budget de blocs a écarté leur "
                            "fenêtre, leur place a été rendue (n5)")


def test_une_reserve_qui_mangerait_le_quota_est_refusee_a_la_construction() -> None:
    """A4 : `max_opens` et la réserve se lisent au même endroit, et ne peuvent plus se contredire."""
    with pytest.raises(ValueError, match="strictement inférieur"):
        _budget(max_opens=2, profil_max_opens=2)
    assert _budget(max_opens=2, profil_max_opens=1).profil_max_opens == 1


# --- story 4.2f : les nœuds réellement lus, publiés par les deux variantes ------------------------

def _noeuds_attendus(corpus: Corpus, block_ids: list[str]) -> list[str]:
    """Les nœuds propriétaires des blocs transmis, lus sur le corpus — jamais sur le résultat."""
    doc = corpus.documents["d"]
    return list(dict.fromkeys(doc.node_of(b) for b in block_ids))


def test_le_deterministe_publie_le_noeud_de_chaque_bloc_transmis() -> None:
    """Story 4.2f : **tout** bloc transmis compte, y compris entré hors fenêtre.

    Une réponse de lecture partielle chiffre ce qui a été lu ; sans ce champ, elle n'aurait eu que
    des blocs à compter. Et le compte doit couvrir tous les blocs : la cible d'un renvoi (`d:p1:5`,
    nœud `n3`) est bien un passage lu, et l'omettre ferait dire à l'écran « 2 sections lues » pour
    des passages venus de trois — deux chiffres qui se contredisent.
    """
    corpus = _corpus()
    result, _step = _run(_parsed(["matricule"]), corpus, Index(corpus))
    assert result.opened_block_ids == ["d:p1:1", "d:p1:2", "d:p2:1", "d:p1:5"]
    assert result.opened_node_ids == _noeuds_attendus(corpus, result.opened_block_ids)
    assert result.opened_node_ids == ["n1", "n2", "n3"]  # n3 apporte la cible du renvoi
    # Aucun double comptage, et jamais plus de sections que de passages.
    assert len(set(result.opened_node_ids)) == len(result.opened_node_ids)
    assert 1 <= len(result.opened_node_ids) <= len(result.opened_block_ids)


def test_le_deterministe_ne_compte_pas_un_noeud_dont_le_budget_a_ecarte_la_fenetre() -> None:
    """Un compteur d'ouvertures aurait compté des nœuds dont rien n'est entré : la réponse aurait
    annoncé une lecture qui n'a pas eu lieu."""
    corpus = _corpus()
    complet, _s1 = _run(_parsed(["matricule"]), corpus, Index(corpus))
    borne, _s2 = _run(_parsed(["matricule"]), corpus, Index(corpus),
                      _budget(max_blocks=1))
    assert borne.truncated is True
    assert len(borne.opened_node_ids) < len(complet.opened_node_ids)
    assert all(n in complet.opened_node_ids for n in borne.opened_node_ids)
    # Et ce qui reste publié reste exact : les nœuds des seuls blocs réellement transmis.
    assert borne.opened_node_ids == _noeuds_attendus(corpus, borne.opened_block_ids)


def test_le_deterministe_ne_publie_aucun_noeud_quand_rien_nest_transmis() -> None:
    corpus = _corpus()
    result, _step = _run(_parsed(["matricule"]), corpus, Index(corpus), _budget(max_tokens=1))
    assert result.blocs == [] and result.truncated is True
    assert result.opened_node_ids == []


async def test_les_outils_publient_le_noeud_de_chaque_bloc_transmis() -> None:
    """La variante **servie** (AD-1 : `outils` est le défaut) suit exactement la même règle.

    C'est celle où l'omission mordait le plus : `primary_node_by_block` n'est renseigné que pour les
    blocs admis depuis une fenêtre, si bien qu'une navigation servie par `definitions` ou par des
    dépendances directes pouvait publier « 0 section lue » sous N passages transmis.
    """
    corpus = _corpus()
    result, _step, _fake, _rb = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["matricule"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1", focus_block_id="d:p1:1"),
                      _tool("ouvrir_noeud", "t3", node_id="n2", focus_block_id="d:p2:1")),
    ], corpus=corpus)
    assert result.opened_node_ids == _noeuds_attendus(corpus, result.opened_block_ids)
    assert "n3" in result.opened_node_ids  # la cible du renvoi, entrée hors fenêtre
    assert len(set(result.opened_node_ids)) == len(result.opened_node_ids)


async def test_un_bloc_transmis_sans_fenetre_a_quand_meme_sa_section() -> None:
    """Le cas que l'omission rendait absurde : aucune fenêtre ouverte, un bloc quand même transmis.

    `definitions` admet ses blocs sans passer par `ouvrir_noeud` : le compte des sections ne peut
    donc pas se lire sur les seules fenêtres, sinon l'écran affiche « 0 section lue, 1 passage
    transmis ». AD-2 garantit qu'un bloc a exactement un nœud propriétaire — il n'y a rien à deviner.
    """
    corpus = _corpus()
    result, _step, _fake, _rb = await _run_outils([
        _tool_message(_tool("definitions", "t1", termes=["contenu"])),
        fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[]),
    ], corpus=corpus, parsed=_parsed(["contenu"]))
    assert result.opened_block_ids == ["d:p3:1"]
    assert result.opened_node_ids == ["n3"]


async def test_les_outils_ne_comptent_pas_une_ouverture_sans_bloc_admis() -> None:
    """Le pendant côté outils : une fenêtre qu'aucun bloc n'a franchie ne fait lire aucune section."""
    corpus = _corpus()
    result, _step, _fake, _rb = await _run_outils([
        _tool_message(_tool("chercher", "t1", termes=["matricule"]),
                      _tool("ouvrir_noeud", "t2", node_id="n1", focus_block_id="d:p1:1"),
                      _tool("ouvrir_noeud", "t3", node_id="n2", focus_block_id="d:p2:1")),
    ], corpus=corpus, budget=_budget(max_blocks=1, max_tokens=6000))
    assert result.truncated is True
    assert result.opened_node_ids == ["n1"]
    assert result.opened_node_ids == _noeuds_attendus(corpus, result.opened_block_ids)


def _dictionnaire_mecanismes() -> Dictionnaire:
    return Dictionnaire(
        charge=True, corpus_ok=True, doc_id="d",
        _groupes={"alias": ("alias", "matricule")},
        _canoniques={"alias": ("matricule",)},
        _candidate_questions={"n3": ("q",)},
    )


def test_deterministe_executes_configured_mechanism_permutations_without_resorting_hits() -> None:
    corpus = _corpus()
    dico = _dictionnaire_mecanismes()
    parsed = _parsed(["alias"])

    faq_first, _ = _run(
        parsed, corpus, Index(corpus), _budget(max_opens=1), doc_id="d", dictionnaire=dico,
        settings=_s(retrieval_mechanism_order="dictionnaire,faq,sommaire,outils"))
    summary_first, _ = _run(
        parsed, corpus, Index(corpus), _budget(max_opens=1), doc_id="d", dictionnaire=dico,
        settings=_s(retrieval_mechanism_order="dictionnaire,sommaire,outils,faq"))
    dictionary_late, _ = _run(
        parsed, corpus, Index(corpus), _budget(max_opens=3), doc_id="d", dictionnaire=dico,
        settings=_s(retrieval_mechanism_order="sommaire,outils,faq,dictionnaire"))

    assert faq_first.opened_node_ids == ["n3"]
    assert summary_first.opened_node_ids[0] == "n1"
    # Le dictionnaire placé après sommaire ne peut rétroactivement élargir son hit `alias`.
    assert dictionary_late.opened_node_ids == ["n3"]


@pytest.mark.parametrize(
    ("order", "faq_visible_during_tools"),
    [
        ("dictionnaire,faq,sommaire,outils", True),
        ("outils,dictionnaire,sommaire,faq", False),
    ],
)
async def test_outils_faq_opens_a_candidate_without_lexical_hit_in_configured_order(
        order: str, faq_visible_during_tools: bool) -> None:
    corpus = _corpus()
    messages = ([_tool_message(_tool("ouvrir_noeud", "t1", node_id="n3"))]
                if faq_visible_during_tools else
                [fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[])])
    result, step, fake, _budget_used = await _run_outils(
        messages,
        corpus=corpus, parsed=_parsed(["zorbule"]), dictionnaire=_dictionnaire_mecanismes(),
        settings=_s(max_cost_eur_per_request=1.0, retrieval_mechanism_order=order))

    assert ("d:p3:1" in result.opened_block_ids) is faq_visible_during_tools
    assert Index(corpus).chercher(["zorbule"], limit=20, doc_id="d") == []
    user_message = str(fake.requests[0]["messages"][0]["content"])
    assert ("faq_candidates" in user_message) is faq_visible_during_tools
    assert step.mechanism_order == order.split(",")


@pytest.mark.parametrize(
    ("order", "summary_visible"),
    [("sommaire,outils,dictionnaire,faq", True),
     ("outils,sommaire,dictionnaire,faq", False)],
)
async def test_outils_only_sees_summary_when_summary_precedes_it(
        order: str, summary_visible: bool) -> None:
    corpus = _corpus()
    result, _step, fake, _request_budget = await _run_outils(
        [_tool_message(_tool("ouvrir_noeud", "t1", node_id="n1",
                             focus_block_id="d:p1:1")),
         fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[])],
        corpus=corpus, parsed=_parsed(["matricule"]),
        settings=_s(max_cost_eur_per_request=1.0, retrieval_mechanism_order=order))

    assert ("d:p1:1" in result.opened_block_ids) is summary_visible
    system = fake.requests[0]["system"][0]["text"]
    assert (("root\n  n1" in system) is summary_visible)


@pytest.mark.parametrize(
    ("order", "expanded_during_tools"),
    [("dictionnaire,outils,faq,sommaire", True),
     ("outils,faq,sommaire,dictionnaire", False)],
)
async def test_dictionary_only_enriches_searches_that_follow_it(
        order: str, expanded_during_tools: bool) -> None:
    result, _step, fake, _request_budget = await _run_outils(
        [_tool_message(_tool("chercher", "t1", termes=["alias"])),
         fake_message(model="claude-sonnet-5", stop_reason="end_turn", content=[])],
        parsed=_parsed(["alias"]), dictionnaire=_dictionnaire_mecanismes(),
        settings=_s(max_cost_eur_per_request=1.0, retrieval_mechanism_order=order))

    tool_feedback = str(fake.requests[1]["messages"])
    assert (("d:p1:1" in tool_feedback) is expanded_during_tools)
    # Les hits ajoutés par une phase ultérieure ne sont jamais ouverts rétroactivement.
    assert result.opened_block_ids == []


async def test_micro_retrieval_callers_disable_provider_prompt_cache() -> None:
    settings = _s(max_cost_eur_per_request=1.0, retrouver_outils_tier="micro",
                  retrieval_prompt_cache=False)
    _result, tools_step, tools_fake, _request_budget = await _run_outils(
        [fake_message(model=TIERS["micro"], stop_reason="end_turn", content=[])],
        settings=settings)
    assert "cache_control" not in tools_fake.requests[0]["system"][0]
    assert tools_step.prompt_cache is False

    corpus = _corpus()
    full_fake = FakeAnthropic([fake_message(
        model=TIERS["micro"], text='{"block_ids":["d:p2:1"]}')])
    result, full_step = await retrouver_full_context(
        _parsed(["matricule"]), corpus=corpus, index=Index(corpus),
        budget=_budget(max_blocks=30, max_tokens=6000), settings=settings,
        client=LlmClient(settings, anthropic_client=full_fake),
        request_budget=RequestBudget(deadline_s=30, max_attempts=8, max_cost_eur=1.0),
        doc_id="d")
    assert result.opened_block_ids == ["d:p2:1"]
    assert "cache_control" not in full_fake.requests[0]["system"][0]
    assert full_step.prompt_cache is False


async def test_full_context_places_all_citable_blocks_in_system_and_only_dynamic_data_in_user() -> None:
    corpus = _corpus()

    class Client:
        request: dict[str, object] | None = None

        async def parse(self, **kwargs):
            self.request = kwargs
            return SimpleNamespace(parsed=FullContextSelection(block_ids=["d:p1:1"]))

    client = Client()
    result, step = await retrouver_full_context(
        _parsed(["matricule"], facettes=["première démarche", "seconde démarche"]),
        corpus=corpus, index=Index(corpus),
        budget=_budget(max_blocks=30, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0), client=client,
        request_budget=RequestBudget(deadline_s=30, max_attempts=8, max_cost_eur=1.0),
        doc_id="d")
    assert result.opened_block_ids == ["d:p1:1"] and result.blocs[0] is corpus.documents["d"].block("d:p1:1")
    assert client.request is not None
    system = str(client.request["system_prefix"])
    user = str(client.request["messages"])
    assert "d:p1:1" in system and "d:p3:1" in system and "root" in system
    assert "d:p1:1" not in user and "Le matricule est délivré" not in user
    assert "première démarche" in user and "seconde démarche" in user
    assert step.prompt_cache is True and step.mechanism_order == [
        "dictionnaire", "faq", "sommaire", "outils"]


async def test_full_context_resolves_model_ids_in_canonical_corpus_order() -> None:
    corpus = _corpus()

    class Client:
        async def parse(self, **kwargs):
            return SimpleNamespace(parsed=FullContextSelection(
                block_ids=["d:p2:1", "d:p1:1"]))

    result, _step = await retrouver_full_context(
        _parsed(["matricule"]), corpus=corpus, index=Index(corpus),
        budget=_budget(max_blocks=30, max_tokens=6000),
        settings=_s(max_cost_eur_per_request=1.0), client=Client(),
        request_budget=RequestBudget(deadline_s=30, max_attempts=8, max_cost_eur=1.0),
        doc_id="d")
    assert result.opened_block_ids == ["d:p1:1", "d:p2:1"]
    assert [block.block_id for block in result.blocs] == result.opened_block_ids


async def test_full_context_applies_common_atomic_closure_and_block_budget() -> None:
    corpus = _corpus()

    class Client:
        async def parse(self, **kwargs):
            return SimpleNamespace(parsed=FullContextSelection(block_ids=["d:p1:2"]))

    complete, _step = await retrouver_full_context(
        _parsed(["zorbule"]), corpus=corpus, index=Index(corpus),
        budget=_budget(max_blocks=2, max_tokens=6000), settings=_s(), client=Client(),
        request_budget=RequestBudget(deadline_s=30, max_attempts=8, max_cost_eur=1.0),
        doc_id="d")
    assert complete.opened_block_ids == ["d:p1:2", "d:p1:5"]

    bounded, bounded_step = await retrouver_full_context(
        _parsed(["zorbule"]), corpus=corpus, index=Index(corpus),
        budget=_budget(max_blocks=1, max_tokens=6000), settings=_s(), client=Client(),
        request_budget=RequestBudget(deadline_s=30, max_attempts=8, max_cost_eur=1.0),
        doc_id="d")
    assert bounded.opened_block_ids == [] and bounded.truncated is True
    assert bounded.discarded_block_ids == bounded_step.discarded_block_ids == ["d:p1:2"]


async def test_full_context_limits_primary_nodes_by_max_opens() -> None:
    corpus = _corpus()

    class Client:
        async def parse(self, **kwargs):
            return SimpleNamespace(parsed=FullContextSelection(
                block_ids=["d:p3:1", "d:p2:1", "d:p1:1"]))

    candidates: list[str] = []
    result, _step = await retrouver_full_context(
        _parsed(["zorbule"]), corpus=corpus, index=Index(corpus),
        budget=_budget(max_opens=1, max_blocks=30, max_tokens=6000), settings=_s(),
        client=Client(),
        request_budget=RequestBudget(deadline_s=30, max_attempts=8, max_cost_eur=1.0),
        doc_id="d", candidats_out=candidates)
    assert candidates == ["d:p3:1", "d:p2:1", "d:p1:1"]
    assert result.opened_node_ids == ["n1"]
    assert result.discarded_block_ids == ["d:p2:1", "d:p3:1"]
    assert result.truncated is True


async def test_full_context_refuses_context_window_before_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.app.steps import retrouver as module

    corpus = _corpus()

    class Client:
        called = False

        async def parse(self, **kwargs):
            self.called = True
            raise AssertionError("le client ne doit pas être appelé")

    client = Client()
    caps = dict(module.MODEL_CAPS[TIERS["micro"]])
    monkeypatch.setitem(module.MODEL_CAPS, TIERS["micro"], {**caps, "context_window": 1})
    with pytest.raises(BudgetExceeded, match="hors enveloppe") as capture:
        await retrouver_full_context(
            _parsed(["matricule"]), corpus=corpus, index=Index(corpus),
            budget=_budget(max_blocks=30, max_tokens=6000), settings=_s(), client=client,
            request_budget=RequestBudget(deadline_s=30, max_attempts=8, max_cost_eur=1.0),
            doc_id="d")
    assert client.called is False
    assert capture.value.step is not None
    assert capture.value.step.prompt_cache is True


async def test_full_context_preflight_counts_the_exact_structured_envelope(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from server.app.steps import retrouver as module

    corpus = _corpus()
    settings = _s(retrouver_outils_max_tokens=7)
    caps = dict(module.MODEL_CAPS[TIERS["micro"]])
    monkeypatch.setitem(module.MODEL_CAPS, TIERS["micro"], {**caps, "context_window": 16})
    monkeypatch.setattr(
        module, "estimate_tokens",
        lambda payload, _settings: 10 if '"output_config"' in payload else 1)

    class Client:
        async def parse(self, **kwargs):
            raise AssertionError("le préflight exact doit refuser avant le client")

    with pytest.raises(BudgetExceeded) as capture:
        await retrouver_full_context(
            _parsed(["matricule"]), corpus=corpus, index=Index(corpus),
            budget=_budget(max_blocks=30, max_tokens=6000), settings=settings, client=Client(),
            request_budget=RequestBudget(deadline_s=30, max_attempts=8, max_cost_eur=1.0),
            doc_id="d")
    assert capture.value.step is not None
