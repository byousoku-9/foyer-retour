#!/usr/bin/env bash
# Bootstrap GCP idempotent pour foyer-retour (story 1.0).
# Re-exécutable sans erreur : chaque étape vérifie l'existant avant de créer.
# Pré-requis : gcloud authentifié, droit propriétaire sur le projet, `.env` avec ANTHROPIC_API_KEY.
# Usage : PDF_LOCAL=/chemin/vers/axa-lu-optihome-2017.pdf bash scripts/gcp_bootstrap.sh
#   (PDF_LOCAL n'est lu que si l'objet gs://foyer-retour-sources/axa-lu-optihome-2017.pdf est absent)
set -euo pipefail

# Constantes volontairement non surchargeables : le projet gcloud par défaut du poste est un autre projet.
PROJECT="foyer-retour"
REGION="europe-west1"
REPO="byousoku-9/foyer-retour"
POOL="github"
PROVIDER="foyer-retour"
DEPLOYER_NAME="deployer"
RUNTIME_NAME="foyer-retour-run"
SOURCES_BUCKET="gs://${PROJECT}-sources"
STAGING_BUCKET="gs://${PROJECT}_cloudbuild"
# Le dépôt de sources de `gcloud run deploy --source` : depuis gcloud 5xx, ce n'est **plus** le
# bucket de staging historique de Cloud Build mais `run-sources-{projet}-{région}`. Le déployeur
# n'y avait aucun droit, et le premier déploiement réel de la story 1.11 a échoué dessus
# (`storage.buckets.get denied`) — le hello-world de 1.0 était parti sous le compte propriétaire,
# qui l'avait créé sans que rien ne le donne au SA déployeur.
RUN_SOURCES_BUCKET="gs://run-sources-${PROJECT}-${REGION}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Chemin local du PDF AXA OptiHome 2017 (non redistribué) : obligatoire, fourni par l'opérateur.
PDF_LOCAL="${PDF_LOCAL:-}"
PDF_SHA256_EXPECTED="6824f9d2bbcb573b0b7c3816ea8a6e5f035b199bd885cf5b777e0978faa4af2c"

G="gcloud --project=${PROJECT} --quiet"
log() { printf '\n== %s\n' "$*"; }
sha256_of() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
# Les liaisons IAM qui suivent la création d'un SA peuvent échouer quelques secondes (propagation) : on réessaie.
retry() { local n=1; until "$@"; do [ $n -ge 5 ] && return 1; sleep $((n * 5)); n=$((n + 1)); done; }
present() { printf '   déjà présent : %s\n' "$*"; }

ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
[ -n "${ACCOUNT}" ] || { echo "gcloud non authentifié : lancer 'gcloud auth login'" >&2; exit 1; }
echo "compte gcloud : ${ACCOUNT} ; projet : ${PROJECT}"

PROJECT_NUMBER="$($G projects describe "${PROJECT}" --format='value(projectNumber)')"
DEPLOY_SA="${DEPLOYER_NAME}@${PROJECT}.iam.gserviceaccount.com"
RUNTIME_SA="${RUNTIME_NAME}@${PROJECT}.iam.gserviceaccount.com"
# Le compte de service sous lequel Cloud Build construit l'image de `--source`. Il est **créé par
# Google** avec le projet, pas par ce script : on le nomme, on ne le fabrique pas. Son existence est
# vérifiée plus bas, avant de lui accorder quoi que ce soit — sans quoi `bind_act_as` partirait en
# création, échouerait cinq fois via `retry` et abandonnerait le bootstrap sur un message gcloud brut.
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

log "APIs"
$G services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com storage.googleapis.com billingbudgets.googleapis.com >/dev/null
echo "   activées"

ensure_sa() { # name display
  if $G iam service-accounts describe "$1@${PROJECT}.iam.gserviceaccount.com" >/dev/null 2>&1; then
    present "$1"
  else
    $G iam service-accounts create "$1" --display-name="$2" >/dev/null
    echo "   créé : $1"
  fi
}

bind_project_role() { # member role
  if $G projects get-iam-policy "${PROJECT}" --flatten='bindings[].members' \
      --filter="bindings.role=$2 AND bindings.members=$1" --format='value(bindings.role)' | grep -q .; then
    present "$1 → $2"
  else
    retry $G projects add-iam-policy-binding "${PROJECT}" --member="$1" --role="$2" >/dev/null
    echo "   lié : $1 → $2"
  fi
}

