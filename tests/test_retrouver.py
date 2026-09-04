"""Ce qui reste de *retrouver* après l'amendement AD-1 du 03/09/2026 (story 5.6, T2).

Les variantes `deterministe` et `outils` ont été supprimées avec leurs témoins : c'étaient elles qui
classaient, réservaient et complétaient à la place du modèle. Ce fichier ne garde donc que ce qui
sert encore — la variante de comparaison `full_context`, la mesure de pertinence lexicale d'un hit
admis, la borne de fréquence des formes de requête, et la structure d'énumération que le corpus
porte. La navigation servie est mesurée par `tests/test_naviguer.py`, la satisfaction d'une demande
de contexte par `tests/test_demande_de_contexte.py`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from server.app.config import Settings
from server.app.corpus.dictionary import Dictionnaire
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
)
from server.app.domain.errors import BudgetExceeded
from server.app.domain.verdict import KINDS_DECISIONNELS
from server.app.domain.question import ParsedQuestion, QuestionScope
from server.app.llm.models import TIERS
from server.app.llm.budget import RequestBudget
from server.app.corpus.requetes import part_du_mot_borne
from server.app.steps.retrouver import _score_positif, retrouver_full_context

ROOT = Path(__file__).resolve().parents[1]


def test_la_suffisance_lexicale_ignore_un_chevauchement_de_mots_outils_seuls() -> None:
    block = Block(
        block_id="d:p1:1", text="Il est pour le contrat.", loc="p1", seq=1,
    )
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="guide", title="t", edition="e",
        nodes=[Node(node_id="root", items=[BlockRef(block_id=block.block_id)])],
        blocks=[block],
    )})
    index = Index(corpus)

    mots_outils = "Il est pour une personne."
    score_mots_outils = index.score_clause(mots_outils, block.block_id).score
    assert score_mots_outils.question_numerator > 0  # le record transporté reste inchangé
    assert not _score_positif(score_mots_outils, question=mots_outils, clause=block.text)

    substantielle = "Le contrat protège une personne."
    score_substantiel = index.score_clause(substantielle, block.block_id).score
    assert _score_positif(score_substantiel, question=substantielle, clause=block.text)

def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)

def _budget(**kw) -> RetrievalBudget:
    base = {"max_opens": 6, "node_window": 30, "search_limit": 20}
    return RetrievalBudget(**(base | kw))

def _parsed(terms: list[str], themes: list[str] | None = None,
            noeuds: list[str] | None = None,
            facettes: list[str] | None = None) -> ParsedQuestion:
    # Le contrat v2 score la question entière : la fixture ne peut plus utiliser un placeholder
    # sans rapport avec ses propres termes de rappel.
    return ParsedQuestion(question_resolue=" ".join(terms), intent="question", terms=terms,
                          scope=QuestionScope(themes=themes or [], noeuds=noeuds or []),
                          facettes=facettes or [])

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
        request_budget=RequestBudget(deadline_s=100, max_attempts=8, max_cost_eur=1.0),
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
        request_budget=RequestBudget(deadline_s=100, max_attempts=8, max_cost_eur=1.0),
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
        request_budget=RequestBudget(deadline_s=100, max_attempts=8, max_cost_eur=1.0),
        doc_id="d")
    assert complete.opened_block_ids == ["d:p1:2", "d:p1:5"]

    bounded, bounded_step = await retrouver_full_context(
        _parsed(["zorbule"]), corpus=corpus, index=Index(corpus),
        budget=_budget(max_blocks=1, max_tokens=6000), settings=_s(), client=Client(),
        request_budget=RequestBudget(deadline_s=100, max_attempts=8, max_cost_eur=1.0),
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
        request_budget=RequestBudget(deadline_s=100, max_attempts=8, max_cost_eur=1.0),
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
    caps = dict(module.MODEL_CAPS[TIERS["reason"]])
    monkeypatch.setitem(module.MODEL_CAPS, TIERS["reason"], {**caps, "context_window": 1})
    with pytest.raises(BudgetExceeded, match="hors enveloppe") as capture:
        await retrouver_full_context(
            _parsed(["matricule"]), corpus=corpus, index=Index(corpus),
            budget=_budget(max_blocks=30, max_tokens=6000), settings=_s(), client=client,
            request_budget=RequestBudget(deadline_s=100, max_attempts=8, max_cost_eur=1.0),
            doc_id="d")
    assert client.called is False
    assert capture.value.step is not None
    assert capture.value.step.prompt_cache is True

async def test_full_context_preflight_counts_the_exact_structured_envelope(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from server.app.steps import retrouver as module

    corpus = _corpus()
    settings = _s(retrouver_outils_max_tokens=7)
    caps = dict(module.MODEL_CAPS[TIERS["reason"]])
    monkeypatch.setitem(module.MODEL_CAPS, TIERS["reason"], {**caps, "context_window": 16})
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
            request_budget=RequestBudget(deadline_s=100, max_attempts=8, max_cost_eur=1.0),
            doc_id="d")
    assert capture.value.step is not None

def _corpus_a_recouvrement_partiel() -> tuple[Corpus, ParsedQuestion]:
    """Le motif mesuré sur A16 : un bloc hors sujet gagne le classement par mots fréquents.

    `pertinent` porte le mot rare de la sous-question, `bavard` n'en porte que les mots communs —
    mais il les porte tous, et il est plus court, donc il gagne le rang 0 au score partiel. Sans la
    garde, c'est **lui** que la réserve garde et que la couverture déclare comme réponse à la
    sous-question ; sur le contrat servi, c'était une exclusion de responsabilité civile immeuble.
    """
    bavard = Block(block_id="d:p1:1", kind="exclusion", kind_source="manual", loc="p1", seq=1,
                   text="Les dommages relatifs aux biens sont écartés.")
    pertinent = Block(block_id="d:p2:1", kind="garantie", kind_source="manual", loc="p2", seq=1,
                      text="Les suies sont prises en charge.")
    corpus = _corpus_neutre_par_noeuds(("bavard", [bavard]), ("pertinent", [pertinent]))
    parsed = _parsed(["dommages"], facettes=["dommages relatifs aux suies", "seconde branche"])
    return corpus, parsed

def test_une_variante_dun_mot_frequent_ne_devient_pas_un_plein_partout() -> None:
    """La garde de fréquence des formes de nombre, appliquée aux variantes écrites du dictionnaire.

    Le raisonnement est celui du tour 3, mot pour mot : une forme d'un seul mot que le document
    porte partout est pleinement couverte par des dizaines de blocs, et `full_matches` — sur lequel
    toute la sélection par sous-question repose depuis R1 — redevient inerte. Une variante de
    plusieurs mots n'est jamais bornée : une phrase entièrement couverte dit quelque chose quelle
    que soit la fréquence de ses mots pris un à un.
    """
    index = Index(load_corpus(ROOT / "data", allow_ungated=True))
    doc_id = "axa-lu-optihome-2017"
    part_max = _s().dictionnaire_variante_max_part
    # Mesuré sur le contrat servi : « incendie » nomme le sujet, « fumees » désigne une clause.
    assert index.part_des_blocs("incendie", doc_id=doc_id) > part_max
    assert 0 < index.part_des_blocs("fumees", doc_id=doc_id) <= part_max

    dictionnaire = Dictionnaire(
        charge=True, corpus_ok=True, doc_id=doc_id,
        _groupes={"sinistre du foyer": ("sinistre du foyer", "incendie", "fumees",
                                        "action subite de la chaleur")},
        _canoniques={"sinistre du foyer": ("sinistre du foyer",)})
    borne = {"part_du_mot": part_du_mot_borne(index, doc_id, part_max=part_max),
             "part_max": part_max}
    assert dictionnaire.expand(["sinistre du foyer"], **borne) == {
        "sinistre du foyer": ["fumees", "action subite de la chaleur"]}
    # Le compte publié dit ce qui a été cherché, donc il porte la même borne.
    assert dictionnaire.variants_count(["sinistre du foyer"], **borne) == 2
    assert dictionnaire.variants_count(["sinistre du foyer"]) == 3

def test_sur_un_document_trop_court_la_borne_de_frequence_sabstient() -> None:
    """Un seuil en part n'est une mesure que si le document a de quoi la porter.

    La plus fine part qu'un document de `N` blocs sache exprimer vaut `1/N`. Tant que le seuil ne
    vaut pas un bloc entier, « dépasser le seuil » et « figurer dans le document » sont la même
    chose, et la borne dirait « cette forme existe » au lieu de « cette forme nomme le sujet ». Elle
    s'abstient — et l'abstention est le bon sens : une variante de dictionnaire est une équivalence
    **écrite** (AD-5), pas une forme dérivée par le code, et la refuser sans mesure désarmerait en
    silence le rappel qu'AD-5 apporte.
    """
    corpus, _parsee = _corpus_a_recouvrement_partiel()
    index = Index(corpus)
    petit = len(corpus.documents["d"].blocks)
    assert petit * _s().dictionnaire_variante_max_part < 1
    assert part_du_mot_borne(index, "d", part_max=_s().dictionnaire_variante_max_part) is None
    # Et la mesure existe pourtant : c'est bien l'abstention qui est décidée, pas une absence.
    assert index.part_des_blocs("dommages", doc_id="d") > 0

def test_un_noeud_feuille_dont_le_parent_na_quun_titre_reste_seul() -> None:
    """La borne : le code ne fabrique jamais une unité à partir d'un titre, ici comme pour un renvoi."""
    titre = Block(block_id="d:p1:1", kind="heading", loc="p1", seq=1, text="Article premier")
    item = Block(block_id="d:p2:1", kind="garantie", kind_source="manual", loc="p2", seq=1,
                 text="Les fumées et les suies ;")
    corpus = Corpus(documents={"d": Document(
        doc_id="d", kind="contrat", title="t", edition="e",
        nodes=[Node(node_id="root", items=[NodeRef(node_id="parent")]),
               Node(node_id="parent", items=[BlockRef(block_id=titre.block_id),
                                             NodeRef(node_id="item")]),
               Node(node_id="item", items=[BlockRef(block_id=item.block_id)])],
        blocks=[titre, item])}, summaries={"d": "root"})
    index = Index(corpus)
    assert index.amorce_de_lenumeration("d:p2:1") is None
    assert index.enumeration_de("d:p2:1") is None

