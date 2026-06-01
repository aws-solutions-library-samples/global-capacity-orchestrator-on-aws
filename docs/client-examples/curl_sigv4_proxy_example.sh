#!/bin/bash
# Example: Submit Kubernetes manifests using curl with aws-sigv4-proxy
#
# This script demonstrates how to use aws-sigv4-proxy to automatically sign
# requests with AWS SigV4, allowing you to use curl for API Gateway requests.
#
# Requirements:
#   - aws-sigv4-proxy (https://github.com/awslabs/aws-sigv4-proxy)
#   - curl
#   - jq
#
# Installation:
#   # macOS
#   brew install aws-sigv4-proxy
#
#   # Linux (download from GitHub releases)
#   wget https://github.com/awslabs/aws-sigv4-proxy/releases/latest/download/aws-sigv4-proxy-linux-amd64
#   chmod +x aws-sigv4-proxy-linux-amd64
#   sudo mv aws-sigv4-proxy-linux-amd64 /usr/local/bin/aws-sigv4-proxy
#
# Usage:
#   ./curl_sigv4_proxy_example.sh

set -e

# Configuration
REGION="us-east-1"
STACK_NAME="gco-regional-${REGION}"
PROXY_PORT="8080"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}=== GCO API Gateway - curl with aws-sigv4-proxy ===${NC}\n"

# Check if aws-sigv4-proxy is installed
if ! command -v aws-sigv4-proxy &> /dev/null; then
  echo -e "${RED}Error: aws-sigv4-proxy is not installed${NC}"
  echo "Install it with: brew install aws-sigv4-proxy"
  echo "Or download from: https://github.com/awslabs/aws-sigv4-proxy/releases"
  exit 1
fi