unbind_project_role() { # member role
  if $G projects get-iam-policy "${PROJECT}" --flatten='bindings[].members' \
      --filter="bindings.role=$2 AND bindings.members=$1" --format='value(bindings.role)' | grep -q .; then
    retry $G projects remove-iam-policy-binding "${PROJECT}" --member="$1" --role="$2" >/dev/null
    echo "   retiré : $1 → $2"
  else
    printf '   déjà absent : %s → %s\n' "$1" "$2"
  fi
}

log "Compte de service déployeur"
ensure_sa "${DEPLOYER_NAME}" "Déploiement Cloud Run depuis GitHub Actions"
# `roles/iam.serviceAccountUser` n'est **pas** dans cette boucle : au niveau projet, il autorise le
# déployeur à agir en tant que **tout** compte de service du projet. L'`actAs` dont
# `gcloud run deploy --source` a besoin est ciblé sur deux comptes nommés — le SA runtime et le SA
# de build —, plus bas (reprise différée de 1.0, tranchée en 1.11 par le déploiement réel : ce sont
# exactement ces deux-là, et pas un de plus, que la commande exige).
# `roles/storage.bucketViewer` = exactement `storage.buckets.get` + `storage.buckets.list`, et rien
# d'autre : aucun accès aux **objets**. `gcloud run deploy --source` énumère les buckets du projet
# pour trouver (ou créer) son dépôt de sources, et cette énumération est une opération de **projet**
# qu'aucune liaison au niveau d'un bucket ne peut satisfaire — mesuré le 24/08/2026, le déploiement
# réel échouait en `storage.buckets.list denied`. L'écriture, elle, reste bornée aux deux buckets.
for role in roles/run.admin roles/cloudbuild.builds.editor \
            roles/artifactregistry.writer roles/serviceusage.serviceUsageConsumer \
            roles/storage.bucketViewer; do
  bind_project_role "serviceAccount:${DEPLOY_SA}" "${role}"
done
# Le retrait du `roles/iam.serviceAccountUser` de projet n'a **pas** lieu ici : il n'est fait qu'une
# fois les **deux** liaisons ciblées posées, plus bas. Le retirer d'abord ouvrait une fenêtre où le
# déployeur n'avait ni le droit large ni ses deux remplaçants — et le contrôle d'existence du compte
# de build, qui sort en 1, tombait précisément dans cette fenêtre : un projet sans API Compute
# repartait avec un déployeur cassé, à réparer à la main (revue 1.11).

# « Agir en tant que » **ce** compte de service, et lui seul — le remplaçant ciblé du
# `roles/iam.serviceAccountUser` au niveau projet, qui autorisait le déployeur à agir en tant que
# n'importe quel SA (story 1.11).
bind_act_as() { # service_account libellé
  if $G iam service-accounts get-iam-policy "$1" --flatten='bindings[].members' \
      --filter="bindings.role=roles/iam.serviceAccountUser AND bindings.members=serviceAccount:${DEPLOY_SA}" \
      --format='value(bindings.role)' | grep -q .; then
    present "actAs $2"
  else
    retry $G iam service-accounts add-iam-policy-binding "$1" \
      --member="serviceAccount:${DEPLOY_SA}" --role=roles/iam.serviceAccountUser >/dev/null
    echo "   lié : déployeur → actAs $2"
  fi
}

log "Compte de service runtime + accès au secret"
ensure_sa "${RUNTIME_NAME}" "Runtime Cloud Run foyer-retour"
# Le déployeur doit pouvoir « agir en tant que » le SA runtime — c'est ce qu'exige `--service-account`.
bind_act_as "${RUNTIME_SA}" "runtime"
# …et en tant que le SA de **build**. `gcloud run deploy --source` fait construire l'image par Cloud
# Build, qui la construit sous le compte de service compute par défaut : sans cet `actAs`, le build
# est refusé (`caller does not have permission to act as service account …`), mesuré le 24/08/2026 au
# premier déploiement réel. Deux liaisons ciblées valent mieux qu'un rôle projet qui les couvre
# toutes les deux **et** toutes les autres.
if ! $G iam service-accounts describe "${BUILD_SA}" >/dev/null 2>&1; then
  echo "   compte de build introuvable : ${BUILD_SA}" >&2
  echo "   c'est le compte de service compute par défaut, créé avec le projet. S'il est absent," >&2
  echo "   activer l'API Compute Engine (\`gcloud services enable compute.googleapis.com --project=${PROJECT}\`)" >&2
  echo "   puis relancer ; s'il est désactivé, le réactiver dans la console IAM. Sans lui," >&2
  echo "   \`gcloud run deploy --source\` n'a aucune identité sous laquelle construire l'image." >&2
  exit 1
