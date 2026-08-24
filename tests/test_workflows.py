"""Les deux workflows, relus et assertés — la seule façon de vérifier hors ligne un fichier qui ne
s'exécute que chez GitHub.

`deploy.yml` et `ci.yml` portent, en texte, des décisions que le spine tient pour des invariants :
l'ordre « smoke puis promotion » d'AD-12, les cinq drapeaux de dimensionnement d'AD-13, la version
épinglée du secret, l'absence d'évals en CI. Rien de tout cela n'a d'exécution locale ; un test qui
relit le YAML est ce qui empêche qu'un réglage se perde entre deux stories.

Ce que ces tests **ne** prouvent pas, et qui reste écrit plutôt que supposé : que l'échange de jeton
OIDC entre GitHub et le pool WIF aboutit. Il ne s'exerce que depuis un runner GitHub.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
DEPLOY = WORKFLOWS / "deploy.yml"
CI = WORKFLOWS / "ci.yml"


def lire(fichier: Path) -> dict[str, Any]:
    return yaml.safe_load(fichier.read_text("utf-8"))


def declencheurs(doc: dict[str, Any]) -> dict[str, Any]:
    """`on:` est lu par YAML 1.1 comme le booléen vrai — d'où cette lecture des deux clés."""
    valeur = doc.get("on", doc.get(True))
    assert isinstance(valeur, dict), "les déclencheurs doivent être une table"
    return valeur


