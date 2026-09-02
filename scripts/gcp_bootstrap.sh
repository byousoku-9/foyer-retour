#!/usr/bin/env bash
# Bootstrap GCP idempotent pour foyer-retour (story 1.0).
# Re-exécutable sans erreur : chaque étape vérifie l'existant avant de créer.
# Pré-requis : gcloud authentifié, droit propriétaire sur le projet, `.env` avec ANTHROPIC_API_KEY.
# Usage : AXA_PDF_LOCAL=/chemin/axa.pdf BALOISE_PDF_LOCAL=/chemin/baloise.pdf bash scripts/gcp_bootstrap.sh
#   (`PDF_LOCAL` reste accepté comme alias historique de `AXA_PDF_LOCAL`.)
set -euo pipefail

# Constantes volontairement non surchargeables : le projet gcloud par défaut du poste est un autre projet.
PROJECT="foyer-retour"
REGION="europe-west1"
REPO="byousoku-9/foyer-retour"
POOL="github"
PROVIDER="foyer-retour"
DEPLOYER_NAME="deployer"
SOURCE_READER_NAME="source-reader"
RUNTIME_NAME="foyer-retour-run"
SOURCE_WORKFLOW_REF="${REPO}/.github/workflows/ci.yml@refs/heads/main"
DEPLOY_WORKFLOW_REF="${REPO}/.github/workflows/deploy.yml@refs/heads/main"
SOURCES_BUCKET="gs://${PROJECT}-sources"
STAGING_BUCKET="gs://${PROJECT}_cloudbuild"
# Le dépôt de sources de `gcloud run deploy --source` : depuis gcloud 5xx, ce n'est **plus** le
# bucket de staging historique de Cloud Build mais `run-sources-{projet}-{région}`. Le déployeur
# n'y avait aucun droit, et le premier déploiement réel de la story 1.11 a échoué dessus
# (`storage.buckets.get denied`) — le hello-world de 1.0 était parti sous le compte propriétaire,
# qui l'avait créé sans que rien ne le donne au SA déployeur.
RUN_SOURCES_BUCKET="gs://run-sources-${PROJECT}-${REGION}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Chemins locaux des PDF (non redistribués), lus seulement si l'objet correspondant est absent.
AXA_PDF_LOCAL="${AXA_PDF_LOCAL:-${PDF_LOCAL:-}}"
BALOISE_PDF_LOCAL="${BALOISE_PDF_LOCAL:-}"

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
SOURCE_READER_SA="${SOURCE_READER_NAME}@${PROJECT}.iam.gserviceaccount.com"
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
  local state
  if state="$($G iam service-accounts describe "$1@${PROJECT}.iam.gserviceaccount.com" --format=json 2>/dev/null)"; then
    if ! printf '%s' "${state}" | python3 "${ROOT}/scripts/gcp_iam_security.py" sa-active; then
      echo "   compte $1 présent mais désactivé ou illisible — refus de continuer" >&2
      return 1
    fi
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

