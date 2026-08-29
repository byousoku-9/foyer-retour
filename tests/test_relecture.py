"""Story 4.5 / FR47 — La seconde lecture : le plan est calculable, le verdict est contrôlé.

Le partage est celui de la règle trusted, et il tient en une phrase : **le builder livre le format et
son contrôle ; l'orchestrateur produit le verdict**. Ces tests couvrent donc les deux moitiés qui
appartiennent au builder, et rien de plus :

1. `plan_de_relecture` — déterministe, dérivé du **corpus servi**, sans réseau, sans clé et sans
   rasterisation. Chaque entrée porte de quoi relire : `doc_id`, `block_id`, page, bbox, texte
   normalisé, et l'URL de l'image de page servie par la route de la story 3.4.
2. `valider_verdict` — quatre liens exigés (schéma exact, révision, empreinte du plan, couverture),
   et un refus au premier écart.

Ce qui n'est **pas** ici, et pourquoi : aucun PDF n'existe dans ce worktree, la route de 3.4 rend
déjà l'image avec sa vérification de source et son surlignage sur les `Line.bbox` exactes, et
dupliquer PyMuPDF sous `server/evals/` ferait dépendre `evals` de la couche `api` — hors de la table
du spine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.text import normalize
from server.app.domain.document import Document, Node
from server.app.domain.ingest import ManifestEntry
from server.evals.relecture import (PlanRelecture, RelectureInvalide, ROUTE_PAGE, ecrire_plan,
                                    empreinte_image, plan_de_relecture, valider_verdict)

DOC = "contrat-neutre"
TEXTE = "Le mobilier garni de la residence est garanti lors d un evenement soudain."
REVISION = "7" * 40


def _index_de_test(*, avec_page: bool = True) -> Index:
    doc = Document(
        doc_id=DOC, kind="contrat", title="Contrat neutre", edition="2020",
        source_hash="s", ingest_fingerprint="f",
        nodes=[Node(node_id=f"{DOC}:n1", level=1, title="N1",
                    items=[{"block_id": f"{DOC}:p3:1"}, {"block_id": f"{DOC}:p3:2"}])],
        blocks=[
            {"block_id": f"{DOC}:p3:1", "loc": "p3", "seq": 1, "kind": "garantie",
             "page": 3 if avec_page else None,
             "bbox": [10.0, 20.0, 300.0, 40.0] if avec_page else None, "text": TEXTE},
            {"block_id": f"{DOC}:p3:2", "loc": "p3", "seq": 2, "kind": "exclusion",
             "page": 3, "bbox": [10.0, 60.0, 300.0, 80.0], "text": TEXTE + " Sauf usure."},
        ])
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    corpus = Corpus(documents={DOC: doc},
                    manifest={DOC: ManifestEntry(status="servi", source_hash="s",
                                                 ingest_fingerprint="f", document_hash="d",
                                                 edition="2020")},
                    summaries={DOC: "# doc"}, alerts={DOC: []})
    return Index(corpus)


def _plan() -> PlanRelecture:
    return plan_de_relecture(_index_de_test(), [f"{DOC}:p3:2", f"{DOC}:p3:1"],
                             candidate_revision=REVISION)


def _verdict(plan: PlanRelecture, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "candidate_revision": REVISION,
        "plan_digest": plan.plan_digest,
        "verdicts": [{"block_id": b.block_id, "verdict": "concordant",
                      "image_sha256": "a" * 64, "note": ""} for b in plan.blocs],
    }
    base.update(kw)
    return base


# --- le plan -------------------------------------------------------------------------------------

def test_le_plan_porte_ce_quil_faut_pour_relire_et_rien_de_plus() -> None:
    """FR47 : par bloc clé, `doc_id`, `block_id`, page, bbox, `text_norm` et l'URL de l'image.

    L'URL est celle de la route de 3.4, avec son paramètre canonique `blocks` — la story ne
    rasterise rien elle-même : l'image vient de la route qui la sert déjà, avec son surlignage sur
    les `Line.bbox` exactes et sa vérification de source.
    """
    plan = _plan()
    assert [b.block_id for b in plan.blocs] == [f"{DOC}:p3:1", f"{DOC}:p3:2"]  # trié, déterministe
    premier = plan.blocs[0]
    assert premier.doc_id == DOC and premier.page == 3
    assert premier.bbox == [10.0, 20.0, 300.0, 40.0]
    assert premier.text_norm == normalize(TEXTE)
    assert premier.image_url == ROUTE_PAGE.format(doc_id=DOC, page=3, block_id=premier.block_id)
    assert premier.image_url.startswith("/api/v1/documents/") and "blocks=" in premier.image_url
    assert set(premier.model_dump()) == {"doc_id", "block_id", "page", "bbox", "text_norm",
                                         "image_url"}


def test_le_plan_est_deterministe_et_sans_doublon() -> None:
    """Deux appels sur le même corpus rendent le même plan, donc le même `plan_digest`.

    C'est ce qui permet au verdict de s'y adosser : un plan qui changerait d'un appel à l'autre
    rendrait tout verdict inintelligible une seconde après avoir été produit.
    """
    a = plan_de_relecture(_index_de_test(), [f"{DOC}:p3:1", f"{DOC}:p3:1", f"{DOC}:p3:2"],
                          candidate_revision=REVISION)
    b = plan_de_relecture(_index_de_test(), [f"{DOC}:p3:2", f"{DOC}:p3:1"], candidate_revision=REVISION)
    assert a.model_dump() == b.model_dump()
    assert a.plan_digest == b.plan_digest and len(a.plan_digest) == 64
    assert len(a.blocs) == 2


def test_un_bloc_sans_image_possible_nentre_pas_dans_le_plan() -> None:
    """On ne demande pas de relire une image qui n'existe pas.

    Un bloc absent du corpus servi, ou dont l'ingestion n'a retenu ni page ni bbox, ne peut pas être
    surligné : l'inclure ferait pointer le second lecteur vers une page qu'aucune route ne rendra.
    Ce qui manque se lit à la **longueur** du plan, pas à une entrée fabriquée.
    """
    sans_page = plan_de_relecture(_index_de_test(avec_page=False), [f"{DOC}:p3:1"],
                                  candidate_revision=REVISION)
    assert sans_page.blocs == []
    inconnu = plan_de_relecture(_index_de_test(), ["autre-doc:p1:1"], candidate_revision=REVISION)
    assert inconnu.blocs == []


def test_le_plan_secrit_tel_quel(tmp_path: Path) -> None:
    """C'est ce fichier que le second lecteur reçoit, et lui que le digest couvre."""
    plan = _plan()
    chemin = tmp_path / "plan.json"
    ecrire_plan(plan, chemin)
    relu = PlanRelecture.model_validate_json(chemin.read_bytes())
    assert relu == plan and relu.plan_digest == plan.plan_digest


