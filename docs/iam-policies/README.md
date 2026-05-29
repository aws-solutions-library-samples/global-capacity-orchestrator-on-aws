# IAM Policy Examples for GCO API Gateway

This directory contains example IAM policies for controlling access to the GCO manifest submission API through AWS API Gateway.

## Table of Contents

- [Policy Templates](#policy-templates)
- [Getting API Gateway Information](#getting-api-gateway-information)
- [Applying Policies](#applying-policies)
- [Multi-Region Considerations](#multi-region-considerations)
- [Testing Permissions](#testing-permissions)
- [Troubleshooting](#troubleshooting)
- [Security Best Practices](#security-best-practices)

## Policy Templates

### 1. Full Access Policy ([full-access-policy.json](full-access-policy.json))

Grants complete access to all manifest operations including:

- Submit new manifests (POST)
- List manifests (GET)
- Get manifest status (GET)
- Delete manifests (DELETE)

**Use case**: Platform administrators, CI/CD pipelines with full deployment permissions

**How to use**:

1. Replace `ACCOUNT_ID` with your AWS account ID
2. Replace `API_ID` with your API Gateway ID (from CloudFormation outputs)
3. Attach to IAM users or roles that need full manifest management access

### 2. Read-Only Policy ([read-only-policy.json](read-only-policy.json))

Grants read-only access to manifest operations:

- List manifests (GET)
- Get manifest status (GET)

**Use case**: Monitoring tools, read-only users, audit systems

**How to use**:

1. Replace `ACCOUNT_ID` with your AWS account ID
2. Replace `API_ID` with your API Gateway ID (from CloudFormation outputs)
3. Attach to IAM users or roles that only need to query manifest status

### 3. Namespace-Restricted Policy ([namespace-restricted-policy.json](namespace-restricted-policy.json))

Grants access to manifest operations within a specific namespace only:

- Submit manifests to specific namespace
- List manifests in specific namespace
- Get manifest status in specific namespace
- Delete manifests in specific namespace

**Use case**: Team-specific access, multi-tenant environments, least-privilege access

**How to use**:

1. Replace `ACCOUNT_ID` with your AWS account ID
2. Replace `API_ID` with your API Gateway ID (from CloudFormation outputs)
3. Replace `NAMESPACE` with the Kubernetes namespace (e.g., `my-team-namespace`)
4. Attach to IAM users or roles that should only access specific namespaces

## Getting API Gateway Information

To find your API Gateway ID and construct the correct ARNs:

```bash
# Get API Gateway ID from CloudFormation
aws cloudformation describe-stacks \
  --stack-name gco-regional-us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiGatewayId`].OutputValue' \
  --output text

# Get your AWS Account ID
aws sts get-caller-identity --query Account --output text
```

## Applying Policies

### To an IAM User

```bash
# Create the policy
aws iam create-policy \
  --policy-name GCOManifestFullAccess \
  --policy-document file://full-access-policy.json

# Attach to user
aws iam attach-user-policy \
  --user-name my-user \
  --policy-arn arn:aws:iam::ACCOUNT_ID:policy/GCOManifestFullAccess
```

### To an IAM Role

```bash
# Attach to role (e.g., for EC2 instances or Lambda functions)
aws iam attach-role-policy \
  --role-name my-role \
  --policy-arn arn:aws:iam::ACCOUNT_ID:policy/GCOManifestFullAccess
```

### Inline Policy (for testing)

```bash
# Put inline policy directly on user
aws iam put-user-policy \
  --user-name my-user \
  --policy-name GCOManifestAccess \
  --policy-document file://full-access-policy.json
```

## Multi-Region Considerations

If you have API Gateways deployed in multiple regions, you can use wildcards in the resource ARN:

```json
"Resource": "arn:aws:execute-api:*:ACCOUNT_ID:*/*/POST/api/v1/manifests"
```

Or specify regions explicitly:

```json
"Resource": [
  "arn:aws:execute-api:us-east-1:ACCOUNT_ID:API_ID/*/POST/api/v1/manifests",
  "arn:aws:execute-api:us-west-2:ACCOUNT_ID:API_ID/*/POST/api/v1/manifests"
]
```

## Testing Permissions

After applying a policy, test access:

```bash
# Test with AWS CLI (uses your current credentials)
aws apigateway test-invoke-method \
  --rest-api-id API_ID \
  --resource-id RESOURCE_ID \
  --http-method POST \
  --path-with-query-string "/api/v1/manifests"
```

Or use the client examples in `docs/client-examples/` to test with real requests.

## Troubleshooting

### "User is not authorized to access this resource"

- Verify the policy is attached to your user/role
- Check that ACCOUNT_ID and API_ID are correct
- Ensure the resource ARN matches the endpoint you're calling
- Check CloudWatch Logs for detailed authorization failures

### "Missing Authentication Token"

- This means the request is not signed with AWS SigV4
- Use AWS SDK or CLI which automatically signs requests
- See client examples for proper request signing

## Security Best Practices

1. **Principle of Least Privilege**: Start with read-only or namespace-restricted policies
2. **Use Roles for Applications**: Prefer IAM roles over user credentials for applications
3. **Rotate Credentials**: Regularly rotate IAM user access keys
4. **Monitor Access**: Enable CloudTrail and review API Gateway access logs
5. **Namespace Isolation**: Use namespace-restricted policies for multi-tenant scenarios
