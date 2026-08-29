"""Story 4.5 — « Le même artefact », prouvé par la comparaison des quatre surfaces.

FR41 demande que les résultats soient publiés ; FR42 que `/` les reprenne. Le piège, dans un projet
qui écrit du Markdown **et** du JSON **et** du HTML, est de publier quatre fois la même chose de
quatre façons qui divergent au premier arrondi. L'AC 4 le ferme en exigeant que « les mêmes valeurs
des douze champs se retrouvent, à l'octet des chiffres près » dans :

1. `docs/evals/latest.md` — le rendu lisible du dépôt ;
2. le Markdown que la CI concatène dans `$GITHUB_STEP_SUMMARY` ;
3. la réponse de `GET /api/v1/evals/latest` ;
4. la vue composée par `/`.

Ces tests ne comparent donc pas quatre textes attendus : ils construisent **un** objet, le publient,
et vérifient que les quatre surfaces en portent les mêmes chiffres. La quatrième passe par le vrai
`tools/accueil/accueil.js`, alimenté par le corps que la route rend réellement — une composition
nourrie d'un corps fabriqué à la main ne prouverait rien.

Aucun réseau, aucune clé, aucun appel de modèle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.app.api.main import create_app
from server.app.config import REPO_ROOT, Settings
from server.app.domain.evals import (PublicationEvals, ReservesPubliees, SecondeLecturePubliee)
from server.app.domain.ingest import Gate
from server.evals import publication as pub_mod
from server.evals import run as runner
from server.evals.espace import EspaceNonInstalle, EspacePublie
from tests.helpers_espace import poser_espace

HARNAIS_VUE = REPO_ROOT / "tests" / "js" / "evals_vue.mjs"
REQUIS = os.environ.get("FRONT_TESTS_REQUIS", "") not in ("", "0")
REVISION = "9" * 40


# --- un rapport de run synthétique et neutre -------------------------------------------------------

def _rapport(*, complete: bool = True, decisions: list[dict[str, Any]] | None = None,
             non_executes: list[str] | None = None,
             labels: dict[str, int] | None = None,
             results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "profile": "full",
        "identity": {"run_digest": "a" * 64},
        "complete": complete,
        "stop_reason": None if complete else "incident technique pendant la seconde répétition",
        "unexecuted_cases": list(non_executes or []),
        "cases_hash": "d" * 64,
        "cases_planned": 2,
        "cases_completed": 2,
        "repeat": 3,
        "cost_eur": 0.055,
        "plancher_digest": "c" * 64,
        "metrics": {
            # **Les sept labels d'AD-14, zéros compris** (revue R1). Une table partielle n'est
            # produite par aucun run — `construire_rapport` compte sur `LABELS` — et la validation
            # canonique la refuse désormais, parce que le journal de CI l'indexe sur les sept : un
            # label manquant y levait un `KeyError` nu au lieu d'un refus dit.
            "labels": labels if labels is not None else _tous_les_labels(),
            "variants": {"outils": 3, "local": 3},
            # Volontairement « rondes » : sans formatage partagé, chaque surface les rendrait à sa
            # façon (`1` contre `1.0000`, `0.02` contre `0.0200`) et la comparaison des quatre
            # surfaces ne verrait rien — c'est le défaut relevé par la revue (P5).
            "recall": 1.0,
            "average_cost_eur": 0.02,
            "latency_p50_ms": 14243,
            "latency_p95_ms": 34370,
            "cost_p95_eur": 0.5,
            "ne_tranche_pas_rate": 0,
        },
        # Les résultats portent ce que **les deux** rendus lisent : `limites_du_rapport` n'a besoin
        # que d'`id` et `label`, `rendre_markdown` publie aussi la ligne complète du cas.
        "results": results if results is not None else [
            {"id": "s-cas-neutre", "label": "bonne_reponse", "suite": "sinistre",
             "variant": "outils", "cost_eur": 0.02, "cost_eur_original": 0.02, "latency_ms": 14243},
            {"id": "p-cas-neutre", "label": "parsing", "suite": "parsing",
             "variant": "local", "cost_eur": 0.0, "cost_eur_original": 0.0, "latency_ms": 0},
        ],
        "stability": {"n": 3, "cases": {
            "s-cas-neutre": {"suite": "sinistre", "stable": False, "comptabilise": True},
            "p-cas-neutre": {"suite": "parsing", "stable": True, "comptabilise": False},
        }},
        "decisions": decisions if decisions is not None else [{
            "metric": "stabilite_sinistre", "producer": "orchestrator", "threshold": 1.0,
            "scope": "suite:sinistre", "n": 3, "run_digest": "a" * 64, "value": 0.0,
            "status": "red", "reason": None,
        }, {
            "metric": "executions_completes", "producer": "orchestrator", "threshold": 1.0,
            "scope": "run", "n": 3, "run_digest": "a" * 64, "value": 1.0, "status": "green",
            "reason": None,
        }],
    }


def _tous_les_labels() -> dict[str, int]:
    """`rendre_markdown` publie **les sept** labels du vocabulaire fixe d'AD-14, zéros compris."""
    return {label: 0 for label in runner.LABELS} | {"bonne_reponse": 3, "parsing": 1}


def _gate(*, evals_ok: bool = False) -> Gate:
    return Gate(profile="full", source_hash="s", ingest_fingerprint="f", overlay_hash=None,
                cases_hash="d" * 64, cases=2, countersigned=False, pipeline_digest="p",
                prompts_digest="q", model_ids={}, evals_ok=evals_ok, date="2026-08-29T00:00:00Z",
                run_digest="a" * 64, plancher_digest="c" * 64, candidate_revision=REVISION,
                report_digest="b" * 64,
                decisions=[] if evals_ok else [])


def _publication(**kw: Any) -> PublicationEvals:
    defauts: dict[str, Any] = {
        "rapport": _rapport(),
        "gate": _gate(),
        "reserves": ReservesPubliees(countersigned=False, validated_by_expert=False,
                                     dictionary_validated=False),
        "relecture": SecondeLecturePubliee(statut="planifiee", blocs_planifies=2,
                                           blocs_verifies=0),
        "report_digest": "b" * 64,
        "candidate_revision": REVISION,
    }
    defauts.update(kw)
    rapport = defauts.pop("rapport")
    gate = defauts.pop("gate")
    return pub_mod.construire_publication(rapport, gate, **defauts, preuve_externe=None)


# --- les douze champs publiés ----------------------------------------------------------------------

def test_les_douze_champs_publies_viennent_du_rapport_et_du_gate() -> None:
    """AC 4 : les douze champs que les quatre surfaces comparent existent, et viennent du run."""
    pub = _publication()
    assert pub.profile == "full"
    assert pub.candidate_revision == REVISION
    assert pub.run_digest == "a" * 64 and pub.report_digest == "b" * 64
    assert pub.plancher_digest == "c" * 64 and pub.cases_hash == "d" * 64
    assert pub.evals_ok is False
    # Les sept labels d'AD-14, zéros compris : c'est ce que `construire_rapport` écrit, et depuis
    # la revue R1 c'est ce que la validation canonique exige (le journal de CI les indexe tous).
    assert pub.labels == _tous_les_labels()
    assert {label: n for label, n in pub.labels.items() if n} == {"bonne_reponse": 3, "parsing": 1}
    assert pub.variantes == {"outils": 3, "local": 3}
    assert pub.recall == 1.0
    # Les cas `parsing` restent hors comptage de stabilité, comme au plancher : la suite est locale
    # et déterministe, sa « stabilité » ne mesure rien du modèle.
    assert (pub.stabilite.n, pub.stabilite.cas_stables, pub.stabilite.cas_comptabilises) == (3, 0, 1)
    assert (pub.cout.froid_eur, pub.cout.moyen_eur, pub.cout.p95_eur) == (0.055, 0.02, 0.5)
    assert (pub.latence.p50_ms, pub.latence.p95_ms) == (14243, 34370)
    assert pub.ne_tranche_pas_rate == 0.0
    assert pub.reserves.model_dump() == {"countersigned": False, "validated_by_expert": False,
                                         "dictionary_validated": False}
    assert [d.metric for d in pub.decisions] == ["stabilite_sinistre", "executions_completes"]
    assert pub.seconde_lecture.statut == "planifiee"


def test_les_limites_sont_derivees_du_run_et_jamais_redigees() -> None:
    """Boundaries : « les limites publiées sont **dérivées mécaniquement** ; aucune prose fabriquée ».

    Cinq sources, et chacune doit apparaître **parce qu'un fait du run l'a produite** : une décision
    rouge chiffrée, une exécution manquante, un écart de parsing, une réserve à faux, un run
    incomplet. Une limite qu'aucun chiffre ne produit serait une limite qu'aucun chiffre ne peut
    démentir.
    """
    pub = _publication(rapport=_rapport(complete=False, non_executes=["s-cas-neutre#r3"]))
    limites = "\n".join(pub.limites)
    # 1. la décision rouge, avec sa valeur, son plancher, son n et son scope.
    assert ("décision rouge stabilite_sinistre : 0.0000 < plancher 1.0000 (n=3, "
            "scope suite:sinistre, producteur orchestrator)") in pub.limites
    # La décision **verte** n'apporte aucune limite.
    assert "executions_completes" not in limites
    # 2. l'état incomplet, avec la raison d'arrêt telle que le runner l'a écrite.
    assert any(l.startswith("run incomplet : incident technique") for l in pub.limites)
    # 3. les exécutions manquantes, nommées.
    assert any("1 exécution(s) planifiée(s) non exécutée(s)" in l and "s-cas-neutre#r3" in l
               for l in pub.limites)
    # 4. l'écart de parsing, dérivé des labels du run.
    assert any("écart de parsing" in l and "p-cas-neutre" in l for l in pub.limites)
    # 5. les trois réserves.
    assert any("contresignature humaine" in l for l in pub.limites)
    assert any("expert assurance" in l for l in pub.limites)
    assert any("dictionnaire des variantes non validé" in l for l in pub.limites)
    # Un run complet, vert, tout signé : les limites correspondantes disparaissent.
    propre = _publication(
        rapport=_rapport(decisions=[],
                         labels=_tous_les_labels() | {"bonne_reponse": 4, "parsing": 0},
                         results=[{"id": "s-cas-neutre", "label": "bonne_reponse",
                                   "suite": "sinistre", "variant": "outils", "cost_eur": 0.02,
                                   "cost_eur_original": 0.02, "latency_ms": 14243}]),
        gate=_gate(evals_ok=True),
        reserves=ReservesPubliees(countersigned=True, validated_by_expert=True,
                                  dictionary_validated=True),
        relecture=SecondeLecturePubliee(statut="concordante", blocs_planifies=2,
                                        blocs_verifies=2))
    assert propre.limites == []


def test_un_resultat_rouge_est_publie_comme_les_autres(tmp_path: Path) -> None:
    """FR41 : la publication est **inconditionnelle** — publier ne promeut rien (AD-8)."""
    pub = _publication()
    assert pub.evals_ok is False
    json_path, md_path = _ecrire(pub, tmp_path)
    assert json_path.is_file() and md_path.is_file()
    lu = PublicationEvals.model_validate_json(json_path.read_bytes())
    assert lu == pub
    rendu = md_path.read_text(encoding="utf-8")
    assert "Gate **rouge**" in rendu
    assert "Publié, jamais promu" in rendu


# --- les quatre surfaces portent les mêmes chiffres ------------------------------------------------

# Les valeurs de la fixture **ne tombent pas** sur quatre décimales : c'est ce qui rend cette
# comparaison capable de voir une divergence de formatage (revue P5). Avec `0.6667` partout, les
# quatre surfaces s'accordaient par accident.
CHIFFRES = {
    "recall": "1.0000",
    "cout_froid": "0.0550",
    "cout_moyen": "0.0200",
    "cout_p95": "0.5000",
    "latence_p50": "14243",
    "latence_p95": "34370",
    "ne_tranche_pas": "0.0000",
    "run_digest": "a" * 64,
    "cases_hash": "d" * 64,
}


