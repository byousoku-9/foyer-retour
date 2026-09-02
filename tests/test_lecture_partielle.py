"""Story 4.2f — une lecture partielle rend une réponse honnête, pas une panne.

Quand un pipeline a réellement lu une partie **bornée** du corpus mais qu'aucune affirmation ne
survit à la validation terminale, il n'y a ni absence à prouver ni panne à signaler : la lecture a eu
lieu, elle était insuffisante, et c'est un fait métier. Ces tests prouvent, sur un corpus
**synthétique et neutre** (aucun `block_id`, `doc_id`, page, titre, question ni vocabulaire de
témoin) :

1. les deux pipelines rendent un 200 typé — `found=false`, `complete=false`, `reason=None`, une
   `LecturePartielle` chiffrée, la lacune `lecture_bornee`, les affirmations écartées conservées ;
2. les vraies défaillances — retrieval vidé par le budget, appel de relance commencé puis échoué,
   corpus indisponible — gardent leur exception et leur code, à l'octet près ;
3. la classification ne dépend que de l'**état de lecture** : sous bijection des identifiants,
   décalage des positions, inversion de l'ordre et reformulation de la question, les décisions sont
   identiques — et un contrôle négatif prouve que la permutation ferait bien rougir un branchement
   sur un identifiant.

Le corpus est construit par une fonction paramétrée : la « permutation » n'est pas une retouche
après coup, c'est le même corpus régénéré sous d'autres identifiants, d'autres positions et un autre
ordre de lecture. Rien ici ne réutilise les fixtures des pipelines : elles portent le vocabulaire des
questions-témoins, que cette story s'interdit.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import anthropic
import httpx
import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.document import Document, Node
from server.app.domain.errors import BudgetExceeded, CorpusUnavailable, LlmUnavailable
from server.app.domain.ingest import ManifestEntry
from server.app.domain.profil import Profil
from server.app.domain.question import Faits
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines import sinistre as pipeline_sinistre
from server.app.pipelines.guide import repondre_guide
from server.app.steps.restituer import PHRASES_DE_LACUNE, PHRASES_DE_LECTURE_PARTIELLE
from tests.llm_fake import FakeAnthropic, fake_message

# --- le corpus synthétique, et sa permutation ----------------------------------------------------
#
# Trois textes inventés, sans rapport avec un guide, un contrat, un assureur ni une question-témoin.
# Les deux premiers partagent le terme cherché ; le troisième n'en porte aucun — c'est lui que
# l'ébauche citera, et qui ne sera donc jamais transmis.
TEXTE_LU = "Le registre commun est ouvert au comptoir pendant les semaines paires."
TEXTE_VOISIN = "Un second comptoir tient le même registre pour les semaines impaires."
TEXTE_FERME = "Les tarifs de reproduction sont affichés à l'entrée du hall."
CITATION_FERMEE = "tarifs de reproduction sont affichés"

QUESTION_INITIALE = "Quand le registre est-il consultable au comptoir ?"
QUESTION_REFORMULEE = "À quel moment peut-on consulter le registre auprès du comptoir ?"
TERMES = ["registre", "comptoir"]


def _document(prefixe: str, *, kind: str, decalage: int, inverser: bool) -> Document:
    """Le même contenu, sous d'autres identifiants, d'autres positions et un autre ordre de lecture.

    `decalage` renumérote les positions (`loc`) **et** les rangs (`seq`) : c'est la « bijection des
    `block_id` » et le « décalage des pages » que l'AC demande, appliqués ensemble puisque
    `block_id = {doc_id}:{loc}:{seq}` (AD-2). `inverser` renverse l'ordre de lecture des nœuds sous
    la racine — donc l'ordre dans lequel `chercher()` rendra ses candidats à score égal.
    """
    lettre = "p" if kind == "contrat" else "f"

    def bid(position: int, rang: int) -> str:
        return f"{prefixe}:{lettre}{position + decalage}:{rang + decalage}"

    blocs = [
        {"block_id": bid(1, 1), "loc": f"{lettre}{1 + decalage}", "seq": 1 + decalage,
         "kind": "para", "text": TEXTE_LU},
        {"block_id": bid(2, 1), "loc": f"{lettre}{2 + decalage}", "seq": 1 + decalage,
         "kind": "para", "text": TEXTE_VOISIN},
        {"block_id": bid(3, 1), "loc": f"{lettre}{3 + decalage}", "seq": 1 + decalage,
         "kind": "para", "text": TEXTE_FERME},
    ]
    noeuds = [
        Node(node_id=f"{prefixe}:n1", level=1, title="Première rubrique",
             items=[{"block_id": bid(1, 1)}]),
        Node(node_id=f"{prefixe}:n2", level=1, title="Deuxième rubrique",
             items=[{"block_id": bid(2, 1)}]),
        Node(node_id=f"{prefixe}:n3", level=1, title="Troisième rubrique",
             items=[{"block_id": bid(3, 1)}]),
    ]
    ordre = list(reversed(noeuds)) if inverser else noeuds
    racine = Node(node_id=f"{prefixe}:racine", level=0, title="Racine",
                  items=[{"node_id": n.node_id} for n in ordre])
    doc = Document(doc_id=prefixe, kind=kind, title="Document synthétique",
                   edition="edition-synthetique", nodes=[*ordre, racine], blocks=blocs)
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    return doc


def _index(prefixe: str = "doc-alpha", *, kind: str = "guide", decalage: int = 0,
           inverser: bool = False) -> Index:
    doc = _document(prefixe, kind=kind, decalage=decalage, inverser=inverser)
    manifest = {prefixe: ManifestEntry(status="servi", source_hash="sha-source",
                                       ingest_fingerprint="fp-1", document_hash="sha-doc",
                                       edition="edition-synthetique")}
    return Index(Corpus(documents={prefixe: doc}, manifest=manifest,
                        summaries={prefixe: "# Document synthétique\n- n1\n- n2\n- n3"}))


def _bloc_ferme(index: Index) -> str:
    """Le bloc qu'aucune recherche sur `TERMES` n'atteint — donc jamais transmis au modèle."""
    doc = next(iter(index.corpus.documents.values()))
    return next(b.block_id for b in doc.blocks if b.text == TEXTE_FERME)


# --- le scriptage du modèle ----------------------------------------------------------------------

def _settings_guide(prefixe: str, **kw) -> Settings:
    kw.setdefault("guide_doc_id", prefixe)
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _settings_sinistre(prefixe: str, **kw) -> Settings:
    kw.setdefault("sinistre_doc_id", prefixe)
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget() -> RequestBudget:
    return RequestBudget(deadline_s=30.0, max_attempts=6, max_cost_eur=0.20)


def _comprendre(*, question_resolue: str = QUESTION_INITIALE, terms: list[str] | None = None,
                **champs: Any) -> dict:
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "intent": "question", "question_resolue": question_resolue, "clarification": None,
        "language": "fr", "terms": terms if terms is not None else list(TERMES),
        "themes": [], "facettes": ["moment de consultation"],
        "bien": None, "evenement": None, "lieu": None, "cause": None, "moment": None,
        **champs}))


def _rediger(block_id: str) -> dict:
    """Une ébauche qui cite un bloc **réel mais non transmis** : la citation ne peut pas être retenue.

    C'est la configuration exacte de la story : la lecture a été bornée, ce qui a été lu n'a pas
    servi, et rien ne survit à la vérification.
    """
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": [{"text": "Une affirmation.", "kind": "factuel", "claim_ids": ["c1"]}],
        "claims": [{"claim_id": "c1", "text": "Une affirmation.",
                    "quotes": [{"block_id": block_id, "quote": CITATION_FERMEE}]}]}))


FAITS_NEUTRES = Faits(date="2030-01-02", lieu="local du comptoir", montant_eur=100.0,
                      description="Une demande de consultation du registre commun est restée sans "
                                  "réponse pendant deux semaines.")


async def _run_guide(index: Index, script: list, *, settings: Settings | None = None,
                     question: str = QUESTION_INITIALE, variant: str = "deterministe"):
    prefixe = next(iter(index.corpus.documents))
    settings = settings or _settings_guide(prefixe, retrieval_max_blocks=1)
    fake = FakeAnthropic(script)
    client = LlmClient(settings, anthropic_client=fake)
    answer, trace = await repondre_guide(question, [], Profil(), corpus=index.corpus, index=index,
                                         client=client, settings=settings, request_id="req-4-2f",
                                         budget=_budget(), variant=variant)
    return answer, trace, fake


async def _run_sinistre(index: Index, script: list, *, settings: Settings | None = None,
                        question: str = QUESTION_INITIALE, variant: str = "deterministe"):
    prefixe = next(iter(index.corpus.documents))
    settings = settings or _settings_sinistre(prefixe, retrieval_max_blocks=1)
    fake = FakeAnthropic(script)
    client = LlmClient(settings, anthropic_client=fake)
    answer, trace = await pipeline_sinistre.run(
        None, question, FAITS_NEUTRES, corpus=index.corpus, index=index, client=client,
        settings=settings, request_id="req-4-2f", budget=_budget(), variant=variant)
    return answer, trace, fake


def _script(index: Index, *, question_resolue: str = QUESTION_INITIALE) -> list:
    """Comprendre, rédiger, puis la relance d'AD-3 — identique, donc arrêtée sans seconde vérification."""
    ferme = _bloc_ferme(index)
    return [_comprendre(question_resolue=question_resolue), _rediger(ferme), _rediger(ferme)]