fi
bind_act_as "${BUILD_SA}" "build (Cloud Build)"

# **Seulement maintenant.** Les deux remplaçants ciblés sont en place ; le droit large peut partir.
# Le retirer, et pas seulement cesser de le poser : le projet l'a reçu au premier bootstrap, et un
# script qui « ne l'accorde plus » laisserait le droit large en place sans que rien ne le dise.
unbind_project_role "serviceAccount:${DEPLOY_SA}" roles/iam.serviceAccountUser

log "Secret ANTHROPIC_API_KEY"
if $G secrets versions list ANTHROPIC_API_KEY --filter='state=ENABLED' --format='value(name)' 2>/dev/null | grep -q .; then
  present "secret (version ENABLED)"
else
  ENV_FILE="${ROOT}/.env"
  [ -f "${ENV_FILE}" ] || { echo "   .env absent : impossible de créer le secret" >&2; exit 1; }
  # La valeur ne transite jamais par la ligne de commande ni par stdout.
  KEY_VALUE="$(sed -n 's/^ANTHROPIC_API_KEY=//p' "${ENV_FILE}" | head -n1 | tr -d "\r\"'")"
  [ -n "${KEY_VALUE}" ] || { echo "   ANTHROPIC_API_KEY vide dans .env : refus de créer un secret vide" >&2; exit 1; }
  if $G secrets describe ANTHROPIC_API_KEY >/dev/null 2>&1; then
    printf '%s' "${KEY_VALUE}" | $G secrets versions add ANTHROPIC_API_KEY --data-file=- >/dev/null
    echo "   version ajoutée"
  else
    printf '%s' "${KEY_VALUE}" | $G secrets create ANTHROPIC_API_KEY --replication-policy=automatic --data-file=- >/dev/null
    echo "   créé (v1)"
  fi
  unset KEY_VALUE
fi
if $G secrets get-iam-policy ANTHROPIC_API_KEY --flatten='bindings[].members' \
    --filter="bindings.role=roles/secretmanager.secretAccessor AND bindings.members=serviceAccount:${RUNTIME_SA}" \
    --format='value(bindings.role)' | grep -q .; then
  present "secretAccessor runtime"
else
  retry $G secrets add-iam-policy-binding ANTHROPIC_API_KEY \
    --member="serviceAccount:${RUNTIME_SA}" --role=roles/secretmanager.secretAccessor >/dev/null
  echo "   lié : runtime → secretAccessor"
fi

log "Workload Identity Federation (${REPO})"
if $G iam workload-identity-pools describe "${POOL}" --location=global >/dev/null 2>&1; then
  present "pool ${POOL}"
else
  $G iam workload-identity-pools create "${POOL}" --location=global --display-name="GitHub Actions" >/dev/null
  echo "   créé : pool ${POOL}"
fi
PROVIDER_ISSUER="https://token.actions.githubusercontent.com"
PROVIDER_MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.actor=assertion.actor"
PROVIDER_CONDITION="attribute.repository == \"${REPO}\""
if PROVIDER_ACTUEL="$($G iam workload-identity-pools providers describe "${PROVIDER}" \
      --workload-identity-pool="${POOL}" --location=global \
      --format='value(attributeCondition)' 2>/dev/null)"; then
  # « Déjà présent » ne suffisait pas : la condition n'était **jamais** comparée, si bien qu'un
  # changement de `REPO` laissait en place un provider qui autorise l'ancien dépôt et refuse le
  # nouveau — une idempotence de façade (reprise différée de 1.0). On compare, et on met à jour.
  if [ "${PROVIDER_ACTUEL}" = "${PROVIDER_CONDITION}" ]; then
    present "provider ${PROVIDER} (condition à jour)"
  else
    echo "   condition divergente : ${PROVIDER_ACTUEL:-<vide>} ≠ ${PROVIDER_CONDITION}"
    $G iam workload-identity-pools providers update-oidc "${PROVIDER}" \
      --workload-identity-pool="${POOL}" --location=global --display-name="${REPO}" \
      --attribute-mapping="${PROVIDER_MAPPING}" \
      --attribute-condition="${PROVIDER_CONDITION}" >/dev/null
    echo "   mis à jour : provider ${PROVIDER} → ${REPO}"
  fi