def test_les_quatre_surfaces_portent_les_memes_chiffres(tmp_path: Path) -> None:
    """AC 4, mot pour mot : « à l'octet des chiffres près », sur les quatre surfaces.

    Le Markdown de `docs/evals/latest.md` et celui que la CI concatène sont **la même chaîne** —
    c'est ce que garantit `rendre_publication_markdown`, appelé une fois et écrit deux fois. Les
    deux autres surfaces sont vérifiées par égalité de valeurs, pas de mise en forme : un JSON et
    une page ne s'écrivent pas comme une table Markdown.
    """
    pub = _publication()
    json_path, md_path = _ecrire(pub, tmp_path)

    # Surface 1 : `docs/evals/latest.md`.
    markdown = md_path.read_text(encoding="utf-8")
    # Surface 2 : le rendu appendu au rapport de CI — **identique**, pas seulement équivalent.
    rendu_ci = pub_mod.rendre_publication_markdown(
        pub, valeur=runner._markdown_value, code=runner._markdown_code)
    assert markdown == rendu_ci

    # Surface 3 : la réponse HTTP.
    corps = _servir(tmp_path)
    assert corps["publie"] is True
    p = corps["publication"]

    # Surface 4 : la composition de `/`, par le vrai `accueil.js`.
    vue = _composer(corps)
    textes = "\n".join(vue["textes"])

    for nom, chiffre in CHIFFRES.items():
        assert chiffre in markdown, f"{nom} absent du Markdown"
        assert chiffre in textes, f"{nom} absent de la composition de `/`"
    assert f"{p['recall']:.4f}" == CHIFFRES["recall"]
    assert f"{p['cout']['froid_eur']:.4f}" == CHIFFRES["cout_froid"]
    assert f"{p['cout']['moyen_eur']:.4f}" == CHIFFRES["cout_moyen"]
    assert f"{p['cout']['p95_eur']:.4f}" == CHIFFRES["cout_p95"]
    assert str(p["latence"]["p50_ms"]) == CHIFFRES["latence_p50"]
    assert str(p["latence"]["p95_ms"]) == CHIFFRES["latence_p95"]
    assert f"{p['ne_tranche_pas_rate']:.4f}" == CHIFFRES["ne_tranche_pas"]
    assert p["run_digest"] == CHIFFRES["run_digest"] and p["cases_hash"] == CHIFFRES["cases_hash"]

    # La stabilité « N/N » se lit identiquement des quatre côtés.
    assert "0/1 (N=3)" in markdown
    assert "0/1 cas stables sur N=3 répétitions" in textes
    assert (p["stabilite"]["cas_stables"], p["stabilite"]["cas_comptabilises"],
            p["stabilite"]["n"]) == (0, 1, 3)

    # Les trois réserves, sur les quatre surfaces.
    assert "## Réserves" in markdown and "<code>False</code>" in markdown
    assert "réserves — contresignature humaine : non" in textes
    assert p["reserves"] == {"countersigned": False, "validated_by_expert": False,
                             "dictionary_validated": False}

    # Les limites, mot pour mot : la page les reprend, elle ne les compose pas.
    for limite in pub.limites:
        assert limite in textes


def test_aucun_run_publie_est_un_etat_type_jamais_un_5xx(tmp_path: Path) -> None:
    """I/O matrix : artefact absent ou illisible ⇒ `publie: false`, **jamais** 5xx, aucun chiffre.

    Les trois causes sont distinguées — absent, illisible, hors schéma — parce qu'elles n'ont pas
    le même correctif, et qu'une seule d'entre elles est un état normal.
    """
    vide = tmp_path / "vide"
    vide.mkdir()
    (vide / "manifest.json").write_text("{}", encoding="utf-8")
    assert _servir(vide) == {"publie": False, "raison": "absent", "publication": None}

    # P14 : `illisible` et `hors_schema` sont **réellement** distingués, par un `json.loads` avant
    # la validation. `model_validate_json` les confondait, si bien qu'un JSON cassé — la cause que
    # le libellé nomme — ressortait `hors_schema`, et qu'`illisible` n'était atteignable que par une
    # erreur d'entrée-sortie. Un état publié qui ne peut pas décrire sa propre cause ne vaut guère
    # mieux qu'un silence.
    illisible = tmp_path / "illisible"
    illisible.mkdir()
    (illisible / "manifest.json").write_text("{}", encoding="utf-8")
    (illisible / "evals-latest.json").write_text("{ pas du json", encoding="utf-8")
    assert _servir(illisible)["raison"] == "illisible"

    hors_schema = tmp_path / "hors-schema"
    hors_schema.mkdir()
    (hors_schema / "manifest.json").write_text("{}", encoding="utf-8")
    (hors_schema / "evals-latest.json").write_text(
        json.dumps({"profile": "full"}), encoding="utf-8")
    corps = _servir(hors_schema)
    assert corps == {"publie": False, "raison": "hors_schema", "publication": None}
    # Et `/` le rend comme une **absence**, sans inventer un chiffre.
    vue = _composer(corps)
    assert any("aucun run publié" in t for t in vue["textes"])
    assert not any(re.search(r"\d", t) for t in vue["textes"] if "rappel" in t)


def test_la_route_vit_sous_api_v1_et_na_pas_dalias_racine(tmp_path: Path) -> None:
    """AD-11 : toute route neuve vit sous `/api/v1` ; rien d'ancien n'attend `/evals` à la racine."""
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    with TestClient(create_app(_reglages(), data_dir=tmp_path)) as client:
        assert client.get("/api/v1/evals/latest").status_code == 200
        assert client.get("/evals/latest").status_code == 404


