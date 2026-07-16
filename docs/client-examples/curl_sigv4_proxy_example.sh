#!/bin/bash
# Example: call the GCO API Gateway through aws-sigv4-proxy.
#
# Requirements: AWS CLI, aws-sigv4-proxy, curl, jq, and (optionally) lsof.
# The proxy uses the normal AWS credential provider chain, so AWS_PROFILE,
# temporary session credentials, SSO, web identity, and IAM roles are supported.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/../.." && pwd)

for command_name in aws aws-sigv4-proxy curl jq; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Error: required command '$command_name' is not installed" >&2
    exit 1
  fi
done

context_value() {
  local jq_filter=$1
  local fallback=$2
  if [[ -f "${PROJECT_ROOT}/cdk.json" ]]; then
    jq -er "${jq_filter} // empty" "${PROJECT_ROOT}/cdk.json" 2>/dev/null || printf '%s\n' "$fallback"
  else
    printf '%s\n' "$fallback"
  fi
}

API_REGION=${API_REGION:-$(context_value '.context.deployment_regions.api_gateway' 'us-east-2')}
PROJECT_NAME=${PROJECT_NAME:-$(context_value '.context.project_name' 'gco')}
STACK_NAME=${STACK_NAME:-${PROJECT_NAME}-api-gateway}
PROXY_PORT=${PROXY_PORT:-8080}

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}=== GCO API Gateway - aws-sigv4-proxy examples ===${NC}\n"
aws sts get-caller-identity >/dev/null

# shellcheck disable=SC2016
API_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$API_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiEndpoint`].OutputValue' \
  --output text)
API_ENDPOINT=${API_ENDPOINT%/}

if [[ -z "$API_ENDPOINT" || "$API_ENDPOINT" == "None" ]]; then
  echo -e "${RED}Error: ApiEndpoint was not found in stack ${STACK_NAME}${NC}" >&2
  exit 1
fi

API_WITHOUT_SCHEME=${API_ENDPOINT#*://}
API_HOST=${API_WITHOUT_SCHEME%%/*}
API_STAGE_PATH=${API_WITHOUT_SCHEME#"$API_HOST"}
LOCAL_API_BASE="http://localhost:${PROXY_PORT}${API_STAGE_PATH}"

echo "API endpoint: ${API_ENDPOINT}"
echo "Signing region: ${API_REGION}"

if command -v lsof >/dev/null 2>&1 && lsof -Pi :"$PROXY_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo -e "${RED}Error: port ${PROXY_PORT} is already in use; choose another PROXY_PORT${NC}" >&2
  exit 1
fi

PROXY_PID=""
PAYLOAD_FILE=$(mktemp "${TMPDIR:-/tmp}/gco-manifest.XXXXXX")
cleanup() {
  rm -f "$PAYLOAD_FILE"
  if [[ -n "$PROXY_PID" ]]; then
    echo -e "\n${GREEN}Stopping aws-sigv4-proxy...${NC}"
    kill "$PROXY_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo -e "${GREEN}Starting aws-sigv4-proxy on port ${PROXY_PORT}...${NC}"
aws-sigv4-proxy \
  --name execute-api \
  --region "$API_REGION" \
  --port "$PROXY_PORT" \
  --upstream-url-scheme https \
  --log-level info &
PROXY_PID=$!
sleep 2
if ! kill -0 "$PROXY_PID" 2>/dev/null; then
  echo -e "${RED}Error: aws-sigv4-proxy failed to start${NC}" >&2
  exit 1
fi

perform_request() {
  local response
  response=$(curl -sS "$@" -w $'\nHTTP_STATUS:%{http_code}')
  HTTP_STATUS=${response##*$'\nHTTP_STATUS:'}
  BODY=${response%$'\nHTTP_STATUS:'*}
  echo "HTTP status: ${HTTP_STATUS}"
  echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
}

echo -e "\n${BLUE}Example 1: submit a Job manifest${NC}"
cat >"$PAYLOAD_FILE" <<'EOF'
{
  "manifests": [
    {
      "apiVersion": "batch/v1",
      "kind": "Job",
      "metadata": {
        "name": "curl-example-job",
        "namespace": "gco-jobs",
        "labels": {
          "app": "curl-example",
          "submitted-by": "curl-sigv4-proxy"
        }
      },
      "spec": {
        "template": {
          "spec": {
            "containers": [
              {
                "name": "example",
                "image": "busybox:1.38.0",
                "command": ["sh", "-c", "echo 'Hello from GCO!' && sleep 10"]
              }
            ],
            "restartPolicy": "Never"
          }
        },
        "backoffLimit": 2
      }
    }
  ]
}
EOF
jq '.' "$PAYLOAD_FILE"
perform_request \
  -X POST "${LOCAL_API_BASE}/api/v1/manifests" \
  -H "Host: ${API_HOST}" \
  -H "Content-Type: application/json" \
  --data-binary "@${PAYLOAD_FILE}"

if [[ "$HTTP_STATUS" != "200" ]]; then
  echo -e "${RED}Manifest submission failed${NC}" >&2
  exit 1
fi

echo -e "\n${BLUE}Example 2: get Job status${NC}"
perform_request \
  "${LOCAL_API_BASE}/api/v1/jobs/gco-jobs/curl-example-job" \
  -H "Host: ${API_HOST}"

echo -e "\n${BLUE}Example 3: list Jobs${NC}"
perform_request \
  "${LOCAL_API_BASE}/api/v1/jobs?namespace=gco-jobs&limit=20" \
  -H "Host: ${API_HOST}"

echo -e "\n${BLUE}Example 4: optional Job deletion${NC}"
read -r -p "Delete gco-jobs/curl-example-job? (y/N): " REPLY
if [[ "$REPLY" =~ ^[Yy]$ ]]; then
  perform_request \
    -X DELETE "${LOCAL_API_BASE}/api/v1/jobs/gco-jobs/curl-example-job" \
    -H "Host: ${API_HOST}"
else
  echo "Skipping deletion."
fi

echo -e "\n${BLUE}Example 5: verify unsigned requests are rejected${NC}"
perform_request "${API_ENDPOINT}/api/v1/jobs?limit=1"
if [[ "$HTTP_STATUS" == "403" ]]; then
  echo -e "${GREEN}Unsigned request correctly rejected.${NC}"
else
  echo -e "${YELLOW}Expected HTTP 403, received ${HTTP_STATUS}.${NC}"
fi

echo -e "\n${BLUE}=== Examples complete ===${NC}"
echo "The local URL includes the API Gateway stage path (${API_STAGE_PATH})."
echo "aws-sigv4-proxy signed requests with the active AWS credential chain."
