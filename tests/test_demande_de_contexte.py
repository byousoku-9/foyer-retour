"""Story 4.2e — un tour sémantique mesuré demande explicitement le contexte qui lui manque.

Toute la matrice I/O de la spec, jouée de bout en bout sur un **corpus synthétique neutre** : demande
valide satisfaite, catégorie hors vocabulaire, cible inconnue, demande insatisfaite, budget épuisé,
seconde demande, reprise non dominante, chemin normal inchangé. Rien du golden set n'entre ici — ni
assureur, ni objet, ni formulation de question-témoin : le vocabulaire du texte est un vocabulaire
de registre, et une garde de neutralité le vérifie à la fin du fichier.

Deux propriétés dépassent la matrice, et ce sont elles qui interdisent la rustine :

1. **l'identité du budget** — la passe de satisfaction reçoit *littéralement le même objet*
   `RetrievalBudget` que la passe initiale (`is`, jamais `==`). AD-1 borne l'étape, pas la passe :
   une seconde borne de même valeur rendrait le rappel deux fois plus large sans que rien ne bouge
   dans la trace ;
2. **l'invariance sous permutation** — bijection des `doc_id`/`block_id`/`claim_id`, décalage des
   pages, inversion de l'ordre de lecture déclaré, reformulation des libellés. Les décisions doivent
   être identiques ; le contrôle négatif prouve que la permutation ferait bien rougir un branchement
   sur un identifiant.

`FakeAnthropic` lève sur tout appel non scripté : **la longueur du script est une assertion**. C'est
ainsi que « une satisfaction, une reprise, jamais deux » se vérifie sans compter les appels.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, NamedTuple

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.answer import DemandeContexte
from server.app.domain.document import Document, Node
from server.app.domain.ingest import ManifestEntry
from server.app.domain.question import Faits
from server.app.domain.retrieval import RetrievalResult
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.pipelines import sinistre
from server.app.pipelines.commun import retrieval_budget
from server.app.steps.restituer import PHRASES_DE_LACUNE
from server.app.steps.retrouver import satisfaire_demande
from tests.llm_fake import FakeAnthropic, fake_message

# --- le corpus neutre, et ce qu'une permutation a le droit de déplacer ----------------------------

QUESTION = "Le dossier décrit relève-t-il de la prise en charge prévue ?"
QUESTION_RESOLUE = "Le dossier décrit relève-t-il de la prise en charge prévue ?"
FAITS = Faits(date="2030-01-02", lieu="local déclaré", montant_eur=100.0,
              description="Le dossier décrit un épisode répertorié ayant atteint la structure "
                          "porteuse dans le local déclaré ; aucun élément nominatif n'est joint.")
# Les termes cherchés ne figurent **que** dans la section ouverte : c'est ce qui laisse l'annexe
# fermée à la première passe, donc ce qui rend la demande de contexte observable.
TERMES = ["structure porteuse", "épisode répertorié"]

# `(clé, kind, texte, defines, refs)` — la **clé** est le rôle du bloc, la seule chose que les
# assertions comparent d'un corpus permuté à l'autre. Aucun mot du lexique fermé des qualificatifs
# (`steps/verifier.QUALIFICATIFS`) : ces preuves portent sur la demande de contexte, pas sur AD-6.
SECTION_LUE = (
    ("titre", "heading", "Titre du chapitre", None, ()),
    ("prise_en_charge", "garantie",
     "Les dommages atteignant l'objet inventorié et la structure porteuse lors d'un épisode "
     "répertorié sont pris en charge.", None, ()),
    ("inscription", "condition",
     "La prise en charge n'est acquise que si le local reste inscrit au registre déclaré.",
     None, ()),
)
# L'annexe n'a **aucun** hit pour les termes cherchés : la première passe ne l'ouvre pas. Ses deux
# définitions sont communes (aucune portée déclarée), donc valides partout — c'est ce qui permet à
# la satisfaction de les résoudre depuis la section lue (AD-2, remontée vers la commune).
SECTION_FERMEE = (
    # **Deux** renvois, et c'est délibéré (revue externe, I1) : ce bloc entre à la première passe
    # comme *dépendance*, et la fermeture d'un niveau ne suit pas les `refs` d'une dépendance — ses
    # deux cibles restent donc fermées, et une demande de `renvoi` qui le vise a exactement deux
    # candidats. C'est ce qu'il faut pour exhiber une satisfaction **partielle** sous le budget.
    ("definition_objet", "definition",
     "L'objet inventorié désigne tout élément porté au registre annexé.",
     "objet inventorié", ("bareme", "annexe_technique")),
    ("definition_valeur", "definition",
     "La valeur résiduelle désigne le montant retenu après application du barème annexé.",
     "valeur résiduelle", ()),
    # Revue 4.2e (C) : ce terme-ci est employé par `definition_objet`, qui entre à la première passe
    # comme dépendance de la clause lue. `Index.definitions(..., blocs_ouverts=…)` rend donc cette
    # définition pour **n'importe quel** terme demandé, par sa branche « rencontré dans un bloc
    # ouvert » — c'est le piège que la satisfaction doit refuser de prendre pour une réponse.
    ("definition_registre", "definition",
     "Le registre annexé recense les éléments couverts par le texte applicable.",
     "registre annexé", ()),
    ("bareme", "para",
     "Le barème annexé fixe le montant retenu pour chaque élément porté au registre.", None, ()),
    ("annexe_technique", "para",
     "L'annexe technique précise le classement retenu pour chaque élément porté au registre.",
     None, ()),
)

# Citation contiguë de la clause lue : plus longue que `quote_min_chars`, présente dans ce seul bloc.
CITATION = "atteignant l'objet inventorié et la structure porteuse"
# Le terme que la demande `definition` vise. Il n'est **pas** dans le corpus lu : il n'entre dans
# l'entrée du modèle que par le texte de l'affirmation rédigée, ce qui est exactement l'univers que
# le code accepte pour une cible `definition`.
TERME_DEMANDE = "valeur résiduelle"
# Un terme que l'entrée du modèle porte et que **rien** ne définit au contrat : une demande qui le
# vise est bien formée et doit rester insatisfaite (revue 4.2e, C).
TERME_SANS_DEFINITION = "montant déclaré"
AFFIRMATION = ("Le texte pris en charge vise l'objet inventorié et fixe la valeur résiduelle "
               "retenue au montant déclaré.")
QUALITE_DEMANDEE = "valeur résiduelle retenue"


class Identite(NamedTuple):
    """Tout ce qu'une permutation déplace : document, pages, `seq`, nœuds, claims, ordre de lecture.

    Rien de ce que cette structure porte n'est censé peser sur une décision — c'est l'hypothèse que
    la garde métamorphique met à l'épreuve.
    """

    doc_id: str
    page_lue: int
    page_fermee: int
    seq_depart: int
    lue: str
    fermee: str
    racine: str
    claim: str
    termes_inverses: bool = False
    ordre_lecture_inverse: bool = False

    def noeud(self, nom: str) -> str:
        return f"{self.doc_id}:{nom}"

    def termes(self) -> list[str]:
        return list(reversed(TERMES)) if self.termes_inverses else list(TERMES)


IDENTITE = Identite(doc_id="texte-neutre-e", page_lue=1, page_fermee=2, seq_depart=1,
                    lue="e1", fermee="e2", racine="e0", claim="k1")
# Permutation de pure identité : bijection du `doc_id`, renumérotation des pages, décalage des
# `seq`, renommage des nœuds et de l'affirmation, inversion de l'ordre des termes **et** de l'ordre
# de lecture déclaré. Aucune décision ne doit bouger.
IDENTITE_PERMUTEE = Identite(doc_id="texte-neutre-f", page_lue=9, page_fermee=10, seq_depart=21,
                             lue="w7", fermee="w8", racine="w6", claim="z9",
                             termes_inverses=True, ordre_lecture_inverse=True)


class CorpusNeutre(NamedTuple):
    index: Index
    identite: Identite
    par_cle: dict[str, str]

    def bloc(self, cle: str) -> str:
        return self.par_cle[cle]

    def cles(self, block_ids: Iterable[str]) -> list[str]:
        inverse = {block_id: cle for cle, block_id in self.par_cle.items()}
        return [inverse[block_id] for block_id in block_ids]


def _corpus_neutre(identite: Identite) -> CorpusNeutre:
    par_cle: dict[str, str] = {}
    blocs: list[dict[str, Any]] = []
    noeuds: list[Node] = []
    sections = ((identite.lue, identite.page_lue, SECTION_LUE),
                (identite.fermee, identite.page_fermee, SECTION_FERMEE))
    # Deux passes : les identifiants d'abord (un `refs` peut viser un bloc décrit plus loin).
    for nom, page, clauses in sections:
        for rang, (cle, _kind, _texte, _defines, _refs) in enumerate(clauses):
            par_cle[cle] = f"{identite.doc_id}:p{page}:{identite.seq_depart + rang}"
    for nom, page, clauses in sections:
        items: list[dict[str, str]] = []
        for rang, (cle, kind, texte, defines, refs) in enumerate(clauses):
            bloc: dict[str, Any] = {"block_id": par_cle[cle], "loc": f"p{page}",
                                    "seq": identite.seq_depart + rang, "kind": kind, "text": texte}
            if kind != "heading":
                bloc["kind_source"] = "manual"
            # Les clauses de la section lue portent leur portée ; les définitions de l'annexe sont
            # **communes** (aucune portée), donc résolubles depuis n'importe quelle section (AD-2).
            if kind not in ("heading", "definition"):
                bloc["scope_node_id"] = identite.noeud(nom)
            if defines is not None:
                bloc["defines"] = defines
            if refs:
                bloc["refs"] = [par_cle[r] for r in refs]
            blocs.append(bloc)
            items.append({"block_id": par_cle[cle]})
        if identite.ordre_lecture_inverse:
            items.reverse()
        noeuds.append(Node(node_id=identite.noeud(nom), level=1, title=f"Section {nom}",
                           items=items))
    branches = [{"node_id": identite.noeud(identite.lue)},
                {"node_id": identite.noeud(identite.fermee)}]
    if identite.ordre_lecture_inverse:
        branches.reverse()
    noeuds.append(Node(node_id=identite.noeud(identite.racine), level=0, title="Texte applicable",
                       items=branches))
    document = Document(doc_id=identite.doc_id, kind="contrat", title="Texte neutre",
                        edition="2030", nodes=noeuds, blocks=blocs)
    for b in document.blocks:
        b.text_norm = normalize(b.text)
    manifest = {identite.doc_id: ManifestEntry(
        status="servi", source_hash=f"sha-{identite.doc_id}",
        ingest_fingerprint=f"fp-{identite.doc_id}", document_hash="sha-doc", edition="2030")}
    sommaire = "\n".join([f"# {document.title}",
                          f"- {identite.lue} Section {identite.lue}",
                          f"- {identite.fermee} Section {identite.fermee}"])
    return CorpusNeutre(
        index=Index(Corpus(documents={identite.doc_id: document}, manifest=manifest,
                           summaries={identite.doc_id: sommaire})),
        identite=identite, par_cle=par_cle)


@pytest.fixture
def neutre() -> CorpusNeutre:
    return _corpus_neutre(IDENTITE)


# --- scripts : la sortie de chaque étape, sans jamais toucher au réseau ---------------------------

def _settings(identite: Identite, **kw) -> Settings:
    kw.setdefault("sinistre_doc_id", identite.doc_id)
    return Settings(_env_file=None, anthropic_api_key="", **kw)


class _BudgetQuiSepuise(RequestBudget):
    """Budget dont le temps restant tombe sous ce qu'un appel demande, après le n-ième appel."""

    def __init__(self, apres_appels: int) -> None:
        super().__init__(deadline_s=100.0, max_attempts=8, max_cost_eur=0.30)
        self._restants = apres_appels

    def note_call(self, usage) -> None:
        super().note_call(usage)
        self._restants -= 1

    def remaining(self) -> float:
        return 100.0 if self._restants > 0 else 1.0