def etapes(doc: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return doc["jobs"][job]["steps"]


def index_de(pas: list[dict[str, Any]], motif: str) -> int:
    """Rang de la première étape dont le `uses` ou le `run` porte le motif."""
    for i, e in enumerate(pas):
        if motif in str(e.get("uses", "")) or motif in str(e.get("run", "")):
            return i
    raise AssertionError(f"aucune étape ne porte {motif!r}")


def _index_par_nom(pas: list[dict[str, Any]], prefixe: str) -> int:
    """Rang de la première étape dont le `name` commence par ce préfixe."""
    for i, e in enumerate(pas):
        if str(e.get("name", "")).startswith(prefixe):
            return i
    raise AssertionError(f"aucune étape nommée {prefixe!r}")


def texte(fichier: Path) -> str:
    return fichier.read_text("utf-8")


# --- les deux fichiers existent, et le dossier n'est plus vide ------------------------------------

def test_les_deux_workflows_existent_et_le_gitkeep_a_disparu() -> None:
    assert DEPLOY.is_file() and CI.is_file()
    assert not (WORKFLOWS / ".gitkeep").exists(), "le dossier n'est plus vide : le témoin s'en va"


# --- `deploy.yml` --------------------------------------------------------------------------------

def test_le_deploiement_ne_part_que_de_main_ou_de_la_main_dun_humain() -> None:
    """AD-12 : « environnement unique (`main` → `foyer-retour`, `europe-west1`) ».

    Ce que ce test défend n'est pas le nombre de déclencheurs mais leur **portée** : aucun d'eux ne
    doit pouvoir déployer depuis une autre référence que `main`, ni depuis une pull request — dont le
    code n'a, par construction, pas encore été relu. `workflow_dispatch` est admis parce qu'il ne
    relâche rien : GitHub ne l'offre qu'à qui peut déjà écrire sur le dépôt, et le workflow rejoue
    lint, suite, build et smokes à l'identique. Sans lui, re-déployer ou re-sonder le service exigeait
    de pousser un commit vide.
    """
    doc = lire(DEPLOY)
    on = declencheurs(doc)
    assert set(on) <= {"push", "workflow_dispatch"}, (
        "un déclencheur de plus est un chemin de plus vers la production : le justifier ici")
    assert "push" in on and on["push"]["branches"] == ["main"]
    for interdit in ("pull_request", "pull_request_target", "schedule"):
        assert interdit not in on
    # `workflow_dispatch` laisse **choisir la branche** dans l'interface de GitHub : sans cette garde,
    # un « Run workflow » sur une branche de travail construirait et promouvrait ce code en
    # production. La portée n'est donc pas tenue par la liste des déclencheurs, mais ici (revue 1.11).
    assert doc["jobs"]["deployer"]["if"] == "github.ref == 'refs/heads/main'", (
        "le job de déploiement doit refuser toute référence autre que `main`")


def test_le_deploiement_demande_le_jeton_oidc() -> None:
    """Sans `id-token: write`, `auth@v3` ne peut pas fédérer — et il faudrait une clé dans le dépôt."""
    assert lire(DEPLOY)["permissions"]["id-token"] == "write"


def test_le_lint_et_la_suite_gardent_la_porte_avant_tout_deploiement() -> None:
    jobs = lire(DEPLOY)["jobs"]
    assert jobs["verifier"]["uses"] == "./.github/workflows/ci.yml", (
        "le job de vérification réutilise `ci.yml` : un seul texte dit ce que « vert » veut dire")
    assert jobs["deployer"]["needs"] == "verifier"


def test_les_versions_dactions_sont_celles_du_spine() -> None:
    """Stack figée : `auth@v3`, `deploy-cloudrun@v3` (AD-12, table Stack)."""
    uses = [e.get("uses", "") for e in etapes(lire(DEPLOY), "deployer")]
    assert "google-github-actions/auth@v3" in uses
    assert "google-github-actions/deploy-cloudrun@v3" in uses


def deploiement() -> dict[str, Any]:
    for e in etapes(lire(DEPLOY), "deployer"):
        if e.get("uses", "").startswith("google-github-actions/deploy-cloudrun"):
            return e["with"]
    raise AssertionError("aucune étape `deploy-cloudrun`")


def test_le_service_la_region_et_le_projet_sont_ceux_dad_12() -> None:
    avec = deploiement()
    env = lire(DEPLOY)["jobs"]["deployer"]["env"]
    assert avec["service"] == "${{ env.SERVICE }}" and env["SERVICE"] == "foyer-retour"
    assert avec["source"] == "."
    assert avec["region"] == "${{ env.REGION }}" and env["REGION"] == "europe-west1"
    # Le projet vient de la variable du dépôt posée par `gcp_bootstrap.sh` : il n'est écrit qu'une fois.
    assert avec["project_id"] == "${{ vars.GCP_PROJECT_ID }}"


def test_le_secret_est_epingle_a_une_version() -> None:
    """AD-12 : « `ANTHROPIC_API_KEY` via Secret Manager (version épinglée) »."""
    avec = deploiement()
    assert "ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:1" in avec["secrets"]
    # `overwrite` : la version est décidée ici, jamais héritée de la configuration du service.
    assert avec["secrets_update_strategy"] == "overwrite"


def test_le_workflow_est_lautorite_sur_les_variables_du_service() -> None:
    """La reprise différée de 1.10 : un `ALLOW_UNGATED=true` posé à la main ne survit pas.

    `merge` (le défaut) aurait laissé vivre indéfiniment une variable posée sur le service. Avec
    `overwrite`, la configuration du service est exactement celle que ce fichier énumère.
    """
    avec = deploiement()
    assert avec["env_vars_update_strategy"] == "overwrite"
    assert "GIT_SHA=${{ steps.sha.outputs.sha7 }}" in avec["env_vars"]
    assert "ALLOW_UNGATED" not in avec["env_vars"]


@pytest.mark.parametrize("drapeau", ["--allow-unauthenticated", "--max-instances=1",
                                     "--concurrency=2", "--timeout=60", "--min-instances=0"])
def test_les_cinq_drapeaux_de_dimensionnement_dad_13(drapeau: str) -> None:
    """AD-13 : le dimensionnement initial, et le plafond dur de la facture."""
    assert drapeau in deploiement()["flags"]


def test_le_dimensionnement_nexiste_quune_fois_dans_le_depot() -> None:
    """Convention Seuils : jamais deux textes faisant autorité sur la même valeur.

    Ce sont des drapeaux d'**infrastructure** : ils ne vivent pas dans `config.py`, qui configure le
    processus. Mais ils ne doivent pas non plus être recopiés dans un second workflow.
    """
    assert "--max-instances" not in texte(CI)
    assert texte(DEPLOY).count("--concurrency=") == 1


def test_le_runtime_porte_le_compte_de_service_dedie() -> None:
    """Seul `foyer-retour-run@` a `secretmanager.secretAccessor` (story 1.0)."""
    assert "--service-account=foyer-retour-run@" in deploiement()["flags"]


def test_la_revision_est_deployee_sans_trafic_sous_un_tag() -> None:
    """AD-12 : les smokes ont lieu « avant promotion du trafic » — donc sur une révision non servie."""
    avec = deploiement()
    assert avec["no_traffic"] is True
    assert avec["tag"] == "${{ env.TAG }}"
    assert lire(DEPLOY)["jobs"]["deployer"]["env"]["TAG"] == "candidat"


def test_wait_est_pose_parce_que_le_spine_lecrit() -> None:
    assert deploiement()["wait"] is True


def test_le_trafic_nest_promu_quapres_le_smoke() -> None:
    """L'invariant de la story, et il est **ordinal** : c'est le rang des étapes qui le porte."""
    pas = etapes(lire(DEPLOY), "deployer")
    deploiement_i = index_de(pas, "deploy-cloudrun")
    smoke_i = index_de(pas, "scripts/smoke.py")
    promotion_i = index_de(pas, "update-traffic")
    assert deploiement_i < smoke_i < promotion_i, (
        "le smoke doit être joué après le déploiement et avant la promotion du trafic (AD-12)")


def test_le_smoke_sonde_lurl_taguee_et_le_sha7_du_commit() -> None:
    """Sonder l'URL du **service** mesurerait l'ancienne révision : le tag est ce qui adresse la neuve."""
    pas = etapes(lire(DEPLOY), "deployer")
    smoke = pas[index_de(pas, "scripts/smoke.py")]
    assert smoke["env"]["URL"] == "${{ steps.candidate.outputs.url }}"
    assert smoke["env"]["SHA7"] == "${{ steps.sha.outputs.sha7 }}"
    resolution = pas[index_de(pas, "status.traffic")]
    assert resolution["id"] == "candidate"
    assert "gcloud run services describe" in resolution["run"]


def test_la_promotion_nomme_la_revision_sondee_et_retire_le_tag() -> None:
    """`--to-latest` promeut « la dernière créée », pas celle que le smoke vient de valider.

    Une révision créée entre-temps — un `gcloud run deploy` lancé à la main pendant les 40 s du
    smoke — recevrait 100 % du trafic sans avoir été sondée : la promotion « quand même » qu'AD-16
    interdit. La révision est donc capturée en même temps que son URL taguée, dans la **même** entrée
    de `status.traffic[]`, et promue nommément.
    """
    pas = etapes(lire(DEPLOY), "deployer")
    promotion = pas[index_de(pas, "update-traffic")]
    run = promotion["run"]
    assert "--to-latest" not in run, "la promotion doit nommer une révision, pas « la dernière »"
    assert '--to-revisions="$REVISION=100"' in run
    assert "--remove-tags=" in run
    assert promotion["env"]["REVISION"] == "${{ steps.candidate.outputs.revision }}"
    # …et cette sortie vient bien de l'étape qui a résolu l'URL taguée, donc de la même entrée.
    resolution = pas[index_de(pas, "status.traffic")]["run"]
    assert "revisionName" in resolution and 'revision=$revision' in resolution


def test_un_smoke_rouge_detague_la_revision_refusee() -> None:
    """Une révision refusée reste sinon publiquement joignable, avec le secret Anthropic monté.

    `--allow-unauthenticated` vaut aussi pour l'URL taguée : sans ce nettoyage, la révision que le
    smoke vient de refuser resterait servie sur `https://candidat---…` jusqu'au prochain déploiement
    réussi. Le `|| true` du script est délibéré — un nettoyage qui échoue ne doit pas remplacer, dans
    l'affichage, la cause d'échec qu'il vient nettoyer.
    """
    pas = etapes(lire(DEPLOY), "deployer")
    nettoyage = pas[_index_par_nom(pas, "Retirer le tag")]
    # `failure()` **et** `cancelled()` : une annulation à la main, ou le plafond `timeout-minutes`
    # atteint pendant les 40 s du smoke, laisse exactement la même révision non validée en vol — et
    # `failure()` seul ne s'y déclenche pas (revue 1.11).
    assert "failure()" in nettoyage["if"] and "cancelled()" in nettoyage["if"], (
        "une annulation laisse la même révision taguée et publique qu'un échec")
    assert "--remove-tags=" in nettoyage["run"]
    assert "|| " in nettoyage["run"], "le nettoyage ne doit pas masquer la cause d'échec d'origine"
    # Il vient **après** la promotion : un `if: failure()` placé avant s'exécuterait quand même, mais
    # le lire dans l'ordre du fichier est ce qui rend la séquence relisible.
    assert _index_par_nom(pas, "Retirer le tag") > index_de(pas, "update-traffic")


def test_les_deux_jobs_ont_un_plafond_de_duree() -> None:
    """Sans `timeout-minutes`, un appel bloqué tient le groupe `concurrency` six heures.

    Et pas à vide : pendant tout ce temps, une révision taguée, publique et non promue resterait en
    vol. Le plafond est ce qui déclenche l'échec, donc le détagage de l'étape `if: failure()`.
    """
    assert lire(DEPLOY)["jobs"]["deployer"]["timeout-minutes"] <= 60
    assert lire(CI)["jobs"]["verifier"]["timeout-minutes"] <= 60


def test_le_deploiement_ne_lance_aucune_eval() -> None:
    """Pas d'évals en CI : elles coûtent et exigent la clé — sous-ensemble rapide en 4.1 (AD-14).

    Le contrôle porte sur ce qui **s'exécute** (`run:`), pas sur le fichier entier : les commentaires
    des deux workflows expliquent précisément pourquoi les évals n'y sont pas, et un test qui
    interdirait le mot interdirait de l'écrire. Une version antérieure neutralisait ce conflit par un
    `.replace()` d'une chaîne qu'aucun des deux fichiers ne contenait — une garde qui ne gardait rien.
    """
    for fichier in (DEPLOY, CI):
        doc = lire(fichier)
        for job in doc["jobs"].values():
            for etape in job.get("steps", []):
                commande = str(etape.get("run", ""))
                assert "evals" not in commande, f"{fichier.name} exécute une éval : {commande!r}"


def test_deux_deploiements_ne_se_promeuvent_pas_dans_le_desordre() -> None:
    concurrence = lire(DEPLOY)["concurrency"]
    assert concurrence["cancel-in-progress"] is False


def test_le_timeout_cloud_run_couvre_la_deadline_du_serveur() -> None:
    """`--timeout` d'AD-13 et `deadline_s` de `config.py` sont deux autorités qui se croisent.

    L'infrastructure coupe la requête à `--timeout` ; le processus s'accorde `deadline_s` pour
    répondre. Si la première descendait sous la seconde, Cloud Run tuerait des requêtes que le
    pipeline honore, et aucune suite ne rougirait — les deux nombres vivent dans deux fichiers qui
    ne se lisent pas l'un l'autre. Ce test est le seul endroit où ils se rencontrent (revue 1.11).
    """
    from server.app.config import Settings

    drapeaux = deploiement()["flags"]
    valeurs = re.findall(r"--timeout=([0-9]+)", drapeaux)
    assert len(valeurs) == 1, f"un seul `--timeout` attendu dans les drapeaux : {valeurs}"
    assert float(valeurs[0]) >= Settings(_env_file=None).deadline_s, (
        "Cloud Run couperait la requête avant que le serveur ait fini de répondre")


def test_le_readme_cite_les_drapeaux_du_workflow_sans_les_reecrire() -> None:
    """Le README **annonce** qu'un test empêche sa citation de prendre le pas sur la source.

    Sans ce test, l'affirmation était fausse : rien ne comparait la phrase du README aux drapeaux de
    `deploy.yml`, et la citation pouvait dériver en silence (revue 1.11). On compare les cinq valeurs
    de dimensionnement, pas `--service-account` : celui-ci est une identité, pas un dimensionnement,
    et il est dérivé d'une variable de dépôt que le README n'a pas à recopier.
    """
    readme = (WORKFLOWS.parents[1] / "README.md").read_text("utf-8")
    # Les jetons qui ne commencent pas par `--` sont les morceaux de l'interpolation
    # `${{ vars.GCP_PROJECT_ID }}` du `--service-account`, découpée par `split()`.
    dimensionnement = [d for d in deploiement()["flags"].split()
                       if d.startswith("--") and not d.startswith("--service-account")]
    assert len(dimensionnement) == 5, f"cinq drapeaux de dimensionnement attendus : {dimensionnement}"
    assert " ".join(dimensionnement) in readme, (
        f"le README ne cite pas les drapeaux tels que `deploy.yml` les décide : {dimensionnement}")


def test_le_readme_compte_les_smokes_que_le_smoke_joue() -> None:
    """Un README qui décrit un smoke plus petit que celui qui tourne se relit comme une garantie."""
    from scripts.smoke import SURFACES

    readme = (WORKFLOWS.parents[1] / "README.md").read_text("utf-8")
    for chemin in SURFACES:
        assert f"`{chemin}`" in readme
    assert "quatre vérifications" in readme, (
        "le smoke en joue quatre (santé, surfaces, chat, sinistre) : le README doit dire le nombre "
        "qu'il joue, pas celui qu'il jouait")


# --- `.gcloudignore` : ce qui quitte le poste ------------------------------------------------------

def test_le_gcloudignore_empeche_la_cle_de_quitter_le_poste() -> None:
    """`.gcloudignore` **remplace** le repli implicite que gcloud dérivait de `.gitignore`.

    Une fois ce fichier posé, `.gitignore` ne protège plus rien du téléversement : `--source` envoie
    dans `gs://run-sources-…` tout ce que `.gcloudignore` n'exclut pas. Une ligne `.env` retirée par
    inadvertance suffirait donc à déposer la clé Anthropic dans un bucket, sans qu'aucun test ne
    rougisse — c'était le cas avant celui-ci (revue 1.11).
    """
    fichier = WORKFLOWS.parents[1] / ".gcloudignore"
    assert fichier.is_file(), "sans ce fichier, gcloud dérive ses exclusions de `.gitignore`"
    motifs = {ligne.strip() for ligne in fichier.read_text("utf-8").splitlines()
              if ligne.strip() and not ligne.lstrip().startswith("#")}
    for secret in (".env", ".env.*", "*.key", "service-account*.json"):
        assert secret in motifs, f"`{secret}` doit rester exclu du téléversement de `--source`"
    # …et rien ne doit les réadmettre par une négation.
    assert not any(m.startswith("!") and "env" in m and m != "!.env.example" for m in motifs)
    # La méthode de travail ne descend jamais dans le dépôt public, et a fortiori pas dans un bucket.
    for interne in ("_bmad", "_bmad-output", "_reference", ".claude"):
        assert interne in motifs


# --- `ci.yml` ------------------------------------------------------------------------------------

def test_la_ci_se_declenche_sur_les_pull_requests() -> None:
    """FR44 d : « `ci.yml` sur PR = tests sans déploiement »."""
    on = declencheurs(lire(CI))
    assert "pull_request" in on
    assert "workflow_call" in on, "appelée aussi par `deploy.yml` : un seul texte pour deux portes"
    assert "push" not in on


def test_la_ci_ne_deploie_rien() -> None:
    """Une pull request ne touche jamais le service (AD-12)."""
    contenu = texte(CI)
    for interdit in ("deploy-cloudrun", "update-traffic", "google-github-actions/auth"):
        assert interdit not in contenu


def test_la_ci_lint_puis_teste() -> None:
    pas = etapes(lire(CI), "verifier")
    runs = " ".join(str(e.get("run", "")) for e in pas)
    assert "ruff check server tests scripts" in runs
    assert "pytest -q" in runs
    assert index_de(pas, "ruff check") < index_de(pas, "pytest")


def test_la_suite_tourne_sans_cle_et_avec_le_front_exige() -> None:
    """Les deux reprises différées : `ANTHROPIC_API_KEY=` (AD-14) et `FRONT_TESTS_REQUIS=1` (1.7/1.9).

    Sans la seconde, une image sans `node` fait passer 130 cas du front en `skip` silencieux, et un
    `skip` est indiscernable d'un succès dans un `pytest -q`.
    """
    pas = etapes(lire(CI), "verifier")
    tests = pas[index_de(pas, "pytest")]
    assert tests["env"]["ANTHROPIC_API_KEY"] == ""
    assert str(tests["env"]["FRONT_TESTS_REQUIS"]) == "1"


def test_le_lock_fait_foi_dans_les_deux_workflows() -> None:
    """`uv sync --frozen` : la CI résout exactement ce que l'image construira (`uv.lock`)."""
    for fichier in (CI, DEPLOY):
        assert "uv sync --frozen" in texte(fichier)


def test_uv_est_epingle_sur_la_serie_du_dockerfile_et_partout_a_la_meme_version() -> None:
    """L'image utilise `ghcr.io/astral-sh/uv:0.11` : la CI ne doit pas résoudre avec une autre série.

    Et pas seulement la même **série** : la même version exacte dans les deux workflows. Contrôler le
    seul préfixe `0.11.` laissait passer un `ci.yml` en 0.11.32 et un `deploy.yml` en 0.11.9 — deux
    résolveurs pour un seul `uv.lock`, et l'écart ne se verrait qu'un jour où il compte.
    """
    dockerfile = (WORKFLOWS.parents[1] / "Dockerfile").read_text("utf-8")
    assert "astral-sh/uv:0.11" in dockerfile
    versions = {v for fichier in (CI, DEPLOY)
                for v in re.findall(r"astral\.sh/uv/([0-9]+\.[0-9]+\.[0-9]+)/install\.sh", texte(fichier))}
    assert len(versions) == 1, f"versions d'uv divergentes entre les workflows : {sorted(versions)}"
    assert next(iter(versions)).startswith("0.11."), "série différente de celle du Dockerfile"


def test_node_est_installe_et_epingle() -> None:
    """`FRONT_TESTS_REQUIS=1` fait dépendre 130 cas de `node` : sa version ne se subit pas.

    Sans étape dédiée, c'est l'image du runner qui décide quel moteur exécute la moitié de nos
    assertions de front, et GitHub la change sans prévenir. `uv` est épinglé ; `node` doit l'être.
    """
    pas = etapes(lire(CI), "verifier")
    node = pas[index_de(pas, "actions/setup-node")]
    version = str(node["with"]["node-version"])
    assert version and version[0].isdigit(), "la version de node doit être explicite, pas 'lts/*'"
    assert index_de(pas, "actions/setup-node") < index_de(pas, "pytest")


def test_la_ci_telecharge_les_sources_avant_de_tester() -> None:
    """Sinon la CI joue une suite plus faible que celle que le dépôt annonce.

    `test_real_pdf_regenerates_committed_artefacts` est gardé par un `skipif` sur la présence de
    `data/axa-lu-optihome-2017/source.pdf`, qui n'existe jamais sur un runner : le seul test qui
    prouve que les artefacts AXA committés correspondent encore au parseur sautait **en silence**,
    dans la CI qui garde la production. Le `skipif` est évalué à la collecte : le téléchargement doit
    donc être une étape à part, avant `pytest`, et non un `fixture`.
    """
    pas = etapes(lire(CI), "verifier")
    assert index_de(pas, "fetch_source --all") < index_de(pas, "pytest")


# --- ce que le workflow ne peut pas faire seul : la procédure écrite -----------------------------

def test_le_readme_porte_la_procedure_de_retour_arriere() -> None:
    """FR44 e — un retour arrière qui n'est écrit nulle part n'existe pas le jour où il faut le faire.

    C'est la seule ligne de la matrice de la story que ni le YAML ni le smoke ne peuvent tenir : elle
    se joue à la main, sur un service déjà en ligne, par quelqu'un qui n'a pas le temps de relire un
    workflow. Le test exige donc la commande **complète** — le service, la révision, la région et le
    projet —, parce qu'une commande incomplète recopiée sous pression échoue sur le projet par défaut
    du poste (qui n'est pas `foyer-retour`).
    """
    readme = (WORKFLOWS.parents[1] / "README.md").read_text("utf-8")
    # **Les deux lignes**, et pas seulement celle qui bascule : sans la première, on ne connaît pas le
    # nom de la révision précédente, et `--to-revisions=<révision>` n'a rien à recevoir. Le groupe est
    # `gcloud run revisions` — `gcloud run services revisions` n'existe pas, et une commande inventée
    # dans un README est pire que pas de README : elle se recopie sous pression, et elle échoue.
    for fragment in ("gcloud run revisions list --service=foyer-retour",
                     "gcloud run services update-traffic foyer-retour",
                     "--to-revisions=", "--region=europe-west1", "--project=foyer-retour"):
        assert fragment in readme, f"le README ne porte pas {fragment!r} : le retour arrière n'est pas documenté"
    assert "gcloud run services revisions" not in readme, (
        "`gcloud run services revisions` n'est pas une commande gcloud (le groupe est `gcloud run revisions`)")


def test_le_readme_conditionne_le_passage_a_2_8_a_une_mesure() -> None:
    """AD-13 : « passage à 2/8 seulement après un test mémoire/latence ». La règle vit dans le README.

    Elle n'a aucune surface exécutable — c'est une décision d'exploitation. La seule chose qu'un test
    puisse tenir est qu'elle soit encore écrite là où quelqu'un qui relève les plafonds la lira.
    """
    readme = (WORKFLOWS.parents[1] / "README.md").read_text("utf-8")
    assert "--max-instances=2 --concurrency=8" in readme
    assert "test mémoire/latence" in readme
