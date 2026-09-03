"""Amendement AD-1 du 03/09/2026 — *naviguer*, hors réseau : ce que le code sert, borne et rend.

Cinq faits, et rien d'autre : la boucle d'outils tourne pour de bon sur le corpus ; seul
`ouvrir_noeud` rend un bloc citable ; le budget de lecture **refuse** en disant ce qu'il refuse ;
l'ébauche sort au schéma exact de *rédiger* avec ses deux projections ; la relance est un message de
plus dans la **même** conversation. Le préfixe — sommaire complet compris — reste byte-identique
d'un appel à l'autre, et chaque requête demande la réflexion adaptative.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.answer import AnswerDraft
from server.app.domain.document import Document, Node
from server.app.domain.ingest import ManifestEntry
from server.app.domain.errors import LlmParse
from server.app.domain.question import Faits, ParsedQuestion, QuestionScope
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import EFFORT, TIERS
from server.app.llm.pricing import estimate_tokens
from server.app.steps.naviguer import (OUTILS, TOOL_CHOICE_AUCUN, Navigation, _rendre_blocs,
                                       blocs_du_noeud, sommaire_complet)
from tests.llm_fake import FakeAnthropic, fake_message

DEFAUT = object()  # « faits non précisés » : distinct de « pas de faits », qui est le guide
DOC_ID = "texte-de-test"
SOCLE = f"{DOC_ID}:socle"
ANNEXE = f"{DOC_ID}:annexe"
REGLE = f"{DOC_ID}:p1:2"
ITEM = f"{DOC_ID}:p2:1"
TITRE = f"{DOC_ID}:p1:1"
TEXTE_REGLE = ("Le texte prend en charge la situation décrite lorsque le signalement est déposé "
               "dans le délai prévu, sous réserve des exclusions ci-après.")
TEXTE_ITEM = ("Sont également pris en charge les épisodes répertoriés survenus sans le concours "
              "d'un tiers identifié.")


def _corpus() -> tuple[Corpus, Index]:
    """Deux sections, trois blocs : assez pour lire, refuser, citer — et rien de plus."""
    blocs = [
        {"block_id": TITRE, "loc": "p1", "seq": 1, "kind": "heading",
         "text": "Prise en charge"},
        {"block_id": REGLE, "loc": "p1", "seq": 2, "kind": "garantie", "text": TEXTE_REGLE,
         "kind_source": "manual"},
        {"block_id": ITEM, "loc": "p2", "seq": 1, "kind": "garantie", "text": TEXTE_ITEM,
         "kind_source": "manual"},
    ]
    document = Document(
        doc_id=DOC_ID, kind="contrat", title="Texte de test", edition="git:test",
        nodes=[Node(node_id=SOCLE, level=1, title="Socle",
                    items=[{"block_id": TITRE}, {"block_id": REGLE}]),
               Node(node_id=ANNEXE, level=1, title="Annexe", items=[{"block_id": ITEM}]),
               Node(node_id=f"{DOC_ID}:root", level=0, title="Texte",
                    items=[{"node_id": SOCLE}, {"node_id": ANNEXE}])],
        blocks=blocs)
    for bloc in document.blocks:
        bloc.text_norm = normalize(bloc.text)
    corpus = Corpus(
        documents={DOC_ID: document},
        manifest={DOC_ID: ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                                        document_hash="d", edition="git:test")},
        summaries={DOC_ID: "# Texte de test"})
    return corpus, Index(corpus)


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _navigation(script: list[Any], *, prompt: str = "naviguer_sinistre",
                faits: Any = DEFAUT, scope: Any = None,
                **reglages: Any) -> tuple[Navigation, FakeAnthropic]:
    corpus, index = _corpus()
    settings = _settings(**reglages)
    fake = FakeAnthropic(script)
    parsed = ParsedQuestion(question_resolue="Le signalement déposé est-il pris en charge ?",
                            intent="question", terms=["prise en charge", "signalement"],
                            facettes=["prise en charge", "délai"],
                            **({"scope": scope} if scope is not None else {}))
    navigation = Navigation(
        parsed, corpus=corpus, index=index, dictionnaire=None, doc_id=DOC_ID, settings=settings,
        client=LlmClient(settings, anthropic_client=fake),
        request_budget=RequestBudget(deadline_s=100.0, max_attempts=8, max_cost_eur=0.75),
        prompt=prompt,
        faits=Faits(description="Un signalement déposé.") if faits is DEFAUT else faits)
    return navigation, fake


def _tour_doutils(*appels: dict[str, Any], thinking: int = 0) -> dict[str, Any]:
    message = fake_message(model=TIERS["reason"], stop_reason="tool_use", content=[
        {"type": "tool_use", "id": f"t{rang}", **appel} for rang, appel in enumerate(appels)])
    message["usage"]["output_tokens_details"] = {"thinking_tokens": thinking}
    return message


def _fin_de_lecture(thinking: int = 0) -> dict[str, Any]:
    message = fake_message(model=TIERS["reason"], stop_reason="end_turn", text="PRÊT")
    message["usage"]["output_tokens_details"] = {"thinking_tokens": thinking}
    return message


def _ebauche(quotes: list[dict[str, str]] | None = None, thinking: int = 0) -> dict[str, Any]:
    quotes = quotes or [{"block_id": REGLE, "quote": "prend en charge la situation décrite"}]
    draft = {"segments": [{"text": "Le texte prend en charge la situation décrite.",
                           "kind": "factuel", "claim_ids": ["c1"]},
                          {"text": "Ce que je ne sais pas.", "kind": "limite", "claim_ids": []}],
             "claims": [{"claim_id": "c1",
                         "text": "Le texte prend en charge la situation décrite.",
                         "quotes": quotes}]}
    message = fake_message(model=TIERS["reason"], text=json.dumps(draft, ensure_ascii=False))
    message["usage"]["output_tokens_details"] = {"thinking_tokens": thinking}
    return message


def _tronquee() -> dict[str, Any]:
    """Un tour terminal coupé par son plafond : du JSON commencé, `stop_reason=max_tokens`.

    C'est la forme exacte du fait mesuré au gate Baloise du 03/09/2026 (`b-bougie-canape` rép. 3) :
    la réflexion adaptative a consommé le plafond partagé, et le contrat JSON s'arrête au milieu.
    """
    message = fake_message(model=TIERS["reason"], stop_reason="max_tokens",
                           text='{"segments": [{"text": "Le texte prend en')
    message["usage"]["output_tokens_details"] = {"thinking_tokens": 3000}
    return message


class _BudgetSansResteApres(RequestBudget):
    """Un budget dont le reste tombe sous la durée majorée d'une reprise, après N appels servis.

    Le témoin ne peut pas obtenir ce reste-là en raccourcissant `deadline_s` : la durée majorée d'un
    appel au plafond du tour terminal (≈ 61 s) borne aussi le **premier** appel, qui ne partirait
    alors pas non plus (C2). Ce qu'on simule est le seul cas réel — une chaîne qui a consommé son
    temps avant d'arriver là.
    """

    def __init__(self, fake: FakeAnthropic, *, apres: int, reste: float, **kw: Any) -> None:
        super().__init__(**kw)
        self._fake, self._apres, self._reste = fake, apres, reste

    def remaining(self) -> float:
        return super().remaining() if len(self._fake.requests) < self._apres else self._reste


def _ebauche_avec_ecarts(ecartes: list[dict[str, str]]) -> dict[str, Any]:
    """Une ébauche qui **déclare** ce qu'elle écarte : le schéma facultatif de T11."""
    draft = json.loads(_ebauche()["content"][0]["text"])
    draft["blocs_ecartes"] = ecartes
    message = fake_message(model=TIERS["reason"], text=json.dumps(draft, ensure_ascii=False))
    message["usage"]["output_tokens_details"] = {"thinking_tokens": 0}
    return message


