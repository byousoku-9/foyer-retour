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
from server.app.llm.pricing import estimate_tokens
from server.app.steps.retrouver import retrouver_deterministe

ROOT = Path(__file__).resolve().parents[1]


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def _budget(**kw) -> RetrievalBudget:
    base = {"max_opens": 6, "node_window": 30, "search_limit": 20, "max_llm_turns": 2}
    return RetrievalBudget(**(base | kw))


def _parsed(terms: list[str], themes: list[str] | None = None,
            noeuds: list[str] | None = None) -> ParsedQuestion:
    return ParsedQuestion(question_resolue="q", intent="question", terms=terms,
                          scope=QuestionScope(themes=themes or [], noeuds=noeuds or []))


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
    return load_dictionary(dossier, corpus, DICO_DOC)


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
    dico = _dictionnaire(tmp_path, corpus, {"matricule": ["contenu"]})
    retrouver_deterministe(_parsed(["matricule"]), corpus=corpus, index=index, budget=_budget(),
                           settings=_s(), doc_id=DICO_DOC, dictionnaire=dico)
    assert vus == [["matricule"]]  # une liste de termes, jamais le dict élargi


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