audit_no_project_roles() { # member
  local roles role
  if ! roles="$($G projects get-iam-policy "${PROJECT}" --flatten='bindings[].members' \
      --filter="bindings.members=$1" --format='value(bindings.role)')"; then
    echo "   audit des rôles projet impossible pour $1 — refus de continuer" >&2
    return 1
  fi
  if [ -z "${roles}" ]; then
    present "$1 sans rôle projet"
    return
  fi
  echo "   rôles projet inattendus pour $1 :" >&2
  for role in ${roles}; do
    echo "   - ${role}" >&2
  done
  echo "   refus de les révoquer automatiquement ; les examiner puis relancer" >&2
  exit 1
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

log "Compte de service lecteur de sources"
ensure_sa "${SOURCE_READER_NAME}" "Lecture CI des PDF sources privés"
# Cette identité ne construit et ne déploie rien. Ses deux seules liaisons attendues sont l'échange
# WIF sur ce compte nommé, puis `storage.objectViewer` sur le bucket nommé plus bas. Un rôle projet
# inattendu bloque le bootstrap et est nommé : un droit manuel n'est jamais révoqué en silence.

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
PROVIDER_ROLE_EXPR="('environment' in assertion && assertion.environment == 'production' && 'workflow_ref' in assertion && assertion.workflow_ref == '${DEPLOY_WORKFLOW_REF}') ? 'deploy' : (('job_workflow_ref' in assertion && assertion.job_workflow_ref == '${SOURCE_WORKFLOW_REF}') ? 'source-reader' : 'none')"
PROVIDER_MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.actor=assertion.actor,attribute.role=${PROVIDER_ROLE_EXPR}"
# **La condition est la frontière d'identité, pas le `if:` du workflow** (revue Codex 1.11). Bornée
# au seul dépôt, elle laissait n'importe quel workflow du dépôt — sur n'importe quelle branche, donc
# poussé sans revue et sans passer par `main` — échanger son jeton OIDC contre l'identité du
# déployeur. La garde `if: github.ref == 'refs/heads/main'` de `deploy.yml` ne protège que
# `deploy.yml` : GCP, lui, n'en sait rien. On borne donc aussi la référence à `main`, le seul
# contexte où AD-12 autorise un déploiement. `assertion.ref` se lit directement dans la condition,
# sans passer par un attribut mappé.
#
# `attribute.role` sépare les deux identités dans le provider partagé. Les claims optionnels sont
# testés avec `in` avant lecture : absence d'environnement ou de workflow appelé ⇒ `none`, que la
# condition refuse. Le déployeur exige l'environnement GitHub `production`; le lecteur exige le
# workflow réutilisable `ci.yml` pris sur `main`.
PROVIDER_BASE_CONDITION="attribute.repository == \"${REPO}\" && assertion.ref == \"refs/heads/main\""
PROVIDER_DEPLOY_CONDITION="${PROVIDER_BASE_CONDITION} && attribute.role == \"deploy\""
PROVIDER_CONDITION="${PROVIDER_BASE_CONDITION} && attribute.role != \"none\""
provider_matches() { # condition attendue ; JSON du describe sur stdin
  python3 "${ROOT}/scripts/gcp_iam_security.py" provider-exact \
    "$1" "${PROVIDER_ROLE_EXPR}" "${PROVIDER_ISSUER}"
}
describe_provider() {
  $G iam workload-identity-pools providers describe "${PROVIDER}" \
    --workload-identity-pool="${POOL}" --location=global --format=json
}
update_provider() { # condition
  $G iam workload-identity-pools providers update-oidc "${PROVIDER}" \
    --workload-identity-pool="${POOL}" --location=global --display-name="${REPO}" \
    --issuer-uri="${PROVIDER_ISSUER}" --attribute-mapping="${PROVIDER_MAPPING}" \
    --attribute-condition="$1" >/dev/null
}
WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
WIF_MEMBER_LEGACY="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}"
WIF_DEPLOY_MEMBER="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.role/deploy"
WIF_SOURCE_READER_MEMBER="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.role/source-reader"
policy_has_wif_binding() { # role member ; policy JSON sur stdin
  python3 "${ROOT}/scripts/gcp_iam_security.py" has-binding "$1" "$2"
}
bind_wif_user() { # service_account member libellé
  local policy status
  if ! policy="$($G iam service-accounts get-iam-policy "$1" --format=json)"; then
    echo "   policy IAM illisible pour $1 — refus de modifier les bindings" >&2
    return 1
  fi
  if printf '%s' "${policy}" | policy_has_wif_binding roles/iam.workloadIdentityUser "$2"; then
    present "workloadIdentityUser $3"
  else
    status=$?
    [ "${status}" -eq 1 ] || { echo "   policy IAM invalide pour $1" >&2; return 1; }
    retry $G iam service-accounts add-iam-policy-binding "$1" \
      --member="$2" --role=roles/iam.workloadIdentityUser >/dev/null
    echo "   lié : GitHub ${REPO} → $3"
  fi
}
unbind_wif_user() { # service_account member libellé
  local policy status
  if ! policy="$($G iam service-accounts get-iam-policy "$1" --format=json)"; then
    echo "   policy IAM illisible pour $1 — refus d'annoncer le binding absent" >&2
    return 1
  fi
  if printf '%s' "${policy}" | policy_has_wif_binding roles/iam.workloadIdentityUser "$2"; then
    retry $G iam service-accounts remove-iam-policy-binding "$1" \
      --member="$2" --role=roles/iam.workloadIdentityUser >/dev/null
    echo "   retiré : $3"
  else
    status=$?
    [ "${status}" -eq 1 ] || { echo "   policy IAM invalide pour $1" >&2; return 1; }
    present "$3 déjà absent"
  fi
}

# Migration en deux phases. Tant que le binding repository historique existe sur le déployeur, le
# provider n'accepte **que** le rôle deploy : un jeton source-reader ne peut donc jamais profiter de
# ce binding large, même si le bootstrap s'interrompt. Le rôle étroit deploy est posé avant retrait
# du legacy; le lecteur et la condition finale ne sont ouverts qu'ensuite.
PROVIDER_FINAL=false
if PROVIDER_ACTUEL="$(describe_provider 2>/dev/null)"; then
  if printf '%s' "${PROVIDER_ACTUEL}" | provider_matches "${PROVIDER_CONDITION}"; then
    PROVIDER_FINAL=true
    present "provider ${PROVIDER} (condition, mapping, issuer et état à jour)"
  else
    status=$?
    [ "${status}" -eq 1 ] || { echo "   JSON du provider invalide — aucune mutation tentée" >&2; exit 1; }
    echo "   provider divergent : passage temporaire deploy-only"
    update_provider "${PROVIDER_DEPLOY_CONDITION}"
  fi
else
  $G iam workload-identity-pools providers create-oidc "${PROVIDER}" \
    --workload-identity-pool="${POOL}" --location=global --display-name="${REPO}" \
    --issuer-uri="${PROVIDER_ISSUER}" --attribute-mapping="${PROVIDER_MAPPING}" \
    --attribute-condition="${PROVIDER_DEPLOY_CONDITION}" >/dev/null
  echo "   créé : provider ${PROVIDER} (deploy-only)"
fi
if [ "${PROVIDER_FINAL}" = false ]; then
  PROVIDER_ACTUEL="$(describe_provider)"
  printf '%s' "${PROVIDER_ACTUEL}" | provider_matches "${PROVIDER_DEPLOY_CONDITION}" || {
    echo "   provider deploy-only non confirmé — refus de poursuivre" >&2
    exit 1
  }
fi
bind_wif_user "${DEPLOY_SA}" "${WIF_DEPLOY_MEMBER}" "déployeur (attribute.role/deploy)"
unbind_wif_user "${DEPLOY_SA}" "${WIF_MEMBER_LEGACY}" "binding repository pool-wide du déployeur"
bind_wif_user "${SOURCE_READER_SA}" "${WIF_SOURCE_READER_MEMBER}" \
  "lecteur de sources (attribute.role/source-reader)"
if [ "${PROVIDER_FINAL}" = false ]; then
  update_provider "${PROVIDER_CONDITION}"
  PROVIDER_ACTUEL="$(describe_provider)"
  printf '%s' "${PROVIDER_ACTUEL}" | provider_matches "${PROVIDER_CONDITION}" || {
    echo "   provider final non confirmé — refus de poursuivre" >&2
    exit 1
  }
  echo "   mis à jour : provider ${PROVIDER} → deploy + source-reader"
fi

audit_sa_wif_policy() { # service_account expected_member
  local policy details
  if ! policy="$($G iam service-accounts get-iam-policy "$1" --format=json)"; then
    echo "   policy du compte lecteur illisible — refus de continuer" >&2
    return 1
  fi
  if details="$(printf '%s' "${policy}" | python3 "${ROOT}/scripts/gcp_iam_security.py" \
      audit-sa-wif "$2")"; then
    present "policy SA du lecteur limitée à workloadIdentityUser étroit"
  else
    echo "   bindings inattendus sur le compte lecteur :" >&2
    if [ -z "${details}" ]; then
      echo "   - binding workloadIdentityUser attendu absent" >&2
    else
      while IFS= read -r detail; do echo "   - ${detail}" >&2; done <<<"${details}"
    fi
    return 1
  fi
}

# Audits post-liaison : aucune réussite ne peut être annoncée sur une policy illisible.
audit_no_project_roles "serviceAccount:${SOURCE_READER_SA}"
audit_sa_wif_policy "${SOURCE_READER_SA}" "${WIF_SOURCE_READER_MEMBER}"

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

source_bucket_security_ok() { # JSON du describe sur stdin
  python3 "${ROOT}/scripts/gcp_bucket_security.py"
}
ensure_source_bucket_security() {
  local state status
  if ! state="$($G storage buckets describe "${SOURCES_BUCKET}" --format=json)"; then
    echo "   configuration du bucket source illisible — refus de continuer" >&2
    return 1
  fi
  if printf '%s' "${state}" | source_bucket_security_ok; then
    present "UBLA actif, Public Access Prevention enforced"
    return
  else
    status=$?
  fi
  if [ "${status}" -ne 1 ]; then
    echo "   JSON du bucket source invalide — aucune mutation tentée" >&2
    return 1
  fi
  if [ "${status}" -eq 1 ]; then
    $G storage buckets update "${SOURCES_BUCKET}" \
      --uniform-bucket-level-access --public-access-prevention >/dev/null
    if ! state="$($G storage buckets describe "${SOURCES_BUCKET}" --format=json)" \
        || ! printf '%s' "${state}" | source_bucket_security_ok; then
      echo "   UBLA/PAP non confirmés après mise à niveau — refus de continuer" >&2
      return 1
    fi
    echo "   mis à niveau : UBLA actif, Public Access Prevention enforced"
  fi
}
ensure_source_bucket_security

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
# Le binding historique du déployeur reste inchangé : cette évolution borne le **nouveau flux CI**,
# elle ne retire aucun droit existant. `ci.yml` n'emploie jamais cette identité pour lire les PDF.
bind_bucket_role "${SOURCES_BUCKET}" "serviceAccount:${DEPLOY_SA}" roles/storage.objectViewer
bind_bucket_role "${SOURCES_BUCKET}" "serviceAccount:${SOURCE_READER_SA}" roles/storage.objectViewer
# Le build Cloud Build (SA compute par défaut) lit le PDF de repli.
bind_bucket_role "${SOURCES_BUCKET}" "serviceAccount:${BUILD_SA}" roles/storage.objectViewer

audit_source_bucket_policy() {
  local policy details
  if ! policy="$($G storage buckets get-iam-policy "${SOURCES_BUCKET}" --format=json | sed -n '/^{/,$p')"; then
    echo "   policy du bucket source illisible — refus de continuer" >&2
    return 1
  fi
  if details="$(printf '%s' "${policy}" | python3 "${ROOT}/scripts/gcp_iam_security.py" \
      audit-source-bucket "serviceAccount:${SOURCE_READER_SA}")"; then
    present "policy source : lecteur objectViewer seul, aucun membre public"
  else
    echo "   policy source inattendue (aucune révocation automatique) :" >&2
    while IFS= read -r detail; do [ -n "${detail}" ] && echo "   - ${detail}" >&2; done <<<"${details}"
    return 1
  fi
}
audit_source_bucket_policy

sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi | cut -d' ' -f1
}