def _budget(*, max_attempts: int = 6, deadline_s: float = 100.0) -> RequestBudget:
    return RequestBudget(deadline_s=deadline_s, max_attempts=max_attempts, max_cost_eur=0.20)


def _comprendre(corpus: CorpusNeutre) -> dict:
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "intent": "question", "question_resolue": QUESTION_RESOLUE, "clarification": None,
        "language": "fr", "terms": corpus.identite.termes(), "themes": [],
        "facettes": ["prise en charge"], "bien": "objet inventorié",
        "evenement": "épisode répertorié", "lieu": "local déclaré", "cause": "agent externe",
        "moment": "période déclarée"}))


def _rediger(corpus: CorpusNeutre) -> dict:
    cid = corpus.identite.claim
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": [{"text": AFFIRMATION, "kind": "factuel", "claim_ids": [cid]}],
        "claims": [{"claim_id": cid, "text": AFFIRMATION,
                    "quotes": [{"block_id": corpus.bloc("prise_en_charge"), "quote": CITATION}]}]}))


def _verifier(corpus: CorpusNeutre, *, demande: dict | None = None, pertinente: bool = True,
              qualites: list[str] | None = None) -> dict:
    """La sortie sinistre de *vérifier*, avec ou sans demande de contexte.

    Les deux listes de qualités sont **fidèles** au texte de la clause : il n'emploie aucun mot du
    lexique fermé, donc le code n'a rien à ajouter (revue Codex 1.8, B3, tour 3).
    """
    cid = corpus.identite.claim
    charge: dict[str, Any] = {
        "verdicts": [{"claim_id": cid, "pertinente": pertinente,
                      "raison": None if pertinente else "non_soutenue"}],
        "facettes": [{"facette": 0, "claim_ids": [cid] if pertinente else []}],
        "segments": [{"segment": 0, "soutenu": pertinente}],
        "applicabilite": [{"claim_id": cid, "fait_requis_present": True, "option_requise": False,
                           "cp_requise": False, "fait_manquant": None,
                           "qualites_exigees": qualites if qualites is not None else [],
                           "qualites_etablies": []}],
    }
    if demande is not None:
        charge["demande_contexte"] = demande
    return fake_message(model=TIERS["reason"], text=json.dumps(charge))


def _demande(corpus: CorpusNeutre, kind: str, cible: str, *,
             raison: str = "definition_manquante", claim_id: str | None = None) -> dict:
    return {"kind": kind, "cible": cible, "raison": raison,
            "claim_id": corpus.identite.claim if claim_id is None else claim_id}


