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

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
from server.app.domain.errors import BudgetExceeded
from server.app.domain.question import Faits
from server.app.domain.verdict import (
    KINDS_DECISIONNELS,
    ChampsApplicabilite,
    ClaimJugee,
    ClauseCitee,
    decider,
)
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.pipelines import sinistre
from server.app.steps.verifier import _mots_qualifiants
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
                     non_etablies: list[str] | None = None) -> list[ClaimJugee]:
    """Les claims affichées, re-jugées avec un jeu de champs typés choisi par le test.

    `fait_requis_present=true`, aucune option, aucune condition particulière, aucun fait manquant :
    le jeu le plus favorable qui soit. `exigees` / `non_etablies` y ajoutent le contrôle des qualités
    (revue Codex 1.8, B3). Rejouer la table dessus montre ce qui tient réellement le verdict ouvert,
    ce que la fixture d'un run unique ne peut pas prouver.
    """
    jugees: list[ClaimJugee] = []
    for claim in answer.claims:
        clauses = []
        for q in claim.quotes:
            document = index.corpus.documents[index.doc_of(q.block_id)]
            bloc = document.block(q.block_id)
            if bloc.kind not in KINDS_DECISIONNELS:
                continue
            noeud = document.node_of(bloc.block_id)
            clauses.append(ClauseCitee(
                block_id=bloc.block_id, kind=bloc.kind, kind_confirmed=bloc.kind_confirmed,
                portee=document.scope_nodes(bloc.block_id), node_id=noeud,
                socle=document.node_scope_kind(noeud) == "commun"))
        jugees.append(ClaimJugee(claim_id=claim.claim_id, clauses=clauses,
                                 champs=ChampsApplicabilite(
                                     fait_requis_present=True, qualites_exigees=exigees or [],
                                     qualites_non_etablies=non_etablies or [])))
    return jugees


def _settings() -> Settings:
    # Seuils par défaut de `config.py`, jamais ceux du `.env` du poste : ils décident des blocs
    # envoyés à *rédiger*, donc de la clé de requête — un `.env` local qui les surcharge rendrait le
    # rejeu hors ligne impossible. La clé ne sert qu'au vrai client, côté recorder.
    return Settings(_env_file=None, anthropic_api_key="")


def _budget() -> RequestBudget:
    s = _settings()
    return RequestBudget(deadline_s=s.deadline_s, max_attempts=s.max_llm_attempts,
                         max_cost_eur=s.max_cost_eur_per_request)


def _demarrages_de_rediger(fournisseur: FakeAnthropic, settings: Settings) -> int:
    """Combien de fois *rédiger* a démarré, d'après les requêtes réellement envoyées.

    Le nombre est rendu tel quel, jamais comparé à 1 par le helper : la relance d'AD-3 est un chemin
    normal, et c'est à l'appelant de dire s'il attend « au moins un » démarrage ou « aucun ».

    `rediger_max_tokens` est l'unique plafond de rédaction des deux pipelines et de toutes leurs
    variantes (`steps/rediger.py`) : c'est donc lui, et non un rang dans la liste ni un identifiant
    de modèle partagé par plusieurs étapes, qui identifie l'appel dont l'AC parle.
    """
    return sum(1 for request in fournisseur.requests
               if request["max_tokens"] == settings.rediger_max_tokens)


