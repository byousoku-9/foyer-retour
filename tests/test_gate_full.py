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
from pathlib import Path
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
from server.evals.plancher import charger_plancher

DOC = "contrat-neutre"
# Volontairement neutre : aucun vocabulaire d'assureur reel, aucune formulation de temoin.
TEXTE = ("Le bien decrit au present chapitre est garanti selon les conditions qui y figurent, "
         "sous reserve des exceptions enoncees a la section suivante.")
REVISION = "0" * 40

# Les neuf témoins que la story ajoute. La liste est **relue du plancher** plus bas : l'écrire ici
# sert à nommer ce que l'AC exige, pas à le définir.
NEUFS = (
    "parsing_ok_rate", "blocs_attendus_ouverts_rate", "citations_retrouvees_rate",
    "zero_5xx_technique_rate", "typage_confirme_rate", "structure_prouvee_rate",
    "stabilite_claim_decisionnelle", "anti_rustine_pass_rate", "metamorphique_pass_rate",
)


# --- fabriques neutres ----------------------------------------------------------------------------

def _settings(**kw: Any) -> Settings:
    defauts: dict[str, Any] = {"anthropic_api_key": "cle-de-test", "guide_doc_id": "guide-neutre",
                               "sinistre_doc_id": DOC}
    defauts.update(kw)
    return Settings(_env_file=None, **defauts)


