# Secret Rotation

Rotates the GCO backend HMAC signing key in AWS Secrets Manager. The key is used to sign exact proxy-to-backend requests and is never sent as a reusable credential. The Lambda follows the standard four-step Secrets Manager rotation protocol.

## Table of Contents

- [Trigger](#trigger)
- [How It Works](#how-it-works)
- [IAM Permissions](#iam-permissions)
- [Dependencies](#dependencies)

## Trigger

Secrets Manager automatic rotation (daily schedule).

## How It Works

Implements the 4-step rotation protocol:

1. **createSecret** — Generates a new 64-character alphanumeric signing key and stores it as `AWSPENDING`
2. **setSecret** — No-op (no external system to update; proxies and services read Secrets Manager directly)
3. **testSecret** — Validates that the pending key can be retrieved and has the expected structure and length
4. **finishSecret** — Atomically moves `AWSPENDING` to `AWSCURRENT`

Multi-region replication distributes the new key automatically. Proxies and services accept both `AWSCURRENT` and `AWSPENDING` during the rotation window for zero downtime; backend requests carry only short-lived HMAC envelopes, never the key itself.

## Input

Secrets Manager rotation event (`SecretId`, `ClientRequestToken`, `Step`).

## Output

None (raises on failure).

## IAM Permissions

- `secretsmanager:GetSecretValue` on the secret
- `secretsmanager:PutSecretValue` on the secret
- `secretsmanager:DescribeSecret` on the secret
- `secretsmanager:UpdateSecretVersionStage` on the secret

## Dependencies

- `boto3` (see `requirements.txt`)
