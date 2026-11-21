#!/usr/bin/env bash
# Deploy Studio: build once, run the same image as the web service and the background worker.
#
# Not `agents-cli deploy`: that pushes the local .env (sqlite, docker sandbox, auth off) onto the
# service and sets --no-allow-unauthenticated. All runtime settings are Terraform-owned
# (deployment/terraform/single-project); this script only ships code.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to your Google Cloud project id}"
REGION="${REGION:-asia-south1}"
SA="studio-app@${PROJECT}.iam.gserviceaccount.com"
cd "$(dirname "$0")/.."

gcloud run deploy studio --project "$PROJECT" --region "$REGION" --source . \
  --service-account "$SA" --allow-unauthenticated \
  --timeout 3600 --memory 2Gi --cpu 1 --min-instances 0 --max-instances 10 --concurrency 8 --quiet

IMAGE="$(gcloud run services describe studio --project "$PROJECT" --region "$REGION" \
  --format='value(spec.template.spec.containers[0].image)')"
# Pin the worker to the exact digest the service now runs.
DIGEST="$(gcloud artifacts docker images describe "$IMAGE" --format='value(image_summary.fully_qualified_digest)')"
gcloud run jobs update studio-worker --project "$PROJECT" --region "$REGION" --image "$DIGEST" --quiet

echo "deployed: service + worker on $DIGEST"
curl -fsS "https://studio-$(gcloud projects describe "$PROJECT" --format='value(projectNumber)').${REGION}.run.app/health" && echo
