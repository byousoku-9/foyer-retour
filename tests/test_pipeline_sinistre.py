"""Matrice I/O de `pipelines/sinistre.py` (spec 1.8), LLM mocké : les cinq étapes, le verdict d'AD-6,
la relance unique d'AD-3, les bornes d'entrée et la trace d'AD-10.

Comme pour le guide, `FakeAnthropic` lève sur tout appel non scripté : la **longueur du script est
une assertion**. C'est ainsi que « *vérifier* n'a fait qu'**un** appel `micro` » (AD-9 amendé) se
vérifie sans compter les appels à la main.
"""

from __future__ import annotations

import json

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.document import Document, Node
from server.app.domain.errors import CorpusUnavailable, InvalidRequest
from server.app.domain.ingest import ManifestEntry
from server.app.domain.question import Faits
from server.app.domain.verdict import ChampsApplicabilite, ClaimJugee, ClauseCitee, MissingPackage, decider
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines import sinistre
from tests.llm_fake import FakeAnthropic, fake_message

DOC_ID = "cg"
GARANTIE = ("Les dégâts occasionnés au mobilier assuré et au bâtiment désigné par un événement "
            "soudain, résultant de l'action subite de la chaleur, sont couverts.")
EXCLUSION = ("Pour les extensions mentionnées aux points 2.1 et 2.2, les dégâts occasionnés au "
             "bâtiment par l'action subite de la chaleur sont exclus.")
# Revue Codex 1.8 (B3, tour 3) : l'exclusion du socle exigeait « intentionnellement », une qualité
# qu'aucun fait de la bougie n'établit — depuis que le **texte de la clause** est relu, elle ne
# pouvait donc plus s'appliquer, et la fixture `non_couvert` de l'AC devenait injouable. Elle exige
# maintenant la même qualité que la garantie (« action subite de la chaleur »), que les faits
# déclarés établissent : l'exclusion mord parce que les faits la portent, pas par défaut.
EXCLUSION_SOCLE = ("Sont exclus, en toute circonstance, les dommages de brûlure résultant de "
                   "l'action subite de la chaleur.")
CONDITION = "La garantie n'est acquise que si le bien est occupé de manière permanente."
DEFINITION = "Le contenu comprend le mobilier de jardin et les objets confiés à un Assuré."

Q_GARANTIE = "dégâts occasionnés au mobilier assuré et au bâtiment désigné par un événement soudain"
Q_EXCLUSION = "dégâts occasionnés au bâtiment par l'action subite de la chaleur sont exclus"
Q_EXCLUSION_SOCLE = "dommages de brûlure résultant de l'action subite de la chaleur"
Q_CONDITION = "n'est acquise que si le bien est occupé de manière permanente"
Q_DEFINITION = "contenu comprend le mobilier de jardin et les objets confiés"

QUESTION = "Ce sinistre est-il couvert par le contrat ?"
FAITS = Faits(date="2026-08-01", lieu="salon du domicile", montant_eur=1200.0,
              description="Une bougie posée sur une table a brûlé le mobilier de salon, "
                          "sans embrasement ni commencement d'incendie. La chute a été soudaine "
                          "et la chaleur a agi de façon subite.")