async def _run(corpus: CorpusNeutre, script: list, *, settings: Settings | None = None,
               budget: RequestBudget | None = None):
    reglages = settings or _settings(corpus.identite)
    fake = FakeAnthropic(script)
    client = LlmClient(reglages, anthropic_client=fake)
    answer, trace = await sinistre.run(
        corpus.identite.doc_id, QUESTION, FAITS, corpus=corpus.index.corpus, index=corpus.index,
        client=client, settings=reglages, request_id="req-4-2e", budget=budget or _budget(),
        variant="deterministe")
    return answer, trace, fake


def _checks(trace, etape: str) -> list[str]:
    return [c.name for step in trace.steps if step.name == etape for c in step.checks]


def _tous_les_checks(trace) -> list[str]:
    return [c.name for step in trace.steps for c in step.checks]


def _lacune(langue: str = "fr") -> str:
    patron = PHRASES_DE_LACUNE[langue]["contexte_non_relu"]
    assert isinstance(patron, str)
    return patron


# --- 1. la première passe laisse bien l'annexe fermée ---------------------------------------------

async def test_la_premiere_passe_ne_lit_pas_lannexe_mais_sa_definition_rencontree(
        neutre: CorpusNeutre) -> None:
    """Prémisse de tout le fichier : sans demande, le contexte visé n'est **pas** dans l'entrée.

    Sans cette propriété, « la demande a rouvert quelque chose » ne prouverait rien : la définition
    aurait été là de toute façon. La définition du terme *rencontré dans la clause lue* entre, elle,
    par la fermeture d'un niveau déjà écrite — c'est le comportement d'avant le diff, et il ne bouge
    pas.
    """
    _answer, trace, fake = await _run(
        neutre, [_comprendre(neutre), _rediger(neutre), _verifier(neutre)])

    assert fake.remaining_script == 0
    lus = set(neutre.cles(trace.steps[1].opened_block_ids))
    assert {"prise_en_charge", "inscription", "definition_objet"} <= lus
    assert "definition_valeur" not in lus and "bareme" not in lus
    # `definition_registre` non plus : son terme n'est employé que par un bloc entré comme
    # **dépendance**, dont les dépendances ne sont jamais suivies à leur tour (un niveau).
    assert "definition_registre" not in lus


# --- 2. demande valide satisfaite : une passe, une reprise ----------------------------------------