# Get API Gateway endpoint
echo -e "${GREEN}Getting API Gateway endpoint from CloudFormation...${NC}"
# shellcheck disable=SC2016
API_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiGatewayEndpoint`].OutputValue' \
  --output text)

if [ -z "$API_ENDPOINT" ]; then
  echo -e "${RED}Error: Could not find ApiGatewayEndpoint in stack $STACK_NAME${NC}"
  exit 1
fi

# Extract host from endpoint
API_HOST=$(echo "$API_ENDPOINT" | sed 's|https://||' | sed 's|http://||' | cut -d'/' -f1)
API_ID=$(echo "$API_HOST" | cut -d'.' -f1)

echo -e "API Endpoint: ${API_ENDPOINT}"
echo -e "API Host: ${API_HOST}"
echo -e "API ID: ${API_ID}\n"

# Check if proxy is already running
if lsof -Pi :"$PROXY_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo -e "${YELLOW}Warning: Port $PROXY_PORT is already in use${NC}"
  echo "If aws-sigv4-proxy is already running, we'll use it."
  echo "Otherwise, stop the process using port $PROXY_PORT and try again."
  PROXY_RUNNING=true
else
  PROXY_RUNNING=false
fi

# Start aws-sigv4-proxy if not running
if [ "$PROXY_RUNNING" = false ]; then
  echo -e "${GREEN}Starting aws-sigv4-proxy...${NC}"
  aws-sigv4-proxy \
    --name execute-api \
    --region "$REGION" \
    --port "$PROXY_PORT" \
    --upstream-url-scheme https \
    --log-level info &
  
  PROXY_PID=$!
  echo "Proxy PID: $PROXY_PID"
  
  # Wait for proxy to start
  echo "Waiting for proxy to start..."
  sleep 2
  
  # Cleanup function
  cleanup() {
    echo -e "\n${GREEN}Stopping aws-sigv4-proxy...${NC}"
    kill "$PROXY_PID" 2>/dev/null || true
  }
  trap cleanup EXIT
fi

echo -e "${GREEN}Proxy is ready!${NC}\n"

# Example 1: Submit a manifest
echo -e "${BLUE}=== Example 1: Submit a Manifest ===${NC}\n"

cat > /tmp/manifest-payload.json <<'EOF'
{
  "manifest": {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {
      "name": "curl-example-job",
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
              "command": ["sh", "-c", "echo 'Hello from curl + aws-sigv4-proxy!' && sleep 10"]
            }
          ],
          "restartPolicy": "Never"
        }
      },
      "backoffLimit": 2
    }
  },
  "namespace": "gco-jobs"
}
EOF

echo "Manifest payload:"
jq '.' < /tmp/manifest-payload.json

echo -e "\nSubmitting manifest via POST /api/v1/manifests..."
RESPONSE=$(curl -s -X POST "http://localhost:${PROXY_PORT}/api/v1/manifests" \
  -H "Content-Type: application/json" \
  -H "Host: ${API_HOST}" \
  -d @/tmp/manifest-payload.json \
  -w "\nHTTP_STATUS:%{http_code}")

HTTP_STATUS=$(echo "$RESPONSE" | grep "HTTP_STATUS" | cut -d':' -f2)
BODY=$(echo "$RESPONSE" | sed '/HTTP_STATUS/d')

echo -e "\nHTTP Status: ${HTTP_STATUS}"
echo "Response:"
echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"

if [ "$HTTP_STATUS" = "200" ] || [ "$HTTP_STATUS" = "201" ]; then
  echo -e "${GREEN}✓ Manifest submitted successfully!${NC}\n"
else
  echo -e "${RED}✗ Failed to submit manifest${NC}\n"
fi

# Example 2: Get manifest status
echo -e "${BLUE}=== Example 2: Get Manifest Status ===${NC}\n"

NAMESPACE="gco-jobs"
MANIFEST_NAME="curl-example-job"

echo "Getting status for manifest: ${NAMESPACE}/${MANIFEST_NAME}"
RESPONSE=$(curl -s -X GET "http://localhost:${PROXY_PORT}/api/v1/manifests/${NAMESPACE}/${MANIFEST_NAME}" \
  -H "Host: ${API_HOST}" \
  -w "\nHTTP_STATUS:%{http_code}")

HTTP_STATUS=$(echo "$RESPONSE" | grep "HTTP_STATUS" | cut -d':' -f2)
BODY=$(echo "$RESPONSE" | sed '/HTTP_STATUS/d')

echo "HTTP Status: ${HTTP_STATUS}"
echo "Response:"
echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"

if [ "$HTTP_STATUS" = "200" ]; then
  echo -e "${GREEN}✓ Retrieved manifest status successfully!${NC}\n"
else
  echo -e "${YELLOW}Note: Manifest may not exist yet or may have been processed${NC}\n"
fi

# Example 3: List all manifests
echo -e "${BLUE}=== Example 3: List All Manifests ===${NC}\n"

echo "Listing all manifests..."
RESPONSE=$(curl -s -X GET "http://localhost:${PROXY_PORT}/api/v1/manifests" \
  -H "Host: ${API_HOST}" \
  -w "\nHTTP_STATUS:%{http_code}")

HTTP_STATUS=$(echo "$RESPONSE" | grep "HTTP_STATUS" | cut -d':' -f2)
BODY=$(echo "$RESPONSE" | sed '/HTTP_STATUS/d')

echo "HTTP Status: ${HTTP_STATUS}"
echo "Response:"
echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"

if [ "$HTTP_STATUS" = "200" ]; then
  echo -e "${GREEN}✓ Retrieved manifest list successfully!${NC}\n"
fi

# Example 4: Delete a manifest (optional)
echo -e "${BLUE}=== Example 4: Delete a Manifest (Optional) ===${NC}\n"

read -p "Do you want to delete the manifest '${MANIFEST_NAME}'? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  echo "Deleting manifest: ${NAMESPACE}/${MANIFEST_NAME}"
  RESPONSE=$(curl -s -X DELETE "http://localhost:${PROXY_PORT}/api/v1/manifests/${NAMESPACE}/${MANIFEST_NAME}" \
    -H "Host: ${API_HOST}" \
    -w "\nHTTP_STATUS:%{http_code}")
  
  HTTP_STATUS=$(echo "$RESPONSE" | grep "HTTP_STATUS" | cut -d':' -f2)
  BODY=$(echo "$RESPONSE" | sed '/HTTP_STATUS/d')
  
  echo "HTTP Status: ${HTTP_STATUS}"
  echo "Response:"
  echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
  
  if [ "$HTTP_STATUS" = "200" ]; then
    echo -e "${GREEN}✓ Manifest deleted successfully!${NC}\n"
  fi
else
  echo "Skipping deletion."
fi

# Example 5: Test authentication failure
echo -e "${BLUE}=== Example 5: Test Authentication (without proxy) ===${NC}\n"

echo "Attempting request without AWS SigV4 signing (should fail with 403)..."
RESPONSE=$(curl -s -X GET "${API_ENDPOINT}/api/v1/manifests" \
  -w "\nHTTP_STATUS:%{http_code}")

HTTP_STATUS=$(echo "$RESPONSE" | grep "HTTP_STATUS" | cut -d':' -f2)
BODY=$(echo "$RESPONSE" | sed '/HTTP_STATUS/d')

echo "HTTP Status: ${HTTP_STATUS}"
echo "Response:"
echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"

if [ "$HTTP_STATUS" = "403" ]; then
  echo -e "${GREEN}✓ Authentication correctly required (403 Forbidden)${NC}\n"
else
  echo -e "${YELLOW}Unexpected status code${NC}\n"
fi

# Cleanup
rm -f /tmp/manifest-payload.json

echo -e "${BLUE}=== Examples Complete ===${NC}\n"
echo "Key takeaways:"
echo "1. aws-sigv4-proxy automatically signs requests with your AWS credentials"
echo "2. You can use curl normally through the proxy (localhost:${PROXY_PORT})"
echo "3. Always include the 'Host' header with the actual API Gateway host"
echo "4. Requests without SigV4 signing are rejected with 403 Forbidden"
echo ""
echo "For production use, consider using the Python boto3 example instead."