def test_une_section_qui_porte_des_sous_parties_nest_pas_une_enumeration() -> None:
    """La borne structurelle : deviner sur une section transmettrait un article pour une feuille."""
    index = Index(load_corpus(ROOT / "data", allow_ungated=True))
    doc_id = "axa-lu-optihome-2017"
    # `a3.1.5.1` porte trois blocs citables : ses voisins font de lui une section, pas un item.
    assert index.enumeration_de(f"{doc_id}:p39:9") is None
    # Et l'énumération des périls incendie l'est, depuis n'importe lequel de ses membres.
    attendue = [f"{doc_id}:p34:{n}" for n in (6, 7, 8, 9, 10, 11, 12)]
    assert index.enumeration_de(f"{doc_id}:p34:6") == attendue
    assert index.enumeration_de(f"{doc_id}:p34:12") == attendue


def test_lamorce_introduit_immediatement_son_item_sur_les_deux_rangements_du_corpus() -> None:
    """Story 5.6 (L1f) : l'adjacence que lit AD-3 se prend sur l'arbre, jamais sur la ponctuation.

    Deux rangements, un seul prédicat : Baloise range l'amorce et son item dans le **même nœud**
    (« Sont exclus : » puis l'exclusion), AXA range l'amorce dans le **nœud parent** et chaque péril
    dans une feuille. Ce qui n'est pas adjacent — un item après un autre item, un bloc lointain,
    l'ordre inverse — n'est introduit par rien.
    """
    index = Index(load_corpus(ROOT / "data", allow_ungated=True))
    baloise = "baloise-lu-home-2-2024"
    axa = "axa-lu-optihome-2017"
    # Même nœud, blocs directs consécutifs : l'amorce introduit l'item.
    assert index.introduit_immediatement(f"{baloise}:p12:8", f"{baloise}:p12:9")
    assert index.introduit_immediatement(f"{baloise}:p12:5", f"{baloise}:p12:6")
    # Nœud parent direct : le cas que `amorce_de_lenumeration` nomme déjà.
    assert index.introduit_immediatement(f"{axa}:p34:6", f"{axa}:p34:11")
    # Les fermetures. L'ordre compte (une amorce précède), un item n'introduit pas son voisin,
    # un bloc lointain n'introduit rien, et un bloc ne s'introduit pas lui-même.
    assert not index.introduit_immediatement(f"{baloise}:p12:9", f"{baloise}:p12:8")
    assert not index.introduit_immediatement(f"{axa}:p34:11", f"{axa}:p34:12")
    assert not index.introduit_immediatement(f"{baloise}:p12:5", f"{baloise}:p12:9")
    assert not index.introduit_immediatement(f"{axa}:p34:6", f"{axa}:p34:6")
    # Un titre n'introduit rien : le code ne fabrique pas d'unité de lecture à partir d'un titre.
    doc = index.corpus.documents[baloise]
    titre = next(b.block_id for b in doc.blocks if b.kind == "heading")
    assert not any(index.introduit_immediatement(titre, b.block_id) for b in doc.blocks)