async def test_une_demande_de_definition_valide_est_satisfaite_puis_reprise_une_fois(
        neutre: CorpusNeutre, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 1 : une passe `retrouver` de plus sous **le même objet** budget, une reprise, et la trace.

    L'identité du budget est prouvée par `is` : AD-1 borne l'étape entière, pas chaque passe. Un
    budget reconstruit à l'identique satisferait une égalité et doublerait pourtant le rappel.
    """
    bornes: list[Any] = []
    reel_deterministe, reel_satisfaire = sinistre.retrouver_deterministe, sinistre.satisfaire_demande

    def capture_initiale(*args: Any, **kw: Any):
        bornes.append(kw["budget"])
        return reel_deterministe(*args, **kw)

    def capture_satisfaction(*args: Any, **kw: Any):
        bornes.append(kw["budget"])
        return reel_satisfaire(*args, **kw)

    monkeypatch.setattr(sinistre, "retrouver_deterministe", capture_initiale)
    monkeypatch.setattr(sinistre, "satisfaire_demande", capture_satisfaction)

    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, demande=_demande(neutre, "definition", TERME_DEMANDE)),
        _verifier(neutre)])

    assert fake.remaining_script == 0
    # Exactement deux passes de retrouver, et **le même objet** de bornes pour les deux.
    assert len(bornes) == 2 and bornes[1] is bornes[0]
    assert bornes[0] == retrieval_budget(_settings(neutre.identite))
    # La trace nomme la demande, la satisfaction, les blocs rouverts et l'issue de la reprise.
    noms = _tous_les_checks(trace)
    assert "demande_contexte" in noms and "satisfaction_demande" in noms
    assert "demande_satisfaite" in noms and "reprise_unique" in noms
    assert "seconde_demande_refusee" not in noms and "reprise_moins_bonne" not in noms
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    assert "definition_valeur" in neutre.cles(retrouver.opened_block_ids)
    # Une seule reprise : deux étapes `verifier` dans la trace, jamais trois.
    assert [s.name for s in trace.steps].count("verifier") == 2
    # La reprise domine (mêmes claims, mêmes facettes, mêmes blocs cités, aucun manque de plus) :
    # c'est elle qui est servie, et la réponse n'a plus de lacune de contexte.
    assert answer.found and answer.complete
    assert _lacune() not in answer.unknown


async def test_une_demande_de_renvoi_rouvre_la_cible_du_renvoi(neutre: CorpusNeutre) -> None:
    """La fermeture `refs` d'un niveau, sur un bloc entré comme **dépendance** de la clause lue.

    Ses propres renvois n'avaient jamais été suivis — c'est précisément le manque qu'un `renvoi`
    exprime. Un niveau, jamais deux.
    """
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, demande=_demande(neutre, "renvoi", neutre.bloc("definition_objet"),
                                           raison="renvoi_non_lu")),
        _verifier(neutre)])

    assert fake.remaining_script == 0
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    assert "bareme" in neutre.cles(retrouver.opened_block_ids)
    assert "demande_satisfaite" in _tous_les_checks(trace) and answer.found


async def test_une_demande_de_qualite_ne_vise_que_ce_que_le_modele_a_enumere(
        neutre: CorpusNeutre) -> None:
    """La cible `qualite` vit dans `qualites_exigees` — ce que le modèle a lui-même écrit."""
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, qualites=[QUALITE_DEMANDEE],
                  demande=_demande(neutre, "qualite", QUALITE_DEMANDEE,
                                   raison="qualite_non_verifiable")),
        _verifier(neutre)])

    assert fake.remaining_script == 0
    assert "demande_satisfaite" in _tous_les_checks(trace)
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    assert "definition_valeur" in neutre.cles(retrouver.opened_block_ids)
    assert answer.found


async def test_une_qualite_que_le_modele_na_pas_enumeree_nest_pas_une_cible(
        neutre: CorpusNeutre) -> None:
    """Le contrôle porte sur **cette** affirmation : une qualité jamais énumérée ne désigne rien."""
    answer, _trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, qualites=[],
                  demande=_demande(neutre, "qualite", QUALITE_DEMANDEE,
                                   raison="qualite_non_verifiable"))])

    assert fake.remaining_script == 0  # aucune reprise : le script s'arrête à la première passe
    assert not answer.complete and _lacune() in answer.unknown


# --- 3. les cas fermés ----------------------------------------------------------------------------

async def test_une_categorie_hors_vocabulaire_ne_produit_aucune_demande(
        neutre: CorpusNeutre) -> None:
    """AC 2 : aucune exception, aucune boucle, aucune demande — et le lot reste jugé."""
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, demande={"kind": "relecture_libre", "cible": TERME_DEMANDE,
                                   "claim_id": neutre.identite.claim,
                                   "raison": "definition_manquante"})])

    assert fake.remaining_script == 0
    noms = _tous_les_checks(trace)
    assert "demande_hors_vocabulaire" in noms
    assert "satisfaction_demande" not in noms and "demande_satisfaite" not in noms
    # La claim visée retombe sur le chemin `humain` déjà écrit ; AD-6 en déduit seule le verdict.
    assert "applicabilite_incomplete" in noms
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert not answer.complete and _lacune() in answer.unknown


async def test_une_raison_hors_vocabulaire_ne_produit_aucune_demande(neutre: CorpusNeutre) -> None:
    """Le second vocabulaire fermé compte autant que le premier : une demande ne se répare pas."""
    _answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, demande=_demande(neutre, "definition", TERME_DEMANDE,
                                           raison="parce_que"))])

    assert fake.remaining_script == 0
    assert "demande_hors_vocabulaire" in _tous_les_checks(trace)


@pytest.mark.parametrize("kind,cible,raison", [
    ("renvoi", "definition_valeur", "renvoi_non_lu"),      # un bloc réel, mais jamais fourni
    ("definition", "prime de reconduction", "definition_manquante"),  # un terme absent de l'entrée
    # Revue 4.2e (B) : `cible` est facultatif dans le schéma envoyé, donc une cible omise ou faite
    # de blancs est une sortie conforme. Elle ne désigne rien : même fermeture que les précédentes,
    # affirmation visée comprise — sans quoi un verdict décisoire serait rendu sur une affirmation
    # que le contrôle vient de déclarer injugeable.
    ("definition", "", "definition_manquante"),
    ("definition", "   ", "definition_manquante"),
])
async def test_une_cible_absente_de_lentree_ne_produit_aucune_demande(
        neutre: CorpusNeutre, kind: str, cible: str, raison: str) -> None:
    """AC 2 : la cible doit être **dans ce qui a été soumis**, sinon la claim visée vaut `humain`."""
    visee = neutre.par_cle.get(cible, cible)
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, demande=_demande(neutre, kind, visee, raison=raison))])

    assert fake.remaining_script == 0
    noms = _tous_les_checks(trace)
    assert "demande_cible_inconnue" in noms and "applicabilite_incomplete" in noms
    assert "satisfaction_demande" not in noms
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert not answer.complete and _lacune() in answer.unknown


async def test_un_objet_de_demande_entierement_vide_nest_pas_une_demande(
        neutre: CorpusNeutre) -> None:
    """Revue 4.2e (D) : `{}` est une sortie **conforme** au schéma, et ce n'est pas un manque.

    `transform_schema` ne rend `demande_contexte` ni requis ni fermé : `{}` et l'objet dont les
    quatre valeurs sont nulles traversent la validation. Les classer « hors vocabulaire » faisait
    servir « il me manquait un élément du texte » alors que le modèle n'avait rien demandé — une
    phrase fausse dans la réponse, et une réponse déclarée incomplète pour rien.
    """
    for vide in ({}, {"kind": None, "cible": "", "claim_id": "", "raison": None},
                 {"cible": "   ", "claim_id": "  "}):
        answer, trace, fake = await _run(neutre, [
            _comprendre(neutre), _rediger(neutre), _verifier(neutre, demande=vide)])

        assert fake.remaining_script == 0
        noms = _tous_les_checks(trace)
        assert not [n for n in noms if n.startswith("demande_")], (vide, noms)
        assert answer.found and answer.complete and answer.unknown == []


@pytest.mark.parametrize("brut", ["urgent", ["definition"], 12, 3.5, True])
async def test_une_demande_qui_nest_pas_un_objet_ne_tue_pas_le_lot(
        neutre: CorpusNeutre, brut: object) -> None:
    """Revue 4.2e (G) : un scalaire ou une liste à cette clé faisait perdre **tous** les verdicts.

    C'est l'anti-modèle exact que la sentinelle existe pour empêcher (revue 4.2a, B2) : une valeur
    brute inconnue n'écarte que ce qu'elle concerne, elle ne fait pas échouer la sortie entière en
    `LlmParse` terminal.
    """
    base = json.loads(_verifier(neutre)["content"][0]["text"])
    base["demande_contexte"] = brut
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        fake_message(model=TIERS["reason"], text=json.dumps(base))])

    assert fake.remaining_script == 0  # aucun retry de parse : la sortie était exploitable
    noms = _tous_les_checks(trace)
    assert "demande_hors_vocabulaire" in noms
    # Le lot reste jugé : l'affirmation a bien reçu son verdict de pertinence.
    assert [c.claim_id for c in answer.claims] == [neutre.identite.claim]
    # Revue externe (B1) : une forme non-objet ne porte **aucun** `claim_id` exploitable. Écarter
    # « celui qu'elle nomme » ne retirait alors rien du tout, et l'affirmation gardait son
    # applicabilité : la réponse disait qu'il manquait un élément et rendait quand même un verdict
    # décisoire, dans le même corps. Faute de cible fiable, la fermeture porte sur **toutes** les
    # entrées décisionnelles — c'est la seule lecture fail-closed de « échoue fermée ».
    assert "applicabilite_incomplete" in noms
    assert all(c.status.applicable == "humain" for c in answer.claims)
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert not answer.complete and _lacune() in answer.unknown


async def test_une_demande_bien_formee_sur_un_terme_sans_definition_reste_insatisfaite(
        neutre: CorpusNeutre) -> None:
    """Revue 4.2e (C) : la satisfaction ne doit rien rendre qui ne réponde pas à la demande.

    Le corpus porte une définition dont le terme est employé par un bloc **déjà ouvert**. Rendue
    pour n'importe quelle cible, elle faisait compter « un bloc neuf », déclarait la demande
    satisfaite, payait un appel `reason` de reprise et sautait la fermeture — alors que le contexte
    demandé n'avait jamais été trouvé. C'est la garantie fail-closed de la story qui tombait.
    """
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, demande=_demande(neutre, "definition", TERME_SANS_DEFINITION))])

    assert fake.remaining_script == 0  # aucune reprise n'a été scriptée : aucune n'a eu lieu
    noms = _tous_les_checks(trace)
    assert "demande_insatisfaite" in noms and "demande_satisfaite" not in noms
    assert [s.name for s in trace.steps].count("verifier") == 1
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    assert "definition_registre" not in neutre.cles(retrouver.opened_block_ids)
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert not answer.complete and _lacune() in answer.unknown


async def test_un_claim_id_jamais_envoye_ne_designe_rien(neutre: CorpusNeutre) -> None:
    """« et dans tous les cas un `claim_id` envoyé » : un identifiant inventé ne décide de rien.

    Revue croisée 4.2e (B1) : « ne décide de rien » se lisait auparavant « ne ferme rien ». Le
    contrôle a bien déclaré qu'il lui manquait de quoi juger — il a seulement mal nommé sa cible.
    Ne rien écarter laissait donc `applicable=oui` et AD-6 rendait `couvert`, sous la phrase qui
    annonce un renvoi à une personne. Faute de cible fiable, **aucune** des affirmations jugées
    n'est retenue : c'est la seule lecture fail-closed de « toute demande mal formée échoue fermée ».
    """
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, demande=_demande(neutre, "definition", TERME_DEMANDE,
                                           claim_id="jamais-soumis"))])

    assert fake.remaining_script == 0
    assert "demande_cible_inconnue" in _tous_les_checks(trace)
    assert "satisfaction_demande" not in _tous_les_checks(trace)
    assert all(c.status.applicable == "humain" for c in answer.claims)
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert not answer.complete and _lacune() in answer.unknown


async def test_une_demande_bien_formee_mais_insatisfaite_ferme_sans_reprise(
        neutre: CorpusNeutre) -> None:
    """AC 2 : `retrouver` ne rend aucun bloc neuf ⇒ aucune reprise, lacune typée, `ne_tranche_pas`."""
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, demande=_demande(neutre, "renvoi", neutre.bloc("inscription"),
                                           raison="renvoi_non_lu"))])

    assert fake.remaining_script == 0  # aucune reprise n'a été scriptée : aucune n'a eu lieu
    noms = _tous_les_checks(trace)
    # La passe a bien eu lieu — et elle n'a rien rendu : les deux faits sont distincts et tracés.
    assert "satisfaction_demande" in noms and "demande_insatisfaite" in noms
    assert "demande_satisfaite" not in noms and "reprise_unique" not in noms
    assert [s.name for s in trace.steps].count("verifier") == 1
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert not answer.complete and _lacune() in answer.unknown


async def test_une_satisfaction_partielle_ne_declenche_aucune_reprise(
        neutre: CorpusNeutre) -> None:
    """Revue croisée 4.2e (I1), au pipeline : à moitié relu ⇒ pas de reprise, `ne_tranche_pas`.

    Le renvoi visé a deux cibles et la borne de l'étape n'en laisse entrer qu'une. Le contrôle
    n'obtient donc pas ce qu'il a demandé — il en obtient la moitié. Avant ce correctif, le pipeline
    lisait « au moins un bloc neuf », déclarait la demande satisfaite, dépensait une reprise, et
    cette reprise pouvait rendre un verdict décisoire sur un contexte incomplet.

    `fake.remaining_script == 0` est ici l'assertion centrale : aucune reprise n'a été scriptée,
    donc aucune n'a eu lieu.
    """
    answer, trace, fake = await _run(
        neutre,
        [_comprendre(neutre), _rediger(neutre),
         _verifier(neutre, demande=_demande(neutre, "renvoi", neutre.bloc("definition_objet"),
                                            raison="renvoi_non_lu"))],
        settings=_settings(neutre.identite, retrieval_max_blocks=5))

    assert fake.remaining_script == 0
    noms = _tous_les_checks(trace)
    # La passe a bien eu lieu et a bien ouvert quelque chose — ce n'est pas le cas « rien trouvé ».
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    assert len(retrouver.discarded_block_ids) == 1
    assert "demande_insatisfaite" in noms and "demande_satisfaite" not in noms
    assert "reprise_unique" not in noms
    assert [s.name for s in trace.steps].count("verifier") == 1
    assert all(c.status.applicable == "humain" for c in answer.claims)
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert not answer.complete and _lacune() in answer.unknown


async def test_sans_place_sous_le_budget_aucune_passe_et_aucun_appel(
        neutre: CorpusNeutre, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 2 : `max_attempts` insuffisant ⇒ aucune passe, aucun appel, jamais un 5xx."""
    appels: list[Any] = []
    monkeypatch.setattr(sinistre, "satisfaire_demande",
                        lambda *a, **kw: appels.append(kw) or (_ for _ in ()).throw(AssertionError))

    answer, trace, fake = await _run(
        neutre,
        [_comprendre(neutre), _rediger(neutre),
         _verifier(neutre, demande=_demande(neutre, "definition", TERME_DEMANDE))],
        budget=_budget(max_attempts=3))

    assert fake.remaining_script == 0 and appels == []
    noms = _tous_les_checks(trace)
    assert "reprise_sans_place" in noms and "satisfaction_demande" not in noms
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert not answer.complete and _lacune() in answer.unknown


async def test_sans_marge_de_deadline_aucune_passe(neutre: CorpusNeutre,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """L'autre moitié du contrôle de place : la marge de deadline, sur le patron de la relance."""
    monkeypatch.setattr(sinistre, "satisfaire_demande",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError))

    answer, trace, fake = await _run(
        neutre,
        [_comprendre(neutre), _rediger(neutre),
         _verifier(neutre, demande=_demande(neutre, "definition", TERME_DEMANDE))],
        # Le temps s'épuise après les trois appels de la chaîne : il ne reste plus de quoi écrire
        # la vérification de la reprise, qui en demande 45,7 au débit minoré. Depuis le correctif du
        # tour 4, c'est ce que l'appel va écrire qui décide, pas une marge fixe — et une horloge
        # figée ne pourrait pas distinguer la première vérification de la seconde, qui coûtent le
        # même temps.
        budget=_BudgetQuiSepuise(3))

    assert fake.remaining_script == 0
    assert "reprise_sans_place" in _tous_les_checks(trace)
    assert not answer.complete and _lacune() in answer.unknown


async def test_une_seconde_demande_est_refusee_sans_boucle(neutre: CorpusNeutre) -> None:
    """AC 2 : la reprise redemande du contexte ⇒ refus, aucune seconde satisfaction, aucune boucle."""
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, demande=_demande(neutre, "definition", TERME_DEMANDE)),
        _verifier(neutre, demande=_demande(neutre, "renvoi", neutre.bloc("definition_objet"),
                                           raison="renvoi_non_lu"))])

    assert fake.remaining_script == 0  # aucune troisième vérification n'a été demandée
    noms = _tous_les_checks(trace)
    assert "seconde_demande_refusee" in noms
    # Une seule satisfaction : le check de *retrouver* n'apparaît qu'une fois.
    assert noms.count("satisfaction_demande") == 1
    assert [s.name for s in trace.steps].count("verifier") == 2
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    assert not answer.complete and _lacune() in answer.unknown


