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
4. la requête entière tient sous le plafond de coût, et *vérifier* n'a fait qu'**un** appel `micro`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.app.corpus.text import normalize
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
from tests.llm_fake import RecordedAnthropic

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
    assert trace.pipeline == "sinistre" and trace.variant == "deterministe"
    assert trace.steps[1].calls == []  # *retrouver* déterministe : aucun modèle
    for step in (s for s in trace.steps if s.name == "verifier"):
        assert len(step.calls) == 1, "AD-9 amendé : un seul appel `micro`, jamais un second"
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