# --- 1. la boucle d'outils ---------------------------------------------------------------


async def test_les_quatre_outils_tournent_et_seule_louverture_rend_citable() -> None:
    """`chercher` et `definitions` **proposent** ; seul `ouvrir_noeud` fait entrer un bloc.

    Le tour demande les quatre outils dans la même réponse. `chercher` propose le bloc de l'annexe,
    que le modèle **n'ouvre pas** : il ne doit apparaître ni dans les blocs transmis à la suite de
    la chaîne, ni parmi les blocs citables. C'est le fait qu'AD-1 nomme (« ses résultats ne sont pas
    transmis à *rédiger*, ils sont offerts au modèle »).
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "sommaire", "input": {}},
                      {"name": "chercher", "input": {"termes": ["épisodes répertoriés"]}},
                      {"name": "definitions", "input": {"termes": ["signalement"]}},
                      {"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture()])

    step = await navigation.lire()
    retrieval = navigation.retrieval()

    resultats = fake.requests[1]["messages"][-1]["content"]
    rendus = {appel["tool_use_id"]: appel["content"] for appel in resultats}
    assert SOCLE in rendus["t0"] and ANNEXE in rendus["t0"]          # le sommaire complet
    assert ITEM in rendus["t1"] and "extrait" in rendus["t1"]        # `chercher` propose
    assert TEXTE_REGLE in rendus["t3"]                               # `ouvrir_noeud` sert le texte
    # Proposé n'est pas lu : le bloc de l'annexe n'entre nulle part.
    assert [b.block_id for b in retrieval.blocs] == [TITRE, REGLE]
    assert retrieval.opened_node_ids == [SOCLE] and not retrieval.truncated
    assert step.name == "retrouver" and len(step.calls) == 2
    assert all(call.tools == [outil["name"] for outil in OUTILS] for call in step.calls)


async def test_chaque_requete_demande_la_reflexion_adaptative_et_le_meme_prefixe() -> None:
    """Le paramètre que le prototype a mesuré manquant — et le préfixe qui reste cacheable."""
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture(), _ebauche()])

    await navigation.lire()
    await navigation.rediger()

    assert [requete["thinking"] for requete in fake.requests] == [{"type": "adaptive"}] * 3
    prefixes = {requete["system"][0]["text"] for requete in fake.requests}
    assert len(prefixes) == 1  # byte-identique : le sommaire n'est écrit qu'une fois (AD-9)
    prefixe = prefixes.pop()
    corpus, _index = _corpus()
    assert sommaire_complet(corpus, DOC_ID) in prefixe
    assert all(requete["system"][0]["cache_control"]["type"] == "ephemeral"
               for requete in fake.requests)


async def test_seul_le_tour_terminal_paie_leffort_releve() -> None:
    """Story 5.6, T11 — `navigation_draft_effort` ne touche que l'appel qui choisit les clauses.

    Les tours d'outils ouvrent des nœuds : ils réfléchissent déjà (62 à 574 tokens mesurés) et
    gardent le défaut de leur palier. Le tour terminal, lui, est mesuré à **0 token** sur les trois
    runs A16 de `f858a28`, et c'est le seul appel de la chaîne qui arrête quelles clauses sont
    citées — les deux omissions de la matinée du 03/09/2026 y tombent toutes les deux.
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture(), _ebauche(), _ebauche()], navigation_draft_effort="high")

    await navigation.lire()
    await navigation.rediger()
    # La relance est le même appel dans le même fil : elle paie le même effort.
    await navigation.relancer("motif")

    outils, _fin, terminal, relance = fake.requests
    defaut_du_palier = EFFORT["reason"]
    assert [requete["output_config"]["effort"] for requete in (outils, _fin)] == \
        [defaut_du_palier] * 2
    assert terminal["output_config"]["effort"] == "high" != defaut_du_palier
    assert relance["output_config"]["effort"] == "high"
    # Le plafond, lui, ne bouge pas : `high` achète de la profondeur dans la place déjà dérivée.
    assert terminal["max_tokens"] == _settings().navigation_rediger_max_tokens


