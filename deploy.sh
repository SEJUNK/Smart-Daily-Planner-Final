#!/usr/bin/env bash
# === deploy.sh ===
# Deploy Smart Daily Planner to Google Cloud Run (us-central1).
#
# Prerequisites:
#   1. gcloud CLI installed and authenticated: gcloud auth login
#   2. Set environment variables:
#        export GCP_PROJECT_ID="your-project-id"
#        export GOOGLE_API_KEY="your-gemini-api-key"
#        export GMAIL_USER_EMAIL="you@gmail.com"
#   3. Billing enabled on the GCP project.
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID env var is required}"
GOOGLE_API_KEY="${GOOGLE_API_KEY:?GOOGLE_API_KEY env var is required}"
GMAIL_USER_EMAIL="${GMAIL_USER_EMAIL:?GMAIL_USER_EMAIL env var is required}"
MCP_SERVER_URL="${MCP_SERVER_URL:-}"

SERVICE_NAME="smart-daily-planner"
REGION="us-central1"
SA_NAME="smart-planner-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"
SECRET_NAME="google-api-key"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Smart Daily Planner — Cloud Run Deployment"
echo "  Project : ${PROJECT_ID}"
echo "  Region  : ${REGION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Step 1: Set active project ─────────────────────────────────────────────────
echo "[1/9] Setting active GCP project..."
gcloud config set project "${PROJECT_ID}"

# ── Step 2: Enable required APIs ──────────────────────────────────────────────
echo "[2/9] Enabling required GCP APIs (this may take a few minutes)..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  calendar-json.googleapis.com \
  gmail.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  containerregistry.googleapis.com \
  --project="${PROJECT_ID}"

echo "✅ APIs enabled."

# ── Step 3: Create Firestore database (native mode) ───────────────────────────
echo "[3/9] Ensuring Firestore database exists..."
gcloud firestore databases create \
  --location="${REGION}" \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  Firestore database already exists — skipping."

# ── Step 4: Create service account ────────────────────────────────────────────
echo "[4/9] Creating service account '${SA_NAME}'..."
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="Smart Daily Planner Runtime SA" \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  Service account already exists — skipping."

# ── Step 5: Grant IAM roles ────────────────────────────────────────────────────
echo "[5/9] Granting IAM roles to service account..."
ROLES=(
  "roles/datastore.user"
  "roles/secretmanager.secretAccessor"
  "roles/logging.logWriter"
  "roles/monitoring.metricWriter"
)
for ROLE in "${ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --quiet
  echo "  ✅ Granted ${ROLE}"
done

# ── Step 6: Store API key in Secret Manager ────────────────────────────────────
echo "[6/9] Storing GOOGLE_API_KEY in Secret Manager..."
if gcloud secrets describe "${SECRET_NAME}" --project="${PROJECT_ID}" &>/dev/null; then
  echo "  Secret '${SECRET_NAME}' already exists — adding new version."
  echo -n "${GOOGLE_API_KEY}" | gcloud secrets versions add "${SECRET_NAME}" \
    --data-file=- --project="${PROJECT_ID}"
else
  echo -n "${GOOGLE_API_KEY}" | gcloud secrets create "${SECRET_NAME}" \
    --data-file=- \
    --replication-policy="automatic" \
    --project="${PROJECT_ID}"
fi
echo "✅ Secret stored."

# ── Step 7: Build and push Docker image ────────────────────────────────────────
echo "[7/9] Building and pushing Docker image to Container Registry..."
gcloud builds submit \
  --tag="${IMAGE}" \
  --project="${PROJECT_ID}" \
  --timeout="15m"
echo "✅ Image built: ${IMAGE}"

# ── Step 8: Deploy to Cloud Run ────────────────────────────────────────────────
echo "[8/9] Deploying to Cloud Run in ${REGION}..."
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}" \
  --platform=managed \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --allow-unauthenticated \
  --port=8080 \
  --memory=1Gi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=10 \
  --concurrency=80 \
  --timeout=300 \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GMAIL_USER_EMAIL=${GMAIL_USER_EMAIL},DEFAULT_TIMEZONE=Asia/Kolkata,GEMINI_MODEL=gemini-2.5-flash-preview-04-17,ENABLE_GMAIL_SEND=true,ENABLE_CLOUD_SCHEDULER_MANAGEMENT=true,CLOUD_SCHEDULER_REGION=${REGION},MCP_SERVER_URL=${MCP_SERVER_URL}" \
  --set-secrets="GOOGLE_API_KEY=${SECRET_NAME}:latest" \
  --project="${PROJECT_ID}"

# Get the service URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --platform=managed \
  --region="${REGION}" \
  --format="value(status.url)" \
  --project="${PROJECT_ID}")

echo "✅ Deployed: ${SERVICE_URL}"

# ── Step 9: Set up Cloud Scheduler for daily briefing ─────────────────────────
echo "[9/9] Setting up Cloud Scheduler for daily morning briefing (8:00 AM IST)..."

# Create a Scheduler HTTP job that calls /briefing/scheduled at 8:00 AM IST
gcloud scheduler jobs create http "daily-briefing-${SERVICE_NAME}" \
  --location="${REGION}" \
  --schedule="0 8 * * *" \
  --time-zone="Asia/Kolkata" \
  --uri="${SERVICE_URL}/briefing/scheduled?user_id=default_user" \
  --http-method=POST \
  --headers="Content-Type=application/json" \
  --message-body='{"user_id":"default_user"}' \
  --oidc-service-account-email="${SA_EMAIL}" \
  --project="${PROJECT_ID}" 2>/dev/null || \
  echo "  Scheduler job already exists — skipping creation."

echo "✅ Cloud Scheduler configured."

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🚀 Deployment complete!"
echo ""
echo "  API Base URL : ${SERVICE_URL}"
echo "  Docs         : ${SERVICE_URL}/docs"
echo "  Health       : ${SERVICE_URL}/health"
echo ""
echo "  Test it:"
echo "  curl -X POST ${SERVICE_URL}/query \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"message\":\"Add a task: Review PR by tomorrow\"}'"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