def _document(*, kind_source: str | None = "manual") -> Document:
    doc = Document(
        doc_id=DOC, kind="contrat", title="Contrat neutre", edition="2020",
        source_hash="s", ingest_fingerprint="f",
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


def _decisions(resultats: list[runner.Resultat], cas: list[runner.Cas], *, repeat: int = 3,
               exigences_full: bool = True, structure: tuple[int, int] | None = (1, 1),
               producer: str = "orchestrator",
               non_executes: list[str] | None = None) -> dict[str, Any]:
    """Les décisions du gate, indexées par métrique. `producer=orchestrator` isole la valeur.

    Sous `builder`, **toutes** les décisions sont rouges (« producteur non probant ») : c'est la
    règle trusted de 4.2b, et elle masquerait ce que ces tests veulent voir — la valeur mesurée.
    """
    charge = charger_plancher()
    decisions = runner.construire_decisions(
        resultats, cas, plancher=charge, repeat=repeat, run_digest="a" * 64, producer=producer,
        non_executes=non_executes, exigences_full=exigences_full, structure=structure)
    return {d.metric: d for d in decisions}


# --- le plancher porte les neuf témoins, et ne perd rien ------------------------------------------

def test_les_neuf_temoins_vivent_dans_le_plancher_et_nabaissent_rien() -> None:
    """Boundaries : « tout nouveau seuil vit dans `plancher.yaml`, jamais en dur ».

    Et il s'y ajoute **sans jamais abaisser ni retirer** un témoin importé : les quatre seuils du
    floor 4.2a et les huit témoins de 4.2b sont encore là, à leur valeur, et les digests des
    snapshots figés n'ont pas bougé — c'est `charger_plancher` qui le vérifie, en refusant de
    charger sinon.
    """
    charge = charger_plancher()
    par_metric = {t.metric: t for t in charge.plancher.temoins}
    for metric in NEUFS:
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
    # Hors gate `full`, aucune des neuf métriques n'apparaît dans les décisions.
    hors = _decisions([_resultat()], cas, exigences_full=False)
    assert not (set(NEUFS) & set(hors))
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


def test_la_structure_non_prouvee_rougit_le_gate_et_cest_letat_reel(tmp_path: Path) -> None:
    """I/O matrix : aucun `structure_hash` au manifest, ou bloquant de structure ⇒ rouge.

    C'est l'état réel du corpus servi : aucune `structure.json` n'y existe, la story 4.2c n'a jamais
    été exercée dessus, et la réingestion est une dette de l'orchestrateur. Le gate doit le dire.
    """
    ctx = _contexte()
    data = tmp_path / "data"
    (data / DOC).mkdir(parents=True)
    # 1. Rien de déclaré : non prouvée.
    assert runner.preuve_de_structure(data, ctx, [DOC]) == (0, 1)
    decision = _decisions([_resultat(repetition=r) for r in (1, 2, 3)], [_cas_sinistre()],
                          structure=(0, 1))["structure_prouvee_rate"]
    assert _rouge(decision) and decision.value == 0.0 and decision.scope == "run"
    # 2. Déclarée et concordante, sans bloquant : prouvée.
    octets = b'{"doc_id": "contrat-neutre"}\n'
    (data / DOC / "structure.json").write_bytes(octets)
    ctx.index.corpus.manifest[DOC].structure_hash = hashlib.sha256(octets).hexdigest()
    assert runner.preuve_de_structure(data, ctx, [DOC]) == (1, 1)
    # 3. Déclarée mais l'artefact a bougé : non prouvée (fail-closed).
    (data / DOC / "structure.json").write_bytes(octets + b"\n")
    assert runner.preuve_de_structure(data, ctx, [DOC]) == (0, 1)
    # 4. Concordante, mais le rapport porte un bloquant de structure : non prouvée.
    (data / DOC / "structure.json").write_bytes(octets)
    (data / DOC / "report.json").write_text(json.dumps({
        "doc_id": DOC,
        "checks": [{"name": "structure_proposee", "level": "bloquant", "detail": "ligne_omise"}],
    }), encoding="utf-8")
    assert runner.preuve_de_structure(data, ctx, [DOC]) == (0, 1)


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


def test_les_neuf_temoins_manquants_sont_rouges_et_pas_neutres() -> None:
    """Boundaries : « une preuve absente est **rouge**, jamais neutre ».

    Un gate `full` qui ne mesure rien du tout produit neuf rouges chiffrés, pas neuf silences —
    c'est la fermeture par vacuité, et c'est ce qui rend ces témoins impossibles à oublier.
    """
    cas = [_cas_sinistre(), _cas_parsing()]
    manquantes = [f"{c.id}#r{r}" for c in cas for r in (1, 2, 3)]
    decisions = _decisions([], cas, structure=None, non_executes=manquantes)
    for metric in NEUFS:
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


def _data_dir(racine: Path) -> Path:
    data = racine / "data"
    dossier = data / DOC
    dossier.mkdir(parents=True, exist_ok=True)
    doc = _document()
    octets = json.dumps(doc.model_dump(mode="json", exclude_defaults=True), ensure_ascii=False,
                        sort_keys=True).encode("utf-8")
    (dossier / "document.json").write_bytes(octets)
    (dossier / "summary.md").write_text("# doc", encoding="utf-8")
    # **Idempotent** : un manifest déjà posé n'est pas réécrit. Un test qui installe un gate vert
    # puis lance le runner doit retrouver ce gate — le recréer ici effacerait précisément l'état
    # que la non-mutation du dernier vert est censée protéger.
    if not (data / "manifest.json").is_file():
        (data / "manifest.json").write_text(json.dumps({
            DOC: {"status": "servi", "source_hash": "s", "ingest_fingerprint": "f",
                  "document_hash": hashlib.sha256(octets).hexdigest(), "edition": "2020",
                  "overlay_hash": None, "gate": None}}, indent=2) + "\n", encoding="utf-8")
    return data


def _cli(racine: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], *,
         cases_parsing: bool = True) -> int:
    data = _data_dir(racine)
    cases = _cases_dir(racine, parsing=cases_parsing)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", _settings)
    return runner.main(argv + ["--cases-dir", str(cases), "--data-dir", str(data)])