async def test_le_tour_terminal_porte_linventaire_exact_des_blocs_decisionnels_ouverts() -> None:
    """Story 5.6, T11 — le code liste ce qu'il a servi ; il n'en retient rien (AD-1).

    Sur les 17 ébauches intégrées du 03/09/2026, l'étage encore variable est l'omission d'une
    clause **lue** (≈ 3/17). Rien dans le fil ne rappelait au tour terminal la liste de ce qu'il
    avait ouvert : le texte y est, dispersé dans des résultats d'outils ; l'inventaire, non. Le code
    le connaît exactement — il l'écrit, avec la règle qui manquait, et s'arrête là.

    Ce que le témoin épingle est précisément la frontière : **tous** les blocs décisionnels ouverts
    sont listés, et rien d'autre. Un sous-ensemble serait une sélection du code ; un bloc ouvert
    mais non décisionnel, ou un bloc décisionnel jamais ouvert, seraient l'un et l'autre une
    invention.
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}},
                      {"name": "ouvrir_noeud", "input": {"node_id": ANNEXE}}),
        _fin_de_lecture(), _ebauche()])

    await navigation.lire()
    await navigation.rediger()

    message = fake.requests[-1]["messages"][-1]["content"]
    lignes = [ligne for ligne in message.splitlines() if ligne.startswith("- ")]
    # Les deux garanties ouvertes, et elles seules : `TITRE` est ouvert aussi mais c'est un
    # `heading`, il ne décide de rien et l'inventaire l'ignore.
    assert TITRE in navigation.ouverts and REGLE in navigation.ouverts
    assert [ligne.split()[1] for ligne in lignes] == [REGLE, ITEM]
    assert f"- {REGLE} (garantie) — Socle : « {TEXTE_REGLE[:60]}" in message
    assert "Blocs décisionnels que ta lecture a ouverts (2) :" in message
    # La règle que le code a le droit de dire — celle qui manquait sur les omissions mesurées.
    assert "une décision par bloc" in message and "n'est jamais redondante" in message
    # Et rien de plus : le code ne dit pas lequel viser, ni dans quel ordre les traiter.
    assert ITEM not in message.split("Blocs décisionnels")[0]


async def test_une_ebauche_qui_ecarte_un_bloc_lu_le_trace() -> None:
    """AD-4 — l'omission d'une clause lue se voit dans la trace, ou elle est silencieuse.

    Sans ce check, rien ne distingue « le modèle a jugé ce bloc hors sujet » de « le modèle l'a
    oublié » : la réponse est cohérente et aucun contrôle ne rougit. Les deux se corrigent
    différemment. Un identifiant qu'aucune ouverture n'a servi est recoupé ici, sur ce que le code
    a réellement rendu, et n'est pas republié (AD-15).
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}},
                      {"name": "ouvrir_noeud", "input": {"node_id": ANNEXE}}),
        _fin_de_lecture(),
        _ebauche_avec_ecarts([{"block_id": ITEM, "motif": "porte sur un tiers, hors des faits"},
                              {"block_id": TITRE, "motif": "un titre"},
                              {"block_id": f"{DOC_ID}:invente", "motif": "jamais ouvert"}])])

    await navigation.lire()
    draft, step = await navigation.rediger()

    assert [(e.block_id, e.motif) for e in draft.blocs_ecartes][0] == (
        ITEM, "porte sur un tiers, hors des faits")
    (check,) = [c for c in step.checks if c.name == "blocs_decisionnels_ecartes"]
    assert check.ok is False
    assert f"1 bloc(s) décisionnel(s) lu(s) écarté(s) par la rédaction ({ITEM}) sur 2 ouvert(s)" \
        in check.detail
    # `TITRE` (heading) et l'identifiant inventé sont comptés ensemble comme hors inventaire, et
    # aucun des deux n'est recopié dans la trace.
    assert "2 identifiant(s) écarté(s)" in check.detail
    assert TITRE not in check.detail and "invente" not in check.detail
    # Une ébauche sans écart ne publie pas le check : c'est un fait, pas une case à cocher.
    navigation2, _fake2 = _navigation([_fin_de_lecture(), _ebauche()])
    await navigation2.lire()
    _draft2, step2 = await navigation2.rediger()
    assert not [c for c in step2.checks if c.name == "blocs_decisionnels_ecartes"]


