#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Standalone Deployment Template for ANY Cloud Run Streaming ADK Agent
# ==============================================================================

PROJECT_ID="${PROJECT_ID:-ceo-dev123}"
REGION="${REGION:-us-central1}"
REPOSITORY="${REPOSITORY:-ceosystem}"

# Set the name of your agent and its local source directory
AGENT_NAME="${1:-my-streaming-agent}"
AGENT_DIR="${2:-./}"
MODEL_NAME="${3:-gemini-3.8-flash}"

echo "================================================================="
echo " Deploying Cloud Run Streaming ADK Agent"
echo " Agent Name : ${AGENT_NAME}"
echo " Source Dir : ${AGENT_DIR}"
echo " Model      : ${MODEL_NAME}"
echo " Project ID : ${PROJECT_ID}"
echo " Region     : ${REGION}"
echo "================================================================="

gcloud config set project "${PROJECT_ID}"

TAG="$(date +%Y%m%d%H%M%S)"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${AGENT_NAME}:${TAG}"
LATEST_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${AGENT_NAME}:latest"

echo "--> [1/2] Building Container Image in Artifact Registry..."
gcloud builds submit "${AGENT_DIR}" \
  --config=<(cat <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ["build", "-t", "${IMAGE_URI}", "-t", "${LATEST_URI}", "."]
images:
- "${IMAGE_URI}"
- "${LATEST_URI}"
EOF
) --project="${PROJECT_ID}"

echo "--> [2/2] Deploying to Cloud Run with Production Sizing..."
gcloud run deploy "${AGENT_NAME}" \
  --image="${IMAGE_URI}" \
  --region="${REGION}" \
  --platform=managed \
  --allow-unauthenticated \
  --cpu=2 \
  --memory=2Gi \
  --concurrency=80 \
  --min-instances=1 \
  --max-instances=30 \
  --timeout=300 \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=1,PROJECT_ID=${PROJECT_ID},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},MAXIMA_MODEL=${MODEL_NAME}" \
  --project="${PROJECT_ID}"

SERVICE_URL="$(gcloud run services describe "${AGENT_NAME}" --region="${REGION}" --format="value(status.url)" --project="${PROJECT_ID}")"

echo "================================================================="
echo " Agent Deployed Successfully to Cloud Run!"
echo " Service URL: ${SERVICE_URL}"
echo ""
echo " Now register this agent in config/agents.prod.yaml:"
echo "---------------------------------------------------"
echo "${AGENT_NAME}:"
echo "  agent_id: ${AGENT_NAME}"
echo "  backend: cloud_run_adk"
echo "  model: ${MODEL_NAME}"
echo "  base_url: ${SERVICE_URL}"
echo "  app_name: app"
echo "  region: ${REGION}"
echo "  streaming_enabled: true"
echo "  persistence_enabled: true"
echo "  auth_policy: firebase"
echo "================================================================="
