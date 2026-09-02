"""Story 4.5 — Le gate `full` décide, condition par condition.

Ce que ces tests protègent, en une phrase : **le profil `full` n'est plus inerte**. Avant cette
story, `full` ne faisait qu'élargir la sélection des cas ; aucune des huit preuves que l'AC exige ne
pouvait rougir le gate, et `expected_blocks_not_opened` était publié sans jamais rien décider.

La méthode est celle des Design Notes : chaque preuve manquante devient un **témoin du plancher**, et
le mécanisme de fermeture par vacuité de `construire_decisions` (« témoin bloquant applicable non
émis ⇒ décision rouge `n=0, value=0.0` ») les rend fail-closed sans une ligne de branchement dans le
runner. On vérifie donc deux choses distinctes, et les deux comptent :

1. chaque condition rouge produit une **décision chiffrée** — `{metric, producer, threshold, scope,
   n, run_digest, value, status}` — et non un booléen ;
2. ce que le gate n'a **pas** mesuré est rouge, jamais neutre.

Corpus et cas sont synthétiques et neutres : aucun identifiant, aucune formulation et aucune page
d'un témoin réel n'entre ici (`tests/test_anti_rustine.py`). Aucun réseau, aucune clé.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.answer import (Answer, AnswerSegment, ClaimStatus, LecturePartielle,
                                      VerifiedClaim, VerifiedQuote)
from server.app.domain.document import Document, Node
from server.app.domain.ingest import ManifestEntry
from server.app.domain.trace import StepTrace, Trace
from server.app.domain.verdict import Verdict
from server.evals import run as runner
from server.evals.espace import EspacePublie, POINTEUR, Transaction
from server.evals.quality_closure import (DEPENDENCIES, ROW_IDS, BranchKey, ClosureInput,
                                          EvidenceArtifact, EvidenceStatus,
                                          QualityClosureRunInput, canonical_registry,
                                          canonical_registry_hash, canonical_required_refs)
from tests.helpers_espace import poser_espace
from server.evals.plancher import charger_plancher

DOC = "contrat-neutre"
# Volontairement neutre : aucun vocabulaire d'assureur reel, aucune formulation de temoin.
TEXTE = ("Le bien decrit au present chapitre est garanti selon les conditions qui y figurent, "
         "sous reserve des exceptions enoncees a la section suivante.")
REVISION = "0" * 40

# Les dix témoins que la story ajoute. La liste est **relue du plancher** plus bas, et l'égalité des
# deux ensembles est asserée : l'écrire ici sert à nommer ce que l'AC exige, pas à le définir — mais
# un témoin `gate_full` ajouté au plancher sans passer par cette liste ferait rougir le test, ce qui
# est le bon comportement pour un fichier qui porte le protocole.
DIX = (
    "parsing_ok_rate", "blocs_attendus_ouverts_rate", "citations_retrouvees_rate",
    "zero_5xx_technique_rate", "typage_confirme_rate", "structure_prouvee_rate",
    "arbre_prouve_rate", "stabilite_claim_decisionnelle", "anti_rustine_pass_rate",
    "metamorphique_pass_rate",
)


_PREUVE_STRUCTURE = runner.preuve_de_structure
_PREUVE_ARBRE = runner.preuve_darbre


def _preuve_structure(data: Path, ctx: runner.Contexte, documents: list[str]) -> tuple[int, int]:
    """Primitive unitaire explicite : ces matrices synthétiques n'exercent pas un entrypoint."""
    from server.app.corpus.racine import _lecture_interne_sans_racine

    with _lecture_interne_sans_racine(data) as lecture:
        return _PREUVE_STRUCTURE(data, ctx, documents, lecture=lecture)


def _preuve_arbre(data: Path, ctx: runner.Contexte, documents: list[str]) -> tuple[int, int]:
    from server.app.corpus.racine import _lecture_interne_sans_racine

    with _lecture_interne_sans_racine(data) as lecture:
        return _PREUVE_ARBRE(data, ctx, documents, lecture=lecture)


# --- fabriques neutres ----------------------------------------------------------------------------

def _settings(**kw: Any) -> Settings:
    defauts: dict[str, Any] = {"anthropic_api_key": "cle-de-test", "guide_doc_id": "guide-neutre",
                               "sinistre_doc_id": DOC}
    defauts.update(kw)
    return Settings(_env_file=None, **defauts)


# Les octets de source d'un document servi. Ce qu'un document **est** se lit sur son disque (règle
# `SOURCE_FILES` du loader) : le corpus synthétique doit donc en porter une, comme le vrai.
OCTETS_SOURCE = {"source.pdf": b"%PDF-1.4 minimal", "source.js": b"var kb = {};"}


def _source_hash(source: str | None) -> str:
    return "s" if source is None else hashlib.sha256(OCTETS_SOURCE[source]).hexdigest()


def _document(*, kind_source: str | None = "manual", source_hash: str = "s") -> Document:
    doc = Document(
        doc_id=DOC, kind="contrat", title="Contrat neutre", edition="2020",
        source_hash=source_hash, ingest_fingerprint="f",
        nodes=[Node(node_id=f"{DOC}:n1", level=1, title="N1",
                    items=[{"block_id": f"{DOC}:p1:1"}])],
        blocks=[{"block_id": f"{DOC}:p1:1", "loc": "p1", "seq": 1, "kind": "garantie",
                 "kind_source": kind_source, "page": 1, "bbox": [10.0, 20.0, 300.0, 40.0],
                 "text": TEXTE}])
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    return doc


def _contexte(*, kind_source: str | None = "manual") -> runner.Contexte:
    docs = {DOC: _document(kind_source=kind_source)}
    manifest = {DOC: ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                                   document_hash="d", edition="2020")}
    corpus = Corpus(documents=docs, manifest=manifest, summaries={DOC: "# doc"},
                    alerts={DOC: []})
    return runner.Contexte(settings=_settings(), index=Index(corpus), client=None,
                           pipeline_digest_hex="pd", prompts_digest_hex="pp")


def _cas_sinistre(case_id: str = "s-cas-neutre") -> runner.Cas:
    return runner.Cas.model_validate({
        "id": case_id, "suite": "sinistre", "profile": "vertical",
        "question": "Cette situation entre-t-elle dans la garantie decrite ?",
        "faits": {"description": "Un bien decrit au contrat a subi une atteinte."},
        "expected": {"found": True, "block_ids": [f"{DOC}:p1:1"],
                     "verdict": ["sous_conditions", "ne_tranche_pas"]},
        "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
        "mode_attendu": "bonne_reponse",
    })


def _cas_parsing(case_id: str = "p-cas-neutre") -> runner.Cas:
    cas = runner.Cas.model_validate({
        "id": case_id, "suite": "parsing", "profile": "full",
        "question": "Le texte du bloc est-il celui du contrat imprime ?",
        "famille": "garantie",
        "expected": {"found": True, "block_ids": [f"{DOC}:p1:1"], "text_norm": normalize(TEXTE)},
        "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
        "mode_attendu": "bonne_reponse",
    })
    cas._doc_id = DOC
    return cas


def _resultat(**kw: Any) -> runner.Resultat:
    defauts: dict[str, Any] = {
        "id": "s-cas-neutre", "suite": "sinistre", "label": "bonne_reponse",
        "variant": runner.DEFAUT_PAR_SUITE["sinistre"], "repetition": 1, "doc_id": DOC,
        "found": True, "verdict": "sous_conditions", "http": 200,
        "expected_block_ids": [f"{DOC}:p1:1"], "opened_block_ids": [f"{DOC}:p1:1"],
        "proofs": [{"doc_id": DOC, "block_id": f"{DOC}:p1:1", "kind": "garantie",
                    "quote_hash": "h", "kind_confirmed": True}],
        "decision_claim": True,
    }
    defauts.update(kw)
    return runner.Resultat(**defauts)


def _cas_guide(case_id: str = "g-cas-neutre") -> runner.Cas:
    return runner.Cas.model_validate({
        "id": case_id, "suite": "guide", "profile": "vertical",
        "question": "Ou trouve-t-on la marche a suivre decrite par la fiche ?",
        "expected": {"found": True}, "mode_attendu": "bonne_reponse",
        "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
    })


def _decisions(resultats: list[runner.Resultat], cas: list[runner.Cas], *, repeat: int = 3,
               exigences_full: bool = True, structure: tuple[int, int] | None = (1, 1),
               arbre: tuple[int, int] | None = (1, 1),
               producer: str = "orchestrator",
               non_executes: list[str] | None = None) -> dict[str, Any]:
    """Les décisions du gate, indexées par métrique. `producer=orchestrator` isole la valeur.

    Sous `builder`, **toutes** les décisions sont rouges (« producteur non probant ») : c'est la
    règle trusted de 4.2b, et elle masquerait ce que ces tests veulent voir — la valeur mesurée.
    """
    charge = charger_plancher()
    decisions = runner.construire_decisions(
        resultats, cas, plancher=charge, repeat=repeat, run_digest="a" * 64, producer=producer,
        non_executes=non_executes, exigences_full=exigences_full, structure=structure,
        arbre=arbre)
    return {d.metric: d for d in decisions}


def _temoins_gate_full_applicables(cas: list[runner.Cas]) -> set[str]:
    """Les témoins `gate_full` que **ce lot** rend applicables — la règle du plancher, pas une liste.

    Le périmètre d'un témoin dépend du lot : les deux preuves de structure sont complémentaires
    (`structure_prouvee_rate` pour un document PDF, `arbre_prouve_rate` pour une copie de site), et
    le typage juridique ne se mesure que sur la suite qui porte des clauses. Écrire « les dix » en
    dur dans chaque assertion aurait exigé qu'un gate de contrat prouve l'arbre d'un guide absent du
    lot — un mur, pas une exigence.
    """
    return {t.metric for t in charger_plancher().plancher.temoins
            if t.arme_par == "gate_full"
            and runner._temoin_applicable(t, cas, exigences_full=True)}


# --- le plancher porte les dix témoins, et ne perd rien -------------------------------------------

def test_les_dix_temoins_vivent_dans_le_plancher_et_nabaissent_rien() -> None:
    """Boundaries : « tout nouveau seuil vit dans `plancher.yaml`, jamais en dur ».

    Et il s'y ajoute **sans jamais abaisser ni retirer** un témoin importé : les quatre seuils du
    floor 4.2a et les huit témoins de 4.2b sont encore là, à leur valeur, et les digests des
    snapshots figés n'ont pas bougé — c'est `charger_plancher` qui le vérifie, en refusant de
    charger sinon.
    """
    charge = charger_plancher()
    par_metric = {t.metric: t for t in charge.plancher.temoins}
    # L'égalité, pas l'inclusion : un témoin `gate_full` ajouté au plancher sans être nommé ici — ou
    # retiré — fait rougir ce test. C'est un fichier de protocole ; il ne bouge pas en silence.
    assert {t.metric for t in charge.plancher.temoins if t.arme_par == "gate_full"} == set(DIX)
    for metric in DIX:
        temoin = par_metric[metric]
        assert temoin.criticite == "bloquant", metric
        assert temoin.plancher == 1.0 and temoin.n == 3, metric
        assert temoin.numerateur.strip() and temoin.denominateur.strip(), metric
        assert temoin.incident.strip(), metric
        # Ils ne s'arment que sur le gate qui revendique la politique complète.
        assert temoin.arme_par == "gate_full", metric
    # Les huit de 4.2b sont intacts et restent armés partout.
    anciens = {"offline_tests_pass_rate", "bougie_post_success_rate", "a16_post_success_rate",
               "decision_claim_rate", "stabilite_sinistre", "stabilite_guide", "cases_ok_rate",
               "executions_completes"}
    assert anciens <= set(par_metric)
    assert all(par_metric[m].arme_par == "toujours" for m in anciens)
    for metric, seuil in charge.plancher.imports.floor_4_2a.thresholds.items():
        assert par_metric[metric].plancher >= seuil
    assert {t.mesure_par for t in (par_metric["anti_rustine_pass_rate"],
                                   par_metric["metamorphique_pass_rate"])} == {"orchestrator"}


def test_un_temoin_gate_full_ne_sarme_pas_ailleurs() -> None:
    """Design Note : un `full` sans gate reste un diagnostic, et `vertical` n'affirme que deux cas.

    Le contrôle porte sur `_temoin_applicable`, l'unique porte : si elle laissait passer, la CI
    (`--profile full` sans `--gate`) rougirait à chaque PR pour une raison étrangère au candidat.
    """
    charge = charger_plancher()
    cas = [_cas_sinistre(), _cas_parsing()]
    for temoin in charge.plancher.temoins:
        if temoin.arme_par != "gate_full":
            continue
        assert runner._temoin_applicable(temoin, cas, exigences_full=False) is False, temoin.metric
    # Hors gate `full`, aucune des dix métriques n'apparaît dans les décisions.
    hors = _decisions([_resultat()], cas, exigences_full=False)
    assert not (set(DIX) & set(hors))
    # Et les décisions historiques, elles, sont toujours là.
    assert {"cases_ok_rate", "executions_completes"} <= set(hors)


# --- les neuf conditions rouges de l'AC 1 ----------------------------------------------------------

def _rouge(decision: Any) -> bool:
    return decision.status == "red" and decision.value < decision.threshold


def test_une_decision_porte_les_huit_champs_que_lac_exige() -> None:
    """AC 1 : « chaque décision porte `{metric, producer, threshold, scope, n, run_digest, value,
    status}` », et `evals_ok` est leur conjonction — jamais un booléen à part."""
    decisions = _decisions([_resultat(repetition=r) for r in (1, 2, 3)], [_cas_sinistre()])
    for metric, d in decisions.items():
        assert d.metric == metric
        assert d.producer == "orchestrator"
        assert isinstance(d.threshold, float) and 0.0 <= d.threshold <= 1.0
        assert isinstance(d.scope, str) and d.scope
        assert isinstance(d.n, int) and d.n >= 0
        assert len(d.run_digest) == 64
        assert isinstance(d.value, float)
        assert d.status in ("green", "red")


def test_un_ecart_de_parsing_rougit_le_gate() -> None:
    """I/O matrix : « un cas `parsing` du document rend `label=parsing` ⇒ `parsing_ok_rate < 1.0` ».

    Le témoin ne peut rougir que parce que la suite `parsing` du document est **dans le lot** sous
    `--gate X --profile full` : c'est le manque que l'AC 1 nomme, et les deux moitiés se tiennent.
    """
    cas = [_cas_sinistre(), _cas_parsing()]
    ok = [_resultat(repetition=r) for r in (1, 2, 3)] + [
        _resultat(id="p-cas-neutre", suite="parsing", label="bonne_reponse", variant="local",
                  http=None, repetition=r) for r in (1, 2, 3)]
    assert _decisions(ok, cas)["parsing_ok_rate"].status == "green"
    divergent = [r for r in ok if r.suite != "parsing"] + [
        _resultat(id="p-cas-neutre", suite="parsing", label="parsing", variant="local", http=None,
                  ecarts=["texte normalisé différent à l'index 12"], repetition=r)
        for r in (1, 2, 3)]
    decision = _decisions(divergent, cas)["parsing_ok_rate"]
    assert _rouge(decision) and decision.value == 0.0 and decision.scope == "suite:parsing"


def test_un_bloc_attendu_non_ouvert_rougit_le_gate() -> None:
    """I/O matrix : `expected_block_ids ⊄ opened_block_ids` ⇒ `blocs_attendus_ouverts_rate < 1.0`.

    `expected_blocks_not_opened` était calculé et publié depuis 4.2b **sans jamais décider** : la
    preuve que le rappel a présenté les blocs attendus au modèle existait, et personne ne la lisait.
    """
    cas = [_cas_sinistre()]
    assert _decisions([_resultat(repetition=r) for r in (1, 2, 3)],
                      cas)["blocs_attendus_ouverts_rate"].status == "green"
    aveugle = [_resultat(repetition=r, opened_block_ids=[]) for r in (1, 2, 3)]
    assert _rouge(_decisions(aveugle, cas)["blocs_attendus_ouverts_rate"])