# --- 2. le budget de lecture -------------------------------------------------------------


async def test_une_ouverture_refusee_par_le_budget_le_dit_au_modele_et_borne_la_lecture() -> None:
    """Le budget s'applique au **refus**, jamais à la sélection : rien n'est coupé en silence.

    Le premier nœud passe, le second est refusé : le modèle reçoit son coût et ce qu'il reste, la
    lecture est déclarée bornée (`truncated`), et les blocs laissés fermés sont publiés — sans quoi
    la chaîne affirmerait une absence sur une borne qui est la nôtre (NFR2).
    """
    corpus, _index = _corpus()
    budget_socle = estimate_tokens(_rendre_blocs(blocs_du_noeud(corpus, DOC_ID, SOCLE)),
                                   _settings())
    navigation, fake = _navigation(
        [_tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}},
                       {"name": "ouvrir_noeud", "input": {"node_id": ANNEXE}}),
         _fin_de_lecture()],
        navigation_budget_tokens=budget_socle)

    step = await navigation.lire()
    retrieval = navigation.retrieval()

    refus = fake.requests[1]["messages"][-1]["content"][1]["content"]
    assert refus.startswith("budget de lecture insuffisant")
    assert "il en reste 0" in refus and "conclus avec ce que tu as déjà lu" in refus
    assert [b.block_id for b in retrieval.blocs] == [TITRE, REGLE]
    assert retrieval.truncated and retrieval.discarded_block_ids == [ITEM]
    assert any(check.name == "lecture_refusee" and not check.ok for check in step.checks)
    assert step.budget_lecture is not None and step.budget_lecture.tokens_remaining == 0


async def test_le_plafond_de_tours_borne_la_lecture_sans_affirmer_dabsence() -> None:
    navigation, _fake = _navigation(
        [_tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
         _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": ANNEXE}})],
        navigation_max_llm_turns=2)

    step = await navigation.lire()

    assert navigation.tours == 2
    assert navigation.retrieval().truncated
    assert any(check.name == "tours_epuises" and not check.ok for check in step.checks)


# --- 3. l'ébauche, au schéma exact de *rédiger* ------------------------------------------


async def test_lebauche_sort_au_schema_de_rediger_avec_ses_deux_projections() -> None:
    """`AnswerDraft` reste la sortie structurée terminale (AD-3), projections comprises.

    Les deux extraits d'un même bloc sont fusionnés en un passage contigu — sans quoi l'invariant
    du domaine rendrait l'ébauche terminale — et les claims du sinistre deviennent les segments
    factuels effectivement soumis à *vérifier*.
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture(),
        _ebauche(quotes=[{"block_id": REGLE, "quote": "prend en charge"},
                         {"block_id": REGLE, "quote": "dans le délai prévu"}])])

    await navigation.lire()
    draft, step = await navigation.rediger()

    assert isinstance(draft, AnswerDraft)
    assert [quote.block_id for claim in draft.claims for quote in claim.quotes] == [REGLE]
    quote = draft.claims[0].quotes[0].quote
    assert quote.startswith("prend en charge") and quote.endswith("dans le delai prevu")
    assert [segment.kind for segment in draft.segments] == ["factuel", "limite"]
    assert draft.segments[0].claim_ids == ["c1"]
    assert any(check.name == "quotes_fusionnees" for check in step.checks)
    assert step.name == "rediger" and len(step.calls) == 1
    # L'ébauche est demandée dans le fil : la conversation porte déjà les résultats d'outils.
    messages = fake.requests[-1]["messages"]
    assert messages[0] == fake.requests[0]["messages"][0]
    assert "Rends maintenant l'ébauche" in messages[-1]["content"]
    assert "Langue de rédaction" in messages[-1]["content"]


async def test_le_guide_emprunte_la_meme_etape_avec_son_seul_prompt_de_redaction() -> None:
    """Un seul chemin pour les deux sujets : aucune branche par document ni par pipeline."""
    navigation, fake = _navigation(
        [_tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
         _fin_de_lecture(), _ebauche()],
        prompt="naviguer_guide", faits=None)

    await navigation.lire()
    draft, _step = await navigation.rediger()

    assert "naviguer" in fake.requests[0]["system"][0]["text"].lower()
    assert "faits" not in fake.requests[0]["messages"][0]["content"]
    # Sans faits, aucune projection sinistre : le brouillon du guide reste à l'octet près.
    assert [segment.kind for segment in draft.segments] == ["factuel", "limite"]


# --- 4. la relance, dans la même conversation --------------------------------------------


async def test_la_relance_est_un_message_de_plus_dans_la_meme_conversation() -> None:
    """AD-3 sans second dialogue : le préfixe est déjà écrit, le modèle a tout sous les yeux.

    La relance n'ouvre aucun tour d'outils, reprend la conversation entière — résultats de lecture
    **et** ébauche précédente — et porte le motif délimité (AD-15) plus les acquis à reconduire,
    dont un identifiant inventé ne peut pas faire partie.
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture(), _ebauche(), _ebauche()])

    await navigation.lire()
    draft, _step = await navigation.rediger()
    relance, step = await navigation.relancer("la citation n'a pas été retrouvée",
                                              blocs_a_conserver=[REGLE, "bloc:invente"])

    assert isinstance(relance, AnswerDraft) and step.name == "rediger"
    messages = fake.requests[-1]["messages"]
    # Le fil entier est repris : première demande, tours d'outils, ébauche précédente, puis le motif.
    assert messages[0] == fake.requests[0]["messages"][0]
    assert any(isinstance(m["content"], list)
               and any(part.get("type") == "tool_result" for part in m["content"])
               for m in messages)
    assert draft.model_dump_json() in [m["content"] for m in messages if isinstance(m["content"], str)]
    dernier = messages[-1]["content"]
    assert '<untrusted kind="motif">' in dernier
    assert "la citation n'a pas été retrouvée" in dernier
    assert f"Acquis à reconduire : {REGLE}" in dernier and "bloc:invente" not in dernier
    # Aucun tour d'outils supplémentaire : quatre appels en tout, dont deux de lecture.
    assert len(fake.requests) == 4 and navigation.tours == 2