def test_lartefact_servi_vit_sous_un_chemin_que_limage_copie() -> None:
    """Design Note : `Dockerfile` copie `server data web tools` — **jamais** `docs/`.

    Un `docs/evals/latest.json` serait absent de l'image, et la route rendrait `publie: false` en
    production, exactement là où FR41 la demande. Le contrôle est statique : il relit le Dockerfile.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    copies = {ligne.split()[1] for ligne in dockerfile.splitlines()
              if ligne.startswith("COPY ") and len(ligne.split()) >= 3
              and not ligne.startswith("COPY --")}
    assert "data" in copies, "l'image doit copier `data/`, où vit l'artefact servi"
    assert "docs" not in copies, (
        "`docs/` n'est pas dans l'image : l'artefact **servi** ne peut pas y vivre")
    assert Settings(_env_file=None).evals_publication_file == "evals-latest.json"
    assert pub_mod.PUBLICATION_JSON == "evals-latest.json"
    assert pub_mod.DOCS_LATEST == ("docs", "evals", "latest.md")


def test_la_reserve_non_experte_ouvre_le_rendu_avant_tout_chiffre(tmp_path: Path) -> None:
    """AD-14 : la première chose qu'un lecteur voit est ce qui n'est pas validé."""
    pub = _publication()
    _, md_path = _ecrire(pub, tmp_path)
    lignes = md_path.read_text(encoding="utf-8").splitlines()
    tete = "\n".join(lignes[:6]).casefold()
    assert "avertissement non expert" in tete and "expert assurance" in tete
    # Aucun chiffre du run avant la réserve.
    assert not any(c.isdigit() for c in lignes[0])


# --- outillage local ------------------------------------------------------------------------------

def _reglages() -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", env="dev")


def _ecrire(pub: PublicationEvals, racine: Path, *,
            nom: str = pub_mod.PUBLICATION_JSON) -> tuple[Path, Path]:
    """Publie par **l'écrivain de production** : préparer, puis basculer (revue R6).

    L'ancien `ecrire_publication` était une seconde plomberie — trois écritures indépendantes, sans
    rollback — que rien n'appelait hors de ces tests. La supprimer valait mieux que la maintenir en
    parallèle, et les tests y gagnent : ils exercent désormais le chemin que le gate emprunte
    réellement, garantie tout-ou-rien comprise.
    """
    data = racine / "data"
    data.mkdir(parents=True, exist_ok=True)
    lot = pub_mod.preparer_publication(
        pub, data_dir=data, repo_root=racine, nom=nom,
        valeur=runner._markdown_value, code=runner._markdown_code)
    poser_espace(racine, data_dir=data, cibles=[Path("data") / nom]).basculer(lot)
    return data / nom, racine.joinpath(*pub_mod.DOCS_LATEST)


def _servir(data_dir: Path) -> dict[str, Any]:
    """Le corps que `GET /api/v1/evals/latest` rend **réellement**, pour ce `data/`."""
    dossier = data_dir / "data" if (data_dir / "data").is_dir() else data_dir
    if not (dossier / "manifest.json").is_file():
        (dossier / "manifest.json").write_text("{}", encoding="utf-8")
    with TestClient(create_app(_reglages(), data_dir=dossier)) as client:
        reponse = client.get("/api/v1/evals/latest")
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


def _composer(corps: dict[str, Any]) -> dict[str, Any]:
    """La vue que `tools/accueil/accueil.js` compose pour ce corps — le vrai fichier, pas un double."""
    node = shutil.which("node")
    if node is None:
        motif = "node absent : la composition de `/` ne peut pas être vérifiée"
        if REQUIS:
            pytest.fail("FRONT_TESTS_REQUIS=1 mais " + motif)
        pytest.skip(motif)
    fini = subprocess.run([node, str(HARNAIS_VUE)], input=json.dumps(corps), capture_output=True,
                          text=True, timeout=120, cwd=str(REPO_ROOT), check=False)
    assert fini.returncode == 0, fini.stderr
    charge = json.loads(fini.stdout)
    assert charge.get("ok") is True, charge.get("erreur")
    assert charge["cas"]["lisible"] is True, "le corps servi n'est pas lisible par `/`"
    return charge["cas"]


# --- revue 4.5 : les correctifs, épinglés ---------------------------------------------------------

def test_une_decision_rouge_qui_porte_sa_raison_ne_publie_pas_une_inegalite_fausse() -> None:
    """Revue P11 : `1.0000 < plancher 1.0000` est une inégalité fausse, et un mauvais diagnostic.

    Une décision « producteur non probant » ou « sous-échantillonné » a une valeur qui **tient** le
    plancher : c'est sa raison qui la rend rouge. Publier la comparaison faisait chercher un défaut
    de mesure là où il n'y en a pas.
    """
    rouge_par_raison = _rapport(decisions=[{
        "metric": "cases_ok_rate", "producer": "builder", "threshold": 1.0, "scope": "run",
        "n": 3, "run_digest": "a" * 64, "value": 1.0, "status": "red",
        "reason": "producteur non probant 'builder' ; attendu 'orchestrator'",
    }])
    limites = _publication(rapport=rouge_par_raison).limites
    assert any("producteur non probant" in limite for limite in limites)
    assert not any("1.0000 < plancher 1.0000" in limite for limite in limites)
    # La valeur et le plancher restent publiés — mais comme un constat, pas comme une inégalité.
    assert any("valeur 1.0000, plancher 1.0000" in limite for limite in limites)
    # Une décision rouge **sans** raison garde l'inégalité, qui est alors vraie et explicative.
    rouge_par_valeur = _rapport(decisions=[{
        "metric": "cases_ok_rate", "producer": "orchestrator", "threshold": 1.0, "scope": "run",
        "n": 3, "run_digest": "a" * 64, "value": 0.5, "status": "red", "reason": None,
    }])
    assert any("0.5000 < plancher 1.0000" in limite
               for limite in _publication(rapport=rouge_par_valeur).limites)


def test_le_rendu_de_ci_est_celui_de_la_publication(tmp_path: Path) -> None:
    """Correctif P6 du tour précédent : **un seul renderer**, y compris pour `$GITHUB_STEP_SUMMARY`.

    La CI lance un diagnostic `full` **sans gate**, donc sans publication, puis concatène
    `results.md` — qui était produit par un second renderer. Deux renderers, deux artefacts, et les
    tests ne comparaient que deux rendus synthétiques.

    Désormais le Markdown que le runner écrit **contient** le rendu de publication de son propre
    run, construit par la fonction autoritaire. Le journal par cas reste à côté : c'est le journal du
    run, pas l'artefact publié.
    """
    rapport = _rapport(labels=_tous_les_labels())
    rapport["reserves"] = {"countersigned": False, "validated_by_expert": False,
                           "dictionary_validated": False}
    rendu = runner.rendre_markdown(rapport, preuve_externe=None)
    # Le rendu de publication est **littéralement** celui de la fonction autoritaire.
    attendu = pub_mod.rendre_publication_markdown(
        pub_mod.construire_publication(rapport, preuve_externe=None),
        valeur=runner._markdown_value, code=runner._markdown_code)
    assert attendu in rendu
    # Et il porte ce que l'Intent reprochait au résumé de CI de taire.
    assert "stabilité" in attendu and "0/1 (N=3)" in attendu
    assert pub_mod.nombre(0.055) in attendu  # coût froid
    assert "34370" in attendu               # latence p95
    assert "a" * 64 in attendu              # run_digest
    assert "## Réserves" in attendu and "## Limites" in attendu
    # Le journal du run reste présent à côté du rendu publié.
    assert "| Cas | Suite | Variante | Label |" in rendu


def test_un_diagnostic_sans_gate_nabuse_daucun_champ_du_gate() -> None:
    """Correctif P6 : « un diagnostic n'a ni `candidate_revision`, ni `plancher_digest`, ni `evals_ok` ».

    Ces champs sont **absents**, jamais fabriqués : publier `evals_ok: false` pour un run qui n'a
    rien jugé serait aussi faux que publier `true`.
    """
    # Un diagnostic n'a **ni** protocole **ni** décisions : `construire_rapport` écrit les deux
    # ensemble ou aucun des deux. Les séparer produirait un état qu'aucun run ne peut atteindre —
    # et que la validation canonique refuse désormais, à raison (cycle de récupération, B5).
    rapport = _rapport(labels=_tous_les_labels(), decisions=[])
    rapport.pop("plancher_digest")
    rapport["identity"] = {"run_digest": "a" * 64}
    pub = pub_mod.construire_publication(rapport, preuve_externe=None)
    assert pub.evals_ok is None
    assert pub.candidate_revision is None
    assert pub.plancher_digest is None
    assert pub.report_digest is None
    # Les réserves d'un rapport qui ne les établit pas ne sont pas inventées non plus.
    assert pub.reserves is None
    rendu = pub_mod.rendre_publication_markdown(pub, valeur=runner._markdown_value,
                                                code=runner._markdown_code)
    assert "diagnostic (aucun gate)" in rendu
    assert "il n'établit ni contresignature" in rendu


def test_le_fichier_concatene_par_la_ci_est_celui_que_le_runner_ecrit() -> None:
    """Correctif P6 : le test doit vérifier **la source réellement envoyée** à `$GITHUB_STEP_SUMMARY`.

    Un test qui n'exercerait que le renderer Python laisserait passer un workflow qui concatène un
    autre fichier que celui que le runner écrit — c'est-à-dire exactement le défaut d'origine, sous
    une autre forme.
    """
    import re as _re

    import yaml

    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    etapes = workflow["jobs"]["verifier"]["steps"]
    evals = next(e for e in etapes if str(e.get("name", "")).startswith("Questions-témoins"))
    ecrit = evals["env"]["EVALS_OUTPUT_MARKDOWN"]
    concatene = _re.search(r"cat (\S+) >> \"\$GITHUB_STEP_SUMMARY\"", evals["run"])
    assert concatene is not None, "le workflow doit concaténer un fichier dans le résumé"
    assert concatene.group(1) == ecrit, (
        f"la CI concatène {concatene.group(1)} mais le runner écrit {ecrit}")
    # Et c'est bien ce chemin que le runner reçoit comme `--output-markdown`.
    from tests.test_evals_live import arguments_evals

    import os as _os
    _os.environ["EVALS_OUTPUT_MARKDOWN"] = ecrit
    try:
        args, _json, markdown = arguments_evals(0.5, Path("/tmp/inexistant"))
    finally:
        _os.environ.pop("EVALS_OUTPUT_MARKDOWN", None)
    assert str(markdown) == ecrit
    assert args[args.index("--output-markdown") + 1] == ecrit


def test_le_latest_precedent_est_archive_avant_detre_remplace(tmp_path: Path) -> None:
    """Revue P7 : « latest » veut dire « le dernier », pas « le seul ».

    Le premier gate `full` écrasait sans retour le registre manuel de la campagne 4.2d — que la
    story 4.4 référence, et qui contient des mesures live que personne ne peut reproduire sans
    repayer. Un journal de campagnes qui perd les précédentes ne prouve plus rien sur la durée.
    """
    racine = tmp_path
    ancien = racine.joinpath(*pub_mod.DOCS_LATEST)
    ancien.parent.mkdir(parents=True)
    ancien.write_text("# Campagne précédente\n\nDes mesures live irremplaçables.\n",
                      encoding="utf-8")
    avant = ancien.read_text(encoding="utf-8")
    _ecrire(_publication(), racine)
    archives = sorted(racine.joinpath(*pub_mod.DOCS_ARCHIVES).glob("*.md"))
    assert len(archives) == 1, "le rendu précédent doit être archivé"
    assert archives[0].read_text(encoding="utf-8") == avant
    assert ancien.read_text(encoding="utf-8") != avant  # remplacé par le run
    # Deux publications successives n'accumulent pas de copies du même contenu.
    contenu_archive = archives[0].read_text(encoding="utf-8")
    _ecrire(_publication(), racine)
    assert len(sorted(racine.joinpath(*pub_mod.DOCS_ARCHIVES).glob("*.md"))) == 2
    assert archives[0].read_text(encoding="utf-8") == contenu_archive
    # Rien à archiver quand il n'y a rien : aucune archive vide n'est créée.
    vierge = tmp_path / "vierge"
    vierge.mkdir()
    _ecrire(_publication(), vierge)
    assert not sorted(vierge.joinpath(*pub_mod.DOCS_ARCHIVES).glob("*.md"))


def test_lecrivain_et_le_lecteur_lisent_le_meme_reglage(tmp_path: Path) -> None:
    """Revue P12 : une seule autorité, **jusqu'au réglage** — pas seulement jusqu'au défaut.

    Un écrivain figé sur la constante et un lecteur sur `Settings.evals_publication_file` auraient
    pu diverger dès qu'un environnement pose la variable : la route serait restée `publie: false`
    pour toujours, sans que rien ne le dise.
    """
    data = tmp_path / "data"
    data.mkdir()
    (data / "manifest.json").write_text("{}", encoding="utf-8")
    _ecrire(_publication(), tmp_path, nom="autre-nom.json")
    assert (data / "autre-nom.json").is_file()
    reglages = Settings(_env_file=None, anthropic_api_key="", env="dev",
                        evals_publication_file="autre-nom.json")
    with TestClient(create_app(reglages, data_dir=data)) as client:
        assert client.get("/api/v1/evals/latest").json()["publie"] is True
    # Et sous le nom par défaut, ce même `data/` n'a rien à publier : les deux se suivent.
    with TestClient(create_app(_reglages(), data_dir=data)) as client:
        assert client.get("/api/v1/evals/latest").json() == {
            "publie": False, "raison": "absent", "publication": None}


# --- B5 : une structure obligatoire absente n'est pas un résultat honnêtement vide ----------------

# `repeat` n'est pas dans cette liste, mais il est bien exigé **inconditionnellement** depuis le
# tour correctif 1/3 : il est le N d'un run sans agrégat, et le dénominateur qu'un agrégat présent
# doit retrouver (`stability.n == repeat`). C'est `test_une_liste_vide_reste_un_resultat_honnete` et
# `test_une_stabilite_mesuree_sur_un_autre_n_que_le_repeat_ferme` qui le couvrent.
@pytest.mark.parametrize("cle", ["metrics", "decisions", "identity", "results",
                                 "unexecuted_cases", "complete"])
def test_une_structure_obligatoire_absente_ferme_au_lieu_de_publier_des_zeros(cle: str) -> None:
    """B5, chemins frères : aucun chiffre fabriqué à sa propre surface.

    Les lectures `rapport.get(x) or defaut` faisaient dire « il n'y en avait pas » à « la clé n'est
    pas là », et publiaient alors des chiffres que personne n'avait mesurés : un `metrics` absent
    devenait `recall=0.0` et des latences à zéro **présentées comme des mesures** ; un `decisions`
    absent devenait « aucune décision rouge », donc un rapport sans limite rouge ; un `stability`
    absent devenait « 0/0 (N=1) ».

    C'est l'invariant « aucun chiffre inventé » de la story, violé là où elle le publie.
    """
    rapport = _rapport(labels=_tous_les_labels())
    rapport["reserves"] = {"countersigned": False, "validated_by_expert": False,
                           "dictionary_validated": False}
    rapport["repeat"] = 3
    assert pub_mod.construire_publication(rapport, preuve_externe=None) is not None  # le nominal se publie
    ampute = {c: v for c, v in rapport.items() if c != cle}
    with pytest.raises(pub_mod.RapportInexploitable, match=cle):
        pub_mod.construire_publication(ampute, preuve_externe=None)


@pytest.mark.parametrize("champ", ["recall", "average_cost_eur", "latency_p50_ms",
                                   "latency_p95_ms", "cost_p95_eur", "ne_tranche_pas_rate"])
def test_une_mesure_absente_nest_jamais_publiee_a_zero(champ: str) -> None:
    """« Publier une absence de mesure à zéro » est précisément ce que la story interdit."""
    rapport = _rapport(labels=_tous_les_labels())
    rapport["repeat"] = 3
    rapport["metrics"] = {c: v for c, v in rapport["metrics"].items() if c != champ}
    with pytest.raises(pub_mod.RapportInexploitable, match=champ):
        pub_mod.construire_publication(rapport, preuve_externe=None)


def test_une_liste_vide_reste_un_resultat_honnete() -> None:
    """La distinction est le point : clé présente et liste vide **se publie**, clé absente ferme."""
    rapport = _rapport(labels=_tous_les_labels(), decisions=[], results=[])
    rapport["repeat"] = 3
    rapport["unexecuted_cases"] = []
    publication = pub_mod.construire_publication(rapport, preuve_externe=None)
    assert publication.decisions == [] and publication.limites == []
    # Et la stabilité : `stability` absent est **légitime** (écrit seulement sous `repeat > 1`),
    # mais le `repeat` du rapport est exigé dans tous les cas, plutôt que fabriqué à 1.
    sans_stabilite = {c: v for c, v in rapport.items() if c != "stability"}
    assert pub_mod.stabilite_du_rapport(sans_stabilite, preuve_externe=None).n == 3
    with pytest.raises(pub_mod.RapportInexploitable, match="repeat"):
        pub_mod.stabilite_du_rapport({c: v for c, v in sans_stabilite.items() if c != "repeat"}, preuve_externe=None)
    # Un `stability` mal typé n'est pas un `stability` vide.
    with pytest.raises(pub_mod.RapportInexploitable, match="stability"):
        pub_mod.stabilite_du_rapport({**rapport, "stability": []}, preuve_externe=None)


def test_un_results_mal_type_est_un_refus_pas_une_erreur_nue() -> None:
    """`r["id"]` sur un élément non-`dict` levait une erreur non typée : un refus dit la remplace."""
    rapport = _rapport(labels=_tous_les_labels())
    rapport["repeat"] = 3
    rapport["results"] = ["pas-un-objet"]
    with pytest.raises(pub_mod.RapportInexploitable, match="results"):
        pub_mod.construire_publication(rapport, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match="results"):
        pub_mod.construire_publication({**rapport, "results": {"id": "x"}}, preuve_externe=None)


# --- B5, cycle de récupération : la valeur d'une mesure, pas seulement sa présence -----------------
#
# Le tour précédent avait fermé la **présence** des clés et laissé leur **valeur** libre. Le recheck
# a reproduit ce qui restait : `metrics.recall = None` et `metrics.latency_p50_ms = None` étaient
# acceptés puis publiés à zéro, `labels`/`variants` à `None` devenaient des tables vides,
# `stability` présent sans `cases` publiait `0/0`, et des décisions non vides sans `plancher_digest`
# racine passaient. Une structure mal formée redevenait ainsi un « résultat honnêtement vide » ou un
# chiffre fabriqué, sur les quatre surfaces.
#
# Les contre-exemples ci-dessous sont rouges sur `b9db3c1` et verts ici. Ils portent tous sur la
# **validation canonique unique**, celle que tous les chemins des quatre surfaces appellent.

def _rapport_publiable() -> dict[str, Any]:
    """Le rapport nominal, complet et publiable — le témoin que chaque mutation doit faire rougir."""
    rapport = _rapport(labels=_tous_les_labels())
    rapport["repeat"] = 3
    rapport["reserves"] = {"countersigned": False, "validated_by_expert": False,
                           "dictionary_validated": False}
    return rapport


@pytest.mark.parametrize("champ", ["recall", "average_cost_eur", "cost_p95_eur",
                                    "ne_tranche_pas_rate", "latency_p50_ms", "latency_p95_ms"])
def test_une_mesure_nulle_ferme_au_lieu_detre_publiee_a_zero(champ: str) -> None:
    """B5 : `None` n'est pas `0`. Le contre-exemple exact du recheck, mesure par mesure."""
    nominal = _rapport_publiable()
    assert pub_mod.construire_publication(nominal, preuve_externe=None) is not None
    for valeur in (None, True, "0"):
        casse = {**nominal, "metrics": {**nominal["metrics"], champ: valeur}}
        with pytest.raises(pub_mod.RapportInexploitable, match=champ):
            pub_mod.construire_publication(casse, preuve_externe=None)
    # Un non-fini n'est pas davantage une mesure.
    casse = {**nominal, "metrics": {**nominal["metrics"], champ: float("nan")}}
    with pytest.raises(pub_mod.RapportInexploitable, match=champ):
        pub_mod.construire_publication(casse, preuve_externe=None)


