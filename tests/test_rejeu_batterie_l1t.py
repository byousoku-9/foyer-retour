"""Rejeu **hors ligne** des trois dossiers incohérents de la batterie du 03/09/2026 (L1t).

Trois cas, une seule famille : le verdict tenait un fait pour acquis et le dossier le redemandait.

- `s10-intention` — « Exclu » fondé sur « le fait d'avoir mis le feu exprès est une faute
  intentionnelle », et au-dessous « Fait à établir auprès du client : faute intentionnelle ou
  dolosive de l'assuré » ;
- `s03-velo` — « Exclu » fondé sur « le vélo … est un bien se trouvant à l'extérieur », et au-dessous
  la localisation du vol à établir ;
- `s11-bijoux` — « Exclu » (vol simple), et au-dessous le dépôt de plainte et la souscription de la
  garantie vol : les deux conditions de la garantie que l'exclusion venait d'écarter.

**Story 5.7 (L1u) : ces trois « Exclu » n'existaient que par la règle (3bis).** Elle est retirée —
un rattachement ne rend plus une exclusion `oui` —, et avec elle les trois `non_couvert` de ce
fichier : `s03-velo` rend `ne_tranche_pas`, `s10-intention` et `s11-bijoux` rendent
`sous_conditions`. C'est le prix mesuré et assumé de L1u, dont le gain se lit sur le gate `-18`
(`tests/test_rejeu_gate_l1u.py`) : un « exclu » que seule une répétition sur trois prononçait.

Ce que ce fichier prouve désormais est ce qui reste vrai des deux règles de L1t : elles n'ont jamais
retiré une question que sur le `oui` de la table, et **le verdict comme le dossier ne dépendent plus
du rattachement** — les trois cas sont rejoués avec et sans, et rendent la même chose des deux
côtés. Ce que le run a affiché reste figé dans `tests/data/batterie-3-l1t.json` (verdict,
`ask_client`, `missing.faits`, et pour chaque affirmation ses blocs, son `applicable`, son
rattachement et le fait qu'elle disait manquant) : c'est la mesure, et c'est à elle que la table
courante est comparée.

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
    applicabilites_des_claims,
    decider,
    exclusion_decisive,
    faits_etablis_par_rattachement,
)
from server.app.steps.verifier import _clauses_citees, _qualites_de_la_clause
from tests.rejeu_gate import SETTINGS, citation_entiere

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
    exclusions qui concluaient, c'était la seule lecture possible du `oui` mesuré, puisqu'un `oui`
    obtenu autrement que par la règle (3bis) n'aurait laissé aucun fait manquant à afficher. Depuis
    L1u, ces exclusions ne concluent plus, et leur `applicable` mesuré n'est plus celui que la
    table rend — la reconstruction ne le corrige pas pour autant : elle reste ce que le run a
    affiché, et c'est l'écart qui est mesuré.
    """
    jugees: list[ClaimJugee] = []
    for claim in cas["claims"]:
        clauses = _clauses_citees([citation_entiere(b, corpus=corpus, index=index)
                                   for b in claim["blocs"]], corpus=corpus, index=index,
                                  settings=SETTINGS)
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


VERDICTS_L1U = {"s03-velo": "ne_tranche_pas",
                "s10-intention": "sous_conditions",
                "s11-bijoux": "sous_conditions"}


@pytest.mark.parametrize("nom", sorted(CAS))
def test_lexclusion_qui_concluait_ne_conclut_plus(nom: str, corpus: Corpus) -> None:
    """Le prix de L1u, mesuré sur les trois dossiers qui l'avaient motivé (L1r).

    Chacun des trois « Exclu » tenait à une exclusion que la règle (3bis) rendait `oui` sur son
    rattachement : le run l'a affichée `oui`, la table courante ne la rend plus telle. Ce qui les
    remplace est nommé, cas par cas — `ne_tranche_pas` là où plus rien ne tranche,
    `sous_conditions` là où une garantie s'applique et qu'une pièce reste ouverte. Aucun ne dit
    l'inverse du run : ils disent moins.
    """
    cas = CAS[nom]
    jugees = _juger(cas, corpus=corpus, index=Index(corpus))
    etat = {claim_id: value for claim_id, (value, _r) in applicabilites_des_claims(jugees).items()}
    observe = {claim["claim_id"]: claim["applicable"] for claim in cas["claims"]}
    ecart = {claim_id for claim_id, value in etat.items() if value != observe[claim_id]}
    assert ecart, observe
    for claim_id in ecart:
        portante = next(j for j in jugees if j.claim_id == claim_id)
        assert portante.kind == "exclusion" and observe[claim_id] == "oui"
        assert portante.fait_rattache
        assert etat[claim_id] != "oui"
    assert cas["verdict_observe"] == "non_couvert"
    assert decider(jugees, ask_client_max=ASK_MAX).value == VERDICTS_L1U[nom]