def test_une_citation_introuvable_rougit_meme_quand_le_cas_lattendait() -> None:
    """I/O matrix : rouge « **même si `mode_attendu` le prévoyait** ».

    C'est le point délicat, et il est voulu : `cases_ok_rate` mesure si le cas a tenu **son**
    attente ; `citations_retrouvees_rate` mesure le **système**. Un golden set qui attend une
    citation introuvable décrit un défaut connu — il ne le rend pas acceptable au gate `full`.
    """
    cas = [_cas_sinistre()]
    resultats = [_resultat(repetition=r, label="citation_introuvable") for r in (1, 2, 3)]
    decision = _decisions(resultats, cas)["citations_retrouvees_rate"]
    assert _rouge(decision) and decision.value == 0.0
    # Le cas, lui, est parfaitement « ok » de son point de vue : aucun écart.
    assert all(r.ok for r in resultats)
    assert _decisions(resultats, cas)["cases_ok_rate"].status == "green"


def test_un_5xx_technique_nest_plus_compte_comme_une_reponse() -> None:
    """I/O matrix : un `Resultat` qui porte `http >= 500` rougit `zero_5xx_technique_rate`.

    Dont la branche `TruncatedRead`, qui produit un `Resultat` **terminé** avec un 503 : sans ce
    témoin, une panne technique et un refus de budget se confondaient dans le même agrégat.
    """
    cas = [_cas_sinistre()]
    assert _decisions([_resultat(repetition=r) for r in (1, 2, 3)],
                      cas)["zero_5xx_technique_rate"].status == "green"
    panne = [_resultat(repetition=1, http=503, label="claim_non_soutenu"),
             _resultat(repetition=2), _resultat(repetition=3)]
    decision = _decisions(panne, cas)["zero_5xx_technique_rate"]
    assert _rouge(decision) and decision.value == pytest.approx(2 / 3, abs=1e-4)
    # Le parsing, local, n'a aucune sémantique HTTP : il ne peut ni verdir ni rougir ce témoin.
    local = _resultat(id="p-cas-neutre", suite="parsing", variant="local", http=None)
    assert runner._sans_5xx_technique(local) is True


def test_un_typage_non_confirme_rougit_le_gate() -> None:
    """I/O matrix : une preuve qui cite un bloc dont `kind_confirmed` est faux ⇒ rouge.

    AD-6/AD-8 : seul un `kind` posé à la main ou vérifié par le modèle est confirmé. Une claim
    décisionnelle appuyée sur une heuristique d'ingestion affirme plus que ce qui est établi.
    """
    cas = [_cas_sinistre()]
    assert _decisions([_resultat(repetition=r) for r in (1, 2, 3)],
                      cas)["typage_confirme_rate"].status == "green"
    devine = [_resultat(repetition=r, proofs=[{"doc_id": DOC, "block_id": f"{DOC}:p1:1",
                                               "kind": "garantie", "quote_hash": "h",
                                               "kind_confirmed": False}]) for r in (1, 2, 3)]
    assert _rouge(_decisions(devine, cas)["typage_confirme_rate"])
    # Une exécution **sans preuve** est vraie par vacuité : elle n'affirme rien qu'un typage
    # devrait soutenir.
    assert runner._typage_confirme(_resultat(proofs=[])) is True


def test_le_typage_confirme_remonte_du_corpus_jusqua_la_preuve() -> None:
    """La chaîne complète : `Block.kind_confirmed` → `_preuves` → décision.

    Sans ce chaînon, le témoin aurait mesuré une valeur que le runner n'aurait jamais renseignée.
    """
    ctx = _contexte(kind_source="manual")
    quote = _quote(ctx)
    preuves = runner._preuves(_reponse([_claim(quote)]), ctx.index)
    assert preuves[0]["kind_confirmed"] is True
    devine = runner._preuves(_reponse([_claim(_quote(_contexte(kind_source=None)))]),
                             _contexte(kind_source=None).index)
    assert devine[0]["kind_confirmed"] is False
    # Le bloc inconnu du corpus n'est pas « confirmé par défaut ».
    inconnu = runner._preuves(_reponse([_claim(VerifiedQuote(
        block_id=f"{DOC}:p9:9", quote="x", start=0, end=1, text_start=0, text_end=1))]), ctx.index)
    assert inconnu[0]["kind_confirmed"] is False


def _attester(data: Path, doc_id: str, ctx: runner.Contexte, *,
              document_hash: str | None = None, structure_hash: str | None = None,
              doc_id_rapport: str | None = None) -> None:
    """Écrit un `report.json` portant l'attestation affirmative de la story 4.5."""
    from server.app.domain.ingest import detail_attestation_structure

    entry = ctx.index.corpus.manifest[doc_id]
    (data / doc_id / "report.json").write_text(json.dumps({
        "doc_id": doc_id_rapport or doc_id,
        "checks": [{"name": "structure_proposee", "level": "info",
                    "detail": detail_attestation_structure(
                        document_hash=document_hash or entry.document_hash,
                        structure_hash=structure_hash or (entry.structure_hash or ""))}],
    }), encoding="utf-8")


def test_la_preuve_de_structure_ne_compte_que_les_documents_issus_dun_pdf(tmp_path: Path) -> None:
    """Revue B4 : le guide n'a aucune **proposition** de structure à prouver, et sort du dénominateur.

    Le témoin était `pipeline: all` : sur le corpus réel il rendait `(0, 1)` pour un document que
    **aucun chemin de production** ne peut doter d'un `structure.json`. Un gate `full` du guide était
    donc définitivement rouge. La règle est celle du loader (`SOURCE_FILES`), sans branche par
    document : ce qu'un document **est** décide, pas son identifiant.
    """
    ctx = _contexte()
    data = tmp_path / "data"
    (data / DOC).mkdir(parents=True)
    # Copie de site : hors dénominateur — le témoin n'a rien à mesurer.
    (data / DOC / "source.js").write_bytes(b"var kb = {};")
    assert _preuve_structure(data, ctx, [DOC]) == (0, 0)
    # Document issu d'un PDF : il entre au dénominateur, et rien ne le prouve encore.
    (data / DOC / "source.js").unlink()
    (data / DOC / "source.pdf").write_bytes(b"%PDF-1.4")
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)


def test_la_structure_nest_prouvee_que_par_une_attestation_rattachee_au_document(
        tmp_path: Path) -> None:
    """Revue B4 : quatre conditions, et l'absence d'une seule suffit à ne pas compter.

    Le fail-open reproduit par la revue : un `structure.json` au contenu **arbitraire**, son hash
    recopié au manifest, **aucun `report.json`** ⇒ la structure passait pour prouvée. `if
    rapport.is_file()` rendait facultative une des trois conditions que la docstring annonçait.
    """
    ctx = _contexte()
    data = tmp_path / "data"
    (data / DOC).mkdir(parents=True)
    (data / DOC / "source.pdf").write_bytes(b"%PDF-1.4")
    entry = ctx.index.corpus.manifest[DOC]

    # 1. Rien de déclaré au manifest : non prouvée.
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)

    # 2. Le fail-open historique : artefact arbitraire, hash recopié, aucun rapport.
    octets = b'{"nimporte": "quoi"}\n'
    (data / DOC / "structure.json").write_bytes(octets)
    entry.structure_hash = hashlib.sha256(octets).hexdigest()
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1), (
        "un artefact sans attestation ne prouve rien")

    # 3. Rapport présent mais illisible : non prouvée.
    (data / DOC / "report.json").write_text("{ pas du json", encoding="utf-8")
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)

    # 4. Rapport lisible mais sans le check : non prouvée.
    (data / DOC / "report.json").write_text(json.dumps({
        "doc_id": DOC, "checks": [{"name": "invariants_arbre", "level": "info", "detail": "ok"}],
    }), encoding="utf-8")
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)

    # 5. Attestation qui ne correspond pas au document (autre `document_hash`) : non prouvée.
    _attester(data, DOC, ctx, document_hash="f" * 64)
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)

    # 6. Attestation qui ne correspond pas à l'artefact (autre `structure_hash`) : non prouvée.
    _attester(data, DOC, ctx, structure_hash="e" * 64)
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)

    # 7. Rapport **étranger** : ses checks ne décrivent pas ce document.
    _attester(data, DOC, ctx, doc_id_rapport="autre-doc")
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)

    # 8. Bloquant de structure : le vérificateur de 4.2c a refusé — jamais prouvée.
    (data / DOC / "report.json").write_text(json.dumps({
        "doc_id": DOC, "checks": [{"name": "structure_proposee", "level": "bloquant",
                                   "detail": "ligne_omise : ..."}],
    }), encoding="utf-8")
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)

    # 9. Enfin : les quatre conditions réunies.
    _attester(data, DOC, ctx)
    assert _preuve_structure(data, ctx, [DOC]) == (1, 1)
    # Et l'artefact qui bouge après coup casse la concordance.
    (data / DOC / "structure.json").write_bytes(octets + b"\n")
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)


def test_la_structure_non_prouvee_rougit_le_gate_et_cest_letat_reel() -> None:
    """I/O matrix : structure non prouvée ⇒ décision rouge chiffrée, jamais neutre."""
    decision = _decisions([_resultat(repetition=r) for r in (1, 2, 3)],
                          [_cas_sinistre(), _cas_parsing()],
                          structure=(0, 1))["structure_prouvee_rate"]
    assert _rouge(decision) and decision.value == 0.0 and decision.scope == "suite:parsing"


# --- revue B4, volet guide : la preuve de structure applicable à une copie de site -----------------

def _attester_arbre(data: Path, doc_id: str, ctx: runner.Contexte, *,
                    document_hash: str | None = None, ingest_fingerprint: str | None = None,
                    doc_id_rapport: str | None = None, niveau: str = "info") -> None:
    """Écrit un `report.json` portant l'attestation d'arbre — telle que l'ingestion l'écrit."""
    from server.app.domain.ingest import detail_attestation_arbre

    entry = ctx.index.corpus.manifest[doc_id]
    (data / doc_id / "report.json").write_text(json.dumps({
        "doc_id": doc_id_rapport or doc_id,
        "checks": [{"name": "invariants_arbre", "level": niveau,
                    "detail": detail_attestation_arbre(
                        document_hash=document_hash or entry.document_hash,
                        ingest_fingerprint=ingest_fingerprint or entry.ingest_fingerprint)}],
    }), encoding="utf-8")


def test_les_deux_perimetres_de_preuve_de_structure_sont_complementaires(tmp_path: Path) -> None:
    """Revue B4 : restreindre `structure_prouvee_rate` aux PDF ne laisse **aucun** périmètre sans exigence.

    C'est le cœur du finding. Restreindre était juste — la story 4.2c ne s'applique pas à une copie
    de site —, mais restreindre **sans remplacer** aurait retiré au guide toute exigence de
    structure : un abaissement du plancher déguisé en correctif de périmètre. Les deux dénominateurs
    partagent donc le lot exactement, par la règle générique `SOURCE_FILES` du loader, et jamais par
    un `doc_id`.
    """
    ctx = _contexte()
    data = tmp_path / "data"
    (data / DOC).mkdir(parents=True)

    # Copie de site : hors du témoin PDF, **dans** le témoin d'arbre.
    (data / DOC / "source.js").write_bytes(b"var kb = {};")
    assert _preuve_structure(data, ctx, [DOC]) == (0, 0)
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 1)

    # Document issu d'un PDF : l'inverse, exactement.
    (data / DOC / "source.js").unlink()
    (data / DOC / "source.pdf").write_bytes(b"%PDF-1.4")
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 0)

    # Aucune source lisible : aucun des deux ne prétend savoir ce que ce document est — et le loader
    # ne le sert pas davantage. Compter un document qu'on ne sait pas lire serait inventer.
    (data / DOC / "source.pdf").unlink()
    assert _preuve_structure(data, ctx, [DOC]) == (0, 0)
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 0)


def test_larbre_nest_prouve_que_par_une_attestation_rattachee_au_document(tmp_path: Path) -> None:
    """Revue B4 : une attestation fabriquée ne verdit rien — trois conditions, aucune facultative.

    Le corpus servi porte aujourd'hui `invariants_arbre: ok` : la forme **historique** du check, une
    déclaration sans empreinte. Si elle suffisait, le témoin serait vert sans qu'aucune ingestion
    n'ait eu lieu — exactement le fail-open que la revue reproche à la preuve de structure PDF, sous
    l'autre périmètre.
    """
    ctx = _contexte()
    data = tmp_path / "data"
    (data / DOC).mkdir(parents=True)
    (data / DOC / "source.js").write_bytes(b"var kb = {};")
    entry = ctx.index.corpus.manifest[DOC]

    # 1. Aucun rapport : rien n'atteste, rien n'est prouvé.
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 1)

    # 2. Rapport illisible.
    (data / DOC / "report.json").write_text("{ pas du json", encoding="utf-8")
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 1)

    # 3. **La forme historique** : `invariants_arbre: ok`, sans empreinte. Une déclaration.
    (data / DOC / "report.json").write_text(json.dumps({
        "doc_id": DOC, "checks": [{"name": "invariants_arbre", "level": "info", "detail": "ok"}],
    }), encoding="utf-8")
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 1), (
        "un `invariants_arbre: ok` sans empreinte ne prouve rien")

    # 4. Attestation qui nomme un autre arbre.
    _attester_arbre(data, DOC, ctx, document_hash="f" * 64)
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 1)

    # 5. Attestation qui nomme une autre ingestion.
    _attester_arbre(data, DOC, ctx, ingest_fingerprint="e" * 64)
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 1)

    # 6. Rapport étranger : ses checks ne décrivent pas ce document.
    _attester_arbre(data, DOC, ctx, doc_id_rapport="autre-doc")
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 1)

    # 7. Arbre **refusé** : un bloquant n'est jamais une preuve, même s'il porte les deux empreintes.
    _attester_arbre(data, DOC, ctx, niveau="bloquant")
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 1)

    # 8. Les trois conditions réunies.
    _attester_arbre(data, DOC, ctx)
    assert _preuve_arbre(data, ctx, [DOC]) == (1, 1)

    # 9. Et le manifest qui bouge — réingestion, ou `document.json` remplacé — détache l'attestation.
    entry.document_hash = "9" * 64
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 1)


def test_larbre_non_prouve_rougit_le_gate_du_guide() -> None:
    """Revue B4 : le témoin de périmètre guide décide, chiffré, avec son scope propre.

    Et il décide **par vacuité** aussi : un lot guide dont le dénominateur est vide n'émet aucune
    décision, et la fermeture de `construire_decisions` la rend rouge `n=0, value=0.0`. Un gate
    `full` du guide ne peut donc pas devenir vert en n'ayant simplement rien à mesurer.
    """
    cas = [_cas_guide()]
    resultats = [_resultat(id="g-cas-neutre", suite="guide", variant="micro", repetition=r,
                           verdict=None, proofs=[]) for r in (1, 2, 3)]
    rouge = _decisions(resultats, cas, arbre=(0, 1))["arbre_prouve_rate"]
    assert _rouge(rouge) and rouge.value == 0.0 and rouge.scope == "suite:guide"
    vert = _decisions(resultats, cas, arbre=(1, 1))["arbre_prouve_rate"]
    assert vert.status == "green" and vert.value == 1.0
    # Dénominateur vide : rien n'est mesuré, donc rien n'est prouvé — pas un vert par absence.
    vide = _decisions(resultats, cas, arbre=(0, 0))["arbre_prouve_rate"]
    assert vide.status == "red" and vide.n == 0 and vide.value == 0.0


def test_une_claim_decisionnelle_instable_rougit_sans_toucher_a_la_signature_figee() -> None:
    """AC 5 : les N répétitions divergent sur le prédicat ⇒ `stabilite_claim_decisionnelle < 1.0`.

    Et — c'est la moitié qui compte autant — `_signature_stabilite` n'est **pas** touchée : son
    numérateur est écrit dans le plancher importé, et y ajouter le prédicat aurait changé le sens
    d'un témoin déjà pré-enregistré. Les deux témoins se lisent séparément.
    """
    cas = [_cas_sinistre()]
    stables = [_resultat(repetition=r, decision_claim=True) for r in (1, 2, 3)]
    assert _decisions(stables, cas)["stabilite_claim_decisionnelle"].status == "green"
    instables = [_resultat(repetition=1, decision_claim=True),
                 _resultat(repetition=2, decision_claim=False),
                 _resultat(repetition=3, decision_claim=True)]
    decision = _decisions(instables, cas)["stabilite_claim_decisionnelle"]
    assert _rouge(decision) and decision.value == 0.0 and decision.scope == "suite:sinistre"
    # La signature de `stabilite_sinistre` ne voit pas le prédicat : les trois répétitions restent
    # stables à ses yeux, et le témoin importé garde exactement le sens qu'il avait.
    assert {_signature(r) for r in instables} == {_signature(stables[0])}
    assert _decisions(instables, cas)["stabilite_sinistre"].status == "green"
    # Une répétition manquante est une interruption : rouge, jamais retirée du dénominateur.
    partielles = instables[:2]
    assert _rouge(_decisions(partielles, cas,
                             non_executes=["s-cas-neutre#r3"])["stabilite_claim_decisionnelle"])