# --- 5. ce que la trace publie -----------------------------------------------------------


async def test_la_trace_publie_les_tours_les_noeuds_la_lecture_et_la_reflexion() -> None:
    """AD-10 : par requête, les tours, les nœuds ouverts, les tokens lus, la réflexion, le coût."""
    navigation, _fake = _navigation([
        _tour_doutils({"name": "chercher", "input": {"termes": ["prise en charge"]}},
                      thinking=56),
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}, thinking=31),
        _fin_de_lecture(thinking=12),
        _ebauche(thinking=204)])

    step_lecture = await navigation.lire()
    _draft, step_redaction = await navigation.rediger()

    detail = next(c.detail for c in step_lecture.checks if c.name == "navigation")
    assert "3 tour(s)" in detail and f"1 nœud(s) ouvert(s) ({SOCLE})" in detail
    assert "1 recherche(s)" in detail and "réflexion 99 tokens" in detail
    assert step_lecture.opened_block_ids == [TITRE, REGLE]
    assert step_lecture.budget_lecture is not None
    assert 0 < step_lecture.budget_lecture.tokens_used < 12000
    assert [call.thinking for call in step_lecture.calls] == [56, 31, 12]
    # Le tour **qui cite** est celui qui doit réfléchir : son compte est publié à part.
    assert [call.thinking for call in step_redaction.calls] == [204]
    assert "réflexion 204 tokens" in next(
        c.detail for c in step_redaction.checks if c.name == "ebauche_dans_la_conversation")
    assert step_lecture.usage.cost_eur > 0 and step_redaction.usage.cost_eur > 0


# --- 5. la place de l'ébauche, re-dérivée sur la mesure (story 5.6 T1b) -------------------


def _ebauche_a_six_claims() -> dict[str, Any]:
    """Six clauses décisionnelles citées, plus une articulation et une réserve.

    C'est la forme que les trois réponses A16 du pipeline appelaient sans pouvoir la rendre :
    deux sous-questions × la garantie de base, l'option et l'exclusion ou la condition qui la borne.
    """
    claims = [{"claim_id": f"c{rang}", "text": f"Le texte dit la clause {rang}.",
               "quotes": [{"block_id": REGLE if rang % 2 else ITEM,
                           "quote": "prend en charge la situation décrite" if rang % 2
                                    else "Sont également pris en charge les épisodes répertoriés"}]}
              for rang in range(1, 7)]
    draft = {"segments": [*({"text": f"Le texte dit la clause {rang}.", "kind": "factuel",
                             "claim_ids": [f"c{rang}"]} for rang in range(1, 7)),
                          {"text": "Deux dommages distincts.", "kind": "transition",
                           "claim_ids": []},
                          {"text": "Ce que je ne sais pas.", "kind": "limite", "claim_ids": []}],
              "claims": claims}
    return fake_message(model=TIERS["reason"], text=json.dumps(draft, ensure_ascii=False))