async def test_preflight_outils_nominal_passe_et_un_depassement_reste_refuse(
        index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le vrai pipeline outils atteint *rédiger* sous 0,12 €, puis refuse juste au-dessus."""
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
        # AD-1 : un premier tour qui rend `tool_use` impose un appel de plus par tour restant — la
        # navigation doit conclure (`sufficient: false`, aucun `result_uid`) avant que *rédiger*
        # démarre. Le nombre de tours n'est **pas** recopié ici : il se lit sur le budget, si bien
        # qu'abaisser `max_llm_turns` raccourcit le script au lieu de le laisser trop long et muet.
        conclusion = fake_message(
            model=modele_attendu("retrouver_outils", settings), stop_reason="end_turn",
            text=json.dumps({"sufficient": False, "result_uid": None}))
        navigation = [outils, *[conclusion] * (settings.max_llm_turns - 1)]
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
    preflights: list[tuple[float, float, str, int]] = []

    def relever(*args, **kwargs):
        estimate = original(*args, **kwargs)
        max_tokens = int(args[3] if len(args) > 3 else kwargs["max_tokens"])
        preflights.append((budget.cost_eur, estimate, str(args[0]), max_tokens))
        return estimate

    monkeypatch.setattr(client_module, "estimate_cost", relever)
    fournisseur = FakeAnthropic(script())
    client = LlmClient(settings, anthropic_client=fournisseur)
    answer, trace = await sinistre.run(
        DOC_ID, QUESTION, FAITS, corpus=index.corpus, index=index, client=client,
        settings=settings, request_id="preflight-outils-reel", budget=budget)

    # La propriété visée n'est ni un rang dans la liste des requêtes ni un identifiant de modèle
    # (deux étapes peuvent partager le même tier, et un rang bouge dès qu'un tour de navigation est
    # ajouté) : c'est le **démarrage de *rédiger***, et ce qui le désigne sans ambiguïté est son
    # plafond de sortie, unique dans la chaîne du sinistre.
    redactions = [p for p in preflights if p[3] == settings.rediger_max_tokens]
    # **Au moins un** démarrage, et c'est le premier que l'AC décrit : la relance d'AD-3 est un
    # chemin normal, pas un incident, et exiger `== 1` ferait rougir ce test sur une chaîne
    # parfaitement conforme qui relance *rédiger* (c'est la correction déjà faite plus bas sur les
    # étapes de la trace, restée à faire ici).
    assert redactions, "*rédiger* n'a jamais démarré sur le chemin nominal"
    engage, majorant, model, _tokens = redactions[0]
    assert model == modele_attendu("rediger", settings)
    # Ce que le coût engagé doit valoir n'est **pas** un littéral en euros : une constante de coût
    # est le même défaut qu'un littéral de tier, déplacé du modèle vers l'euro — elle n'est vraie que
    # pour l'affectation d'étages du jour. La propriété est que ce coût est celui des étapes qui ont
    # réellement tourné avant *rédiger*, quel que soit le tier qui les sert.
    avant_rediger = [pas for pas in trace.steps
                     if pas.name in ("comprendre", "retrouver") and pas.calls]
    assert [pas.name for pas in avant_rediger] == ["comprendre", "retrouver"]
    assert engage == pytest.approx(sum(pas.usage.cost_eur for pas in avant_rediger), abs=1e-4)
    assert engage + majorant <= settings.max_cost_eur_per_request
    assert _demarrages_de_rediger(fournisseur, settings) >= 1
    assert answer.verdict is not None

    fournisseur_bloque = FakeAnthropic(script())
    client_bloque = LlmClient(settings, anthropic_client=fournisseur_bloque)
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
            request_id="preflight-outils-bloque", budget=budget)
    # La chaîne est allée au bout de la navigation (les deux tours d'AD-1) puis s'est arrêtée
    # **avant** de payer *rédiger* : c'est le refus que l'AC demande, et c'est le démarrage de
    # *rédiger* qui le dit — pas le tier, que la navigation partage désormais avec la rédaction.
    # `max_llm_turns` borne la navigation ; le navigateur peut conclure avant (le scénario nominal
    # rend son verdict au deuxième tour), donc le compte est un **majorant**, pas une égalité.
    assert 2 <= len(fournisseur_bloque.requests) <= 1 + settings.max_llm_turns
    assert _demarrages_de_rediger(fournisseur_bloque, settings) == 0


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
        decisionnelle = any(documents[index.doc_of(q.block_id)].block(q.block_id).kind
                            in ("garantie", "exclusion", "condition", "franchise")
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
    assert trace.pipeline == "sinistre" and trace.variant == sinistre.VARIANT == "outils"
    retrouver = trace.steps[1]
    assert retrouver.name == "retrouver"
    verifier_etage(retrouver, settings, etape="retrouver_outils")
    assert len(retrouver.calls) >= 1, "la navigation par outils appelle le modèle"
    assert len(retrouver.calls) <= settings.max_llm_turns  # bornée à deux tours (AD-1)
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
    ouvert = decider(_au_mieux_disant(answer, index, exigees=[subite], non_etablies=[subite]),
                     ask_client_max=settings.ask_client_max)
    assert ouvert.value != "couvert", ouvert.value  # la qualité non établie referme la règle (3)
    assert any(subite in q for q in ouvert.ask_client)
    # et la règle (3) n'est pas morte pour autant : sans qualité exigée, la même garantie du socle
    # sort `couvert`. C'est bien le corroborant qui manque au cas bougie, pas le chemin.
    sans_qualite = decider(_au_mieux_disant(answer, index), ask_client_max=settings.ask_client_max)
    assert sans_qualite.value == "couvert", sans_qualite.value

    # (5bis) Revue Codex 1.8 (B3, tour 3) : l'énumération des qualités ne repose plus sur la parole du
    # modèle. Le code relit le **texte** de la clause citée, et sur la garantie réelle de l'article
    # 3.1.1.1.6 il y trouve les quatre qualificatifs que le contrat écrit — c'est la source
    # indépendante qui manquait au tour 2, mesurée ici sur le corpus réel et non sur une fixture.
    garantie = index.corpus.documents[DOC_ID].block(f"{DOC_ID}:p34:12")
    assert set(_mots_qualifiants(garantie.text)) == {"soudain", "subit", "direct", "immediat"}
    # Et la conséquence, quelle qu'ait été l'humeur du modèle : tant que la clause n'est pas tenue
    # pour applicable, chacune des qualités qu'elle écrit est **demandée** au client — nommée par le
    # modèle, ou ajoutée par le code faute de l'avoir été.
    citante = next((c for c in answer.claims
                    if any(q.block_id == garantie.block_id for q in c.quotes)), None)
    if citante is not None and citante.status.applicable != "oui":
        demandes = _mots_qualifiants(" ".join(verdict.missing.faits))
        assert all(racine in demandes for racine in _mots_qualifiants(garantie.text)), (
            verdict.missing.faits)

    # AD-6 : un verdict autre que `ne_tranche_pas` repose sur une clause fondatrice **affichée**
    if verdict.value != "ne_tranche_pas":
        fondatrices = [c for c in answer.claims
                       if any(documents[index.doc_of(q.block_id)].block(q.block_id).kind
                              in ("garantie", "exclusion") for q in c.quotes)]
        assert fondatrices
