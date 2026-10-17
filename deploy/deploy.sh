#!/usr/bin/env bash
# Deploy osrs-mcp to Cloud Run. Idempotent: every step checks before it creates.
#
#   deploy/deploy.sh --dry-run   # print what would happen, change nothing
#   deploy/deploy.sh             # do it
#
# Reads non-secret settings and two secrets (GOOGLE_CLIENT_SECRET, INGEST_TOKEN) from the local
# .env. JWT_SIGNING_KEY and STORAGE_ENCRYPTION_KEY are generated fresh for the cloud the first
# time. Secret values are never printed. Existing secrets are never overwritten; to rotate one,
# add a new version with `gcloud secrets versions add` and redeploy.
#
# Env vars and secrets are applied with --update-* (merge), so anything set on the service by
# hand survives a redeploy.
set -euo pipefail

PROJECT="${PROJECT:-your-gcp-project}"
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-osrs-mcp}"
REPO="${REPO:-osrs-mcp}"
SA_NAME="${SA_NAME:-osrs-mcp-run}"
# Public address: a Cloud Run domain mapping for DOMAIN (a whole hostname), with the app
# itself serving everything under PREFIX. DOMAIN= (empty) uses the bare run.app URL instead.
DOMAIN="${DOMAIN-rs.example.com}"
PREFIX="${PREFIX-/osrs}"
ENV_FILE="${ENV_FILE:-.env}"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

cd "$(dirname "$0")/.."

# Use the POSIX `gcloud` script, also present in Git Bash on Windows. Never gcloud.cmd: it
# mangles any argument containing a space ("'C:\Program' is not recognized").
GCLOUD=gcloud
g() { "$GCLOUD" "$@" --project "$PROJECT" --quiet; }

log() { printf '\n==> %s\n' "$*"; }
run() {
  # Print a mutating command; run it unless --dry-run. Never pass secret values through here.
  printf '  $ gcloud %s\n' "$*"
  if [[ $DRY_RUN -eq 0 ]]; then g "$@"; fi
  return 0
}

env_value() {
  # The value of KEY in $ENV_FILE ("" if absent). Must return 0: used in $(...) under set -e.
  local line
  line="$(grep -E "^$1=" "$ENV_FILE" | tail -n 1 || true)"
  printf '%s' "${line#*=}"
  return 0
}

require() {
  local missing=()
  for key in "$@"; do [[ -n "$(env_value "$key")" ]] || missing+=("$key"); done
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Missing in $ENV_FILE: ${missing[*]}" >&2
    exit 1
  fi
  return 0
}

# --- preflight --------------------------------------------------------------------------------

[[ -f "$ENV_FILE" ]] || { echo "No $ENV_FILE" >&2; exit 1; }
require GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET ALLOWED_EMAIL DEFAULT_RSN CONTACT GIM_GROUP INGEST_TOKEN

if [[ -n "$(git status --porcelain -- Dockerfile pyproject.toml uv.lock .python-version src)" ]]; then
  echo "Uncommitted changes to build inputs; commit first so the image tag names a real commit." >&2
  exit 1
fi
COMMIT="$(git rev-parse --short=12 HEAD)"

PROJECT_NUMBER="$(g projects describe "$PROJECT" --format='value(projectNumber)')"
RUN_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
if [[ -n "$DOMAIN" ]]; then PUBLIC_URL="https://${DOMAIN}${PREFIX}"; else PUBLIC_URL="$RUN_URL"; PREFIX=""; fi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${COMMIT}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

log "Project $PROJECT ($PROJECT_NUMBER), region $REGION, service $SERVICE"
echo "  public URL: $PUBLIC_URL   (run.app: $RUN_URL)"
echo "  image: $IMAGE"
[[ $DRY_RUN -eq 1 ]] && echo "  DRY RUN: nothing will be changed"

# --- APIs -------------------------------------------------------------------------------------

log "APIs"
APIS=(run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
      secretmanager.googleapis.com firestore.googleapis.com)
