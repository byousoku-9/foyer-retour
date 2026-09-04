"""Rejeu **hors ligne** des trois dossiers incohérents de la batterie du 03/09/2026 (L1t).

Trois cas, une seule famille : le verdict tenait un fait pour acquis et le dossier le redemandait.

- `s10-intention` — « Exclu » fondé sur « le fait d'avoir mis le feu exprès est une faute
  intentionnelle », et au-dessous « Fait à établir auprès du client : faute intentionnelle ou
  dolosive de l'assuré » ;
- `s03-velo` — « Exclu » fondé sur « le vélo … est un bien se trouvant à l'extérieur », et au-dessous
  la localisation du vol à établir ;
- `s11-bijoux` — « Exclu » (vol simple), et au-dessous le dépôt de plainte et la souscription de la
  garantie vol : les deux conditions de la garantie que l'exclusion venait d'écarter.

**Le rouge et le vert sont deux rejeux de la même entrée.** Ce que le run a affiché est figé dans
`tests/data/batterie-3-l1t.json` — verdict, `ask_client`, `missing.faits`, et pour chaque affirmation
ses blocs, son `applicable`, son rattachement et le fait qu'elle disait manquant. Le premier témoin
recompose les questions comme elles l'étaient avant L1t et doit rendre `ask_client` mot pour mot :
c'est le rouge, mesuré et reproduit. Le second rejoue la table courante sur ce même dossier : c'est
le vert. Sans le premier, le second ne dirait rien du run.

Aucun appel réseau : le corpus est celui de `data/`, la réduction est figée dans `tests/data/`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.app.config import get_settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.domain.conversation import _question_candidates
from server.app.domain.verdict import (
    ChampsApplicabilite,
    ClaimJugee,
    MissingPackage,
    _libelles_manquants,
    _questions_de_section,
    applicabilites_des_claims,
    conditions_de_section_ouvertes,
    decider,
    questions_du_paquet_typees,
)
from server.app.steps.verifier import _clauses_citees, _qualites_de_la_clause
from tests.rejeu_gate import citation_entiere

ROOT = Path(__file__).resolve().parents[1]
BATTERIE = json.loads((Path(__file__).parent / "data" / "batterie-3-l1t.json").read_text())
CAS = {cas["nom"]: cas for cas in BATTERIE["cas"]}
ASK_MAX = get_settings().ask_client_max


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(ROOT / "data", allow_ungated=True)


def _juger(cas: dict, *, corpus: Corpus, index: Index) -> list[ClaimJugee]:
    """Le dossier mesuré, reconstruit en champs typés — même idiome que `tests.rejeu_gate`.

    Un `applicable` observé se réécrit sans ambiguïté : `oui` = fait requis présent, `non` = fait
    requis absent **sans** fait manquant, `humain` = un fait manquant nommé ou une pièce du contrat
    en suspens, `null` = aucune clause qui décide. S'y ajoutent le `fait_manquant` tel que le run l'a
    affiché — c'est lui l'objet de la mesure — et les deux pièces que la clause subordonne, relues
    dans les précisions du paquet contractuel (« Une clause citée ne joue qu'à cette condition. »,
    « Une clause citée y renvoie. ») et rattachées à la clause dont le texte porte le renvoi.

    Les qualités que le code compose à partir du texte de la clause (B3, L1n) ne sont reconstruites
    que sur une claim mesurée `humain` : sur une claim mesurée `oui`, le dossier les tenait toutes
    pour établies — le run n'a affiché aucune qualité à confirmer —, et les composer ici rendrait
    `humain` une claim que la mesure dit `oui`.

    `rattache` est lu sur la présence d'un rattachement dans la réponse publiée : pour les trois
    exclusions qui concluent, c'est la seule lecture possible du `oui` mesuré, puisqu'un `oui` obtenu
    autrement que par la règle (3bis) n'aurait laissé aucun fait manquant à afficher.
    """
    jugees: list[ClaimJugee] = []
    for claim in cas["claims"]:
        clauses = _clauses_citees([citation_entiere(b, corpus=corpus, index=index)
                                   for b in claim["blocs"]], corpus=corpus, index=index)
        observe = claim["applicable"]
        manquant = claim["fait_manquant"]
        exigees = (_qualites_de_la_clause(clauses, nommees=manquant or "", place=8)
                   if observe == "humain" else [])
        jugees.append(ClaimJugee(
            claim_id=claim["claim_id"], clauses=clauses, retenue=True,
            fait_rattache=claim["rattache"],
            champs=ChampsApplicabilite(
                fait_requis_present=observe == "oui",
                option_requise=claim["option_requise"],
                cp_requise=claim["cp_requise"],
                fait_manquant=manquant,
                qualites_exigees=exigees, qualites_non_etablies=exigees)))
    return jugees


@pytest.mark.parametrize("nom", sorted(CAS))
def test_la_reconstruction_reproduit_le_dossier_mesure(nom: str, corpus: Corpus) -> None:
    """Le témoin de mesure : mêmes `applicable`, même verdict, **et le même dossier redemandé**.

    Le verdict ne bouge pas — c'est le point : L1t ne change aucune décision, il change ce que le
    dossier demande encore. La composition d'avant L1t est donc rejouée telle qu'elle était — le
    paquet contractuel, les conditions de section ouvertes, puis les faits manquants de toutes les
    claims — et elle doit rendre `ask_client` **mot pour mot**. C'est le rouge : sans lui, le vert
    d'après ne dirait rien du run.
    """
    cas = CAS[nom]
    jugees = _juger(cas, corpus=corpus, index=Index(corpus))
    etat = {claim_id: value for claim_id, (value, _r) in applicabilites_des_claims(jugees).items()}
    assert etat == {claim["claim_id"]: claim["applicable"] for claim in cas["claims"]}
    assert decider(jugees, ask_client_max=ASK_MAX).value == cas["verdict_observe"] == "non_couvert"
    avant = ([texte for _kind, texte in questions_du_paquet_typees(jugees, MissingPackage())]
             + _questions_de_section(conditions_de_section_ouvertes(jugees, etat=etat))
             + [f"Fait à établir auprès du client : {libelle}"
                for libelle in _libelles_manquants(jugees, etat=etat, place=ASK_MAX)])
    assert avant == cas["ask_client_observe"]
    assert cas["faits_observes"]
    for question in cas["questions_de_trop"]:
        assert question in avant


@pytest.mark.parametrize("nom", sorted(CAS))
def test_un_fait_acquis_et_une_garantie_ecartee_ne_se_demandent_plus(nom: str,
                                                                    corpus: Corpus) -> None:
    """Le vert d'après : « Exclu » ne pose plus que le paquet contractuel.

    Les deux règles de L1t se lisent ici ensemble. Le fait qu'un rattachement retenu a établi
    disparaît de `missing.faits` comme de `ask_client` (`s10`, `s03`) ; les conditions de la garantie
    que l'exclusion écarte disparaissent avec elle (`s11` : le dépôt de plainte, et la question qui
    demandait si les conditions particulières mentionnent la garantie vol). Ce qui reste est ce qu'un
    verdict rendu « au regard des conditions générales seules » doit dire de toute façon : les pièces
    du contrat qu'il n'a pas lues.
    """
    cas = CAS[nom]
    jugees = _juger(cas, corpus=corpus, index=Index(corpus))
    verdict = decider(jugees, ask_client_max=ASK_MAX)
    assert verdict.value == "non_couvert"
    assert "n'ont plus d'objet" in verdict.reason
    assert verdict.missing.faits == []
    for question in cas["questions_de_trop"]:
        assert question not in verdict.ask_client
    assert not [q for q in verdict.ask_client if q.startswith("Fait à établir")]
    assert not [q for q in verdict.ask_client if q.startswith("Qualité exigée")]
    assert not [q for q in verdict.ask_client if "mentionnent-elles" in q]
    assert verdict.ask_client and all("options" in q or "conditions particulières" in q
                                      or "pris effet" in q for q in verdict.ask_client)


@pytest.mark.parametrize("nom", sorted(CAS))
def test_le_fil_ne_repose_pas_ce_que_le_verdict_a_cesse_de_demander(nom: str,
                                                                   corpus: Corpus) -> None:
    """Le fil pose les mêmes questions que le verdict : il doit se taire pour les mêmes raisons.

    `_question_candidates` compose ses questions depuis les claims, pas depuis `ask_client` : sans
    le même filtre, le verdict se taisait et la page de suivi reposait le dépôt de plainte. Aucune
    question de fait ne subsiste, et celle de la condition de section redevient la demande de dossier
    sur les conditions particulières.
    """
    cas = CAS[nom]
    jugees = _juger(cas, corpus=corpus, index=Index(corpus))
    verdict = decider(jugees, ask_client_max=ASK_MAX)
    questions = _question_candidates(jugees, verdict)
    assert questions
    assert not [q for q in questions if q.kind == "fait"]
    assert not [q for q in questions if "mentionnent-elles" in q.text]
    assert {q.kind for q in questions} <= {"option", "conditions_particulieres", "avenant_date"}


@pytest.mark.parametrize("nom", sorted(CAS))
def test_sans_rattachement_soutenu_rien_n_est_acquis_ni_sans_objet(nom: str,
                                                                   corpus: Corpus) -> None:
    """La borne des deux règles : elles ne tiennent que par le rattachement que le code a soutenu.

    Le même dossier, le même corpus, `fait_rattache` retiré partout — c'est-à-dire un contrôle groupé
    qui n'aurait relié aucun mot de la citation à aucun mot des faits déclarés. Aucune des trois
    exclusions ne conclut plus, donc plus rien n'est « sans objet », et rien n'est établi non plus :
    ce que L1t retire, il ne le retire jamais sur la seule présence d'une phrase de rattachement.
    """
    cas = CAS[nom]
    jugees = _juger(cas, corpus=corpus, index=Index(corpus))
    sans = [claim.model_copy(update={"fait_rattache": False}) for claim in jugees]
    verdict = decider(sans, ask_client_max=ASK_MAX)
    assert verdict.value != "non_couvert"
    assert "n'ont plus d'objet" not in verdict.reason


def test_la_garantie_ecartee_de_s11_redemande_tout_sans_le_rattachement(corpus: Corpus) -> None:
    """Contre-épreuve pleine sur le cas où les deux questions venaient de la garantie écartée.

    Sans l'exclusion des vols simples pour conclure, le dépôt de plainte et la souscription de la
    garantie vol redeviennent exactement ce qu'ils sont : les deux conditions d'une garantie que rien
    n'écarte plus. Elles reviennent toutes les deux — c'est la preuve que L1t les a tues **parce
    que** l'exclusion s'appliquait, et non parce qu'il aurait cessé de les voir.
    """
    cas = CAS["s11-bijoux"]
    jugees = _juger(cas, corpus=corpus, index=Index(corpus))
    sans = [claim.model_copy(update={"fait_rattache": False}) for claim in jugees]
    verdict = decider(sans, ask_client_max=ASK_MAX)
    assert verdict.value == "sous_conditions"
    for question in cas["questions_de_trop"]:
        assert question in verdict.ask_client