async def test_lebauche_de_navigation_tient_six_claims_avec_son_articulation_et_sa_reserve() -> None:
    """La borne re-dérivée est celle qui part au prompt **et** celle que la projection applique.

    `draft_max_claims` (4) venait de `rediger_max_tokens` (2 048), calibré sur l'ancien *rédiger*.
    La navigation rédige dans une conversation dont le tour terminal demande la réflexion
    adaptative, et son plafond est `navigation_rediger_max_tokens`. Le témoin tient les trois bouts
    ensemble : la borne annoncée, la borne appliquée, et le plafond de sortie qui va avec.
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}},
                      {"name": "ouvrir_noeud", "input": {"node_id": ANNEXE}}),
        _fin_de_lecture(), _ebauche_a_six_claims()])
    settings = navigation.settings

    await navigation.lire()
    draft, step = await navigation.rediger()

    # Rien n'est écarté : les six claims passent, et la réserve garde sa place sous la borne de
    # segments — c'est elle que six segments factuels sous `draft_max_segments` (6) chassaient.
    assert [claim.claim_id for claim in draft.claims] == [f"c{rang}" for rang in range(1, 7)]
    assert [segment.kind for segment in draft.segments] == (
        ["factuel"] * 6 + ["transition", "limite"])
    assert not any(check.name == "claims_hors_borne_ecartees" for check in step.checks)
    # La borne annoncée au préfixe est celle de la navigation, pas celle de l'ancien *rédiger*.
    prefixe = fake.requests[0]["system"][0]["text"]
    assert f"au plus {settings.navigation_draft_max_segments} segments et " \
           f"{settings.navigation_draft_max_claims} claims" in prefixe
    # Et le tour terminal part sous le plafond dérivé pour lui, pas sous celui de *rédiger*.
    assert fake.requests[-1]["max_tokens"] == settings.navigation_rediger_max_tokens == 5056


async def test_la_borne_de_claims_de_la_navigation_mord_toujours_quand_on_labaisse() -> None:
    """L'autre sens : la borne n'a pas disparu, elle a été re-dérivée — et elle est tracée.

    Abaissée à quatre, la même ébauche est ramenée à quatre claims et l'écart est **dit** : la
    borne annoncée au prompt fait foi, et rien n'est jeté en silence (AD-16).
    """
    navigation, _fake = _navigation(
        [_tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}},
                       {"name": "ouvrir_noeud", "input": {"node_id": ANNEXE}}),
         _fin_de_lecture(), _ebauche_a_six_claims()],
        navigation_draft_max_claims=4)

    await navigation.lire()
    draft, step = await navigation.rediger()

    assert len(draft.claims) == 4
    ecart = next(c for c in step.checks if c.name == "claims_hors_borne_ecartees")
    assert not ecart.ok and "navigation_draft_max_claims" in ecart.detail
# --- 5 bis. le tour terminal, demandé sans outils (story 5.6, T1e) -----------------------


async def test_le_tour_terminal_part_outils_fermes_apres_un_end_turn_precoce() -> None:
    """`end_turn` avant la borne : l'ébauche est demandée dans la foulée, `tool_choice` fermé.

    Ce que ferme `tool_choice` et **pas** le retrait de `tools` : les outils restent dans le corps,
    donc le préfixe facturable — sommaire compris — ne bouge pas entre le dernier tour de lecture et
    le tour qui cite. Le fournisseur ne réécrit pas son cache pour un changement de `tool_choice` ;
    il le réécrirait pour un changement de `tools`.
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture(), _ebauche()])

    await navigation.lire()
    draft, step = await navigation.rediger()

    assert isinstance(draft, AnswerDraft)
    assert navigation.tours == 2 and navigation.tour_terminal_force == 0
    terminal = fake.requests[-1]
    assert terminal["tool_choice"] == TOOL_CHOICE_AUCUN == {"type": "none"}
    assert terminal["tools"] == fake.requests[0]["tools"]  # le préfixe facturable ne bouge pas
    assert "tool_choice" not in fake.requests[0]
    # Le modèle sait où il en est : le message le dit, il ne se déduit pas du corps de la requête.
    assert "sans appel d'outil" in terminal["messages"][-1]["content"]
    assert not any(check.name == "tour_terminal_force" for check in step.checks)
    assert "0 tour(s) terminal(aux) forcé(s)" in next(
        c.detail for c in step.checks if c.name == "ebauche_dans_la_conversation")


async def test_la_borne_des_tours_atteinte_demande_lebauche_sans_outils() -> None:
    """L'autre entrée du tour terminal : la lecture est **bornée**, pas finie — et rien n'échoue.

    C'est le chemin que le prototype ne connaissait pas : sa boucle ne demandait l'ébauche qu'après
    un `end_turn`, donc jamais alors que le modèle voulait encore lire.
    """
    navigation, fake = _navigation(
        [_tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
         _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": ANNEXE}}),
         _ebauche()],
        navigation_max_llm_turns=2)

    step_lecture = await navigation.lire()
    draft, _step = await navigation.rediger()

    assert any(check.name == "tours_epuises" and not check.ok for check in step_lecture.checks)
    assert isinstance(draft, AnswerDraft) and navigation.retrieval().truncated
    assert fake.requests[-1]["tool_choice"] == TOOL_CHOICE_AUCUN