def test_un_cost_eur_nul_ou_absent_ferme_au_lieu_de_publier_un_run_gratuit() -> None:
    """B5 : le coût froid est **la** mesure que la publication présente comme ce qu'une campagne paie."""
    nominal = _rapport_publiable()
    for valeur in (None, True, "0.055"):
        with pytest.raises(pub_mod.RapportInexploitable, match="cost_eur"):
            pub_mod.construire_publication({**nominal, "cost_eur": valeur}, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match="cost_eur"):
        pub_mod.construire_publication({c: v for c, v in nominal.items() if c != "cost_eur"}, preuve_externe=None)


@pytest.mark.parametrize("table", ["labels", "variants"])
def test_une_table_de_comptage_nulle_ne_devient_pas_une_table_vide(table: str) -> None:
    """B5 : `dict(metrics.get(x) or {})` faisait dire « rien n'a été observé » à « rien n'a été écrit »."""
    nominal = _rapport_publiable()
    for valeur in (None, [], "aucun"):
        casse = {**nominal, "metrics": {**nominal["metrics"], table: valeur}}
        with pytest.raises(pub_mod.RapportInexploitable, match=table):
            pub_mod.construire_publication(casse, preuve_externe=None)
    ampute = {**nominal,
              "metrics": {c: v for c, v in nominal["metrics"].items() if c != table}}
    with pytest.raises(pub_mod.RapportInexploitable, match=table):
        pub_mod.construire_publication(ampute, preuve_externe=None)


def test_une_stabilite_presente_sans_cases_ne_publie_pas_zero_sur_zero() -> None:
    """B5 : « `stability` présent sans `cases` » publiait `0/0` — un dénominateur inventé."""
    nominal = _rapport_publiable()
    sans_cases = {**nominal, "stability": {"n": 3}}
    with pytest.raises(pub_mod.RapportInexploitable, match="cases"):
        pub_mod.construire_publication(sans_cases, preuve_externe=None)
    # `cases` mal typé, et `n` absent ou mal typé, ferment de la même façon.
    with pytest.raises(pub_mod.RapportInexploitable, match="cases"):
        pub_mod.construire_publication({**nominal, "stability": {"n": 3, "cases": []}}, preuve_externe=None)
    un_cas = {"c": {"stable": True, "comptabilise": True}}
    with pytest.raises(pub_mod.RapportInexploitable, match="stability.n"):
        pub_mod.construire_publication({**nominal, "stability": {"cases": un_cas}}, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match="stability.n"):
        pub_mod.construire_publication({**nominal, "stability": {"n": 0, "cases": un_cas}}, preuve_externe=None)
    # **`cases` vide** ferme aussi (revue P4) : l'absence avait été fermée, pas le vide, et
    # `{"n": 3, "cases": {}}` publiait le `0/0` fabriqué que tout ce module refuse.
    with pytest.raises(pub_mod.RapportInexploitable, match="est vide"):
        pub_mod.construire_publication({**nominal, "stability": {"n": 3, "cases": {}}}, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match="est vide"):
        pub_mod.stabilite_du_rapport({**nominal, "stability": {"n": 3, "cases": {}}}, preuve_externe=None)
    # Ce qui manque **légitimement** reste distinct : `stability` absent sous un run sans répétition.
    sans_stabilite = {c: v for c, v in nominal.items() if c != "stability"}
    assert pub_mod.stabilite_du_rapport(sans_stabilite, preuve_externe=None).n == 3


def test_des_decisions_non_vides_sans_plancher_digest_sont_refusees() -> None:
    """B5 : une décision qui ne nomme pas son protocole ne dit pas contre quel seuil elle est prise."""
    nominal = _rapport_publiable()
    orphelines = {c: v for c, v in nominal.items() if c != "plancher_digest"}
    assert orphelines["decisions"], "le contre-exemple exige des décisions non vides"
    with pytest.raises(pub_mod.RapportInexploitable, match="plancher_digest"):
        pub_mod.construire_publication(orphelines, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match="plancher_digest"):
        pub_mod.construire_publication({**orphelines, "plancher_digest": None}, preuve_externe=None)
    # Décisions vides **et** plancher absent : c'est un diagnostic, et il se publie.
    assert pub_mod.construire_publication({**orphelines, "decisions": []}, preuve_externe=None) is not None


def test_la_validation_canonique_est_la_meme_sur_les_quatre_surfaces(tmp_path: Path) -> None:
    """B5 : « une validation canonique unique, appelée par tous les chemins ».

    Le point du finding n'est pas qu'un chemin refuse, c'est que **tous** refusent, et **avant**
    qu'aucune surface n'affiche quoi que ce soit.

    Revue R4 : la quatrième surface était vérifiée **à vide**. Les trois premiers chemins opèrent en
    mémoire et aucun ne reçoit de `data_dir`, si bien que l'assertion finale portait sur un
    répertoire où rien n'avait jamais tenté d'écrire — vraie avant comme après le correctif, donc
    incapable de rougir. C'est la « preuve qui ne peut pas échouer » que le correctif P5 du premier
    tour avait déjà eu à fermer. L'assertion porte désormais sur un chemin qui **écrit réellement** :
    on publie d'abord un artefact valide, on tente de republier le rapport cassé, et on vérifie que
    la surface servie n'a pas bougé d'un octet.
    """
    casse = {**_rapport_publiable(),
             "metrics": {**_rapport_publiable()["metrics"], "recall": None}}
    for chemin in (
            lambda: pub_mod.construire_publication(casse, preuve_externe=None),
            lambda: pub_mod.stabilite_du_rapport(casse, preuve_externe=None),
            lambda: pub_mod.limites_du_rapport(casse, [], preuve_externe=None),
            lambda: runner.rendre_markdown(casse, preuve_externe=None)):
        with pytest.raises(pub_mod.RapportInexploitable, match="recall"):
            chemin()

    # Une première publication **valide**, réellement écrite et réellement servie.
    data = tmp_path / "data"
    data.mkdir()
    (data / "manifest.json").write_text("{}", encoding="utf-8")
    _ecrire(_publication(), tmp_path)
    servi_avant = _servir(tmp_path)
    assert servi_avant["publie"] is True
    octets_avant = (data / pub_mod.PUBLICATION_JSON).read_bytes()
    md_avant = tmp_path.joinpath(*pub_mod.DOCS_LATEST).read_bytes()

    # Puis le rapport cassé, par l'écrivain de production : il refuse **avant** la première bascule.
    with pytest.raises(pub_mod.RapportInexploitable, match="recall"):
        pub_mod.preparer_publication(
            pub_mod.construire_publication(casse, preuve_externe=None), data_dir=data, repo_root=tmp_path,
            valeur=runner._markdown_value, code=runner._markdown_code)

    # La quatrième surface porte toujours le run valide : rien de la donnée cassée n'y est arrivé,
    # et aucun temporaire n'a survécu au refus.
    assert (data / pub_mod.PUBLICATION_JSON).read_bytes() == octets_avant
    assert tmp_path.joinpath(*pub_mod.DOCS_LATEST).read_bytes() == md_avant
    assert _servir(tmp_path) == servi_avant
    assert _temporaires(tmp_path) == []


def test_aucune_ligne_de_decision_nest_fabriquee_quand_il_ny_en_a_pas() -> None:
    """B5, rendu Markdown partagé : `| — | — | — | 0 | 0.0000 | 0.0000 | — |` publiait trois chiffres.

    Un `n` et deux seuils qu'aucune décision n'avait produits, dans un tableau intitulé « Décisions
    du plancher ». « Il n'y a pas de décision » se dit en toutes lettres.
    """
    rapport = _rapport_publiable()
    rapport["decisions"] = []
    rapport.pop("plancher_digest")
    rendu = pub_mod.rendre_publication_markdown(
        pub_mod.construire_publication(rapport, preuve_externe=None),
        valeur=runner._markdown_value, code=runner._markdown_code)
    assert "aucune décision de plancher" in rendu
    assert "| 0 | 0.0000 | 0.0000 |" not in rendu


# --- B7, cycle de récupération : zéro temporaire résiduel sur refus de préparation -----------------
#
# `preparer_publication` créait le temporaire de `data/evals-latest.json` **avant** de lire l'archive
# à remplacer. Quand cette lecture levait `ArchivePrecedenteIllisible`, la fonction sortait sans
# rendre `a_basculer` : l'appelant gardait `prepares = []`, `_abandonner` ne recevait rien, et le
# temporaire échappait à tout nettoyage. Les refus répétés polluaient `data/`.
#
# La fermeture porte sur la **classe** du défaut, pas sur la sonde : lecture et validation avant le
# premier temporaire, **et** rollback local couvrant tous les rangs de préparation.

def _temporaires(racine: Path) -> list[str]:
    """Tous les temporaires laissés sous `racine` — **quelle que soit la convention de nommage**.

    Revue R5 : le glob précédent était `rglob(".*.tmp")`, avec point initial obligatoire. Il
    correspondait à `_preparer_atomique` (`prefix=f".{path.name}."`) mais **pas** à `preparer_gate`
    (`prefix=manifest_path.name + "."`, sans point) — si bien que l'assertion « aucun temporaire »
    ne pouvait tout simplement pas échouer sur ce chemin frère. Une sonde liée au nommage d'un seul
    écrivain ne prouve rien du suivant : la sonde porte donc sur le **suffixe**, que
    `tempfile.mkstemp` reçoit identiquement dans les deux écrivains.
    """
    return sorted(str(p.relative_to(racine)) for p in racine.rglob("*")
                  if p.is_file() and p.name.endswith(".tmp"))


def test_une_archive_illisible_ne_laisse_aucun_temporaire(tmp_path: Path) -> None:
    """B7 : un refus de préparation ne laisse rien — désormais parce qu'il n'écrit rien du tout.

    La cause nommée par le cycle de récupération (le temporaire créé avant la lecture de l'archive)
    ne peut plus exister : `preparer_publication` ne touche plus le disque, elle rend le lot et
    c'est la bascule qui l'écrit, dans une génération inactive.
    """
    data = tmp_path / "data"
    data.mkdir()
    latest = tmp_path.joinpath(*pub_mod.DOCS_LATEST)
    latest.parent.mkdir(parents=True)
    latest.write_text("# campagne précédente\n", encoding="utf-8")
    avant = latest.read_bytes()
    latest.chmod(0o000)
    try:
        if latest.exists():
            with pytest.raises(pub_mod.ArchivePrecedenteIllisible):
                pub_mod.preparer_publication(
                    _publication(), data_dir=data, repo_root=tmp_path,
                    valeur=runner._markdown_value, code=runner._markdown_code)
    finally:
        latest.chmod(0o644)
    assert _temporaires(tmp_path) == [], "un refus de préparation ne laisse aucun temporaire"
    # Aucune cible n'a bougé non plus.
    assert latest.read_bytes() == avant
    assert not list(data.glob("*.json"))
    assert not list(tmp_path.joinpath(*pub_mod.DOCS_ARCHIVES).glob("*.md")) if \
        tmp_path.joinpath(*pub_mod.DOCS_ARCHIVES).is_dir() else True


