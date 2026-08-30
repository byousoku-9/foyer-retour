"""Story 4.2b — Gardes métamorphiques : les décisions ne dépendent d'aucun identifiant.

Un système juste décide depuis les **champs typés** — kind, portée, socle, applicabilité — jamais
depuis un `block_id`, un `doc_id`, une page ou un ordre de lecture. Ces tests construisent des
corpus **synthétiques**, sans vocabulaire AXA/Baloise, puis les **permutent** (bijection des
identifiants, mélange de l'ordre des claims, renumérotation des pages) : les décisions doivent
rester identiques. Une décision qui bouge sous permutation prouve un branchement sur l'identité —
la définition même d'une rustine, vue depuis l'autre côté.
"""

from __future__ import annotations

from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain import Block, BlockRef, Document, Node, NodeRef
from server.app.domain.question import ParsedQuestion, QuestionScope
from server.app.domain.verdict import (ChampsApplicabilite, ClaimJugee, ClauseCitee,
                                       applicabilites_des_claims, decider)
from server.evals.plancher import charger_plancher as _charger_plancher_4_5
from server.evals.run import Cas as _Cas4_5
from server.evals.run import Resultat as _Resultat4_5
from server.evals.run import construire_decisions as _decisions4_5
from server.evals.run import quote_hash

# Vocabulaire volontairement neutre : aucun assureur réel, aucun cas du golden set.
SOCLE = "n-socle"


def _clause(kind: str, *, block_id: str, node_id: str = SOCLE, socle: bool = True,
            portee: set[str] | None = None, qualificatifs: list[str] | None = None) -> ClauseCitee:
    return ClauseCitee(block_id=block_id, kind=kind, kind_confirmed=True,
                       portee={node_id} if portee is None else portee, node_id=node_id,
                       socle=socle, qualificatifs=qualificatifs or [])


def _champs(present: bool = True, *, option: bool = False, cp: bool = False,
            manquant: str | None = None) -> ChampsApplicabilite:
    return ChampsApplicabilite(fait_requis_present=present, option_requise=option, cp_requise=cp,
                               fait_manquant=manquant)


def _corpus_synthetique() -> list[ClaimJugee]:
    """Un dossier synthétique : garantie socle établie, exclusion hors portée, condition ouverte."""
    return [
        ClaimJugee(claim_id="c-garantie",
                   clauses=[_clause("garantie", block_id="doc-neutre:p3:1")],
                   champs=_champs(True)),
        ClaimJugee(claim_id="c-exclusion",
                   clauses=[_clause("exclusion", block_id="doc-neutre:p7:2",
                                    node_id="n-annexe", socle=False, portee={"n-annexe"})],
                   champs=_champs(False)),
        ClaimJugee(claim_id="c-condition",
                   clauses=[_clause("condition", block_id="doc-neutre:p9:4")],
                   champs=_champs(False, manquant="date exacte de l'evenement")),
    ]


def _permuter(claims: list[ClaimJugee], *, prefixe: str, pages: int) -> list[ClaimJugee]:
    """Bijection des identifiants + renumérotation des pages + inversion de l'ordre de lecture."""
    def _id(block_id: str) -> str:
        doc, loc, seq = block_id.split(":")
        page = int(loc.removeprefix("p")) + pages
        return f"{prefixe}:{f'p{page}'}:{int(seq) + 5}"

    def _noeud(node_id: str) -> str:
        return f"{prefixe}-{node_id}"

    permutees = [
        claim.model_copy(update={
            "claim_id": f"{prefixe}-{claim.claim_id}",
            "clauses": [clause.model_copy(update={
                "block_id": _id(clause.block_id),
                "node_id": _noeud(clause.node_id),
                "portee": {_noeud(n) for n in clause.portee},
            }) for clause in claim.clauses],
        })
        for claim in claims
    ]
    return list(reversed(permutees))


def test_le_verdict_est_invariant_sous_permutation_des_identifiants() -> None:
    """AC 4.2b : corpus synthétiques permutés ⇒ décisions identiques."""
    original = _corpus_synthetique()
    permute = _permuter(_corpus_synthetique(), prefixe="autre", pages=40)
    v1 = decider(original, ask_client_max=8)
    v2 = decider(permute, ask_client_max=8)
    assert v1.value == v2.value == "sous_conditions"
    # Les questions au client sont composées depuis les libellés typés, pas depuis les ids :
    # mêmes libellés, même nombre, quel que soit l'ordre de lecture.
    assert sorted(v1.ask_client) == sorted(v2.ask_client)