@pytest.fixture(scope="module")
def index() -> Index:
    """Mini-contrat à l'image du contrat AXA : un socle `commun`, une branche `extension`."""
    blocs = [
        {"block_id": f"{DOC_ID}:p1:1", "loc": "p1", "seq": 1, "kind": "heading", "text": "Incendie"},
        {"block_id": f"{DOC_ID}:p1:2", "loc": "p1", "seq": 2, "kind": "garantie", "text": GARANTIE,
         "kind_source": "manual", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p1:3", "loc": "p1", "seq": 3, "kind": "condition", "text": CONDITION,
         "kind_source": "manual", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p1:4", "loc": "p1", "seq": 4, "kind": "definition", "text": DEFINITION,
         "kind_source": "manual", "defines": "contenu", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p1:5", "loc": "p1", "seq": 5, "kind": "exclusion",
         "text": EXCLUSION_SOCLE, "kind_source": "manual", "scope_node_id": f"{DOC_ID}:socle"},
        {"block_id": f"{DOC_ID}:p2:1", "loc": "p2", "seq": 1, "kind": "exclusion", "text": EXCLUSION,
         "kind_source": "manual", "scope_node_id": f"{DOC_ID}:ext"},
    ]
    doc = Document(
        doc_id=DOC_ID, kind="contrat", title="Mini contrat", edition="juin 2017",
        nodes=[Node(node_id=f"{DOC_ID}:socle", level=1, title="Socle commun",
                    items=[{"block_id": f"{DOC_ID}:p1:1"}, {"block_id": f"{DOC_ID}:p1:2"},
                           {"block_id": f"{DOC_ID}:p1:3"}, {"block_id": f"{DOC_ID}:p1:4"},
                           {"block_id": f"{DOC_ID}:p1:5"}]),
               Node(node_id=f"{DOC_ID}:ext", level=1, title="Extensions", scope={"kind": "extension"},
                    items=[{"block_id": f"{DOC_ID}:p2:1"}]),
               Node(node_id=f"{DOC_ID}:root", level=0, title="Contrat",
                    items=[{"node_id": f"{DOC_ID}:socle"}, {"node_id": f"{DOC_ID}:ext"}])],
        blocks=blocs)
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    manifest = {DOC_ID: ManifestEntry(status="servi", source_hash="sha-source", ingest_fingerprint="fp-1",
                                      document_hash="sha-doc", edition="juin 2017")}
    return Index(Corpus(documents={DOC_ID: doc}, manifest=manifest,
                        summaries={DOC_ID: "# Mini contrat\n- socle Socle commun\n- ext Extensions"}))


def _settings(**kw) -> Settings:
    kw.setdefault("sinistre_doc_id", DOC_ID)
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget(deadline_s: float = 30.0) -> RequestBudget:
    return RequestBudget(deadline_s=deadline_s, max_attempts=6, max_cost_eur=0.10)


def _comprendre(intent: str = "question", *, terms: list[str] | None = None,
                clarification: str | None = None) -> dict:
    resolue = None if clarification else "Le mobilier de salon brûlé par une bougie est-il couvert ?"
    return fake_message(model=TIERS["micro"], text=json.dumps({
        "intent": intent, "question_resolue": resolue, "clarification": clarification,
        "language": "fr", "terms": terms if terms is not None else ["mobilier", "chaleur", "contenu"],
        "themes": [], "facettes": ["couverture du sinistre"], "bien": "mobilier de salon",
        "evenement": "incendie sans embrasement", "lieu": "domicile", "cause": "bougie",
        "moment": "2026-08-01"}))


def _rediger(*claims: tuple[str, str, list[tuple[str, str]]]) -> dict:
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": [{"text": f"Clause {cid}.", "kind": "factuel", "claim_ids": [cid]}
                     for cid, _, _ in claims],
        "claims": [{"claim_id": cid, "text": texte,
                    "quotes": [{"block_id": b, "quote": q} for b, q in quotes]}
                   for cid, texte, quotes in claims]}))


# Deux fragments **mot pour mot** des faits déclarés, chacun employant les mots d'une qualité : les
# deux conditions pour qu'elle soit tenue pour établie (revue Codex 1.8, B3, tour 2).
FRAGMENT = "la chaleur a agi de façon subite"
FRAGMENT_SOUDAIN = "La chute a été soudaine"
SOUDAIN, SUBITE = "caractère soudain de l'événement", "action subite de la chaleur"
# L'énumération **fidèle** des clauses de ce contrat de test : les deux qualités que leur texte écrit
# (« soudain », « subite »), chacune établie par un fragment des faits déclarés. C'est le défaut du
# script depuis la revue Codex 1.8 (B3, tour 3) : le code relit le texte de la clause et ajoute
# lui-même toute qualité que le modèle n'a pas nommée, si bien que deux listes vides ne valent plus
# « aucune qualité exigée ». Les tests qui ne portent pas sur ce contrôle n'ont donc pas à la
# réécrire ; ceux qui portent dessus donnent leurs listes explicitement.
QUALITES_FIDELES = ([SOUDAIN, SUBITE], [(SOUDAIN, FRAGMENT_SOUDAIN), (SUBITE, FRAGMENT)])


