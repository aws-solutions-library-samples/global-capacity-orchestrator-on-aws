#!/bin/bash
# Example: Submit Kubernetes manifests to GCO API Gateway using AWS CLI
#
# This script demonstrates how to interact with the GCO API Gateway
# using curl with AWS SigV4 signing.
#
# Requirements:
#   - AWS CLI v2 (for credentials)
#   - curl with --aws-sigv4 support (curl 7.75+)
#   - jq (for JSON processing)
#
# Usage:
#   ./aws_cli_examples.sh

set -e

# Configuration

# API Gateway region - reads from cdk.json or defaults to us-east-2
# You can override this by setting API_REGION environment variable
if [ -z "${API_REGION:-}" ]; then
    if [ -f "cdk.json" ]; then
        API_REGION=$(python3 -c "import json; d=json.load(open('cdk.json')); print(d.get('context',{}).get('deployment_regions',{}).get('api_gateway','us-east-2'))" 2>/dev/null || echo "us-east-2")
    else
        API_REGION="us-east-2"
    fi
fi

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== GCO API Gateway - AWS CLI Examples ===${NC}\n"

# Get API Gateway endpoint from CloudFormation
echo -e "${GREEN}Getting API Gateway endpoint from CloudFormation...${NC}"
# shellcheck disable=SC2016
API_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name "gco-api-gateway" \
  --region "$API_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiEndpoint`].OutputValue' \
  --output text)

if [ -z "$API_ENDPOINT" ]; then
  echo -e "${RED}Error: Could not find ApiEndpoint in stack gco-api-gateway${NC}"
  exit 1
fi

# Remove trailing slash if present
API_ENDPOINT="${API_ENDPOINT%/}"

echo -e "API Endpoint: ${API_ENDPOINT}\n"

# Example 1: Submit a simple Job manifest
echo -e "${GREEN}Example 1: Submit a simple Job manifest${NC}"

# The API expects 'manifests' as an array of JSON objects
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

echo "Manifest payload:"
echo "$MANIFEST_PAYLOAD" | jq '.'

echo -e "\nSubmitting manifest..."

# Use curl with AWS SigV4 signing
# Note: Requires curl 7.75+ for --aws-sigv4 support
RESPONSE=$(curl -s --aws-sigv4 "aws:amz:${API_REGION}:execute-api" \
  --user "$(aws configure get aws_access_key_id):$(aws configure get aws_secret_access_key)" \
  -X POST "${API_ENDPOINT}/api/v1/manifests" \
  -H "Content-Type: application/json" \
  -d "$MANIFEST_PAYLOAD")

echo -e "\nResponse:"
echo "$RESPONSE" | jq '.'

# Example 2: Submit a GPU Job with node selector
echo -e "\n${GREEN}Example 2: Submit a GPU Job with node selector${NC}"

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
                "resources": {
                  "limits": {
                    "nvidia.com/gpu": "1"
                  }
                }
              }
            ],
            "restartPolicy": "Never",
            "nodeSelector": {
              "karpenter.sh/capacity-type": "on-demand"
            },
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
  ]
}
EOF
)

echo "GPU Job payload:"
echo "$GPU_MANIFEST_PAYLOAD" | jq '.'

echo -e "\n${BLUE}To submit this GPU job, run:${NC}"
cat <<EOF
curl -s --aws-sigv4 "aws:amz:${API_REGION}:execute-api" \\
  --user "\$(aws configure get aws_access_key_id):\$(aws configure get aws_secret_access_key)" \\
  -X POST "${API_ENDPOINT}/api/v1/manifests" \\
  -H "Content-Type: application/json" \\
  -d '${GPU_MANIFEST_PAYLOAD}'
EOF

# Example 3: Submit multiple manifests at once
echo -e "\n${GREEN}Example 3: Submit multiple manifests at once${NC}"

MULTI_MANIFEST_PAYLOAD=$(cat <<'EOF'
{
  "manifests": [
    {
      "apiVersion": "v1",
      "kind": "ConfigMap",
      "metadata": {
        "name": "example-config",
        "namespace": "gco-jobs"
      },
      "data": {
        "config.yaml": "key: value\nother: setting"
      }
    },
    {
      "apiVersion": "batch/v1",
      "kind": "Job",
      "metadata": {
        "name": "config-reader-job",
        "namespace": "gco-jobs"
      },
      "spec": {
        "template": {
          "spec": {
            "containers": [
              {
                "name": "reader",
                "image": "busybox:1.38.0",
                "command": ["cat", "/config/config.yaml"],
                "volumeMounts": [
                  {
                    "name": "config-volume",
                    "mountPath": "/config"
                  }
                ]
              }
            ],
            "volumes": [
              {
                "name": "config-volume",
                "configMap": {
                  "name": "example-config"
                }
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

echo "Multiple manifests payload:"
echo "$MULTI_MANIFEST_PAYLOAD" | jq '.'

# Example 4: Check your IAM permissions
echo -e "\n${GREEN}Example 4: Verify your IAM permissions${NC}"
echo "Check which IAM principal you're using:"
aws sts get-caller-identity

echo -e "\n${BLUE}=== Examples Complete ===${NC}"
echo -e "\nKey points:"
echo "1. The API expects 'manifests' as an array of JSON objects (not YAML strings)"
echo "2. Each manifest must include apiVersion, kind, and metadata with name"
echo "3. Use --aws-sigv4 with curl for IAM authentication"
echo "4. For production use, see python_boto3_example.py for a more robust implementation"