# --- le contrôle du verdict rempli ----------------------------------------------------------------

def test_un_verdict_complet_et_concordant_est_accepte() -> None:
    plan = _plan()
    verdict = valider_verdict(_verdict(plan), plan, candidate_revision=REVISION)
    assert verdict.concordant is True
    assert [v.block_id for v in verdict.verdicts] == [b.block_id for b in plan.blocs]


def test_un_verdict_divergent_est_accepte_et_dit_divergent() -> None:
    """Un désaccord est un **résultat**, pas une erreur : il se publie, il ne se refuse pas."""
    plan = _plan()
    brut = _verdict(plan)
    brut["verdicts"][0]["verdict"] = "divergent"
    brut["verdicts"][0]["note"] = "le texte extrait omet une ligne de la colonne de droite"
    verdict = valider_verdict(brut, plan, candidate_revision=REVISION)
    assert verdict.concordant is False


def test_un_verdict_dune_autre_revision_est_refuse() -> None:
    """M2, appliqué à la seconde lecture : un verdict d'un autre commit ne dit rien de celui-ci."""
    plan = _plan()
    with pytest.raises(RelectureInvalide, match="candidate_revision"):
        valider_verdict(_verdict(plan, candidate_revision="8" * 40), plan,
                        candidate_revision=REVISION)


def test_un_verdict_adosse_a_un_autre_plan_est_refuse() -> None:
    plan = _plan()
    with pytest.raises(RelectureInvalide, match="plan_digest"):
        valider_verdict(_verdict(plan, plan_digest="0" * 64), plan, candidate_revision=REVISION)