def _navigation(index: Index) -> dict:
    """Un tour de navigation par outils sur le corpus synthétique : chercher, puis ouvrir deux nœuds.

    `retrouver_outils` **exécute réellement** les outils sur l'index ; seul le choix des appels est
    scripté. C'est ce qui rend la variante servie éprouvable sans réseau.
    """
    prefixe = next(iter(index.corpus.documents))
    doc = index.corpus.documents[prefixe]
    lu = next(b.block_id for b in doc.blocks if b.text == TEXTE_LU)
    voisin = next(b.block_id for b in doc.blocks if b.text == TEXTE_VOISIN)
    return fake_message(model=TIERS["micro"], stop_reason="tool_use", content=[
        {"type": "tool_use", "id": "t-chercher", "name": "chercher",
         "input": {"termes": list(TERMES)}},
        {"type": "tool_use", "id": "t-ouvrir-1", "name": "ouvrir_noeud",
         "input": {"node_id": doc.node_of(lu), "focus_block_id": lu}},
        {"type": "tool_use", "id": "t-ouvrir-2", "name": "ouvrir_noeud",
         "input": {"node_id": doc.node_of(voisin), "focus_block_id": voisin}},
    ])


def _script_outils(index: Index, *, besoin_fondateur: bool = False) -> list:
    """Le même scénario, sur la variante **servie** : le tour d'outils s'insère après *comprendre*."""
    ferme = _bloc_ferme(index)
    navigation = [
        _navigation(index),
        # La conclusion de suffisance est un choix sémantique distinct de la navigation et passe
        # donc toujours par le plancher Sonnet, y compris lorsque le guide n'impose pas de kind.
        fake_message(model=TIERS["reason"], stop_reason="end_turn", content=[]),
    ]
    return [_comprendre(), *navigation, _rediger(ferme), _rediger(ferme)]