def test_les_deux_gardes_orchestrateur_absentes_sont_deux_rouges_nommes() -> None:
    """I/O matrix : « deux décisions rouges "témoin orchestrateur applicable absent" ».

    Anti-rustine et métamorphique sont mesurées **hors** de ce runner (elles portent sur le dépôt).
    Le gate ne peut pas les simuler ; il peut refuser d'être vert sans elles, et c'est ce qu'il fait.
    """
    decisions = _decisions([_resultat(repetition=r) for r in (1, 2, 3)], [_cas_sinistre()])
    for metric in ("anti_rustine_pass_rate", "metamorphique_pass_rate"):
        d = decisions[metric]
        assert d.status == "red" and d.n == 0 and d.value == 0.0
        assert d.reason == "témoin orchestrateur applicable absent de ce run"


def test_les_dix_temoins_manquants_sont_rouges_et_pas_neutres() -> None:
    """Boundaries : « une preuve absente est **rouge**, jamais neutre ».

    Un gate `full` qui ne mesure rien du tout produit dix rouges chiffrés, pas dix silences —
    c'est la fermeture par vacuité, et c'est ce qui rend ces témoins impossibles à oublier.

    Le lot porte les **trois** suites, pour que les dix soient applicables d'un coup : les deux
    preuves de structure ont des périmètres complémentaires, et un lot qui n'en couvrirait qu'un
    laisserait l'autre témoin hors du contrôle sans que rien ne le dise.
    """
    cas = [_cas_sinistre(), _cas_parsing(), _cas_guide()]
    assert _temoins_gate_full_applicables(cas) == set(DIX)
    manquantes = [f"{c.id}#r{r}" for c in cas for r in (1, 2, 3)]
    decisions = _decisions([], cas, structure=None, arbre=None, non_executes=manquantes)
    for metric in DIX:
        assert decisions[metric].status == "red", metric
        assert decisions[metric].value == 0.0, metric
    # Et rien de vert nulle part : un run qui n'a rien mesuré ne peut pas produire un gate vert.
    assert all(d.status == "red" for d in decisions.values())


# --- AC 1 : un gate vert exige que tout soit vert --------------------------------------------------

def test_evals_ok_est_la_conjonction_et_une_seule_cause_suffit() -> None:
    """AC 2 : `evals_ok=False` pour **n'importe laquelle** des neuf causes.

    On construit un lot où tout est vert sauf un témoin à la fois, et on vérifie que chacun, seul,
    fait basculer la conjonction. Un test qui ne rougirait qu'en cassant tout ne prouverait rien.
    """
    cas = [_cas_sinistre(), _cas_parsing()]
    base = [_resultat(repetition=r) for r in (1, 2, 3)] + [
        _resultat(id="p-cas-neutre", suite="parsing", variant="local", http=None, repetition=r)
        for r in (1, 2, 3)]
    causes: dict[str, list[runner.Resultat]] = {
        "parsing_ok_rate": [r for r in base if r.suite != "parsing"] + [
            _resultat(id="p-cas-neutre", suite="parsing", variant="local", http=None,
                      label="parsing", ecarts=["écart"], repetition=r) for r in (1, 2, 3)],
        "blocs_attendus_ouverts_rate": [
            r.__class__(**{**r.__dict__, "opened_block_ids": []}) for r in base],
        "citations_retrouvees_rate": [
            r.__class__(**{**r.__dict__, "label": "citation_introuvable"}) for r in base],
        "zero_5xx_technique_rate": [
            r.__class__(**{**r.__dict__, "http": 503}) for r in base],
        "typage_confirme_rate": [
            r.__class__(**{**r.__dict__, "proofs": [
                {"doc_id": DOC, "block_id": f"{DOC}:p1:1", "kind": "garantie",
                 "quote_hash": "h", "kind_confirmed": False}]}) for r in base],
    }
    for metric, resultats in causes.items():
        decisions = _decisions(resultats, cas)
        assert decisions[metric].status == "red", metric
        assert not all(d.status == "green" for d in decisions.values()), metric


# --- les gardes de `full` : refus **avant tout appel**, code 2 -------------------------------------

def test_un_gate_full_sans_n_refuse_avant_tout_appel(tmp_path: Path, capsys: Any,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """I/O matrix : `--gate X --profile full --repeat 1` ⇒ refus, code 2, message chiffré."""
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "1",
                 "--candidate-revision", REVISION])
    assert code == 2
    err = capsys.readouterr().err
    assert "--repeat >= 3" in err and "n_minimum" in err
    _manifest_intact(tmp_path)


def test_un_gate_full_sans_revision_refuse_avant_tout_appel(tmp_path: Path, capsys: Any,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """I/O matrix : sans `--candidate-revision`, refus avant tout appel, code 2."""
    code = _cli(tmp_path, monkeypatch, ["--gate", DOC, "--profile", "full", "--repeat", "3"])
    assert code == 2
    assert "--candidate-revision" in capsys.readouterr().err
    _manifest_intact(tmp_path)


def test_une_revision_mal_formee_est_refusee(tmp_path: Path, capsys: Any,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", "pas-un-sha"])
    assert code == 2
    assert "40 caractères hexadécimaux" in capsys.readouterr().err


def test_une_preuve_trusted_sans_rapport_est_refusee(tmp_path: Path, capsys: Any,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """`--orchestrator-report` est obligatoire dès que `--orchestrator-evidence` est donné (M2)."""
    preuve = tmp_path / "preuve.json"
    preuve.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LIVE_CAMPAIGN_ID", "campagne-de-test")
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION, "--producer", "orchestrator",
                 "--max-cost", "1.0", "--series-kind", "final", "--series-id", "s",
                 "--orchestrator-evidence", str(preuve)])
    assert code == 2
    assert "--orchestrator-report" in capsys.readouterr().err


def test_les_gardes_full_ne_sarment_pas_sur_un_profil_sans_gate(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 6 : la CI lance `--profile full` **sans** `--gate` ni `--repeat` — rien ne se déclenche.

    C'est la Design Note : exiger `--repeat >= 3` sur le profil seul rendrait la CI rouge à chaque
    PR pour une raison qui n'a rien à voir avec le candidat.
    """
    assert _cli(tmp_path, monkeypatch, ["--profile", "full", "--dry-run"]) == 0
    assert _cli(tmp_path, monkeypatch, ["--profile", "full", "--repeat", "1", "--dry-run"]) == 0


# --- AC 1 : la suite `parsing` entre dans le lot sous `full` ---------------------------------------

def test_la_suite_parsing_du_document_entre_dans_le_gate_full(tmp_path: Path) -> None:
    """AC 1 : « cas `vertical`+`full` de la suite de X **et** ses cas `parsing` ».

    Sous `vertical`, le périmètre est inchangé — c'est ce qui garde les gates historiques
    comparables. Sous `full`, `cases_hash` change, et c'est exactement ce que `cases_hash` existe
    pour dire : deux profils, deux périmètres, deux hashes.
    """
    cases = _cases_dir(tmp_path)
    reglages = _settings()
    assert runner.suites_du_gate(reglages, DOC, "vertical", cases_dir=cases) == ("sinistre",)
    assert runner.suites_du_gate(reglages, DOC, "full", cases_dir=cases) == (
        "sinistre", f"parsing/{DOC}")
    # Un document sans suite `parsing` ne s'en invente pas une.
    autre = _cases_dir(tmp_path / "sans-parsing", parsing=False)
    assert runner.suites_du_gate(reglages, DOC, "full", cases_dir=autre) == ("sinistre",)


# --- AC 2 : un candidat rouge n'écrit rien sur le dernier vert -------------------------------------

def test_un_candidat_rouge_laisse_le_manifest_byte_identique(tmp_path: Path, capsys: Any) -> None:
    """AC 2 : `data/manifest.json` **byte-identique**, la publication écrite quand même, refus dit.

    C'est la promesse de 4.2b, reconduite mot pour mot : un rouge est un **résultat**, publié dans
    le rapport ; le dernier vert — gate, révision, trafic — reste intact. `ecrire_gate` est le seul
    écrivain de `data/manifest.json`, et il rend `False` sans toucher le disque.
    """
    chemin = tmp_path / "manifest.json"
    vert = {DOC: {"status": "servi", "source_hash": "s", "ingest_fingerprint": "f",
                  "document_hash": "d", "edition": "2020", "overlay_hash": None,
                  "gate": {"profile": "full", "source_hash": "s", "ingest_fingerprint": "f",
                           "overlay_hash": None, "cases_hash": "c", "cases": 1,
                           "countersigned": False, "pipeline_digest": "p", "prompts_digest": "q",
                           "model_ids": {}, "evals_ok": True, "date": "2026-08-28",
                           "run_digest": "a" * 64, "plancher_digest": "b" * 64,
                           "candidate_revision": REVISION, "report_digest": "c" * 64,
                           "decisions": [{"metric": "m", "producer": "orchestrator",
                                          "threshold": 1.0, "scope": "run", "n": 3,
                                          "run_digest": "a" * 64, "value": 1.0,
                                          "status": "green"}]}}}
    chemin.write_text(json.dumps(vert, indent=2) + "\n", encoding="utf-8")
    avant = chemin.read_bytes()
    entry = ManifestEntry.model_validate(vert[DOC])
    rouge = runner.construire_gate(
        entry, _contexte(), profil="full", cas=[_cas_sinistre()],
        cases_dir=_cases_dir(tmp_path), evals_ok=False,
        snapshot=runner.CasesSnapshot(cases_hash="h"),
        plancher_digest="d" * 64, candidate_revision="1" * 40, report_digest="e" * 64,
        run_digest="f" * 64)
    assert runner.ecrire_gate(chemin, DOC, rouge) is False
    assert chemin.read_bytes() == avant
    assert "gate candidat rouge" in capsys.readouterr().err


# --- I/O matrix : le reste ------------------------------------------------------------------------

def test_un_run_incomplet_ne_peut_jamais_produire_un_vert() -> None:
    """I/O matrix : soit `executions_completes < 1.0`, soit code 3 **sans gate écrit**."""
    cas = [_cas_sinistre()]
    partiel = _decisions([_resultat(repetition=1)], cas, non_executes=["s-cas-neutre#r2",
                                                                      "s-cas-neutre#r3"])
    assert _rouge(partiel["executions_completes"])
    assert not all(d.status == "green" for d in partiel.values())


def test_un_200_partiel_nest_jamais_bonne_reponse() -> None:
    """I/O matrix : `answer.lecture_partielle is not None` ⇒ `claim_non_soutenu` (4.2f conservé).

    La story 4.5 fait remonter le prédicat décisionnel dans `Resultat` ; elle ne touche pas à la
    précédence des labels, dont ce cas fait partie depuis 4.2f.
    """
    ctx = _contexte()
    cas = _cas_sinistre()
    # Story 4.2f : `found=False` porte **exactement un** état — une preuve d'absence, **ou** une
    # lecture partielle. C'est cette seconde qui décrit le 200 partiel.
    reponse = Answer(found=False, complete=False, texte="", unknown=["lecture_bornee"],
                     lecture_partielle=LecturePartielle(nodes_read=1, blocks_read=2,
                                                        documents=[DOC]))
    label, ecarts = runner.juger(cas, reponse, doc_id=DOC, index=ctx.index)
    assert label == "claim_non_soutenu"
    assert any("lecture partielle" in e for e in ecarts)
    # Aucune claim ne survit : le prédicat décisionnel est faux, et il est **publié** comme tel.
    assert runner.predicat_decisionnel(reponse, ctx.index) is False


def test_les_trois_reserves_ne_bloquent_aucune_decision() -> None:
    """AC 3 : `countersigned`, `validated_by_expert` et `dictionary.validated` tous faux ⇒ rien ne
    change au verdict, et le refus « zéro hit » reste désarmé.

    Les rendre bloquantes ferait refuser de servir pour une signature manquante ; les taire ferait
    affirmer une relecture qui n'a pas eu lieu. Elles sont donc publiées, et publiées seulement.
    """
    cas = [_cas_sinistre()]
    resultats = [_resultat(repetition=r) for r in (1, 2, 3)]
    decisions = _decisions(resultats, cas, structure=(1, 1))
    # Aucune métrique du plancher ne nomme une réserve : ce sont des dettes dites, pas des mesures.
    assert not any(mot in d.metric for d in decisions.values()
                   for mot in ("countersign", "expert", "dictionary"))
    charge = charger_plancher()
    assert not any(mot in t.metric for t in charge.plancher.temoins
                   for mot in ("countersign", "expert", "dictionary"))
    gate = runner.construire_gate(
        ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                      document_hash="d", edition="2020"),
        _contexte(), profil="full", cas=cas, cases_dir=Path("/inexistant"), evals_ok=True,
        snapshot=runner.CasesSnapshot(cases_hash="h"), plancher_digest="a" * 64,
        candidate_revision=REVISION, report_digest="b" * 64)
    assert gate.countersigned is False and gate.evals_ok is True
    reserves = runner.reserves_du_lot(cas, dictionary_validated=False)
    assert (reserves.countersigned, reserves.validated_by_expert,
            reserves.dictionary_validated) == (False, False, False)
    # La règle de `countersigned` est **la même** que celle qu'écrit `construire_gate` : une seule
    # dérivation, sans quoi le gate et le résumé de CI auraient pu se contredire.
    assert reserves.countersigned == gate.countersigned


# --- outillage local ------------------------------------------------------------------------------

def _signature(r: runner.Resultat) -> str:
    return json.dumps(runner._signature_stabilite(r), sort_keys=True)


def _quote(ctx: runner.Contexte) -> VerifiedQuote:
    texte = ctx.index.corpus.documents[DOC].block(f"{DOC}:p1:1").text
    extrait = texte[:20]
    return VerifiedQuote(block_id=f"{DOC}:p1:1", quote=extrait, start=0, end=len(extrait),
                         text_start=0, text_end=len(extrait))


def _claim(quote: VerifiedQuote) -> VerifiedClaim:
    return VerifiedClaim(claim_id="c1", text="Une affirmation.", quotes=[quote],
                         status=ClaimStatus(retrouvee=True, pertinente=True, applicable="oui",
                                            edition="2020"))


def _reponse(claims: list[VerifiedClaim]) -> Answer:
    return Answer(found=True, complete=True, texte="Une affirmation.",
                  segments=[AnswerSegment(text="Une affirmation.", kind="factuel",
                                          claim_ids=[c.claim_id for c in claims])],
                  claims=claims, verdict=Verdict(value="sous_conditions", reason="r"))


CAS_SINISTRE_YAML = """
id: s-cas-neutre
suite: sinistre
profile: vertical
question: "Cette situation entre-t-elle dans la garantie decrite ?"
faits:
  description: "Un bien decrit au contrat a subi une atteinte."
expected:
  found: true
  block_ids: ["{doc}:p1:1"]
  verdict: [sous_conditions, ne_tranche_pas]
mode_attendu: bonne_reponse
truth:
  source: lecture_humaine
  validated_by_expert: false
  note: "relu"
"""

CAS_PARSING_YAML = """
id: p-cas-neutre
suite: parsing
profile: full
question: "Le texte du bloc est-il celui du contrat imprime ?"
famille: garantie
expected:
  found: true
  block_ids: ["{doc}:p1:1"]
  text_norm: "{texte}"
mode_attendu: bonne_reponse
truth:
  source: lecture_humaine
  validated_by_expert: false
  note: "relu"
"""


def _cases_dir(racine: Path, *, parsing: bool = True) -> Path:
    cases = racine / "cases"
    (cases / "sinistre").mkdir(parents=True, exist_ok=True)
    (cases / "sinistre" / "s-cas-neutre.yaml").write_text(
        CAS_SINISTRE_YAML.format(doc=DOC), encoding="utf-8")
    if parsing:
        (cases / "parsing" / DOC).mkdir(parents=True, exist_ok=True)
        (cases / "parsing" / DOC / "p-cas-neutre.yaml").write_text(
            CAS_PARSING_YAML.format(doc=DOC, texte=normalize(TEXTE)), encoding="utf-8")
    return cases


def _data_dir(racine: Path, *, source: str | None = "source.pdf") -> Path:
    """Le `data/` d'un document servi. `source` dit ce que ce document **est** (règle `SOURCE_FILES`).

    Le défaut est `source.pdf` : `DOC` est le contrat que la suite `sinistre` sert, et un document
    servi porte sa source sur le disque. `source=None` reproduit le checkout frais — sources
    téléchargées au build, `.gitignore` —, que la revue C fait refuser sous `full`.
    """
    data = racine / "data"
    dossier = data / DOC
    dossier.mkdir(parents=True, exist_ok=True)
    if source is not None:
        (dossier / source).write_bytes(OCTETS_SOURCE[source])
    doc = _document(source_hash=_source_hash(source))
    octets = json.dumps(doc.model_dump(mode="json", exclude_defaults=True), ensure_ascii=False,
                        sort_keys=True).encode("utf-8")
    (dossier / "document.json").write_bytes(octets)
    (dossier / "summary.md").write_text("# doc", encoding="utf-8")
    # **Idempotent** : un manifest déjà posé n'est pas réécrit. Un test qui installe un gate vert
    # puis lance le runner doit retrouver ce gate — le recréer ici effacerait précisément l'état
    # que la non-mutation du dernier vert est censée protéger.
    if not (data / "manifest.json").is_file():
        (data / "manifest.json").write_text(json.dumps({
            DOC: {"status": "servi", "source_hash": _source_hash(source),
                  "ingest_fingerprint": "f",
                  "document_hash": hashlib.sha256(octets).hexdigest(), "edition": "2020",
                  "overlay_hash": None, "gate": None}}, indent=2) + "\n", encoding="utf-8")
    # **La disposition de publication est posée ici, hors de tout run** (story 4.5, B7) : la bascule
    # refuse une cible que le pointeur unique ne résout pas, et ne l'installe jamais elle-même.
    poser_espace(racine, data_dir=data)
    return data


def _cli(racine: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], *,
         cases_parsing: bool = True, revision: str | None = REVISION,
         source: str | None = "source.pdf",
         sales: list[str] | None = None) -> int:
    """Lance le runner sur un `data/` et des cas synthétiques.

    La **révision réellement exécutée** est doublée : `revision_executee` interroge `git` sur le
    dépôt produit, et un arbre de travail en cours de modification ferait refuser tous ces runs pour
    une raison étrangère à ce qu'ils mesurent. La fonction elle-même est éprouvée séparément, sur un
    vrai dépôt temporaire (`test_la_revision_executee_*`) : c'est l'environnement qu'on double ici,
    jamais la règle.
    """
    data = _data_dir(racine, source=source)
    cases = _cases_dir(racine, parsing=cases_parsing)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", _settings)
    monkeypatch.setattr(runner, "revision_executee",
                        lambda _racine, **_kw: (revision, list(sales or [])))
    return runner.main(argv + ["--cases-dir", str(cases), "--data-dir", str(data)])


def _manifest_intact(racine: Path) -> None:
    manifest = json.loads((racine / "data" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[DOC]["gate"] is None, "un refus de tourner ne touche jamais le manifest"


def _temporaires(racine: Path) -> list[str]:
    """Tous les temporaires laissés sous `racine` — **quelle que soit la convention de nommage**.

    Le glob des tours précédents était `rglob(".*.tmp")`, avec point initial obligatoire : il ne
    correspondait qu'à l'un des écrivains, si bien que l'assertion « aucun temporaire » ne pouvait
    pas échouer sur les chemins frères. La sonde porte donc sur le **suffixe**, que `tempfile.mkstemp`
    reçoit identiquement partout, et elle balaie tout l'arbre, bundle de publication compris.
    """
    return sorted(str(p.relative_to(racine)) for p in racine.rglob("*")
                  if p.is_file() and p.name.endswith(".tmp"))


def _quality_evidence(path: Path, *, red_row: str | None = None) -> None:
    registry = canonical_registry()
    rows = []
    for row_id in ROW_IDS:
        key = BranchKey(
            row_id=row_id, trigger_branch="TRUE",
            t2_eligibility_mode="ISOLATED" if row_id == "T-01" else "NONE",
        )
        refs = canonical_required_refs(key)
        requires_live = any(registry[ref].evidence_class == "LIVE" for ref in refs)
        rows.append(ClosureInput(
            branch_key=key, trigger="TRUE",
            tests=("RED",) if row_id == red_row else ("GREEN",),
            live_mode="REQUIRED" if requires_live else "N_A",
            live_branch="SATISFIED_LIVE" if requires_live else "SATISFIED_NO_LIVE",
            live_justification=None if requires_live else "preuve hermétique",
            gate_rule="TRUE", provided_ref_list=tuple(sorted(refs)), required_refs=refs,
            evidence_status={ref: EvidenceStatus.RESOLVED for ref in refs},
            evidence_artifacts={
                ref: EvidenceArtifact.create(
                    ref=ref, gate_uid="gate-quality-main", run_uid="run-quality-main",
                    source=("LIVE_ORCHESTRATOR"
                            if registry[ref].evidence_class == "LIVE"
                            else "HERMETIC_RUNNER"),
                    payload=f"preuve:{ref}",
                ) for ref in refs
            },
            registry=registry, registry_hash=canonical_registry_hash(registry),
            hermetic_selection="NON_EMPTY",
        ))
    value = QualityClosureRunInput(dependencies=DEPENDENCIES, rows=tuple(rows))
    path.write_text(json.dumps(value.model_dump(mode="json")), encoding="utf-8")


def _etat_du_lot(cibles: list[Path]) -> dict[str, tuple[bytes | None, int | None]]:
    """Contenu **et** type d'entrée (`lstat`) de chaque cible d'un lot — l'état observable entier.

    L'interdiction 7 définit « modifiée » par l'état observable : contenu, type d'entrée, présence ou
    absence. Comparer les seuls contenus laisserait passer une migration de type, qui est exactement
    la substitution qu'un candidat précédent avait employée pour paraître tout-ou-rien.
    """
    etat: dict[str, tuple[bytes | None, int | None]] = {}
    for cible in cibles:
        try:
            octets: bytes | None = cible.read_bytes()
        except OSError:
            octets = None
        try:
            marque: int | None = os.lstat(cible).st_mode >> 12
        except OSError:
            marque = None
        etat[str(cible)] = (octets, marque)
    return etat


# --- de bout en bout : un gate `full` réel, hors réseau -------------------------------------------

def test_un_gate_full_de_bout_en_bout_ecrit_ses_decisions_et_publie(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 1 + AC 2 + FR41, sur le chemin complet : sélection, décisions, gate, publication.

    C'est le seul test qui traverse `main()` de bout en bout sous `--gate X --profile full` : il
    prouve que les morceaux se tiennent — la suite `parsing` entre dans le lot, les neuf témoins
    produisent leurs décisions chiffrées, le gate porte son protocole/sa révision/son rapport, et la
    publication est écrite **alors même que le verdict est rouge**.

    Le verdict *est* rouge, et c'est l'état attendu : un run de builder n'est jamais probant (règle
    trusted), et les deux gardes de dépôt ne sont pas fournies. Publier ne promeut rien.
    """
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION])
    # Rouge : un run de builder n'est pas une preuve, et les gardes de dépôt manquent.
    assert code == 1

    manifest = json.loads((tmp_path / "data" / "manifest.json").read_text(encoding="utf-8"))
    gate = manifest[DOC]["gate"]
    # AC 1 : `manifest.gate` porte le profil, le plancher, la révision et le rapport.
    assert gate["profile"] == "full"
    assert gate["plancher_digest"] == charger_plancher().digest
    assert gate["candidate_revision"] == REVISION
    assert len(gate["report_digest"]) == 64
    assert gate["evals_ok"] is False
    # La suite `parsing` du document est bien dans le lot : deux cas, pas un.
    assert gate["cases"] == 2
    # Chaque décision porte les huit champs, et les neuf témoins de la story y sont.
    metriques = {d["metric"] for d in gate["decisions"]}
    # Les témoins que **ce lot** rend applicables, tirés du plancher : le document est issu d'un PDF,
    # donc `structure_prouvee_rate` l'oppose, et `arbre_prouve_rate` — le pendant guide — ne
    # s'applique pas. Les deux périmètres sont complémentaires, jamais cumulés.
    attendus = _temoins_gate_full_applicables([_cas_sinistre(), _cas_parsing()])
    assert attendus <= metriques
    assert "arbre_prouve_rate" not in attendus and "structure_prouvee_rate" in attendus
    for d in gate["decisions"]:
        assert set(d) == {"metric", "producer", "threshold", "scope", "n", "run_digest", "value",
                          "status", "reason"}
    # `evals_ok` est la conjonction, et rien d'autre.
    assert gate["evals_ok"] == all(d["status"] == "green" for d in gate["decisions"])

    # FR41 : publié quand même, sur ses deux faces, et appendu au rapport lu par la CI.
    publie = tmp_path / "data" / "evals-latest.json"
    lisible = tmp_path / "docs" / "evals" / "latest.md"
    assert publie.is_file() and lisible.is_file()
    charge = json.loads(publie.read_text(encoding="utf-8"))
    assert charge["evals_ok"] is False and charge["candidate_revision"] == REVISION
    assert charge["plancher_digest"] == gate["plancher_digest"]
    assert charge["limites"], "un run rouge publie ses limites"
    rendu = lisible.read_text(encoding="utf-8")
    assert "Avertissement non expert" in rendu.splitlines()[2]
    assert rendu in (tmp_path / "eval-results.md").read_text(encoding="utf-8")

    # Et la publication du **dépôt** n'a pas été touchée : le run écrit sous son `--data-dir`.
    assert not (tmp_path / "docs" / "evals" / "latest.md").samefile(
        runner.REPO_ROOT / "docs" / "evals" / "latest.md")


