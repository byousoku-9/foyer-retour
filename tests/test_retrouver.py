"""Matrice I/O de *retrouver* déterministe (spec 1.4) : code pur, zéro appel modèle — ouverture groupée
bornée par `RetrievalBudget` (nœuds, blocs **et** tokens), suivi d'un niveau des renvois et des
définitions hors quota, `truncated`, hits non transmis en `discarded_block_ids`, blocs relus du
corpus, aucune absence affirmée."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.domain import Block, BlockRef, Document, Node, NodeRef, RetrievalBudget
from server.app.domain.question import ParsedQuestion, QuestionScope
from server.app.llm.models import STEP_TIERS
from server.app.steps.retrouver import retrouver_deterministe

ROOT = Path(__file__).resolve().parents[1]


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def _budget(**kw) -> RetrievalBudget:
    base = {"max_opens": 6, "node_window": 30, "search_limit": 20, "max_llm_turns": 2}
    return RetrievalBudget(**(base | kw))


def _parsed(terms: list[str], themes: list[str] | None = None) -> ParsedQuestion:
    return ParsedQuestion(question_resolue="q", intent="question", terms=terms,
                          scope=QuestionScope(themes=themes or []))


def _run(parsed: ParsedQuestion, corpus: Corpus, index: Index, budget: RetrievalBudget | None = None,
         *, settings: Settings | None = None, doc_id: str | None = None):
    return retrouver_deterministe(parsed, corpus=corpus, index=index, budget=budget or _budget(),
                                  settings=settings or _s(), doc_id=doc_id)


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
    from server.app.llm.pricing import estimate_tokens
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

def _dictionnaire(corpus: Corpus, termes: dict[str, list[str]], *, validated: bool = False,
                  source_hash: str | None = None):
    """Un `Dictionnaire` chargé depuis un fichier réel : jamais un objet fabriqué à la main.

    C'est ce que le serveur fait au démarrage, et c'est le seul chemin qui exerce à la fois le schéma
    du fichier, la comparaison des empreintes et l'indexation des formes.
    """
    import json
    import tempfile

    from server.app.corpus.dictionary import load_dictionary

    dossier = Path(tempfile.mkdtemp())
    hashes = {d: (source_hash if source_hash is not None else corpus.manifest[d].source_hash)
              for d in corpus.served}
    signature = ({"validated": True, "validated_by": "Lancelot Oudin",
                  "validated_at": "2026-08-25T10:00:00Z"} if validated else
                 {"validated": False, "validated_by": None, "validated_at": None})
    (dossier / "dictionary.json").write_text(json.dumps(
        {"schema_version": "1", "corpus_source_hashes": hashes, "corpus": termes,
         "intents": {}, "candidate_questions": {}, **signature}, ensure_ascii=False), "utf-8")
    return load_dictionary(dossier, corpus)


def _corpus_avec_manifest() -> Corpus:
    from server.app.domain.ingest import ManifestEntry

    corpus = _corpus()
    corpus.manifest["d"] = ManifestEntry(status="servi", source_hash="sha-source",
                                         ingest_fingerprint="fp", document_hash="sha-doc",
                                         edition="e")
    return corpus


def test_une_variante_ouvre_la_fiche_du_canonique() -> None:
    """AC 2.1 : « une question dont un terme français est une variante d'un canonique du guide ⇒ la
    fiche du canonique figure dans `RetrievalResult.opened_block_ids` »."""
    corpus = _corpus_avec_manifest()
    dico = _dictionnaire(corpus, {"matricule": ["numéro de sécurité sociale"]})
    assert dico.utilisable is True

    # Sans dictionnaire, ce terme ne touche rien : c'est exactement le faux refus qu'AD-5 vise.
    nu, _ = _run(_parsed(["numéro de sécurité sociale"]), corpus, Index(corpus))
    assert nu.opened_block_ids == []

    result, step = retrouver_deterministe(
        _parsed(["numéro de sécurité sociale"]), corpus=corpus, index=Index(corpus),
        budget=_budget(), settings=_s(), dictionnaire=dico)
    assert "d:p1:1" in result.opened_block_ids  # la fiche du canonique « matricule »
    (check,) = [c for c in step.checks if c.name == "dictionnaire"]
    assert check.ok is True and check.detail == "1 variante(s) ajoutée(s) à 1 terme(s)"


def test_la_trace_dit_le_nombre_de_variantes_jamais_lesquelles() -> None:
    """AD-4 : `AbsenceProof` ne porte jamais la liste des variantes ; AD-10 : la trace non plus.

    Publier les formes ajoutées ferait fuir le dictionnaire terme par terme, dans un canal que le
    front « pourquoi cette réponse » affiche.
    """
    corpus = _corpus_avec_manifest()
    dico = _dictionnaire(corpus, {"matricule": ["numero national", "social security number"]})
    _result, step = retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=Index(corpus),
                                           budget=_budget(), settings=_s(), dictionnaire=dico)
    detail = next(c.detail for c in step.checks if c.name == "dictionnaire")
    assert detail == "2 variante(s) ajoutée(s) à 1 terme(s)"
    for variante in ("numero national", "social security number"):
        assert variante not in step.model_dump_json()


def test_un_dictionnaire_inutilisable_ne_change_rien_du_tout() -> None:
    """`corpus_ok` commande les variantes : un dictionnaire d'un **autre** corpus ne dit rien de
    celui-ci, et *retrouver* fait exactement ce qu'il faisait avant cette story — sans check."""
    corpus = _corpus_avec_manifest()
    perime = _dictionnaire(corpus, {"matricule": ["numéro de sécurité sociale"]},
                           source_hash="empreinte-dun-autre-corpus")
    assert perime.charge is True and perime.utilisable is False

    attendu, _ = _run(_parsed(["numéro de sécurité sociale"]), corpus, Index(corpus))
    result, step = retrouver_deterministe(
        _parsed(["numéro de sécurité sociale"]), corpus=corpus, index=Index(corpus),
        budget=_budget(), settings=_s(), dictionnaire=perime)
    assert result.opened_block_ids == attendu.opened_block_ids == []
    assert [c.name for c in step.checks] == []


