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

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
    assert doc["jobs"]["deployer"]["environment"] == "production", (
        "le claim OIDC environment=production distingue le déployeur du lecteur")


def test_le_deploiement_demande_le_jeton_oidc() -> None:
    """Sans `id-token: write`, `auth@v3` ne peut pas fédérer — et il faudrait une clé dans le dépôt."""
    assert lire(DEPLOY)["permissions"]["id-token"] == "write"


def test_le_lint_et_la_suite_gardent_la_porte_avant_tout_deploiement() -> None:
    jobs = lire(DEPLOY)["jobs"]
    assert jobs["verifier"]["uses"] == "./.github/workflows/ci.yml", (
        "le job de vérification réutilise `ci.yml` : un seul texte dit ce que « vert » veut dire")
    assert jobs["verifier"]["with"]["sources_reelles"] == "${{ github.ref == 'refs/heads/main' }}", (
        "seule main demande la variante réelle ; un workflow_dispatch hors main reste hermétique")
    assert jobs["verifier"]["permissions"]["id-token"] == "write"
    assert jobs["deployer"]["needs"] == "verifier"


def test_les_versions_dactions_sont_celles_du_spine() -> None:
    """Stack figée : `auth@v3`, `deploy-cloudrun@v3` (AD-12, table Stack)."""
    uses = [e.get("uses", "") for e in etapes(lire(DEPLOY), "deployer")]
    auth_sha = "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093"
    assert auth_sha in uses
    assert auth_sha in [e.get("uses", "") for e in etapes(lire(CI), "verifier")]
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
    # Story 4.5 (revue B2) : le service porte la révision **complète**. `/api/v1/sante` continue de
    # publier le `sha7` qu'AD-11 promet, mais comme **projection** de celle-ci — une seule source de
    # vérité. Un `GIT_SHA` court rendait la comparaison de gate incapable de distinguer deux commits
    # partageant sept caractères.
    assert "GIT_SHA=${{ steps.sha.outputs.sha40 }}" in avec["env_vars"]
    assert "GIT_SHA=${{ steps.sha.outputs.sha7 }}" not in avec["env_vars"]
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
    """L'AC écrit « smoke tests sur `steps.deploy.outputs.url` », et cette sortie **est** l'URL taguée.

    `deploy-cloudrun@v3` publie `status.url` — l'URL du service — *sauf* quand l'entrée `tag` est
    posée : `parseDeployResponse` cherche alors l'entrée de `status.traffic[]` qui porte ce tag et
    publie son `url` (`src/output-parser.ts`, v3). Le workflow résolvait l'URL de son côté et sondait
    la sienne ; c'est la sortie de l'action qui fait autorité (revue Codex 1.11). Sonder l'URL du
    **service** mesurerait l'ancienne révision, puisque la candidate est déployée sans trafic.
    """
    pas = etapes(lire(DEPLOY), "deployer")
    smoke = pas[index_de(pas, "scripts/smoke.py")]
    assert smoke["env"]["URL"] == "${{ steps.deploy.outputs.url }}", (
        "l'AC nomme `steps.deploy.outputs.url` ; avec `tag`, c'est l'URL de la révision candidate")
    assert smoke["env"]["SHA7"] == "${{ steps.sha.outputs.sha7 }}"
    resolution = pas[index_de(pas, "status.traffic")]
    assert resolution["id"] == "candidate"
    assert "gcloud run services describe" in resolution["run"]


def test_lurl_resolue_et_celle_de_laction_sont_confrontees_avant_promotion() -> None:
    """Deux façons de nommer la révision candidate ne doivent pas pouvoir diverger en silence.

    Le smoke sonde l'URL publiée par l'action ; la promotion nomme la révision lue par `describe`.
    Si un `gcloud run deploy --tag=candidat` lancé à la main déplaçait le tag entre les deux appels,
    on promouvrait une révision que personne n'a sondée — la promotion « quand même » qu'AD-16
    interdit. L'étape de résolution compare donc les deux URL et échoue **avant** le smoke.
    """
    pas = etapes(lire(DEPLOY), "deployer")
    resolution = pas[index_de(pas, "status.traffic")]
    assert resolution["env"]["URL_ACTION"] == "${{ steps.deploy.outputs.url }}"
    assert '"$url" != "$URL_ACTION"' in resolution["run"], (
        "les deux résolutions de la révision candidate doivent être confrontées")
    assert index_de(pas, "status.traffic") < index_de(pas, "scripts/smoke.py")


def test_le_projet_deploye_est_celui_que_lac_nomme() -> None:
    """AC 1.11 : `project_id: foyer-retour` ; FR44 a : « toujours `--project=foyer-retour` ».

    Le projet vient d'une variable du dépôt — éditable depuis l'interface de GitHub, sans revue et
    sans diff. « Non vide » ne suffit donc pas : une valeur erronée ferait construire et déployer
    ailleurs. Le nom autoritaire est écrit une fois dans le workflow et confronté à la variable
    **avant** l'authentification (revue Codex 1.11).
    """
    pas = etapes(lire(DEPLOY), "deployer")
    controle = pas[_index_par_nom(pas, "Contrôler les variables")]
    assert controle["env"]["PROJET_ATTENDU"] == "foyer-retour"
    assert '"$GCP_PROJECT_ID" != "$PROJET_ATTENDU"' in controle["run"]
    assert _index_par_nom(pas, "Contrôler les variables") < index_de(pas, "google-github-actions/auth")


