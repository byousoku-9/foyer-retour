#!/usr/bin/env bash
# Bootstrap GCP idempotent pour foyer-retour (story 1.0).
# Re-exécutable sans erreur : chaque étape vérifie l'existant avant de créer.
# Pré-requis : gcloud authentifié, droit propriétaire sur le projet, `.env` avec ANTHROPIC_API_KEY.
# Usage : bash scripts/gcp_bootstrap.sh
set -euo pipefail

PROJECT="${PROJECT:-foyer-retour}"
REGION="${REGION:-europe-west1}"
REPO="${REPO:-byousoku-9/foyer-retour}"
POOL="github"
PROVIDER="foyer-retour"
DEPLOYER_NAME="deployer"
RUNTIME_NAME="foyer-retour-run"
SOURCES_BUCKET="gs://${PROJECT}-sources"
STAGING_BUCKET="gs://${PROJECT}_cloudbuild"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PDF_LOCAL="${PDF_LOCAL:-${ROOT}/../_reference/cg-pdf/axa-lu-optihome-2017.pdf}"
PDF_SHA256_EXPECTED="6824f9d2bbcb573b0b7c3816ea8a6e5f035b199bd885cf5b777e0978faa4af2c"

G="gcloud --project=${PROJECT} --quiet"
log() { printf '\n== %s\n' "$*"; }
present() { printf '   déjà présent : %s\n' "$*"; }

PROJECT_NUMBER="$($G projects describe "${PROJECT}" --format='value(projectNumber)')"
DEPLOY_SA="${DEPLOYER_NAME}@${PROJECT}.iam.gserviceaccount.com"
RUNTIME_SA="${RUNTIME_NAME}@${PROJECT}.iam.gserviceaccount.com"

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
    $G projects add-iam-policy-binding "${PROJECT}" --member="$1" --role="$2" >/dev/null
    echo "   lié : $1 → $2"
  fi
}

log "Compte de service déployeur"
ensure_sa "${DEPLOYER_NAME}" "Déploiement Cloud Run depuis GitHub Actions"
for role in roles/run.admin roles/iam.serviceAccountUser roles/cloudbuild.builds.editor \
            roles/artifactregistry.writer roles/serviceusage.serviceUsageConsumer; do
  bind_project_role "serviceAccount:${DEPLOY_SA}" "${role}"
done

log "Compte de service runtime + accès au secret"
ensure_sa "${RUNTIME_NAME}" "Runtime Cloud Run foyer-retour"
# Le déployeur doit pouvoir « agir en tant que » le SA runtime.
if $G iam service-accounts get-iam-policy "${RUNTIME_SA}" --flatten='bindings[].members' \
    --filter="bindings.role=roles/iam.serviceAccountUser AND bindings.members=serviceAccount:${DEPLOY_SA}" \
    --format='value(bindings.role)' | grep -q .; then
  present "actAs runtime"
else
  $G iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
    --member="serviceAccount:${DEPLOY_SA}" --role=roles/iam.serviceAccountUser >/dev/null
  echo "   lié : déployeur → actAs runtime"
fi

log "Secret ANTHROPIC_API_KEY"
if $G secrets describe ANTHROPIC_API_KEY >/dev/null 2>&1; then
  present "secret"
else
  ENV_FILE="${ROOT}/.env"
  [ -f "${ENV_FILE}" ] || { echo "   .env absent : impossible de créer le secret" >&2; exit 1; }
  # La valeur ne transite jamais par la ligne de commande ni par stdout.
  sed -n 's/^ANTHROPIC_API_KEY=//p' "${ENV_FILE}" | tr -d '\n' \
    | $G secrets create ANTHROPIC_API_KEY --replication-policy=automatic --data-file=- >/dev/null
  echo "   créé (v1)"
fi
if $G secrets get-iam-policy ANTHROPIC_API_KEY --flatten='bindings[].members' \
    --filter="bindings.role=roles/secretmanager.secretAccessor AND bindings.members=serviceAccount:${RUNTIME_SA}" \
    --format='value(bindings.role)' | grep -q .; then
  present "secretAccessor runtime"
else
  $G secrets add-iam-policy-binding ANTHROPIC_API_KEY \
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
if $G iam workload-identity-pools providers describe "${PROVIDER}" \
    --workload-identity-pool="${POOL}" --location=global >/dev/null 2>&1; then
  present "provider ${PROVIDER}"
else
  $G iam workload-identity-pools providers create-oidc "${PROVIDER}" \
    --workload-identity-pool="${POOL}" --location=global --display-name="${REPO}" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.actor=assertion.actor" \
    --attribute-condition="attribute.repository == \"${REPO}\"" >/dev/null
  echo "   créé : provider ${PROVIDER}"
