# Backend TLS Certificate Manager

Creates and rotates the deployment-local private certificate authority (CA) and the regional AWS Certificate Manager (ACM) certificates used by GCO's HTTPS-only backend path. The function runs as a container-image [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) and is invoked both by a [CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) custom resource and an [EventBridge](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-what-is.html) schedule.

## Table of Contents

- [Responsibilities](#responsibilities)
- [Lifecycle](#lifecycle)
  - [Create and Update](#create-and-update)
  - [Scheduled Reconciliation](#scheduled-reconciliation)
  - [Root Rollover](#root-rollover)
  - [Delete](#delete)
- [Security Model](#security-model)
- [Certificate Identity](#certificate-identity)
- [Configuration](#configuration)
  - [Environment Variables](#environment-variables)
  - [Custom Resource Properties](#custom-resource-properties)
- [Published State](#published-state)
- [Metrics and Alarms](#metrics-and-alarms)
- [IAM Permissions](#iam-permissions)
- [Packaging](#packaging)
- [Failure Behavior](#failure-behavior)
- [Recovery Runbook](#recovery-runbook)

## Responsibilities

The manager:

1. Bootstraps an ECDSA P-256 root CA for one GCO deployment.
2. Stores root state only in a customer-managed-KMS-encrypted [Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) secret.
3. Publishes a public-only root trust bundle to the project's [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html) namespace.
4. Issues a unique short-lived ECDSA leaf certificate for every configured workload region.
5. Imports each leaf into ACM in its target region and records the ARN in global-region SSM.
6. Reimports renewed leaves into the existing ACM ARN so [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) certificate associations remain stable.
7. Stages root rollover so clients receive the next public root before any leaf starts using it.
8. Emits root and leaf expiry metrics for operational alarms.
9. Persists regions removed by an Update until their certificates and public
   ARN parameters are safely deleted, retrying while an ALB listener still
   uses a leaf.
10. Removes current and persisted-retired certificates plus public SSM
    parameters during ordered stack teardown.

## Lifecycle

### Create and Update

The [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) provider invokes `lambda_handler` with a CloudFormation `Create` or `Update` event. The handler validates every region, namespace, lifetime, and certificate identity before it mutates state. It then creates or loads the root, publishes the trust bundle, and ensures every regional ACM certificate exists and is current.

The custom resource uses a stable physical ID. Updating lifecycle policy therefore reconciles the existing PKI rather than replacing it. When `Regions` removes a workload region, the manager records that retirement in the encrypted root state before cleanup. If ACM reports that an ALB listener still uses the leaf, the public ARN parameter remains intact and scheduled reconciliation retries after listener teardown.

### Scheduled Reconciliation

EventBridge invokes the handler with `{"Action": "Rotate"}` on the configured schedule. Reconciliation renews leaves inside their rotate-before window, advances any staged root transition, retries persisted retired-region cleanup, republishes expiry metrics, and otherwise remains idempotent.

### Root Rollover

Root rollover is deliberately multi-phase:

1. Generate a pending root when the configured generation increases or the current root approaches expiry.
2. Publish the pending public certificate alongside the current root.
3. Wait for the configured activation delay so Lambda trust caches can refresh.
4. Promote the pending root and issue leaves from it.
5. Retain the previous public root through the configured overlap period.
6. Remove the previous root only after the overlap expires.

This prevents a leaf/root cutover from outrunning cached client trust. The overlap must exceed leaf validity, and configuration validation enforces that invariant.

### Delete

On a CloudFormation `Delete`, the manager deletes certificates and SSM ARN parameters for both the current `Regions` property and every persisted retired region, then deletes the public trust parameter. Regional stacks depend on the owning API stack, so their ALB listeners are removed before certificate cleanup. An unexpected `ResourceInUseException` fails Delete rather than abandoning an ARN with no future scheduler. The root secret and [KMS](https://docs.aws.amazon.com/kms/latest/developerguide/overview.html) key follow the removal policies defined by the CDK stack rather than being deleted directly by this handler.

## Security Model

- Root and leaf keys use ECDSA P-256.
- The root private key exists durably only inside the KMS-encrypted root secret.
- Leaf private keys exist only in Lambda memory long enough to call ACM `ImportCertificate`.
- SSM stores public certificates and ACM ARNs only.
- The handler never writes private key material to logs or `/tmp`.
- Namespace validation confines all SSM writes to `/<project>/backend-tls/`.
- Stored ACM ARNs are validated against the expected partition, account, and region before use.
- Certificates carry the server-auth extended key usage and the configured private DNS identity as a subject alternative name.
- Reconciliation is serialized by the infrastructure stack to prevent concurrent root transitions.

The HMAC request envelope is separate from TLS. TLS supplies transport confidentiality and endpoint authentication; HMAC supplies application-level integrity, freshness, and replay resistance.

## Certificate Identity

Every regional leaf represents one stable private identity:

```text
backend.<project>.gco.internal
```

Clients connect to dynamic [Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html) or internal-ALB DNS names but explicitly send and verify this identity through TLS SNI and hostname assertion. No public domain registration or public CA is required.

## Configuration

Defaults and cross-field validation are defined in `cdk.json` and `gco/config/config_loader.py`. The handler accepts the same policy through environment variables and custom-resource properties so scheduled and lifecycle invocations use one validation path.

### Environment Variables

| Variable | Required | Description |
|---|---:|---|
| `ROOT_SECRET_ARN` | Yes | Secrets Manager ARN containing root state and the root private key |
| `PROJECT_NAME` | Yes | Deployment namespace |
| `REGISTRY_REGION` | Yes | Region containing the public SSM registry |
| `CERTIFICATE_REGIONS` | Yes | JSON array of workload regions managed in ACM |
| `BACKEND_TLS_SERVER_NAME` | Yes | Stable private certificate identity |
| `ROOT_CA_PARAMETER_NAME` | Yes | Public trust-bundle SSM parameter |
| `CERTIFICATE_PARAMETER_PREFIX` | Yes | Prefix for per-region ACM ARN parameters |
| `ROOT_GENERATION` | Yes | Desired root generation; increasing it stages rollover |
| `ROOT_VALIDITY_DAYS` | Yes | Root certificate lifetime |
| `ROOT_ROTATE_BEFORE_DAYS` | Yes | Root renewal window |
| `ROOT_ACTIVATION_DELAY_HOURS` | Yes | Pending-root trust propagation delay |
| `ROOT_OVERLAP_DAYS` | Yes | Previous-root retention period after promotion |
| `LEAF_VALIDITY_DAYS` | Yes | Regional leaf lifetime |
| `LEAF_ROTATE_BEFORE_DAYS` | Yes | Regional leaf renewal window |
| `AWS_ACCOUNT_ID` | Yes | Account expected in managed ACM ARNs |
| `AWS_PARTITION` | Yes | Partition expected in managed ACM ARNs |

### Custom Resource Properties

| Property | Description |
|---|---|
| `Regions` | Workload regions receiving imported ACM certificates |
| `ServerName` | Private DNS identity placed in every leaf SAN |
| `ProjectName` | Deployment namespace |
| `RegistryRegion` | Region containing public SSM state |
| `RootCaParameterName` | Public trust-bundle parameter name |
| `CertificateParameterPrefix` | Prefix for regional certificate ARN parameters |
| `RootGeneration` | Desired root generation |
| `RootValidityDays` | Root lifetime |
| `RootRotateBeforeDays` | Root renewal window |
| `RootActivationDelayHours` | Delay before pending-root promotion |
| `RootOverlapDays` | Previous-root retention period |
| `LeafValidityDays` | Leaf lifetime |
| `LeafRotateBeforeDays` | Leaf renewal window |

## Published State

For a project named `gco`, public state uses these paths:

| State | Location | Sensitive |
|---|---|---:|
| Current, pending, overlapping roots, and retired-region cleanup state | Secrets Manager: `gco/backend-tls/root-ca` | Yes |
| Current, pending, and overlapping public roots | SSM: `/gco/backend-tls/root-ca.pem` | No |
| Stable regional ACM ARN | SSM: `/gco/backend-tls/certificate-arn/<region>` | No |
| Regional leaf certificate and private key | ACM in the workload region | ACM-managed |

The SSM trust bundle may contain multiple public roots during staged rollover. It must never contain a private-key PEM block.

## Metrics and Alarms

The function publishes to the `GCO/BackendTLS` [CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/WhatIsCloudWatch.html) namespace:

- `ReconciliationSuccess`, dimensioned by project. A missing heartbeat for two schedule intervals alarms even when EventBridge is disabled or no Lambda error metric is emitted.
- `RootCertificateDaysToExpiry`, dimensioned by project.
- `LeafCertificateDaysToExpiry`, dimensioned by project and region.

The owning CDK stack also alarms on a missing reconciliation heartbeat, root/leaf expiry thresholds, manager errors, and dead-letter queue messages. Metric publication is best effort; certificate reconciliation still fails on any state, Secrets Manager, SSM, or ACM error.

## IAM Permissions

The execution role is scoped for:

- Reading and updating the one root Secrets Manager secret.
- Using the root secret's customer-managed KMS key through Secrets Manager.
- Reading, publishing, and deleting the project's backend-TLS SSM parameters.
- Importing, reading, tagging, and deleting managed ACM certificates in configured regions.
- Publishing `GCO/BackendTLS` CloudWatch metrics.
- Writing Lambda logs and [X-Ray](https://docs.aws.amazon.com/xray/latest/devguide/aws-xray.html) traces.

Proxy roles cannot read the root secret or use its KMS key; they can read only the public trust parameter. The aggregator cannot read either root state or public private-root trust because its regional [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) hop uses the AWS-managed TLS chain.

## Packaging

`Dockerfile` builds from the AWS Lambda Python 3.14 base image, applies current Amazon Linux security updates, and installs exact dependency versions from `requirements.txt`:

- `boto3==1.43.59`
- `cryptography==49.0.0`

The container entry point is `handler.lambda_handler`. A container image is required because the Lambda managed runtime does not provide the pinned `cryptography` package.

## Failure Behavior

Configuration, malformed secret state, unsafe lifetime relationships, invalid ACM ARN ownership, certificate-signature mismatch, and issuance/import failures are fatal. A pending root's activation delay starts only after SSM successfully publishes a trust bundle containing it, so an SSM outage cannot silently consume the proxy propagation window. A retired certificate that is still attached is the deliberate exception: its ARN parameter and encrypted retirement record remain until a scheduled retry can safely remove both. CloudFormation or EventBridge retry behavior, the encrypted dead-letter queue, heartbeat/expiry metrics, and CloudWatch alarms surface failures. Expiry and heartbeat metric publication is the only best-effort operation because an observability outage must not interrupt an otherwise safe certificate rotation.

## Recovery Runbook

For alarm diagnosis, metadata-only Secrets Manager version recovery, public
trust and regional ACM/ALB validation, staged emergency generation bumps, and
irrecoverable-root escalation, follow
[Backend Certificate Rotation Fails](../../docs/RUNBOOKS.md#backend-certificate-rotation-fails),
including its [Root-State Recovery](../../docs/RUNBOOKS.md#root-state-recovery)
procedure. Never print or export private root material during diagnosis.