@pytest.mark.parametrize("rang", [1, 2, 3, 4, 5])
def test_un_echec_a_nimporte_quel_rang_de_publication_ne_laisse_aucun_temporaire(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rang: int) -> None:
    """B7 : « vérifier les rangs (échec au 1ᵉʳ, au 2ᵉ, au dernier) », sur le lot de publication réel.

    Quatre surfaces sont publiées dans le cas complet — le JSON servi, l'archive du rendu précédent,
    le rendu lisible, et le Markdown que la CI concatène —, écrites dans la génération inactive puis
    publiées par l'atome. L'échec est injecté à chacun des rangs, atome compris, et l'invariant doit
    tenir à tous : aucune surface visible, aucun temporaire.
    """
    data = tmp_path / "data"
    data.mkdir()
    latest = tmp_path.joinpath(*pub_mod.DOCS_LATEST)
    latest.parent.mkdir(parents=True)
    latest.write_text("# campagne précédente\n", encoding="utf-8")
    espace = poser_espace(tmp_path, data_dir=data)
    avant = latest.read_bytes()
    lot = pub_mod.preparer_publication(
        _publication(), data_dir=data, repo_root=tmp_path,
        markdown_run="# journal du run\n", chemin_run=tmp_path / "eval-results.md",
        valeur=runner._markdown_value, code=runner._markdown_code)
    vrai = os.replace
    compte = {"n": 0}

    def replace_faillible(src: Any, dst: Any) -> Any:
        compte["n"] += 1
        if compte["n"] == rang:
            raise OSError("disque plein (injecté)")
        return vrai(src, dst)

    monkeypatch.setattr(os, "replace", replace_faillible)
    with pytest.raises(OSError, match="injecté"):
        espace.basculer(lot)
    monkeypatch.undo()
    assert _temporaires(tmp_path) == []
    # Et aucune cible n'a été modifiée : rien ne bascule tant que l'atome n'a pas eu lieu.
    assert latest.read_bytes() == avant
    assert not (data / pub_mod.PUBLICATION_JSON).exists()
    assert not (tmp_path / "eval-results.md").exists()


def test_ecrire_rapports_ne_laisse_aucun_temporaire_si_le_second_echoue(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B7, chemin frère : le couple rapport/table est un lot, publié par le même unique atome."""
    rapport = _rapport_publiable()
    json_path = tmp_path / "eval-results.json"
    md_path = tmp_path / "eval-results.md"
    espace = poser_espace(tmp_path, data_dir=tmp_path / "data")
    vrai = os.replace
    appels = {"n": 0}

    def replace_faillible(src: Any, dst: Any) -> Any:
        appels["n"] += 1
        if appels["n"] == 2:
            raise OSError("disque plein (injecté)")
        return vrai(src, dst)

    monkeypatch.setattr(os, "replace", replace_faillible)
    with pytest.raises(OSError, match="injecté"):
        runner.ecrire_rapports(rapport, json_path, md_path, preuve_externe=None, espace=espace)
    monkeypatch.undo()
    assert _temporaires(tmp_path) == []
    assert not json_path.exists() and not md_path.exists()


def test_preparer_gate_ne_laisse_aucun_temporaire_sur_une_serialisation_impossible(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B7, chemin frère : la préparation de l'entrée de manifest ne nettoyait que sur `OSError`.

    Toute autre cause d'échec postérieure à `mkstemp` — une sérialisation impossible, par exemple —
    laissait le temporaire dans `data/`.
    """
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "doc": {"status": "servi", "source_hash": "s", "ingest_fingerprint": "f",
                "document_hash": "d", "edition": "e"}}), encoding="utf-8")
    avant = manifest.read_bytes()

    def dumps_casse(*a: Any, **k: Any) -> str:
        raise TypeError("sérialisation impossible (injectée)")

    monkeypatch.setattr(runner.json, "dumps", dumps_casse)
    with pytest.raises(TypeError, match="injectée"):
        runner.preparer_gate(manifest, "doc", _gate())
    assert _temporaires(tmp_path) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.json"]
    assert manifest.read_bytes() == avant


# --- R1 : la validation canonique couvre ce que les rendus partagés indexent réellement -----------
#
# Le tour précédent avait fermé les *mesures* et laissé les clés de structure que le journal du run
# indexe : `cases_hash`, `cases_completed`, `cases_planned`, `stop_reason`, les sept labels d'AD-14,
# et les sept champs de chaque exécution. Un rapport amputé de l'une d'elles levait un `KeyError`
# nu — qui n'est pas une `ValueError` — et ressortait de `run._main` en « incident de gate »,
# code 3 : un défaut de données étiqueté panne technique, franchissant la ligne de partage des codes
# de sortie que la spec interdit de bouger.

@pytest.mark.parametrize("cle", ["cases_hash", "cases_planned", "cases_completed", "stop_reason"])
def test_une_cle_du_journal_absente_est_un_refus_dit_pas_un_keyerror(cle: str) -> None:
    """R1 : les clés que `rendre_markdown` indexe à la racine sont exigées par la validation."""
    nominal = _rapport_publiable()
    assert runner.rendre_markdown(dict(nominal), preuve_externe=None)  # le nominal se rend
    ampute = {c: v for c, v in nominal.items() if c != cle}
    with pytest.raises(pub_mod.RapportInexploitable, match=cle):
        pub_mod.valider_rapport_publiable(ampute, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match=cle):
        runner.rendre_markdown(ampute, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match=cle):
        pub_mod.construire_publication(ampute, preuve_externe=None)


def test_stop_reason_nulle_est_legitime_mais_absente_ne_lest_pas() -> None:
    """R1 : « la clé n'est pas là » n'est pas « il n'y a pas eu d'arrêt » — la nuance de tout ce cycle."""
    nominal = _rapport_publiable()
    assert pub_mod.valider_rapport_publiable({**nominal, "stop_reason": None}, preuve_externe=None) is not None
    assert pub_mod.valider_rapport_publiable({**nominal, "stop_reason": "plafond atteint"}, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match="stop_reason"):
        pub_mod.valider_rapport_publiable({c: v for c, v in nominal.items() if c != "stop_reason"}, preuve_externe=None)


def test_le_vocabulaire_de_labels_incomplet_ferme_au_lieu_de_lever_un_keyerror() -> None:
    """R1 : le journal indexe `metrics.labels[label]` sur **les sept** labels fixes d'AD-14."""
    nominal = _rapport_publiable()
    partiel = {**nominal, "metrics": {**nominal["metrics"], "labels": {"bonne_reponse": 1}}}
    with pytest.raises(pub_mod.RapportInexploitable, match="AD-14|labels"):
        runner.rendre_markdown(partiel, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match="AD-14|labels"):
        pub_mod.construire_publication(partiel, preuve_externe=None)
    # Un comptage négatif ou non entier n'est pas un comptage.
    for mauvais in (-1, 1.5, True, None):
        casse = {**nominal,
                 "metrics": {**nominal["metrics"], "labels": _tous_les_labels() | {"parsing": mauvais}}}
        with pytest.raises(pub_mod.RapportInexploitable, match="labels"):
            pub_mod.valider_rapport_publiable(casse, preuve_externe=None)


@pytest.mark.parametrize("champ", ["id", "suite", "variant", "label", "cost_eur",
                                    "cost_eur_original", "latency_ms"])
def test_une_execution_amputee_est_un_refus_dit(champ: str) -> None:
    """R1 : les sept clés que le journal indexe par exécution sont exigées, avec leur type."""
    nominal = _rapport_publiable()
    ampute = {**nominal,
              "results": [{c: v for c, v in nominal["results"][0].items() if c != champ}]}
    with pytest.raises(pub_mod.RapportInexploitable, match=champ):
        runner.rendre_markdown(ampute, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match=champ):
        pub_mod.construire_publication(ampute, preuve_externe=None)
    # Et le type est exigé, pas seulement la présence.
    mauvais = 42 if champ in ("id", "suite", "variant", "label") else "beaucoup"
    casse = {**nominal, "results": [{**nominal["results"][0], champ: mauvais}]}
    with pytest.raises(pub_mod.RapportInexploitable, match=champ):
        pub_mod.valider_rapport_publiable(casse, preuve_externe=None)