def test_une_couverture_partielle_ou_dupliquee_est_refusee() -> None:
    """Le verdict couvre **exactement** le plan : ni un bloc de moins, ni un bloc deux fois.

    Un verdict partiel accepté ferait passer pour relu ce que personne n'a regardé ; un doublon
    ferait compter deux fois la même lecture.
    """
    plan = _plan()
    partiel = _verdict(plan)
    partiel["verdicts"] = partiel["verdicts"][:1]
    with pytest.raises(RelectureInvalide, match="couverture"):
        valider_verdict(partiel, plan, candidate_revision=REVISION)
    double = _verdict(plan)
    double["verdicts"] = [double["verdicts"][0], dict(double["verdicts"][0])]
    with pytest.raises(RelectureInvalide, match="couverture"):
        valider_verdict(double, plan, candidate_revision=REVISION)
    etranger = _verdict(plan)
    etranger["verdicts"][0]["block_id"] = f"{DOC}:p9:9"
    with pytest.raises(RelectureInvalide, match="couverture"):
        valider_verdict(etranger, plan, candidate_revision=REVISION)


def test_un_verdict_hors_schema_est_refuse() -> None:
    """Vocabulaire fermé : deux valeurs de verdict, un `image_sha256` bien formé, aucune clé en trop."""
    plan = _plan()
    for mutation, motif in (
        (lambda b: b.update(extra="?"), "clés racine"),
        (lambda b: b.pop("plan_digest"), "clés racine"),
        (lambda b: b["verdicts"][0].update(verdict="peut-etre"), "champ"),
        (lambda b: b["verdicts"][0].update(image_sha256="court"), "champ"),
        (lambda b: b["verdicts"][0].update(inconnu=1), "champ"),
        (lambda b: b.update(schema_version=2), "champ"),
    ):
        brut = _verdict(plan)
        mutation(brut)
        with pytest.raises(RelectureInvalide, match=motif):
            valider_verdict(brut, plan, candidate_revision=REVISION)
    with pytest.raises(RelectureInvalide, match="objet JSON"):
        valider_verdict([1, 2], plan, candidate_revision=REVISION)


def test_lempreinte_dimage_est_celle_des_octets_regardes() -> None:
    """Le second lecteur reporte l'empreinte de l'image **qu'il a vue**, pas celle d'un chemin."""
    import hashlib

    octets = b"\x89PNG\r\n\x1a\n-image-de-test"
    assert empreinte_image(octets) == hashlib.sha256(octets).hexdigest()
    assert empreinte_image(octets) != empreinte_image(octets + b"x")


def test_le_module_nappelle_ni_modele_ni_rasteriseur() -> None:
    """Boundaries : « sans aucun appel Anthropic ni rasterisation locale ».

    Contrôle statique sur la source : `evals/relecture.py` ne doit importer ni le SDK, ni PyMuPDF,
    ni la couche `api`. Dupliquer une seconde recette de rendu aurait produit des images que le
    corpus réel n'aurait jamais servies — et fait dépendre `evals` d'`api`, hors table du spine.
    """
    from server.app.config import REPO_ROOT

    source = (REPO_ROOT / "server" / "evals" / "relecture.py").read_text(encoding="utf-8")
    lignes = [l for l in source.splitlines() if l.startswith(("import ", "from "))]
    interdits = ("anthropic", "pymupdf", "fitz", "server.app.api", "httpx")
    fautes = [l for l in lignes if any(mot in l.lower() for mot in interdits)]
    assert not fautes, fautes


def test_le_plan_est_serialisable_tel_quel(tmp_path: Path) -> None:
    """Le format livré à l'orchestrateur est du JSON pur : aucun objet Python n'y fuit."""
    plan = _plan()
    json.dumps(plan.model_dump(mode="json"))  # ne lève pas
    assert plan.model_dump(mode="json")["candidate_revision"] == REVISION


# --- revue 4.5, P4 : FR47 a un point d'entrée, et le verdict a un chemin --------------------------

def _rapport_de_run() -> dict[str, Any]:
    return {
        "schema_version": 3,
        "results": [
            {"id": "s-neutre", "repetition": 1,
             "proofs": [{"doc_id": DOC, "block_id": f"{DOC}:p3:1", "kind": "garantie",
                         "quote_hash": "h", "kind_confirmed": True}],
             "expected_blocks_not_opened": [f"{DOC}:p3:2"]},
            {"id": "s-neutre", "repetition": 2,
             "proofs": [{"doc_id": DOC, "block_id": f"{DOC}:p3:1", "kind": "garantie",
                         "quote_hash": "h", "kind_confirmed": True}],
             "expected_blocks_not_opened": []},
        ],
    }


