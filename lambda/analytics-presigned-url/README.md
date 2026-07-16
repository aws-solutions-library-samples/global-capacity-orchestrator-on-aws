# Analytics Presigned URL Lambda

This Lambda exchanges a Cognito-authorized API Gateway request for a short-lived
SageMaker Studio URL and lazily provisions the caller's Studio profile and EFS
home-directory access point.

## Table of Contents

- [Request Flow](#request-flow)
- [Environment](#environment)
- [Responses](#responses)
- [Security](#security)
- [Source and Dependencies](#source-and-dependencies)

## Request Flow

1. Read the Cognito username claim from the API Gateway authorizer context.
2. Resolve the configured SageMaker Studio domain.
3. describe, create, or recover the user's Studio profile.
4. Return `202` while profile provisioning is in progress.
5. Ensure a deterministic per-user EFS access point.
6. Return a time-limited `CreatePresignedDomainUrl` result.

## Environment

| Variable | Purpose |
|---|---|
| `STUDIO_DOMAIN_ID` | SageMaker Studio domain identifier |
| `SAGEMAKER_EXECUTION_ROLE_ARN` | Execution role assigned to new profiles |
| `STUDIO_EFS_ID` | EFS file system for per-user home directories |
| `URL_EXPIRES_SECONDS` | Presigned URL lifetime; default `300` |
| `SESSION_EXPIRES_SECONDS` | Studio session lifetime; default `43200` |

## Responses

- `200`: URL is ready, with `url` and `expires_in`.
- `202`: the user profile is still provisioning and the client should poll.
- `401`: no usable Cognito username claim.
- `404`: the Studio domain could not be resolved.
- `500`: an opaque generation failure token; exception details remain in logs.

## Security

The handler trusts only API Gateway authorizer claims, returns no AWS exception
text, creates `0700` per-user EFS roots, and uses short-lived URLs. Its IAM role
must remain limited to the specific Studio domain, profiles, and EFS resources.

## Source and Dependencies

- [`handler.py`](handler.py) implements the Lambda entry point.
- [`requirements.txt`](requirements.txt) pins packaged dependencies.
- [Generated control-flow diagram](../../diagrams/code_diagrams/lambda/analytics-presigned-url/handler.lambda_handler.html)