def _verifier(*entrees: tuple, nb_segments: int = 8, enumere: bool = True) -> dict:
    """`(claim_id, pertinente, fait_requis_present, option_requise, cp_requise, fait_manquant)`,
    éventuellement suivi de `(qualites_exigees, qualites_etablies)` — revue Codex 1.8 (B3).

    Une qualité établie est un libellé (le fragment cité est alors `FRAGMENT`) ou un couple
    `(qualite, fait_cite)`. `enumere=False` omet les deux listes, comme un modèle qui n'énumère rien.
    """
    applicabilite = []
    for entree in entrees:
        cid, _p, present, option, cp, manquant = entree[:6]
        bloc = {"claim_id": cid, "fait_requis_present": present, "option_requise": option,
                "cp_requise": cp, "fait_manquant": manquant}
        if enumere:
            # Défaut : l'énumération fidèle de la clause quand le modèle dit le fait requis présent,
            # les deux listes vides sinon — ce que le prompt exige (« si le périmètre n'est pas bon,
            # les deux listes sont vides »), et ce que le contrôle du tour 3 lit.
            fidele = QUALITES_FIDELES if present else ([], [])
            bloc["qualites_exigees"] = list(entree[6]) if len(entree) > 6 else list(fidele[0])
            bloc["qualites_etablies"] = [
                {"qualite": q, "fait_cite": FRAGMENT} if isinstance(q, str)
                else {"qualite": q[0], "fait_cite": q[1]}
                for q in (entree[7] if len(entree) > 7 else fidele[1])]
        applicabilite.append(bloc)
    return fake_message(model=TIERS["micro"], text=json.dumps({
        "verdicts": [{"claim_id": c, "pertinente": p} for c, p, *_ in entrees],
        "facettes": [{"facette": 0, "claim_ids": [c for c, p, *_ in entrees if p]}],
        "segments": [{"segment": i, "soutenu": True} for i in range(nb_segments)],
        "applicabilite": applicabilite}))


async def _run(index: Index, script: list, *, settings: Settings | None = None,
               budget: RequestBudget | None = None, faits=FAITS, doc_id: str | None = None,
               variant: str = "deterministe", dossier: MissingPackage | None = None):
    settings = settings or _settings()
    fake = FakeAnthropic(script)
    client = LlmClient(settings, anthropic_client=fake)
    answer, trace = await sinistre.run(doc_id, QUESTION, faits, corpus=index.corpus, index=index,
                                       client=client, settings=settings, request_id="req-sinistre",
                                       variant=variant, budget=budget or _budget(), dossier=dossier)
    return answer, trace, fake


def _questions_attendues(verdict) -> bool:
    """AD-6 : « toujours avec le paquet manquant **et les questions à poser** ».

    Les quatre pièces annoncées manquantes doivent chacune avoir sa question — sans quoi le
    gestionnaire lit quatre absences et se voit demander deux choses (revue 1.8).
    """
    questions = " ".join(verdict.ask_client).lower()
    return ("options" in questions and "conditions particulières" in questions
            and "avenant" in questions and "date" in questions)


GAR = ("c1", "Les dégâts au mobilier par action subite de la chaleur sont couverts.",
       [(f"{DOC_ID}:p1:2", Q_GARANTIE)])
DEF = ("c2", "Le contenu comprend le mobilier.", [(f"{DOC_ID}:p1:4", Q_DEFINITION)])
EXC_EXT = ("c3", "Les extensions excluent les dégâts au bâtiment.", [(f"{DOC_ID}:p2:1", Q_EXCLUSION)])
EXC_SOCLE = ("c4", "Le dommage de brûlure est exclu.", [(f"{DOC_ID}:p1:5", Q_EXCLUSION_SOCLE)])
COND = ("c5", "Le bien doit être occupé de manière permanente.", [(f"{DOC_ID}:p1:3", Q_CONDITION)])
MAUVAISE = ("c9", "Le sinistre est couvert à 100 %.", [(f"{DOC_ID}:p1:2", "couvert à cent pour cent")])