else
  $G iam workload-identity-pools providers create-oidc "${PROVIDER}" \
    --workload-identity-pool="${POOL}" --location=global --display-name="${REPO}" \
    --issuer-uri="${PROVIDER_ISSUER}" \
    --attribute-mapping="${PROVIDER_MAPPING}" \
    --attribute-condition="${PROVIDER_CONDITION}" >/dev/null
  echo "   créé : provider ${PROVIDER}"
fi
WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
WIF_MEMBER="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}"
if $G iam service-accounts get-iam-policy "${DEPLOY_SA}" --flatten='bindings[].members' \
    --filter="bindings.role=roles/iam.workloadIdentityUser AND bindings.members=${WIF_MEMBER}" \
    --format='value(bindings.role)' | grep -q .; then
  present "workloadIdentityUser"
else
  retry $G iam service-accounts add-iam-policy-binding "${DEPLOY_SA}" \
    --member="${WIF_MEMBER}" --role=roles/iam.workloadIdentityUser >/dev/null
  echo "   lié : GitHub ${REPO} → déployeur"
fi
# Reste, si `REPO` change : la liaison `workloadIdentityUser` de l'**ancien** principalSet survit sur
# le SA déployeur. Elle est inoffensive tant que la condition ci-dessus refuse les jetons de l'ancien
# dépôt (aucun jeton ne peut plus prendre cette identité), mais elle est à retirer à la main le jour
# d'un renommage — le script ne connaît pas l'ancien nom.

log "Buckets"
ensure_bucket() { # uri
  if $G storage buckets describe "$1" >/dev/null 2>&1; then present "$1"; else
    $G storage buckets create "$1" --location="${REGION}" --uniform-bucket-level-access >/dev/null
    echo "   créé : $1"
  fi
}
ensure_bucket "${STAGING_BUCKET}"
ensure_bucket "${RUN_SOURCES_BUCKET}"
ensure_bucket "${SOURCES_BUCKET}"
bind_bucket_role() { # bucket member role
  # `sed -n '/^{/,$p'` : sur certains postes, `gcloud storage` écrit un avertissement d'environnement
  # (`An error occurred: module 'importlib.metadata' …`, gcloud sur Python 3.9) **sur stdout**, avant
  # le JSON. Sans le filtre, le contrôle échouait à le lire, et le script re-liait un rôle déjà lié à
  # chaque exécution — l'opération est idempotente, mais l'affichage mentait sur ce qu'il faisait.
  if $G storage buckets get-iam-policy "$1" --format=json | sed -n '/^{/,$p' \
      | python3 -c 'import json,sys; r,m=sys.argv[1:]; sys.exit(0 if any(b["role"]==r and m in b.get("members",[]) for b in json.load(sys.stdin).get("bindings",[])) else 1)' "$3" "$2"; then
    present "$1 $2 → $3"
  else
    $G storage buckets add-iam-policy-binding "$1" --member="$2" --role="$3" >/dev/null
    echo "   lié : $1 $2 → $3"
  fi
}
# storage.admin restreint aux deux buckets de dépôt de source (jamais au projet) : le déployeur
# doit y lire la configuration du bucket (`storage.buckets.get`) et y écrire l'archive de la
# source à chaque déploiement.
bind_bucket_role "${STAGING_BUCKET}" "serviceAccount:${DEPLOY_SA}" roles/storage.admin
bind_bucket_role "${RUN_SOURCES_BUCKET}" "serviceAccount:${DEPLOY_SA}" roles/storage.admin
bind_bucket_role "${SOURCES_BUCKET}" "serviceAccount:${DEPLOY_SA}" roles/storage.objectViewer
# Le build Cloud Build (SA compute par défaut) lit le PDF de repli.
bind_bucket_role "${SOURCES_BUCKET}" "serviceAccount:${BUILD_SA}" roles/storage.objectViewer