read_committed_source_sha() { # doc_id
  local sha_path="${ROOT}/data/$1/source.sha256" expected
  if [ ! -f "${sha_path}" ] || ! expected="$(sed -n '1{s/[[:space:]]//g;p;}' "${sha_path}")"; then
    echo "   source.sha256 illisible pour $1 — refus de continuer" >&2
    return 1
  fi
  if ! [[ "${expected}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "   source.sha256 invalide pour $1 : ${expected:-<vide>}" >&2
    return 1
  fi
  printf '%s' "${expected}"
}

ensure_source_object() { # doc_id chemin_local nom_variable
  local doc_id="$1" local_path="$2" variable_name="$3" object actual expected names snapshot
  expected="$(read_committed_source_sha "${doc_id}")"
  object="${SOURCES_BUCKET}/${doc_id}.pdf"
  if ! names="$($G storage objects list "${SOURCES_BUCKET}" \
      --filter="name=${doc_id}.pdf" --format='value(name)')"; then
    echo "   inventaire du bucket source impossible — refus de traiter ${object}" >&2
    return 1
  fi
  if [ "${names}" = "${doc_id}.pdf" ]; then
    if ! actual="$($G storage cat "${object}" | sha256_stdin)"; then
      echo "   ${object} existe mais sa lecture a échoué — refus de continuer" >&2
      return 1
    fi
    [ "${actual}" = "${expected}" ] || {
      echo "   ${object} existe mais porte le sha256 ${actual}, attendu ${expected} — refus de l'écraser" >&2
      exit 1
    }
    present "${object} (sha256 vérifié)"
    return
  fi
  # La création conditionnelle tranche sans écraser si l'objet est apparu depuis le `describe` :
  # generation-match=0 échoue alors et le bootstrap s'arrête sans jamais annoncer de conformité.
  if [ -z "${local_path}" ] || [ ! -f "${local_path}" ]; then
    echo "   objet absent ou illisible, et ${variable_name} absent ou introuvable (${local_path:-non défini}) :" >&2
    echo "   relancer avec ${variable_name}=/chemin/vers/${doc_id}.pdf" >&2
    exit 1
  fi
  snapshot="$(mktemp "${TMPDIR:-/tmp}/foyer-retour-source.XXXXXX.pdf")"
  cp -- "${local_path}" "${snapshot}"
  actual="$(sha256_of "${snapshot}")"
  [ "${actual}" = "${expected}" ] || {
    rm -f -- "${snapshot}"
    echo "   sha256 inattendu pour le snapshot de ${local_path} : ${actual} (attendu ${expected})" >&2
    exit 1
  }
  if ! $G storage cp --if-generation-match=0 "${snapshot}" "${object}" >/dev/null; then
    rm -f -- "${snapshot}"
    echo "   création conditionnelle en échec pour ${object} — rien n'est annoncé conforme" >&2
    return 1
  fi
  rm -f -- "${snapshot}"
  if ! actual="$($G storage cat "${object}" | sha256_stdin)"; then
    echo "   objet déposé mais relecture impossible : ${object}" >&2
    return 1
  fi
  [ "${actual}" = "${expected}" ] || {
    echo "   dépôt non vérifié : ${object} porte ${actual}, attendu ${expected}" >&2
    exit 1
  }
  echo "   déposé et vérifié : ${object}"
}

ensure_source_object "axa-lu-optihome-2017" "${AXA_PDF_LOCAL}" "AXA_PDF_LOCAL"
ensure_source_object "baloise-lu-home-2-2024" "${BALOISE_PDF_LOCAL}" "BALOISE_PDF_LOCAL"

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
# `--timeout=120` (AD-13), le plafond de coût par requête, et le crédit prépayé Anthropic.

log "Sorties (variables GitHub)"
echo "GCP_PROJECT_ID=${PROJECT}"
echo "WIF_PROVIDER=${WIF_PROVIDER}"
echo "DEPLOY_SA=${DEPLOY_SA}"
echo "SOURCE_READER_SA=${SOURCE_READER_SA}"
echo "RUNTIME_SA=${RUNTIME_SA}"