def test_main_full_propage_quality_closure_rouge_au_rapport_et_au_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La preuve n'est pas un helper isolé : `_main` la charge et elle ferme le gate publié."""
    evidence = tmp_path / "quality-evidence.json"
    _quality_evidence(evidence, red_row="P-01")
    monkeypatch.setenv("LIVE_CAMPAIGN_ID", "campaign-quality-main")
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    green = runner.GateDecision(
        metric="preuve_interne", producer="builder", threshold=1.0, scope="run",
        n=3, run_digest="a" * 64, value=1.0, status="green",
    )
    monkeypatch.setattr(
        runner, "construire_decisions",
        lambda *_a, **kw: [green.model_copy(update={"run_digest": kw["run_digest"]})],
    )

    code = _cli(tmp_path, monkeypatch, [
        "--gate", DOC, "--profile", "full", "--repeat", "3",
        "--candidate-revision", REVISION, "--producer", "orchestrator",
        "--series-kind", "final", "--series-id", "quality-main", "--max-cost", "1",
        "--quality-evidence", str(evidence),
    ])

    assert code == 1
    report = json.loads((tmp_path / "eval-results.json").read_text(encoding="utf-8"))
    closure = report["quality_closure"]
    assert closure["complete_input"] is True and closure["v05_gate"] is False
    assert closure["rows"]["P-01"]["gate"] is False
    manifest = json.loads((tmp_path / "data" / "manifest.json").read_text(encoding="utf-8"))
    gate = manifest[DOC]["gate"]
    decisions = {decision["metric"]: decision for decision in gate["decisions"]}
    assert decisions["preuve_interne"]["status"] == "green"
    assert decisions["v05_quality_closure"]["status"] == "red"
    assert gate["evals_ok"] is False


def test_un_second_gate_full_rouge_ne_touche_pas_un_vert_existant(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 2, sur le chemin complet : manifest **byte-identique**, publication écrite quand même."""
    data = _data_dir(tmp_path)
    brut = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    brut[DOC]["gate"] = {
        "profile": "full", "source_hash": brut[DOC]["source_hash"], "ingest_fingerprint": "f",
        "overlay_hash": None,
        "cases_hash": "c", "cases": 2, "countersigned": False, "pipeline_digest": "p",
        "prompts_digest": "q", "model_ids": {}, "evals_ok": True, "date": "2026-08-28",
        "run_digest": "a" * 64, "plancher_digest": "b" * 64, "candidate_revision": "1" * 40,
        "report_digest": "c" * 64,
        "decisions": [{"metric": "m", "producer": "orchestrator", "threshold": 1.0, "scope": "run",
                       "n": 3, "run_digest": "a" * 64, "value": 1.0, "status": "green"}],
    }
    (data / "manifest.json").write_text(json.dumps(brut, indent=2) + "\n", encoding="utf-8")
    avant = (data / "manifest.json").read_bytes()

    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    assert (data / "manifest.json").read_bytes() == avant
    assert (data / "evals-latest.json").is_file(), "la publication est écrite même sur un rouge"


def _double_sinistre() -> Any:
    """Un double du pipeline sinistre : une réponse conforme, une trace à la variante servie.

    Aucun réseau, aucune clé — mais un coût simulé, pour que le plafond de run se mesure sur autre
    chose qu'un compteur nul.
    """
    async def _sinistre(*args: Any, **kw: Any) -> Any:
        budget = kw.get("budget")
        if budget is not None:
            budget.cost_eur = round(budget.cost_eur + 0.01, 4)
        ctx = _contexte()
        return _reponse([_claim(_quote(ctx))]), Trace(
            request_id="eval", pipeline="sinistre",
            variant=runner.DEFAUT_PAR_SUITE["sinistre"], total_cost_eur=0.01,
            steps=[StepTrace(name="comprendre", tier="micro", calls=[],
                             opened_block_ids=[f"{DOC}:p1:1"])])

    return _sinistre


# --- revue 4.5 : les correctifs, épinglés ---------------------------------------------------------

def test_un_document_pdf_sans_cas_parsing_ferme_le_gate_full(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue P2 : un gate `full` ne peut pas être vert sans la moindre preuve de parsing.

    `parsing_ok_rate` a un scope `suite` : sans cas `parsing` dans le lot, le témoin n'était pas
    applicable, aucune décision rouge n'était émise, et le gate pouvait verdir sur la première
    condition rouge de l'AC sans l'avoir mesurée. Le lot est mal composé — c'est une faute d'appel,
    refusée **avant tout appel**, et non une mesure.
    """
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION], cases_parsing=False)
    assert code == 2
    err = capsys.readouterr().err
    assert "ingéré depuis un PDF" in err and "cas `parsing`" in err
    _manifest_intact(tmp_path)