def test_lapplicabilite_est_invariante_sous_permutation() -> None:
    original = _corpus_synthetique()
    permute = _permuter(_corpus_synthetique(), prefixe="miroir", pages=11)
    statuts_1 = sorted((a or "", r or "") for a, r in applicabilites_des_claims(original).values())
    statuts_2 = sorted((a or "", r or "") for a, r in applicabilites_des_claims(permute).values())
    assert statuts_1 == statuts_2


def test_decision_et_applicabilite_resistent_aux_reformulations() -> None:
    """AC 4.2b : synonymes et ordre des mots ne changent pas les champs typés décisionnels."""
    original = _corpus_synthetique()
    reformule = [
        claim.model_copy(update={
            "champs": claim.champs.model_copy(update={
                "fait_manquant": ("moment précis du fait"
                                   if claim.champs.fait_manquant else None),
            })
        })
        for claim in reversed(original)
    ]
    assert decider(original, ask_client_max=8).value == decider(
        reformule, ask_client_max=8).value
    statuts_original = sorted((a or "", r or "")
                              for a, r in applicabilites_des_claims(original).values())
    statuts_reformules = sorted((a or "", r or "")
                                for a, r in applicabilites_des_claims(reformule).values())
    assert statuts_original == statuts_reformules


def test_controle_negatif_un_decideur_branche_sur_block_id_est_detecte() -> None:
    """Le contrôle prouve que la permutation ferait bien rougir un branchement interdit."""
    original = _corpus_synthetique()
    permute = _permuter(_corpus_synthetique(), prefixe="miroir", pages=11)

    def mauvais_decideur(claims: list[ClaimJugee]) -> bool:
        return any(clause.block_id == "doc-neutre:p3:1"
                   for claim in claims for clause in claim.clauses)

    assert mauvais_decideur(original) is True
    assert mauvais_decideur(permute) is False


def _index_multifacette(prefixe: str, *, inverse: bool) -> tuple[Index, ParsedQuestion]:
    textes = [
        "Thème de profil général.",
        "Thème de profil général répété.",
        "Première démarche avec son justificatif.",
        "Seconde démarche avec son échéance.",
        "Sujet ajouté sans rapport.",
    ]
    blocs = [Block(block_id=f"{prefixe}:p{i + 10}:{i + 1}", text=texte,
                   loc=f"p{i + 10}", seq=i + 1)
             for i, texte in enumerate(textes)]
    ordre = list(reversed(blocs)) if inverse else blocs
    nodes = [Node(node_id=f"{prefixe}-root",
                  items=[NodeRef(node_id=f"{prefixe}-n{i}") for i in range(len(ordre))])]
    nodes.extend(Node(node_id=f"{prefixe}-n{i}", items=[BlockRef(block_id=bloc.block_id)])
                 for i, bloc in enumerate(ordre))
    document = Document(doc_id=prefixe, kind="guide", title="Neutre", edition="e",
                        nodes=nodes, blocks=blocs)
    parsed = ParsedQuestion(
        question_resolue="Comparer deux démarches.", intent="question",
        terms=["première démarche", "seconde démarche"],
        scope=QuestionScope(themes=["thème de profil", "sujet ajouté"]),
        facettes=["première démarche", "seconde démarche"],
    )
    return Index(Corpus(documents={prefixe: document})), parsed


def test_la_diversification_multifacette_est_invariante_aux_ids_pages_ordre_et_theme_etranger() -> None:
    original, question_1 = _index_multifacette("doc-neutre", inverse=False)
    permute, question_2 = _index_multifacette("miroir-neutre", inverse=True)

    def passages(index: Index, parsed: ParsedQuestion) -> list[str]:
        hits = index.chercher(parsed.termes_de_recherche(), limit=2,
                              groupes_prioritaires=parsed.facettes)
        return [index.corpus.documents[index.doc_of(block_id)].block(block_id).text
                for block_id, _node_id in hits]

    attendu = ["Première démarche avec son justificatif.",
               "Seconde démarche avec son échéance."]
    assert passages(original, question_1) == passages(permute, question_2) == attendu