async def test_une_reprise_non_dominante_ne_remplace_pas_lacquis(neutre: CorpusNeutre) -> None:
    """La règle de la relance, réutilisée telle quelle : l'acquis fait foi, et la trace le dit."""
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        _verifier(neutre, demande=_demande(neutre, "definition", TERME_DEMANDE)),
        _verifier(neutre, pertinente=False)])

    assert fake.remaining_script == 0
    noms = _tous_les_checks(trace)
    assert "reprise_moins_bonne" in noms
    # L'acquis fait foi : l'affirmation retenue au premier tour est celle qui est servie.
    assert answer.found and [c.claim_id for c in answer.claims] == [neutre.identite.claim]
    # Elle vaut `humain` — le contrôle avait dit qu'il lui manquait de quoi juger.
    assert answer.verdict is not None and answer.verdict.value == "ne_tranche_pas"
    # Revue 4.2e (E) : c'était le seul chemin de fermeture du bloc qui servait la réponse pour
    # **complète**. Le jugement retenu est celui rendu AVANT relecture du contexte : il ne peut pas
    # être donné pour complet, et une demande `renvoi` y contredirait en outre AD-1.
    assert not answer.complete and _lacune() in answer.unknown


# --- 3 bis. ce que la reprise relit, et ce qu'elle ne doit pas perdre (revue 4.2e) ----------------

CITATION_INSCRIPTION = "n'est acquise que si le local reste inscrit au registre déclaré"


