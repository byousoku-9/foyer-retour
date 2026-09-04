"""Le cas témoin de la bougie, joué en vrai sur le contrat AXA (story 1.8), enregistré puis rejoué.

Avec `ANTHROPIC_API_KEY` : la chaîne des cinq étapes tourne pour de bon sur le corpus
`axa-lu-optihome-2017` réel, réponses brutes sérialisées dans `tests/llm_fixtures/`. Sans (variable
vide) : mêmes assertions, réponses rejouées — zéro réseau.

Ce que le cas doit établir (AC de la story, et « verdict cadré » de l'epic) :

1. le verdict est **conservateur** — l'AC borne le cas bougie à `{sous_conditions, ne_tranche_pas}`,
   donc jamais `couvert`. Ce qui le tient ouvert n'est pas une politique globale (revue Codex 1.8,
   B1, tour 2 : la seconde branche de la règle (2) d'AD-6 se lit sur la **clause**, pas sur le
   dossier) mais le contrôle des qualités exigées (B3) : la garantie de l'article 3.1.1.1.6 exige un
   événement *soudain* et l'action *subite* de la chaleur, et les faits déclarés ne les établissent
   pas. Le test le rejoue hors modèle, dans les deux sens ;
1bis. les questions posées au client nomment **ce que la clause exige** — l'AC parle de la nature
   « subite ». Le run du 24/08 avait montré que le prompt seul ne le garantit pas : le modèle avait
   déclaré la qualité établie et aucune question ne la mentionnait. Le code compose désormais une
   question par qualité exigée, établie ou non (revue Codex 1.8, B3) ;
2. il est adossé aux **clauses exactes** : au moins un des trois blocs relus à la main en story 1.2
   (p9 « contenu », p11 « mobilier de jardin », p34 la garantie de l'action subite de la chaleur) ;
3. l'exclusion de la page 46 est **explicitement écartée** — absente, ou affichée `applicable="non"`
   parce qu'elle vise le bâtiment des extensions 3.1.8.3-6 et non le contenu du domicile ;
4. la requête entière tient sous le plafond de coût, et *vérifier* n'a fait qu'**un** appel groupé,
   à l'étage que la configuration lui affecte (AD-9, lu sur `Settings` et jamais recopié ici).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, NamedTuple

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
from server.app.domain.errors import BudgetExceeded
from server.app.domain.question import Faits
from server.app.domain.trace import StepTrace
from server.app.domain.verdict import (
    KINDS_DECISIONNELS,
    ChampsApplicabilite,
    ClaimJugee,
    ClauseCitee,
    decider,
)
from server.app.llm.audit import MemoryAuditSink
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines import sinistre
from server.app.steps.verifier import _condition_de_section, _mots_qualifiants
from tests.fixtures import LLMRecorder
from tests.helpers_tiers import modele_attendu, verifier_etage
from tests.llm_fake import FakeAnthropic, RecordedAnthropic, fake_message

ROOT = Path(__file__).resolve().parents[1]
DOC_ID = "axa-lu-optihome-2017"
QUESTION = "Ce sinistre est-il couvert par les conditions générales du contrat ?"
FAITS = Faits(
    date="2026-08-01", lieu="salon du domicile assuré", montant_eur=1200.0,
    description="Une bougie allumée posée sur une table basse est tombée sur le canapé. "
                "Le mobilier de salon a brûlé sur une partie, sans embrasement ni commencement "
                "d'incendie : il n'y a eu ni flammes propagées, ni dégât au bâtiment.")
# Les trois blocs relus à la main en story 1.2 : la définition du contenu, celle du mobilier de
# jardin, et la garantie « action subite de la chaleur » de l'article 3.1.1.1.6.
BLOCS_ATTENDUS = {f"{DOC_ID}:p9:2", f"{DOC_ID}:p11:12", f"{DOC_ID}:p34:12"}
EXCLUSION_P46 = f"{DOC_ID}:p46:1"


@pytest.fixture(scope="module")
def index() -> Index:
    return Index(load_corpus(ROOT / "data", allow_ungated=True))


def _au_mieux_disant(answer, index: Index, *, exigees: list[str] | None = None,
                     non_etablies: list[str] | None = None) -> tuple[list[ClaimJugee], list[str]]:
    """Les claims affichées, re-jugées avec un jeu de champs typés choisi par le test.

    `fait_requis_present=true`, aucune option, aucune condition particulière, aucun fait manquant :
    le jeu le plus favorable qui soit. `exigees` / `non_etablies` y ajoutent le contrôle des qualités
    (revue Codex 1.8, B3). Rejouer la table dessus montre ce qui tient réellement le verdict ouvert,
    ce que la fixture d'un run unique ne peut pas prouver.

    **Ce que « au mieux-disant » ne peut pas atteindre, et pourquoi il faut le dire (story 5.6,
    T1c).** Ce helper choisit des `ChampsApplicabilite` ; il ne choisit pas le corpus. Or la règle (2)
    d'`applicable_de_claim` rend `humain` toute claim qui cite un bloc décisionnel **sans `kind`
    confirmé**, *avant* de regarder le moindre champ : aucun jeu de champs ne peut la rendre
    favorable. Une telle claim laissée dans le rejeu ne mesure plus les qualités exigées, elle mesure
    le typage du corpus — et elle ferme la règle (3) pour une raison que le témoin ne teste pas.
    Elle est donc **mise de côté**, et rendue à l'appelant : le témoin re-dérive la raison de chaque
    mise à l'écart, de sorte que rien ne puisse être escamoté en silence.

    **La trace qui l'a rendu nécessaire.** Depuis que la place de l'ébauche de navigation vaut
    `navigation_draft_max_claims` (6, T1b), la réponse bougie affiche **deux** claims au lieu d'une :
    `c1` sur `p34:12` (la garantie de l'article 3.1.1.1.6, `kind_confirmed=True`, socle) et `c2` sur
    `p34:7` (« 3.1.1.1.1 L'incendie … », `kind` = `garantie`, `kind_confirmed=False`). Le rejeu
    donnait `{c1: oui, c2: humain}` : la règle (3) refusait de trancher sur `c2` et le verdict tombait
    en (4), `ne_tranche_pas`. Ce n'est **pas** F3 (`facettes_sans_reponse`) — ce paramètre vaut 0 dans
    le rejeu, la branche n'est jamais atteinte, et le pipeline lui-même rend « Aucune règle de la
    table ne tranche sur les clauses retrouvées », pas la phrase des sous-questions. Le produit a
    raison : une seconde garantie au typage non confirmé est ouverte pour de bon. C'est le témoin qui
    avait cessé d'isoler sa propriété.
    """
    documents = index.corpus.documents
    jugees: list[ClaimJugee] = []
    ecartees: list[str] = []
    for claim in answer.claims:
        blocs = [documents[index.doc_of(q.block_id)].block(q.block_id) for q in claim.quotes]
        decisionnels = [bloc for bloc in blocs if bloc.kind in KINDS_DECISIONNELS]
        if any(not bloc.kind_confirmed for bloc in decisionnels):
            ecartees.append(claim.claim_id)
            continue
        # Story 5.7 (L1q). La seconde chose que ce helper ne peut pas choisir : **le contenu des
        # conditions particulières.** Une claim qui cite le bloc de la condition d'applicabilité de
        # sa propre section — ici `p34:4`, « Les présentes conditions spéciales sont applicables si
        # les conditions particulières mentionnent que la garantie “incendie et périls assimilés”
        # est accordée » — reçoit du rejeu `fait_requis_present=true`, ce qui revient à décréter que
        # les CP la mentionnent. Le pipeline, lui, la rend `humain` (T18 : le texte renvoie aux CP,
        # et rien ne les lit à J+1). Laissée dans le rejeu, elle **établit** la condition de section
        # et rouvre la règle (3) : le témoin mesurerait alors une propriété que le produit n'atteint
        # jamais. Elle est mise de côté comme le typage non confirmé, et rendue à l'appelant.
        conditions_de_section = {
            documents[index.doc_of(bloc.block_id)].condition_de_section_applicable(
                documents[index.doc_of(bloc.block_id)].node_of(bloc.block_id))
            for bloc in decisionnels}
        if any(bloc.block_id in conditions_de_section for bloc in decisionnels):
            ecartees.append(claim.claim_id)
            continue
        clauses = []
        for bloc in decisionnels:
            document = documents[index.doc_of(bloc.block_id)]
            noeud = document.node_of(bloc.block_id)
            clauses.append(ClauseCitee(
                block_id=bloc.block_id, kind=bloc.kind, kind_confirmed=bloc.kind_confirmed,
                portee=document.scope_nodes(bloc.block_id), node_id=noeud,
                socle=document.node_scope_kind(noeud) == "commun",
                # Story 5.7 (L1e, puis L1o et L1q) : le rejeu doit lire la même chose que
                # `steps.verifier._clauses_citees` — sans quoi il prouverait une propriété **plus
                # faible** que celle du pipeline, sur des clauses que le code sert déjà autrement.
                # L'amorce d'énumération (L1o) et l'amorce qui introduit l'item (L1q) en font partie :
                # sans la première, `p34:6` passerait ici pour une garantie qui décide ; sans la
                # seconde, deux périls frères ne seraient pas reconnus pour sœurs.
                amorce=index.est_amorce_denumeration(bloc.block_id),
                amorce_de=index.amorce_qui_introduit(bloc.block_id) or "",
                condition_section=_condition_de_section(document, noeud)))
        # Tour D2 (04/09/2026) : la claim qui cite **la condition de section elle-même**, quand
        # celle-ci renvoie aux conditions particulières, ne peut pas être rendue favorable au
        # premier tour — les CP ne sont pas au dossier. C'est ce que `_conditions_ouvertes` écrit
        # (« une condition qui renvoie aux CP ne peut jamais être établie au premier tour ») et ce
        # que le pipeline produit. Le laisser à `oui` ici rendrait la condition *établie*, lèverait
        # le plafond de L1e et ferait sortir `couvert` d'un rejeu que le produit ne peut pas rendre.
        cp_requise = any(
            clause.condition_section is not None
            and clause.condition_section.block_id == clause.block_id
            and clause.condition_section.renvoie_cp
            for clause in clauses)
        jugees.append(ClaimJugee(claim_id=claim.claim_id, clauses=clauses,
                                 champs=ChampsApplicabilite(
                                     fait_requis_present=True, cp_requise=cp_requise,
                                     qualites_exigees=exigees or [],
                                     qualites_non_etablies=non_etablies or [])))
    return jugees, ecartees


def _settings() -> Settings:
    # Seuils par défaut de `config.py`, jamais ceux du `.env` du poste : ils décident des blocs
    # envoyés à *rédiger*, donc de la clé de requête — un `.env` local qui les surcharge rendrait le
    # rejeu hors ligne impossible. La clé ne sert qu'au vrai client, côté recorder.
    return Settings(_env_file=None, anthropic_api_key="")


def _budget() -> RequestBudget:
    s = _settings()
    return RequestBudget(deadline_s=s.deadline_s, max_attempts=s.max_llm_attempts,
                         max_cost_eur=s.max_cost_eur_per_request)


class Preflight(NamedTuple):
    """Un majorant calculé avant l'envoi, rattaché à l'**étape** qui a demandé l'appel."""

    etape: str
    engage: float
    majorant: float
    model: str
    tokens: int


def _suivre_l_etape(monkeypatch: pytest.MonkeyPatch) -> Callable[[], str]:
    """Rend un lecteur qui dit quelle **étape** émet l'appel en cours, préflight compris.

    Un préflight se mesure *avant* l'envoi : ni la trace, ni le journal d'audit, ni les requêtes du
    faux fournisseur ne peuvent encore le nommer. `LlmClient.parse` et `LlmClient.tool_turn`, eux,
    reçoivent le `StepTrace` de l'étape appelante : c'est le seul endroit où « l'appel de rédaction »
    se désigne sans compter les requêtes ni reconnaître un plafond — deux repères que l'amendement
    AD-1 du 03/09/2026 a rendus faux. La chaîne n'émet jamais deux appels de front : une seule étape
    est donc courante à la fois.
    """
    courante = [""]

    def poser(nom: str) -> None:
        methode = getattr(LlmClient, nom)

        async def relayer(self: LlmClient, *, step: StepTrace, **kwargs):
            precedente, courante[0] = courante[0], step.name
            try:
                return await methode(self, step=step, **kwargs)
            finally:
                courante[0] = precedente

        monkeypatch.setattr(LlmClient, nom, relayer)

    for nom_de_methode in ("parse", "tool_turn"):
        poser(nom_de_methode)
    return lambda: courante[0]


def _demarrages_de_rediger(audit: MemoryAuditSink) -> int:
    """Combien de fois *rédiger* a démarré, d'après les appels réellement envoyés.

    Le nombre est rendu tel quel, jamais comparé à 1 par le helper : la relance d'AD-3 est un chemin
    normal, et c'est à l'appelant de dire s'il attend « au moins un » démarrage ou « aucun ».

    Ce qui identifie l'appel dont l'AC parle est son **étape**. Le journal d'audit exact ne porte
    qu'un événement par appel parti sur le fil, avec le nom de l'étape que le client recopie du
    `StepTrace` de l'appelant (`ExactLlmAuditEvent.step`) : un appel refusé au préflight n'y entre
    pas, ce qui est exactement la propriété que ce helper doit rendre. Le plafond de sortie, lui, ne
    désigne plus rien depuis l'amendement AD-1 du 03/09/2026 : l'ébauche servie est le tour terminal
    de la navigation, et son plafond (`navigation_rediger_max_tokens`) a valu jusqu'à T13 celui de
    *vérifier* — quand `rediger_max_tokens`, lui, n'est plus envoyé par aucun appel de cette chaîne.
    """
    return sum(1 for event in audit.events if event.step == "rediger")


async def test_preflight_nominal_passe_et_un_depassement_reste_refuse(
        index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le pipeline servi atteint la rédaction sous 0,12 €, puis refuse juste au-dessus."""
    import server.app.llm.client as client_module

    settings = _settings()
    document = index.corpus.documents[DOC_ID]
    garantie = next(
        bloc for bloc in document.blocks
        if bloc.kind == "garantie" and "action subite de la chaleur" in normalize(bloc.text)
    )
    node_id = document.node_of(garantie.block_id)
    quote = garantie.text[:settings.quote_max_chars]

    def script() -> list[dict]:
        comprendre = fake_message(model=modele_attendu("comprendre", settings), text=json.dumps({
            "intent": "question",
            "question_resolue": QUESTION,
            "clarification": None,
            "language": "fr",
            "terms": ["mobilier", "chaleur"],
            "themes": [],
            "facettes": ["couverture du sinistre"],
            "bien": "mobilier de salon",
            "evenement": "action de la chaleur",
            "lieu": "domicile assuré",
            "cause": "bougie",
            "moment": "2026-08-01",
        }), input_tokens=1000, output_tokens=100)
        outils = fake_message(
            model=modele_attendu("retrouver_outils", settings), stop_reason="tool_use",
            input_tokens=9575, output_tokens=1024,
            content=[
                {"type": "tool_use", "id": "toolu_chercher", "name": "chercher",
                 "input": {"termes": ["mobilier", "chaleur"]}},
                {"type": "tool_use", "id": "toolu_ouvrir", "name": "ouvrir_noeud",
                 "input": {"node_id": node_id}},
            ],
        )
        # Amendement AD-1 du 03/09/2026 : un tour qui rend `tool_use` impose un appel de plus, et la
        # lecture se **clôt** sur un tour sans outil — après quoi l'ébauche est demandée dans la
        # même conversation. Le nombre de tours n'est pas recopié ici : la fin de lecture est
        # explicite, si bien que le script reste juste quel que soit `navigation_max_llm_turns`.
        fin_de_lecture = fake_message(
            model=modele_attendu("retrouver_outils", settings), stop_reason="end_turn",
            text="J'ai lu ce qu'il faut.")
        # Story 5.7 (L1q) : **deux** fins de lecture, et c'est le correctif qui l'impose. Le nœud
        # ouvert ci-dessus porte une garantie (`3.1.1.1.6`) et aucune section d'exclusions n'a été
        # lue : le premier tour terminal est refusé une fois, avec les sections que le contrat
        # attache à ce qui vient d'être lu. Le script ne les ouvre pas — ce n'est pas ce que ce
        # témoin mesure — et se contente de reconclure, ce qui prouve au passage la borne du
        # correctif : le refus n'a lieu **qu'une** fois, il ne boucle pas et ne mange pas le budget.
        navigation = [outils, fin_de_lecture, fin_de_lecture]
        rediger = fake_message(model=modele_attendu("rediger", settings), text=json.dumps({
            "segments": [{"text": "La garantie vise l'action subite de la chaleur.",
                          "kind": "factuel", "claim_ids": ["c1"]}],
            "claims": [{"claim_id": "c1",
                        "text": "La garantie vise l'action subite de la chaleur.",
                        "quotes": [{"block_id": garantie.block_id, "quote": quote}]}],
        }))
        verifier = fake_message(model=modele_attendu("verifier", settings), text=json.dumps({
            "verdicts": [{"claim_id": "c1", "pertinente": True, "raison": None}],
            "facettes": [{"facette": 0, "claim_ids": ["c1"]}],
            "segments": [{"segment": 0, "soutenu": True}],
            "applicabilite": [{
                "claim_id": "c1",
                "fait_requis_present": False,
                "option_requise": False,
                "cp_requise": False,
                "fait_manquant": "caractère subit de l'action de la chaleur",
                "qualites_exigees": [],
                "qualites_etablies": [],
            }],
        }))
        return [comprendre, *navigation, rediger, verifier]

    original = client_module.estimate_cost
    budget = _budget()
    etape = _suivre_l_etape(monkeypatch)
    preflights: list[Preflight] = []

    def relever(*args, **kwargs):
        estimate = original(*args, **kwargs)
        max_tokens = int(args[3] if len(args) > 3 else kwargs["max_tokens"])
        preflights.append(Preflight(etape(), budget.cost_eur, estimate, str(args[0]), max_tokens))
        return estimate

    monkeypatch.setattr(client_module, "estimate_cost", relever)
    fournisseur = FakeAnthropic(script())
    audit = MemoryAuditSink()
    client = LlmClient(settings, anthropic_client=fournisseur, audit_sink=audit)
    answer, trace = await sinistre.run(
        DOC_ID, QUESTION, FAITS, corpus=index.corpus, index=index, client=client,
        settings=settings, request_id="preflight-navigation-reel", budget=budget)

    # La propriété visée n'est ni un rang dans la liste des requêtes, ni un identifiant de modèle,
    # ni une valeur de plafond (deux étapes peuvent partager le même tier, un rang bouge dès qu'un
    # tour de navigation est ajouté, et `navigation_rediger_max_tokens` a valu `verifier_max_tokens`
    # jusqu'à T13) : c'est le **démarrage de *rédiger***, que seule l'étape désigne.
    redactions = [p for p in preflights if p.etape == "rediger"]
    # **Au moins un** démarrage, et c'est le premier que l'AC décrit : la relance d'AD-3 est un
    # chemin normal, pas un incident, et exiger `== 1` ferait rougir ce test sur une chaîne
    # parfaitement conforme qui relance *rédiger* (c'est la correction déjà faite plus bas sur les
    # étapes de la trace, restée à faire ici).
    assert redactions, "*rédiger* n'a jamais démarré sur le chemin nominal"
    _etape, engage, majorant, model, _tokens = redactions[0]
    # L'étage servi est celui de l'étape qui **rend** l'ébauche sur le chemin servi : depuis
    # l'amendement AD-1 du 03/09/2026, c'est le tour terminal de la conversation de navigation, donc
    # `navigation_tier` et non `rediger_tier`. Lu sur `Settings`, jamais recopié (AD-16).
    assert model == TIERS[settings.navigation_tier]
    # Ce que le coût engagé doit valoir n'est **pas** un littéral en euros : une constante de coût
    # est le même défaut qu'un littéral de tier, déplacé du modèle vers l'euro — elle n'est vraie que
    # pour l'affectation d'étages du jour. La propriété est que ce coût est celui des étapes qui ont
    # réellement tourné avant *rédiger*, quel que soit le tier qui les sert.
    avant_rediger = [pas for pas in trace.steps
                     if pas.name in ("comprendre", "retrouver") and pas.calls]
    assert [pas.name for pas in avant_rediger] == ["comprendre", "retrouver"]
    assert engage == pytest.approx(sum(pas.usage.cost_eur for pas in avant_rediger), abs=1e-4)
    assert engage + majorant <= settings.max_cost_eur_per_request
    assert _demarrages_de_rediger(audit) >= 1
    assert answer.verdict is not None

    fournisseur_bloque = FakeAnthropic(script())
    audit_bloque = MemoryAuditSink()
    client_bloque = LlmClient(settings, anthropic_client=fournisseur_bloque,
                              audit_sink=audit_bloque)
    plafond_trop_bas = round(engage + majorant - 0.0001, 4)
    budget = RequestBudget(
        deadline_s=settings.deadline_s,
        max_attempts=settings.max_llm_attempts,
        max_cost_eur=plafond_trop_bas,
    )
    with pytest.raises(BudgetExceeded, match="coût"):
        await sinistre.run(
            DOC_ID, QUESTION, FAITS, corpus=index.corpus, index=index,
            client=client_bloque, settings=settings,
            request_id="preflight-navigation-bloque", budget=budget)
    # La chaîne est allée au bout de la lecture puis s'est arrêtée **avant** de payer l'ébauche :
    # c'est le refus que l'AC demande, et c'est le démarrage de la rédaction qui le dit — pas le
    # tier, que la navigation partage avec elle. `navigation_max_llm_turns` borne les tours ; le
    # modèle peut clore sa lecture avant, donc le compte est un **majorant**, pas une égalité.
    assert 2 <= len(fournisseur_bloque.requests) <= 1 + settings.navigation_max_llm_turns
    # Le zéro ci-dessous doit être un zéro **mesuré**, pas un relevé muet : le journal d'audit porte
    # exactement un événement par requête partie chez le fournisseur.
    assert len(audit_bloque.events) == len(fournisseur_bloque.requests)
    assert _demarrages_de_rediger(audit_bloque) == 0


async def test_the_candle_case_gets_a_conservative_verdict_on_the_exact_clauses(
        index: Index, llm_recorder: LLMRecorder) -> None:
    settings, budget = _settings(), _budget()
    answer, trace = await sinistre.run(
        DOC_ID, QUESTION, FAITS, corpus=index.corpus, index=index,
        client=LlmClient(settings, anthropic_client=RecordedAnthropic(llm_recorder)),
        settings=settings, request_id="live-sinistre-1", budget=budget)

    # (1) verdict conservateur, jamais `couvert` sur ce cas.
    verdict = answer.verdict
    assert verdict is not None, "AD-16 : un sinistre sort toujours avec un verdict, refus compris"
    # L'AC, mot à mot : « le cas bougie … `value ∈ {sous_conditions, ne_tranche_pas}` ». Le tour 1
    # avait fixé la valeur en fermant la règle (3) à tout le monde ; la revue Codex 1.8 (B1) a montré
    # que c'était l'AC de la règle (2) qui était réécrit. La borne est donc celle de l'AC, et ce qui
    # la tient est le contrôle des qualités — vérifié en (5).
    assert verdict.value in ("sous_conditions", "ne_tranche_pas"), verdict.value
    assert "conditions générales seules" in verdict.reason
    # AD-6 : le paquet manquant accompagne toujours le verdict, et les questions à poser aussi.
    # « `ask_client` n'est pas vide » ne prouvait rien : le run du 24/08 qui a motivé la revue Codex
    # 1.8 (B3) était vert alors qu'aucune question ne parlait de la nature subite. L'AC est donc
    # épinglé mot à mot — options, conditions particulières, **et** la qualité que la clause exige.
    assert verdict.missing.conditions_particulieres and verdict.missing.options_souscrites
    questions = " ".join(verdict.ask_client).lower()
    assert "options" in questions and "conditions particulières" in questions
    assert "subit" in questions or "soudain" in questions, verdict.ask_client

    # (2) les clauses exactes : au moins un des trois blocs relus à la main en 1.2
    cites = {q.block_id for c in answer.claims for q in c.quotes}
    assert cites & BLOCS_ATTENDUS, f"aucun des blocs témoins parmi {sorted(cites)}"

    # (3) l'exclusion de la page 46 est écartée, jamais opposée en silence. L'AC dit « absente ou
    # `applicable="non"` », et `non` est bien atteignable : `p46:1` est `model_verified` après les
    # deux lectures 3.2 et porte `scope_node_ids` = 3.1.8.3–3.1.8.6 — les deux
    # marches qui mèneraient sinon à `humain`. Accepter `humain` ici laisserait passer une régression
    # du typage ou de la portée sans que rien ne le dise.
    affichee = next((c for c in answer.claims
                     if any(q.block_id == EXCLUSION_P46 for q in c.quotes)), None)
    assert affichee is None or affichee.status.applicable == "non"

    # chaque claim affichée porte un statut d'applicabilité typé, dérivé par le code (AD-4)
    documents = index.corpus.documents
    for claim in answer.claims:
        assert claim.status.retrouvee is True and claim.status.pertinente is True
        assert claim.status.edition  # « édition juin 2017 — actualité non vérifiée »
        # Story 5.7 (L1o) : « décisionnelle » se lit désormais sur la structure autant que sur le
        # `kind`. Une **amorce d'énumération** — ici `p34:6`, « La Compagnie assure les biens
        # désignés, contre les périls suivants : », que ce run cite seule — est typée `garantie` par
        # l'ingestion et n'énonce pourtant aucun péril : la claim qui n'a cité qu'elle est un
        # contexte, `applicable=None`, hors de la table. C'est ce que l'attendu dit maintenant.
        decisionnelle = any(documents[index.doc_of(q.block_id)].block(q.block_id).kind
                            in ("garantie", "exclusion", "condition", "franchise")
                            and not index.est_amorce_denumeration(q.block_id)
                            for q in claim.quotes)
        assert (claim.status.applicable in ("oui", "non", "humain")) is decisionnelle
        for q in claim.quotes:
            bloc = documents[index.doc_of(q.block_id)].block(q.block_id)
            assert bloc.kind != "heading"
            # AD-3 : la citation affichée est le passage **relu dans le corpus**, aux offsets prouvés
            assert q.quote == bloc.text[q.text_start:q.text_end]
            assert bloc.text_norm[q.start:q.end] == normalize(q.quote)

    # (4) la chaîne, ses tiers, son unique appel groupé et son coût.
    # La forme admise tolère la relance d'AD-3 : elle est un chemin **normal**, pas un incident, et
    # un run réel l'emprunte dès qu'une citation est rejetée. Une assertion sur les cinq premières
    # étapes rendait donc ce test rouge sur un run parfaitement conforme (mesuré : le run du
    # 24/08 qui a relancé *rédiger*). On exige la chaîne d'AD-1 et, au plus, **une** paire de
    # relance insérée entre la première vérification et *restituer*.
    etapes = [s.name for s in trace.steps]
    assert etapes[:4] == ["comprendre", "retrouver", "rediger", "verifier"]
    assert etapes[-1] == "restituer"
    assert etapes[4:-1] in ([], ["rediger"], ["rediger", "verifier"]), etapes
    # Story 4.2d : `run` est appelé **sans** `variant`, et le défaut du pipeline est désormais la
    # navigation par outils (AD-1, amendement du 25/08/2026) — c'est ce que sert `POST /api/v1/sinistre`,
    # donc ce que ce témoin live doit mesurer. *retrouver* porte maintenant son appel de navigation et
    # publie son tier réel, lu sur la configuration (AD-9) et jamais recopié ici.
    assert trace.pipeline == "sinistre" and trace.variant == sinistre.VARIANT == "navigation"
    retrouver = trace.steps[1]
    assert retrouver.name == "retrouver"
    verifier_etage(retrouver, settings, etape="retrouver_outils")
    assert len(retrouver.calls) >= 1, "la navigation par outils appelle le modèle"
    # Amendement AD-1 du 03/09/2026 : la lecture est bornée par le plafond de sûreté des tours
    # de navigation, et le modèle clôt le plus souvent bien avant.
    assert len(retrouver.calls) <= settings.navigation_max_llm_turns
    for step in (s for s in trace.steps if s.name == "verifier"):
        # AD-9 amendé : **un** appel groupé, jamais un second — et à l'étage configuré, quel qu'il
        # soit (le tier a été promu depuis l'écriture de ce test ; l'invariant, lui, n'a pas bougé).
        verifier_etage(step, settings, appels=1)
    assert budget.cost_eur < settings.max_cost_eur_per_request
    assert trace.total_cost_eur == pytest.approx(budget.cost_eur, abs=1e-4)

    # (5) la propriété que le run seul ne prouve pas, rejouée hors modèle sur les clauses affichées :
    # ce qui tient le cas bougie ouvert est le contrôle des qualités exigées (B3), pas une politique
    # qui fermerait la règle (3) à tout le monde (B1, tour 2). Deux rejeux, avec le jeu de champs le
    # plus favorable qui soit (`fait_requis_present=true`, aucune option, aucune CP) — celui qui a
    # produit un `couvert` sur un run d'avant-correctif.
    subite = "action subite de la chaleur"
    rejeu, ecartees = _au_mieux_disant(answer, index, exigees=[subite], non_etablies=[subite])
    ouvert = decider(rejeu, ask_client_max=settings.ask_client_max)
    assert ouvert.value != "couvert", ouvert.value  # la qualité non établie referme la règle (3)
    assert any(subite in q for q in ouvert.ask_client)
    # Story 5.7 (L1e) — l'attendu de ce second rejeu a changé, et le changement est le correctif.
    # Sans qualité exigée, la garantie du socle sortait `couvert` : c'était faux sur **ce** contrat.
    # L'article 3.1.1 s'ouvre par `p34:4` — « Les présentes conditions spéciales sont applicables si
    # les conditions particulières mentionnent que la garantie “incendie et périls assimilés” est
    # accordée » —, et rien, à J+1, ne lit les conditions particulières. Le verdict est donc plafonné
    # à `sous_conditions`, la condition est citée dans la raison, et la question est posée.
    # Que la règle (3) reste vivante là où la section n'est pas conditionnée se prouve sur du code pur
    # (`test_verdict.py`) et sur le pipeline (`test_pipeline_sinistre.py`), pas ici : sur AXA, aucune
    # garantie de conditions spéciales n'est acquise sans les CP, et c'est ce que le contrat dit.
    sans_qualite_jugees, _ = _au_mieux_disant(answer, index)
    sans_qualite = decider(sans_qualite_jugees, ask_client_max=settings.ask_client_max)
    assert sans_qualite.value == "sous_conditions", sans_qualite.value
    assert f"{DOC_ID}:p34:4" in sans_qualite.reason
    assert any("conditions particulières mentionnent-elles" in q.casefold()
               for q in sans_qualite.ask_client), sans_qualite.ask_client
    # T1c : le rejeu doit encore porter **la** garantie du cas bougie — sans quoi il prouverait la
    # règle (3) sur autre chose que la clause dont l'AC parle.
    assert any(clause.block_id == f"{DOC_ID}:p34:12"
               for jugee in sans_qualite_jugees for clause in jugee.clauses), \
        "la garantie de l'article 3.1.1.1.6 a disparu du rejeu : le témoin ne prouve plus rien"
    # Et ce que le mieux-disant a mis de côté l'a été pour l'une des **deux** raisons qu'aucun jeu de
    # champs ne peut lever : la règle (2) d'`applicable_de_claim` (`kind` décisionnel non confirmé),
    # ou la condition d'applicabilité de la section elle-même (L1q). Les deux raisons sont
    # re-dérivées ici, à partir du corpus, et non lues sur le helper qui les a appliquées.
    for claim_id in ecartees:
        claim = next(c for c in answer.claims if c.claim_id == claim_id)
        blocs = [documents[index.doc_of(q.block_id)].block(q.block_id) for q in claim.quotes]
        typage = any(bloc.kind in KINDS_DECISIONNELS and not bloc.kind_confirmed for bloc in blocs)
        # Story 5.7 (L1q). La claim cite le bloc de la condition d'applicabilité de sa propre
        # section : lui donner `fait_requis_present=true` reviendrait à décréter ce que disent les
        # conditions particulières, que le pipeline ne lit pas à J+1 (T18, L1e). Le produit la rend
        # `humain` ; aucun jeu de champs n'a le droit de faire mieux.
        condition = any(
            bloc.block_id == documents[index.doc_of(bloc.block_id)]
            .condition_de_section_applicable(
                documents[index.doc_of(bloc.block_id)].node_of(bloc.block_id))
            for bloc in blocs if bloc.kind in KINDS_DECISIONNELS)
        assert typage or condition, (
            f"claim {claim_id} écartée du rejeu sans que son typage ni la condition de sa section "
            "soient en cause : le mieux-disant escamote une claim que la table aurait dû juger")

    # (5bis) Revue Codex 1.8 (B3, tour 3) : l'énumération des qualités ne repose plus sur la parole du
    # modèle. Le code relit le **texte** de la clause citée, et sur la garantie réelle de l'article
    # 3.1.1.1.6 il y trouve les quatre qualificatifs que le contrat écrit — c'est la source
    # indépendante qui manquait au tour 2, mesurée ici sur le corpus réel et non sur une fixture.
    garantie = index.corpus.documents[DOC_ID].block(f"{DOC_ID}:p34:12")
    assert set(_mots_qualifiants(garantie.text)) == {"soudain", "subit", "direct", "immediat"}
    # Et la conséquence, quelle qu'ait été l'humeur du modèle : tant que la clause n'est pas tenue
    # pour applicable, chacune des qualités qu'elle écrit est **demandée** au client — nommée par le
    # modèle, ou ajoutée par le code faute de l'avoir été.
    #
    # Deux resserrages du tour 4, tous deux sur la garde et aucun sur l'assertion. `!= "oui"`
    # embarquait `applicable="non"` : une clause tenue pour inapplicable ne demande rien, et le
    # témoin serait tombé sur un run où le code fait exactement ce qu'on lui demande. Et quand un
    # contrôle d'entrée abandonne le jeu de champs (`champs is None`), `_libelles_manquants` ignore
    # la claim entière : `missing.faits` est vide par construction, pour une raison prescrite et non
    # pour une omission. Hors de ces deux cas, l'assertion reste l'AC recopiée — l'affaiblir
    # rendrait l'AC observable au lieu de vérifiable.
    ABANDONS = {"applicabilite_incomplete", "applicabilite_hors_borne", "qualites_non_enumerees"}
    champs_exploites = not [c for etape in trace.steps for c in etape.checks
                            if c.name in ABANDONS]
    citante = next((c for c in answer.claims
                    if any(q.block_id == garantie.block_id for q in c.quotes)), None)
    if citante is not None and citante.status.applicable == "humain" and champs_exploites:
        demandes = _mots_qualifiants(" ".join(verdict.missing.faits))
        assert all(racine in demandes for racine in _mots_qualifiants(garantie.text)), (
            verdict.missing.faits)

    # AD-6 : un verdict autre que `ne_tranche_pas` repose sur une clause fondatrice **affichée**
    if verdict.value != "ne_tranche_pas":
        fondatrices = [c for c in answer.claims
                       if any(documents[index.doc_of(q.block_id)].block(q.block_id).kind
                              in ("garantie", "exclusion") for q in c.quotes)]
        assert fondatrices