# --- nominal : le cas bougie -------------------------------------------------
async def test_the_candle_case_runs_the_five_steps_and_carries_its_verdict(index: Index) -> None:
    """AC : cinq étapes, `pipeline="sinistre"`, un seul appel `micro` dans *vérifier*, verdict complet."""
    answer, trace, fake = await _run(index, [
        _comprendre(),
        _rediger(GAR, DEF, EXC_EXT),
        _verifier(("c1", True, False, False, False, "caractère subit de l'action de la chaleur"),
                  ("c2", True, False, False, False, None),
                  ("c3", True, False, False, False, None))])
    assert fake.remaining_script == 0 and len(fake.requests) == 3  # micro, reason, micro
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier", "restituer"]
    assert trace.pipeline == "sinistre" and trace.variant == "deterministe"
    assert len(next(s for s in trace.steps if s.name == "verifier").calls) == 1

    assert answer.found is True
    verdict = answer.verdict
    assert verdict is not None
    # Le modèle est **intégralement scripté** : la valeur est déterministe, elle se fixe. Exclusion
    # p2:1 écartée (`non` : elle vise les extensions), garantie incertaine (`humain` par son fait
    # manquant), aucune garantie `oui` ni exclusion `oui` ⇒ règle (4) de la table.
    assert verdict.value == "ne_tranche_pas"
    statuts = {c.claim_id: c.status.applicable for c in answer.claims}
    assert statuts == {"c1": "humain", "c2": None, "c3": "non"}
    assert verdict.missing.faits == ["caractère subit de l'action de la chaleur"]
    # matrice I/O : `ask_client` cite les options / conditions particulières **et** la nature « subite »
    assert any("caractère subit" in q for q in verdict.ask_client)
    assert any("options" in q for q in verdict.ask_client)
    assert any("conditions particulières" in q for q in verdict.ask_client)
    assert verdict.reason and "conditions générales seules" in verdict.reason
    # AD-6 : le paquet manquant est toujours là, et le verdict n'est jamais une décision d'indemnisation
    assert verdict.missing.conditions_particulieres and verdict.missing.options_souscrites


async def test_every_displayed_claim_carries_a_typed_applicability(index: Index) -> None:
    """AC : chaque claim affichée porte `oui|non|humain`, ou `None` si elle ne cite aucune clause."""
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, DEF),
        _verifier(("c1", True, True, False, False, None), ("c2", True, False, False, False, None))])
    for claim in answer.claims:
        assert claim.status.applicable in ("oui", "non", "humain", None)
        assert claim.status.retrouvee is True and claim.status.pertinente is True


# --- les autres lignes de la table -------------------------------------------
async def test_an_applicable_exclusion_over_the_case_is_not_covered(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, EXC_SOCLE),
        _verifier(("c1", True, True, False, False, None), ("c4", True, True, False, False, None))])
    assert answer.verdict is not None and answer.verdict.value == "non_couvert"


async def test_a_baseline_guarantee_alone_is_covered(index: Index) -> None:
    """La fixture que l'AC exige nommément — « `couvert` (garantie socle) » —, **jouée par le pipeline**.

    Revue Codex 1.8 (B1, tour 2). Le tour 1 lisait la seconde branche de la règle (2) d'AD-6 sur
    `MissingPackage` — le dossier **global** —, si bien que `couvert` réclamait un argument `dossier`
    que l'AC ne mentionne nulle part. L'AC dit : « (2) garantie `oui` et (condition/franchise/exclusion
    `humain` ou **garantie hors socle / dépendant d'une option**) », puis « (3) garantie du socle `oui`
    sans condition ouverte ⇒ `couvert` ». La dépendance se lit donc sur la clause, et la fixture est
    jouée telle que l'AC l'écrit : la garantie du socle seule, sans rien de plus.

    Ce qui empêche un `couvert` de complaisance n'est plus une politique globale mais le contrôle des
    qualités (B3) : la clause exige « un événement soudain » et « l'action subite de la chaleur », le
    modèle les énumère et cite pour chacune un fragment des faits **relu mot pour mot** par le code.
    Le test suivant retire cette corroboration et le verdict retombe.
    """
    soudain, subite = "caractère soudain de l'événement", "action subite de la chaleur"
    answer, trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [soudain, subite],
                   [(soudain, FRAGMENT_SOUDAIN), (subite, FRAGMENT)]))])
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "couvert"
    assert "socle commun" in verdict.reason and "conditions générales seules" in verdict.reason
    assert verdict.escalate == [] and verdict.missing.faits == []
    assert [c.status.applicable for c in answer.claims] == ["oui"]
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "restituer"]
    # AD-6 : le paquet manquant accompagne **aussi** un `couvert` — le verdict ne vaut qu'au regard
    # des conditions générales, et il le dit en réclamant les quatre pièces qu'il n'a pas lues.
    assert verdict.missing.conditions_particulieres and verdict.missing.options_souscrites
    assert _questions_attendues(verdict)
    # les deux qualités exigées restent à faire confirmer par le client (B3)
    assert sum("confirmer" in q for q in verdict.ask_client) == 2

    # et la table dit la même chose hors pipeline, sur la clause relue dans le corpus
    (claim,) = answer.claims
    (quote,) = claim.quotes
    document = index.corpus.documents[DOC_ID]
    bloc = document.block(quote.block_id)
    noeud = document.node_of(bloc.block_id)
    jugee = ClaimJugee(
        claim_id=claim.claim_id, champs=ChampsApplicabilite(fait_requis_present=True),
        clauses=[ClauseCitee(block_id=bloc.block_id, kind=bloc.kind, kind_confirmed=bloc.kind_confirmed,
                             portee=document.scope_nodes(bloc.block_id), node_id=noeud,
                             socle=document.node_scope_kind(noeud) == "commun")])
    assert decider([jugee], ask_client_max=_settings().ask_client_max).value == "couvert"


