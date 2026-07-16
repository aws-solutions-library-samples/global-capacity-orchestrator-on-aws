#!/bin/bash
# Example: submit Kubernetes manifests to the GCO API Gateway with curl SigV4.
#
# Requirements:
#   - AWS CLI v2 (including `aws configure export-credentials`)
#   - curl 7.75+ with --aws-sigv4 support
#   - jq
#
# The AWS CLI credential provider chain is authoritative. AWS_PROFILE, SSO,
# role assumption, web identity, environment credentials, and instance/container
# roles are all supported; this script never reads static keys from config files.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/../.." && pwd)

for command_name in aws curl jq; do
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

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}=== GCO API Gateway - curl SigV4 examples ===${NC}\n"

echo -e "${GREEN}Using the active AWS identity:${NC}"
aws sts get-caller-identity

echo -e "\n${GREEN}Getting ApiEndpoint from ${STACK_NAME} in ${API_REGION}...${NC}"
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

echo "API endpoint: ${API_ENDPOINT}"

# Export the resolved credentials rather than reading static profile fields.
# This preserves session tokens and works with profiles that assume roles or
# source credentials from SSO, web identity, ECS, or EC2 metadata.
CREDENTIALS_JSON=$(aws configure export-credentials --format process)
ACCESS_KEY_ID=$(jq -er '.AccessKeyId' <<<"$CREDENTIALS_JSON")
SECRET_ACCESS_KEY=$(jq -er '.SecretAccessKey' <<<"$CREDENTIALS_JSON")
SESSION_TOKEN=$(jq -r '.SessionToken // empty' <<<"$CREDENTIALS_JSON")

SIGV4_ARGS=(
  --aws-sigv4 "aws:amz:${API_REGION}:execute-api"
  --user "${ACCESS_KEY_ID}:${SECRET_ACCESS_KEY}"
)
if [[ -n "$SESSION_TOKEN" ]]; then
  SIGV4_ARGS+=(--header "X-Amz-Security-Token: ${SESSION_TOKEN}")
fi

signed_curl() {
  curl -sS "${SIGV4_ARGS[@]}" "$@"
}

echo -e "\n${GREEN}Example 1: submit a Job manifest${NC}"
MANIFEST_PAYLOAD=$(cat <<'EOF'
{
  "manifests": [
    {
      "apiVersion": "batch/v1",
      "kind": "Job",
      "metadata": {
        "name": "example-job",
        "namespace": "gco-jobs"
      },
      "spec": {
        "template": {
          "spec": {
            "containers": [
              {
                "name": "example",
                "image": "busybox:1.38.0",
                "command": ["echo", "Hello from GCO!"]
              }
            ],
            "restartPolicy": "Never"
          }
        },
        "backoffLimit": 3
      }
    }
  ]
}
EOF
)

echo "$MANIFEST_PAYLOAD" | jq '.'
RESPONSE=$(signed_curl \
  -X POST "${API_ENDPOINT}/api/v1/manifests" \
  -H "Content-Type: application/json" \
  --data "$MANIFEST_PAYLOAD")
echo "$RESPONSE" | jq '.'

echo -e "\n${GREEN}Example 2: list Jobs in gco-jobs${NC}"
signed_curl \
  --get "${API_ENDPOINT}/api/v1/jobs" \
  --data-urlencode "namespace=gco-jobs" \
  --data-urlencode "limit=20" | jq '.'

echo -e "\n${GREEN}Example 3: inspect the submitted Job${NC}"
signed_curl "${API_ENDPOINT}/api/v1/jobs/gco-jobs/example-job" | jq '.'

echo -e "\n${GREEN}Example 4: validate a GPU Job without applying it${NC}"
GPU_MANIFEST_PAYLOAD=$(cat <<'EOF'
{
  "manifests": [
    {
      "apiVersion": "batch/v1",
      "kind": "Job",
      "metadata": {
        "name": "gpu-example-job",
        "namespace": "gco-jobs"
      },
      "spec": {
        "template": {
          "spec": {
            "containers": [
              {
                "name": "gpu-example",
                "image": "nvidia/cuda:12.0-base",
                "command": ["nvidia-smi"],
                "resources": {"limits": {"nvidia.com/gpu": "1"}}
              }
            ],
            "restartPolicy": "Never",
            "nodeSelector": {"karpenter.sh/capacity-type": "on-demand"},
            "tolerations": [
              {
                "key": "nvidia.com/gpu",
                "operator": "Exists",
                "effect": "NoSchedule"
              }
            ]
          }
        },
        "backoffLimit": 3
      }
    }
  ],
  "dry_run": true
}
EOF
)

signed_curl \
  -X POST "${API_ENDPOINT}/api/v1/manifests" \
  -H "Content-Type: application/json" \
  --data "$GPU_MANIFEST_PAYLOAD" | jq '.'

echo -e "\n${BLUE}=== Examples complete ===${NC}"
echo "The API expects a 'manifests' array of JSON objects."
echo "Requests were signed with the active AWS CLI credential chain, including any session token."