def test_luv_est_installe_avant_que_le_credentiel_federe_existe() -> None:
    """`curl | sh` n'a rien à voler tant qu'`auth@v3` n'a pas écrit `gha-creds-*.json`.

    L'installateur d'uv est un script téléchargé et exécuté. Joué après l'authentification, il
    tournerait sur un runner portant le crédentiel fédéré du déployeur — une compromission de cette
    ressource distante suffirait alors à agir avec ses droits (revue Codex 1.11). L'ordre est la
    seule protection possible ici, et il n'a aucun coût : rien de ce que ces deux étapes font ne
    dépend de GCP.
    """
    pas = etapes(lire(DEPLOY), "deployer")
    assert index_de(pas, "astral.sh/uv") < index_de(pas, "google-github-actions/auth")
    assert index_de(pas, "uv sync --frozen") < index_de(pas, "google-github-actions/auth")


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


def test_la_ci_lance_quick_sur_pr_full_sur_main_et_resume_en_markdown() -> None:
    """4.1 : le workflow réutilisé choisit le profil par événement et garde le coût explicite."""
    doc = lire(CI)
    assert declencheurs(doc)["workflow_call"]["secrets"]["ANTHROPIC_API_KEY"]["required"] is False
    pas = etapes(doc, "verifier")
    evals = pas[_index_par_nom(pas, "Questions-témoins quick")]
    assert evals["if"] == "env.EVALS_API_KEY != ''"
    assert evals["env"]["EVALS_QUICK"] == "${{ github.ref == 'refs/heads/main' && '0' || '1' }}"
    assert "pytest -m evals" in evals["run"] and "--evals-max-cost" in evals["run"]
    assert 'cat .evals/results.md >> "$GITHUB_STEP_SUMMARY"' in evals["run"]
    # Story 4.5, B7 : la disposition de publication des sorties de run est posée par la CI, avant
    # le run, parce que `.evals/` est ignoré par git et que la bascule n'installe jamais ses cibles.
    # La ligne épinglée ci-dessus est inchangée : `cat` suit un lien symbolique.
    assert "python -m server.evals.espace" in evals["run"]
    assert "--cible .evals/results.md" in evals["run"]
    assert evals["run"].index("server.evals.espace") < evals["run"].index("pytest -m evals"), \
        "la disposition se pose avant le run, jamais pendant une bascule"
    restauration = pas[index_de(pas, "actions/cache/restore@v4")]
    sauvegarde = pas[index_de(pas, "actions/cache/save@v4")]
    assert restauration["id"] == "evals_cache"
    assert restauration["with"]["path"] == sauvegarde["with"]["path"] == ".evals/cache"
    assert restauration["with"]["key"] == sauvegarde["with"]["key"]
    assert "always()" in sauvegarde["if"] and "outcome == 'success'" in sauvegarde["if"]
    assert "cache-hit != 'true'" in sauvegarde["if"]
    saut = pas[_index_par_nom(pas, "Questions-témoins non exécutées")]
    assert saut["if"] == "env.EVALS_API_KEY == ''" and "ignorées explicitement" in saut["run"]
    assert lire(DEPLOY)["jobs"]["verifier"]["secrets"] == "inherit"