async def test_un_tour_terminal_qui_redemande_un_outil_le_sert_puis_obtient_lebauche() -> None:
    """Le fait mesuré du 03/09/2026 : `stop_reason=tool_use` au tour qui devait citer.

    Il valait un 503 (« dialogue d'outils non supporté ») sur une lecture qui n'avait rien
    d'anormal. Il vaut désormais une lecture de plus, servie dans la borne des tours, puis l'ébauche
    — et la trace le dit au lieu de le taire.
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture(),
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": ANNEXE}}),
        _ebauche()])

    await navigation.lire()
    draft, step = await navigation.rediger()

    assert isinstance(draft, AnswerDraft)
    assert navigation.tour_terminal_force == 1 and navigation.tours == 3
    # L'outil a été exécuté pour de bon : le bloc de l'annexe est entré dans la lecture.
    assert ITEM in navigation.ouverts and step.opened_block_ids == [TITRE, REGLE, ITEM]
    # Le rappel voyage dans le **même** message que le résultat, en bloc `text` après lui.
    dernier = fake.requests[-1]["messages"][-1]["content"]
    assert any(part.get("type") == "tool_result" and TEXTE_ITEM in part["content"]
               for part in dernier)
    assert dernier[-1]["type"] == "text" and "sans appel d'outil" in dernier[-1]["text"]
    force = next(c for c in step.checks if c.name == "tour_terminal_force")
    assert not force.ok and "tool_choice" in force.detail
    assert "1 tour(s) terminal(aux) forcé(s)" in next(
        c.detail for c in step.checks if c.name == "ebauche_dans_la_conversation")


async def test_un_tour_terminal_tronque_par_sa_sortie_est_redemande_une_fois_a_low() -> None:
    """Story 5.6, T14 — le fait mesuré trois fois le 03/09/2026 : `stop_reason=max_tokens`.

    Le gate Baloise l'a rendu à `high` (12 h 11), à 3 072 (13 h 14) puis à 5 056 et `medium`
    (13 h 43) : sur Sonnet 5 la réflexion adaptative partage `max_tokens` avec le JSON, et sa queue
    dépasse tout plafond que la deadline autorise. Ce qui manque n'est donc pas de la place mais un
    tour qui ne réfléchisse pas — le même fil, dont le préfixe est en cache, et `low`.

    Le client relance déjà une fois de lui-même, au **même** effort : c'est le quatrième appel du
    script, et c'est précisément parce qu'il rejoue la même dépense qu'il ne suffit pas.
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture(), _tronquee(), _tronquee(), _ebauche()],
        navigation_draft_effort="high")

    await navigation.lire()
    draft, step = await navigation.rediger()

    assert isinstance(draft, AnswerDraft) and navigation.tour_terminal_repris == 1
    terminal, _relance_du_client, reprise = fake.requests[2:]
    assert terminal["output_config"]["effort"] == "high"
    assert reprise["output_config"]["effort"] == "low"
    # Le plafond ne bouge pas — la deadline n'a plus de place à donner (T13) — et le préfixe reste
    # byte-identique : la reprise ne repaie que ce qu'elle ajoute.
    assert reprise["max_tokens"] == terminal["max_tokens"]
    assert reprise["system"] == terminal["system"]
    # Deux messages de plus dans le **même** fil : l'alternance, puis la consigne du code. La sortie
    # coupée n'est jamais réinjectée.
    assert reprise["messages"][:-2] == terminal["messages"]
    assert reprise["messages"][-2] == {"role": "assistant", "content": "(réponse tronquée omise)"}
    assert "sans réflexion étendue" in reprise["messages"][-1]["content"]
    assert "prend en" not in str(reprise["messages"][-2:])
    repris = next(c for c in step.checks if c.name == "tour_terminal_repris")
    assert not repris.ok and "1 tour(s)" in repris.detail and "low" in repris.detail


async def test_un_tour_terminal_tronque_sans_temps_rend_lerreur_de_troncature() -> None:
    """Story 5.6, T14 — la borne C2 : la reprise n'est tentée que si le temps la couvre.

    C'est elle qui rend la reprise compatible avec `deadline_s` **sans l'amender** : comptée au
    plafond, elle sortirait la chaîne de sa deadline (témoin de `test_config.py`). Et l'erreur rendue
    reste celle de la troncature — pas le `Timeout` que le client lèverait sur l'appel suivant, qui
    dirait « plus de temps » d'une chaîne dont le vrai défaut est une sortie trop longue.
    """
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture(), _tronquee(), _tronquee()])
    navigation.request_budget = _BudgetSansResteApres(
        fake, apres=4, reste=30.0, deadline_s=100.0, max_attempts=8, max_cost_eur=0.75)

    await navigation.lire()
    with pytest.raises(LlmParse) as leve:
        await navigation.rediger()

    assert "réponse tronquée" in leve.value.message and leve.value.stop_reason == "max_tokens"
    assert navigation.tour_terminal_repris == 0 and len(fake.requests) == 4
    repris = next(c for c in leve.value.step.checks if c.name == "tour_terminal_repris")
    assert not repris.ok and "non repris" in repris.detail and "30.0 s restantes" in repris.detail


async def test_un_tour_terminal_nest_jamais_repris_deux_fois() -> None:
    """Story 5.6, T14 — une reprise, pas une boucle : la seconde troncature est terminale."""
    navigation, fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture(), _tronquee(), _tronquee(), _tronquee(), _tronquee()])

    await navigation.lire()
    with pytest.raises(LlmParse):
        await navigation.rediger()

    # Le tour terminal, sa relance interne, la reprise à `low`, la relance interne de la reprise :
    # quatre appels et pas un de plus.
    assert navigation.tour_terminal_repris == 1 and len(fake.requests) == 6


# --- 6. l'unité d'énumération, gardée par la structure du rendu --------------------------