def test_controle_negatif_la_coupe_globale_sans_facettes_perd_les_deux_passages_utiles() -> None:
    original, question_1 = _index_multifacette("doc-neutre", inverse=False)
    permute, question_2 = _index_multifacette("miroir-neutre", inverse=True)

    def mauvais_selecteur(index: Index, parsed: ParsedQuestion) -> list[str]:
        return [index.corpus.documents[index.doc_of(block_id)].block(block_id).text
                for block_id, _node_id in index.chercher(parsed.termes_de_recherche(), limit=2)]

    attendus = {"Première démarche avec son justificatif.",
                "Seconde démarche avec son échéance."}
    assert attendus.isdisjoint(mauvais_selecteur(original, question_1))
    assert attendus.isdisjoint(mauvais_selecteur(permute, question_2))


def test_une_exclusion_applicable_reste_applicable_sous_permutation() -> None:
    """La branche `non_couvert` aussi : l'exclusion qui couvre le cas le couvre sous tout renommage."""
    def _dossier(prefixe: str) -> list[ClaimJugee]:
        return [
            ClaimJugee(claim_id=f"{prefixe}-g",
                       clauses=[_clause("garantie", block_id=f"{prefixe}:p1:1",
                                        node_id=f"{prefixe}-n1")],
                       champs=_champs(True)),
            ClaimJugee(claim_id=f"{prefixe}-x",
                       clauses=[_clause("exclusion", block_id=f"{prefixe}:p2:1",
                                        node_id=f"{prefixe}-n1", portee={f"{prefixe}-n1"})],
                       champs=_champs(True)),
        ]

    v1 = decider(_dossier("un"), ask_client_max=8)
    v2 = decider(list(reversed(_dossier("deux"))), ask_client_max=8)
    assert v1.value == v2.value == "non_couvert"


def test_quote_hash_est_invariant_sous_renommage_du_corpus() -> None:
    """La preuve de stabilité (`quote_hash`) ne dépend que du texte relu, jamais des ids.

    Deux corpus permutés qui portent la même clause au même texte produisent la même empreinte :
    l'agrégat de stabilité compare des passages, pas des positions.
    """
    texte = normalize("Le degat cause par un evenement soudain au batiment assure est couvert.")
    assert quote_hash(texte) == quote_hash(texte)
    assert quote_hash(texte) != quote_hash(texte + " autre phrase")


# --- B3 (corrective 4.2a) : la règle de frontière ne dépend d'aucun triplet particulier ----------
# La frontière objet/applicabilité — une règle conditionnelle soutenue est jugée sur ses champs
# typés : qualité exigée non établie ⇒ `humain`, périmètre connu et contraire ⇒ `non` — doit rendre
# la même décision quels que soient l'objet assuré, la condition exigée, l'ordre de lecture et la
# formulation du libellé. Un exemple de prompt n'est jamais la règle : si la décision bougeait d'un
# triplet à l'autre, elle dépendrait d'un vocabulaire, pas des champs.
TRIPLETS_FRONTIERE = [
    ("appareil menager", "brutal"),
    ("cloture du jardin", "imprevisible"),
    ("velo range a la cave", "avec effraction"),
]


def _dossier_qualite_manquante(objet: str, qualite: str) -> list[ClaimJugee]:
    """Une règle conditionnelle soutenue dont la qualité exigée n'est pas établie par les faits."""
    return [
        ClaimJugee(claim_id=f"c-{normalize(objet).replace(' ', '-')}",
                   clauses=[_clause("garantie", block_id="doc-neutre:p2:1",
                                    qualificatifs=[qualite])],
                   champs=ChampsApplicabilite(fait_requis_present=True,
                                              qualites_exigees=[qualite],
                                              qualites_non_etablies=[qualite])),
    ]


def _dossier_perimetre_contraire(objet: str) -> list[ClaimJugee]:
    """Une clause au périmètre connu et contraire : `non` par le code, jamais une omission."""
    return [
        ClaimJugee(claim_id=f"c-{normalize(objet).replace(' ', '-')}",
                   clauses=[_clause("garantie", block_id="doc-neutre:p2:1")],
                   champs=_champs(False)),
    ]


def test_la_qualite_manquante_rend_humain_quel_que_soit_le_triplet() -> None:
    """Objet, condition et formulation varient ; la frontière rend toujours `humain`."""
    decisions = set()
    for objet, qualite in TRIPLETS_FRONTIERE:
        dossier = _dossier_qualite_manquante(objet, qualite)
        statuts = [a for a, _r in applicabilites_des_claims(dossier).values()]
        assert statuts == ["humain"], (objet, qualite)
        decisions.add(decider(dossier, ask_client_max=8).value)
    assert decisions == {"ne_tranche_pas"}