async def test_the_caller_who_holds_the_file_is_not_asked_for_it_again(index: Index) -> None:
    """`run(..., dossier=…)` ne change pas la valeur, il change ce qu'on réclame (B1, tour 2).

    Le paquet contractuel que l'appelant détient reste une **entrée** du pipeline — jamais une
    déduction — et il alimente `MissingPackage` : ce qui est au dossier n'est plus annoncé manquant
    ni redemandé. La table, elle, a déjà tranché sans lui.
    """
    soudain, subite = "caractère soudain de l'événement", "action subite de la chaleur"
    complet = MissingPackage(conditions_particulieres=False, options_souscrites=False,
                             avenants=False, date_effet=False)
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [soudain, subite],
                   [(soudain, FRAGMENT_SOUDAIN), (subite, FRAGMENT)]))],
        dossier=complet)
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "couvert"
    assert verdict.missing.conditions_particulieres is False
    assert all("options" not in q for q in verdict.ask_client)


async def test_a_guarantee_whose_qualities_are_not_enumerated_is_never_covered(index: Index) -> None:
    """Revue Codex 1.8 (B3, tour 2) : le silence du modèle ne vaut pas « aucune qualité exigée ».

    Même chaîne, mêmes clauses, même `fait_requis_present=true` que la fixture `couvert` — mais le
    modèle **n'énumère pas** les qualités. C'est le trou par lequel un `couvert` passait : le défaut
    vide des deux listes rendait le contrôle sans prise sur une clause qui exige pourtant « un
    événement soudain ». Le jeu de champs est désormais inexploitable et la claim vaut `humain`.
    """
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None), enumere=False)])
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "ne_tranche_pas"
    assert [c.status.applicable for c in answer.claims] == ["humain"]


async def test_a_guarantee_whose_qualities_are_declared_empty_is_never_covered(index: Index) -> None:
    """Revue Codex 1.8 (B3), tour 3 : la liste vide **écrite** n'est pas une preuve non plus.

    Même chaîne et même `fait_requis_present=true` que la fixture `couvert` de l'AC, mais le modèle
    rend `"qualites_exigees": []` sur une garantie dont le texte écrit « par un événement soudain,
    résultant de l'action subite de la chaleur ». Le tour 2 l'acceptait — « une clause qui n'exige
    réellement aucune qualité se dit `[]` » —, c'est-à-dire encore sur la parole du modèle. Le texte
    de la clause tranche désormais : les deux qualités qu'il écrit deviennent des qualités non
    établies, la garantie vaut `humain` et le verdict ne peut plus être `couvert`.
    """
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [], []))])
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "ne_tranche_pas"
    assert [c.status.applicable for c in answer.claims] == ["humain"]
    assert verdict.missing.faits == ["caractère « soudain » exigé par la clause citée",
                                     "caractère « subite » exigé par la clause citée"]