def test_ouvrir_un_noeud_rend_lenumeration_entiere_amorce_et_items() -> None:
    """Ce que T2 garde de l'unité d'énumération : `ouvrir_noeud` la rend, sans borne ni rang.

    Les items d'une même énumération se qualifient les uns les autres — « même lorsqu'il n'y a pas
    eu embrasement, ni commencement d'incendie » du sixième péril dit quelque chose des cinq autres
    —, et les lire séparément fait mentir chacun par omission. La passe de *retrouver* qui honorait
    cette unité par une réservation est partie avec la variante qui la portait ; la propriété, elle,
    reste, et elle est **structurelle** : `blocs_du_noeud` rend le nœud demandé **et** ses enfants
    feuilles à un seul bloc. `Index.enumeration_de` sert ici d'oracle indépendant, mesuré sur le
    contrat servi — aucun `kind` privilégié, aucun cas particulier.
    """
    from pathlib import Path

    from server.app.corpus.loader import load_corpus

    corpus = load_corpus(Path(__file__).resolve().parents[1] / "data", allow_ungated=True)
    index = Index(corpus)
    doc_id = "axa-lu-optihome-2017"
    amorce = f"{doc_id}:p34:6"  # « La Compagnie assure les biens désignés, contre les périls… »
    enumeration = index.enumeration_de(amorce)
    assert enumeration is not None and len(enumeration) > 1

    noeud = index.parent_node(amorce)
    rendus = [b.block_id for b in blocs_du_noeud(corpus, doc_id, noeud)]
    assert set(enumeration) <= set(rendus), (enumeration, rendus)


# --- 6. la portée dérivée du profil (AC 2.3) ---------------------------------------------


async def test_les_fiches_du_profil_sont_suggerees_au_modele_jamais_ouvertes_par_le_code() -> None:
    """AC 2.3 sur le chemin servi : le profil **désigne**, le modèle décide, la trace le publie.

    L'AC dit « *retrouver* priorise ces nœuds ». La passe qui l'honorait réservait des places dans
    un budget d'ouvertures — c'est-à-dire qu'elle choisissait pour le modèle, ce que l'amendement
    AD-1 interdit désormais. Ce qui reste honorable de l'AC est la désignation : elle est servie
    **dans la demande**, nommément, et rien d'autre ne se produit.

    Ce que ce témoin tient, et qui n'est pas une reformulation de l'implémentation :
    aucune ouverture avant le premier tour du modèle ; la suggestion nomme la fiche **et** son
    titre ; le nœud ouvert est celui que le modèle a demandé — ici l'annexe, que le profil ne
    suggérait pas — et la fiche suggérée qu'il a ignorée reste fermée.
    """
    navigation, _fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": ANNEXE}}),
        _fin_de_lecture()],
        scope=QuestionScope(themes=["prise en charge"], noeuds=[SOCLE, "autre-doc:fiche"]))

    # La désignation est une donnée du code, pas un jugement du modèle : elle est là avant le
    # premier appel, et le nœud d'un autre document n'y entre pas.
    assert navigation.fiches_suggerees() == [(SOCLE, "Socle")]
    demande = navigation._demande()
    assert '"fiches_suggerees_par_le_profil"' in demande
    assert f'"node_id": "{SOCLE}"' in demande and '"titre": "Socle"' in demande
    # Et **rien** n'est ouvert de ce fait : le code n'a lu aucun bloc avant que le modèle le demande.
    assert navigation.ouverts == {} and navigation.noeuds_ouverts == []

    step = await navigation.lire()

    # Le modèle a ouvert l'annexe, que le profil ne suggérait pas, et laissé la fiche suggérée :
    # c'est son droit entier, et le contrôle le **rapporte** sans le tenir pour une faute.
    assert navigation.noeuds_ouverts == [ANNEXE]
    (check,) = [c for c in step.checks if c.name == "noeuds_du_profil"]
    assert check.ok is True
    assert check.detail == (f"1 fiche(s) suggérée(s) par le profil ({SOCLE}), 0 ouverte(s) par le "
                            "modèle (aucune) : une indication, jamais une ouverture")
    # AD-10 : des `node_id` de l'ingestion, jamais une clé de profil ni un contenu de bloc.
    assert "enfants" not in check.detail and TEXTE_REGLE[:20] not in check.detail


async def test_une_fiche_suggeree_que_le_modele_ouvre_est_publiee_comme_ouverte() -> None:
    """Le versant positif : la trace distingue la fiche suivie de la fiche ignorée."""
    navigation, _fake = _navigation([
        _tour_doutils({"name": "ouvrir_noeud", "input": {"node_id": SOCLE}}),
        _fin_de_lecture()],
        scope=QuestionScope(noeuds=[SOCLE, ANNEXE]))

    step = await navigation.lire()

    (check,) = [c for c in step.checks if c.name == "noeuds_du_profil"]
    assert check.detail == (f"2 fiche(s) suggérée(s) par le profil ({SOCLE}, {ANNEXE}), "
                            f"1 ouverte(s) par le modèle ({SOCLE}) : une indication, jamais une "
                            "ouverture")


async def test_sans_profil_la_demande_et_la_trace_sont_inchangees() -> None:
    """Le corps de requête du sinistre ne bouge pas : il n'a ni profil ni parcours.

    C'est la borne qui rend le changement sûr — la clé `fiches_suggerees_par_le_profil` n'existe
    que si le profil a désigné quelque chose, et le contrôle n'est publié que dans ce cas. Sans
    quoi les fixtures enregistrées du sinistre cesseraient de se rejouer, sans qu'aucune règle ait
    bougé.
    """
    sans, _fake = _navigation([_fin_de_lecture()])
    avec_scope_vide, _fake2 = _navigation([_fin_de_lecture()], scope=QuestionScope())

    assert sans.fiches_suggerees() == [] and avec_scope_vide.fiches_suggerees() == []
    assert "fiches_suggerees" not in sans._demande()
    assert sans._demande() == avec_scope_vide._demande()

    step = await sans.lire()
    assert [c.name for c in step.checks if c.name == "noeuds_du_profil"] == []