fi
WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
WIF_MEMBER="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}"
if $G iam service-accounts get-iam-policy "${DEPLOY_SA}" --flatten='bindings[].members' \
    --filter="bindings.role=roles/iam.workloadIdentityUser AND bindings.members=${WIF_MEMBER}" \
    --format='value(bindings.role)' | grep -q .; then
  present "workloadIdentityUser"
else
  $G iam service-accounts add-iam-policy-binding "${DEPLOY_SA}" \
    --member="${WIF_MEMBER}" --role=roles/iam.workloadIdentityUser >/dev/null
  echo "   lié : GitHub ${REPO} → déployeur"
fi

log "Buckets"
ensure_bucket() { # uri
  if $G storage buckets describe "$1" >/dev/null 2>&1; then present "$1"; else
    $G storage buckets create "$1" --location="${REGION}" --uniform-bucket-level-access >/dev/null
    echo "   créé : $1"
  fi
}
ensure_bucket "${STAGING_BUCKET}"
ensure_bucket "${SOURCES_BUCKET}"
bind_bucket_role() { # bucket member role
  if $G storage buckets get-iam-policy "$1" --format=json \
      | python3 -c 'import json,sys; r,m=sys.argv[1:]; sys.exit(0 if any(b["role"]==r and m in b.get("members",[]) for b in json.load(sys.stdin).get("bindings",[])) else 1)' "$3" "$2"; then
    present "$1 $2 → $3"
  else
    $G storage buckets add-iam-policy-binding "$1" --member="$2" --role="$3" >/dev/null
    echo "   lié : $1 $2 → $3"
  fi
}
# storage.admin restreint au bucket de staging Cloud Build (jamais au projet).
bind_bucket_role "${STAGING_BUCKET}" "serviceAccount:${DEPLOY_SA}" roles/storage.admin
bind_bucket_role "${SOURCES_BUCKET}" "serviceAccount:${DEPLOY_SA}" roles/storage.objectViewer
# Le build Cloud Build (SA compute par défaut) lit le PDF de repli.
bind_bucket_role "${SOURCES_BUCKET}" "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" roles/storage.objectViewer

PDF_OBJECT="${SOURCES_BUCKET}/axa-lu-optihome-2017.pdf"
if $G storage objects describe "${PDF_OBJECT}" >/dev/null 2>&1; then
  present "${PDF_OBJECT}"
elif [ -f "${PDF_LOCAL}" ]; then
  ACTUAL="$(shasum -a 256 "${PDF_LOCAL}" | cut -d' ' -f1)"
  [ "${ACTUAL}" = "${PDF_SHA256_EXPECTED}" ] || { echo "   sha256 inattendu pour le PDF : ${ACTUAL}" >&2; exit 1; }
  $G storage cp "${PDF_LOCAL}" "${PDF_OBJECT}" >/dev/null
  echo "   déposé : ${PDF_OBJECT}"
else
  echo "   PDF local absent (${PDF_LOCAL}) : dépôt à faire à la main"
fi

log "Budget (alerte 50 %) — best-effort"
BILLING_ACCOUNT="$($G billing projects describe "${PROJECT}" --format='value(billingAccountName)' 2>/dev/null | sed 's#^billingAccounts/##' || true)"
if [ -z "${BILLING_ACCOUNT}" ]; then
  echo "   compte de facturation non lisible : budget à créer manuellement dans la console"
elif $G billing budgets list --billing-account="${BILLING_ACCOUNT}" --format='value(displayName)' 2>/dev/null | grep -qx "foyer-retour-50"; then
  present "budget foyer-retour-50"
elif $G billing budgets create --billing-account="${BILLING_ACCOUNT}" --display-name="foyer-retour-50" \
      --budget-amount=50 --threshold-rule=percent=0.5 --threshold-rule=percent=1.0 \
      --filter-projects="projects/${PROJECT_NUMBER}" >/dev/null 2>&1; then
  echo "   créé : budget foyer-retour-50 (50 dans la devise du compte, alertes 50 % et 100 %)"
else
  echo "   droit facturation manquant : créer le budget à la main (console > Facturation > Budgets, alerte 50 %)"
fi

log "Sorties (variables GitHub)"
echo "GCP_PROJECT_ID=${PROJECT}"
echo "WIF_PROVIDER=${WIF_PROVIDER}"
echo "DEPLOY_SA=${DEPLOY_SA}"
echo "RUNTIME_SA=${RUNTIME_SA}"