async def test_facts_that_deny_the_quality_never_yield_a_covered_verdict(index: Index) -> None:
    """Revue Codex 1.8 (B3), tour 3 : un fait qui **contredit** la qualité ne l'établit pas.

    Contre-exemple de la revue : « action subite de la chaleur » tenue pour établie par « La chaleur a
    agi lentement », parce que le seul mot *chaleur* recoupait la qualité. Joué de bout en bout, avec
    des faits déclarés qui disent le contraire de ce que la clause exige.
    """
    faits = Faits(date="2026-08-01", lieu="salon du domicile", montant_eur=1200.0,
                  description="Une bougie posée sur une table a brûlé le mobilier de salon. "
                              "La chaleur a agi lentement, sur plusieurs heures.")
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [SOUDAIN, SUBITE],
                   [(SOUDAIN, "La chaleur a agi lentement"),
                    (SUBITE, "La chaleur a agi lentement")]))], faits=faits)
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "ne_tranche_pas"
    assert [c.status.applicable for c in answer.claims] == ["humain"]
    assert verdict.missing.faits == [SOUDAIN, SUBITE]


async def test_a_quality_corroborated_by_nothing_in_the_file_is_never_covered(index: Index) -> None:
    """Revue Codex 1.8 (B3, tour 2) : une auto-déclaration ne vaut pas corroboration par les faits.

    Le modèle recopie les qualités exigées dans les qualités établies — ce que le run réel du 24/08 a
    fait — mais le fragment qu'il cite pour chacune est repris de la **clause**, pas des faits
    déclarés. Le code le relit dans les faits soumis (AD-3 appliqué aux faits), ne l'y trouve pas, et
    les qualités retombent en « non établies ».
    """
    soudain = "caractère soudain de l'événement"
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [soudain],
                   [(soudain, "un événement soudain, résultant de l'action subite de la chaleur")]))])
    verdict = answer.verdict
    assert verdict is not None and verdict.value == "ne_tranche_pas"
    assert [c.status.applicable for c in answer.claims] == ["humain"]
    # Tour 3 : le modèle n'avait énuméré **qu'**une des deux qualités que la clause écrit — le code
    # relit son texte et ajoute la seconde, qui part elle aussi en question au client.
    assert verdict.missing.faits == [soudain, "caractère « subite » exigé par la clause citée"]


async def test_a_refusal_keeps_the_file_the_caller_already_has(index: Index) -> None:
    """Le `dossier` accompagne aussi un refus : on ne réclame pas au gestionnaire ce qu'il a déjà."""
    complet = MissingPackage(conditions_particulieres=False, options_souscrites=False,
                             avenants=False, date_effet=False)
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(MAUVAISE), _rediger(("c9", "Autre tentative, aussi fausse.",
                                                     [(f"{DOC_ID}:p1:2", "couvert à quatre-vingt pour cent")]))],
        dossier=complet)
    assert answer.found is False and answer.verdict is not None
    assert answer.verdict.value == "ne_tranche_pas"
    assert answer.verdict.missing.conditions_particulieres is False
    assert answer.verdict.ask_client == []


async def test_the_same_guarantee_with_an_unestablished_quality_is_not_covered(index: Index) -> None:
    """Revue Codex 1.8 (B3), le pendant du test précédent : ce qui sépare `couvert` de `ne_tranche_pas`.

    La garantie de la fixture exige « l'action **subite** de la chaleur ». Le modèle la nomme et ne la
    retrouve pas dans les faits déclarés — puis coche quand même `fait_requis_present`, exactement
    comme le run réel qui a motivé le finding. Le code fait la différence des deux listes, la claim
    vaut `humain`, et la qualité manquante part en question au client.
    """
    subite = "caractère subit de l'action de la chaleur"
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR),
        _verifier(("c1", True, True, False, False, None, [subite], []))])
    verdict = answer.verdict
    assert [c.status.applicable for c in answer.claims] == ["humain"]
    assert verdict is not None and verdict.value == "ne_tranche_pas"
    # Tour 3 : « soudain », que la clause écrit et que le modèle n'a pas nommé, s'ajoute par le code.
    assert verdict.missing.faits == [subite, "caractère « soudain » exigé par la clause citée"]
    assert any(subite in q for q in verdict.ask_client)


async def test_an_open_condition_keeps_the_verdict_conditional(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, COND),
        _verifier(("c1", True, True, False, False, None),
                  ("c5", True, False, False, False, "occupation permanente du bien"))])
    assert answer.verdict is not None and answer.verdict.value == "sous_conditions"


