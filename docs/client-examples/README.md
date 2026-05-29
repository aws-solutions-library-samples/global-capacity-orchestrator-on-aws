# Client Examples for GCO API Gateway

This directory contains working examples for interacting with the GCO API Gateway using various tools and programming languages.

## Table of Contents

- [Overview](#overview)
- [Available Examples](#available-examples)
- [Getting Started](#getting-started)
- [API Endpoints](#api-endpoints)
- [Authentication Errors](#authentication-errors)
- [Troubleshooting](#troubleshooting)
- [Best Practices](#best-practices)
- [Additional Resources](#additional-resources)

## Overview

The GCO API Gateway requires AWS IAM authentication using AWS Signature Version 4 (SigV4). All requests must be signed with valid AWS credentials that have the appropriate `execute-api:Invoke` permissions.

## Available Examples

### 1. Python with boto3 ([python_boto3_example.py](python_boto3_example.py))

**Recommended for production use**

A complete Python example using boto3 and the `aws-requests-auth` library to automatically sign requests.

**Requirements:**

```bash
pip install boto3 requests aws-requests-auth
```

**Usage:**

```bash
python python_boto3_example.py
```

**Features:**

- Automatically retrieves API Gateway endpoint from CloudFormation
- Uses your AWS credentials (from environment, ~/.aws/credentials, or IAM role)
- Supports temporary credentials (STS, IAM roles)
- Includes examples for all operations: submit, get, list, delete

**When to use:**

- Production applications
- CI/CD pipelines
- Automated workflows
- When you need programmatic access

### 2. AWS CLI ([aws_cli_examples.sh](aws_cli_examples.sh))

Examples using AWS CLI and curl with manual SigV4 signing.

**Requirements:**

```bash
# AWS CLI v2
brew install awscli

# jq for JSON processing
brew install jq
```

**Usage:**

```bash
./aws_cli_examples.sh
```

**Features:**

- Shows how to get API Gateway endpoint from CloudFormation
- Demonstrates curl command structure
- Includes IAM permission checking
- Useful for understanding the API structure

**When to use:**

- Quick testing and debugging
- Learning how the API works
- Checking IAM permissions
- One-off manual operations

### 3. curl with aws-sigv4-proxy ([curl_sigv4_proxy_example.sh](curl_sigv4_proxy_example.sh))

**Recommended for testing and development**

Uses `aws-sigv4-proxy` to automatically sign curl requests with AWS SigV4.

**Requirements:**

```bash
# macOS
brew install aws-sigv4-proxy

# Linux - download from GitHub
wget https://github.com/awslabs/aws-sigv4-proxy/releases/latest/download/aws-sigv4-proxy-linux-amd64
chmod +x aws-sigv4-proxy-linux-amd64
sudo mv aws-sigv4-proxy-linux-amd64 /usr/local/bin/aws-sigv4-proxy
```

**Usage:**

```bash
./curl_sigv4_proxy_example.sh
```

**Features:**

- Automatically starts and manages aws-sigv4-proxy
- Uses familiar curl syntax
- Interactive examples with all operations
- Shows authentication failure scenarios

**When to use:**

- Manual testing during development
- Debugging API issues
- When you prefer curl over Python
- Quick ad-hoc requests

## Getting Started

### Step 1: Ensure you have AWS credentials configured

```bash
# Check your current AWS identity
aws sts get-caller-identity

# Configure credentials if needed
aws configure
```

### Step 2: Verify IAM permissions

Your IAM user or role needs the `execute-api:Invoke` permission. See `../iam-policies/` for example policies.

```bash
# Check if you have the required permissions
aws iam simulate-principal-policy \
  --policy-source-arn $(aws sts get-caller-identity --query Arn --output text) \
  --action-names execute-api:Invoke \
  --resource-arns "arn:aws:execute-api:*:*:*"
```

### Step 3: Get your API Gateway endpoint

```bash
# From CloudFormation (replace with your region)
aws cloudformation describe-stacks \
  --stack-name gco-regional-us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiGatewayEndpoint`].OutputValue' \
  --output text
```

### Step 4: Run an example

```bash
# Python example (recommended)
python python_boto3_example.py

# Or curl with aws-sigv4-proxy
./curl_sigv4_proxy_example.sh
```

## API Endpoints

All endpoints require AWS SigV4 authentication.

### POST /api/v1/manifests

Submit a new Kubernetes manifest

**Request:**

```json
{
  "manifest": {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "my-job"},
    "spec": {...}
  },
  "namespace": "gco-jobs"
}
```

**Response (200):**

```json
{
  "status": "success",
  "message": "Manifest submitted successfully",
  "resource": {...}
}
```

### GET /api/v1/manifests

List all submitted manifests

**Response (200):**

```json
{
  "manifests": [...]
}
```

### GET /api/v1/manifests/{namespace}/{name}

Get status of a specific manifest

**Response (200):**

```json
{
  "status": "success",
  "manifest": {...}
}
```

### DELETE /api/v1/manifests/{namespace}/{name}

Delete a specific manifest

**Response (200):**

```json
{
  "status": "success",
  "message": "Manifest deleted successfully"
}
```

## Authentication Errors

### 403 Forbidden - Missing Authentication Token

**Cause:** Request is not signed with AWS SigV4

**Solution:** Use one of the provided examples that automatically signs requests

### 403 Forbidden - Invalid signature

**Cause:** Request signature is incorrect or credentials are invalid

**Solution:**

- Verify your AWS credentials are valid: `aws sts get-caller-identity`
- Check that your system clock is synchronized (SigV4 is time-sensitive)
- Ensure you're using the correct region

### 403 Forbidden - User is not authorized

**Cause:** Your IAM user/role lacks `execute-api:Invoke` permission

**Solution:**

- Attach the appropriate IAM policy (see `../iam-policies/`)
- Verify permissions: `aws iam get-user-policy --user-name YOUR_USER --policy-name POLICY_NAME`

## Troubleshooting

### "Could not find ApiGatewayEndpoint in stack"

The CloudFormation stack may not be deployed yet, or the output name is different.

```bash
# List all outputs
aws cloudformation describe-stacks \
  --stack-name gco-regional-us-east-1 \
  --query 'Stacks[0].Outputs'
```

### "Connection refused" when using aws-sigv4-proxy

The proxy may not be running or port 8080 is in use.

```bash
# Check if port is in use
lsof -i :8080

# Start proxy manually
aws-sigv4-proxy --name execute-api --region us-east-1 --port 8080
```

### "ModuleNotFoundError: No module named 'aws_requests_auth'"

Install the required Python package:

```bash
pip install aws-requests-auth
```

### Requests are slow or timing out

- Check VPC Link health status
- Verify NLB target health
- Check CloudWatch Logs for API Gateway execution logs
- Ensure Kubernetes pods are running and healthy

## Best Practices

1. **Use Python boto3 for production**: Most reliable and maintainable
2. **Use aws-sigv4-proxy for testing**: Quick and easy for manual testing
3. **Store credentials securely**: Use IAM roles instead of access keys when possible
4. **Enable CloudWatch logging**: Monitor API Gateway access logs for debugging
5. **Handle errors gracefully**: Implement retry logic with exponential backoff
6. **Use temporary credentials**: Prefer STS temporary credentials over long-lived access keys

## Additional Resources

- [AWS Signature Version 4 Documentation](https://docs.aws.amazon.com/general/latest/gr/signature-version-4.html)
- [API Gateway IAM Authentication](https://docs.aws.amazon.com/apigateway/latest/developerguide/permissions.html)
- [aws-sigv4-proxy GitHub](https://github.com/awslabs/aws-sigv4-proxy)
- [IAM Policy Examples](../iam-policies/README.md)

## Support

For issues or questions:

1. Check CloudWatch Logs for API Gateway execution logs
2. Verify IAM permissions using `aws iam simulate-principal-policy`
3. Test with the curl example to isolate client vs. server issues
4. Review the main README for migration guides and troubleshooting