def test_un_rapport_inexploitable_hors_gate_est_un_refus_dit_pas_un_incident(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """R1, chemin `ecrire_rapports` hors gate : code 1 et cause nommée, jamais « incident », code 3.

    La ligne de partage d'AD-8 : un rapport que la validation refuse est un **défaut de données**.
    L'étiqueter incident technique enverrait chercher une panne réseau là où une clé manque, et le
    code 3 promet « manifest non modifié » pour une raison qui n'est pas la bonne.
    """
    json_path, md_path = tmp_path / "r.json", tmp_path / "r.md"
    ampute = {c: v for c, v in _rapport_publiable().items() if c != "cases_completed"}
    with pytest.raises(pub_mod.RapportInexploitable, match="cases_completed"):
        runner.ecrire_rapports(ampute, json_path, md_path, preuve_externe=None,
                               espace=poser_espace(tmp_path, data_dir=tmp_path / "data"))
    # Rien n'a été écrit, et aucun temporaire ne subsiste : le refus précède toute bascule.
    assert not json_path.exists() and not md_path.exists()
    assert _temporaires(tmp_path) == []
    # Le handler de `_main` traite bien cette exception comme un refus dit (code 1), pas comme un
    # incident (code 3) : la table des codes de sortie du module le dit désormais aussi.
    table = runner.__doc__ or ""
    assert "RapportInexploitable" in table
    assert "tombe du côté 1" in table


# --- R3 : des réserves présentes mais illisibles ne sont pas des réserves absentes ----------------

def test_des_reserves_mal_formees_ferment_au_lieu_de_publier_un_diagnostic() -> None:
    """R3 : la faute exacte que B5 ferme ailleurs, restée ouverte sur `_reserves_du_rapport`.

    Rabattre sur `None` faisait écrire « ce run est un diagnostic : il n'établit ni contresignature,
    ni validation par un expert, ni signature du dictionnaire » sur un run qui *a* établi ses
    réserves — et faisait **disparaître les trois limites de réserve** des limites publiées.
    """
    nominal = _rapport_publiable()
    # Le nominal : les réserves sont lues, et leurs trois limites sont publiées.
    pub = pub_mod.construire_publication(nominal, preuve_externe=None)
    assert pub.reserves is not None
    assert sum(1 for l in pub.limites
               if "contresignature" in l or "expert assurance" in l or "dictionnaire" in l) == 3
    # Un champ mal typé, un champ absent, un objet qui n'en est pas un : trois refus nommés.
    for reserves, motif in (
            ({"countersigned": "oui", "validated_by_expert": True, "dictionary_validated": True},
             "countersigned"),
            ({"countersigned": True, "validated_by_expert": True}, "dictionary_validated"),
            ("aucune", "reserves"),
            ([], "reserves")):
        with pytest.raises(pub_mod.RapportInexploitable, match=motif):
            pub_mod.construire_publication({**nominal, "reserves": reserves}, preuve_externe=None)
    # L'absence de la clé reste un diagnostic légitime : rien n'est inventé, rien n'est refusé.
    diagnostic = {c: v for c, v in nominal.items() if c != "reserves"}
    assert pub_mod.construire_publication(diagnostic, preuve_externe=None).reserves is None


# --- R7 : le domaine des mesures et le type des décisions, dans le refus dit ----------------------

def test_une_decision_mal_typee_est_un_refus_dit_pas_une_validationerror_nue() -> None:
    """R7 : `decisions=[42]` n'était refusé que par pydantic, sans nommer la clé du rapport."""
    nominal = _rapport_publiable()
    for decisions in ([42], ["texte"], [None]):
        with pytest.raises(pub_mod.RapportInexploitable, match="decisions"):
            pub_mod.construire_publication({**nominal, "decisions": decisions}, preuve_externe=None)


@pytest.mark.parametrize(("champ", "valeur"), [
    ("recall", -5.0), ("recall", 2.0),
    ("ne_tranche_pas_rate", 2.0), ("ne_tranche_pas_rate", -0.5),
    ("latency_p50_ms", -3), ("latency_p95_ms", -1),
    ("average_cost_eur", -0.01), ("cost_p95_eur", -1.0),
])
def test_une_mesure_hors_domaine_est_un_refus_dit(champ: str, valeur: float) -> None:
    """R7 : le domaine est refusé par la validation canonique, pas seulement par le modèle publié."""
    nominal = _rapport_publiable()
    casse = {**nominal, "metrics": {**nominal["metrics"], champ: valeur}}
    with pytest.raises(pub_mod.RapportInexploitable, match=champ):
        pub_mod.valider_rapport_publiable(casse, preuve_externe=None)


def test_un_cout_froid_negatif_est_un_refus_dit() -> None:
    """R7 : `cost_eur` est une mesure comme les autres — son domaine aussi."""
    with pytest.raises(pub_mod.RapportInexploitable, match="cost_eur"):
        pub_mod.valider_rapport_publiable({**_rapport_publiable(), "cost_eur": -0.01}, preuve_externe=None)


# --- R8 : le contrat de retour d'`archiver_latest`, fixé ------------------------------------------

def test_archiver_latest_ne_rend_un_chemin_que_lorsquil_a_ecrit(tmp_path: Path) -> None:
    """R8 : `None` veut dire « rien n'a été écrit », dans les trois cas où rien ne l'est.

    La sémantique a changé avec `_archive_a_ecrire` (le cas idempotent rendait le chemin, il rend
    `None`) sans qu'aucun test ne fixe l'un ou l'autre contrat. Celui-ci le fixe : **le retour est
    le chemin de ce qui vient d'être écrit**, et rien d'autre — un appelant peut donc s'en servir
    pour dire « archivé » sans se tromper.
    """
    latest = tmp_path.joinpath(*pub_mod.DOCS_LATEST)
    latest.parent.mkdir(parents=True)

    # 1. absent → rien à écrire.
    assert pub_mod.archiver_latest(latest, repo_root=tmp_path,
                                   ecrire=runner._ecrire_atomique) is None
    # 2. vide → rien à écrire (une archive vide ne prouve rien).
    latest.write_text("   \n", encoding="utf-8")
    assert pub_mod.archiver_latest(latest, repo_root=tmp_path,
                                   ecrire=runner._ecrire_atomique) is None
    assert not list(tmp_path.joinpath(*pub_mod.DOCS_ARCHIVES).glob("*.md")) \
        if tmp_path.joinpath(*pub_mod.DOCS_ARCHIVES).is_dir() else True
    # 3. contenu neuf → le chemin **écrit** est rendu, et il porte le contenu remplacé.
    latest.write_text("# campagne à conserver\n", encoding="utf-8")
    archive = pub_mod.archiver_latest(latest, repo_root=tmp_path, ecrire=runner._ecrire_atomique)
    assert archive is not None and archive.is_file()
    assert archive.read_text(encoding="utf-8") == "# campagne à conserver\n"
    # 4. déjà archivé à l'identique → rien n'est réécrit, donc `None`.
    assert pub_mod.archiver_latest(latest, repo_root=tmp_path,
                                   ecrire=runner._ecrire_atomique) is None
    assert len(list(tmp_path.joinpath(*pub_mod.DOCS_ARCHIVES).glob("*.md"))) == 1


# --- R11 : la surface de CI ne disparaît pas en silence de la bascule -----------------------------

def test_le_couple_markdown_run_et_chemin_run_est_indivisible(tmp_path: Path) -> None:
    """R11 : n'en fournir qu'un retirait de la bascule la surface que la CI concatène, sans le dire."""
    data = tmp_path / "data"
    data.mkdir()
    for kw in ({"markdown_run": "# journal\n"}, {"chemin_run": tmp_path / "eval-results.md"}):
        with pytest.raises(ValueError, match="vont ensemble"):
            pub_mod.preparer_publication(
                _publication(), data_dir=data, repo_root=tmp_path,
                    valeur=runner._markdown_value, code=runner._markdown_code, **kw)  # type: ignore[arg-type]
    assert _temporaires(tmp_path) == []
    # Le couple complet prépare bien quatre cibles, dont celle de la CI.
    prepares = pub_mod.preparer_publication(
        _publication(), data_dir=data, repo_root=tmp_path,
        markdown_run="# journal\n", chemin_run=tmp_path / "eval-results.md",
        valeur=runner._markdown_value, code=runner._markdown_code)
    assert (tmp_path / "eval-results.md") in [cible for cible, _contenu in prepares]
    # Rien n'a été écrit : la préparation ne touche plus le disque du tout.
    assert _temporaires(tmp_path) == [] and not (tmp_path / "eval-results.md").exists()


# --- R6 : il n'y a qu'un écrivain ------------------------------------------------------------------

def test_il_ny_a_quun_ecrivain_de_publication() -> None:
    """R6 : la seconde plomberie est supprimée, pas maintenue en parallèle.

    `ecrire_publication` n'avait aucun appelant de production et enchaînait trois écritures sans
    rollback : un échec sur la troisième laissait `data/evals-latest.json` sur le nouveau verdict et
    `docs/evals/latest.md` sur l'ancien — « une surface affirme un verdict que l'autre ne porte
    pas », le défaut que B3 et B7 ont fermé sur l'écrivain de production.
    """
    assert not hasattr(pub_mod, "ecrire_publication")
    assert "ecrire_publication" not in (pub_mod.__doc__ or "")


# --- B5, tour correctif 1/3 : le **domaine** des valeurs, pas seulement leur présence --------------
#
# Les tours précédents avaient fermé la présence d'une clé, puis son type. Le verdict Codex a montré
# la couche manquante : la **valeur**. Un rapport dont toutes les clés sont là, toutes bien typées,
# passait la validation, la construction *et* le rendu avec un `label` hors vocabulaire, des coûts
# négatifs, une latence négative, une stabilité en chaînes et une décision dont les nombres étaient
# coercés par pydantic. Le journal que la CI concatène imprimait littéralement
# `| cas-1 | vertical | v | LABEL_INCONNU | -12.5000 | -9.0000 | -7 |`.
#
# Les contre-exemples ci-dessous portent donc sur des **valeurs invalides**, pas sur des clés
# absentes : c'est ce que le tour précédent n'avait pas prouvé.

def _resultat(**surcharges: Any) -> dict[str, Any]:
    base = {"id": "cas-1", "suite": "sinistre", "variant": "outils", "label": "bonne_reponse",
            "cost_eur": 0.02, "cost_eur_original": 0.02, "latency_ms": 14243}
    base.update(surcharges)
    return base


def _rapport_fabrique() -> dict[str, Any]:
    """Le rapport exact de la sonde Codex : toutes les clés présentes, quatre valeurs impossibles."""
    rapport = _rapport(results=[_resultat(suite="vertical", label="LABEL_INCONNU",
                                          cost_eur=-12.5, cost_eur_original=-9.0,
                                          latency_ms=-7)])
    rapport["stability"] = {"n": 3, "cases": {"cas-1": {"stable": "oui", "comptabilise": "oui"}}}
    rapport["decisions"] = [{
        "metric": "m", "producer": "orchestrator", "threshold": "1.0", "scope": "run",
        "n": "3", "run_digest": "f" * 64, "value": "0.5", "status": "red", "reason": None,
    }]
    return rapport


LIGNE_DU_VERDICT = "| cas-1 | vertical | v | LABEL_INCONNU | -12.5000 | -9.0000 | -7 |"


def test_le_rapport_fabrique_du_verdict_ne_traverse_aucune_des_quatre_surfaces(
        tmp_path: Path) -> None:
    """B1/B5 du tour correctif : la sonde du reviewer, refusée avant toute surface visible.

    Le test porte sur le **harnais réel des quatre surfaces** (revue P8), pas sur trois appelables
    en mémoire : `_ecrire` publie par l'écrivain de production, `_servir` interroge la vraie route,
    `_composer` exécute le vrai `tools/accueil/accueil.js`. « Aucune surface n'affiche quoi que ce
    soit » se prouve en regardant les surfaces, pas en constatant qu'une fonction a levé.

    La ligne littérale que l'AC nomme — celle que `run.rendre_markdown` imprimait, et non
    `rendre_publication_markdown`, qui ne porte pas les coûts par exécution — est cherchée dans
    **tous** les textes rendus.
    """
    fabrique = _rapport_fabrique()
    # Le run de bout en bout, **d'abord** : `ecrire_rapports` est le chemin par lequel le journal
    # de CI atteint un fichier. Aucun octet ne doit en sortir, et l'assertion porte sur le
    # répertoire, pas sur le type de l'exception — c'est elle qui mord si un chemin de rendu cesse
    # de valider.
    sortie = tmp_path / "sortie"
    try:
        runner.ecrire_rapports(fabrique, sortie / "eval-results.json",
                               sortie / "eval-results.md", preuve_externe=None,
                               espace=poser_espace(
                                   sortie, data_dir=sortie / "data",
                                   cibles=[Path("eval-results.json"), Path("eval-results.md")]))
    except pub_mod.RapportInexploitable:
        pass  # le refus dit — mais ce n'est pas lui que cette assertion regarde
    # L'assertion porte sur les **surfaces**, pas sur le répertoire : depuis B7, la disposition de
    # publication (le bundle et son pointeur) vit sous `sortie/data/.publie`, et sa présence ne dit
    # rien de ce qui a été publié. Ce qui doit rester vide, c'est ce qu'un lecteur lit — et rien de
    # ce que la génération porte ne doit contenir le rapport fabriqué.
    assert not (sortie / "eval-results.json").exists(), "le rapport de CI a été écrit"
    assert not (sortie / "eval-results.md").exists(), "le journal de CI a été écrit"
    assert not [p for p in (sortie / "data" / ".publie").rglob("*") if p.is_file()], (
        "le rapport fabriqué a atteint le bundle")

    # 1 et 2. Le journal de CI et l'artefact publié : les deux chemins de rendu refusent.
    for appel in (pub_mod.valider_rapport_publiable, pub_mod.construire_publication,
                  runner.rendre_markdown):
        with pytest.raises(pub_mod.RapportInexploitable):
            appel(fabrique, preuve_externe=None)

    # 3 et 4. La route et la page ne peuvent porter que ce qui a été publié — et rien ne l'a été.
    # On publie donc un artefact **valide et différent**, puis on vérifie qu'aucune des quatre
    # surfaces ne porte une seule des valeurs fabriquées : l'assertion mordrait si un chemin avait
    # laissé passer le rapport (revue R4, la « preuve qui ne peut pas échouer »).
    pub = _publication()
    _json_path, md_path = _ecrire(pub, tmp_path)
    corps = _servir(tmp_path)
    textes = "\n".join(_composer(corps)["textes"])
    servi = json.dumps(corps, ensure_ascii=False)
    markdown = md_path.read_text(encoding="utf-8")
    rendu_ci = runner.rendre_markdown(_rapport(), pub, preuve_externe=None)
    for surface in (markdown, servi, textes, rendu_ci):
        assert LIGNE_DU_VERDICT not in surface
        for fabriquee in ("LABEL_INCONNU", "-12.5000", "-9.0000", "vertical"):
            assert fabriquee not in surface, f"{fabriquee!r} publié sur une surface"
    # Et aucun fichier écrit sous ce répertoire ne porte la ligne du verdict, quel qu'il soit.
    for fichier in tmp_path.rglob("*"):
        if fichier.is_file():
            assert LIGNE_DU_VERDICT not in fichier.read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("champ,valeur,motif", [
    ("suite", "vertical", "vocabulaire"),
    ("label", "LABEL_INCONNU", "vocabulaire"),
    ("id", "", "vide"),
    ("variant", "", "vide"),
    ("cost_eur", -12.5, "domaine"),
    ("cost_eur_original", -9.0, "domaine"),
    ("latency_ms", -7, "domaine"),
    ("latency_ms", 3.5, "entier"),
])
def test_une_execution_hors_domaine_est_un_refus_dit(champ: str, valeur: Any, motif: str) -> None:
    """Chaque valeur que le journal du run imprime est refusée séparément, et le refus la nomme."""
    rapport = _rapport(results=[_resultat(**{champ: valeur})])
    with pytest.raises(pub_mod.RapportInexploitable, match=f"results\\[0\\]\\.{champ}"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match=motif):
        runner.rendre_markdown(rapport, preuve_externe=None)


@pytest.mark.parametrize("drapeau", ["stable", "comptabilise"])
def test_une_stabilite_en_chaines_nest_jamais_rendue_un_sur_un(drapeau: str) -> None:
    """B5 : `"oui"` est vrai en Python, jamais dans une mesure — et `1/1` serait fabriqué."""
    rapport = _rapport()
    rapport["stability"]["cases"]["s-cas-neutre"][drapeau] = "oui"
    with pytest.raises(pub_mod.RapportInexploitable, match=drapeau):
        pub_mod.stabilite_du_rapport(rapport, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match=drapeau):
        pub_mod.construire_publication(rapport, preuve_externe=None)


@pytest.mark.parametrize("drapeau", ["stable", "comptabilise"])
def test_un_drapeau_de_stabilite_absent_ferme(drapeau: str) -> None:
    rapport = _rapport()
    del rapport["stability"]["cases"]["s-cas-neutre"][drapeau]
    with pytest.raises(pub_mod.RapportInexploitable, match=drapeau):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)