async def test_a_condition_not_met_never_yields_a_covered_verdict(index: Index) -> None:
    """Revue 1.8 (P1) : une condition citée dont le fait exigé n'est **pas** établi ouvre le verdict.

    Le modèle rend ici `fait_requis_present=false` sans nommer de fait manquant — la lecture naturelle
    de « le bien n'est pas occupé de manière permanente ». Traiter cela comme `non` sortirait la
    condition de la règle (2) et rendrait `couvert` sur un dossier dont une condition est en défaut.
    """
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR, COND),
        _verifier(("c1", True, True, False, False, None), ("c5", True, False, False, False, None))])
    assert [c.status.applicable for c in answer.claims] == ["oui", "humain"]
    assert answer.verdict is not None and answer.verdict.value == "sous_conditions"


async def test_a_guarantee_depending_on_an_option_is_conditional(index: Index) -> None:
    answer, _trace, _fake = await _run(index, [
        _comprendre(), _rediger(GAR), _verifier(("c1", True, True, True, False, None))])
    assert answer.verdict is not None and answer.verdict.value == "sous_conditions"
    assert any("options" in q for q in answer.verdict.ask_client)


# --- claims rejetées, relance, refus ------------------------------------------
async def test_a_rejected_claim_triggers_one_retry_then_refuses_with_a_verdict(index: Index) -> None:
    """AD-3 puis AD-16 : relance unique, puis refus motivé — **avec** `ne_tranche_pas`, jamais rien."""
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(MAUVAISE), _rediger(("c9", "Autre tentative, aussi fausse.",
                                                     [(f"{DOC_ID}:p1:2", "couvert à quatre-vingt pour cent")]))])
    assert fake.remaining_script == 0 and len(fake.requests) == 3  # comprendre, rédiger, rédiger
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "rediger", "verifier", "restituer"]
    assert answer.found is False
    assert answer.reason is not None and answer.reason.kind == "claims_rejetes"
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert answer.rejected_claims and all(c.rejection_kind == "non_retrouvee"
                                          for c in answer.rejected_claims)
    # le dossier qui a le plus besoin d'être complété est aussi celui qu'on questionne (revue 1.8)
    assert _questions_attendues(answer.verdict)
    # et la phrase servie est celle du **sinistre**, pas celle du guide
    assert "clause" in answer.texte and "guide" not in answer.texte


async def test_a_claim_mixing_two_clauses_is_sent_back_to_the_writer(index: Index) -> None:
    """D6 : la claim ambiguë déclenche la relance, et la seconde ébauche éclatée passe."""
    melangee = ("c1", "La garantie joue sauf pour les extensions.",
                [(f"{DOC_ID}:p1:2", Q_GARANTIE), (f"{DOC_ID}:p2:1", Q_EXCLUSION)])
    answer, trace, fake = await _run(index, [
        _comprendre(), _rediger(melangee), _rediger(GAR, EXC_EXT),
        _verifier(("c1", True, True, False, False, None), ("c3", True, False, False, False, None))])
    assert fake.remaining_script == 0
    # la première vérification n'a fait **aucun** appel : rien n'avait survécu au contrôle de citation
    assert [s.name for s in trace.steps].count("verifier") == 2
    assert answer.found is True and answer.verdict is not None
    # l'exclusion des extensions ne couvre pas le cas : la garantie du socle reprend la main
    assert answer.verdict.value == "couvert"
    assert "socle commun" in answer.verdict.reason
    # La claim mêlée a bien été rejetée par la **première** vérification — c'est elle qui a nourri la
    # relance. Elle ne figure plus dans la réponse servie parce que la seconde vérification domine et
    # la remplace en entier (AD-3) ; c'est la trace qui garde la preuve du rejet.
    premiere = next(s for s in trace.steps if s.name == "verifier")
    citations = next(c for c in premiere.checks if c.name == "citations")
    assert citations.ok is False and "1 rejetée(s) sur 1" in citations.detail
    assert answer.rejected_claims == []  # la seconde ébauche, éclatée, ne rejette rien


# --- bornes d'entrée : rien de facturé -----------------------------------------
async def test_an_unknown_document_is_refused_before_any_billed_call(index: Index) -> None:
    with pytest.raises(CorpusUnavailable):
        await _run(index, [], doc_id="inconnu")


async def test_an_unknown_variant_is_refused_before_any_billed_call(index: Index) -> None:
    with pytest.raises(InvalidRequest, match="variante"):
        await _run(index, [], variant="autre")