def test_une_amorce_se_reconnait_a_ce_quelle_precede_ses_items() -> None:
    """Story 5.7 (L1o) : la question inverse de `amorce_de_lenumeration`, et ses deux fermetures.

    Le verdict a besoin de savoir si **ce bloc-ci** ouvre une énumération : citée seule, une amorce
    est le contexte d'une clause, pas une clause. Les deux rangements sont ceux de L1f — AXA range
    ses périls dans des feuilles enfants, Baloise range ses exclusions dans une liste du même nœud.

    Les deux fermetures sont celles qui font la précision de la règle, et chacune vient d'une mesure
    sur les contrats servis. **Précéder ses items** : `p29:10` est le dernier bloc citable de `2.18`
    et il *suit* les sept items — c'est l'épilogue de l'énumération, pas son annonce, et le tenir
    pour une amorce sortirait une vraie condition de la table. **N'être pas soi-même un item** : un
    bloc que l'ingestion a rangé en `list` est un item, quoi qu'il ait autour de lui.
    """
    index = Index(load_corpus(ROOT / "data", allow_ungated=True))
    baloise, axa = "baloise-lu-home-2-2024", "axa-lu-optihome-2017"
    # Nœud parent (AXA) et même nœud (Baloise) : l'annonce est reconnue, ses items ne le sont pas.
    assert index.est_amorce_denumeration(f"{axa}:p34:6")
    assert not index.est_amorce_denumeration(f"{axa}:p34:12")
    assert index.est_amorce_denumeration(f"{baloise}:p12:8")
    assert not index.est_amorce_denumeration(f"{baloise}:p12:9")
    # L'épilogue qui suit les items n'est pas l'amorce ; celle qui les précède l'est.
    assert not index.est_amorce_denumeration(f"{axa}:p29:10")
    assert index.est_amorce_denumeration(f"{axa}:p29:2")
    # Un bloc `list` est un item, et un titre n'ouvre rien : le code ne fabrique pas d'unité de
    # lecture à partir d'un titre, ici comme dans les deux méthodes voisines.
    assert index.corpus.documents[axa].block(f"{axa}:p5:3").structural_kind == "list"
    assert not index.est_amorce_denumeration(f"{axa}:p5:3")
    assert not any(index.est_amorce_denumeration(b.block_id)
                   for b in index.corpus.documents[baloise].blocks if b.kind == "heading")
    # Un bloc inconnu ne lève pas : il n'introduit rien et ne se cite pas.
    assert not index.est_amorce_denumeration("d:p1:1")