def test_une_stabilite_mesuree_sur_un_autre_n_que_le_repeat_ferme() -> None:
    """L'invariant de dénominateur : la stabilité se mesure sur les répétitions **planifiées**."""
    rapport = _rapport()
    rapport["stability"]["n"] = 1
    with pytest.raises(pub_mod.RapportInexploitable, match="repeat"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)


@pytest.mark.parametrize("champ,valeur", [
    ("threshold", "1.0"), ("n", "3"), ("value", "0.5"),
    ("threshold", True), ("n", 1.5), ("value", None),
    ("status", "peut-etre"), ("producer", "quelquun"), ("metric", ""),
    ("value", 1.5), ("threshold", -0.1),
    # `n` était le seul champ sans domaine, et `reason` le seul garde de type sans test (P3, P9).
    ("n", -5), ("reason", ["motif"]), ("reason", 7),
])
def test_une_decision_coercible_ou_hors_domaine_est_refusee(champ: str, valeur: Any) -> None:
    """B5 : pydantic convertissait `"3"` en `3` — le refus dit s'arrêtait donc au type de l'entrée."""
    rapport = _rapport()
    rapport["decisions"][0][champ] = valeur
    with pytest.raises(pub_mod.RapportInexploitable, match=f"decisions\\[0\\]\\.{champ}"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)


def _preuve_verifiee(*, candidate_revision: str = "a" * 40,
                     plancher_digest: str = "9" * 64):
    """Une preuve externe **réellement vérifiée**, obtenue par le seul chemin qui en produit.

    Story 4.5, tour correctif 2/3, revue B5. La validation admettait une décision au `run_digest`
    étranger dès que le rapport l'inscrivait lui-même dans `external_run_digests` : la liste des
    empreintes « légitimes » était donc lue dans l'entrée non fiable qu'on validait, et `'f' * 64`
    passait. L'ancrage est désormais l'objet que `plancher.verifier_liaison_preuve` construit sur
    des octets — impossible à fabriquer par le lecteur d'un rapport, ce qui est toute la propriété.

    Le rapport de preuve est synthétique et neutre ; son `run_digest` est **recalculé** par la
    vérification, donc il n'est pas choisi ici : le test le lit sur la preuve rendue.
    """
    from server.evals.cache import empreinte_canonique
    from server.evals.plancher import CHAMPS_IMAGE, verifier_liaison_preuve

    image = {champ: f"image-{champ}" for champ in CHAMPS_IMAGE}
    image["plancher_digest"] = plancher_digest
    identite: dict[str, Any] = {"candidate_revision": candidate_revision, "image": image}
    identite["run_digest"] = empreinte_canonique(identite)
    octets = json.dumps({"identity": identite, "plancher_digest": plancher_digest},
                        sort_keys=True).encode("utf-8")
    brut = {
        "plancher_digest": plancher_digest,
        "candidate_revision": candidate_revision,
        "report_digest": hashlib.sha256(octets).hexdigest(),
        "run_digest": identite["run_digest"],
        "decisions": [{"metric": "offline_tests_pass_rate", "n": 3, "value": 1.0,
                       "run_digest": identite["run_digest"]}],
    }
    return verifier_liaison_preuve(brut, plancher_digest=plancher_digest,
                                   candidate_revision=candidate_revision,
                                   report_bytes=octets, image_courante=image)


def test_le_run_digest_dune_decision_est_oppose_a_lidentite_du_run() -> None:
    """B5 : « son `run_digest` arbitraire n'est opposé à rien ».

    Trois cas, et la distinction est le point. Une décision **mesurée par ce run** porte l'empreinte
    de ce run. Une décision venue d'une preuve trusted porte celle du run que la preuve mesure — et
    ce qui l'autorise est la **preuve vérifiée**, pas ce que le rapport déclare sur lui-même. Une
    empreinte qui n'est ni l'une ni l'autre ferme.
    """
    rapport = _rapport()
    rapport["decisions"][0]["run_digest"] = "f" * 64
    with pytest.raises(pub_mod.RapportInexploitable, match="opposée à aucun run"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)
    # **La sonde exacte du recheck** : le rapport se déclare lui-même digne de confiance. Sur
    # `2ba74ae`, cette ligne suffisait à faire accepter et publier `'f' * 64`.
    rapport["external_run_digests"] = ["f" * 64]
    rapport["decisions"][0]["producer"] = "orchestrator"
    with pytest.raises(pub_mod.RapportInexploitable, match="preuve externe vérifiée"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)
    # Et elle ne devient pas légitime parce qu'une preuve existe : la preuve établit **une**
    # empreinte, et ce n'est pas celle-là.
    verifiee = _preuve_verifiee()
    with pytest.raises(pub_mod.RapportInexploitable, match="preuve externe vérifiée"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=verifiee)
    # Le chemin légitime : l'empreinte que la preuve a réellement établie, portée par une décision
    # `orchestrator`. C'est `--orchestrator-evidence`, et il n'est pas sacrifié à une règle plus
    # simple que la réalité.
    legitime = _rapport()
    legitime["decisions"][0]["run_digest"] = verifiee.run_digest
    legitime["decisions"][0]["producer"] = "orchestrator"
    legitime["external_run_digests"] = [verifiee.run_digest]
    assert pub_mod.valider_rapport_publiable(legitime, preuve_externe=verifiee) is legitime
    # Mais la déclaration doit elle-même être une liste d'empreintes.
    legitime["external_run_digests"] = verifiee.run_digest
    with pytest.raises(pub_mod.RapportInexploitable, match="external_run_digests"):
        pub_mod.valider_rapport_publiable(legitime, preuve_externe=verifiee)


@pytest.mark.parametrize("profil", ["hors-domaine", "", "FULL", "vertical ", 3, None])
def test_un_profil_hors_du_vocabulaire_autoritaire_ne_se_publie_pas(profil: object) -> None:
    """B5, tour correctif 2/3 : la sonde exacte du recheck.

    `profile` n'était contrôlé que comme « chaîne non vide », et `profile='hors-domaine'` était
    publié tel quel sur les quatre surfaces. Le vocabulaire autoritaire existait pourtant déjà et
    n'a que deux valeurs ; il vit désormais dans `server/app/domain/evals.py`, d'où le runner **et**
    la validation le lisent — recopier le littéral dans la validation aurait fait deux autorités,
    c'est-à-dire aucune.
    """
    rapport = _rapport()
    rapport["profile"] = profil
    with pytest.raises(pub_mod.RapportInexploitable, match="profile"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match="profile"):
        pub_mod.construire_publication(rapport, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match="profile"):
        runner.rendre_markdown(rapport, preuve_externe=None)


def test_le_vocabulaire_des_profils_a_une_seule_autorite() -> None:
    """Le runner et la validation lisent la **même** définition, jamais deux littéraux jumeaux."""
    from server.app.domain import evals as domaine

    assert runner.PROFILS_LIVRES is domaine.PROFILS_LIVRES
    assert set(domaine.PROFILS_LIVRES) == {"vertical", "full"}


def test_une_preuve_verifiee_ne_se_fabrique_pas_par_lappelant() -> None:
    """B5 : l'ancrage n'existe que s'il a été **établi**, jamais parce qu'on l'a construit.

    Python n'offre pas d'objet infalsifiable, et ce test ne prétend pas le contraire : ce qu'il
    prouve est que la voie **commode** — se donner à soi-même la preuve qu'on n'a pas — est fermée.
    """
    from server.evals.plancher import PlancherInvalide, PreuveExterneVerifiee

    with pytest.raises(PlancherInvalide, match="ne se construit que par"):
        PreuveExterneVerifiee(object(), run_digest="a" * 64, candidate_revision="b" * 40,
                              report_digest="c" * 64, plancher_digest="d" * 64)
    verifiee = _preuve_verifiee()
    with pytest.raises(AttributeError):
        verifiee.run_digest = "0" * 64  # type: ignore[misc]


def test_une_empreinte_etrangere_declaree_reste_liee_a_ce_que_le_rapport_montre() -> None:
    """P5 : la preuve n'est pas un blanc-seing — la cohérence avec les décisions reste exigée.

    Ce que la preuve établit, c'est **quelle** empreinte étrangère est légitime. Elle ne dit rien de
    la façon dont le rapport s'en sert : une empreinte étrangère portée par une décision `builder`
    reste incohérente — le runner n'écrit jamais autre chose que sa propre empreinte pour ses
    propres mesures —, et une déclaration qu'aucune décision ne porte reste une porte ouverte.
    """
    verifiee = _preuve_verifiee()
    rapport = _rapport()
    rapport["decisions"][0]["run_digest"] = verifiee.run_digest
    rapport["decisions"][0]["producer"] = "builder"
    rapport["external_run_digests"] = [verifiee.run_digest]
    with pytest.raises(pub_mod.RapportInexploitable, match="producteur"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=verifiee)
    # Une déclaration orpheline ferme, même quand toutes les décisions sont par ailleurs valides.
    orpheline = _rapport()
    orpheline["external_run_digests"] = [verifiee.run_digest]
    with pytest.raises(pub_mod.RapportInexploitable, match="qu'aucune décision ne porte"):
        pub_mod.valider_rapport_publiable(orpheline, preuve_externe=verifiee)
    # Et le contrôle a lieu **même sans décisions** : le retour anticipé le sautait entièrement.
    sans_decisions = {c: v for c, v in _rapport().items()
                      if c not in ("decisions", "plancher_digest")}
    sans_decisions["external_run_digests"] = ["pas-une-empreinte"]
    with pytest.raises(pub_mod.RapportInexploitable, match="external_run_digests"):
        pub_mod.valider_rapport_publiable(sans_decisions, preuve_externe=None)


def test_une_cle_inconnue_dans_une_decision_est_un_refus_dit() -> None:
    """P3 : `decisions[i]` était la seule structure fermée contrôlée sans égalité d'ensemble.

    `GateDecision` est `extra="forbid"` : une clé en trop y levait une `ValidationError` nue, qui
    n'est pas une `ValueError`, tombait dans le dernier `except Exception` du runner et ressortait
    en « incident de rapport », **code 3** — la ligne de partage d'AD-8 franchie dans le sens que la
    spec interdit, et que R1 avait déjà eu à fermer une fois.
    """
    rapport = _rapport()
    rapport["decisions"][0]["inconnu"] = 1
    with pytest.raises(pub_mod.RapportInexploitable, match="clés inconnues"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)
    with pytest.raises(pub_mod.RapportInexploitable, match="clés inconnues"):
        pub_mod.construire_publication(rapport, preuve_externe=None)


@pytest.mark.parametrize("table", ["labels", "variants"])
@pytest.mark.parametrize("cle", ["", 7, None])
def test_une_cle_vide_ou_non_textuelle_de_table_de_comptage_ferme(table: str, cle: Any) -> None:
    """P10 : sans ce garde, le journal émettrait `| <code></code> | 3 |` et la page `{"": 3}`."""
    rapport = _rapport()
    rapport["metrics"][table] = dict(rapport["metrics"][table]) | {cle: 3}
    with pytest.raises(pub_mod.RapportInexploitable, match=f"metrics.{table}"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)