# --- 1. les deux pipelines rendent une réponse -----------------------------------------------------

async def test_le_guide_rend_une_reponse_chiffree_au_lieu_de_lever() -> None:
    """AC : aucune exception, `found=false`, `complete=false`, `reason=None`, compteurs exacts."""
    index = _index()
    answer, trace, fake = await _run_guide(index, _script(index))

    assert fake.remaining_script == 0
    assert answer.found is False and answer.complete is False
    # Aucune absence n'est affirmée : c'est l'invariant d'AD-1/NFR2, tenu sans 503.
    assert answer.reason is None
    assert answer.lecture_partielle is not None
    # Les compteurs sont ce qui a été **lu**, jamais la taille du document (3 blocs).
    assert answer.lecture_partielle.blocks_read == len(_retrieval(trace))
    assert answer.lecture_partielle.blocks_read == 1
    assert answer.lecture_partielle.nodes_read == 1
    assert answer.lecture_partielle.documents == [next(iter(index.corpus.documents))]
    # La réponse dit ce qui lui manque, et ce qui a été écarté reste montrable.
    assert PHRASES_DE_LACUNE["fr"]["lecture_bornee"] in answer.unknown
    assert [c.rejection_kind for c in answer.rejected_claims] == ["non_retrouvee"]
    # Le texte est celui du registre guide, composé par le code.
    assert answer.texte == PHRASES_DE_LECTURE_PARTIELLE["guide"]["fr"]
    assert [s.kind for s in answer.segments] == ["limite"]
    # La trace porte le check nommé par l'AC, et la chaîne s'est achevée par *restituer*.
    restituer = trace.steps[-1]
    assert restituer.name == "restituer"
    assert any(c.name == "lecture_partielle" for c in restituer.checks)
    assert trace.truncations == 1
    # Le guide n'a pas de verdict, et rien n'en fabrique un.
    assert answer.verdict is None


