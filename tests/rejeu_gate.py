"""Rejeu **hors ligne** des répétitions d'un cas de gate, sur le corpus réel et la table courante.

Story 5.7 (L1n) l'a écrit pour un rapport, L1p le généralise à tous : un gate rouge sur
`stabilite_sinistre` dit qu'un même cas a rendu deux verdicts, et la seule façon d'en nommer la cause
sans rappeler le modèle est de rejouer la table sur ce que le rapport porte.

Le rapport porte les claims, leurs citations et l'`applicable` que la table leur a donné ; il ne porte
pas le JSON brut du vérificateur. Le rejeu reconstruit donc, pour chaque affirmation, le **jeu de
champs canonique** qui produit cet `applicable`, puis rejoue sur les clauses relues dans le corpus les
deux relectures que le code fait par-dessus le modèle — les qualités exigées par le texte (B3, L1n) et
les renvois aux conditions particulières ou aux options (T18). Le premier témoin de chaque rejeu
vérifie que cette reconstruction reproduit la signature mesurée : sans quoi tout ce qui suit ne dirait
rien du run.

`avant` nomme les correctifs qui n'étaient **pas** en vigueur le jour du run — ou, pour L1u, les
règles qui l'étaient encore et que ce tour retire. C'est ce qui permet de
rejouer côte à côte la lecture mesurée et la lecture corrigée : la même entrée, qui divergeait et qui
converge. Aucun appel réseau — le corpus est celui de `data/`, les rapports sont figés dans
`tests/data/`.
"""

from __future__ import annotations

import json
from pathlib import Path

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.answer import VerifiedQuote
from server.app.domain.verdict import ChampsApplicabilite, ClaimJugee, decider
from server.app.steps.verifier import (RENVOIS_CP, RENVOIS_OPTION, _clauses_citees,
                                       _exigence_niee_par_la_declaration, _lecture_niee,
                                       _mots_qualifiants, _mots_renvoi, _qualites_de_personne,
                                       _qualites_de_la_clause)

SETTINGS = Settings(_env_file=None, anthropic_api_key="")

DATA = Path(__file__).parent / "data"


def citation_entiere(block_id: str, *, corpus: Corpus, index: Index) -> VerifiedQuote:
    """Le bloc cité **en entier**, sous la forme que `_clauses_citees` attend (story 5.7, L1r).

    Depuis L1r, ce que la clause exige se lit dans l'item que la **citation** traverse, et non plus
    dans tout le bloc : `_clauses_citees` reçoit donc des citations vérifiées. Ni les rapports de
    gate figés ni les contre-épreuves de structure n'enregistrent de bornes de citation — ils
    nomment des blocs. Les rejouer en citant le bloc entier reproduit exactement la lecture sous
    laquelle leur signature a été mesurée, et c'est aussi la plus large des deux.
    """
    bloc = corpus.documents[index.doc_of(block_id)].block(block_id)
    return VerifiedQuote(block_id=block_id, quote=bloc.text, start=0, end=len(bloc.text_norm),
                         text_start=0, text_end=len(bloc.text))


def rapport(nom: str) -> dict:
    """Le rapport de gate figé, réduit à un cas et à ses répétitions."""
    return json.loads((DATA / nom).read_text())


def _relire_sans_amorce(clause, *, corpus: Corpus, index: Index):
    """La clause telle que le code la lisait **avant** L1p : sur le texte de son seul bloc.

    L1p relit un item d'énumération avec l'amorce qui le subordonne (`_texte_de_la_clause`). Pour
    rejouer un run antérieur, il faut donc défaire précisément cela — et rien d'autre.
    """
    texte = corpus.documents[index.doc_of(clause.block_id)].block(clause.block_id).text
    return clause.model_copy(update={
        "qualificatifs": list(_mots_qualifiants(texte).values()),
        "renvois": sorted(_mots_renvoi(texte)),
        "qualites_personne": _qualites_de_personne(texte)})