def _manifest_intact(racine: Path) -> None:
    manifest = json.loads((racine / "data" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[DOC]["gate"] is None, "un refus de tourner ne touche jamais le manifest"


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
    assert set(NEUFS) <= metriques
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


def test_un_second_gate_full_rouge_ne_touche_pas_un_vert_existant(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 2, sur le chemin complet : manifest **byte-identique**, publication écrite quand même."""
    data = _data_dir(tmp_path)
    brut = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    brut[DOC]["gate"] = {
        "profile": "full", "source_hash": "s", "ingest_fingerprint": "f", "overlay_hash": None,
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
    data = _data_dir(tmp_path)
    (data / DOC / "source.pdf").write_bytes(b"%PDF-1.4 minimal")
    _cases_dir(tmp_path, parsing=False)
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION], cases_parsing=False)
    assert code == 2
    err = capsys.readouterr().err
    assert "ingéré depuis un PDF" in err and "cas `parsing`" in err
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

    # Et sur le chemin complet : sans PDF, un gate `full` sans cas `parsing` tourne comme avant —
    # il écrit son gate (rouge, faute de preuve orchestrateur), la garde ne s'est pas déclenchée.
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION], cases_parsing=False) == 1
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


def test_une_publication_impossible_empeche_le_run_detre_vert(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue P8 : « FR41 n'a pas publié » ne doit pas être indiscernable de « rien n'a tourné ».

    L'échec était avalé par un `except Exception` qui imprimait sur stderr et laissait sortir en 0 :
    le seul endroit où le défaut se lisait était une ligne que personne ne relit après un succès.

    Le code reste dans la ligne de partage d'AD-8/D4 — ni 2, ni 3 (qui promet « manifest non
    modifié », faux ici), ni 4 —, et le gate déjà écrit n'est pas touché.
    """
    ecrire_reel = runner._ecrire_atomique

    def _ecrire(path: Path, contenu: str) -> None:
        if path.name == "evals-latest.json":
            raise OSError("répertoire de publication en lecture seule")
        ecrire_reel(path, contenu)

    monkeypatch.setattr(runner, "_ecrire_atomique", _ecrire)
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION])
    assert code != 0
    err = capsys.readouterr().err
    assert "échec de publication" in err
    # Le gate mesuré reste écrit : ce qui a été mesuré reste mesuré.
    manifest = json.loads((tmp_path / "data" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[DOC]["gate"] is not None


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
    """Revue P4 : le chemin qui **ingère** un verdict rempli va jusqu'aux quatre surfaces.

    Sans lui, FR47 restait une bibliothèque sans appelant et `statut` ne pouvait jamais quitter
    `planifiee` : l'orchestrateur produisait un verdict que rien ne lisait.
    """
    from server.evals.relecture import blocs_cles_du_rapport, plan_de_relecture

    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION]) == 1
    rapport = json.loads((tmp_path / "eval-results.json").read_text(encoding="utf-8"))
    plan = plan_de_relecture(_contexte().index, blocs_cles_du_rapport(rapport),
                             candidate_revision=REVISION)
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({
        "schema_version": 1, "candidate_revision": REVISION, "plan_digest": plan.plan_digest,
        "verdicts": [{"block_id": b.block_id, "verdict": "concordant",
                      "image_sha256": "a" * 64, "note": ""} for b in plan.blocs],
    }), encoding="utf-8")

    assert _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION, "--relecture-verdict", str(verdict)]) == 1
    publie = json.loads((tmp_path / "data" / "evals-latest.json").read_text(encoding="utf-8"))
    assert publie["seconde_lecture"] == {"statut": "concordante", "blocs_planifies": 1,
                                         "blocs_verifies": 1}
    # La limite disparaît : elle était dérivée de l'état, pas rédigée.
    assert not any("seconde lecture" in limite for limite in publie["limites"])


def test_un_verdict_de_seconde_lecture_qui_ne_concorde_pas_empeche_le_vert(
        tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un verdict adossé à un autre plan n'est jamais publié au rabais : il fait échouer la publication."""
    monkeypatch.setattr(runner.pipeline_sinistre, "run", _double_sinistre())
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({
        "schema_version": 1, "candidate_revision": REVISION, "plan_digest": "0" * 64,
        "verdicts": [{"block_id": f"{DOC}:p1:1", "verdict": "concordant",
                      "image_sha256": "a" * 64, "note": ""}],
    }), encoding="utf-8")
    code = _cli(tmp_path, monkeypatch,
                ["--gate", DOC, "--profile", "full", "--repeat", "3",
                 "--candidate-revision", REVISION, "--relecture-verdict", str(verdict)])
    assert code != 0
    assert "échec de publication" in capsys.readouterr().err