PDF_OBJECT="${SOURCES_BUCKET}/axa-lu-optihome-2017.pdf"
if $G storage objects describe "${PDF_OBJECT}" >/dev/null 2>&1; then
  present "${PDF_OBJECT}"
elif [ -z "${PDF_LOCAL}" ] || [ ! -f "${PDF_LOCAL}" ]; then
  echo "   objet absent et PDF_LOCAL absent ou introuvable (${PDF_LOCAL:-non défini}) :" >&2
  echo "   relancer avec PDF_LOCAL=/chemin/vers/axa-lu-optihome-2017.pdf" >&2
  exit 1
else
  ACTUAL="$(sha256_of "${PDF_LOCAL}")"
  [ "${ACTUAL}" = "${PDF_SHA256_EXPECTED}" ] || { echo "   sha256 inattendu pour le PDF : ${ACTUAL}" >&2; exit 1; }
  $G storage cp "${PDF_LOCAL}" "${PDF_OBJECT}" >/dev/null
  $G storage ls "${PDF_OBJECT}" >/dev/null 2>&1 || { echo "   dépôt non vérifié : ${PDF_OBJECT} absent" >&2; exit 1; }
  echo "   déposé et vérifié : ${PDF_OBJECT}"
fi

# Le budget du projet : son **montant** est une décision d'exploitation qui a déjà bougé une fois
# (50 → 10 le 23/08/2026, avant de lancer la boucle autonome). Nom et montant sont donc dérivés d'une
# seule variable : un script dont la branche de création écrit un montant que le dépôt contredit
# ailleurs rendrait le bootstrap menteur sur un projet neuf.
BUDGET_AMOUNT="${BUDGET_AMOUNT:-10}"
BUDGET_NAME="${PROJECT}-${BUDGET_AMOUNT}"
log "Budget ${BUDGET_NAME} (alertes 50 % et 100 %) — best-effort"
BILLING_ACCOUNT="$($G billing projects describe "${PROJECT}" --format='value(billingAccountName)' 2>/dev/null | sed 's#^billingAccounts/##' || true)"
if [ -z "${BILLING_ACCOUNT}" ]; then
  echo "   compte de facturation non lisible : budget à créer manuellement dans la console"
# Le témoin est le **préfixe**, et non le nom exact : le montant d'un budget se change en cours de
# route (celui-ci a été abaissé à 10 $ le 23/08/2026 avant de lancer la boucle autonome, et renommé
# `foyer-retour-10` en conséquence). Chercher `foyer-retour-50` faisait alors créer un **second**
# budget à chaque exécution du script — mesuré le 24/08/2026, et supprimé à la main. Un bootstrap
# idempotent ne recrée pas ce qu'il ne reconnaît plus.
elif $G billing budgets list --billing-account="${BILLING_ACCOUNT}" \
      --format='value(displayName)' 2>/dev/null | grep -q "^${PROJECT}-"; then
  present "budget ${PROJECT}-* (montant réglé à la main)"
elif $G billing budgets create --billing-account="${BILLING_ACCOUNT}" --display-name="${BUDGET_NAME}" \
      --budget-amount="${BUDGET_AMOUNT}" --threshold-rule=percent=0.5 --threshold-rule=percent=1.0 \
      --filter-projects="projects/${PROJECT_NUMBER}" >/dev/null 2>&1; then
  echo "   créé : budget ${BUDGET_NAME} (${BUDGET_AMOUNT} dans la devise du compte, alertes 50 % et 100 %)"
else
  echo "   droit facturation manquant : créer le budget à la main (console > Facturation > Budgets," \
       "${BUDGET_AMOUNT} dans la devise du compte, alerte 50 %)"
fi
# Un budget n'**arrête** rien : il alerte. Les plafonds durs sont ailleurs — `--max-instances=1`,
# `--timeout=60` (AD-13), le plafond de coût par requête, et le crédit prépayé Anthropic.

log "Sorties (variables GitHub)"
echo "GCP_PROJECT_ID=${PROJECT}"
echo "WIF_PROVIDER=${WIF_PROVIDER}"
echo "DEPLOY_SA=${DEPLOY_SA}"
echo "RUNTIME_SA=${RUNTIME_SA}"