def _retrieval(trace: Any) -> list[str]:
    return next(s for s in trace.steps if s.name == "retrouver").opened_block_ids


async def test_le_sinistre_rend_la_meme_reponse_avec_son_verdict() -> None:
    """AC : idem, plus `ne_tranche_pas` — jamais un verdict de remplacement, jamais aucun verdict."""
    index = _index("doc-contrat", kind="contrat")
    answer, trace, fake = await _run_sinistre(index, _script(index))

    assert fake.remaining_script == 0
    assert answer.found is False and answer.complete is False and answer.reason is None
    assert answer.lecture_partielle is not None
    assert (answer.lecture_partielle.nodes_read, answer.lecture_partielle.blocks_read) == (1, 1)
    assert PHRASES_DE_LACUNE["fr"]["lecture_bornee"] in answer.unknown
    assert answer.rejected_claims
    # AD-6 : le `ne_tranche_pas` vient de la règle (0bis) appliquée à zéro clause affichée.
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    # `faits_compris` reste publié : sur l'écran le plus démuni, c'est souvent tout ce qu'il reste.
    assert answer.faits_compris is not None
    # La phrase est celle du **registre sinistre**, jamais celle du guide.
    assert answer.texte == PHRASES_DE_LECTURE_PARTIELLE["sinistre"]["fr"]
    assert answer.texte != PHRASES_DE_LECTURE_PARTIELLE["guide"]["fr"]
    assert trace.steps[-1].name == "restituer"


async def test_le_guide_rend_la_meme_reponse_sur_la_variante_servie() -> None:
    """P5 : `outils` est le **défaut** d'AD-1, donc le chemin que la production emprunte.

    Prouver la nouvelle branche sur la seule baseline déterministe laissait la variante réellement
    servie sans preuve de bout en bout — et c'est celle où le compteur de sections a le plus de
    façons de se tromper (blocs entrés par `definitions` ou comme dépendance, hors fenêtre).
    """
    index = _index()
    answer, trace, fake = await _run_guide(index, _script_outils(index), variant="outils")

    assert fake.remaining_script == 0 and trace.variant == "outils"
    assert answer.found is False and answer.complete is False and answer.reason is None
    assert answer.lecture_partielle is not None
    # Le compteur couvre **tous** les blocs transmis : jamais « 0 section lue » sous N passages.
    assert answer.lecture_partielle.blocks_read >= 1
    assert 1 <= answer.lecture_partielle.nodes_read <= answer.lecture_partielle.blocks_read
    assert answer.lecture_partielle.blocks_read == len(_retrieval(trace))
    assert PHRASES_DE_LACUNE["fr"]["lecture_bornee"] in answer.unknown
    assert any(c.name == "lecture_partielle" for c in trace.steps[-1].checks)


async def test_le_sinistre_rend_la_meme_reponse_sur_la_variante_servie() -> None:
    """Le pendant sinistre, sur la variante servie — verdict compris."""
    index = _index("doc-contrat", kind="contrat")
    answer, trace, fake = await _run_sinistre(
        index, _script_outils(index, besoin_fondateur=True), variant="outils")

    assert fake.remaining_script == 0 and trace.variant == "outils"
    assert answer.found is False and answer.complete is False and answer.reason is None
    assert answer.lecture_partielle is not None
    assert answer.lecture_partielle.blocks_read >= 1
    assert 1 <= answer.lecture_partielle.nodes_read <= answer.lecture_partielle.blocks_read
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert PHRASES_DE_LACUNE["fr"]["lecture_bornee"] in answer.unknown