async def test_an_oversized_description_is_rejected_never_truncated(index: Index) -> None:
    # la borne citée dans le message vient du schéma, pas d'une constante recopiée (revue 1.8)
    assert sinistre._DESCRIPTION_MAX == 2000
    with pytest.raises(InvalidRequest, match="2000 caractères"):
        await _run(index, [], faits={"description": "x" * 2001})
    # le domaine porte la même borne : le pipeline ne fait que lui donner son code d'erreur
    with pytest.raises(ValueError):
        Faits(description="x" * 2001)


@pytest.mark.parametrize("mal_forme", ["une description libre", ["description"], 42])
async def test_malformed_facts_are_a_bad_request_not_an_internal_error(index: Index,
                                                                       mal_forme: object) -> None:
    """AD-16 : un corps que le serveur ne sait pas lire est un 400, jamais un 500 `internal`.

    `dict(faits)` lève `ValueError`/`TypeError` sur une chaîne, une liste ou un entier : hors du
    `try`, ces trois-là ressortaient en erreur interne alors que l'appelant a simplement mal formé
    son corps (revue 1.8).
    """
    with pytest.raises(InvalidRequest, match="objet lisible"):
        await _run(index, [], faits=mal_forme)


async def test_a_budget_and_a_deadline_are_exclusive(index: Index) -> None:
    """Revue 1.8 : `deadline_s` était ignoré en silence quand un budget était fourni.

    Le budget porte sa deadline et son horloge court déjà : accepter les deux laissait l'appelant
    croire qu'il bornait la requête alors qu'il ne bornait rien.
    """
    settings = _settings()
    client = LlmClient(settings, anthropic_client=FakeAnthropic([]))
    with pytest.raises(InvalidRequest, match="exclusifs"):
        await sinistre.run(None, QUESTION, FAITS, corpus=index.corpus, index=index, client=client,
                           settings=settings, request_id="r", budget=_budget(), deadline_s=5.0)


# --- court-circuits d'AD-5, toujours avec un verdict ----------------------------
async def test_an_out_of_scope_request_is_refused_after_one_micro_call(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre("hors_perimetre")])
    assert fake.remaining_script == 0 and len(fake.requests) == 1  # l'étage `reason` n'est pas atteint
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.found is False and answer.reason is not None
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert answer.verdict.escalate  # AD-16 : le dossier repart à la main, ce n'est pas un repli
    assert _questions_attendues(answer.verdict)
    # le texte servi parle du **contrat**, pas du guide (revue 1.8)
    assert "assurance habitation" in answer.texte and "guide" not in answer.texte


async def test_a_search_without_a_single_block_refuses_with_a_verdict(index: Index) -> None:
    answer, trace, fake = await _run(index, [_comprendre(terms=["zzzz"])])
    assert fake.remaining_script == 0 and len(fake.requests) == 1
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "restituer"]
    assert answer.reason is not None and answer.reason.kind == "zero_hit"
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert _questions_attendues(answer.verdict)
    assert "aucune clause du contrat" in answer.texte.lower() and "guide" not in answer.texte


async def test_a_request_that_cannot_be_made_autonomous_still_carries_a_verdict(index: Index) -> None:
    answer, trace, _fake = await _run(index, [_comprendre(clarification="De quel bien parlez-vous ?")])
    assert [s.name for s in trace.steps] == ["comprendre", "restituer"]
    assert answer.clarification == "De quel bien parlez-vous ?"
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert _questions_attendues(answer.verdict)
    assert "contrat" in answer.texte and "guide" not in answer.texte


# --- retrieval : les clauses passent devant à score égal (D7) --------------------
async def test_decisional_blocks_come_first_at_equal_score(index: Index) -> None:
    """AC : « cherche les blocs garantie|exclusion|condition|franchise candidats » — priorité, pas filtre."""
    answer, trace, _fake = await _run(index, [
        _comprendre(terms=["mobilier"]), _rediger(GAR), _verifier(("c1", True, True, False, False, None))])
    ouverts = next(s for s in trace.steps if s.name == "rediger").opened_block_ids
    assert f"{DOC_ID}:p1:2" in ouverts  # la garantie est bien transmise
    assert f"{DOC_ID}:p1:4" in ouverts  # la définition aussi : le typage ne **filtre** pas
    assert answer.found is True