def test_le_rayon_daction_de_la_regle_des_amorces_est_mesure_sur_les_deux_contrats() -> None:
    """Story 5.7 (L1o) — combien de clauses la règle sort de la table, mesuré le 04/09/2026.

    Une amorce citée seule vaut `applicable=None` : la règle *retire* de la table des blocs que
    l'ingestion a typés décisionnels, et son rayon d'action doit donc se voir. Sur les deux contrats
    servis, 72 blocs décisionnels sur 1 247 sont des amorces, et **70 d'entre eux finissent par un
    deux-points** — la structure dit ici ce que la ponctuation dirait, sans jamais la lire. Les deux
    autres sont l'endroit exact où les deux lectures divergent : une amorce dont l'extraction a perdu
    le deux-points (`p36:6`, « Ne sont toutefois pas couverts les dommages »), que le texte aurait
    manquée, et une condition qui n'annonce rien (`p62:11`), que la structure retient à tort. Le
    témoin épingle les trois nombres pour qu'une réingestion ou un retypage se voie.
    """
    index = Index(load_corpus(ROOT / "data", allow_ungated=True))
    mesures, sans_deux_points = {}, []
    for doc_id in ("axa-lu-optihome-2017", "baloise-lu-home-2-2024"):
        doc = index.corpus.documents[doc_id]
        decisionnels = [b for b in doc.blocks if b.kind in KINDS_DECISIONNELS]
        amorces = [b for b in decisionnels if index.est_amorce_denumeration(b.block_id)]
        sans_deux_points += [b.block_id for b in amorces if not b.text.rstrip().endswith(":")]
        mesures[doc_id] = (len(decisionnels), len(amorces))
    assert mesures == {"axa-lu-optihome-2017": (785, 51), "baloise-lu-home-2-2024": (462, 21)}
    assert sans_deux_points == ["axa-lu-optihome-2017:p36:6", "axa-lu-optihome-2017:p62:11"]