@pytest.mark.parametrize("champ,valeur,motif", [
    ("cases_hash", "pas-une-empreinte", "cases_hash"),
    ("cases_hash", True, "cases_hash"),
    ("cases_planned", -1, "domaine"),
    ("cases_completed", 3, "dépasse"),
    ("stop_reason", "", "stop_reason"),
    ("stop_reason", 7, "stop_reason"),
    ("unexecuted_cases", [""], "unexecuted_cases"),
    ("unexecuted_cases", [7], "unexecuted_cases"),
])
def test_un_compteur_ou_une_liste_racine_incoherente_est_un_refus_dit(
        champ: str, valeur: Any, motif: str) -> None:
    """« listes et compteurs racine stricts et cohérents entre eux » — chacun pris séparément."""
    rapport = _rapport()
    rapport[champ] = valeur
    with pytest.raises(pub_mod.RapportInexploitable, match=motif):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)


def test_la_table_de_labels_nadmet_aucune_cle_supplementaire() -> None:
    """AD-14 : « labels fixes ». Une clé en trop serait un huitième label publié sans en être un."""
    rapport = _rapport(labels=_tous_les_labels() | {"label_maison": 4})
    with pytest.raises(pub_mod.RapportInexploitable, match="en trop"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)


def test_une_empreinte_presente_mais_invalide_ferme_au_lieu_de_setre_rabattue_sur_none() -> None:
    """B5 : `_empreinte` ne rabat plus silencieusement — absent reste absent, invalide ferme."""
    assert pub_mod._empreinte(None) is None
    assert pub_mod._empreinte("") is None
    assert pub_mod._empreinte("a" * 64) == "a" * 64
    with pytest.raises(pub_mod.RapportInexploitable, match="empreinte"):
        pub_mod._empreinte("pas-une-empreinte")
    with pytest.raises(pub_mod.RapportInexploitable, match="empreinte"):
        pub_mod._empreinte("A" * 64)


def test_un_plancher_digest_racine_mal_forme_ferme() -> None:
    rapport = _rapport()
    rapport["plancher_digest"] = "pas-une-empreinte"
    with pytest.raises(pub_mod.RapportInexploitable, match="plancher_digest"):
        pub_mod.valider_rapport_publiable(rapport, preuve_externe=None)


# --- B7 : l'invariant autoritaire de la bascule ----------------------------------------------------
#
# Ces tests étaient, aux tours précédents, des **consécrations** de l'état mêlé : ils exigeaient que
# `BasculePartielle` nomme les cibles laissées dans le nouvel état, et acceptaient donc qu'une cible
# de rang antérieur reste publiée. Ils sont devenus les contre-sondes de l'invariant autoritaire :
#
#     après toute exception, à n'importe quel rang, `BaseException` et interruption comprises,
#     **zéro cible du lot** n'est modifiée ni visible dans le nouvel état.
#
# La contre-sonde exacte du recheck — un lot d'au moins trois cibles remises **au même appel**, panne
# au deuxième renommage, puis `KeyboardInterrupt` pendant ce qui était la restauration — est
# `test_la_contre_sonde_du_recheck_ne_laisse_aucune_cible_publiee`. Elle est rouge sur `24569cc`
# (`a` y vaut `'nouveau-a'`) et verte ici.


def _espace_de_lot(racine: Path, noms: tuple[str, ...]) -> tuple[EspacePublie, list[Path]]:
    """Un espace posé et un lot d'au moins trois cibles, dont le manifest, à l'état « ancien ».

    Le manifest fait partie du lot, littéralement : la sixième substitution interdite est de l'en
    sortir pour le basculer à part. Les cibles vivent dans trois racines différentes — `data/`,
    `docs/` et une sortie de run — pour que la sonde porte sur le cas réel et pas sur un lot
    commodément rassemblé dans un seul répertoire.
    """
    cibles = [Path("data") / "manifest.json", Path("docs") / "evals" / "latest.md",
              Path("out") / "results.md", *(Path("out") / nom for nom in noms)]
    espace = EspacePublie(racine)
    espace.installer(cibles)
    espace.basculer([(racine / c, f"ancien-{c.name}") for c in cibles])
    return espace, [racine / c for c in cibles]


def _etat_observable(cibles: list[Path]) -> dict[str, tuple[str, int]]:
    """Contenu **et** type d'entrée (`lstat`) de chaque cible.

    L'interdiction 7 définit « modifiée » par l'état observable : contenu, type d'entrée, cible de
    lien, présence ou absence. Comparer les seuls contenus laisserait passer une migration de type,
    qui est exactement la substitution qu'un candidat précédent avait employée.
    """
    etat = {}
    for cible in cibles:
        marque = os.lstat(cible).st_mode >> 12
        etat[str(cible)] = (cible.read_text(encoding="utf-8"), marque)
    return etat


def test_la_contre_sonde_du_recheck_ne_laisse_aucune_cible_publiee(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La sonde exacte du recheck : panne au **deuxième** renommage, puis interruption ensuite.

    Sur `24569cc`, elle rendait `BasculePartielle`, `non_restaurees=['…/a']` et laissait la cible du
    premier rang à `'nouveau-a'` — et le test d'alors restait vert *précisément parce qu'il
    acceptait cet état*. Ici, aucune cible n'a bougé, parce qu'aucune n'est modifiée tant que
    l'unique `os.replace` du pointeur n'a pas eu lieu : il n'y a rien à défaire, donc rien qui
    puisse échouer ou être interrompu en défaisant.
    """
    espace, cibles = _espace_de_lot(tmp_path, ("a", "b", "c"))
    avant = _etat_observable(cibles)
    vrai = os.replace
    compte = {"n": 0}

    def replace_faillible(src: Any, dst: Any) -> Any:
        compte["n"] += 1
        if compte["n"] == 2:
            raise OSError("panne au deuxième renommage (injectée)")
        if compte["n"] == 3:
            raise KeyboardInterrupt()
        return vrai(src, dst)

    monkeypatch.setattr(os, "replace", replace_faillible)
    with pytest.raises(BaseException):  # noqa: B017, PT011 — l'identité de la cause est éprouvée ailleurs
        espace.basculer([(c, f"nouveau-{c.name}") for c in cibles])
    monkeypatch.undo()

    assert _etat_observable(cibles) == avant, (
        "aucune cible du lot n'est modifiée ni visible dans le nouvel état")
    assert _temporaires(tmp_path) == [], "aucun temporaire ne subsiste"


@pytest.mark.parametrize("rang", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("exception", [OSError("panne injectée"),
                                       RuntimeError("panne injectée hors OSError"),
                                       KeyboardInterrupt()],
                         ids=["oserror", "runtimeerror", "interruption"])
def test_une_bascule_interrompue_a_nimporte_quel_rang_ne_laisse_ni_cible_ni_temporaire(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rang: int,
        exception: BaseException) -> None:
    """Chaque rang, trois causes : le seul état admissible à la sortie est « rien n'a bougé ».

    Le dernier rang est l'atome lui-même. Y échouer laisse le pointeur sur l'ancienne génération,
    donc le lot entier dans son état d'avant : c'est le même invariant, à la frontière.
    """
    espace, cibles = _espace_de_lot(tmp_path, ("a", "b"))
    avant = _etat_observable(cibles)
    vrai = os.replace
    compte = {"n": 0}

    def replace_faillible(src: Any, dst: Any) -> Any:
        compte["n"] += 1
        if compte["n"] == rang:
            raise exception
        return vrai(src, dst)

    monkeypatch.setattr(os, "replace", replace_faillible)
    with pytest.raises(type(exception)):
        espace.basculer([(c, f"nouveau-{c.name}") for c in cibles])
    monkeypatch.undo()

    assert _etat_observable(cibles) == avant, "aucune cible ne reste dans le nouvel état"
    assert _temporaires(tmp_path) == [], "aucun temporaire ne subsiste"


def test_la_bascule_ne_cree_ne_migre_ni_ne_change_le_type_daucune_cible(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La distinction que l'interdiction 7 trace, **prouvée** et non affirmée.

    Ce qu'elle proscrit est la pose séquentielle à l'exécution : des liens installés cible par cible
    avant la bascule, où une exception en cours de pose laisse des cibles modifiées. La disposition
    de ce protocole est statique — committée dans le dépôt, posée par `installer()` hors de toute
    transaction — et la bascule n'appelle jamais `os.symlink`, `os.unlink` ni `shutil.move` sur une
    cible. Ici on l'observe : aucun appel de pose ne vise une cible du lot, et le type d'entrée de
    chacune est le même avant et après une bascule **réussie**.
    """
    espace, cibles = _espace_de_lot(tmp_path, ("a", "b"))
    types_avant = {str(c): os.lstat(c).st_mode >> 12 for c in cibles}
    vises: list[str] = []
    vrai_symlink, vrai_unlink, vrai_move = os.symlink, os.unlink, shutil.move
    noms = {str(c) for c in cibles}

    def _noter(fn: Any, nom: str) -> Any:
        def _piste(src: Any, dst: Any = None, *a: Any, **k: Any) -> Any:
            for candidat in (src, dst):
                if candidat is not None and str(candidat) in noms:
                    vises.append(f"{nom}({candidat})")
            return fn(src, dst, *a, **k) if dst is not None else fn(src)
        return _piste

    monkeypatch.setattr(os, "symlink", _noter(vrai_symlink, "symlink"))
    monkeypatch.setattr(os, "unlink", _noter(vrai_unlink, "unlink"))
    monkeypatch.setattr(shutil, "move", _noter(vrai_move, "move"))
    espace.basculer([(c, f"nouveau-{c.name}") for c in cibles])
    monkeypatch.undo()

    assert vises == [], f"la bascule a posé ou retiré une cible : {vises}"
    assert {str(c): os.lstat(c).st_mode >> 12 for c in cibles} == types_avant, (
        "le type d'entrée d'une cible a changé pendant une bascule")
    assert all(c.read_text(encoding="utf-8") == f"nouveau-{c.name}" for c in cibles), (
        "la bascule réussie publie bien tout le lot")


def test_une_cible_hors_de_lespace_est_un_refus_avant_toute_ecriture(tmp_path: Path) -> None:
    """La bascule n'installe jamais ce qui manque : elle refuse, et nomme la commande qui l'installe."""
    espace, cibles = _espace_de_lot(tmp_path, ("a",))
    avant = _etat_observable(cibles)
    etrangere = tmp_path / "hors-espace.md"
    etrangere.write_text("intact", encoding="utf-8")
    with pytest.raises(EspaceNonInstalle, match="n'est pas résolue"):
        espace.basculer([(c, "neuf") for c in cibles] + [(etrangere, "neuf")])
    assert _etat_observable(cibles) == avant, "un refus ne publie rien du lot"
    assert etrangere.read_text(encoding="utf-8") == "intact"
    assert not etrangere.is_symlink(), "un refus n'installe pas la cible qu'il refuse"
    assert _temporaires(tmp_path) == []


def test_ecrire_gate_est_couvert_par_le_meme_contrat(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """B7, chemin frère : `ecrire_gate` passe par le même unique pointeur atomique.

    Il n'a plus de temporaire à lui — `preparer_gate` ne fait que lire, valider et sérialiser —,
    donc plus de temporaire à oublier. Une panne de renommage laisse le manifest byte-identique.
    """
    data = tmp_path / "data"
    data.mkdir()
    manifest = data / "manifest.json"
    entree = {"status": "servi", "source_hash": "s", "ingest_fingerprint": "f",
              "document_hash": "d", "edition": "2020", "overlay_hash": None,
              "structure_hash": None, "gate": None}
    manifest.write_text(json.dumps({"doc-neutre": entree}, indent=2) + "\n", encoding="utf-8")
    poser_espace(tmp_path, data_dir=data)
    avant = manifest.read_bytes()

    vrai = os.replace

    def replace_faillible(src: Any, dst: Any) -> Any:
        raise OSError("panne de renommage (injectée)")

    monkeypatch.setattr(os, "replace", replace_faillible)
    with pytest.raises(OSError, match="injectée"):
        runner.ecrire_gate(manifest, "doc-neutre", _gate())
    monkeypatch.setattr(os, "replace", vrai)
    assert _temporaires(tmp_path) == [], "aucun `.tmp` ne reste dans data/"
    assert manifest.read_bytes() == avant, "le manifest est byte-identique"

    # Et le chemin nominal publie bien, par le même unique atome.
    assert runner.ecrire_gate(manifest, "doc-neutre", _gate()) is True
    assert json.loads(manifest.read_text(encoding="utf-8"))["doc-neutre"]["gate"] is not None