def test_un_lot_qui_narme_aucune_preuve_de_structure_ferme_le_gate_full(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue B4 : la garde de composition couvre **les deux** périmètres, pas seulement le parsing.

    Restreindre `structure_prouvee_rate` aux documents issus d'un PDF a ouvert la même faille que P2,
    de l'autre côté : un document non-PDF mesuré par une suite qui n'arme pas `arbre_prouve_rate`
    n'aurait opposé **aucune** exigence de structure, et un gate `full` aurait pu verdir sans en avoir
    prouvé la moindre. La question posée est unique — « le témoin qui couvre ce document est-il armé
    par ce lot ? » —, et sa réponse négative est un refus avant tout appel.
    """
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION], cases_parsing=False, source="source.js")
    assert code == 2
    err = capsys.readouterr().err
    assert "arbre_prouve_rate" in err and "n'est pas ingéré depuis un PDF" in err
    _manifest_intact(tmp_path)


def test_un_document_sans_pdf_reste_inchange(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Une copie de site (`source.js`) n'a pas d'extraction à prouver : rien ne change pour elle.

    C'est la moitié qui rend la fermeture générique plutôt qu'arbitraire : la règle porte sur ce
    qu'un document **est**, pas sur son identifiant.
    """
    # La règle est celle du loader, rejouée à l'identique : la **première** source présente dans
    # `SOURCE_FILES` fait foi — `source.js` avant `source.pdf`.
    sources = tmp_path / "sources"
    (sources / DOC).mkdir(parents=True)
    assert runner.document_parse_depuis_un_pdf(sources, DOC) is False  # aucune source
    (sources / DOC / "source.js").write_bytes(b"var kb = {};")
    assert runner.document_parse_depuis_un_pdf(sources, DOC) is False
    (sources / DOC / "source.pdf").write_bytes(b"%PDF-1.4")
    assert runner.document_parse_depuis_un_pdf(sources, DOC) is False  # `source.js` d'abord
    (sources / DOC / "source.js").unlink()
    assert runner.document_parse_depuis_un_pdf(sources, DOC) is True

    # Et sur le chemin complet : sans PDF, la garde du **parsing** ne se déclenche pas — un gate
    # `vertical` sans cas `parsing` tourne exactement comme avant, et écrit son gate (rouge, faute
    # de preuve orchestrateur). Les exigences de structure, elles, ne s'arment que sous `full` :
    # c'est `test_un_lot_qui_narme_aucune_preuve_de_structure_ferme_le_gate_full` qui couvre ce
    # second versant.
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "vertical", "--repeat", "3"],
                cases_parsing=False, source="source.js") == 1
    manifest = json.loads((tmp_path / "data" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[DOC]["gate"] is not None


def test_orchestrator_report_seul_est_refuse(tmp_path: Path, capsys: Any,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue P16 : `--orchestrator-report` sans `--orchestrator-evidence` ne veut rien dire.

    Il n'est pas inoffensif : un opérateur qui le passe seul croit avoir fourni une preuve liée
    alors qu'aucune n'est lue, et le gate se calcule sans elle.
    """
    rapport = tmp_path / "rapport.json"
    rapport.write_text("{}", encoding="utf-8")
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION, "--orchestrator-report", str(rapport)])
    assert code == 2
    assert "--orchestrator-report n'a de sens qu'avec --orchestrator-evidence" in \
        capsys.readouterr().err


def test_une_publication_impossible_nempeche_pas_seulement_le_vert_elle_empeche_le_gate(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """B3 : **l'écriture du gate est la dernière étape**. Rien n'est promu si rien n'est publié.

    Le test précédent *entérinait* le défaut : il affirmait qu'un échec de publication laissait le
    gate écrit, en présentant cela comme une propriété. C'en était une, et c'était la mauvaise — un
    `evals_ok: true` promu, immédiatement servable, que rien ne documentait.

    L'ordre est désormais : préparer → basculer la publication → écrire le gate. Un échec de
    publication survient donc **avant** que le manifest ne bouge d'un octet.

    Story 4.5, B7 : `_preparer_atomique` n'existe plus, parce qu'il n'y a plus de préparation par
    cible. L'injection porte donc là où **toute** écriture passe désormais — `os.replace` —, sur
    l'écriture du slot `evals-latest.json` dans la génération inactive. La propriété prouvée est la
    même, et le chemin est celui de production.
    """
    data = _data_dir(tmp_path)
    avant = (data / "manifest.json").read_bytes()
    replace_reel = runner.os.replace

    def _replace(source: Any, cible: Any) -> None:
        if Path(cible).name == "evals-latest.json":
            raise OSError("répertoire de publication en lecture seule")
        replace_reel(source, cible)

    monkeypatch.setattr(runner.os, "replace", _replace)
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION])
    monkeypatch.setattr(runner.os, "replace", replace_reel)
    assert code != 0
    assert "échec de publication" in capsys.readouterr().err
    # **Aucun gate n'a été écrit**, et le manifest est byte-identique.
    assert (data / "manifest.json").read_bytes() == avant
    assert json.loads(avant)[DOC]["gate"] is None
    # Et rien n'a été publié à moitié : ni artefact servi, ni rendu lisible.
    assert not (data / "evals-latest.json").exists()
    assert not (tmp_path / "docs" / "evals" / "latest.md").exists()
    # Ni le moindre temporaire, nulle part — y compris dans le bundle abandonné.
    assert _temporaires(tmp_path) == []


def test_un_gate_impossible_a_ecrire_ne_publie_rien(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue A : publier **puis** écrire le gate déplaçait la fenêtre de B3 au lieu de la fermer.

    `ecrire_gate` peut encore échouer après la publication — `data/` en lecture seule, disque plein,
    conteneur sans droit d'écriture. Dans cet ordre, les trois surfaces publiaient déjà
    `evals_ok: true` alors qu'aucun gate n'existait, et le message d'erreur affirmait littéralement
    « rien n'a été publié ». L'ordre inverse avait le défaut symétrique.

    La fermeture est de **ne plus rien laisser d'échouable après la première bascule** : le manifest
    est préparé comme les publications, et tout bascule ensemble. On double donc l'environnement —
    l'écriture du temporaire du manifest échoue — jamais la règle.

    Story 4.5, B7 : il n'y a plus de temporaire de manifest **dans `data/`**, parce qu'il n'y a plus
    de préparation par cible ; le manifest est un slot du lot, écrit dans la génération inactive
    comme les autres, et rendu visible par l'unique bascule du pointeur. L'environnement doublé est
    donc l'écriture de ce slot-là. La propriété est **plus forte** qu'avant : le manifest n'est plus
    seulement écrit avant les publications, il est publié **par le même atome** qu'elles.
    """
    mkstemp_reel = runner.tempfile.mkstemp

    def _mkstemp(*args: Any, **kw: Any) -> Any:
        if str(kw.get("prefix", "")).startswith(".manifest.json"):
            raise OSError("data/ en lecture seule")
        return mkstemp_reel(*args, **kw)

    data = _data_dir(tmp_path)
    avant = (data / "manifest.json").read_bytes()
    monkeypatch.setattr(runner.tempfile, "mkstemp", _mkstemp)
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION])
    monkeypatch.setattr(runner.tempfile, "mkstemp", mkstemp_reel)
    assert code == 1
    assert "échec de publication" in capsys.readouterr().err
    assert (data / "manifest.json").read_bytes() == avant
    # **Aucune surface n'a publié un verdict que le manifest ne porte pas.**
    assert not (data / "evals-latest.json").exists()
    assert not (tmp_path / "docs" / "evals" / "latest.md").exists()
    # Et aucun temporaire n'est resté derrière — nulle part, pas seulement dans `data/`.
    assert not list(data.glob("*.tmp")) and not list(data.glob(".*.tmp"))
    assert _temporaires(tmp_path) == []


def test_un_manifest_hors_schema_ne_publie_rien_non_plus(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue A, seconde porte : `preparer_gate` refuse aussi sur une entrée hors schéma.

    Le manifest est parfaitement lisible ; c'est **une autre entrée** qui est abîmée. Le refus
    survient donc au moment de préparer le gate, après que la publication a été préparée — et c'est
    exactement le cas que l'ordre précédent laissait passer avec des surfaces déjà basculées.
    """
    data = _data_dir(tmp_path)
    brut = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    brut["autre-doc"] = {"status": "servi", "source_hash": 12, "gate": None}
    (data / "manifest.json").write_text(json.dumps(brut, indent=2) + "\n", encoding="utf-8")
    (data / "autre-doc").mkdir()
    (data / "autre-doc" / "source.url").write_text("https://example.invalid/source", "utf-8")
    poser_espace(tmp_path, data_dir=data)
    avant = (data / "manifest.json").read_bytes()
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION])
    assert code == 2
    assert "invalide" in capsys.readouterr().err
    assert (data / "manifest.json").read_bytes() == avant
    assert not (data / "evals-latest.json").exists()
    assert not (tmp_path / "docs" / "evals" / "latest.md").exists()


def test_un_gate_full_publie_et_promeut_en_une_seule_sequence(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue A, versant nominal : après la préparation, plus rien ne peut échouer à moitié.

    On observe la séquence réelle : au moment où la bascule de publication a lieu, **aucune** de ses
    cibles ne porte encore son nouveau contenu. Autrement dit, il n'existe aucun instant où une
    surface affirme un verdict que le manifest ne porte pas — ni l'inverse.

    Story 4.5, B7 : la propriété observée est **plus forte** qu'au tour précédent, pas plus faible.
    Elle portait alors sur l'**ordre** de la file (« le manifest en dernier »), qui n'était qu'un
    palliatif : entre le premier et le dernier `os.replace`, il existait un instant où une surface
    avait basculé sans le manifest. Ici les quatre surfaces **et** `manifest.json` sont remises au
    **même** appel de `EspacePublie.basculer`, donc publiées par un unique `os.replace` de pointeur :
    il n'y a plus d'ordre à vérifier parce qu'il n'y a plus de fenêtre à ordonner.

    Tour correctif 3/3 : la propriété est encore renforcée, et ce test est devenu la contre-sonde de
    sa propre version précédente. Il exigeait alors **deux** bascules — le couple `eval-results.*`
    d'abord, la publication et le manifest ensuite —, c'est-à-dire deux atomes dans une même
    opération de production : entre eux, `eval-results.md` avait déjà changé alors qu'il appartient
    au lot final. L'AC porte sur l'opération, pas sur l'appel : il n'y a donc plus qu'**une** seule
    bascule, et elle porte les cinq cibles.
    """
    # Le point d'observation est le **commit** — `Transaction.publier` —, et non plus la seule
    # forme courte `EspacePublie.basculer` : depuis que l'opération de gate ouvre sa transaction
    # elle-même (pour relire le manifest et le rendu précédent sous le verrou), c'est là que passe
    # tout ce qui publie, `basculer` compris. La sonde voit donc **plus** de chemins qu'avant, et
    # elle exige toujours exactement un appel portant les cinq cibles.
    basculer_reel = Transaction.publier
    observees: list[list[str]] = []
    deja_publiees: list[list[str]] = []

    def _basculer(self: Any, lot: list[tuple[Path, str]]) -> None:
        observees.append([Path(cible).name for cible, _contenu in lot])
        # **Rien du lot n'est encore visible** : aucune cible ne porte déjà son nouveau contenu au
        # moment où la bascule commence. C'est ce que garantissait autrefois « les temporaires sont
        # tous écrits » — en plus fort, puisque la garantie porte ici sur les cibles elles-mêmes.
        deja_publiees.append([
            Path(cible).name for cible, contenu in lot
            if Path(cible).is_file() and Path(cible).read_text(encoding="utf-8") == contenu])
        basculer_reel(self, lot)

    monkeypatch.setattr(Transaction, "publier", _basculer)
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    # **Un seul commit pour toute l'opération de production** : le rapport, sa table, les surfaces
    # publiées et le manifest sont remis au même appel. Deux bascules seraient deux atomes, donc un
    # état mêlé entre eux — c'est exactement ce que ce test exigeait avant le tour correctif 3/3.
    assert [set(lot) for lot in observees] == [
        {"eval-results.json", "eval-results.md", "evals-latest.json", "latest.md",
         "manifest.json"},
    ], observees
    # Le manifest n'est pas « la dernière cible » : il est **du même lot** que tout le reste, et il
    # n'existe aucune bascule supplémentaire pour l'écrire à part.
    assert len(observees) == 1, observees
    assert "manifest.json" in observees[-1]
    assert deja_publiees == [[]], deja_publiees
    # Et le lot entier est bien visible à la sortie : promouvoir et publier sont un seul geste.
    manifest = json.loads((tmp_path / "data" / "manifest.json").read_text(encoding="utf-8"))
    publie = json.loads(
        (tmp_path / "data" / "evals-latest.json").read_text(encoding="utf-8"))
    assert manifest[DOC]["gate"] is not None
    assert (tmp_path / "docs" / "evals" / "latest.md").is_file()
    assert publie["report_digest"] == manifest[DOC]["gate"]["report_digest"]


def test_un_second_gate_consecutif_nest_pas_refuse_par_les_sorties_du_premier(
        tmp_path: Path) -> None:
    """B3 bis : `SORTIES_DU_RUN` doit lister **toutes** les sorties que le run écrit.

    `revision_executee` refuse un arbre sale. Si les sorties du run n'en sont pas exclues, le
    **deuxième** gate d'une campagne voit l'arbre sali par le premier et refuse : une campagne
    multi-documents devient impossible, ce qui est le contraire du but.

    Le contrôle porte sur un vrai dépôt git temporaire — c'est la seule façon de prouver que la
    liste est exhaustive plutôt que plausible.
    """
    import subprocess

    depot = tmp_path / "depot"
    (depot / "data").mkdir(parents=True)
    (depot / "docs" / "evals").mkdir(parents=True)
    for chemin, contenu in (("data/manifest.json", "{}\n"),
                            ("docs/evals/latest.md", "# ancien\n"),
                            ("server/app/config.py", "x = 1\n")):
        cible = depot / chemin
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_text(contenu, encoding="utf-8")
    for commande in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "initial"]):
        subprocess.run(["git", "-C", str(depot), *commande], check=True, capture_output=True)

    revision, sales = runner.revision_executee(depot)
    assert revision is not None and len(revision) == 40 and sales == []

    # Le premier gate écrit ses sorties : l'arbre est « sale » au sens de git…
    (depot / "data" / "manifest.json").write_text('{"doc": {}}\n', encoding="utf-8")
    (depot / "data" / "evals-latest.json").write_text('{"publie": true}\n', encoding="utf-8")
    (depot / "docs" / "evals" / "latest.md").write_text("# nouveau\n", encoding="utf-8")
    (depot / "docs" / "evals" / "campagnes").mkdir()
    (depot / "docs" / "evals" / "campagnes" / "20260101-abc.md").write_text("x", encoding="utf-8")
    # … mais le second gate n'en est pas empêché : ce sont **ses propres** sorties.
    revision_2, sales_2 = runner.revision_executee(depot)
    assert revision_2 == revision and sales_2 == [], sales_2
    # Une modification **produit**, elle, est bien vue.
    (depot / "server" / "app" / "config.py").write_text("x = 2\n", encoding="utf-8")
    _revision_3, sales_3 = runner.revision_executee(depot)
    assert sales_3 == ["server/app/config.py"]


def test_la_revision_executee_refuse_ce_quelle_ne_peut_pas_etablir(tmp_path: Path,
                                                                   monkeypatch: Any) -> None:
    """B1 : une liaison qu'on ne peut pas prouver n'est pas une liaison.

    Hors dépôt git et sans `GIT_SHA` en 40 hexadécimaux, la révision est **inconnue** — et le runner
    refuse plutôt que de croire l'argument sur parole.
    """
    hors_depot = tmp_path / "hors-depot"
    hors_depot.mkdir()
    monkeypatch.delenv("GIT_SHA", raising=False)
    assert runner.revision_executee(hors_depot) == (None, [])
    # `GIT_SHA` en 40 hex est le repli d'une image sans `.git` : il n'y a alors aucun arbre à juger.
    monkeypatch.setenv("GIT_SHA", "d" * 40)
    assert runner.revision_executee(hors_depot) == ("d" * 40, [])
    # Un `GIT_SHA` court (le `sha7` du déploiement) ne suffit pas à établir une révision de gate.
    monkeypatch.setenv("GIT_SHA", "abcdef1")
    assert runner.revision_executee(hors_depot) == (None, [])