def test_le_rayon_daction_des_amorces_non_uniques_est_une_minorite_nommee() -> None:
    """Story 5.6 (L1f) — la borne du correctif, mesurée le 04/09/2026 sur les deux contrats servis.

    Une amorce d'énumération dont le texte se relit ailleurs dans le document était, depuis L1c, la
    seule que le code refusait de joindre — et c'est justement celle qui ouvre les exclusions, donc
    celle dont l'item a le plus besoin. Le compte dit l'ampleur de ce que L1f rouvre : ce n'est pas
    la majorité des amorces, c'est la minorité qui portait le plus de contexte perdu. Le témoin
    l'épingle pour qu'un changement d'ingestion ou de typage se voie.
    """
    index = Index(load_corpus(ROOT / "data", allow_ungated=True))
    mesures = {}
    for doc_id in ("axa-lu-optihome-2017", "baloise-lu-home-2-2024"):
        doc = index.corpus.documents[doc_id]
        amorces = {a for b in doc.blocks
                   if (a := index.amorce_de_lenumeration(b.block_id)) is not None}
        non_uniques = {a for a in amorces
                       if any(o.block_id != a and doc.block(a).text_norm in o.text_norm
                              for o in doc.blocks)}
        # Toutes restent adjacentes à leur item : le correctif les couvre, aucune n'est laissée.
        assert all(index.introduit_immediatement(index.amorce_de_lenumeration(b.block_id), b.block_id)
                   for b in doc.blocks if index.amorce_de_lenumeration(b.block_id) is not None)
        mesures[doc_id] = (len(amorces), len(non_uniques))
    assert mesures == {"axa-lu-optihome-2017": (58, 8), "baloise-lu-home-2-2024": (6, 2)}