ENABLED="$(g services list --enabled --format='value(config.name)')"
TO_ENABLE=()
for api in "${APIS[@]}"; do grep -qx "$api" <<<"$ENABLED" || TO_ENABLE+=("$api"); done
if [[ ${#TO_ENABLE[@]} -gt 0 ]]; then run services enable "${TO_ENABLE[@]}"; else echo "  all enabled"; fi

# APIs we just enabled can't be queried in a dry run.
if [[ $DRY_RUN -eq 1 && ${#TO_ENABLE[@]} -gt 0 ]]; then
  log "Dry run stops here: the remaining checks need the APIs above enabled."
  exit 0
fi

# --- Firestore (PERMANENT: location and mode can't be changed later) ---------------------------

log "Firestore (default) database"
if g firestore databases describe --database='(default)' --format='value(locationId)' >/dev/null 2>&1; then
  echo "  exists in $(g firestore databases describe --database='(default)' --format='value(locationId)')"
else
  run firestore databases create --database='(default)' --location="$REGION" --type=firestore-native
fi

# --- Artifact Registry --------------------------------------------------------------------------

log "Artifact Registry repository"
if g artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1; then
  echo "  exists"
else
  run artifacts repositories create "$REPO" --location="$REGION" --repository-format=docker \
    --description="osrs-mcp images"
fi

# --- service account ----------------------------------------------------------------------------

log "Service account $SA"
if g iam service-accounts describe "$SA" >/dev/null 2>&1; then
  echo "  exists"
else
  run iam service-accounts create "$SA_NAME" --display-name="osrs-mcp Cloud Run"
fi
# Firestore read/write only. (The binding is idempotent.)
run projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" \
  --role=roles/datastore.user --condition=None --format=none

# --- secrets ------------------------------------------------------------------------------------

create_secret() {
  # create_secret NAME VALUE: create once, never overwrite; grant only this service account.
  local name="$1" value="$2"
  if g secrets describe "$name" >/dev/null 2>&1; then
    echo "  $name exists (left unchanged)"
  else
    printf '  creating %s\n' "$name"
    if [[ $DRY_RUN -eq 0 ]]; then
      printf '%s' "$value" | g secrets create "$name" --replication-policy=automatic --data-file=-
    fi
  fi
  run secrets add-iam-policy-binding "$name" --member="serviceAccount:$SA" \
    --role=roles/secretmanager.secretAccessor --format=none
  return 0
}

new_key() { python -c "import secrets; print(secrets.token_urlsafe(48), end='')"; return 0; }
new_fernet() {
  python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode(), end='')"
  return 0
}

log "Secrets"
create_secret osrs-mcp-google-client-secret "$(env_value GOOGLE_CLIENT_SECRET)"
create_secret osrs-mcp-ingest-token "$(env_value INGEST_TOKEN)"
create_secret osrs-mcp-jwt-signing-key "$(new_key)"
create_secret osrs-mcp-storage-encryption-key "$(new_fernet)"

# --- build --------------------------------------------------------------------------------------

log "Build $IMAGE"
if g artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  echo "  already built for this commit"
else
  run builds submit --tag "$IMAGE" .
fi

# --- deploy -------------------------------------------------------------------------------------

log "Deploy"
ENV_VARS=(
  "AUTH_MODE=google"
  "PUBLIC_BASE_URL=$PUBLIC_URL"
  "STORE_BACKEND=firestore"
  "GOOGLE_CLIENT_ID=$(env_value GOOGLE_CLIENT_ID)"
  "ALLOWED_EMAIL=$(env_value ALLOWED_EMAIL)"
  "DEFAULT_RSN=$(env_value DEFAULT_RSN)"
  "DEFAULT_ACCOUNT_TYPE=$(env_value DEFAULT_ACCOUNT_TYPE)"
  "CONTACT=$(env_value CONTACT)"
  "GIM_GROUP=$(env_value GIM_GROUP)"
)
# "^##^" makes ## the list separator (gcloud topic escaping), so values may contain commas and
# the @ of email addresses.
ENV_ARG="^##^$(printf '%s##' "${ENV_VARS[@]}")"
ENV_ARG="${ENV_ARG%##}"
SECRETS_ARG="GOOGLE_CLIENT_SECRET=osrs-mcp-google-client-secret:latest,\
INGEST_TOKEN=osrs-mcp-ingest-token:latest,\
JWT_SIGNING_KEY=osrs-mcp-jwt-signing-key:latest,\
STORAGE_ENCRYPTION_KEY=osrs-mcp-storage-encryption-key:latest"

run run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" \
  --service-account="$SA" --allow-unauthenticated \
  --min-instances=0 --max-instances=1 --cpu=1 --memory=512Mi --timeout=300 \
  --update-env-vars="$ENV_ARG" --update-secrets="$SECRETS_ARG"

# --- custom domain ------------------------------------------------------------------------------

if [[ -n "$DOMAIN" ]]; then
  log "Domain mapping $DOMAIN"
  if g beta run domain-mappings describe --domain="$DOMAIN" --region="$REGION" >/dev/null 2>&1; then
    echo "  exists"
  else
    run beta run domain-mappings create --service="$SERVICE" --domain="$DOMAIN" --region="$REGION"
  fi
  if [[ $DRY_RUN -eq 0 ]]; then
    echo "  DNS records it needs (add them at your DNS host, unproxied):"
    g beta run domain-mappings describe --domain="$DOMAIN" --region="$REGION"       --format='table[box](status.resourceRecords[].name,status.resourceRecords[].type,status.resourceRecords[].rrdata)'
  fi
fi

[[ $DRY_RUN -eq 1 ]] && { log "Dry run complete."; exit 0; }

# --- verify the shape, not the exit code --------------------------------------------------------

log "Verify"
"$GCLOUD" run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --format='yaml(spec.template.spec.serviceAccountName,spec.template.spec.containers[].image,status.url,status.latestReadyRevisionName)'
echo "  containers: $("$GCLOUD" run services describe "$SERVICE" --region="$REGION" \
  --project="$PROJECT" --format='value(spec.template.spec.containers.len())')"
echo "  env var names:"
"$GCLOUD" run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --format='value(spec.template.spec.containers[0].env[].name)' | tr ';' '\n' | sed 's/^/    /'
CHECK="${RUN_URL}${PREFIX}"
echo "  $CHECK/health: $(curl -s -o /dev/null -w "%{http_code}" "$CHECK/health")"
echo "  /mcp without token: $(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{}' "$CHECK/mcp") (expect 401)"
echo "  protected resource: $(curl -s "$RUN_URL/.well-known/oauth-protected-resource${PREFIX}/mcp")"
log "Done. Connector URL: $PUBLIC_URL/mcp"
echo "  Google OAuth redirect URI: $PUBLIC_URL/auth/callback"
echo "  RuneLite plugin base URL:  $PUBLIC_URL"