async def test_le_compteur_de_sections_ne_contredit_jamais_celui_des_passages() -> None:
    """La propriété que P5 réclame, sur les **deux** variantes : tout passage transmis a sa section.

    AD-2 garantit qu'un bloc est rattaché à exactement un nœud ; il ne peut donc pas exister de
    lecture partielle annonçant des passages sans aucune section — deux chiffres qui se
    contrediraient sous les yeux de l'utilisateur.
    """
    for variant, script in (("deterministe", _script), ("outils", _script_outils)):
        index = _index(f"doc-{variant}")
        answer, _trace, _fake = await _run_guide(index, script(index), variant=variant)
        lue = answer.lecture_partielle
        assert lue is not None, variant
        assert lue.blocks_read >= 1, variant
        assert lue.nodes_read >= 1, variant
        assert lue.nodes_read <= lue.blocks_read, variant


async def test_les_compteurs_ne_sont_pas_la_taille_du_document() -> None:
    """Le défaut que la story ferme : `AbsenceProof.blocks_scanned` publie `len(document.blocks)`,
    c'est-à-dire l'annonce d'un balayage exhaustif. Les compteurs de la lecture partielle sont
    strictement ceux de ce qui est parti au modèle."""
    index = _index()
    document = next(iter(index.corpus.documents.values()))
    answer, _trace, _fake = await _run_guide(index, _script(index))
    assert answer.lecture_partielle is not None
    assert answer.lecture_partielle.blocks_read < len(document.blocks)
    assert answer.lecture_partielle.nodes_read < len([n for n in document.nodes if n.level == 1])


async def test_un_refus_sans_troncature_reste_un_refus_prouve() -> None:
    """Le pendant : lecture **complète** et zéro claim ⇒ l'`AbsenceProof` dit vrai, et rien ne change."""
    index = _index()
    prefixe = next(iter(index.corpus.documents))
    answer, trace, _fake = await _run_guide(
        index, _script(index), settings=_settings_guide(prefixe))
    assert trace.truncations == 0
    assert answer.found is False and answer.lecture_partielle is None
    assert answer.reason is not None and answer.reason.kind == "claims_rejetes"
    # Et la lacune de lecture bornée n'apparaît pas là où rien n'a été borné.
    assert PHRASES_DE_LACUNE["fr"]["lecture_bornee"] not in answer.unknown


# --- 2. les vraies défaillances gardent leur erreur ------------------------------------------------

async def test_un_retrieval_vide_par_le_budget_reste_une_erreur_terminale() -> None:
    """AD-1 : aucun bloc transmis ⇒ rien n'a été lu, donc rien à chiffrer — l'erreur reste due."""
    index = _index()
    prefixe = next(iter(index.corpus.documents))
    settings = _settings_guide(prefixe, retrieval_max_tokens=1)
    with pytest.raises(BudgetExceeded, match="aucune absence du corpus n'est affirmée") as capture:
        await _run_guide(index, [_comprendre()], settings=settings)
    assert capture.value.code.value == "budget_exceeded"
    assert capture.value.trace is not None and capture.value.trace.truncations == 1


async def test_un_retrieval_vide_par_le_budget_reste_une_erreur_terminale_en_sinistre() -> None:
    index = _index("doc-contrat", kind="contrat")
    prefixe = next(iter(index.corpus.documents))
    settings = _settings_sinistre(prefixe, retrieval_max_tokens=1)
    with pytest.raises(BudgetExceeded, match="aucune absence du contrat n'est affirmée"):
        await _run_sinistre(index, [_comprendre()], settings=settings)