def test_les_variantes_servent_sans_validation_humaine() -> None:
    """Design Note 2.1 : `validated` ne commande **que** le court-circuit d'AD-5, pas l'élargissement.

    Élargir n'affirme rien — chaque phrase affichée reste vérifiée contre le corpus (AD-3) ; refuser
    est une affirmation négative, et c'est elle que la signature humaine garde.
    """
    corpus = _corpus_avec_manifest()
    dico = _dictionnaire(corpus, {"matricule": ["numéro de sécurité sociale"]}, validated=False)
    assert (dico.utilisable, dico.court_circuit_actif) == (True, False)
    result, _step = retrouver_deterministe(
        _parsed(["numéro de sécurité sociale"]), corpus=corpus, index=Index(corpus),
        budget=_budget(), settings=_s(), dictionnaire=dico)
    assert "d:p1:1" in result.opened_block_ids


def test_les_definitions_continuent_de_recevoir_les_termes_seuls() -> None:
    """Reprise différée `target_story: 4.2` : `definitions()` apparie `defines` et les termes dans
    les **deux sens**, et lui passer les variantes multiplierait un faux positif connu et non
    corrigé. Le spec le dit littéralement : « `index.definitions()` continue de recevoir `terms` ».
    """
    corpus = _corpus_avec_manifest()
    index = Index(corpus)
    vus: list = []
    reel = index.definitions

    def espion(termes, **kw):
        vus.append(termes)
        return reel(termes, **kw)

    index.definitions = espion  # type: ignore[method-assign]
    dico = _dictionnaire(corpus, {"matricule": ["contenu"]})
    retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=index, budget=_budget(),
                           settings=_s(), dictionnaire=dico)
    assert vus == [["matricule"]]  # une liste de termes, jamais le dict élargi