def test_un_controle_darbre_qui_nechoue_pas_a_conclure_est_traite_comme_un_arbre_sale(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Revue B : ne pas pouvoir contrôler l'arbre, c'est refuser — pas laisser passer.

    Deux chemins affirmaient un arbre propre sans l'avoir regardé : un `git status --porcelain`
    sortant en code non nul (un `index.lock` tenu suffit) laissait la liste des modifications vide,
    et une exception rabattait sur `GIT_SHA` un dépôt pourtant présent. Un gate `full` passait alors
    sur un arbre dont personne n'avait rien su.
    """
    import subprocess

    depot = tmp_path / "depot"
    (depot / ".git").mkdir(parents=True)
    monkeypatch.delenv("GIT_SHA", raising=False)
    reel = subprocess.run

    def _git(sortie_status: Any) -> Any:
        def _run(argv: list[str], **kw: Any) -> Any:
            if "rev-parse" in argv:
                return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr="")
            return sortie_status()
        return _run

    # 1. `git status` sort en code non nul : l'arbre n'a pas été contrôlé.
    runner_subprocess = subprocess
    monkeypatch.setattr(runner_subprocess, "run",
                        _git(lambda: SimpleNamespace(returncode=128, stdout="", stderr="lock")))
    revision, sales = runner.revision_executee(depot)
    assert revision == "a" * 40
    assert sales == [runner.ARBRE_NON_VERIFIABLE], sales

    # 2. `git status` lève : même conclusion, et **pas** de repli sur `GIT_SHA`.
    monkeypatch.setenv("GIT_SHA", "d" * 40)

    def _leve() -> Any:
        raise OSError("git indisponible")

    monkeypatch.setattr(runner_subprocess, "run", _git(_leve))
    revision, sales = runner.revision_executee(depot)
    assert revision == "a" * 40 and sales == [runner.ARBRE_NON_VERIFIABLE]

    # 3. `rev-parse` lui-même échoue sur un dépôt **présent** : `GIT_SHA` ne le remplace pas.
    def _rev_parse_casse(argv: list[str], **kw: Any) -> Any:
        return SimpleNamespace(returncode=128, stdout="", stderr="fatal")

    monkeypatch.setattr(runner_subprocess, "run", _rev_parse_casse)
    assert runner.revision_executee(depot) == (None, [])
    # Sans dépôt du tout, en revanche, `GIT_SHA` reste le repli légitime d'une image sans `.git`.
    hors_depot = tmp_path / "image"
    hors_depot.mkdir()
    assert runner.revision_executee(hors_depot) == ("d" * 40, [])

    monkeypatch.setattr(runner_subprocess, "run", reel)


def test_un_arbre_non_verifiable_ferme_le_gate_full(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue B, jusqu'au verdict : l'appelant ne distingue pas « sale » de « non vérifiable »."""
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION], sales=[runner.ARBRE_NON_VERIFIABLE])
    assert code == 2
    assert "modifications non commises" in capsys.readouterr().err
    _manifest_intact(tmp_path)