def test_la_qualite_manquante_est_invariante_sous_permutation_et_reformulation() -> None:
    """Renommage des ids, renumérotation des pages, ordre inversé, libellé reformulé : rien ne bouge."""
    for objet, qualite in TRIPLETS_FRONTIERE:
        original = _dossier_qualite_manquante(objet, qualite)
        permute = _permuter(_dossier_qualite_manquante(objet, qualite), prefixe="autre", pages=17)
        reformule = [
            claim.model_copy(update={"champs": claim.champs.model_copy(update={
                "qualites_exigees": [f"caractere {qualite} de l'evenement"],
                "qualites_non_etablies": [f"caractere {qualite} de l'evenement"],
            })})
            for claim in _dossier_qualite_manquante(objet, qualite)
        ]
        for variante in (original, permute, reformule):
            assert [a for a, _r in applicabilites_des_claims(variante).values()] == ["humain"]
            assert decider(variante, ask_client_max=8).value == "ne_tranche_pas"


def test_le_perimetre_contraire_rend_non_quel_que_soit_le_triplet() -> None:
    """Le périmètre connu et contraire vaut `non` calculé — jamais une omission — pour tout objet."""
    for objet, _qualite in TRIPLETS_FRONTIERE:
        original = _dossier_perimetre_contraire(objet)
        permute = _permuter(_dossier_perimetre_contraire(objet), prefixe="miroir", pages=23)
        for variante in (original, permute):
            assert [a for a, _r in applicabilites_des_claims(variante).values()] == ["non"]
            assert decider(variante, ask_client_max=8).value == "ne_tranche_pas"


def test_le_vocabulaire_des_corpus_synthetiques_est_neutre() -> None:
    """Never 4.2b : pas de vocabulaire d'assureur réel dans les corpus synthétiques permutés."""
    import inspect
    source = (inspect.getsource(_corpus_synthetique) + inspect.getsource(_permuter)
              + inspect.getsource(_clause) + inspect.getsource(_champs)
              + inspect.getsource(_dossier_qualite_manquante)
              + inspect.getsource(_dossier_perimetre_contraire)
              + repr(TRIPLETS_FRONTIERE)).lower()
    for interdit in ("axa", "baloise", "optihome", "s-bougie", "p34:12",
                     "bougie", "canape", "mobilier", "soudain", "chaleur"):
        assert interdit not in source, \
            f"vocabulaire non neutre dans les corpus synthétiques : {interdit!r}"


# --- story 4.5 : les **décisions de gate** ne dépendent d'aucun identifiant ------------------------
#
# La garde métamorphique gardait les verdicts du domaine ; la story 4.5 lui donne neuf témoins de
# plus à garder — parsing, blocs ouverts, citations, 5xx, typage, structure, stabilité de la claim
# décisionnelle et les deux gardes de dépôt. Une garde anti-rustine s'étend avec la surface qu'elle
# garde : sans cela, un branchement sur un `block_id` **dans le calcul d'un témoin** passerait
# exactement là où la story prétend fermer la porte.
#
# Les corpus restent synthétiques et neutres, et la permutation est la même que ci-dessus :
# bijection des identifiants, renumérotation des pages, inversion de l'ordre, reformulation.

DOC_NEUTRE_4_5 = "doc-neutre"


def _cas_neutre_4_5(doc: str) -> list[_Cas4_5]:
    sinistre = _Cas4_5.model_validate({
        "id": "s-neutre", "suite": "sinistre", "profile": "vertical",
        "question": "Cette situation entre-t-elle dans la garantie decrite ?",
        "faits": {"description": "Un bien decrit au contrat a subi une atteinte."},
        "expected": {"found": True, "block_ids": [f"{doc}:p3:1"],
                     "verdict": ["sous_conditions", "ne_tranche_pas"]},
        "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
        "mode_attendu": "bonne_reponse",
    })
    parsing = _Cas4_5.model_validate({
        "id": "p-neutre", "suite": "parsing", "profile": "full",
        "question": "Le texte du bloc est-il celui du document imprime ?",
        "famille": "garantie",
        "expected": {"found": True, "block_ids": [f"{doc}:p3:1"],
                     "text_norm": normalize("Le bien decrit est garanti selon les conditions.")},
        "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
        "mode_attendu": "bonne_reponse",
    })
    parsing._doc_id = doc
    return [sinistre, parsing]


