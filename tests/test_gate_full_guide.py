"""Story 4.5 — un gate `full` du **guide**, mesuré sur le corpus servi réel du dépôt.

Pourquoi un fichier à part. `tests/test_gate_full.py` prouve les conditions rouges sur un corpus
synthétique neutre, et c'est ce qu'il doit faire. Mais deux findings de la revue portent précisément
sur ce qu'un corpus synthétique **ne peut pas** montrer :

- **I1** — `typage_confirme_rate` était opposé à toutes les preuves, guide compris. Or aucun bloc du
  guide ne porte de `kind_source` : le témoin y était rouge quelle que soit la qualité du candidat.
  Un test dont la fixture force `kind_confirmed: True` n'aurait jamais vu le mur ; il faut le corpus
  réel, et son `0 sur 506`.
- **B4, volet guide** — restreindre `structure_prouvee_rate` aux documents issus d'un PDF a retiré au
  guide **toute** exigence de structure. Le témoin qui l'y remplace doit être vérifiable sur ce que
  l'ingestion réelle du guide produit réellement, pas sur un rapport écrit par le test.

Ce fichier ne touche donc jamais `data/` : il le **lit**, et fait toutes ses écritures sur une copie
temporaire. Aucun réseau, aucune clé, aucune fixture — la ré-ingestion du guide est déterministe et
purement locale.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from server.app.config import REPO_ROOT, Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import load_corpus
from server.evals import run as runner
from server.evals.plancher import charger_plancher
from server.ingest import kb_to_blocks

DATA = REPO_ROOT / "data"


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **({"anthropic_api_key": "cle-de-test"} | kw))


def _contexte(data_dir: Path) -> runner.Contexte:
    corpus = load_corpus(data_dir, allow_ungated=True)
    return runner.Contexte(settings=_settings(), index=Index(corpus), client=None,
                           pipeline_digest_hex="pd", prompts_digest_hex="pp")


def _guide_doc_id() -> str:
    """Jamais un slug écrit à la main : le réglage nomme le document du guide (AD-14)."""
    return _settings().guide_doc_id


def _cas_guide(case_id: str = "g-reel") -> runner.Cas:
    return runner.Cas.model_validate({
        "id": case_id, "suite": "guide", "profile": "vertical",
        "question": "Ou trouve-t-on la marche a suivre decrite par la fiche ?",
        "expected": {"found": True}, "mode_attendu": "bonne_reponse",
        "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
    })


def _resultat(doc_id: str, block_id: str, kind: str, *, repetition: int,
              **kw: Any) -> runner.Resultat:
    defauts: dict[str, Any] = {
        "id": "g-reel", "suite": "guide", "label": "bonne_reponse", "variant": "micro",
        "repetition": repetition, "doc_id": doc_id, "found": True, "http": 200,
        "expected_block_ids": [block_id], "opened_block_ids": [block_id],
        # Le typage tel qu'il est **réellement** dans le corpus servi : jamais confirmé.
        "proofs": [{"doc_id": doc_id, "block_id": block_id, "kind": kind,
                    "quote_hash": "h", "kind_confirmed": False}],
    }
    defauts.update(kw)
    return runner.Resultat(**defauts)


def _decisions(resultats: list[runner.Resultat], cas: list[runner.Cas], *,
               structure: tuple[int, int] | None, arbre: tuple[int, int] | None,
               repeat: int = 3) -> dict[str, Any]:
    decisions = runner.construire_decisions(
        resultats, cas, plancher=charger_plancher(), repeat=repeat, run_digest="a" * 64,
        producer="orchestrator", exigences_full=True, structure=structure, arbre=arbre)
    return {d.metric: d for d in decisions}


# --- I1 : le typage juridique n'est pas une exigence du périmètre guide ----------------------------

def test_aucun_bloc_du_guide_servi_ne_porte_de_typage_confirme() -> None:
    """Le fait qui rendait le témoin infranchissable — mesuré, pas supposé.

    Le guide est une copie de site : ses blocs sont des paragraphes, des titres et des tableaux, pas
    des clauses juridiques. Aucun chemin de production ne leur pose de `kind_source` (`manual` ou
    `model_verified` viennent du typage des contrats). Opposer `typage_confirme_rate` à ce document,
    c'était lui demander une preuve qu'aucun travail ne peut produire : un mur, pas une exigence.
    """
    corpus = load_corpus(DATA, allow_ungated=True)
    guide = corpus.documents[_guide_doc_id()]
    assert guide.blocks, "le guide servi doit porter des blocs"
    assert {b.kind_source for b in guide.blocks} == {None}
    assert not any(b.kind_confirmed for b in guide.blocks)


def test_un_gate_full_du_guide_nest_plus_structurellement_rouge_par_le_typage() -> None:
    """Revue I1 : `typage_confirme_rate` ne s'oppose plus à un lot qui ne porte aucune clause.

    Le témoin est désormais de portée `suite:sinistre` — l'univers exact des clauses fondatrices
    qu'AD-6 vise. Sur un lot guide, il n'est pas applicable : aucune décision, ni verte ni rouge.
    Ce que le guide doit prouver, il le prouve — et le test le montre en le faisant rougir.
    """
    doc_id = _guide_doc_id()
    corpus = load_corpus(DATA, allow_ungated=True)
    bloc = corpus.documents[doc_id].blocks[0]
    cas = [_cas_guide()]

    tenu = [_resultat(doc_id, bloc.block_id, bloc.kind, repetition=r) for r in (1, 2, 3)]
    decisions = _decisions(tenu, cas, structure=(0, 0), arbre=(1, 1))

    # I1 : le témoin de typage n'est plus opposé à ce périmètre — et ce n'est pas un vert offert,
    # c'est une absence d'applicabilité, la règle que `_temoin_applicable` porte déjà.
    assert "typage_confirme_rate" not in decisions
    temoin_typage = charger_plancher().plancher.temoin("typage_confirme_rate")
    assert temoin_typage is not None
    assert runner._temoin_applicable(temoin_typage, cas, exigences_full=True) is False
    # De même pour la preuve de structure PDF : le guide n'en a pas, et son pendant la remplace.
    assert "structure_prouvee_rate" not in decisions

    # Ce qui **tient** le gate du guide, en revanche, est là et vert.
    applicables = {t.metric for t in charger_plancher().plancher.temoins
                   if t.arme_par == "gate_full"
                   and runner._temoin_applicable(t, cas, exigences_full=True)}
    assert applicables == {"blocs_attendus_ouverts_rate", "citations_retrouvees_rate",
                           "zero_5xx_technique_rate", "arbre_prouve_rate",
                           "anti_rustine_pass_rate", "metamorphique_pass_rate"}
    for metric in ("blocs_attendus_ouverts_rate", "citations_retrouvees_rate",
                   "zero_5xx_technique_rate", "arbre_prouve_rate"):
        assert decisions[metric].status == "green", metric

    # Et chacun des trois témoins mesurés rougit dès que le système ne tient plus — sinon ce serait
    # trois verts décoratifs.
    aveugle = [_resultat(doc_id, bloc.block_id, bloc.kind, repetition=r, opened_block_ids=[])
               for r in (1, 2, 3)]
    assert _decisions(aveugle, cas, structure=(0, 0),
                      arbre=(1, 1))["blocs_attendus_ouverts_rate"].status == "red"
    introuvable = [_resultat(doc_id, bloc.block_id, bloc.kind, repetition=r,
                             label="citation_introuvable", found=False) for r in (1, 2, 3)]
    assert _decisions(introuvable, cas, structure=(0, 0),
                      arbre=(1, 1))["citations_retrouvees_rate"].status == "red"
    assert _decisions(tenu, cas, structure=(0, 0),
                      arbre=(0, 1))["arbre_prouve_rate"].status == "red"


# --- B4, volet guide : une exigence de structure que le guide peut satisfaire honnêtement ----------

def test_le_corpus_servi_prouve_son_arbre_et_le_gate_le_dit() -> None:
    """L'état **réel** : le guide servi a été réingéré le 2026-09-02 et `report.json` porte
    l'attestation d'arbre avec son empreinte.

    Avant cette réingestion, le témoin était rouge (arbre déclaré `ok` sans empreinte), publié tel
    quel comme `structure_prouvee_rate` l'est pour les contrats : un plancher ne se satisfait pas
    d'une déclaration. Il redeviendrait rouge si l'artefact servi cessait de porter sa preuve.
    """
    doc_id = _guide_doc_id()
    avant = {chemin: chemin.read_bytes() for chemin in sorted(DATA.rglob("*")) if chemin.is_file()}
    ctx = _contexte(DATA)
    assert runner.preuve_darbre(DATA, ctx, [doc_id]) == (1, 1)
    # Le témoin PDF, lui, ne compte pas le guide du tout : les deux dénominateurs se partagent le lot.
    assert runner.preuve_de_structure(DATA, ctx, [doc_id]) == (0, 0)
    # Lecture seule, vérifiée : ce test ne touche jamais le corpus servi.
    assert {chemin: chemin.read_bytes()
            for chemin in sorted(DATA.rglob("*")) if chemin.is_file()} == avant


def test_la_reingestion_reelle_du_guide_rend_son_arbre_prouvable(tmp_path: Path) -> None:
    """Revue B4 : le témoin est **satisfaisable honnêtement**, et par le seul chemin qui le doit.

    On copie le corpus servi, on relance sur lui l'ingestion réelle du guide — déterministe, locale,
    sans réseau — et le témoin devient vert. C'est la preuve qu'il ne s'agit pas d'un mur : le gate
    `full` du guide a une action qui le satisfait, et cette action est exactement celle qui produit
    la preuve.

    Puis on fabrique. Un `report.json` écrit à la main, et un `document.json` qui bouge après coup :
    ni l'un ni l'autre ne verdit quoi que ce soit.
    """
    from server.app.domain.ingest import detail_attestation_arbre

    doc_id = _guide_doc_id()
    data = tmp_path / "data"
    # `symlinks=True` : `data/manifest.json` et `data/evals-latest.json` sont désormais des liens
    # vers l'espace de publication (`server/evals/espace.py`, story 4.5, B7). Les suivre les
    # déréférencerait — et `evals-latest.json` n'a pas de cible tant qu'aucun run ne l'a publié
    # (un lien pendant, l'équivalent d'une absence) : `shutil.copytree` par défaut lève alors une
    # `Error`. Copier le lien préserve exactement la même disposition, pendante comprise.
    shutil.copytree(DATA, data, symlinks=True)
    manifest_path = data / "manifest.json"
    edition = json.loads(manifest_path.read_text(encoding="utf-8"))[doc_id]["edition"]

    # Le corpus servi porte déjà sa preuve (réingéré le 2026-09-02) : la relance doit la conserver.
    assert runner.preuve_darbre(data, _contexte(data), [doc_id]) == (1, 1)

    # 1. L'ingestion réelle, sur la copie. C'est le chemin de production, pas un raccourci de test.
    report, entry = kb_to_blocks.run(data / doc_id, edition=edition)
    assert not report.blocking and entry.status == "servi"
    ctx = _contexte(data)
    assert runner.preuve_darbre(data, ctx, [doc_id]) == (1, 1)
    # Le document reste servi : prouver sa structure ne le met pas en quarantaine.
    assert doc_id in ctx.index.corpus.documents

    # ... et la décision du gate devient verte, sur le même chemin que le reste.
    cas = [_cas_guide()]
    bloc = ctx.index.corpus.documents[doc_id].blocks[0]
    resultats = [_resultat(doc_id, bloc.block_id, bloc.kind, repetition=r) for r in (1, 2, 3)]
    decision = _decisions(resultats, cas, structure=(0, 0),
                          arbre=runner.preuve_darbre(data, ctx, [doc_id]))["arbre_prouve_rate"]
    assert decision.status == "green" and decision.value == 1.0 and decision.scope == "suite:guide"

    # 2. Une attestation **fabriquée** ne verdit rien : le couple attesté doit être celui du manifest.
    rapport = json.loads((data / doc_id / "report.json").read_text(encoding="utf-8"))
    fabrique = dict(rapport)
    fabrique["checks"] = [
        {"name": "invariants_arbre", "level": "info",
         "detail": detail_attestation_arbre(document_hash="f" * 64, ingest_fingerprint="e" * 64)}
        if c["name"] == "invariants_arbre" else c for c in rapport["checks"]]
    (data / doc_id / "report.json").write_text(json.dumps(fabrique), encoding="utf-8")
    assert runner.preuve_darbre(data, _contexte(data), [doc_id]) == (0, 1)

    # 3. La forme historique, `invariants_arbre: ok`, ne verdit rien non plus.
    nu = dict(rapport)
    nu["checks"] = [{"name": "invariants_arbre", "level": "info", "detail": "ok"}
                    if c["name"] == "invariants_arbre" else c for c in rapport["checks"]]
    (data / doc_id / "report.json").write_text(json.dumps(nu), encoding="utf-8")
    assert runner.preuve_darbre(data, _contexte(data), [doc_id]) == (0, 1)

    # 4. Le rapport authentique remis, mais l'arbre remplacé : l'attestation se détache d'elle-même.
    (data / doc_id / "report.json").write_text(json.dumps(rapport), encoding="utf-8")
    assert runner.preuve_darbre(data, _contexte(data), [doc_id]) == (1, 1)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[doc_id]["document_hash"] = "9" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ctx_perime = runner.Contexte(
        settings=_settings(), index=Index(load_corpus(data, allow_ungated=True)), client=None,
        pipeline_digest_hex="pd", prompts_digest_hex="pp")
    ctx_perime.index.corpus.manifest[doc_id].document_hash = "9" * 64
    assert runner.preuve_darbre(data, ctx_perime, [doc_id]) == (0, 1)


def test_aucun_perimetre_servi_nest_laisse_sans_exigence_de_structure() -> None:
    """La propriété que la revue exige, énoncée telle quelle : **aucun** périmètre sans preuve.

    Les deux témoins de structure se partagent le lot par la règle `SOURCE_FILES`, et la garde de
    composition du runner refuse **avant tout appel** un gate `full` dont le témoin couvrant ne
    serait pas armé. Ce test dit la moitié déclarative : chacune des deux suites que le dépôt sert
    arme bien une preuve de structure. Si l'une des deux venait à être retirée sans que l'autre
    couvre son périmètre, il rougirait — c'est exactement le défaut que la revue a nommé.
    """
    temoins = charger_plancher().plancher.temoins

    def _armes(cas: list[runner.Cas]) -> set[str]:
        return {t.metric for t in temoins if t.arme_par == "gate_full"
                and runner._temoin_applicable(t, cas, exigences_full=True)}

    # Le guide : sa suite arme la preuve d'arbre, et **pas** la preuve de structure PDF.
    guide = _armes([_cas_guide()])
    assert "arbre_prouve_rate" in guide and "structure_prouvee_rate" not in guide

    # Un contrat : sous `full`, sa suite `parsing` entre dans le lot (`suites_du_gate`) et arme la
    # preuve de structure PDF — c'est ce que la garde de composition rend obligatoire.
    contrat = _armes([
        runner.Cas.model_validate({
            "id": "x-sinistre", "suite": "sinistre", "profile": "vertical",
            "question": "Cette situation entre-t-elle dans ce qui est decrit ?",
            "faits": {"description": "Un bien decrit a subi une atteinte."},
            "expected": {"found": True},
            "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
            "mode_attendu": "bonne_reponse"}),
        runner.Cas.model_validate({
            "id": "x-parsing", "suite": "parsing", "profile": "full", "famille": "garantie",
            "question": "Le texte du bloc est-il celui du contrat imprime ?",
            "expected": {"found": True, "block_ids": ["x-doc:p1:1"], "text_norm": "texte"},
            "truth": {"source": "lecture_humaine", "validated_by_expert": False, "note": "relu"},
            "mode_attendu": "bonne_reponse"}),
    ])
    assert "structure_prouvee_rate" in contrat and "arbre_prouve_rate" not in contrat