def _rediger_libre(corpus: CorpusNeutre, *claims: tuple[str, str, str]) -> dict:
    """`(claim_id, texte, clé du bloc cité)` — une ébauche dont chaque claim a son segment factuel."""
    citations = {"prise_en_charge": CITATION, "inscription": CITATION_INSCRIPTION}
    return fake_message(model=TIERS["reason"], text=json.dumps({
        "segments": [{"text": texte, "kind": "factuel", "claim_ids": [cid]}
                     for cid, texte, _cle in claims],
        "claims": [{"claim_id": cid, "text": texte,
                    "quotes": [{"block_id": corpus.bloc(cle), "quote": citations[cle]}]}
                   for cid, texte, cle in claims]}))


def _verifier_libre(*paires: tuple[str, bool], demande: dict | None = None) -> dict:
    charge: dict[str, Any] = {
        "verdicts": [{"claim_id": cid, "pertinente": ok,
                      "raison": None if ok else "non_soutenue"} for cid, ok in paires],
        "facettes": [{"facette": 0, "claim_ids": [cid for cid, ok in paires if ok]}],
        "segments": [{"segment": rang, "soutenu": ok} for rang, (_cid, ok) in enumerate(paires)],
        "applicabilite": [{"claim_id": cid, "fait_requis_present": True, "option_requise": False,
                           "cp_requise": False, "fait_manquant": None,
                           "qualites_exigees": [], "qualites_etablies": []} for cid, _ok in paires]}
    if demande is not None:
        charge["demande_contexte"] = demande
    return fake_message(model=TIERS["reason"], text=json.dumps(charge))