def test_les_blocs_cles_dun_run_sont_ses_preuves_et_ses_attentes_non_ouvertes() -> None:
    """Une **seule** définition, partagée par la CLI et par la publication.

    Deux notions séparées auraient produit un plan de N blocs et un `blocs_planifies` de M, et
    personne n'aurait su lequel des deux croire. Les blocs attendus non ouverts en font partie : un
    bloc que le rappel n'a pas présenté au modèle est le premier suspect d'un défaut de parsing.
    """
    from server.evals.relecture import blocs_cles_du_rapport

    assert blocs_cles_du_rapport(_rapport_de_run()) == [f"{DOC}:p3:1", f"{DOC}:p3:2"]
    assert blocs_cles_du_rapport({}) == []
    # Robuste à un rapport partiel : ce qui n'est pas une liste de preuves n'en devient pas une.
    assert blocs_cles_du_rapport({"results": [None, {"proofs": [{"block_id": 12}]}]}) == []


def test_la_cli_ecrit_un_plan_deterministe_sans_reseau(tmp_path: Path,
                                                       monkeypatch: Any) -> None:
    """`python -m server.evals.relecture` — le point d'entrée que FR47 n'avait pas.

    Sans lui, `ecrire_plan`/`valider_verdict` n'avaient aucun appelant : l'orchestrateur, à qui
    `docs/tests-live.md` assigne le verdict, ne recevait jamais de plan, et le statut publié ne
    pouvait pas quitter `planifiee`.
    """
    from server.evals import relecture as module

    rapport = tmp_path / "rapport.json"
    rapport.write_text(json.dumps(_rapport_de_run()), encoding="utf-8")
    sortie = tmp_path / "plan.json"
    # L'index est doublé par celui de ce module : la commande lit le **corpus servi**, jamais le
    # réseau. C'est le seul point qu'un test hors ligne ne peut pas fournir sur disque ici.
    monkeypatch.setattr(
        module, "plan_de_relecture",
        lambda _index, blocs, *, candidate_revision: plan_de_relecture(
            _index_de_test(), blocs, candidate_revision=candidate_revision))
    code = module._main(["--report", str(rapport), "--candidate-revision", REVISION,
                         "--data-dir", str(tmp_path / "data-absent"), "--out", str(sortie)])
    assert code == 0
    ecrit = PlanRelecture.model_validate_json(sortie.read_bytes())
    assert [b.block_id for b in ecrit.blocs] == [f"{DOC}:p3:1", f"{DOC}:p3:2"]
    assert ecrit.candidate_revision == REVISION
    # Déterministe : deux exécutions rendent le même digest.
    module._main(["--report", str(rapport), "--candidate-revision", REVISION,
                  "--data-dir", str(tmp_path / "data-absent"), "--out", str(sortie)])
    assert PlanRelecture.model_validate_json(sortie.read_bytes()).plan_digest == ecrit.plan_digest


def test_la_cli_refuse_un_rapport_illisible(tmp_path: Path) -> None:
    """Un rapport illisible est un refus dit (code 2), jamais un plan vide qu'on croirait complet."""
    from server.evals import relecture as module

    casse = tmp_path / "casse.json"
    casse.write_text("{ pas du json", encoding="utf-8")
    assert module._main(["--report", str(casse), "--candidate-revision", REVISION]) == 2
    assert module._main(["--report", str(tmp_path / "absent.json"),
                         "--candidate-revision", REVISION]) == 2


def test_le_statut_ne_dit_concordante_que_si_tout_concorde() -> None:
    """`statut_du_verdict` : jamais « concordante par défaut », jamais sur une liste vide."""
    from server.evals.relecture import statut_du_verdict

    plan = _plan()
    concordant = valider_verdict(_verdict(plan), plan, candidate_revision=REVISION)
    assert statut_du_verdict(concordant) == "concordante"
    brut = _verdict(plan)
    brut["verdicts"][1]["verdict"] = "divergent"
    assert statut_du_verdict(
        valider_verdict(brut, plan, candidate_revision=REVISION)) == "divergente"
    # Un plan vide ne peut produire aucune concordance : `concordant` est faux sur une liste vide.
    vide = plan_de_relecture(_index_de_test(), [], candidate_revision=REVISION)
    assert statut_du_verdict(valider_verdict(_verdict(vide), vide,
                                             candidate_revision=REVISION)) == "divergente"