def rejouer(repetition: dict, *, corpus: Corpus, index: Index, faits: str = "",
            avant: frozenset[str] = frozenset()) -> tuple[str, list[ClaimJugee]]:
    """La répétition rejouée : champs canoniques, relectures du code, puis la table AD-6.

    Un `applicable` observé se réécrit sans ambiguïté en champs typés : `oui` = fait requis présent et
    rien d'ouvert, `non` = fait requis absent **sans** fait manquant (la signature du fait connu et
    contraire), `humain` = un fait manquant nommé, `null` = la claim n'a cité aucune clause qui décide
    et la table ne la voit pas. Par-dessus viennent les relectures que le code fait du texte des
    clauses, indépendantes du modèle : elles ne peuvent que fermer un `oui`.

    `faits` porte la **déclaration** du cas, et c'est la troisième de ces relectures (story 5.7,
    L1v) : une exigence que la déclaration ne nomme que sous une négation est contredite, quoi
    qu'ait rendu le modèle. Vide — les rapports antérieurs ne la portaient pas —, la relecture ne
    s'exerce pas et le rejeu est celui d'avant.
    """
    lus_des_faits = _lecture_niee(normalize(faits), fenetre=SETTINGS.negation_fenetre_mots)
    jugees: list[ClaimJugee] = []
    for claim in repetition["claims"]:
        clauses = _clauses_citees([citation_entiere(b, corpus=corpus, index=index)
                                   for b in claim["blocs"]], corpus=corpus, index=index,
                                  settings=SETTINGS)
        if "L1o" in avant:
            # Avant L1o, une amorce d'énumération décidait comme une clause : c'est la seule façon
            # de reproduire une signature mesurée sous un correctif qui la corrige.
            clauses = [clause.model_copy(update={"amorce": False}) for clause in clauses]
        if "L1p" in avant:
            clauses = [_relire_sans_amorce(c, corpus=corpus, index=index) for c in clauses]
        observe = claim["applicable"]
        if claim.get("ferme_par_le_rattachement") and "L1u" not in avant:
            # Story 5.7 (L1u). Ce `oui` n'existait que par la porte du rattachement : la claim
            # rendait un fait manquant — l'exigence que sa clause écrit —, et le rattachement le
            # fermait. La porte est retirée ; la claim retombe donc sur ce que le **modèle** a
            # rendu, une exigence restée ouverte. Le rapport figé porte le marqueur, jamais le
            # code : c'est la lecture de la mesure, et `avant={"L1u"}` rejoue la lecture du run.
            observe = "humain"
        if "L1v" not in avant and _exigence_niee_par_la_declaration(
                (mot for clause in clauses if not clause.amorce for mot in clause.exigences),
                lus_des_faits, min_chars=SETTINGS.qualite_mot_min_chars):
            # L1v : la déclaration nie tout ce que la clause affirme — le fait exigé est connu et
            # contraire, et le booléen du modèle n'y change rien. `avant={"L1v"}` rejoue la lecture
            # du run, où le code ne lisait pas la déclaration.
            observe = "non"
        exigees = _qualites_de_la_clause(clauses, nommees="", place=8) if observe != "non" else []
        renvois = {r for clause in clauses if clause.kind == "garantie" for r in clause.renvois}
        jugees.append(ClaimJugee(
            claim_id=claim["claim_id"], clauses=clauses, retenue=True,
            champs=ChampsApplicabilite(
                fait_requis_present=observe == "oui",
                option_requise=bool(renvois & RENVOIS_OPTION), cp_requise=bool(renvois & RENVOIS_CP),
                fait_manquant=None if observe != "humain" else "fait exigé par la clause citée",
                qualites_exigees=exigees, qualites_non_etablies=exigees)))
    return decider(jugees, ask_client_max=6).value, jugees


def verdicts(rapport_: dict, *, corpus: Corpus, index: Index,
             avant: frozenset[str] = frozenset()) -> list[str]:
    """Les verdicts rejoués, dans l'ordre des répétitions du rapport.

    La déclaration du cas est lue dans le rapport figé (`faits`) quand il la porte : c'est ce que
    L1v relit, et les rapports d'avant ne l'enregistraient pas.
    """
    return [rejouer(r, corpus=corpus, index=index, faits=rapport_.get("faits", ""), avant=avant)[0]
            for r in rapport_["repetitions"]]


def verdicts_observes(rapport_: dict) -> list[str]:
    return [r["verdict_observe"] for r in rapport_["repetitions"]]