@pytest.mark.parametrize("nom", sorted(CAS))
def test_le_dossier_redemande_ce_que_la_conclusion_taisait(nom: str, corpus: Corpus) -> None:
    """L'autre moitié du prix : les deux règles de L1t n'ont plus rien à taire ici.

    Elles ne retirent une question que sous une exclusion que la table tient pour `oui`
    (`exclusion_decisive`) ou sur un fait qu'un rattachement retenu a établi sur un `oui`
    (`faits_etablis_par_rattachement`). Plus aucune des trois exclusions n'atteint `oui` : les deux
    règles sont inertes sur ces dossiers, et ce que le run taisait revient — les conditions de la
    garantie de `s11` en tête. C'est la contrepartie exacte d'un verdict qui ne conclut plus, et
    elle est cohérente : ce qui n'est plus décidé doit être demandé.
    """
    cas = CAS[nom]
    jugees = _juger(cas, corpus=corpus, index=Index(corpus))
    etat = {claim_id: value for claim_id, (value, _r) in applicabilites_des_claims(jugees).items()}
    assert exclusion_decisive(jugees, etat=etat) is None
    assert faits_etablis_par_rattachement(jugees, etat=etat) == []
    verdict = decider(jugees, ask_client_max=ASK_MAX)
    assert "n'ont plus d'objet" not in verdict.reason
    # Le paquet contractuel reste dû quel que soit le verdict — c'est ce que la lecture n'a pas lu.
    assert verdict.ask_client
    # Les questions de la garantie que l'exclusion écartait sont de nouveau posées, là où le run les
    # portait sur une claim que la table ne referme plus.
    for question in cas["questions_de_trop"]:
        porteuses = [c for c in cas["claims"]
                     if (c["fait_manquant"] or "").strip() and c["fait_manquant"] in question]
        if porteuses and all(etat.get(c["claim_id"]) not in {"non", None} for c in porteuses):
            assert question in verdict.ask_client, question


@pytest.mark.parametrize("nom", sorted(CAS))
def test_le_fil_ne_repose_pas_ce_que_le_verdict_a_cesse_de_demander(nom: str,
                                                                   corpus: Corpus) -> None:
    """Le fil pose les mêmes questions que le verdict : il doit se taire pour les mêmes raisons.

    `_question_candidates` compose ses questions depuis les claims, pas depuis `ask_client` : sans
    le même filtre, le verdict se taisait et la page de suivi reposait le dépôt de plainte. Le
    miroir est ce qui compte, et il vaut dans les deux sens — depuis L1u ces dossiers ne concluent
    plus, les deux règles n'y retirent donc rien, et le fil doit alors reposer exactement ce que le
    verdict demande : aucune question de fait qui ne soit dans `ask_client`, aucune qui manque.
    """
    cas = CAS[nom]
    jugees = _juger(cas, corpus=corpus, index=Index(corpus))
    verdict = decider(jugees, ask_client_max=ASK_MAX)
    questions = _question_candidates(jugees, verdict)
    assert questions
    faits_du_fil = [q for q in questions if q.kind == "fait"]
    assert bool(faits_du_fil) == bool([q for q in verdict.ask_client
                                       if q.startswith("Fait à établir")])
    for question in faits_du_fil:
        assert any(libelle in question.text for libelle in verdict.missing.faits), question.text


@pytest.mark.parametrize("nom", sorted(CAS))
def test_le_verdict_et_le_dossier_ne_dependent_plus_du_rattachement(nom: str,
                                                                   corpus: Corpus) -> None:
    """La propriété que L1u installe, sur les trois dossiers qui avaient motivé L1r.

    Le même dossier, le même corpus, `fait_rattache` posé partout où le run a publié un
    rattachement, puis retiré partout — c'est-à-dire un contrôle groupé qui n'aurait relié aucun mot
    de la citation à aucun mot des faits déclarés. Verdict, raison, questions et faits manquants
    sont identiques des deux côtés : plus rien de ce que le client lit ne dépend de la phrase que le
    modèle a choisi d'écrire.
    """
    cas = CAS[nom]
    jugees = _juger(cas, corpus=corpus, index=Index(corpus))
    assert any(claim.fait_rattache for claim in jugees)
    sans = [claim.model_copy(update={"fait_rattache": False}) for claim in jugees]
    avec_verdict = decider(jugees, ask_client_max=ASK_MAX)
    sans_verdict = decider(sans, ask_client_max=ASK_MAX)
    assert avec_verdict.value == sans_verdict.value != "non_couvert"
    assert avec_verdict.reason == sans_verdict.reason
    assert avec_verdict.ask_client == sans_verdict.ask_client
    assert avec_verdict.missing.faits == sans_verdict.missing.faits
    assert _question_candidates(jugees, avec_verdict) == _question_candidates(sans, sans_verdict)


def test_la_garantie_de_s11_redemande_tout_avec_ou_sans_rattachement(corpus: Corpus) -> None:
    """Contre-épreuve pleine sur le cas où les deux questions venaient de la garantie écartée.

    Sans l'exclusion des vols simples pour conclure, le dépôt de plainte et la souscription de la
    garantie vol redeviennent exactement ce qu'ils sont : les deux conditions d'une garantie que
    rien n'écarte plus. Elles reviennent toutes les deux, et — c'est le changement de L1u — que le
    rattachement soit tenu pour soutenu ou non : L1t les taisait **parce que** l'exclusion
    s'appliquait, et plus aucune exclusion ne s'applique par ce chemin.
    """
    cas = CAS["s11-bijoux"]
    jugees = _juger(cas, corpus=corpus, index=Index(corpus))
    sans = [claim.model_copy(update={"fait_rattache": False}) for claim in jugees]
    for dossier in (jugees, sans):
        verdict = decider(dossier, ask_client_max=ASK_MAX)
        assert verdict.value == "sous_conditions"
        for question in cas["questions_de_trop"]:
            assert question in verdict.ask_client
