# Analytics Presigned URL Lambda

This [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) exchanges a Cognito-authorized [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) request for a short-lived
[SageMaker](https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html) Studio URL and lazily provisions the caller's Studio profile, whose
home directory is `/home/<username>` on the Studio [EFS](https://docs.aws.amazon.com/efs/latest/ug/whatisefs.html) file system.

## Table of Contents

- [Request Flow](#request-flow)
- [Environment](#environment)
- [Responses](#responses)
- [Security](#security)
- [Source and Dependencies](#source-and-dependencies)

## Request Flow

1. Read the [Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/what-is-amazon-cognito.html) username claim from the API Gateway authorizer context.
2. Read the Studio domain ID from `STUDIO_DOMAIN_ID`, which the stack always
   sets. There is no lookup fallback.
3. Describe the user's Studio profile and create it if it does not exist. A
   `Failed` profile is deleted and created again.
4. Return `202` while the profile was just created or is `Pending`,
   `Updating` or `Deleting`. Return `409` for a profile no poll will change:
   `Update_Failed`, `Delete_Failed`, or `Failed` with its delete refused (for
   example `AccessDeniedException` when the role lacks
   `sagemaker:DeleteUserProfile`).
5. Return a time-limited `CreatePresignedDomainUrl` result.

Earlier releases also created a per-user EFS access point at
`/home/<username>` before step 5. A profile's `EFSFileSystemConfig` takes
no access point, so nothing mounted through it, and the Lambda no longer
makes one; it creates no EFS client at all.

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
- `404`: `SagemakerDomainNotFound`, `STUDIO_DOMAIN_ID` is empty.
- `409`: `UserProfileFailed`, the profile is stuck in a failed state the
  Lambda cannot recover from; an operator must delete it (the next login
  creates it again). The CLI stops polling and reports the status.
- `500`: an opaque generation failure token; exception details remain in logs.

## Security

The handler trusts only API Gateway authorizer claims, returns no AWS exception
text, creates `0700` per-user EFS roots, and uses short-lived URLs. Its [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) role
must remain limited to the specific Studio domain, profiles, and EFS resources.

## Source and Dependencies

- [`handler.py`](handler.py) implements the Lambda entry point.
- [`requirements.txt`](requirements.txt) pins packaged dependencies.
- [Generated control-flow diagram](../../diagrams/code_diagrams/lambda/analytics-presigned-url/handler.lambda_handler.html)