def test_un_gate_full_sur_un_document_sans_source_est_refuse(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue C : sans source sur le disque, aucun des deux témoins de structure ne couvre le document.

    C'est l'état **réel** d'un checkout frais : les `source.pdf` ne sont pas committés (`.gitignore`)
    et sont téléchargés au build. La règle `SOURCE_FILES` ne sait alors pas ce que le document est,
    il sort des deux dénominateurs, aucune décision de structure n'est émise — et le loader, lui, le
    **sert** quand même (simple alerte `source_absente`). Le gate `full` verdirait donc sans qu'aucune
    preuve de structure ne lui ait été opposée.
    """
    ctx = _contexte()
    data_sans_source = tmp_path / "sonde"
    (data_sans_source / DOC).mkdir(parents=True)
    assert _preuve_structure(data_sans_source, ctx, [DOC]) == (0, 0)
    assert _preuve_arbre(data_sans_source, ctx, [DOC]) == (0, 0)

    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION], source=None)
    assert code == 2
    err = capsys.readouterr().err
    assert "aucune source n'est présente" in err
    # Le refus nomme la remise en état, sans quoi il n'apprend rien à qui le lit.
    assert "server.ingest.fetch_source" in err
    _manifest_intact(tmp_path)
    # Et sous `vertical`, rien ne change : les exigences de structure ne s'arment que sur `full`.
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "vertical", "--repeat", "3"], source=None) == 1


def test_un_check_bloquant_interdit_de_compter_meme_a_cote_dune_attestation(
        tmp_path: Path) -> None:
    """Revue D : « jamais bloquant » est une règle, pas une intention de docstring.

    Le vérificateur qui refuse et l'ingestion qui atteste écrivent le **même** nom de check. Un
    rapport portant les deux — un refus et une acceptation — comptait comme prouvé, parce que seule
    la présence d'une attestation non bloquante était exigée. Le chemin de production n'en émet
    qu'un à la fois ; une preuve ne se lit pas en pariant là-dessus.
    """
    from server.app.domain.ingest import (detail_attestation_arbre,
                                          detail_attestation_structure)

    ctx = _contexte()
    data = tmp_path / "data"
    (data / DOC).mkdir(parents=True)
    entry = ctx.index.corpus.manifest[DOC]

    # --- périmètre guide : `invariants_arbre` ---------------------------------------------------
    (data / DOC / "source.js").write_bytes(b"var kb = {};")
    atteste = {"name": "invariants_arbre", "level": "info",
               "detail": detail_attestation_arbre(document_hash=entry.document_hash,
                                                  ingest_fingerprint=entry.ingest_fingerprint)}
    refus = {"name": "invariants_arbre", "level": "bloquant", "detail": "cycle_noeuds : ..."}
    (data / DOC / "report.json").write_text(json.dumps({"doc_id": DOC, "checks": [atteste]}),
                                            encoding="utf-8")
    assert _preuve_arbre(data, ctx, [DOC]) == (1, 1)
    (data / DOC / "report.json").write_text(
        json.dumps({"doc_id": DOC, "checks": [atteste, refus]}), encoding="utf-8")
    assert _preuve_arbre(data, ctx, [DOC]) == (0, 1), (
        "un arbre refusé n'est pas un arbre prouvé, même si une acceptation traîne à côté")

    # --- périmètre PDF : `structure_proposee` ----------------------------------------------------
    (data / DOC / "source.js").unlink()
    (data / DOC / "source.pdf").write_bytes(b"%PDF-1.4")
    octets = b'{"doc_id": "contrat-neutre"}\n'
    (data / DOC / "structure.json").write_bytes(octets)
    entry.structure_hash = hashlib.sha256(octets).hexdigest()
    atteste = {"name": "structure_proposee", "level": "info",
               "detail": detail_attestation_structure(document_hash=entry.document_hash,
                                                      structure_hash=entry.structure_hash)}
    refus = {"name": "structure_proposee", "level": "bloquant", "detail": "ligne_omise : ..."}
    (data / DOC / "report.json").write_text(json.dumps({"doc_id": DOC, "checks": [atteste]}),
                                            encoding="utf-8")
    assert _preuve_structure(data, ctx, [DOC]) == (1, 1)
    (data / DOC / "report.json").write_text(
        json.dumps({"doc_id": DOC, "checks": [atteste, refus]}), encoding="utf-8")
    assert _preuve_structure(data, ctx, [DOC]) == (0, 1)


def test_une_revision_annoncee_qui_nest_pas_celle_executee_est_refusee(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """B1 : « preuve et argument concordants entre eux mais divergents de la révision exécutée ».

    `--candidate-revision` n'était comparée qu'à elle-même : le runner la recopiait dans le gate et
    dans la preuve, puis recoupait la preuve avec ce même argument. Trois surfaces d'accord sur une
    révision que personne n'a exécutée.
    """
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION], revision="b" * 40)
    assert code == 2
    err = capsys.readouterr().err
    assert "≠ révision réellement exécutée" in err
    _manifest_intact(tmp_path)

    # Un arbre sale est refusé pour la même raison : le commit annoncé ne décrit pas ce code.
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION], sales=["server/app/pipelines/guide.py"])
    assert code == 2
    assert "modifications non commises" in capsys.readouterr().err

    # Une révision indéterminable est un refus, jamais un laissez-passer.
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION], revision=None)
    assert code == 2
    assert "n'a pu être établie" in capsys.readouterr().err


def test_letat_de_seconde_lecture_est_celui_que_le_run_ecrit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue P10 : l'observation porte sur l'artefact **écrit par le run**, pas sur une valeur fournie.

    Un `statut="concordante", blocs_verifies=n` codé en dur aurait passé toute la suite, et les
    quatre surfaces auraient affirmé qu'une seconde lecture humaine a concordé alors qu'aucune n'a
    eu lieu — l'invention même que la story combat.
    """
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    publie = json.loads((tmp_path / "data" / "evals-latest.json").read_text(encoding="utf-8"))
    seconde = publie["seconde_lecture"]
    # Aucun verdict déposé : `planifiee`, et **zéro** bloc relu. Jamais « concordante par défaut ».
    assert seconde["statut"] == "planifiee"
    assert seconde["blocs_verifies"] == 0
    # Le plan compte exactement les blocs clés que le run a cités et qui sont relisibles (page+bbox).
    rapport = json.loads((tmp_path / "eval-results.json").read_text(encoding="utf-8"))
    from server.evals.relecture import blocs_cles_du_rapport
    assert blocs_cles_du_rapport(rapport) == [f"{DOC}:p1:1"]
    assert seconde["blocs_planifies"] == 1
    # Et la limite correspondante est publiée, dérivée de cet état.
    assert any("seconde lecture" in limite and "planifiee" in limite
               for limite in publie["limites"])


def test_un_verdict_de_seconde_lecture_rempli_remonte_jusqua_la_publication(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR47 : le chemin qui **ingère** un verdict rempli va jusqu'aux quatre surfaces.

    Le test précédent *entérinait* B5 : il posait `image_sha256: "aaaa…"` et concluait
    « concordante ». Il prouvait donc exactement ce que la revue reproche — qu'une empreinte
    inventée suffit. Ici les octets existent, le validateur recalcule leur empreinte, et c'est
    l'égalité qui décide.
    """
    from server.evals.relecture import (blocs_cles_du_rapport, empreinte_image, nom_image,
                                        plan_de_relecture)

    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    rapport = json.loads((tmp_path / "eval-results.json").read_text(encoding="utf-8"))
    plan = plan_de_relecture(_contexte().index, blocs_cles_du_rapport(rapport),
                             candidate_revision=REVISION)
    assert plan.blocs, "le run doit produire un plan non vide pour que ce test morde"

    # Les octets « réellement regardés » : synthétiques, mais **les mêmes** que ceux dont le verdict
    # annonce l'empreinte. Aucun PDF, aucun rendu, aucun réseau.
    images = tmp_path / "images"
    images.mkdir()
    octets = {b.block_id: b"\x89PNG\r\n\x1a\n" + b.block_id.encode("utf-8") for b in plan.blocs}
    for block_id, contenu in octets.items():
        (images / nom_image(block_id)).write_bytes(contenu)

    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({
        "schema_version": 1, "candidate_revision": REVISION, "plan_digest": plan.plan_digest,
        "verdicts": [{"block_id": b.block_id, "verdict": "concordant",
                      "image_sha256": empreinte_image(octets[b.block_id]), "note": ""}
                     for b in plan.blocs],
    }), encoding="utf-8")

    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION, "--relecture-verdict", str(verdict),
                 "--relecture-images", str(images)]) == 1
    publie = json.loads((tmp_path / "data" / "evals-latest.json").read_text(encoding="utf-8"))
    assert publie["seconde_lecture"] == {"statut": "concordante",
                                         "blocs_planifies": len(plan.cles_attendues),
                                         "blocs_verifies": len(plan.cles_attendues),
                                         "blocs_non_projetables": 0}
    # Le dénominateur est celui des clés **attendues**, et il n'y a rien de perdu à couvrir.
    assert not plan.non_projetables
    # La limite disparaît : elle était dérivée de l'état, pas rédigée.
    assert not any("seconde lecture" in limite for limite in publie["limites"])


def test_un_verdict_dont_les_images_ne_concordent_pas_empeche_le_vert(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """B5, de bout en bout : une empreinte inventée ne devient jamais une seconde lecture publiée.

    Trois façons de ne pas prouver, et les trois sont refusées : une empreinte fabriquée, une image
    absente, et un verdict fourni **sans** les octets.
    """
    from server.evals.relecture import blocs_cles_du_rapport, nom_image, plan_de_relecture

    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    rapport = json.loads((tmp_path / "eval-results.json").read_text(encoding="utf-8"))
    plan = plan_de_relecture(_contexte().index, blocs_cles_du_rapport(rapport),
                             candidate_revision=REVISION)
    images = tmp_path / "images"
    images.mkdir()
    for bloc in plan.blocs:
        (images / nom_image(bloc.block_id)).write_bytes(b"des-octets-de-page")

    invente = tmp_path / "verdict.json"
    invente.write_text(json.dumps({
        "schema_version": 1, "candidate_revision": REVISION, "plan_digest": plan.plan_digest,
        "verdicts": [{"block_id": b.block_id, "verdict": "concordant",
                      "image_sha256": "a" * 64, "note": ""} for b in plan.blocs],
    }), encoding="utf-8")
    manifest_avant = (tmp_path / "data" / "manifest.json").read_bytes()
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION, "--relecture-verdict", str(invente),
                 "--relecture-images", str(images)]) != 0
    err = capsys.readouterr().err
    assert "échec de publication" in err and "ne porte pas sur l'image" in err
    # B3 : rien n'est promu si rien n'est publié — le manifest n'a pas bougé.
    assert (tmp_path / "data" / "manifest.json").read_bytes() == manifest_avant

    # Sans les octets, il n'y a rien à recouper : refus aussi.
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION, "--relecture-verdict", str(invente)]) != 0
    assert "exige --relecture-images" in capsys.readouterr().err


# --- B3 : tout ou rien sous échec de renommage, à **chaque** étape ---------------------------------

def _empreintes_des_cibles(
        racine: Path) -> dict[str, tuple[bool, int | None, str | None, str | None]]:
    """L'état observable de **toutes** les cibles du lot, sur les quatre dimensions de l'AC.

    Tour correctif 3/3 : `eval-results.json` et `eval-results.md` y **entrent**. Les en exclure au
    motif qu'ils sont le journal du run courant était le rétrécissement de frontière que l'AC
    interdit : ils appartiennent au lot que l'opération de production publie, et le recheck a montré
    qu'ils avaient déjà changé quand le second atome échouait. Aucune cible n'est retirée de la
    comparaison ; et la comparaison se fait **depuis l'entrée de l'opération de production**, pas
    depuis l'entrée du dernier appel de bascule.

    Story 4.5, B7, et l'interdiction 7 qui définit « modifiée » par l'état observable : les quatre
    dimensions sont capturées littéralement — présence de l'entrée, type d'entrée (`lstat`), cible
    de lien, contenu —, dans cet ordre. Une comparaison de seules empreintes laisserait passer une
    migration de type ou une cible repointée ailleurs.
    """
    cibles = {
        "evals-latest.json": racine / "data" / "evals-latest.json",
        "latest.md": racine / "docs" / "evals" / "latest.md",
        "manifest.json": racine / "data" / "manifest.json",
        "eval-results.json": racine / "eval-results.json",
        "eval-results.md": racine / "eval-results.md",
    }
    etat: dict[str, tuple[bool, int | None, str | None, str | None]] = {}
    for nom, chemin in cibles.items():
        try:
            marque = os.lstat(chemin).st_mode >> 12
        except OSError:
            etat[nom] = (False, None, None, None)
            continue
        lien = os.readlink(chemin) if os.path.islink(chemin) else None
        try:
            empreinte: str | None = hashlib.sha256(chemin.read_bytes()).hexdigest()
        except OSError:
            empreinte = None
        etat[nom] = (True, marque, lien, empreinte)
    return etat


def test_loperation_de_production_dun_gate_full_na_quun_seul_point_de_commit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B7, tour correctif 3/3 : « un atome tout-ou-rien » n'est pas « une opération tout-ou-rien ».

    Le run de gate appelait `EspacePublie.basculer` **deux** fois : une première pour le couple
    `eval-results.*`, une seconde pour le gate, la publication et le manifest. Chaque appel était
    tout-ou-rien, l'opération ne l'était pas : si le second échouait, `eval-results.md` — pourtant
    membre du lot final — avait déjà changé. L'AC porte sur l'opération, pas sur l'appel.

    La sonde compte les **points de commit** de l'opération, c'est-à-dire les `os.replace` qui
    remplacent le pointeur. Il doit y en avoir exactement un, quel que soit le nombre de surfaces.
    """
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    # La disposition est posée **hors** du comptage : `installer()` remplace elle aussi le pointeur,
    # une fois, et c'est un geste d'opérateur, pas un commit de l'opération de production.
    _data_dir(tmp_path)
    replace_reel = runner.os.replace
    commits: list[str] = []

    def _replace(source: Any, cible: Any) -> None:
        if Path(cible).name == POINTEUR:
            commits.append(str(cible))
        replace_reel(source, cible)

    monkeypatch.setattr(runner.os, "replace", _replace)
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    monkeypatch.setattr(runner.os, "replace", replace_reel)

    assert len(commits) == 1, (
        f"l'opération de production a {len(commits)} points de commit : entre deux, l'état est "
        "mêlé, quand bien même chacun serait tout-ou-rien")
    # Et le commit unique a bien tout publié : le rapport, sa table, les surfaces et le manifest.
    apres = _empreintes_des_cibles(tmp_path)
    assert all(present and empreinte is not None
               for present, _marque, _lien, empreinte in apres.values()), apres


@pytest.mark.parametrize("rang", [0, 1, 2, 3, "atome"])
def test_un_echec_de_bascule_a_nimporte_quelle_etape_ne_laisse_aucun_lot_partiel(
        rang: int | str, tmp_path: Path, capsys: Any,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """B3 : « le dernier lot visible est préservé intégralement, ou le nouveau est rendu en entier ».

    Contre-exemple reproduit par la revue : un échec sur le **deuxième** `os.replace` publiait
    `data/evals-latest.json` **seul**, avec un `latest.md` inexistant, un `eval-results.md` périmé
    et un manifest sans gate — pendant que le message affirmait littéralement « rien n'a été
    publié ». `_abandonner` ne restaurait rien.

    Story 4.5, B7 : la propriété **est** désormais l'atomicité, et non plus « tout ou rien par
    restauration ». Aucune cible n'est modifiée tant que l'unique `os.replace` du pointeur n'a pas
    eu lieu, donc il n'y a rien à défaire — et rien qui puisse échouer ou être interrompu en
    défaisant. L'injection porte sur `os.replace` : les rangs 0 à 3 sont des écritures de slots dans
    la génération inactive, le rang `atome` est la bascule du pointeur elle-même. L'invariant est le
    même aux cinq rangs, et il porte sur l'état observable **entier** — contenu et type d'entrée —
    de **toutes** les cibles du lot, `eval-results.md` et l'archive de campagne comprises.
    """
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    # Un premier gate complet : c'est le « dernier lot visible » que l'échec ne doit pas entamer.
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    avant = _empreintes_des_cibles(tmp_path)
    assert all(present and empreinte is not None
               for present, _marque, _lien, empreinte in avant.values()), avant

    # Le second run mesure une **autre** révision : sans cela, sa publication serait octet pour
    # octet celle du premier, et une bascule partielle passerait inaperçue — un test qui ne peut
    # pas voir le défaut ne le ferme pas.
    autre_revision = "c" * 40

    # L'injection vise **le lot de publication** (celui qui porte le manifest), et lui seul :
    # `ecrire_rapports` a le sien, éprouvé à part.
    basculer_reel = Transaction.publier
    replace_reel = runner.os.replace
    # L'état observable du lot **entier**, relevé au moment même où la bascule commence : c'est lui
    # que l'invariant protège, et il couvre des cibles que `_empreintes_des_cibles` n'a pas à
    # suivre d'un run à l'autre (le journal du run, l'archive de campagne).
    lots: list[tuple[list[Path], dict[str, tuple[bytes | None, int | None]]]] = []

    def _basculer(self: Any, lot: list[tuple[Path, str]]) -> None:
        if not any(Path(cible).name == "manifest.json" for cible, _contenu in lot):
            basculer_reel(self, lot)
            return
        cibles = [Path(cible) for cible, _contenu in lot]
        lots.append((cibles, _etat_du_lot(cibles)))
        compteur = {"n": 0}

        def _replace(source: Any, cible: Any) -> None:
            if Path(cible).name == POINTEUR:
                # L'atome : le seul pas qui porte l'ancien état vers le nouveau.
                if rang == "atome":
                    raise OSError(28, "No space left on device (injecté par le test)")
                replace_reel(source, cible)
                return
            if compteur["n"] == rang:
                compteur["n"] += 1
                raise OSError(28, "No space left on device (injecté par le test)")
            compteur["n"] += 1
            replace_reel(source, cible)

        monkeypatch.setattr(runner.os, "replace", _replace)
        try:
            basculer_reel(self, lot)
        finally:
            monkeypatch.setattr(runner.os, "replace", replace_reel)

    monkeypatch.setattr(Transaction, "publier", _basculer)
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", autre_revision], revision=autre_revision)
    assert code != 0
    # **Aucune surface n'a bougé** : le lot précédent est intégralement préservé.
    assert _empreintes_des_cibles(tmp_path) == avant, (
        f"échec injecté à l'étape {rang} : lot partiel visible")
    # Et cela vaut de **toutes** les cibles du lot, pas seulement des trois surfaces durables :
    # contenu et type d'entrée identiques à ce qu'ils étaient à l'entrée de la bascule.
    assert lots, "la bascule de publication n'a pas été atteinte : le test ne prouve rien"
    for cibles, etat in lots:
        assert _etat_du_lot(cibles) == etat, (
            f"échec injecté à l'étape {rang} : une cible du lot a été modifiée")
    # Et le journal du run reste un document entier, jamais un fichier à moitié écrit.
    journal = (tmp_path / "eval-results.md").read_text(encoding="utf-8")
    assert journal.startswith("# Résultat des questions-témoins") and journal.endswith("\n")
    err = capsys.readouterr().err
    assert "échec de publication" in err
    # Aucun temporaire résiduel, où que ce soit — bundle de publication compris.
    assert _temporaires(tmp_path) == []


def test_aucun_etat_mele_nest_atteignable_donc_il_ny_a_rien_a_nommer(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """B3 → B7 : ce test exigeait qu'un état mêlé soit **nommé** ; il exige maintenant qu'il n'existe pas.

    Sous l'ancien protocole, une panne au deuxième renommage laissait `data/evals-latest.json`
    publié et le reste non ; la restauration pouvait échouer par-dessus, et l'exigence de probité
    était que le runner **dise** quelle cible était dans quel état plutôt que d'affirmer « rien n'a
    été publié ». Mieux signaler un état mêlé n'était pas l'invariant de l'AC : le rendre
    inatteignable l'est. Ce test est donc devenu la contre-sonde de sa propre ancienne exigence.

    Deux choses sont prouvées ici, et il faut les deux :

    1. **l'API qui nommait l'état mêlé n'existe plus, et rien ne l'a remplacée** — ni
       `BasculePartielle`, ni la file de renommages, ni la restauration qui la rattrapait ;
    2. **le comportement suit** : la même injection qu'autrefois (panne sur `latest.md`, la
       deuxième cible de l'ancienne file) ne laisse aucune cible dans le nouvel état, et la phrase
       « rien n'a été publié » — qui était alors un mensonge — est cette fois **vraie**, ce que
       l'état sur disque vérifie ligne suivante.
    """
    # 1. Aucune de ces surfaces ne subsiste : un état mêlé n'a plus de vocabulaire parce qu'il n'a
    #    plus de lieu où survenir.
    for disparu in ("BasculePartielle", "EtatPrecedentIllisible", "_basculer", "_restaurer",
                    "_abandonner", "_preparer_atomique", "_ecrire_atomique_octets",
                    "_lire_ou_absent"):
        assert not hasattr(runner, disparu), (
            f"runner.{disparu} est revenu : la file de renommages et sa restauration sont "
            "précisément ce que l'invariant interdit de réintroduire")

    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    avant = _empreintes_des_cibles(tmp_path)
    capsys.readouterr()

    replace_reel = runner.os.replace

    def _replace(source: Any, cible: Any) -> None:
        nom = Path(cible).name
        if nom == "latest.md":  # la deuxième cible de l'ancienne file de publication
            raise OSError(28, "No space left on device (injecté par le test)")
        replace_reel(source, cible)

    monkeypatch.setattr(runner.os, "replace", _replace)
    # Le second run mesure une **autre** révision : sans cela sa publication serait octet pour octet
    # celle du premier, et une cible restée dans le nouvel état passerait inaperçue.
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", "c" * 40], revision="c" * 40)
    monkeypatch.setattr(runner.os, "replace", replace_reel)
    assert code != 0
    err = capsys.readouterr().err
    # 2. Plus aucun message d'état mêlé, parce que plus aucun état mêlé.
    assert "état MÊLÉ" not in err, err
    assert "rien n'a été publié" in err
    # Et la phrase est **vraie** : aucune cible du lot n'est dans le nouvel état, ni en contenu ni
    # en type d'entrée. C'est ce que l'ancien test ne pouvait pas asserter — il asserait l'inverse.
    assert _empreintes_des_cibles(tmp_path) == avant, (
        "une cible est restée dans le nouvel état : c'est l'état mêlé lui-même")
    assert _temporaires(tmp_path) == []


def test_le_rapport_et_sa_table_basculent_ensemble(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """B3, chemin frère : `ecrire_rapports` enchaînait deux écritures sans rollback.

    Ce n'est pas un couple anodin : `digest_octets(output_json)` alimente `gate.report_digest`, donc
    l'empreinte par laquelle un gate se réclame de son rapport. Un JSON neuf à côté d'un Markdown
    périmé se propage jusque dans le gate.

    Story 4.5, B7 : ce couple est un lot comme un autre, remis au **même** appel de
    `EspacePublie.basculer`. L'injection porte donc sur `os.replace` — l'écriture du slot
    `rapport.md` dans la génération inactive —, et l'invariant vérifié est plus fort qu'une simple
    égalité de contenus : le type d'entrée des deux cibles est comparé lui aussi.
    """
    json_path = tmp_path / "rapport.json"
    md_path = tmp_path / "rapport.md"
    json_path.write_text('{"ancien": true}\n', encoding="utf-8")
    md_path.write_text("# ancien\n", encoding="utf-8")
    # La disposition de publication est posée **hors** de tout run : la bascule refuse une cible
    # qu'aucun pointeur ne résout, et ne l'installe jamais elle-même.
    espace = poser_espace(tmp_path, cibles=[Path("rapport.json"), Path("rapport.md")])
    avant = (json_path.read_bytes(), md_path.read_bytes())
    etat_avant = _etat_du_lot([json_path, md_path])

    rapport = {
        "schema_version": 3, "profile": "full", "complete": True, "stop_reason": None,
        "unexecuted_cases": [], "cases_hash": "d" * 64, "cases_planned": 1, "cases_completed": 1,
        "cost_eur": 0.0, "identity": {"run_digest": "a" * 64},
        "metrics": {"labels": {label: 0 for label in runner.LABELS}, "variants": {},
                    "recall": 1.0, "average_cost_eur": 0.0, "latency_p50_ms": 0,
                    "latency_p95_ms": 0, "cost_p95_eur": 0.0, "ne_tranche_pas_rate": 0.0},
        # Les structures que la publication **exige** (revue B5) : un rapport qui les omet ne se
        # publie pas, il refuse — c'est le point du correctif.
        "results": [], "decisions": [], "repeat": 1,
    }
    replace_reel = runner.os.replace

    def _replace(source: Any, cible: Any) -> None:
        if Path(cible).name == "rapport.md":
            raise OSError(28, "No space left on device (injecté par le test)")
        replace_reel(source, cible)

    monkeypatch.setattr(runner.os, "replace", _replace)
    with pytest.raises(OSError):
        runner.ecrire_rapports(rapport, json_path, md_path, preuve_externe=None, espace=espace)
    monkeypatch.setattr(runner.os, "replace", replace_reel)
    # Les deux fichiers sont restés dans leur état d'avant : aucun lot mêlé.
    assert (json_path.read_bytes(), md_path.read_bytes()) == avant
    assert _etat_du_lot([json_path, md_path]) == etat_avant
    assert not [p.name for p in tmp_path.glob(".*.tmp")]
    assert _temporaires(tmp_path) == []
    # Et le chemin nominal publie bien **les deux ensemble**, par le même unique atome.
    runner.ecrire_rapports(rapport, json_path, md_path, preuve_externe=None, espace=espace)
    assert json.loads(json_path.read_text(encoding="utf-8"))["cases_hash"] == "d" * 64
    assert md_path.read_text(encoding="utf-8").startswith("# Résultat des questions-témoins")


# --- B5 : ce que la publication affirme quand une clé attendue est improjetable --------------------

def test_une_cle_attendue_improjetable_est_publiee_et_ne_dit_jamais_concordante(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B5, de bout en bout : le dénominateur publié est celui des clés **attendues**.

    Contre-exemple reproduit par la revue : le plan perdait les clés improjetables, la couverture ne
    portait que sur le résidu, et la publication annonçait `concordante`, `blocs_planifies=1`,
    `blocs_verifies=1` — un ratio parfait alors qu'une page servie n'avait jamais été regardée.
    """
    from server.evals.relecture import blocs_cles_du_rapport, plan_de_relecture

    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    # Le run cite un bloc clé de plus, que le corpus sert **sans** page ni bbox : aucune image
    # possible, donc aucune relecture possible.
    reel = runner.blocs_cles_du_rapport

    def _avec_cle_perdue(rapport: dict[str, Any]) -> list[str]:
        return sorted({*reel(rapport), f"{DOC}:p7:1"})

    monkeypatch.setattr(runner, "blocs_cles_du_rapport", _avec_cle_perdue)
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1

    publie = json.loads((tmp_path / "data" / "evals-latest.json").read_text(encoding="utf-8"))
    seconde = publie["seconde_lecture"]
    # **Jamais `concordante`**, et jamais `absente` non plus : la preuve est impossible, et le dit.
    assert seconde["statut"] == "impossible"
    assert seconde["blocs_non_projetables"] == 1
    # Le dénominateur compte la clé perdue : 2 attendues, 0 relue.
    assert seconde["blocs_planifies"] == 2 and seconde["blocs_verifies"] == 0
    # Et la limite dérivée la nomme, plutôt que de la taire.
    assert any("impossibles à projeter" in limite for limite in publie["limites"])

    # Le plan, lui, porte la clé perdue avec sa raison.
    rapport = json.loads((tmp_path / "eval-results.json").read_text(encoding="utf-8"))
    plan = plan_de_relecture(_contexte().index, _avec_cle_perdue(rapport),
                             candidate_revision=REVISION)
    assert [(n.block_id, n.raison) for n in plan.non_projetables] == [
        (f"{DOC}:p7:1", "inconnu_de_lindex")]
    del blocs_cles_du_rapport


def test_un_run_sans_bloc_cle_ne_se_confond_pas_avec_un_run_dont_tout_est_perdu(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B5, propriété 4, sur la surface publiée : `absente` ≠ `impossible`."""
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    monkeypatch.setattr(runner, "blocs_cles_du_rapport", lambda _rapport: [])
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    seconde = json.loads(
        (tmp_path / "data" / "evals-latest.json").read_text(encoding="utf-8"))["seconde_lecture"]
    assert seconde == {"statut": "absente", "blocs_planifies": 0, "blocs_verifies": 0,
                       "blocs_non_projetables": 0}

    monkeypatch.setattr(runner, "blocs_cles_du_rapport", lambda _rapport: [f"{DOC}:p7:1"])
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    seconde = json.loads(
        (tmp_path / "data" / "evals-latest.json").read_text(encoding="utf-8"))["seconde_lecture"]
    assert seconde == {"statut": "impossible", "blocs_planifies": 1, "blocs_verifies": 0,
                       "blocs_non_projetables": 1}


# --- B3/B6 : « je n'ai pas pu lire » n'est jamais « il n'y avait rien » ----------------------------

def test_seule_labsence_est_une_absence(tmp_path: Path) -> None:
    """B3/B6, l'invariant lui-même : `FileNotFoundError` seule signifie « il n'y avait rien ».

    `except OSError: return None` faisait dire « absent » à « illisible ». La conséquence n'était pas
    l'imprécision : la restauration croyait la cible absente d'avant, exécutait `unlink()` — donc
    **supprimait** le fichier qui venait d'être publié —, réussissait, et ne signalait donc aucune
    restauration manquée.

    Story 4.5, B7 : `runner._lire_ou_absent` n'existe plus, parce que le protocole ne capture plus
    d'état précédent — il n'a rien à défaire, donc rien à relire. L'invariant, lui, n'a pas disparu :
    il vit dans le seul lecteur qui subsiste, `_archive_a_ecrire`, et c'est le lecteur **le plus
    grave** des deux, puisque celui-là détruit même quand la bascule réussit (un rendu non archivé
    est ensuite écrasé). La distinction se prouve donc là, dans ses trois cas, un pour un.
    """
    from server.evals import publication as pub_mod

    docs = tmp_path.joinpath(*pub_mod.DOCS_LATEST).parent
    docs.mkdir(parents=True)
    # Absent : « il n'y avait rien », et rien à archiver.
    assert pub_mod._archive_a_ecrire(docs / "jamais-cree.md", repo_root=tmp_path) is None
    present = docs / "present.md"
    present.write_bytes(b"contenu")
    a_ecrire = pub_mod._archive_a_ecrire(present, repo_root=tmp_path)
    assert a_ecrire is not None and a_ecrire[1] == "contenu"
    # Un répertoire n'est pas un fichier absent : le lire lève `IsADirectoryError` (une
    # `OSError`), et c'est un refus, pas une absence.
    with pytest.raises(pub_mod.ArchivePrecedenteIllisible, match="n'a pas pu être lu"):
        pub_mod._archive_a_ecrire(docs, repo_root=tmp_path)
    # Et l'ancien vocabulaire du repli silencieux n'est pas revenu par une autre porte.
    assert not hasattr(runner, "_lire_ou_absent")


@pytest.mark.parametrize("rang", [0, 1, 2, 3])
def test_une_cible_illisible_ne_produit_jamais_de_lot_partiel(
        rang: int, tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """B3/B6 → B7 : une cible qu'on ne peut pas lire ne peut pas produire une publication partielle.

    Ce test exigeait qu'un état d'avant illisible **arrête la préparation**. Cette exigence était
    celle d'un protocole qui, pour pouvoir défaire ce qu'il avait fait, devait d'abord relire chaque
    cible : ne pas pouvoir la relire, c'était ne pas pouvoir la restaurer, donc devoir refuser. Le
    protocole de B7 ne capture aucun état précédent, parce qu'il n'a rien à défaire — la lecture
    dont dépendait ce refus n'existe plus, et il serait malhonnête d'en simuler une.

    Ce qu'elle protégeait, elle, tient toujours, et se prouve directement : **quoi qu'il arrive à
    une cible, l'état observable du lot reste entier**. Le second run tourne avec l'une des quatre
    cibles réellement illisible (`chmod 000`, pas un double), et l'issue admissible est l'une des
    deux issues *entières* — le lot précédent intégralement préservé, ou le nouveau intégralement
    rendu. Jamais un mélange, jamais une cible seule.

    Le rang `latest.md` est en outre un refus **déterministe**, éprouvé avec ses messages par
    `test_un_rendu_precedent_illisible_refuse_de_publier_avant_toute_bascule` : celui-là est le
    lecteur qui subsiste, et le seul dont l'illisibilité doit encore arrêter la publication.
    """
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    avant = _empreintes_des_cibles(tmp_path)

    chemins = {
        "evals-latest.json": tmp_path / "data" / "evals-latest.json",
        "latest.md": tmp_path / "docs" / "evals" / "latest.md",
        "eval-results.md": tmp_path / "eval-results.md",
        "manifest.json": tmp_path / "data" / "manifest.json",
    }
    ordre = ["evals-latest.json", "latest.md", "eval-results.md", "manifest.json"]
    illisible = chemins[ordre[rang]]

    illisible.chmod(0o000)
    try:
        code = _cli(tmp_path, monkeypatch,
                    ["--gate", DOC, "--profile", "full", "--repeat", "3",
                     "--candidate-revision", "c" * 40], revision="c" * 40)
    finally:
        # Rendue lisible **avant** toute observation : une cible qu'on ne sait pas relire se
        # comparerait à elle-même par un `None`, et le test ne pourrait plus voir ce qui a bougé.
        illisible.chmod(0o644)
    assert code != 0
    apres = _empreintes_des_cibles(tmp_path)
    # **Aucun lot partiel** : les trois surfaces durables ont toutes bougé, ou aucune. Une seule
    # d'entre elles dans le nouvel état est exactement l'état mêlé que l'AC interdit.
    #
    # L'issue entière attendue est **nommée** rang par rang, plutôt que laissée à un « l'une ou
    # l'autre » : deux de ces cibles sont lues avant toute bascule — le rendu précédent, qui doit
    # être archivé avant d'être remplacé, et le manifest, que le chargement du corpus relit — donc
    # leur illisibilité refuse et rien ne bouge ; les deux autres ne sont lues par personne, donc le
    # lot bascule en entier. Les deux issues sont entières, et c'est cela l'invariant.
    issue_entiere = {"evals-latest.json": "publie", "latest.md": "intact",
                     "eval-results.md": "publie", "manifest.json": "intact"}
    bouge = {nom for nom in avant if apres[nom] != avant[nom]}
    attendu = set(avant) if issue_entiere[ordre[rang]] == "publie" else set()
    assert bouge == attendu, (
        f"rang {rang} ({ordre[rang]}) : lot partiel visible, cibles déplacées = {sorted(bouge)}")
    # Et le lot visible est **cohérent** : l'artefact servi et le manifest qui promeut se réclament
    # du même rapport. C'est la propriété qu'un état mêlé briserait en premier.
    publie = json.loads((tmp_path / "data" / "evals-latest.json").read_text(encoding="utf-8"))
    gate = json.loads(
        (tmp_path / "data" / "manifest.json").read_text(encoding="utf-8"))[DOC]["gate"]
    assert publie["report_digest"] == gate["report_digest"], (
        "une surface affirme un verdict que le manifest ne porte pas")
    assert publie["candidate_revision"] == gate["candidate_revision"]
    capsys.readouterr()
    assert _temporaires(tmp_path) == []


def test_un_rendu_precedent_illisible_refuse_de_publier_avant_toute_bascule(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """B3/B6 : « je n'ai pas pu lire » n'est jamais « il n'y avait rien », de bout en bout.

    C'est la moitié déterministe de l'ancien test paramétré, conservée mot pour mot dans ses
    assertions : la cause est **nommée**, le diagnostic dit qu'aucune bascule n'a eu lieu, et le lot
    précédent est intégralement préservé. Le lecteur d'état qui subsiste est celui de l'archive du
    rendu précédent — le plus grave des deux, puisque lui détruit même quand la bascule réussit —,
    et son illisibilité arrête toujours la publication avant le premier octet écrit.
    """
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    avant = _empreintes_des_cibles(tmp_path)
    capsys.readouterr()

    latest = tmp_path / "docs" / "evals" / "latest.md"
    latest.chmod(0o000)
    try:
        code = _cli(tmp_path, monkeypatch,
                    ["--gate", DOC, "--profile", "full", "--repeat", "3",
                     "--candidate-revision", "c" * 40], revision="c" * 40)
        err = capsys.readouterr().err
    finally:
        latest.chmod(0o644)
    assert code != 0
    # **Aucune bascule** : le lot précédent est intégralement préservé, rien n'a été supprimé.
    assert _empreintes_des_cibles(tmp_path) == avant, "une cible a bougé"
    # La cause est **nommée**, et le diagnostic dit qu'aucune bascule n'a eu lieu — jamais que
    # « il n'y avait rien ».
    assert "n'a pas pu être lu" in err
    assert "refus de publier" in err or "refus d'écrire les rapports" in err
    assert "aucune bascule n'a eu lieu" in err
    assert _temporaires(tmp_path) == []


def test_un_latest_illisible_nest_jamais_ecrase_sans_archive(tmp_path: Path) -> None:
    """B3/B6, chemin frère **plus grave** : l'archive détruit même quand la bascule réussit.

    `_preparer_archive` rendait `None` — « rien à archiver » — sur n'importe quelle `OSError`. Un
    `docs/evals/latest.md` illisible n'était donc pas archivé, **puis écrasé** par le nouveau rendu.
    C'est le registre de campagne que la docstring décrit comme « des mesures live que personne ne
    peut reproduire sans repayer ».

    Le contrôle porte sur un fichier réellement illisible (`chmod 000`), pas sur un double.

    Cycle de récupération (B7) : la décision d'archivage est passée dans `_archive_a_ecrire`, qui la
    prend **sans rien écrire**, si bien qu'elle peut lever avant le premier temporaire. La preuve
    est la même, sur la fonction qui porte désormais la lecture.
    """
    from server.evals import publication as pub_mod

    latest = tmp_path.joinpath(*pub_mod.DOCS_LATEST)
    latest.parent.mkdir(parents=True)
    latest.write_text("# campagne précédente, irremplaçable\n", encoding="utf-8")
    avant = latest.read_bytes()
    latest.chmod(0o000)
    try:
        # Sans garde : le refus est **inconditionnel**. La condition précédente s'appuyait sur
        # `runner._lire_ou_absent`, qui n'existe plus, et elle rendait de surcroît le contrôle
        # sautable — un test qui peut ne rien vérifier ne ferme rien.
        with pytest.raises(pub_mod.ArchivePrecedenteIllisible, match="n'a pas pu être lu"):
            pub_mod._archive_a_ecrire(latest, repo_root=tmp_path)
        with pytest.raises(pub_mod.ArchivePrecedenteIllisible, match="n'a pas pu être lu"):
            pub_mod.archiver_latest(latest, repo_root=tmp_path,
                                    ecrire=runner._ecrire_atomique)
    finally:
        latest.chmod(0o644)
    # Le rendu précédent est **intact** : ni archivé à moitié, ni écrasé.
    assert latest.read_bytes() == avant
    # Et une absence reste une absence : rien à archiver, aucune exception.
    absent = tmp_path / "docs" / "evals" / "jamais.md"
    assert pub_mod._archive_a_ecrire(absent, repo_root=tmp_path) is None
    assert pub_mod.archiver_latest(absent, repo_root=tmp_path,
                                   ecrire=runner._ecrire_atomique) is None


def test_ecrire_rapports_ne_laisse_jamais_le_couple_mele(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """B3/B6 → B7 sur le chemin frère `ecrire_rapports` : le couple ne se mêle jamais.

    Ce test exigeait un refus (`EtatPrecedentIllisible`) devant une cible illisible. Ce refus était
    la conséquence d'un besoin qui n'existe plus : pour pouvoir défaire, il fallait d'abord relire.
    Le couple rapport/table est maintenant un lot remis au même appel de `EspacePublie.basculer`, et
    rien n'est modifié tant que l'unique `os.replace` du pointeur n'a pas eu lieu.

    Ce que le refus protégeait — jamais un `eval-results.json` neuf à côté d'un `eval-results.md`
    périmé, puisque `digest_octets(output_json)` alimente `gate.report_digest` — est prouvé ici
    **directement**, et deux fois :

    1. avec la cible réellement illisible (`chmod 000`, pas un double), l'issue est entière ;
    2. avec une `KeyboardInterrupt` levée sur l'atome lui-même — le cas que l'ancien protocole ne
       pouvait pas tenir, puisqu'une interruption pendant la restauration laissait l'état mêlé —,
       **aucune** des deux cibles n'a bougé, ni en contenu ni en type d'entrée.
    """
    json_path = tmp_path / "rapport.json"
    md_path = tmp_path / "rapport.md"
    json_path.write_text('{"ancien": true}\n', encoding="utf-8")
    md_path.write_text("# ancien\n", encoding="utf-8")
    espace = poser_espace(tmp_path, cibles=[Path("rapport.json"), Path("rapport.md")])
    avant = (json_path.read_bytes(), md_path.read_bytes())
    etat_avant = _etat_du_lot([json_path, md_path])
    rapport = {
        "schema_version": 3, "profile": "full", "complete": True, "stop_reason": None,
        "unexecuted_cases": [], "cases_hash": "d" * 64, "cases_planned": 1, "cases_completed": 1,
        "cost_eur": 0.0, "identity": {"run_digest": "a" * 64}, "repeat": 1, "decisions": [],
        "metrics": {"labels": {label: 0 for label in runner.LABELS}, "variants": {},
                    "recall": 1.0, "average_cost_eur": 0.0, "latency_p50_ms": 0,
                    "latency_p95_ms": 0, "cost_p95_eur": 0.0, "ne_tranche_pas_rate": 0.0},
        "results": [],
    }
    # 2. L'interruption sur l'atome : le rang qui n'a jamais eu de réparation possible.
    replace_reel = runner.os.replace

    def _replace(source: Any, cible: Any) -> None:
        if Path(cible).name == POINTEUR:
            raise KeyboardInterrupt()
        replace_reel(source, cible)

    monkeypatch.setattr(runner.os, "replace", _replace)
    with pytest.raises(KeyboardInterrupt):
        runner.ecrire_rapports(rapport, json_path, md_path, preuve_externe=None, espace=espace)
    monkeypatch.setattr(runner.os, "replace", replace_reel)
    assert (json_path.read_bytes(), md_path.read_bytes()) == avant
    assert _etat_du_lot([json_path, md_path]) == etat_avant, (
        "une interruption sur l'atome a laissé une cible dans le nouvel état")
    assert not [p.name for p in tmp_path.glob(".*.tmp")]
    assert _temporaires(tmp_path) == []

    # 1. La cible réellement illisible : l'issue reste entière — les deux cibles portent le nouveau
    #    rendu, ou aucune. Un `eval-results.json` neuf à côté d'un `.md` périmé est le seul état
    #    interdit, et c'est celui-là que l'assertion nomme.
    md_path.chmod(0o000)
    try:
        runner.ecrire_rapports(rapport, json_path, md_path, preuve_externe=None, espace=espace)
    finally:
        md_path.chmod(0o644)
    apres = (json_path.read_bytes(), md_path.read_bytes())
    bouge = {nom for nom, ancien, neuf in (("json", avant[0], apres[0]),
                                           ("md", avant[1], apres[1])) if ancien != neuf}
    assert bouge == {"json", "md"}, (
        f"couple mêlé : {sorted(bouge)} — le rapport et sa table basculent ensemble ou pas du tout")
    assert json.loads(json_path.read_text(encoding="utf-8"))["cases_hash"] == "d" * 64
    assert md_path.read_text(encoding="utf-8").startswith("# Résultat des questions-témoins")
    assert _temporaires(tmp_path) == []


def test_la_decision_de_gate_partage_un_seul_repere_entre_ses_trois_preuves(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue du tour N1–N3, constat 13 : le repère partagé de la décision n'était observé par rien.

    `_main` pince **un** repère et le passe à `construire_contexte`, `preuve_de_structure` et
    `preuve_darbre` : une décision de gate ne peut plus être composée de trois générations. Mais
    `lecture` y est un argument optionnel dont le défaut re-pince chacun de son côté ; retirer les
    trois `lecture=…` laissait tous les tests de gate verts, et sous une publication concurrente le
    verdict redevenait un mélange — un `structure_prouvee_rate` mesuré contre un `structure.json`
    que le corpus chargé ne décrit pas.

    La sonde observe l'**opération réelle**, jamais les trois fonctions appelées à la main : elle
    exige que les trois reçoivent le même objet `Lecture`, et qu'aucune ne retombe sur son défaut.
    """
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    reperes: dict[str, Any] = {}
    for nom in ("verifier_composition_gate_full", "construire_contexte",
                "preuve_de_structure", "preuve_darbre"):
        vrai = getattr(runner, nom)

        def _noter(*a: Any, _nom: str = nom, _vrai: Any = vrai, **k: Any) -> Any:
            reperes[_nom] = k.get("lecture", a[4] if _nom == "verifier_composition_gate_full"
                                  and len(a) > 4 else None)
            resultat = _vrai(*a, **k)
            if _nom == "verifier_composition_gate_full":
                data = Path(a[0])
                EspacePublie(tmp_path, data).basculer([
                    (data / DOC / "source.js", b"var kb = {};"),
                    (data / DOC / "source.pdf", None),
                ])
            return resultat

        monkeypatch.setattr(runner, nom, _noter)

    _cli(tmp_path, monkeypatch, ["--gate", DOC, "--profile", "full", "--repeat", "3",
                                 "--candidate-revision", REVISION])

    assert set(reperes) == {"verifier_composition_gate_full", "construire_contexte",
                            "preuve_de_structure", "preuve_darbre"}, (
        f"l'opération de gate n'a pas appelé les quatre décisions : {sorted(reperes)}")
    manquants = [nom for nom, repere in reperes.items() if repere is None]
    assert manquants == [], f"ces preuves sont retombées sur leur repère par défaut : {manquants}"
    assert len({id(repere) for repere in reperes.values()}) == 1, (
        "les quatre preuves de la décision de gate pincent chacune leur génération : le verdict "
        "peut être composé de plusieurs états")