async def test_un_appel_de_relance_commence_puis_echoue_reste_une_panne() -> None:
    """AD-16 : « un appel commencé qui échoue reste terminal ». La lecture est pourtant bornée — ce
    n'est pas la troncature qui décide, c'est le fait qu'un appel ait été facturé puis raté."""
    index = _index()
    panne = anthropic.OverloadedError("529", response=httpx.Response(
        529, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
    with pytest.raises(LlmUnavailable) as capture:
        await _run_guide(index, [_comprendre(), _rediger(_bloc_ferme(index)), panne])
    assert capture.value.code.value == "llm_unavailable"  # 503, comme avant le diff
    assert capture.value.trace is not None


async def test_un_corpus_indisponible_reste_une_erreur_terminale() -> None:
    """Le court-circuit antérieur à *retrouver* ne passe jamais par le nouveau chemin."""
    index = _index()
    settings = _settings_guide("document-absent")
    with pytest.raises(CorpusUnavailable):
        await _run_guide(index, [], settings=settings)


# --- 3. la classification ne dépend que de l'état de lecture ---------------------------------------

def _decision(answer: Any) -> tuple:
    """Ce que la story classe : l'état publié, sans aucun identifiant ni texte de corpus."""
    return (
        answer.found,
        answer.complete,
        answer.reason is None,
        answer.lecture_partielle is not None,
        answer.lecture_partielle.nodes_read if answer.lecture_partielle else None,
        answer.lecture_partielle.blocks_read if answer.lecture_partielle else None,
        tuple(answer.unknown),
        answer.verdict.value if answer.verdict else None,
        tuple(c.rejection_kind for c in answer.rejected_claims),
    )


VARIANTES = [
    ("doc-alpha", 0, False, QUESTION_INITIALE),
    ("autre-document", 40, True, QUESTION_REFORMULEE),
    ("troisieme-document", 11, True, QUESTION_INITIALE),
]


async def test_la_classification_est_invariante_sous_permutation_du_guide() -> None:
    """AC : bijection des identifiants, décalage des positions, inversion de l'ordre, reformulation
    de la question ⇒ décisions identiques. Rien de ce qui décide ne lit un identifiant."""
    decisions = set()
    for prefixe, decalage, inverser, question in VARIANTES:
        index = _index(prefixe, decalage=decalage, inverser=inverser)
        answer, _trace, _fake = await _run_guide(
            index, _script(index, question_resolue=question), question=question)
        decision = _decision(answer)
        decisions.add(decision)
        # Le seul identifiant publié est celui du document interrogé : il varie avec lui, et c'est
        # tout ce qui varie.
        assert answer.lecture_partielle is not None
        assert answer.lecture_partielle.documents == [prefixe]
    assert len(decisions) == 1, decisions


async def test_la_classification_est_invariante_sous_permutation_du_sinistre() -> None:
    decisions = set()
    for prefixe, decalage, inverser, question in VARIANTES:
        index = _index(f"contrat-{prefixe}", kind="contrat", decalage=decalage, inverser=inverser)
        answer, _trace, _fake = await _run_sinistre(
            index, _script(index, question_resolue=question), question=question)
        decisions.add(_decision(answer))
    assert len(decisions) == 1, decisions
    assert next(iter(decisions))[7] == "ne_tranche_pas"


async def test_controle_negatif_une_decision_branchee_sur_un_identifiant_est_detectee() -> None:
    """La garde ci-dessus ne vaut que si la permutation ferait **réellement** rougir un branchement
    interdit. Ce décideur fautif classe sur un `block_id` codé en dur : il change d'avis sous
    permutation, et c'est exactement ce que les deux tests précédents interdisent au vrai code."""
    original = _index("doc-alpha")
    permute = _index("autre-document", decalage=40, inverser=True)

    def mauvais_classificateur(index: Index) -> bool:
        return any(b.block_id == "doc-alpha:f3:1"
                   for b in next(iter(index.corpus.documents.values())).blocks)

    assert mauvais_classificateur(original) is True
    assert mauvais_classificateur(permute) is False


def test_le_vocabulaire_du_corpus_synthetique_est_neutre() -> None:
    """Never de la story : aucun `block_id`, `doc_id`, page, titre, question ou mot de témoin."""
    source = (inspect.getsource(_document) + inspect.getsource(_index)
              + TEXTE_LU + TEXTE_VOISIN + TEXTE_FERME + CITATION_FERMEE
              + QUESTION_INITIALE + QUESTION_REFORMULEE + repr(TERMES)
              + FAITS_NEUTRES.description).lower()
    interdits = ("axa", "baloise", "optihome", "lux-guide", "bougie", "mobilier", "canape",
                 "soudain", "chaleur", "biergercenter", "arrivée", "arrivee", "sinistre",
                 "assur", "franchise", "garantie", "p34", "f1:2")
    for interdit in interdits:
        assert interdit not in source, f"vocabulaire non neutre : {interdit!r}"