async def test_la_reprise_relit_lebauche_qui_a_produit_la_demande(
        neutre: CorpusNeutre, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue 4.2e (A) : après une relance AD-3 adoptée, la demande porte sur la **seconde** ébauche.

    Reprendre la première dépensait un appel `reason` sur une ébauche périmée, soumettait un lot où
    le `claim_id` de la demande n'existe pas, et — `domine()` n'étant pas stricte — pouvait faire
    servir la rédaction d'**avant** la relance : la correction que la relance venait d'obtenir
    disparaissait alors en silence.
    """
    vues: list[list[str]] = []
    reel = sinistre.verifier

    async def capture(draft, **kw: Any):
        vues.append([c.claim_id for c in draft.claims])
        return await reel(draft, **kw)

    monkeypatch.setattr(sinistre, "verifier", capture)
    answer, trace, fake = await _run(neutre, [
        _comprendre(neutre),
        _rediger_libre(neutre, ("k1", AFFIRMATION, "prise_en_charge")),
        _verifier_libre(("k1", False)),                       # rien ne survit ⇒ relance due
        _rediger_libre(neutre, ("k1b", AFFIRMATION + " Corrigée.", "prise_en_charge")),
        _verifier_libre(("k1b", True),
                        demande={"kind": "definition", "cible": TERME_DEMANDE,
                                 "claim_id": "k1b", "raison": "definition_manquante"}),
        _verifier_libre(("k1b", True))])

    assert fake.remaining_script == 0
    # Trois vérifications : la première, celle de la relance, puis la reprise — et la reprise relit
    # l'ébauche de la relance, jamais la première.
    assert vues == [["k1"], ["k1b"], ["k1b"]]
    assert "demande_satisfaite" in _tous_les_checks(trace)
    assert [c.claim_id for c in answer.claims] == ["k1b"]


async def test_la_reprise_ne_perd_pas_la_lacune_dune_relance_abandonnee(
        neutre: CorpusNeutre) -> None:
    """Revue 4.2e (L) : une reprise dominante adoptée telle quelle effaçait la lacune du pipeline.

    *vérifier* dérive ses lacunes de son propre état ; il ne peut pas savoir qu'une relance due n'a
    jamais démarré. Adopter la reprise sans reconduire cette cause-là faisait servir `complete=True`
    sur une réponse dont une relance manque toujours.
    """
    answer, trace, fake = await _run(
        neutre,
        [_comprendre(neutre),
         _rediger_libre(neutre, ("k1", AFFIRMATION, "prise_en_charge"),
                        ("k2", "Une inscription au registre est requise.", "inscription")),
         _verifier_libre(("k1", True), ("k2", False),
                         demande={"kind": "definition", "cible": TERME_DEMANDE,
                                  "claim_id": "k1", "raison": "definition_manquante"}),
         _verifier_libre(("k1", True), ("k2", True))],
        settings=_settings(neutre.identite, relance_sur_non_pertinence=True),
        # De quoi vérifier une seconde fois, mais pas de quoi relancer *rédiger* : la relance est
        # due et n'a jamais démarré, la reprise du contexte tient encore.
        budget=_budget(max_attempts=4))

    assert fake.remaining_script == 0
    noms = _tous_les_checks(trace)
    assert "relance_abandonnee" in noms and "demande_satisfaite" in noms
    assert answer.found and not answer.complete
    assert PHRASES_DE_LACUNE["fr"]["relance_abandonnee"] in answer.unknown


async def test_une_demande_insatisfaite_par_le_budget_publie_sa_borne(
        neutre: CorpusNeutre) -> None:
    """Revue 4.2e (F) : des blocs écartés par le budget, et `truncations=0` dans la même trace.

    La passe de complément écartait des candidats sans que `truncated` remonte : la trace publiait
    une lecture bornée et la comptait pour zéro, et un refus repartait sur une preuve d'absence qui
    annonce un balayage **exhaustif**. Une lecture bornée dont rien n'a survécu n'affirme aucune
    absence (NFR2) : elle chiffre ce qu'elle a lu.
    """
    answer, trace, fake = await _run(
        neutre,
        [_comprendre(neutre),
         _rediger_libre(neutre, ("k1", AFFIRMATION, "prise_en_charge")),
         _verifier_libre(("k1", False),
                         demande={"kind": "definition", "cible": TERME_DEMANDE,
                                  "claim_id": "k1", "raison": "definition_manquante"}),
         # Aucune affirmation n'a survécu : la relance d'AD-3 est due et rend la **même** ébauche.
         # Elle s'arrête donc sur `relance_sans_effet`, sans seconde vérification — la sortie qui
         # nous intéresse reste celle qui porte la demande, et le bloc 4.2e la reçoit intacte.
         _rediger_libre(neutre, ("k1", AFFIRMATION, "prise_en_charge"))],
        # La première passe lit exactement quatre blocs ; le candidat du complément n'a plus de place.
        settings=_settings(neutre.identite, retrieval_max_blocks=4))

    assert fake.remaining_script == 0
    assert "relance_sans_effet" in _tous_les_checks(trace)
    assert [s.name for s in trace.steps].count("verifier") == 1  # aucune reprise n'a eu lieu
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    assert "definition_valeur" in neutre.cles(retrouver.discarded_block_ids)
    assert "demande_insatisfaite" in _tous_les_checks(trace)
    # La borne est publiée, et le refus ne prétend plus avoir tout lu.
    assert trace.truncations == 1
    assert answer.reason is None and answer.lecture_partielle is not None
    assert answer.lecture_partielle.blocks_read == 4


# --- 4. le chemin normal, à l'octet du contrat près -----------------------------------------------

async def test_sans_demande_le_chemin_est_celui_davant_le_diff(neutre: CorpusNeutre) -> None:
    """AC 3 : aucun champ rendu ⇒ aucun check nouveau, aucune passe de plus, aucune lacune."""
    answer, trace, fake = await _run(
        neutre, [_comprendre(neutre), _rediger(neutre), _verifier(neutre)])

    assert fake.remaining_script == 0
    noms = _tous_les_checks(trace)
    for nouveau in ("demande_contexte", "demande_hors_vocabulaire", "demande_cible_inconnue",
                    "satisfaction_demande", "demande_satisfaite", "demande_insatisfaite",
                    "reprise_unique", "reprise_moins_bonne", "reprise_sans_place",
                    "seconde_demande_refusee"):
        assert nouveau not in noms, nouveau
    assert [s.name for s in trace.steps] == ["comprendre", "retrouver", "rediger", "verifier",
                                             "restituer"]
    assert answer.found and answer.complete and answer.unknown == []


async def test_un_champ_absent_et_un_champ_nul_sont_le_meme_chemin(neutre: CorpusNeutre) -> None:
    """`demande_contexte: null` n'est pas une demande vide : c'est l'absence de demande."""
    sans, trace_sans, _f1 = await _run(
        neutre, [_comprendre(neutre), _rediger(neutre), _verifier(neutre)])
    nul, trace_nul, _f2 = await _run(neutre, [
        _comprendre(neutre), _rediger(neutre),
        fake_message(model=TIERS["reason"], text=json.dumps({
            **json.loads(_verifier(neutre)["content"][0]["text"]), "demande_contexte": None}))])

    assert _tous_les_checks(trace_sans) == _tous_les_checks(trace_nul)
    assert (sans.found, sans.complete, sans.unknown) == (nul.found, nul.complete, nul.unknown)


# --- 5. gardes métamorphiques : aucune décision ne dépend d'un identifiant -------------------------

async def _decisions(identite: Identite, faire_demande: bool) -> dict[str, Any]:
    """Ce qu'une exécution **décide**, lu sans aucun identifiant : rôles, checks, verdict, lacunes."""
    corpus = _corpus_neutre(identite)
    script = [_comprendre(corpus), _rediger(corpus)]
    if faire_demande:
        script += [_verifier(corpus, demande=_demande(corpus, "definition", TERME_DEMANDE)),
                   _verifier(corpus)]
    else:
        script.append(_verifier(corpus))
    answer, trace, fake = await _run(corpus, script)
    retrouver = next(s for s in trace.steps if s.name == "retrouver")
    return {
        "script_epuise": fake.remaining_script == 0,
        "checks": sorted(set(_tous_les_checks(trace))),
        "roles_lus": sorted(corpus.cles(retrouver.opened_block_ids)),
        "found": answer.found,
        "complete": answer.complete,
        "verdict": None if answer.verdict is None else answer.verdict.value,
        "unknown": list(answer.unknown),
        "etapes": [s.name for s in trace.steps],
    }


@pytest.mark.parametrize("faire_demande", [True, False])
async def test_les_decisions_sont_invariantes_sous_permutation(faire_demande: bool) -> None:
    """AC : bijection des ids, décalage des pages, ordre de lecture inversé ⇒ décisions identiques."""
    assert await _decisions(IDENTITE, faire_demande) == await _decisions(IDENTITE_PERMUTEE,
                                                                          faire_demande)


async def test_les_decisions_resistent_a_une_reformulation_de_la_cible() -> None:
    """Une cible reformulée — casse, accents, ponctuation — désigne le même terme, ou rien de plus.

    La comparaison passe par la clé de terme du projet (`normalize` puis `words`), la même que
    `Index.chercher` : ni un jugement, ni une similarité, ni un lexique propre à un document.
    """
    corpus = _corpus_neutre(IDENTITE)
    resultats = []
    for cible in (TERME_DEMANDE, "Valeur Résiduelle", "  valeur   residuelle  "):
        answer, trace, fake = await _run(corpus, [
            _comprendre(corpus), _rediger(corpus),
            _verifier(corpus, demande=_demande(corpus, "definition", cible)),
            _verifier(corpus)])
        assert fake.remaining_script == 0
        resultats.append((sorted(set(_tous_les_checks(trace))), answer.found, answer.complete))
    assert len(set(map(str, resultats))) == 1


def test_controle_negatif_une_decision_branchee_sur_un_identifiant_est_detectee() -> None:
    """La permutation ferait bien rougir un branchement interdit — sinon elle ne prouverait rien."""
    original = _corpus_neutre(IDENTITE)
    permute = _corpus_neutre(IDENTITE_PERMUTEE)

    def mauvaise_regle(corpus: CorpusNeutre) -> bool:
        return corpus.bloc("prise_en_charge") == f"{IDENTITE.doc_id}:p1:2"

    assert mauvaise_regle(original) is True
    assert mauvaise_regle(permute) is False


def test_le_vocabulaire_du_corpus_synthetique_est_neutre() -> None:
    """Never 4.2b : aucun vocabulaire d'assureur réel ni de question-témoin dans ce corpus."""
    source = " ".join([*(t for _c, _k, t, _d, _r in (*SECTION_LUE, *SECTION_FERMEE)),
                       QUESTION, QUESTION_RESOLUE, FAITS.description or "", AFFIRMATION,
                       TERME_DEMANDE, QUALITE_DEMANDEE, *TERMES]).lower()
    for interdit in ("axa", "baloise", "optihome", "bougie", "canape", "canapé", "mobilier",
                     "soudain", "chaleur", "p34:12"):
        assert interdit not in source, f"vocabulaire non neutre : {interdit!r}"


# --- 6. le type du domaine : ses invariants se posent une fois ------------------------------------

def test_le_type_de_la_demande_est_ferme_et_non_vide() -> None:
    """`extra="forbid", frozen=True`, cible et claim non vides, vocabulaires fermés."""
    from pydantic import ValidationError

    demande = DemandeContexte(kind="definition", cible="terme", claim_id="c1",
                              raison="definition_manquante")
    with pytest.raises(ValidationError):
        demande.cible = "autre"  # frozen : une demande ne se corrige pas en route
    for invalide in (
            {"kind": "autre", "cible": "t", "claim_id": "c1", "raison": "definition_manquante"},
            {"kind": "definition", "cible": "", "claim_id": "c1", "raison": "definition_manquante"},
            {"kind": "definition", "cible": "t", "claim_id": "", "raison": "definition_manquante"},
            {"kind": "definition", "cible": "t", "claim_id": "c1", "raison": "parce_que"},
            {"kind": "definition", "cible": "t", "claim_id": "c1",
             "raison": "definition_manquante", "surnumeraire": 1},
    ):
        with pytest.raises(ValidationError):
            DemandeContexte(**invalide)


# --- 7. la passe de satisfaction, prise seule : code pur, un niveau, bornée par l'étape ------------

def _retrieval(corpus: CorpusNeutre, *cles: str) -> RetrievalResult:
    document = corpus.index.corpus.documents[corpus.identite.doc_id]
    blocs = [document.block(corpus.bloc(cle)) for cle in cles]
    return RetrievalResult(blocs=blocs, opened_block_ids=[b.block_id for b in blocs],
                           opened_node_ids=[corpus.identite.noeud(corpus.identite.lue)])


def _satisfaire(corpus: CorpusNeutre, demande: DemandeContexte, *, retrieval: RetrievalResult,
                **kw):
    reglages = _settings(corpus.identite, **kw)
    return satisfaire_demande(demande, retrieval=retrieval, corpus=corpus.index.corpus,
                              index=corpus.index, budget=retrieval_budget(reglages),
                              settings=reglages, doc_id=corpus.identite.doc_id)


def test_la_satisfaction_najoute_aucun_appel_modele(neutre: CorpusNeutre) -> None:
    """AD-1 : la satisfaction est une résolution du corpus, pas une navigation. Coût nul en appels."""
    depart = _retrieval(neutre, "prise_en_charge", "inscription", "definition_objet")
    augmente, step = _satisfaire(
        neutre,
        DemandeContexte(kind="definition", cible=TERME_DEMANDE, claim_id="k1",
                        raison="definition_manquante"),
        retrieval=depart)

    assert step.calls == [] and step.name == "retrouver"
    assert neutre.cles(step.opened_block_ids) == ["definition_valeur"]
    assert "definition_valeur" in neutre.cles(b.block_id for b in augmente.blocs)
    # Le résultat est **augmenté**, jamais remplacé : rien de ce qui était lu ne disparaît.
    assert [b.block_id for b in depart.blocs] == [b.block_id for b in augmente.blocs][:3]
    assert augmente.truncated is False
    # `opened_node_ids` est recalculé sur les blocs transmis : le compteur ne contredit pas l'autre.
    assert len(augmente.opened_node_ids) == 2


def test_la_satisfaction_ne_suit_quun_seul_niveau(neutre: CorpusNeutre) -> None:
    """Les `refs` du bloc visé, jamais les `refs` de leurs cibles : la chaîne ne s'allonge pas."""
    depart = _retrieval(neutre, "prise_en_charge", "definition_objet")
    augmente, _step = _satisfaire(
        neutre,
        DemandeContexte(kind="renvoi", cible=neutre.bloc("definition_objet"), claim_id="k1",
                        raison="renvoi_non_lu"),
        retrieval=depart)
    lus = set(neutre.cles(b.block_id for b in augmente.blocs))
    # Les **deux** `refs` du bloc visé, et rien au-delà : ni les `refs` de `bareme` ou d'
    # `annexe_technique`, ni la définition d'un terme que le bloc visé emploie.
    assert lus == {"prise_en_charge", "definition_objet", "bareme", "annexe_technique"}


def test_la_satisfaction_est_bornee_par_le_budget_de_letape_entiere(neutre: CorpusNeutre) -> None:
    """AD-1 : « un `RetrievalBudget` borne **toute** l'étape », donc les deux passes ensemble.

    Une seconde passe repartie de compteurs neufs pourrait admettre `max_blocks` blocs de plus :
    elle ne serait plus bornée du tout. Les compteurs sont donc amorcés avec ce que la passe
    initiale a déjà fait lire, et ce qui n'entre pas est **écarté** et dit, jamais tu.
    """
    depart = _retrieval(neutre, "prise_en_charge", "inscription", "definition_objet")
    demande = DemandeContexte(kind="definition", cible=TERME_DEMANDE, claim_id="k1",
                              raison="definition_manquante")

    juste = _satisfaire(neutre, demande, retrieval=depart, retrieval_max_blocks=4)[0]
    assert len(juste.blocs) == 4 and juste.truncated is False

    plein, step = _satisfaire(neutre, demande, retrieval=depart, retrieval_max_blocks=3)
    assert len(plein.blocs) == 3 and plein.truncated is True
    assert neutre.cles(plein.discarded_block_ids) == ["definition_valeur"]
    assert neutre.cles(step.discarded_block_ids) == ["definition_valeur"]
    assert step.opened_block_ids == []
    assert not next(c for c in step.checks if c.name == "satisfaction_demande").ok


def test_une_satisfaction_partielle_nest_pas_une_satisfaction(neutre: CorpusNeutre) -> None:
    """Revue croisée 4.2e (I1) : un candidat ouvert **et** un candidat écarté ⇒ demande insatisfaite.

    Le cas est celui d'un renvoi à deux cibles avec une seule place restante. Le rappel n'a rien
    fait de mal : il a lu ce qu'il pouvait sous la borne de l'étape. Mais le contexte demandé n'est
    relu qu'**à moitié**, et « à moitié » n'est pas une réponse à « il me manque ceci pour juger ».
    Déclarer la demande satisfaite laissait alors une reprise juger sur un contexte incomplet et
    rendre un verdict décisoire — la place insuffisante ne fermait plus vers `humain`.
    """
    depart = _retrieval(neutre, "prise_en_charge", "inscription", "definition_objet")
    demande = DemandeContexte(kind="renvoi", cible=neutre.bloc("definition_objet"), claim_id="k1",
                              raison="renvoi_non_lu")

    partielle, step = _satisfaire(neutre, demande, retrieval=depart, retrieval_max_blocks=4)
    # La passe a bien ouvert un bloc — ce n'est pas le cas « rien trouvé » — et en a écarté un.
    assert len(step.opened_block_ids) == 1 and len(step.discarded_block_ids) == 1
    assert partielle.truncated is True
    # Et elle ne se déclare pas satisfaite pour autant : c'est le fait que le pipeline lit.
    assert not next(c for c in step.checks if c.name == "satisfaction_demande").ok

    # Contrôle : avec la place pour les deux, la même demande est bien satisfaite.
    entiere, step_entier = _satisfaire(neutre, demande, retrieval=depart, retrieval_max_blocks=5)
    assert len(step_entier.opened_block_ids) == 2 and step_entier.discarded_block_ids == []
    assert entiere.truncated is False
    assert next(c for c in step_entier.checks if c.name == "satisfaction_demande").ok


def test_la_satisfaction_ne_rend_rien_quand_le_contexte_nexiste_pas(neutre: CorpusNeutre) -> None:
    """Aucun bloc neuf n'est une issue **normale** : c'est au pipeline de fermer, pas à l'étape."""
    depart = _retrieval(neutre, "prise_en_charge", "inscription", "definition_objet")
    augmente, step = _satisfaire(
        neutre,
        DemandeContexte(kind="renvoi", cible=neutre.bloc("inscription"), claim_id="k1",
                        raison="renvoi_non_lu"),
        retrieval=depart)

    assert [b.block_id for b in augmente.blocs] == [b.block_id for b in depart.blocs]
    assert augmente.truncated is False and step.opened_block_ids == []
    assert not next(c for c in step.checks if c.name == "satisfaction_demande").ok


def test_la_satisfaction_refuse_un_document_quelle_ne_sert_pas(neutre: CorpusNeutre) -> None:
    """La même garde que les deux variantes : un `doc_id` inconnu est une faute d'appel."""
    with pytest.raises(KeyError):
        satisfaire_demande(
            DemandeContexte(kind="definition", cible=TERME_DEMANDE, claim_id="k1",
                            raison="definition_manquante"),
            retrieval=_retrieval(neutre, "prise_en_charge"), corpus=neutre.index.corpus,
            index=neutre.index, budget=retrieval_budget(_settings(neutre.identite)),
            settings=_settings(neutre.identite), doc_id="document-absent")