def _resultats_neutres_4_5(doc: str, *, prefixe: str = "") -> list[_Resultat4_5]:
    bloc = f"{doc}:p3:1"
    sinistres = [
        _Resultat4_5(
            id="s-neutre", suite="sinistre", label="bonne_reponse", variant="outils",
            repetition=r, doc_id=doc, found=True, verdict="sous_conditions", http=200,
            expected_block_ids=[bloc], opened_block_ids=[bloc],
            proofs=[{"doc_id": doc, "block_id": bloc, "kind": "garantie",
                     "quote_hash": f"{prefixe}h", "kind_confirmed": True}],
            decision_claim=True)
        for r in (1, 2, 3)
    ]
    parsings = [
        _Resultat4_5(id="p-neutre", suite="parsing", label="bonne_reponse", variant="local",
                     repetition=r, doc_id=doc, found=True, http=None,
                     expected_block_ids=[bloc])
        for r in (1, 2, 3)
    ]
    return sinistres + parsings


def _table_de_decisions_4_5(doc: str, *, prefixe: str = "",
                            ordre_inverse: bool = False) -> dict[str, tuple[float, str, int]]:
    cas = _cas_neutre_4_5(doc)
    resultats = _resultats_neutres_4_5(doc, prefixe=prefixe)
    if ordre_inverse:
        cas = list(reversed(cas))
        resultats = list(reversed(resultats))
    decisions = _decisions4_5(
        resultats, cas, plancher=_charger_plancher_4_5(), repeat=3, run_digest="a" * 64,
        producer="orchestrator", exigences_full=True, structure=(1, 1))
    return {d.metric: (d.value, d.status, d.n) for d in decisions}


def test_les_decisions_de_gate_sont_invariantes_sous_permutation() -> None:
    """AC 4.5 : les neuf témoins ne dépendent ni d'un `doc_id`, ni d'une page, ni d'un ordre.

    Un témoin qui bougerait sous renommage prouverait un branchement sur l'identité — la définition
    d'une rustine, vue depuis le côté de ce qui mesure.
    """
    original = _table_de_decisions_4_5(DOC_NEUTRE_4_5)
    permute = _table_de_decisions_4_5("autre-doc-neutre", prefixe="z", ordre_inverse=True)
    assert original == permute
    # Et les neuf témoins de la story sont bien dans la table gardée.
    assert {"parsing_ok_rate", "blocs_attendus_ouverts_rate", "citations_retrouvees_rate",
            "zero_5xx_technique_rate", "typage_confirme_rate", "structure_prouvee_rate",
            "stabilite_claim_decisionnelle", "anti_rustine_pass_rate",
            "metamorphique_pass_rate"} <= set(original)


def test_controle_negatif_un_temoin_branche_sur_un_identifiant_est_detecte() -> None:
    """Le contrôle prouve que la permutation ferait rougir un témoin branché sur l'identité.

    Sans lui, l'invariance ci-dessus pourrait tenir parce que rien n'est mesuré — un test vert qui
    ne protège rien.
    """
    def mauvais_temoin(resultats: list[_Resultat4_5]) -> float:
        cibles = [r for r in resultats
                  if any(p["block_id"] == f"{DOC_NEUTRE_4_5}:p3:1" for p in r.proofs)]
        return len(cibles) / max(1, len(resultats))

    assert mauvais_temoin(_resultats_neutres_4_5(DOC_NEUTRE_4_5)) > 0.0
    assert mauvais_temoin(_resultats_neutres_4_5("autre-doc-neutre")) == 0.0


def test_le_vocabulaire_des_corpus_de_gate_est_neutre() -> None:
    """Never : aucun identifiant, aucune formulation et aucune page d'un témoin réel."""
    import inspect
    source = (inspect.getsource(_cas_neutre_4_5) + inspect.getsource(_resultats_neutres_4_5)
              + inspect.getsource(_table_de_decisions_4_5)).lower()
    for interdit in ("axa", "baloise", "optihome", "s-bougie", "b-bougie", "p34:12",
                     "bougie", "canape", "mobilier", "soudain", "chaleur", "congelateur"):
        assert interdit not in source, \
            f"vocabulaire non neutre dans les corpus de gate : {interdit!r}"