def test_la_ci_annule_seulement_la_campagne_perimee_dune_pr() -> None:
    concurrence = lire(CI)["concurrency"]
    assert "pull_request.number" in concurrence["group"]
    assert concurrence["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


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

    settings = Settings(_env_file=None)
    drapeaux = deploiement()["flags"]
    valeurs = re.findall(r"--timeout=([0-9]+)", drapeaux)
    assert len(valeurs) == 1, f"un seul `--timeout` attendu dans les drapeaux : {valeurs}"
    # **Strictement** supérieur (tour « budgets Sonnet », 02/09/2026) : `>=` laissait passer
    # l'égalité, où une requête honorée à la milliseconde près est coupée par l'infrastructure. La
    # marge que le navigateur s'ajoute (`client_abort_margin_s`) n'entre pas dans cette comparaison :
    # elle rend le client plus patient que le serveur, elle ne demande rien à Cloud Run.
    assert float(valeurs[0]) > settings.deadline_s, (
        f"--timeout={valeurs[0]} s ne laisse pas au serveur ses {settings.deadline_s} s : à égalité "
        "Cloud Run couperait une requête que le pipeline honore encore, et le 503 viendrait de "
        "l'infrastructure au lieu de la seule autorité qui sait pourquoi (AD-16)")


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
    for secret in (".env", ".env.*", "*.key", "service-account*.json", "gha-creds-*.json"):
        assert secret in motifs, f"`{secret}` doit rester exclu du téléversement de `--source`"
    # …et rien ne doit les réadmettre par une négation.
    assert not any(m.startswith("!") and "env" in m and m != "!.env.example" for m in motifs)
    # La méthode de travail ne descend jamais dans le dépôt public, et a fortiori pas dans un bucket.
    for interne in ("_bmad", "_bmad-output", "_reference", ".claude"):
        assert interne in motifs


def test_le_credentiel_federe_ne_part_ni_dans_le_bucket_ni_dans_limage() -> None:
    """`auth@v3` dépose `gha-creds-<aléa>.json` **dans le répertoire de travail**, et avant le déploiement.

    `create_credentials_file` vaut vrai par défaut : le fichier existe donc sur le disque du runner
    quand `deploy-cloudrun --source` archive le dossier. Sans exclusion, ce crédentiel fédéré — qui
    vaut l'identité du déployeur pour la durée du job — partait dans `gs://run-sources-…` puis dans
    le contexte de build (revue Codex 1.11, reproduit par `gcloud meta list-files-for-upload`, qui
    listait bien un `gha-creds-*.json` posé sur le poste). Les trois filtres le disent maintenant :
    `.gcloudignore` (ce qui quitte le poste), `.dockerignore` (ce qui entre dans l'image) et
    `.gitignore` (ce qui entre dans l'historique public).
    """
    racine = WORKFLOWS.parents[1]
    for nom in (".gcloudignore", ".dockerignore", ".gitignore"):
        motifs = {ligne.strip() for ligne in (racine / nom).read_text("utf-8").splitlines()
                  if ligne.strip() and not ligne.lstrip().startswith("#")}
        assert "gha-creds-*.json" in motifs, (
            f"`{nom}` doit exclure le crédentiel écrit par `auth@v3` dans le répertoire de travail")


def _chemins_admis_gitignore(fichier: Path, relatifs: set[str]) -> set[str]:
    """Délègue la sémantique gitignore de gcloud au moteur Git, négations comprises."""
    with tempfile.TemporaryDirectory(prefix="foyer-retour-gitignore-") as temporaire:
        depot = Path(temporaire)
        subprocess.run(["git", "init", "-q"], cwd=depot, check=True)
        (depot / ".gitignore").write_bytes(fichier.read_bytes())
        entree = "\0".join(sorted(relatifs)) + "\0"
        resultat = subprocess.run(
            ["git", "check-ignore", "--no-index", "-z", "--stdin"], cwd=depot,
            input=entree, capture_output=True, text=True, check=False,
        )
        assert resultat.returncode in (0, 1), resultat.stderr
        exclus = {p for p in resultat.stdout.split("\0") if p}
    return relatifs - exclus


def _motif_moby_regex(motif: str) -> re.Pattern[str]:
    """Compile les constructions Moby présentes dans ce `.dockerignore`.

    Moby nettoie et ancre les motifs à la racine ; `*` ne traverse pas `/`, tandis que `**/`
    représente zéro ou plusieurs répertoires. Le fichier ne contient volontairement ni classe de
    caractères ni échappement : les refuser empêche cet oracle ciblé d'inventer une sémantique.
    """
    motif = motif.strip("/")
    assert "[" not in motif and "\\" not in motif, f"construction Moby non couverte : {motif}"
    morceaux: list[str] = ["^"]
    i = 0
    while i < len(motif):
        if motif.startswith("**/", i):
            morceaux.append("(?:.*/)?")
            i += 3
        elif motif.startswith("**", i):
            morceaux.append(".*")
            i += 2
        elif motif[i] == "*":
            morceaux.append("[^/]*")
            i += 1
        elif motif[i] == "?":
            morceaux.append("[^/]")
            i += 1
        else:
            morceaux.append(re.escape(motif[i]))
            i += 1
    morceaux.append("$")
    return re.compile("".join(morceaux))


def _chemin_admis_moby(fichier: Path, relatif: str) -> bool:
    """Applique l'ordre Moby, y compris la propagation d'un match depuis un répertoire parent."""
    ignore = False
    parents = ["/".join(relatif.split("/")[:i]) for i in range(1, relatif.count("/") + 1)]
    for brute in fichier.read_text("utf-8").splitlines():
        motif = brute.strip()
        if not motif or motif.startswith("#") or motif == ".":
            continue
        negation = motif.startswith("!")
        motif = motif[1:] if negation else motif
        compile = _motif_moby_regex(motif)
        if compile.fullmatch(relatif) or any(compile.fullmatch(parent) for parent in parents):
            ignore = not negation
    return not ignore


def _chemins_admis(fichier: Path, relatifs: set[str]) -> set[str]:
    if fichier.name == ".dockerignore":
        return {relatif for relatif in relatifs if _chemin_admis_moby(fichier, relatif)}
    return _chemins_admis_gitignore(fichier, relatifs)


def test_les_entrees_du_dockerfile_survivent_aux_deux_frontieres() -> None:
    racine = WORKFLOWS.parents[1]
    dockerfile = (racine / "Dockerfile").read_text("utf-8")
    sources = {motif for ligne in dockerfile.splitlines()
               if ligne.startswith("COPY ") and not ligne.startswith("COPY --")
               for motif in ligne.split()[1:-1]}
    necessaires = {"Dockerfile", "pyproject.toml", "uv.lock", "server", "data", "web", "tools"}
    assert necessaires - {"Dockerfile"} <= sources, "toutes les sources COPY doivent être sondées"
    assert all((racine / chemin).exists() for chemin in necessaires)
    for nom in (".gcloudignore", ".dockerignore"):
        admis = _chemins_admis(racine / nom, necessaires)
        assert admis == necessaires, f"{nom} retire des entrées requises par le Dockerfile"


def test_les_pdf_sont_exclus_generiquement_et_les_liens_du_checkout_restent_committes() -> None:
    """Les contextes excluent les PDF ; Git garde les liens déjà suivis, sans liste de documents."""
    racine = WORKFLOWS.parents[1]
    manifest = json.loads((racine / "data" / "manifest.json").read_text("utf-8"))
    references = {p.parent.name for motif in ("source.sha256", "source.js")
                  for p in (racine / "data").glob(f"*/{motif}")}
    attendus = sorted(f"data/{doc_id}/source.pdf" for doc_id in set(manifest) | references)
    liens = sorted(p.relative_to(racine).as_posix()
                   for p in (racine / "data").glob("*/source.pdf") if p.is_symlink())
    assert liens == attendus, "chaque document ou référence doit porter son lien statique"
    exclus = [
        "data/.publie/a/data/document-prive/source.pdf",
        "data/.publie/b/data/autre-document/source.pdf",
        "data/.publie/.verrou",
        "data/.publie/a/data/doc/.source.pdf.telechargement.tmp",
    ]
    for nom in (".dockerignore", ".gcloudignore", ".gitignore"):
        ignore = racine / nom
        assert "data/*/source.pdf" in ignore.read_text("utf-8"), nom
        sondes = set(liens) | set(exclus)
        assert not (_chemins_admis(ignore, sondes) & sondes), nom
    suivis = subprocess.run(
        ["git", "ls-files", "--", *liens], cwd=racine, check=True,
        capture_output=True, text=True).stdout.splitlines()
    assert sorted(suivis) == liens, "les liens racine du checkout doivent rester committés"


def _copier_contexte_filtre(racine: Path, destination: Path, ignore: Path) -> None:
    sources = [source for dossier in (racine / "server", racine / "data")
               for source in (dossier, *dossier.rglob("*"))
               if not (source.is_dir() and not source.is_symlink())]
    relatifs = {source.relative_to(racine).as_posix() for source in sources}
    admis = _chemins_admis(ignore, relatifs)
    for source in sources:
        relatif = source.relative_to(racine).as_posix()
        if relatif not in admis:
            continue
        cible = destination / relatif
        cible.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            os.symlink(os.readlink(source), cible)
        else:
            shutil.copy2(source, cible)


def _octets_des_generations(racine: Path) -> dict[str, bytes]:
    espace = racine / "data" / ".publie"
    return {
        fichier.relative_to(espace).as_posix(): fichier.read_bytes()
        for generation in espace.iterdir()
        if generation.is_dir() and not generation.is_symlink()
        for fichier in generation.rglob("*")
        if fichier.is_file() and not fichier.is_symlink()
    }


def _poser_disposition(contexte: Path) -> subprocess.CompletedProcess[str]:
    environnement = os.environ.copy()
    environnement["PYTHONPATH"] = str(contexte)
    return subprocess.run(
        [sys.executable, "-m", "server.evals.espace", "--racine", str(contexte),
         "--data-dir", str(contexte / "data"), "--depot", "--migrer"],
        cwd=contexte, env=environnement, capture_output=True, text=True, check=False)


def test_le_dockerfile_repose_vraiment_la_disposition_du_contexte_filtre_avant_le_batch(
        tmp_path: Path) -> None:
    racine = WORKFLOWS.parents[1]
    dockerfile = (racine / "Dockerfile").read_text("utf-8")
    pose = "python -m server.evals.espace --racine . --data-dir data --depot --migrer"
    fetch = "python -m server.ingest.fetch_source --all"
    assert pose in dockerfile and dockerfile.index(pose) < dockerfile.index(fetch)
    doc_ids = set(json.loads((racine / "data" / "manifest.json").read_text("utf-8")))
    for fichier in (".dockerignore", ".gcloudignore", ".gitignore", "Dockerfile"):
        contenu = (racine / fichier).read_text("utf-8")
        for doc_id in doc_ids:
            contextes_nomines = (
                f"data/{doc_id}/", f"fetch_source {doc_id}", f'fetch_source "{doc_id}"')
            assert not any(fragment in contenu for fragment in contextes_nomines), (
                f"{fichier} ne doit coder aucun chemin ou argument documentaire")

    contexte = tmp_path / "contexte"
    _copier_contexte_filtre(racine, contexte, racine / ".dockerignore")
    assert not list((contexte / "data").glob("*/source.pdf"))
    assert not list((contexte / "data" / ".publie").glob("*/data/*/source.pdf"))
    resultat = _poser_disposition(contexte)
    assert resultat.returncode == 0, resultat.stdout + resultat.stderr

    from server.app.corpus.racine import lecture_de

    with lecture_de(contexte / "data") as lecture:
        lecture.verifier()
    assert all((contexte / "data" / doc_id / "source.pdf").is_symlink()
               for doc_id in doc_ids)
    assert not list((contexte / "data" / ".publie").glob("*/data/*/source.pdf")), (
        "la pose hors réseau ne doit inventer aucun octet PDF privé")


@pytest.mark.parametrize("archive_materialisee", [False, True], ids=["checkout", "gcloud"])
def test_gcloud_et_docker_excluent_le_pointeur_reconstructible_sans_perdre_les_generations(
        tmp_path: Path, archive_materialisee: bool) -> None:
    """Le pointeur n'est jamais un payload ; les générations sont le payload à préserver.

    Le second cas reproduit la forme observée dans Cloud Build : `courant` arrive comme un
    répertoire ordinaire. Le filtre Docker doit le retirer avant la pose, sans demander à l'API
    opérateur de supprimer une entrée existante d'un type inattendu.
    """
    racine = WORKFLOWS.parents[1]
    generations = {p.name for p in (racine / "data" / ".publie").iterdir()
                   if p.is_dir() and not p.is_symlink()}
    pointeur_checkout = racine / "data" / ".publie" / "courant"
    assert pointeur_checkout.is_symlink() and os.readlink(pointeur_checkout) in generations
    entree_git = subprocess.run(
        ["git", "ls-files", "-s", "--", "data/.publie/courant"], cwd=racine,
        check=True, capture_output=True, text=True).stdout
    assert entree_git.startswith("120000 "), "le checkout autoritaire doit porter un blob symlink"
    for ignore in (racine / ".gcloudignore", racine / ".dockerignore"):
        chemins = {"data/.publie/courant"} | {
            f"data/.publie/{generation}" for generation in generations
        }
        admis = _chemins_admis(ignore, chemins)
        assert "data/.publie/courant" not in admis
        assert chemins - {"data/.publie/courant"} <= admis

    source = tmp_path / "archive-source"
    for dossier in ("server", "data"):
        shutil.copytree(racine / dossier, source / dossier, symlinks=True)

    generation_sonde = sorted(generations)[0]
    racine_docs = source / "data" / ".publie" / generation_sonde / "docs"
    fichiers_interdits = (
        ".env", ".env.production", "secret.key", "service-account-build.json",
        "gha-creds-ephemere.json", "source.pdf", "brouillon.tmp", ".verrou",
        "type-clauses.lock", "module.pyc",
    )
    dossiers_interdits = (
        ".git", "_bmad", "_bmad-output", "_reference", ".claude", ".agents", "tests",
        "scripts", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
        ".mypy_cache", "node_modules",
    )
    sentinelles = []
    for profondeur in ((), ("niveau-1",), ("niveau-1", "niveau-2")):
        for nom in fichiers_interdits:
            sentinelle = racine_docs.joinpath(*profondeur, nom)
            sentinelle.parent.mkdir(parents=True, exist_ok=True)
            sentinelle.write_bytes(b"ne doit jamais entrer dans le contexte")
            sentinelles.append(sentinelle.relative_to(source).as_posix())
        for nom in dossiers_interdits:
            sentinelle = racine_docs.joinpath(*profondeur, nom, "temoin.txt")
            sentinelle.parent.mkdir(parents=True, exist_ok=True)
            sentinelle.write_bytes(b"repertoire interne interdit")
            sentinelles.append(sentinelle.relative_to(source).as_posix())
    doc_necessaire = racine_docs / "niveau-1" / "niveau-2" / "preuve.json"
    doc_necessaire.write_bytes(b'{"publie": true}\n')
    relatif_necessaire = doc_necessaire.relative_to(source).as_posix()
    documents_publies = sorted(
        chemin for chemin in (
            source / "data" / ".publie" / generation_sonde / "data"
        ).iterdir()
        if chemin.is_dir()
    )
    assert documents_publies, "la génération sondée doit contenir au moins un document"
    pdf_authentifie = documents_publies[0] / "source.pdf"
    pdf_authentifie.write_bytes(b"%PDF-1.7\nsource authentifiee temporaire\n%%EOF\n")
    relatif_pdf_authentifie = pdf_authentifie.relative_to(source).as_posix()

    for ignore in (racine / ".gcloudignore", racine / ".dockerignore"):
        admis = _chemins_admis(
            ignore,
            set(sentinelles) | {relatif_necessaire, relatif_pdf_authentifie},
        )
        assert not (admis & set(sentinelles)), ignore
        assert relatif_pdf_authentifie not in admis, (
            f"{ignore} doit retirer une source authentifiée déjà téléchargée"
        )
        assert relatif_necessaire in admis, f"{ignore} doit conserver les docs publiés non sensibles"
        sans_readmission = tmp_path / f"sans-readmission-{ignore.name.removeprefix('.')}" / ignore.name
        sans_readmission.parent.mkdir()
        sans_readmission.write_text("\n".join(
            ligne for ligne in ignore.read_text("utf-8").splitlines()
            if not ligne.strip().startswith("!")
        ) + "\n", "utf-8")
        assert relatif_necessaire not in _chemins_admis(sans_readmission, {relatif_necessaire}), (
            f"les négations de {ignore} doivent rester nécessaires à la conservation des docs sûrs")

    attendu = {
        chemin: octets for chemin, octets in _octets_des_generations(source).items()
        if not chemin.endswith("/source.pdf")
        and f"data/.publie/{chemin}" not in sentinelles
    }
    assert attendu, "le bundle publié doit contenir des octets dont on prouve la conservation"
    if archive_materialisee:
        pointeur = source / "data" / ".publie" / "courant"
        cible = os.readlink(pointeur)
        pointeur.unlink()
        shutil.copytree(pointeur.parent / cible, pointeur, symlinks=True)
        assert pointeur.is_dir() and not pointeur.is_symlink()
        filtre = racine / ".dockerignore"
    else:
        filtre = racine / ".gcloudignore"

    contexte = tmp_path / "contexte-pose"
    _copier_contexte_filtre(source, contexte, filtre)
    pointeur_filtre = contexte / "data" / ".publie" / "courant"
    assert not pointeur_filtre.exists() and not pointeur_filtre.is_symlink()
    assert _octets_des_generations(contexte) == attendu
    assert (contexte / relatif_necessaire).read_bytes() == b'{"publie": true}\n'
    assert all(not (contexte / chemin).exists() for chemin in sentinelles)

    resultat = _poser_disposition(contexte)
    assert resultat.returncode == 0, resultat.stdout + resultat.stderr
    assert pointeur_filtre.is_symlink()
    assert os.readlink(pointeur_filtre) in generations
    assert _octets_des_generations(contexte) == attendu
    assert not [pdf for pdf in (contexte / "data" / ".publie").rglob("*.pdf")
                if pdf.is_file() and not pdf.is_symlink()], (
        "la pose hors réseau ne doit matérialiser aucun PDF")


# --- `scripts/gcp_bootstrap.sh` : la frontière d'identité, côté GCP -------------------------------

def test_la_condition_du_provider_wif_borne_le_depot_et_la_branche() -> None:
    """Le `if:` d'un workflow n'est pas une frontière IAM (revue Codex 1.11).

    La condition du provider était `attribute.repository == "byousoku-9/foyer-retour"` seule :
    **n'importe quel** workflow du dépôt, sur **n'importe quelle** branche, pouvait échanger son
    jeton OIDC contre l'identité du déployeur — il suffisait de pousser un fichier dans
    `.github/workflows/` sur une branche de travail, sans revue et sans passer par `main`. La garde
    `if: github.ref == 'refs/heads/main'` du job ne protège que `deploy.yml` lui-même ; elle ne dit
    rien à GCP. C'est la condition d'attribut qui doit le dire, et elle exige donc aussi
    `assertion.ref == "refs/heads/main"` — le seul contexte où AD-12 autorise un déploiement.
    """
    script = (WORKFLOWS.parents[1] / "scripts" / "gcp_bootstrap.sh").read_text("utf-8")
    base = re.search(r'^PROVIDER_BASE_CONDITION="(.+)"$', script, re.M)
    assert base, "la frontière dépôt + branche doit rester relisible du script"
    texte_condition = base.group(1)
    assert r'attribute.repository == \"${REPO}\"' in texte_condition, "le dépôt reste borné"
    assert r'assertion.ref == \"refs/heads/main\"' in texte_condition, (
        "la branche doit être bornée côté IAM, pas seulement par le `if:` du workflow")
    assert " && " in texte_condition, "les deux bornes valent ensemble, pas au choix"
    # …et le mapping doit exposer ce que la condition lit, sinon `update-oidc` la refuse.
    assert "attribute.repository=assertion.repository" in script
    assert "attribute.role=${PROVIDER_ROLE_EXPR}" in script
    assert "'environment' in assertion" in script
    assert "assertion.environment == 'production'" in script
    assert "'workflow_ref' in assertion" in script
    assert "${DEPLOY_WORKFLOW_REF}" in script
    assert "'job_workflow_ref' in assertion" in script
    assert "${SOURCE_WORKFLOW_REF}" in script
    assert "? 'source-reader' : 'none'" in script
    assert 'PROVIDER_DEPLOY_CONDITION="${PROVIDER_BASE_CONDITION} && attribute.role == \\"deploy\\""' in script
    assert 'PROVIDER_CONDITION="${PROVIDER_BASE_CONDITION} && attribute.role != \\"none\\""' in script


def test_la_migration_wif_refuse_le_lecteur_avant_de_retirer_le_binding_large() -> None:
    script = (WORKFLOWS.parents[1] / "scripts" / "gcp_bootstrap.sh").read_text("utf-8")
    deploy = 'bind_wif_user "${DEPLOY_SA}" "${WIF_DEPLOY_MEMBER}"'
    reader = 'bind_wif_user "${SOURCE_READER_SA}" "${WIF_SOURCE_READER_MEMBER}"'
    retire = 'unbind_wif_user "${DEPLOY_SA}" "${WIF_MEMBER_LEGACY}"'
    deploy_only = 'update_provider "${PROVIDER_DEPLOY_CONDITION}"'
    final = 'update_provider "${PROVIDER_CONDITION}"'
    assert 0 <= script.index(deploy_only) < script.index(deploy) < script.index(retire)
    assert script.index(retire) < script.index(reader) < script.index(final)
    assert "attribute.repository/${REPO}" in script
    assert "attribute.role/deploy" in script and "attribute.role/source-reader" in script
    assert "remove-iam-policy-binding" in script
    assert not re.search(
        r'^bind_wif_user "\$\{DEPLOY_SA\}" "\$\{WIF_MEMBER_LEGACY\}"', script, re.M
    )
    assert "condition, mapping, issuer et état à jour" in script and "provider_matches" in script


def test_le_lecteur_de_sources_est_distinct_et_sans_role_projet() -> None:
    """Le jeton qui prouve les PDF ne doit jamais porter les pouvoirs du déployeur."""
    script = (WORKFLOWS.parents[1] / "scripts" / "gcp_bootstrap.sh").read_text("utf-8")
    assert 'SOURCE_READER_NAME="source-reader"' in script
    assert 'SOURCE_READER_SA="${SOURCE_READER_NAME}@${PROJECT}.iam.gserviceaccount.com"' in script
    assert 'ensure_sa "${SOURCE_READER_NAME}"' in script
    assert 'audit_no_project_roles "serviceAccount:${SOURCE_READER_SA}"' in script
    assert "rôles projet inattendus" in script
    assert "refus de les révoquer automatiquement" in script
    assert 'bind_wif_user "${SOURCE_READER_SA}" "${WIF_SOURCE_READER_MEMBER}"' in script
    assert ('bind_bucket_role "${SOURCES_BUCKET}" "serviceAccount:${SOURCE_READER_SA}" '
            'roles/storage.objectViewer') in script
    assert 'bind_project_role "serviceAccount:${SOURCE_READER_SA}"' not in script
    assert ('bind_bucket_role "${SOURCES_BUCKET}" "serviceAccount:${DEPLOY_SA}" '
            'roles/storage.objectViewer') in script
    assert 'unbind_bucket_role "${SOURCES_BUCKET}" "serviceAccount:${DEPLOY_SA}"' not in script
    assert 'SOURCE_READER_SA=${SOURCE_READER_SA}' in script
    assert "audit_sa_wif_policy" in script and "audit_source_bucket_policy" in script
    assert "audit des rôles projet impossible" in script
    audit = script[script.index("audit_no_project_roles()"):
                   script.index('log "Compte de service déployeur"')]
    assert "|| true" not in audit
    helper = (WORKFLOWS.parents[1] / "scripts" / "gcp_iam_security.py").read_text("utf-8")
    assert "allUsers" in helper and "allAuthenticatedUsers" in helper
    assert "--uniform-bucket-level-access" in script
    assert "--public-access-prevention" in script
    assert "--public-access-prevention=enforced" not in script
    assert 'python3 "${ROOT}/scripts/gcp_bucket_security.py"' in script


def test_le_bootstrap_verifie_les_deux_objets_prives_par_leur_sha() -> None:
    script = (WORKFLOWS.parents[1] / "scripts" / "gcp_bootstrap.sh").read_text("utf-8")
    documents_prives = re.findall(r'^ensure_source_object "([^"]+)" ', script, re.MULTILINE)
    assert len(documents_prives) == len(set(documents_prives)) == 2
    for doc_id in documents_prives:
        assert f'ensure_source_object "{doc_id}"' in script
        reference = WORKFLOWS.parents[1] / "data" / doc_id / "source.sha256"
        assert reference.is_file()
        sha = reference.read_text("utf-8").strip()
        assert sha not in script, "le hash committé ne doit pas avoir une seconde autorité dans le shell"
    assert 'read_committed_source_sha "${doc_id}"' in script
    assert 'data/$1/source.sha256' in script
    assert "storage cp --if-generation-match=0" in script
    assert script.index('storage objects list "${SOURCES_BUCKET}"') < script.index(
        'storage cat "${object}" | sha256_stdin'
    )
    assert "inventaire du bucket source impossible" in script
    assert "existe mais sa lecture a échoué" in script
    assert 'storage cat "${object}" | sha256_stdin' in script, (
        "un objet déjà présent doit être vérifié, pas seulement déclaré présent")
    assert 'mktemp "${TMPDIR:-/tmp}/foyer-retour-source.XXXXXX.pdf"' in script
    assert 'storage cp --if-generation-match=0 "${snapshot}"' in script


# --- `ci.yml` ------------------------------------------------------------------------------------

def test_la_ci_se_declenche_sur_les_pull_requests() -> None:
    """FR44 d : « `ci.yml` sur PR = tests sans déploiement »."""
    on = declencheurs(lire(CI))
    assert "pull_request" in on
    assert "workflow_call" in on, "appelée aussi par `deploy.yml` : un seul texte pour deux portes"
    assert "push" not in on
    entree = on["workflow_call"]["inputs"]["sources_reelles"]
    assert entree == {"description": "Régénérer les artefacts depuis les PDF authentifiés avant les tests",
                      "required": False, "default": False, "type": "boolean"}


def test_la_ci_ne_deploie_rien() -> None:
    """Une pull request ne touche jamais le service (AD-12)."""
    contenu = texte(CI)
    for interdit in ("deploy-cloudrun", "update-traffic"):
        assert interdit not in contenu


def test_la_pr_est_hermetique_et_lidentite_nexiste_que_pour_les_sources_reelles() -> None:
    pas = etapes(lire(CI), "verifier")
    controle = pas[_index_par_nom(pas, "Contrôler l'identité")]
    auth = pas[index_de(pas, "google-github-actions/auth")]
    fetch = pas[index_de(pas, "fetch_source --all")]
    assert controle["if"] == "inputs.sources_reelles"
    assert controle["env"] == {
        "WIF_PROVIDER": "${{ vars.WIF_PROVIDER }}",
        "SOURCE_READER_SA": "${{ vars.SOURCE_READER_SA }}",
        "WIF_PROVIDER_ATTENDU": (
            "projects/1061254857807/locations/global/workloadIdentityPools/github/providers/foyer-retour"
        ),
        "SOURCE_READER_SA_ATTENDU": "source-reader@foyer-retour.iam.gserviceaccount.com",
    }
    assert '"$WIF_PROVIDER" = "$WIF_PROVIDER_ATTENDU"' in controle["run"]
    assert '"$SOURCE_READER_SA" = "$SOURCE_READER_SA_ATTENDU"' in controle["run"]
    assert _index_par_nom(pas, "Contrôler l'identité") < index_de(pas, "google-github-actions/auth")
    assert auth["if"] == "inputs.sources_reelles"
    assert fetch["if"] == "inputs.sources_reelles"
    assert auth["with"] == {
        "workload_identity_provider": "${{ vars.WIF_PROVIDER }}",
        "service_account": "${{ vars.SOURCE_READER_SA }}",
        "token_format": "access_token",
        "access_token_lifetime": "300s",
        "access_token_scopes": "https://www.googleapis.com/auth/devstorage.read_only",
        "create_credentials_file": False,
    }
    assert fetch["env"] == {
        "GOOGLE_OAUTH_ACCESS_TOKEN": "${{ steps.auth_sources.outputs.access_token }}",
    }, "le jeton court ne doit être transmis qu'au processus qui lit le bucket privé"
    assert all("GOOGLE_OAUTH_ACCESS_TOKEN" not in e.get("env", {}) for e in pas if e is not fetch)
    assert "DEPLOY_SA" not in texte(CI), "la vérification réelle ne doit jamais emprunter le déployeur"


def test_main_lit_le_bucket_prive_et_joue_les_deux_preuves_pdf_nommees() -> None:
    pas = etapes(lire(CI), "verifier")
    fetch = pas[index_de(pas, "fetch_source --all")]
    assert fetch["if"] == "inputs.sources_reelles"
    assert "--all --private-source" in fetch["run"]
    preuve = pas[index_de(pas, "test_real_pdf_regenerates_committed_artefacts")]
    assert preuve["if"] == "inputs.sources_reelles"
    assert preuve["env"] == {"REAL_PDF_TESTS_REQUIRED": "1"}
    nodeids = (
        "tests/test_parsing_axa.py::test_real_pdf_regenerates_committed_artefacts",
        "tests/test_parsing_baloise.py::test_real_baloise_pdf_regenerates_the_committed_structural_identity",
    )
    assert all(nodeid in preuve["run"] for nodeid in nodeids)
    preuve_i = index_de(pas, nodeids[0])
    assert index_de(pas, "fetch_source --all") < preuve_i < _index_par_nom(pas, "Tests unitaires")
    assert all("REAL_PDF_TESTS_REQUIRED" not in e.get("env", {}) for e in pas if e is not preuve)


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
    tests = pas[_index_par_nom(pas, "Tests unitaires")]
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


def test_la_ci_authentifie_et_telecharge_les_sources_reelles_avant_de_tester() -> None:
    """Sinon la CI joue une suite plus faible que celle que le dépôt annonce.

    `test_real_pdf_regenerates_committed_artefacts` est gardé par un `skipif` sur la présence de
    le `source.pdf` privé, qui n'existe jamais sur un runner : le seul test qui prouve que les
    artefacts committés correspondent encore au parseur sautait **en silence**,
    dans la CI qui garde la production. Le `skipif` est évalué à la collecte : le téléchargement doit
    donc être une étape à part, avant `pytest`, et non un `fixture`.
    """
    pas = etapes(lire(CI), "verifier")
    assert index_de(pas, "google-github-actions/auth") < index_de(pas, "fetch_source --all") < index_de(pas, "pytest")


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


def test_la_ci_reste_un_diagnostic_full_sans_gate_ni_repetition() -> None:
    """AC 4.5 : « la CI telle qu'elle est configurée … le comportement est celui d'avant le diff ».

    Le contrôle est double, et les deux moitiés se tiennent :

    1. le **workflow** lance bien `EVALS_PROFILE: full` **sans** `--gate` ni `--repeat` — c'est un
       diagnostic, pas un gate, et c'est pourquoi les exigences de `full` s'arment sur `--gate` et
       non sur le profil (Design Notes 4.5). L'inverse aurait rendu la CI rouge à chaque PR pour une
       raison étrangère au candidat ;
    2. l'**adaptateur d'arguments** (`tests/test_evals_live.py::arguments_evals`) ne compose ni
       `--gate`, ni `--repeat`, ni `--candidate-revision` : aucune garde neuve ne peut s'y déclencher.
    """
    from tests.test_evals_live import arguments_evals

    pas = etapes(lire(CI), "verifier")
    evals = pas[_index_par_nom(pas, "Questions-témoins quick")]
    assert evals["env"]["EVALS_PROFILE"] == "full"
    assert "--gate" not in evals["run"] and "--repeat" not in evals["run"]
    assert "--candidate-revision" not in evals["run"]

    args, _json, _md = arguments_evals(0.5, Path("/tmp/inexistant-pour-le-test"))
    assert "--profile" in args and args[args.index("--profile") + 1] == "full"
    for neuf in ("--gate", "--repeat", "--candidate-revision", "--orchestrator-evidence",
                 "--orchestrator-report"):
        assert neuf not in args, f"la CI ne doit pas composer {neuf}"
